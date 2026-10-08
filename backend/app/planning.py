"""Read-only host recipes, baseline-bound statements and deterministic plan checks."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select

from . import models
from .core import ROOT
from .db import PlanningAsset, RequirementRecord
from .planning_schemas import (
    Asset,
    ConfirmPlanning,
    EmptyInput,
    GeneratePlan,
    GenerateTechnical,
    PlanningWorkspace,
    SavePlan,
    SaveTechnical,
    TechnicalText,
)
from .requirements import audit, mutate, owned_record, project, timestamp
from .scanner import SECRET, BusinessError, canonical, digest

CHECKS = [
    "范围实现与后续边界",
    "自动化测试",
    "真实模型（无调用需求时须有不适用依据）",
    "类型检查 / Lint / 构建",
    "密钥与秘密排除",
    "重启恢复",
    "桌面与移动宽度",
    "README 启动与验证说明",
    "项目状态更新",
    "证据与人工验收",
]
DECLARATIONS = [
    "package.json",
    "frontend/package.json",
    "pyproject.toml",
    "backend/pyproject.toml",
    "requirements.txt",
    "backend/requirements.txt",
]


def assets(session, pid, kind=None):
    query = select(PlanningAsset).where(PlanningAsset.project_id == pid)
    if kind:
        query = query.where(PlanningAsset.kind == kind)
    return list(
        session.scalars(query.order_by(PlanningAsset.created_at.desc(), PlanningAsset.revision.desc()))
    )


def latest(session, pid, kind):
    rows = assets(session, pid, kind)
    return max(rows, key=lambda r: r.revision) if rows else None


def owned(session, pid, identifier, kind=None):
    row = session.get(PlanningAsset, identifier)
    if not row or row.project_id != pid or kind and row.kind != kind:
        raise BusinessError("NOT_FOUND", "规划记录不存在或不属于此项目", 404)
    return row


def public(row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "kind": row.kind,
        "revision": row.revision,
        "baseline_id": row.baseline_id,
        "payload": row.payload,
        "content_hash": digest(row.payload),
        "created_at": timestamp(row.created_at),
    }


def append(session, pid, kind, baseline_id, payload):
    old = latest(session, pid, kind)
    row = PlanningAsset(
        id=str(uuid4()),
        project_id=pid,
        kind=kind,
        revision=old.revision + 1 if old else 1,
        baseline_id=baseline_id,
        payload=payload,
    )
    session.add(row)
    session.flush()
    audit(session, pid, "planning_" + kind, {"asset_id": row.id, "revision": row.revision})
    return row


def expected(session, pid, kind, revision):
    row = latest(session, pid, kind)
    if revision != (row.revision if row else 0):
        raise BusinessError("REVISION_CONFLICT", "规划修订已变化，输入保留，请核对服务器版本", 409)


def baselines(session, pid):
    rows = list(
        session.scalars(
            select(RequirementRecord).where(
                RequirementRecord.project_id == pid, RequirementRecord.kind == "baseline"
            )
        )
    )
    return sorted(rows, key=lambda r: r.payload["revision"], reverse=True)


def baseline(session, pid, identifier):
    row = owned_record(session, identifier, pid, "baseline")
    current = baselines(session, pid)
    if not current or current[0].id != row.id:
        raise BusinessError("BASELINE_STALE", "已有新冻结基线，请核对新需求后重新绑定方案", 409)
    return row


def dependency_facts(root):
    """No scripts, free-form paths, package managers or document contents."""
    fingerprints = {}
    frameworks = set()
    problems = []
    for name in DECLARATIONS:
        file = root / name
        if not file.exists() and not file.is_symlink():
            fingerprints[name] = "absent"
            continue
        # Refuse every symlink component, including frontend/backend directory links.
        if any(
            (root / Path(*Path(name).parts[:i])).is_symlink() for i in range(1, len(Path(name).parts) + 1)
        ):
            fingerprints[name] = "unsafe"
            problems.append("依赖声明含符号链接：" + name)
            continue
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("not regular")
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("too large")
            text = raw.decode("utf-8")
            fingerprints[name] = digest(text)
            if SECRET.search(text) or re.search(
                r"""(?i)["\'](?:api[_-]?key|access[_-]?key|password|secret|token)["\']\s*[:=]\s*["\'][^"\']+""",
                text,
            ):
                raise ValueError("secret")
            if file.name == "package.json":
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("invalid package")
                deps = data.get("dependencies", {})
                dev = data.get("devDependencies", {})
                if not isinstance(deps, dict) or not isinstance(dev, dict):
                    raise ValueError("invalid dependencies")
                for key, label in {"next": "Next.js", "react": "React", "vue": "Vue", "vite": "Vite"}.items():
                    if key in deps or key in dev:
                        frameworks.add(label)
                frameworks.add("Node.js 工程")
            else:
                for key, label in {"fastapi": "FastAPI", "django": "Django", "flask": "Flask"}.items():
                    if re.search(r"\b" + key + r"\b", text, re.I):
                        frameworks.add(label)
                if file.suffix == ".toml":
                    import tomllib

                    tomllib.loads(text)
                frameworks.add("Python 工程")
        except (OSError, ValueError, UnicodeError, RecursionError):
            fingerprints[name] = "unreadable"
            problems.append("依赖声明无法安全解析，请由执行者核对：" + name)
    return digest(fingerprints), sorted(frameworks), problems


def trusted_version(name):
    # System recipe only: no shell, PATH search, project cwd, npm or repository command.
    folders: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
    if name == "node":
        folders = (*folders, "/opt/homebrew/opt/node@22/bin", "/usr/local/opt/node@22/bin")
        candidate = shutil.which("node")
        runtime_roots = [
            Path.home() / ".local/share/pi-node",
            Path.home() / ".nvm/versions/node",
            Path.home() / ".local/share/fnm/node-versions",
        ]
        if candidate:
            resolved = Path(candidate).resolve()
            if resolved.name == "node" and any(resolved.is_relative_to(root) for root in runtime_roots):
                folders = (*folders, str(resolved.parent))
    for folder in folders:
        binary = Path(folder) / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            continue
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                cwd=ROOT,
                timeout=2,
                capture_output=True,
                env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "LANG": "C"},
                check=False,
            )
            # Keep a version only. Never persist command output or arbitrary error text.
            value = re.search(rb"(?:v|version\s+)?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", completed.stdout[:4096])
            if completed.returncode == 0 and value:
                return value[1].decode()
            return "无法核对"
        except (OSError, subprocess.TimeoutExpired):
            return "无法核对"
    return "未检测到"


def environment(settings, row):
    root = canonical(row.canonical_path, settings.allowed_roots)
    checks = []

    def add(key, label, status, result, method, guidance=""):
        checks.append(
            dict(key=key, label=label, status=status, result=result, method=method, guidance=guidance)
        )

    add(
        "directory",
        "项目目录",
        "passed" if root.is_dir() else "blocked",
        "目录存在" if root.is_dir() else "项目目录不存在",
        "授权路径解析及目录存在性",
        "由执行者核对登记目录，保留原资料",
    )
    readable = root.is_dir() and os.access(root, os.R_OK | os.X_OK)
    writable = root.is_dir() and os.access(root, os.W_OK | os.X_OK)
    add(
        "permission",
        "目录权限标志",
        "passed" if readable and writable else "blocked",
        f"可读：{'是' if readable else '否'}；可写标志：{'是' if writable else '否'}",
        "os.access；未创建探针文件",
        "让执行者核对目录所有者及授权，不能自动提权",
    )
    fingerprint, frameworks, problems = (
        dependency_facts(root) if readable else ("unavailable", [], ["无法读取依赖声明"])
    )
    add(
        "dependencies",
        "现有工程识别",
        "blocked" if problems else "passed",
        "；".join(problems)
        if problems
        else "、".join(frameworks) or "固定范围内未发现依赖声明；按新/未知工程核对",
        "最多六个固定位置的依赖声明；每个最多64 KiB，不执行脚本",
    )
    add("python", "Python", "passed", sys.version.split()[0], "当前服务运行时，不执行项目 Python")
    for tool in ("node", "git"):
        value = trusted_version(tool)
        relevant = tool == "git" or any(f in frameworks for f in ["Node.js 工程"]) or not frameworks
        add(
            tool,
            "Node.js" if tool == "node" else "Git",
            "blocked"
            if relevant and value in {"未检测到", "无法核对"}
            else "info"
            if not relevant
            else "passed",
            value,
            "受信系统/已批准运行时目录固定 --version 配方，超时2秒；非项目命令",
            "由执行者打开官方安装入口，安装后重新检查；平台不会自动安装",
        )
    try:
        free = shutil.disk_usage(root).free
        add(
            "disk",
            "磁盘可用空间",
            "passed" if free > 0 else "blocked",
            f"{free // (1024 * 1024)} MiB",
            "本机文件系统可用空间；未测项目运行内存或负载",
        )
    except OSError:
        add("disk", "磁盘可用空间", "blocked", "无法读取", "文件系统检查", "由执行者检查目录是否仍存在")
    add(
        "model",
        "模型服务",
        "info",
        "已配置，真实验证须另查" if models.available(settings) else "未启用",
        "仅读取配置状态，不测试连接、不外发",
    )
    add(
        "executor",
        "业务执行器",
        "pending",
        "A3 未交付，当前只准备方案和计划",
        "当前产品能力",
        "后续 FR08—FR12 独立开发",
    )
    return {
        "fingerprint": fingerprint,
        "frameworks": frameworks,
        "checks": checks,
        "limitations": [
            "权限标志不证明实际文件写入或构建成功；未安装、构建或运行项目。",
            "未扫描全部代码、Git 差异和业务资料；技术建议须人工核对。",
            "环境检查15分钟有效；确认前重新读取依赖声明指纹。",
        ],
    }


def issue(code, message, object_id=""):
    return {"code": code, "message": message, "object_id": object_id}


def technical_issues(session, settings, pid, row):
    if not row:
        return [issue("TECHNICAL_REQUIRED", "请先生成并核对技术适配草稿")]
    found = []
    try:
        baseline(session, pid, row.baseline_id)
    except BusinessError as exc:
        found.append(issue(exc.code, exc.message, row.id))
    env = owned(session, pid, row.payload["environment_id"], "environment")
    if datetime.now(UTC) - env.created_at.replace(tzinfo=UTC) > timedelta(minutes=15):
        found.append(issue("ENVIRONMENT_EXPIRED", "环境检查已过期，请重新检查并将新结果绑定到方案", env.id))
    found.extend(
        issue("ENVIRONMENT_BLOCKED", c["result"], c["key"])
        for c in env.payload["checks"]
        if c["status"] == "blocked"
    )
    try:
        target = canonical(project(session, pid).canonical_path, settings.allowed_roots)
        fp, _, problems = dependency_facts(target)
        if not target.is_dir() or fp != env.payload["fingerprint"] or problems:
            found.append(issue("ENVIRONMENT_CHANGED", "目录或依赖声明变化，请重新检查并核对方案", env.id))
        if not os.access(target, os.R_OK | os.W_OK | os.X_OK):
            found.append(issue("ENVIRONMENT_CHANGED", "目录权限已变化，请核对后重新检查", env.id))
    except BusinessError as exc:
        found.append(issue(exc.code, exc.message, env.id))
    return found


def approval(session, pid, kind, asset_id):
    return next((r for r in assets(session, pid, kind) if r.payload["asset_id"] == asset_id), None)


def require_technical(session, settings, pid, identifier):
    row = owned(session, pid, identifier, "technical")
    if latest(session, pid, "technical").id != row.id or not approval(
        session, pid, "technical_approval", row.id
    ):
        raise BusinessError("TECHNICAL_REQUIRED", "当前技术方案尚未确认，或已有新方案需重新核对", 409)
    found = technical_issues(session, settings, pid, row)
    if found:
        raise BusinessError("INPUT_STALE", found[0]["message"], 409)
    return row


def graph_issues(rows, label):
    found = []
    identifiers = [r["id"] for r in rows]
    if len(set(identifiers)) != len(identifiers):
        found.append(issue("DUPLICATE_ID", label + "编号重复"))
    graph = {r["id"]: list(r["depends_on"]) for r in rows}
    for key, refs in graph.items():
        if len(set(refs)) != len(refs):
            found.append(issue("DUPLICATE_DEPENDENCY", label + "依赖重复", key))
        for ref in refs:
            if ref not in graph:
                found.append(issue("DEPENDENCY_MISSING", label + "依赖不存在：" + ref, key))
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            found.append(issue("DEPENDENCY_CYCLE", label + "依赖成环，涉及：" + key, key))
            return
        if key in visited:
            return
        visiting.add(key)
        for ref in graph.get(key, []):
            if ref in graph:
                visit(ref)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    return found


def reachable(graph, start, target):
    seen, todo = set(), list(graph.get(start, []))
    while todo:
        current = todo.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            todo.extend(graph.get(current, []))
    return False


def plan_checks(text, base):
    versions, stages, tasks = text["versions"], text["stages"], text["tasks"]
    found = []
    for rows, label in [(versions, "版本"), (stages, "阶段"), (tasks, "任务")]:
        if not rows:
            found.append(issue("PLAN_EMPTY", "至少需要一个" + label))
        found.extend(graph_issues(rows, label))
    vm, sm, tm = ({r["id"]: r for r in rows} for rows in (versions, stages, tasks))
    requirements = {r["id"]: r for r in base["items"]}
    stage_graph = {s["id"]: s["depends_on"] for s in stages}
    version_graph = {v["id"]: v["depends_on"] for v in versions}
    parents = {s["id"]: [s["parent_id"]] if s["parent_id"] else [] for s in stages}
    found.extend(graph_issues([{"id": k, "depends_on": v} for k, v in parents.items()], "父阶段"))
    for s in stages:
        if s["version_id"] not in vm:
            found.append(issue("VERSION_MISSING", "阶段引用未知版本", s["id"]))
        if s["parent_id"] in sm and sm[s["parent_id"]]["version_id"] != s["version_id"]:
            found.append(issue("PARENT_VERSION", "父子阶段必须属于同一版本", s["id"]))
        for ref in s["depends_on"]:
            if (
                ref in sm
                and sm[ref]["version_id"] != s["version_id"]
                and not reachable(version_graph, s["version_id"], sm[ref]["version_id"])
            ):
                found.append(issue("VERSION_DEPENDENCY", "跨版本阶段依赖需在版本中明确", s["id"]))
    covered = set()
    for t in tasks:
        if t["stage_id"] not in sm:
            found.append(issue("STAGE_MISSING", "任务引用未知阶段", t["id"]))
        if t["kind"] == "requirement" and not t["requirement_ids"]:
            found.append(issue("REQUIREMENT_REQUIRED", "业务任务必须关联基线需求", t["id"]))
        if t["kind"] == "defect" and len(t["defect_ref"].strip()) < 3:
            found.append(issue("DEFECT_REQUIRED", "缺陷任务必须填写可核对的缺陷标识", t["id"]))
        if t["kind"] == "infrastructure" and len(t["reason"].strip()) < 3:
            found.append(issue("REASON_REQUIRED", "基础设施任务必须说明理由", t["id"]))
        if len(set(t["requirement_ids"])) != len(t["requirement_ids"]):
            found.append(issue("DUPLICATE_REQUIREMENT", "任务需求关联重复", t["id"]))
        for rid in t["requirement_ids"]:
            req = requirements.get(rid)
            if not req:
                found.append(issue("REQUIREMENT_UNKNOWN", "新增或未知需求须先形成需求变更：" + rid, t["id"]))
                continue
            covered.add(rid)
            if int(t["priority"][1]) > int(req["priority"][1]):
                found.append(issue("PRIORITY_WEAKENED", "任务优先级不能弱化基线需求：" + rid, t["id"]))
            stage = sm.get(t["stage_id"])
            if (
                stage
                and stage["version_id"] in vm
                and vm[stage["version_id"]]["name"] != req["target_version"]
            ):
                found.append(issue("VERSION_MISMATCH", "需求目标版本不匹配：" + rid, t["id"]))
        for ref in t["depends_on"]:
            dep = tm.get(ref)
            if (
                dep
                and dep["stage_id"] != t["stage_id"]
                and not reachable(stage_graph, t["stage_id"], dep["stage_id"])
            ):
                found.append(issue("STAGE_DEPENDENCY", "跨阶段任务依赖需在阶段中明确", t["id"]))
    for rid in requirements.keys() - covered:
        found.append(issue("REQUIREMENT_UNCOVERED", "基线需求未安排任务：" + rid, rid))
    for s in stages:
        if not any(t["stage_id"] == s["id"] for t in tasks):
            found.append(issue("STAGE_EMPTY", "阶段没有任务", s["id"]))
    for v in versions:
        if not v["name"].strip() or not v["goal"].strip():
            found.append(issue("VERSION_INPUT", "版本名称和目标不能为空", v["id"]))
        if not any(s["version_id"] == v["id"] for s in stages):
            found.append(issue("VERSION_EMPTY", "版本没有阶段", v["id"]))
    for s in stages:
        if any(len(s[k].strip()) < 3 for k in ("name", "scope", "out_of_scope", "deliverable")):
            found.append(issue("STAGE_INPUT", "阶段范围与交付内容需完整", s["id"]))
    for t in tasks:
        if len(t["title"].strip()) < 3:
            found.append(issue("TASK_INPUT", "任务标题需完整", t["id"]))
    return found


def plan_issues(session, settings, pid, row):
    if not row:
        return [issue("PLAN_REQUIRED", "请先生成并核对阶段计划")]
    found = []
    try:
        require_technical(session, settings, pid, row.payload["technical_id"])
    except BusinessError as exc:
        found.append(issue(exc.code, exc.message, row.id))
    base = owned_record(session, row.baseline_id, pid, "baseline")
    found.extend(plan_checks(row.payload["text"], base.payload))
    return found


def workspace(session, settings, pid):
    project(session, pid)
    bases = baselines(session, pid)
    env, tech, plan = [latest(session, pid, k) for k in ("environment", "technical", "plan")]
    ti, pi = technical_issues(session, settings, pid, tech), plan_issues(session, settings, pid, plan)
    ta = approval(session, pid, "technical_approval", tech.id) if tech else None
    pa = approval(session, pid, "plan_approval", plan.id) if plan else None
    notices = ["规则草稿需人工核对；已确认或可交接都不代表执行许可或阶段通过。"]
    if bases:
        from .requirements import draft_output

        if draft_output(session, pid)["revision"] > bases[0].payload["revision"]:
            notices.append("存在尚未冻结的新需求草稿，本计划只覆盖所绑定的冻结基线。")
        if bases[0].payload["deferred"]:
            notices.append("基线保留后续待验项，真实效果验收不能被计划确认替代。")
    else:
        notices.append("没有冻结需求基线；可检查环境，方案与计划不能确认。")
    return {
        "baselines": [
            {"id": r.id, "revision": r.payload["revision"], "created_at": r.payload["created_at"]}
            for r in bases
        ],
        "environment": public(env) if env else None,
        "technical": public(tech) if tech else None,
        "plan": public(plan) if plan else None,
        "technical_approval": public(ta) if ta else None,
        "plan_approval": public(pa) if pa else None,
        "history": [
            {k: public(r)[k] for k in ("id", "kind", "revision", "baseline_id", "created_at")}
            for r in assets(session, pid)
        ],
        "technical_issues": ti,
        "plan_issues": pi,
        "notices": notices,
        "required_checks": plan.payload["required_checks"] if plan else CHECKS,
        "acceptance": plan.payload["acceptance"] if plan else {},
        "handoff_ready": bool(ta and pa and not ti and not pi),
        "execution_supported": False,
    }


def initial_technical(base, env):
    fields = {f["key"]: f["content"] for f in base["fields"]}
    frameworks = env.payload["frameworks"]
    text = {
        "understanding": fields["user_task"] + "\n输入与产物：" + fields["input_output"],
        "recommendation": "保留已识别技术栈："
        + "、".join(frameworks)
        + "；增量实现基线需求，具体版本与集成方式由执行者核对。"
        if frameworks
        else "规则建议：本机 Web 纵向切片，Next.js/TypeScript、FastAPI/Python；按需求决定数据库和模型模块，未知旧工程先人工核对。",
        "retained": "保留原工程、原数据、既有测试和依赖管理，不按模板整体重构。",
        "modules": "只启用需求触发的模块；需人工逐项核对数据库、文件、模型等能力。",
        "deviations": "现有工程优先；规则建议不能替代业务判断，未知工程不自动替换。",
        "costs": fields["model_cost"]
        + "\n本次环境检查与规则草稿不创建云资源、不调用模型；实际实施新增费用另行说明与确认。",
        "deferred": "基线待验项继续保留；当前只确认准备方案，真实效果、执行及发布另行验证。",
        "data_flow": fields["data"] + "\n当前检查与规划只在本机处理，不发送项目资料。",
        "criteria": "逐项覆盖基线原验收要求；保留测试、恢复、安全、界面与证据闸门，待验不能写为通过。",
        "risks": "固定依赖声明识别不是全工程分析；未执行构建、仓库脚本或业务任务，依赖风险和资源需求需人工核对。",
    }
    for key, value in text.items():
        limit = 3000 if key in {"understanding", "recommendation"} else 2000
        if len(value) > limit:
            text[key] = value[: limit - 35] + "……（摘要已截断，请核对所绑定基线完整字段）"
    return TechnicalText.model_validate(text).model_dump()


def initial_plan(base):
    items = sorted(base["items"], key=lambda r: (r["priority"], r["target_version"], r["id"]))
    versions: list[dict] = []
    stages: list[dict] = []
    tasks: list[dict] = []
    names = list(dict.fromkeys(r["target_version"] for r in items))
    previous_stage = previous_version = None
    for index, name in enumerate(names, 1):
        vid, sid, verify = f"V{index}", f"S{index * 2 - 1}", f"S{index * 2}"
        selected = [r for r in items if r["target_version"] == name]
        versions.append(
            dict(
                id=vid,
                name=name,
                goal="覆盖该版本冻结需求并形成有效证据",
                depends_on=[previous_version] if previous_version else [],
            )
        )
        stages.extend(
            [
                dict(
                    id=sid,
                    version_id=vid,
                    name="需求实现与增量验证",
                    scope="实现该版本基线需求，保留既有工程",
                    out_of_scope="未冻结新需求及其他版本，未经授权费用与发布",
                    deliverable="需求关联的代码或文档及自动验证结果",
                    depends_on=[previous_stage] if previous_stage else [],
                    parent_id=None,
                ),
                dict(
                    id=verify,
                    version_id=vid,
                    name="集成验证与人工验收",
                    scope="核对需求原验收与阶段必需证据",
                    out_of_scope="未经通过的对外发布及未授权动作",
                    deliverable="证据包、待验记录和人工验收结论",
                    depends_on=[sid],
                    parent_id=None,
                ),
            ]
        )
        implementation_ids = []
        for req in selected:
            tid = f"T{len(tasks) + 1}"
            implementation_ids.append(tid)
            tasks.append(
                dict(
                    id=tid,
                    stage_id=sid,
                    title=req["value"][:200],
                    kind="requirement",
                    requirement_ids=[req["id"]],
                    defect_ref="",
                    reason="",
                    priority=req["priority"],
                    depends_on=[],
                )
            )
        tasks.append(
            dict(
                id=f"T{len(tasks) + 1}",
                stage_id=verify,
                title="整理证据并核对该版本原验收要求",
                kind="infrastructure",
                requirement_ids=[r["id"] for r in selected],
                defect_ref="",
                reason="阶段交付需要完整验证与人工效果判断，不能按任务成功代替验收",
                priority=min(r["priority"] for r in selected),
                depends_on=implementation_ids,
            )
        )
        previous_stage, previous_version = verify, vid
    return dict(versions=versions, stages=stages, tasks=tasks)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/planning"

    @app.get(prefix, response_model=PlanningWorkspace)
    def get_workspace(project_id: str):
        with sessions() as session:
            return workspace(session, settings, project_id)

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def get_asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(owned(session, project_id, asset_id))

    @app.post(prefix + "/environment", response_model=Asset)
    def check_environment(
        project_id: str, body: EmptyInput, idempotency_key: str | None = Header(default=None)
    ):
        def operation(session):
            facts = environment(settings, project(session, project_id))
            return public(append(session, project_id, "environment", None, facts))

        return mutate(sessions, project_id, "planning_environment", {}, idempotency_key, operation)

    def save_technical(project_id, body, key, generated):
        def operation(session):
            expected(session, project_id, "technical", body.expected_revision)
            base = baseline(session, project_id, body.baseline_id)
            env = owned(session, project_id, body.environment_id, "environment")
            payload = dict(
                environment_id=env.id,
                source="rule_template" if generated else "user_edited",
                text=initial_technical(base.payload, env) if generated else body.text.model_dump(),
            )
            return public(append(session, project_id, "technical", base.id, payload))

        return mutate(
            sessions, project_id, "planning_technical_" + str(generated), body.model_dump(), key, operation
        )

    @app.post(prefix + "/technical/generate", response_model=Asset)
    def generate_technical(
        project_id: str, body: GenerateTechnical, idempotency_key: str | None = Header(default=None)
    ):
        return save_technical(project_id, body, idempotency_key, True)

    @app.post(prefix + "/technical/revisions", response_model=Asset)
    def revise_technical(
        project_id: str, body: SaveTechnical, idempotency_key: str | None = Header(default=None)
    ):
        return save_technical(project_id, body, idempotency_key, False)

    def save_plan(project_id, body, key, generated):
        def operation(session):
            expected(session, project_id, "plan", body.expected_revision)
            tech = require_technical(session, settings, project_id, body.technical_id)
            base = owned_record(session, tech.baseline_id, project_id, "baseline")
            payload = dict(
                technical_id=tech.id,
                acceptance={r["id"]: r["acceptance"] for r in base.payload["items"]},
                required_checks=CHECKS,
                source="rule_template" if generated else "user_edited",
                text=initial_plan(base.payload) if generated else body.text.model_dump(),
            )
            return public(append(session, project_id, "plan", base.id, payload))

        return mutate(
            sessions, project_id, "planning_plan_" + str(generated), body.model_dump(), key, operation
        )

    @app.post(prefix + "/plans/generate", response_model=Asset)
    def generate_plan(
        project_id: str, body: GeneratePlan, idempotency_key: str | None = Header(default=None)
    ):
        return save_plan(project_id, body, idempotency_key, True)

    @app.post(prefix + "/plans/revisions", response_model=Asset)
    def revise_plan(project_id: str, body: SavePlan, idempotency_key: str | None = Header(default=None)):
        return save_plan(project_id, body, idempotency_key, False)

    def confirm(project_id, body, key, kind):
        def operation(session):
            expected(session, project_id, kind, body.expected_revision)
            row = latest(session, project_id, kind)
            if not row or digest(row.payload) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "待确认规划内容已变化", 409)
            found = (
                technical_issues(session, settings, project_id, row)
                if kind == "technical"
                else plan_issues(session, settings, project_id, row)
            )
            if found:
                raise BusinessError("PLAN_BLOCKED", found[0]["message"], 409)
            prior = approval(session, project_id, kind + "_approval", row.id)
            if prior:
                return public(prior)
            return public(
                append(
                    session,
                    project_id,
                    kind + "_approval",
                    row.baseline_id,
                    dict(
                        asset_id=row.id,
                        content_hash=digest(row.payload),
                        reason=body.reason,
                        approved_by="local_operator",
                    ),
                )
            )

        return mutate(sessions, project_id, "planning_confirm_" + kind, body.model_dump(), key, operation)

    @app.post(prefix + "/technical/confirm", response_model=Asset)
    def confirm_technical(
        project_id: str, body: ConfirmPlanning, idempotency_key: str | None = Header(default=None)
    ):
        return confirm(project_id, body, idempotency_key, "technical")

    @app.post(prefix + "/plans/confirm", response_model=Asset)
    def confirm_plan(
        project_id: str, body: ConfirmPlanning, idempotency_key: str | None = Header(default=None)
    ):
        return confirm(project_id, body, idempotency_key, "plan")
