"""Fixed, reviewed, zero-cost recipes over private snapshots; no project code execution."""

import hashlib
import http.client
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select

from . import execution, patches
from .command_schemas import Asset, Preview, Result, Run, Start, Stop, Workspace
from .core import ROOT
from .db import CommandAsset, CommandRun, PatchAsset, PatchRun
from .patch_files import Files, identity, workspace_lock
from .requirements import audit, locked, mutate, project, timestamp
from .scanner import BusinessError, digest

ACTIVE = {"queued", "starting", "running", "serving"}
RUNNER = ROOT / "backend/app/recipe_runner.py"
OUTPUT_LIMIT = 16384
RECIPES = {
    "python_syntax": {
        "id": "python_syntax",
        "name": "Python 语法检查",
        "description": "真实 Python 子进程只解析所选代码，产生逐文件语法结果",
        "limitations": "不导入、不执行项目代码；不能代替单元测试、类型检查或功能验收",
        "supported": True,
    },
    "json_validate": {
        "id": "json_validate",
        "name": "JSON 格式检查",
        "description": "真实 Python 子进程校验所选 JSON，拒绝非标准常量",
        "limitations": "只检查格式，不能代替业务字段或数据库验收",
        "supported": True,
    },
    "static_preview": {
        "id": "static_preview",
        "name": "HTML/CSS 静态预览",
        "description": "仅提供已选 HTML/CSS 快照，监听本机指定端口，最长 10 分钟",
        "limitations": "禁用 JavaScript、外部连接与表单；不能代替动态应用验收",
        "supported": True,
    },
}
LIMITATIONS = [
    "只执行三种可信平台配方；项目 shell/npm/pytest、安装、发布和数据库变更为手动执行待核对。",
    "命令使用已审阅的私有文件快照；不执行项目程序，不继承用户环境，无模型调用或外发费用。",
    "每文件64KiB、总256KiB；stdout/stderr原文不保存，有限日志保留30天，结果资产长期保存。",
    "同项目操作保守串行；运行成功不表示阶段验收通过，后续文件变化需要复验。",
]


def rows(session, pid=None):
    stmt = select(CommandRun)
    if pid:
        stmt = stmt.where(CommandRun.project_id == pid)
    return list(session.scalars(stmt.order_by(CommandRun.created_at.desc(), CommandRun.id)))


def owned(session, pid, identifier, cls=CommandAsset):
    row = session.get(cls, identifier)
    if not row or row.project_id != pid:
        raise BusinessError("NOT_FOUND", "命令记录不属于此项目", 404)
    return row


def program():
    binary = Path(sys.executable).resolve(strict=True)
    metadata = binary.stat()
    raw = RUNNER.read_bytes()
    return {
        "binary": str(binary),
        "binary_identity": [metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns],
        "runner_hash": hashlib.sha256(raw).hexdigest(),
    }, raw


def available(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def prerequisites(session, settings, pid, asset, skip_run=None, occupancy=True):
    row = execution.owned(session, pid, asset.execution_id, "plan")
    if (
        execution.latest(session, pid, row.task_id, "plan").id != row.id
        or digest(row.payload) != asset.payload["execution_hash"]
    ):
        raise BusinessError("INPUT_STALE", "执行版本变化，请核对后另存命令预览", 409)
    execution.source(session, settings, pid, row.planning_id)
    text, inputs = row.payload["text"], row.payload["inputs"]
    ops = {s["operation"] for s in text["steps"]}
    if (
        "local_command" not in ops
        or ops - {"read_only", "code_write", "local_command"}
        or text["estimate_micro"]
    ):
        raise BusinessError("RECIPE_UNSUPPORTED", "需明确本地零费命令步骤；其他执行器为手动待核对", 409)
    if any(inputs[k] for k in ("task_dependencies", "stage_dependencies", "version_dependencies")):
        raise BusinessError("DEPENDENCIES_PENDING", "前置结果尚未验证，不能执行", 409)
    grant = execution.authorization(session, pid, row)
    if not grant:
        raise BusinessError("AUTHORIZATION_REQUIRED", "本地执行范围未确认或已撤销", 409)
    root = patches.safe_root(session, settings, pid)
    patches.target_scope(session, pid, root, [f["path"] for f in asset.payload["files"]])
    if str(root) != asset.payload["root"] or execution.source_file_issues(session, settings, pid, row):
        raise BusinessError("INPUT_STALE", "来源或显式文件已变化，需重新核对", 409)
    if program()[0] != asset.payload["program"]:
        raise BusinessError("RECIPE_CHANGED", "可信配方或 Python 环境变化，请重新预览", 409)
    with Files(root, [f["path"] for f in asset.payload["files"]]) as files:
        if files.root_identity != asset.payload["root_identity"]:
            raise BusinessError("FILE_CONFLICT", "项目目录被替换，已阻止", 409)
        for item in asset.payload["files"]:
            if (
                identity(os.fstat(files.parents[item["path"]])) != item["parent_identity"]
                or files.read(item["path"])[0] != item["content"]
            ):
                raise BusinessError("FILE_CONFLICT", "输入目录或文件发生变化，已阻止", 409)
    if occupancy:
        for run in rows(session):
            if (
                run.id != skip_run
                and (run.state in ACTIVE or run.details["error_code"] == "PROCESS_STOP_UNCONFIRMED")
                and session.get(CommandAsset, run.asset_id).payload["root_identity"]
                == asset.payload["root_identity"]
            ):
                raise BusinessError("WORKSPACE_BUSY", "本项目已有命令或服务运行，请先查询/停止", 409)
        for run in session.scalars(select(PatchRun).where(PatchRun.state.in_(patches.UNFINISHED))):
            if (
                session.get(PatchAsset, run.patch_id).payload["root_identity"]
                == asset.payload["root_identity"]
            ):
                raise BusinessError("RECOVERY_REQUIRED", "本项目补丁未结束或待恢复，不能运行命令", 409)
    return root, grant


def facts(asset):
    return [
        {
            "path": f["path"],
            "size": len(f["content"].encode()),
            "sha256": hashlib.sha256(f["content"].encode()).hexdigest(),
        }
        for f in asset.payload["files"]
    ]


def asset_public(session, settings, asset):
    issues = []
    try:
        prerequisites(session, settings, asset.project_id, asset)
        if any(r.asset_id == asset.id for r in rows(session, asset.project_id)):
            raise BusinessError("ALREADY_STARTED", "此预览已有运行，不重复执行；复验请另存新预览", 409)
        with workspace_lock(settings, Path(asset.payload["root"])):
            pass
        if asset.payload["recipe_id"] == "static_preview" and not available(asset.payload["port"]):
            raise BusinessError("PORT_IN_USE", "预览端口被占用，不会结束无关程序", 409)
    except (BusinessError, OSError) as exc:
        issues.append(exc.message if isinstance(exc, BusinessError) else "本机文件或配方暂不可核对")
    p = asset.payload
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "execution_id": asset.execution_id,
        "task_id": p["task_id"],
        "content_hash": digest(p),
        "created_at": timestamp(asset.created_at),
        "recipe": RECIPES[p["recipe_id"]],
        "step": p["step"],
        "argv": [p["program"]["binary"], "-I", "-B", "-S", "-u", "{私有运行目录}/runner.py", p["recipe_id"]],
        "files": facts(asset),
        "inputs": p["inputs"],
        "acceptance": p["acceptance"],
        "required_checks": p["required_checks"],
        "timeout_seconds": p["timeout_seconds"],
        "port": p["port"],
        "root": p["root"],
        "limitations": LIMITATIONS,
        "can_start": not issues,
        "blockers": issues,
    }


def run_public(row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "asset_id": row.asset_id,
        "state": row.state,
        "created_at": timestamp(row.created_at),
        "details": {**row.details, "logs": [], "log_status": "expired"}
        if row.state not in ACTIVE
        and row.created_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(days=30)
        else row.details,
    }


def preview(session, settings, pid, body):
    row = execution.selected_current(session, pid, body)
    execution.source(session, settings, pid, row.planning_id)
    if execution.source_file_issues(session, settings, pid, row):
        raise BusinessError("INPUT_STALE", "先核对并保存当前执行文件范围", 409)
    if any(name not in row.payload["text"]["files"] for name in body.files):
        raise BusinessError("PATH_FORBIDDEN", "命令输入必须属于当前执行文件范围", 409)
    candidates = [
        step
        for step in row.payload["text"]["steps"]
        if step["operation"] == "local_command" and (body.step_id is None or step["id"] == body.step_id)
    ]
    if len(candidates) != 1:
        raise BusinessError("STEP_REQUIRED", "需明确关联一个本地受控配方步骤", 409)
    suffixes = {"python_syntax": {".py"}, "json_validate": {".json"}, "static_preview": {".html", ".css"}}[
        body.recipe_id
    ]
    if any(Path(name).suffix.lower() not in suffixes for name in body.files):
        raise BusinessError("RECIPE_UNSUPPORTED", "所选文件格式不匹配配方")
    if body.recipe_id == "static_preview" and "index.html" not in body.files:
        raise BusinessError("INPUT_INVALID", "静态预览需明确选择根目录 index.html")
    root = patches.safe_root(session, settings, pid)
    patches.target_scope(session, pid, root, body.files)
    items = []
    with Files(root, body.files) as files:
        for name in body.files:
            content, _ = files.read(name)
            if content is None:
                raise BusinessError("FILE_UNAVAILABLE", "命令输入文件尚不存在", 409)
            items.append(
                {"path": name, "content": content, "parent_identity": identity(os.fstat(files.parents[name]))}
            )
        if sum(len(f["content"].encode()) for f in items) > 262144:
            raise BusinessError("INPUT_INVALID", "输入快照总量超过 256 KiB")
        payload = {
            "recipe_id": body.recipe_id,
            "step": candidates[0],
            "execution_hash": digest(row.payload),
            "task_id": row.task_id,
            "root": str(root),
            "root_identity": files.root_identity,
            "files": items,
            "inputs": row.payload["inputs"],
            "acceptance": row.payload["acceptance"],
            "required_checks": row.payload["required_checks"],
            "program": program()[0],
            "timeout_seconds": body.timeout_seconds,
            "port": body.port if body.recipe_id == "static_preview" else None,
        }
    asset = CommandAsset(id=str(uuid4()), project_id=pid, execution_id=row.id, payload=payload)
    session.add(asset)
    session.flush()
    audit(
        session,
        pid,
        "command_preview",
        {"asset_id": asset.id, "recipe_id": body.recipe_id, "file_count": len(items)},
    )
    return asset_public(session, settings, asset)


def log(details, message):
    if len(details["logs"]) < 50:
        details["logs"].append({"at": timestamp(), "message": message[:160]})


def persist(sessions, run_id, state, details):
    with locked(sessions) as session:
        row = session.get(CommandRun, run_id)
        # stop_requested may have been committed while this process was reading output.
        details["stop_requested"] = details["stop_requested"] or row.details["stop_requested"]
        row.state, row.details = state, details
        audit(
            session,
            row.project_id,
            "command_" + state,
            {"run_id": run_id, "recipe_id": details["recipe_id"], "exit_code": details["exit_code"]},
        )
    return details


def private_file(directory, name, raw):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def expire_logs(sessions):
    with locked(sessions) as session:
        for row in rows(session):
            if (
                row.state not in ACTIVE
                and row.created_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(days=30)
                and row.details["log_status"] != "expired"
            ):
                row.details = {**row.details, "logs": [], "log_status": "expired"}
                audit(session, row.project_id, "command_logs_expired", {"run_id": row.id})


def reconcile(sessions):
    with locked(sessions) as session:
        for row in rows(session):
            if row.state in ACTIVE:
                d = {
                    **row.details,
                    "health": "unknown",
                    "ended_at": timestamp(),
                    "error_code": "INTERRUPTED",
                    "message": "平台重启后运行失联，需核对；不自动重发、不按历史 PID 结束进程",
                }
                log(d, "平台重启，记录保留并标为待核对")
                row.state, row.details = "interrupted", d
                audit(session, row.project_id, "command_interrupted", {"run_id": row.id})


def terminate(process):
    def send(signum):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            # macOS can reject a group signal while its leader is exiting. Popen still
            # owns the unreaped child, so this fallback never targets a persisted PID.
            if process.poll() is None:
                try:
                    process.send_signal(signum)
                except (ProcessLookupError, PermissionError):
                    if process.poll() is None:
                        raise BusinessError(
                            "PROCESS_STOP_UNCONFIRMED", "本次进程停止未确认，需核对", 409
                        ) from None

    if process.poll() is None:
        send(signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise BusinessError("PROCESS_STOP_UNCONFIRMED", "本次进程停止未确认，需核对", 409) from None


def health(port, run_id, instance_id):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
    try:
        connection.request("GET", "/__factory_health")
        response = connection.getresponse()
        raw = response.read(1025)
        return (
            response.status == 200
            and len(raw) <= 1024
            and json.loads(raw) == {"run_id": run_id, "instance_id": instance_id}
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def event(raw, asset, details):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BusinessError("OUTPUT_INVALID", "配方输出格式无效，原始输出已过滤") from None
    if not isinstance(value, dict):
        raise BusinessError("OUTPUT_INVALID", "配方输出结构无效")
    kind = value.get("kind")
    if (
        asset.payload["recipe_id"] != "static_preview"
        and kind == "check"
        and set(value) == {"kind", "index", "valid", "line", "column", "error_code"}
    ):
        i = value["index"]
        if type(i) is not int or not 0 <= i < len(asset.payload["files"]) or type(value["valid"]) is not bool:
            raise BusinessError("OUTPUT_INVALID", "配方文件结果无效")
        if any(
            value[k] is not None and (type(value[k]) is not int or not 0 <= value[k] <= 1000000)
            for k in ("line", "column")
        ) or value["error_code"] not in {None, "PYTHON_SYNTAX", "JSON_SYNTAX", "INVALID_STRUCTURE"}:
            raise BusinessError("OUTPUT_INVALID", "配方结果类型无效")
        if (
            value["valid"]
            and (value["error_code"] is not None or value["line"] is not None or value["column"] is not None)
            or not value["valid"]
            and value["error_code"] is None
        ):
            raise BusinessError("OUTPUT_INVALID", "配方结果相互矛盾")
        if any(c["path"] == asset.payload["files"][i]["path"] for c in details["checks"]):
            raise BusinessError("OUTPUT_INVALID", "配方重复返回文件结果")
        details["checks"].append(
            {
                "path": asset.payload["files"][i]["path"],
                **{k: value[k] for k in ("valid", "line", "column", "error_code")},
            }
        )
        log(details, f"文件 {i + 1} 检查" + ("通过" if value["valid"] else "失败，位置见产物"))
    elif (
        asset.payload["recipe_id"] != "static_preview"
        and kind == "done"
        and set(value) == {"kind", "count"}
        and type(value["count"]) is int
        and value["count"] == len(asset.payload["files"])
    ):
        return "done"
    elif (
        asset.payload["recipe_id"] == "static_preview"
        and kind == "ready"
        and set(value) == {"kind", "port"}
        and type(value["port"]) is int
        and value["port"] == asset.payload["port"]
    ):
        return "ready"
    elif (
        kind == "error"
        and set(value) == {"kind", "code"}
        and value["code"] in {"PORT_IN_USE", "RECIPE_FAILED"}
    ):
        details["error_code"] = value["code"]
        log(
            details,
            "配方报告"
            + ("端口占用，无关进程保留" if value["code"] == "PORT_IN_USE" else "执行错误，原始输出已过滤"),
        )
    else:
        raise BusinessError("OUTPUT_INVALID", "配方返回未批准输出，已过滤")
    return None


def artifact(directory, asset, details):
    raw = json.dumps(
        {
            "checks": details["checks"],
            "input_files": facts(asset),
            "exit_code": details["exit_code"],
            "error_code": details["error_code"],
        },
        ensure_ascii=False,
    ).encode()
    private_file(directory, "result.json", raw)
    details["artifacts"] = [
        {"path": "result.json", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    ]


class Manager:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings
        self.shutdown = threading.Event()
        self.failure_code = None
        self.thread = threading.Thread(target=self.dispatch, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.shutdown.set()
        self.thread.join(timeout=8)

    def dispatch(self):
        futures: dict[str, Future] = {}
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                last_expiry = 0.0
                while not self.shutdown.is_set():
                    for identifier, completed in futures.items():
                        if completed.done() and completed.exception() is not None:
                            self.mark_unknown(identifier)
                    futures = {key: f for key, f in futures.items() if not f.done()}
                    if time.monotonic() - last_expiry > 60:
                        expire_logs(self.sessions)
                        last_expiry = time.monotonic()
                    with self.sessions() as session:
                        queued = [r.id for r in rows(session) if r.state == "queued"]
                    for identifier in queued:
                        if identifier not in futures and len(futures) < 3:
                            futures[identifier] = pool.submit(self.execute, identifier)
                    self.shutdown.wait(0.1)
        except Exception:
            self.failure_code = "STORAGE_UNAVAILABLE"

    def mark_unknown(self, run_id):
        # Observe worker failures; never abandon a persisted running state or replay it.
        with self.sessions() as session:
            row = session.get(CommandRun, run_id)
            if row.state not in ACTIVE:
                return
            details = dict(row.details)
        details.update(
            ended_at=timestamp(),
            health="unknown",
            error_code="PROCESS_STOP_UNCONFIRMED",
            message="执行器异常退出，进程停止未确认，请人工核对；不自动重发",
        )
        log(details, "异常结果已保留，未确认停止时不释放本项目执行占用")
        persist(self.sessions, run_id, "interrupted", details)

    def execute(self, run_id):
        process = None
        details = None
        directory = None
        try:
            with self.sessions() as session:
                run = session.get(CommandRun, run_id)
                asset = session.get(CommandAsset, run.asset_id)
                details = json.loads(json.dumps(run.details))
            with workspace_lock(self.settings, Path(asset.payload["root"])):
                with locked(self.sessions) as session:
                    prerequisites(session, self.settings, run.project_id, asset, run_id)
                    current = session.get(CommandRun, run_id)
                    if current.details["stop_requested"] or self.shutdown.is_set():
                        details.update(
                            ended_at=timestamp(),
                            health="stopped",
                            message="启动前停止，未创建进程",
                            stop_requested=True,
                        )
                        current.state, current.details = "stopped", details
                        return
                    if asset.payload["recipe_id"] == "static_preview" and not available(
                        asset.payload["port"]
                    ):
                        raise BusinessError("PORT_IN_USE", "端口被占用，保留无关程序", 409)
                    current.state = "starting"
                    current.details = {
                        **details,
                        "started_at": timestamp(),
                        "message": "启动意图已保存，正在创建可信进程",
                    }
                    details = dict(current.details)
                directory = self.settings.data_dir / "command-runs"
                directory.mkdir(mode=0o700, exist_ok=True)
                directory = directory / run_id
                directory.mkdir(mode=0o700)
                current_program, raw_program = program()
                if current_program != asset.payload["program"]:
                    raise BusinessError("RECIPE_CHANGED", "配方变化，请重新预览", 409)
                private_file(directory, "runner.py", raw_program)
                snapshot = {
                    "files": [{"path": f["path"], "content": f["content"]} for f in asset.payload["files"]],
                    "port": asset.payload["port"],
                    "parent_pid": os.getpid(),
                    "run_id": run_id,
                    "instance_id": details["instance_id"],
                }
                private_file(directory, "input.json", json.dumps(snapshot, ensure_ascii=False).encode())
                details["argv"] = [
                    asset.payload["program"]["binary"],
                    "-I",
                    "-B",
                    "-S",
                    "-u",
                    str(directory / "runner.py"),
                    asset.payload["recipe_id"],
                ]
                details["cwd"] = str(directory)
                with locked(self.sessions) as session:
                    prerequisites(session, self.settings, run.project_id, asset, run_id)
                    current = session.get(CommandRun, run_id)
                    if current.details["stop_requested"] or self.shutdown.is_set():
                        details.update(
                            ended_at=timestamp(),
                            health="stopped",
                            message="启动前停止，未创建进程",
                            stop_requested=True,
                        )
                        current.state, current.details = "stopped", details
                        return
                    process = subprocess.Popen(
                        details["argv"],
                        cwd=directory,
                        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    details.update(pid=process.pid, message="可信配方正在运行", health="starting")
                    log(details, "可信子进程已启动，输入为已审阅的私有快照")
                    current.state, current.details = "running", details
                    audit(session, run.project_id, "command_running", {"run_id": run_id, "pid": process.pid})
                self.monitor(process, run_id, asset, details, directory)
        except Exception as exc:
            if process:
                terminate(process)
            try:
                with self.sessions() as session:
                    row = session.get(CommandRun, run_id)
                    details = details if details is not None else dict(row.details)
                code = exc.code if isinstance(exc, BusinessError) else "EXECUTOR_FAILED"
                details.update(
                    ended_at=timestamp(),
                    exit_code=process.returncode if process else None,
                    health="conflict" if code == "PORT_IN_USE" else "stopped",
                    error_code=code,
                    message="配方未完成：端口占用，无关进程保留"
                    if code == "PORT_IN_USE"
                    else "配方未完成，原始错误已过滤，记录保留",
                )
                log(details, "运行失败，原始错误和文档内容不进入日志")
                if directory is not None and directory.is_dir() and not (directory / "result.json").exists():
                    artifact(directory, asset, details)
                persist(self.sessions, run_id, "failed", details)
            except Exception:
                self.failure_code = "STORAGE_UNAVAILABLE"
        finally:
            if process:
                terminate(process)
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()

    def monitor(self, process, run_id, asset, details, directory):
        deadline = time.monotonic() + (
            600 if asset.payload["recipe_id"] == "static_preview" else asset.payload["timeout_seconds"]
        )
        buffer = b""
        done, ready, serving = False, False, False
        stop_state = None
        last_check = time.monotonic()
        with selectors.DefaultSelector() as selector:
            for pipe, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, kind)
            while selector.get_map() or process.poll() is None:
                with self.sessions() as session:
                    requested = session.get(CommandRun, run_id).details["stop_requested"]
                if requested or self.shutdown.is_set():
                    stop_state = "stopped"
                    details.update(stop_requested=True, message="本次进程已停止，原文件与记录保留")
                    terminate(process)
                elif time.monotonic() > deadline:
                    stop_state = "stopped" if asset.payload["recipe_id"] == "static_preview" else "timed_out"
                    details.update(
                        error_code=None if stop_state == "stopped" else "TIMEOUT",
                        message="预览到期，已停止"
                        if stop_state == "stopped"
                        else "步骤超时，已结束本次子进程组",
                    )
                    terminate(process)
                if serving and not stop_state and time.monotonic() - last_check > 1:
                    with self.sessions() as session:
                        prerequisites(session, self.settings, asset.project_id, asset, run_id)
                    if not health(asset.payload["port"], run_id, details["instance_id"]):
                        raise BusinessError("HEALTH_FAILED", "预览健康失效")
                    last_check = time.monotonic()
                for key, _ in selector.select(timeout=0.05):
                    raw = os.read(key.fd, 4096)
                    if not raw:
                        selector.unregister(key.fileobj)
                        continue
                    counter = key.data + "_bytes"
                    details[counter] += len(raw)
                    if details["stdout_bytes"] + details["stderr_bytes"] > OUTPUT_LIMIT:
                        raise BusinessError("OUTPUT_LIMIT", "输出超过限额，已过滤并停止")
                    if key.data == "stdout":
                        buffer += raw
                        if len(buffer) > 4096:
                            raise BusinessError("OUTPUT_INVALID", "输出行过长，已过滤并停止")
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            result = event(line, asset, details)
                            done |= result == "done"
                            ready |= result == "ready"
                            details = persist(
                                self.sessions, run_id, "serving" if serving else "running", details
                            )
                if (
                    ready
                    and not serving
                    and not stop_state
                    and process.poll() is None
                    and health(asset.payload["port"], run_id, details["instance_id"])
                ):
                    serving = True
                    details.update(
                        health="healthy", message="静态快照预览运行中；不代表动态应用或阶段验收通过"
                    )
                    log(details, "本机端口与本次实例健康核对通过")
                    details = persist(self.sessions, run_id, "serving", details)
            process.wait(timeout=1)
        if buffer:
            raise BusinessError("OUTPUT_INVALID", "配方输出未正常收尾")
        state = stop_state or (
            "succeeded"
            if process.returncode == 0
            and done
            and len(details["checks"]) == len(asset.payload["files"])
            and all(c["valid"] for c in details["checks"])
            else "failed"
        )
        details.update(exit_code=process.returncode, ended_at=timestamp(), health="stopped")
        if not stop_state:
            details["error_code"] = (
                None if state == "succeeded" else details["error_code"] or "VALIDATION_FAILED"
            )
            details["message"] = (
                "本配方检查通过；仍需其他工程检查和人工验收"
                if state == "succeeded"
                else "本配方检查失败，位置与退出码已保存，原文不进日志"
            )
        if details["error_code"] == "PORT_IN_USE":
            details["health"] = "conflict"
        if details["stderr_bytes"]:
            log(details, "收到标准错误输出，原文已过滤")
        log(details, "本次进程已结束，退出码与产物已保存")
        artifact(directory, asset, details)
        persist(self.sessions, run_id, state, details)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/commands"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            project(session, project_id)
            assets = session.scalars(
                select(CommandAsset)
                .where(CommandAsset.project_id == project_id)
                .order_by(CommandAsset.created_at.desc(), CommandAsset.id)
            )
            return {
                "recipes": list(RECIPES.values()),
                "assets": [asset_public(session, settings, a) for a in assets],
                "runs": [run_public(r) for r in rows(session, project_id)],
            }

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def get_asset(project_id: str, asset_id: str):
        with sessions() as session:
            return asset_public(session, settings, owned(session, project_id, asset_id))

    @app.get(prefix + "/runs/{run_id}", response_model=Run)
    def get_run(project_id: str, run_id: str):
        with sessions() as session:
            return run_public(owned(session, project_id, run_id, CommandRun))

    @app.get(prefix + "/runs/{run_id}/artifact", response_model=Result)
    def get_artifact(project_id: str, run_id: str):
        with sessions() as session:
            row = owned(session, project_id, run_id, CommandRun)
            if not row.details["artifacts"]:
                raise BusinessError("ARTIFACT_PENDING", "结果产物尚未保存", 409)
            fact = row.details["artifacts"][0]
            directory = settings.data_dir / "command-runs" / row.id
            with os.fdopen(os.open(directory / "result.json", os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                raw = stream.read(16385)
            if (
                len(raw) > 16384
                or len(raw) != fact["size"]
                or hashlib.sha256(raw).hexdigest() != fact["sha256"]
            ):
                raise BusinessError("ARTIFACT_CONFLICT", "结果产物被修改，不能作为原结果", 409)
            try:
                return Result.model_validate_json(raw)
            except ValueError:
                raise BusinessError("ARTIFACT_INVALID", "结果产物格式无效") from None

    @app.post(prefix + "/preview", response_model=Asset)
    def get_preview(project_id: str, body: Preview, idempotency_key: str | None = Header(default=None)):
        return mutate(
            sessions,
            project_id,
            "command_preview",
            body.model_dump(),
            idempotency_key,
            lambda s: preview(s, settings, project_id, body),
        )

    @app.post(prefix + "/start", response_model=Run)
    def start(project_id: str, body: Start, idempotency_key: str | None = Header(default=None)):
        if (
            type(body.confirmed) is not bool
            or not body.reason.strip()
            or execution.contains_secret(body.reason)
        ):
            raise BusinessError("CONFIRMATION_REQUIRED", "需审阅具体配方并明确确认，不填写秘密")

        def operation(session):
            asset = owned(session, project_id, body.asset_id)
            if digest(asset.payload) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "配方预览指纹不匹配，不能启动", 409)
            previous = next((r for r in rows(session, project_id) if r.asset_id == asset.id), None)
            if previous:
                return {"run_id": previous.id}
            root, grant = prerequisites(session, settings, project_id, asset)
            with workspace_lock(settings, root):
                pass
            if asset.payload["recipe_id"] == "static_preview" and not available(asset.payload["port"]):
                raise BusinessError("PORT_IN_USE", "端口被占用，不会结束无关程序", 409)
            identifier = str(uuid4())
            details = {
                "root": str(root),
                "task_id": asset.payload["task_id"],
                "step_id": asset.payload["step"]["id"],
                "step_instruction": asset.payload["step"]["instruction"],
                "recipe_id": asset.payload["recipe_id"],
                "argv": [],
                "cwd": None,
                "reason": body.reason,
                "authorization_id": grant.id,
                "started_at": None,
                "ended_at": None,
                "exit_code": None,
                "pid": None,
                "port": asset.payload["port"],
                "instance_id": str(uuid4()),
                "health": "not_started",
                "error_code": None,
                "message": "已批准具体配方，等待执行",
                "stop_requested": False,
                "checks": [],
                "artifacts": [],
                "logs": [],
                "log_status": "available",
                "stdout_bytes": 0,
                "stderr_bytes": 0,
            }
            row = CommandRun(
                id=identifier, project_id=project_id, asset_id=asset.id, state="queued", details=details
            )
            session.add(row)
            session.flush()
            audit(
                session,
                project_id,
                "command_approved",
                {"run_id": row.id, "asset_id": asset.id, "authorization_id": grant.id},
            )
            return {"run_id": row.id}

        result = mutate(sessions, project_id, "command_start", body.model_dump(), idempotency_key, operation)
        with sessions() as session:
            return run_public(owned(session, project_id, result["run_id"], CommandRun))

    @app.post(prefix + "/runs/{run_id}/stop", response_model=Run)
    def stop(project_id: str, run_id: str, body: Stop, idempotency_key: str | None = Header(default=None)):
        if type(body.confirmed) is not bool:
            raise BusinessError("CONFIRMATION_REQUIRED", "请明确确认停止本次预览")

        def operation(session):
            row = owned(session, project_id, run_id, CommandRun)
            if row.state in ACTIVE:
                row.details = {
                    **row.details,
                    "stop_requested": True,
                    "message": "停止请求已保存，等待本次进程退出",
                }
                audit(session, project_id, "command_stop_requested", {"run_id": run_id})
            return {"run_id": row.id}

        mutate(sessions, project_id, "command_stop:" + run_id, body.model_dump(), idempotency_key, operation)
        with sessions() as session:
            return run_public(owned(session, project_id, run_id, CommandRun))
