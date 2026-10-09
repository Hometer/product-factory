"""Persist control intent; only repeat reviewed, known-safe local recipes."""

import copy
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select

from . import recovery
from .control_schemas import Action, Entry, Workspace
from .db import CommandAsset, CommandControl, CommandRun
from .patch_files import workspace_lock
from .requirements import audit, mutate, project, timestamp
from .scanner import BusinessError, digest


def initialize(session, run, root_id=None, parent_id=None, attempt=1):
    row = session.get(CommandControl, run.id)
    if row:
        return row
    row = CommandControl(
        run_id=run.id,
        project_id=run.project_id,
        root_id=root_id or run.id,
        parent_id=parent_id,
        attempt=attempt,
        payload={"revision": 0, "request": None},
    )
    session.add(row)
    session.flush()
    return row


def control_hash(session, run):
    control = session.get(CommandControl, run.id)
    asset = session.get(CommandAsset, run.asset_id)
    return digest(
        {
            "run_id": run.id,
            "asset_hash": digest(asset.payload),
            "control": control.payload if control else {"revision": 0, "request": None},
        }
    )


def request(session, run):
    control = session.get(CommandControl, run.id)
    return control.payload["request"] if control else None


def update_request(control, value):
    control.payload = {"revision": control.payload["revision"] + 1, "request": value}


def apply_before_launch(session, run):
    value = request(session, run)
    if not value or value["effect"] != "pending":
        return False
    control = session.get(CommandControl, run.id)
    kind = value["kind"]
    run.state = "paused" if kind == "pause" else "cancelled"
    run.details = {
        **run.details,
        "ended_at": timestamp(),
        "health": "stopped",
        "message": "暂停已生效，尚未创建本次进程" if kind == "pause" else "取消已生效，本次尝试已安全停止",
    }
    update_request(
        control,
        {
            **value,
            "effect": "applied",
            "effective_at": timestamp(),
            "process_started": bool(run.details["pid"]),
        },
    )
    recovery.checkpoint(session, run, run.state, run.state, value["reason"])
    return True


def finish(session, run, state, details):
    from . import commands

    value = request(session, run)
    if not value or value["effect"] != "pending" or state in commands.ACTIVE:
        return state, details
    control = session.get(CommandControl, run.id)
    effect = "applied"
    if state == "interrupted" or details["error_code"] == "PROCESS_STOP_UNCONFIRMED":
        effect = "unknown"
    elif value["kind"] == "cancel":
        state = "cancelled"
        details["message"] = "取消已生效，本次安全步骤已结束；结果与可能已发生的操作保留"
    elif state != "paused":
        effect = "step_completed"
        details["message"] += "；暂停已生效，当前唯一安全步骤已结束，没有后续自动步骤"
    update_request(
        control,
        {
            **value,
            "effect": effect,
            "effective_at": timestamp() if effect != "unknown" else None,
            "process_started": bool(details["pid"]) if effect != "unknown" else None,
        },
    )
    return state, details


def family(session, run):
    c = session.get(CommandControl, run.id)
    if not c:
        return None, [], run.id
    members = list(
        session.scalars(
            select(CommandControl).where(CommandControl.root_id == c.root_id).order_by(CommandControl.attempt)
        )
    )
    return c, members, c.root_id


def never_launched(session, settings, run):
    # Only an applied control checkpoint can establish this; an absent PID is insufficient.
    d = run.details
    value = request(session, run)
    return (
        bool(
            value
            and value["kind"] == "pause"
            and value["effect"] == "applied"
            and value["process_started"] is False
        )
        and run.state == "paused"
        and not any(
            d[k]
            for k in (
                "started_at",
                "pid",
                "cwd",
                "argv",
                "checks",
                "artifacts",
                "stdout_bytes",
                "stderr_bytes",
            )
        )
        and not os.path.lexists(settings.data_dir / "command-runs" / run.id)
    )


def repeat_issues(session, settings, run):
    from . import commands

    issues = []
    if run.state not in {"paused", "failed", "timed_out", "stopped"}:
        return ["仅已知安全的暂停/失败/超时/停止可继续或重试；成功、取消与未知禁止重发"]
    verdict, _ = recovery.inspect(session, settings, run)
    if verdict in {"result_unknown", "conflict"} or run.details["error_code"] == "PROCESS_STOP_UNCONFIRMED":
        return ["结果或进程状态未确认，不能重复执行"]
    value = request(session, run)
    verified_before_launch = bool(
        value
        and value["kind"] == "pause"
        and value["effect"] == "applied"
        and value["process_started"] is False
    )
    if not run.details["artifacts"] and not verified_before_launch:
        return ["缺少可核对结果，不能判断安全重复"]
    asset = session.get(CommandAsset, run.asset_id)
    try:
        root, grant = commands.prerequisites(session, settings, run.project_id, asset, run.id)
        if grant.id != run.details["authorization_id"]:
            raise BusinessError("AUTHORIZATION_CHANGED", "原授权身份变化，不能继承重试", 409)
        with workspace_lock(settings, root):
            pass
        if asset.payload["recipe_id"] == "static_preview" and not commands.available(asset.payload["port"]):
            raise BusinessError("PORT_IN_USE", "端口占用，不会结束无关程序", 409)
    except (BusinessError, OSError) as exc:
        issues.append(exc.message if isinstance(exc, BusinessError) else "当前环境不能核对")
    return issues


def public(session, settings, run):
    from . import commands

    c, members, root_id = family(session, run)
    latest = not members or members[-1].run_id == run.id
    count = c.attempt if c else 1
    value = request(session, run)
    pending = bool(value and value["effect"] in {"pending", "unknown"})
    cancelled = any(m.payload["request"] and m.payload["request"]["kind"] == "cancel" for m in members)
    issues = repeat_issues(session, settings, run)
    if not latest:
        issues.append("这是旧尝试；请选择当前最新尝试，旧结果保持")
    if cancelled:
        issues.append("原任务已有取消请求，禁止继续或重试")
    if count >= 3 and not never_launched(session, settings, run):
        issues.append("已达到初次加两次重试的三次上限；暂停服务重建也计入")
    if pending:
        issues.append("请求尚未确认生效，不能继续或重试")
    active = run.state in commands.ACTIVE and not run.details["stop_requested"]
    return {
        "run": commands.run_public(run),
        "root_id": root_id,
        "parent_id": c.parent_id if c else None,
        "attempt": count,
        "max_attempts": 3,
        "cost_micro": 0,
        "request": value,
        "expected_hash": control_hash(session, run),
        "can_pause": latest and active and not pending,
        "can_cancel": latest and run.state in commands.ACTIVE | {"paused", "interrupted"} and not cancelled,
        "can_continue": run.state == "paused" and not issues,
        "can_retry": run.state in {"failed", "timed_out", "stopped"} and not issues,
        "blockers": issues,
    }


def install(app, sessions, settings):
    from . import commands

    prefix = "/api/v1/factory/projects/{project_id}/controls"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            project(session, project_id)
            runs = commands.rows(session, project_id)
            return {
                "entries": [public(session, settings, r) for r in runs[:100]],
                "total": len(runs),
                "message": "最近100次尝试，原历史保留；三种本机只读配方零费。模型/外部付费调用控制未接入，已发送无法保证撤回，未知结果与预算须独立核对。",
            }

    def change(project_id, run_id, action, body, key):
        recovery.validate_reason(body.reason)

        def operation(session):
            row = commands.owned(session, project_id, run_id, CommandRun)
            if control_hash(session, row) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "控制记录或原配方变化，请刷新后审阅", 409)
            c, members, _ = family(session, row)
            if members and members[-1].run_id != row.id:
                raise BusinessError("CONTROL_BLOCKED", "请选择当前最新尝试", 409)
            view = public(session, settings, row)
            if not view["can_" + action]:
                if (
                    action in {"retry", "continue"}
                    and view["attempt"] >= 3
                    and not never_launched(session, settings, row)
                ):
                    raise BusinessError("RETRY_LIMIT", "已达到三次上限，未启动新尝试", 409)
                raise BusinessError("CONTROL_BLOCKED", "操作不允许：" + "；".join(view["blockers"]), 409)
            c = initialize(session, row)
            if action in {"pause", "cancel"}:
                value = {
                    "kind": action,
                    "reason": body.reason,
                    "requested_at": timestamp(),
                    "effect": "pending",
                    "effective_at": None,
                    "process_started": None,
                }
                update_request(c, value)
                recovery.checkpoint(
                    session,
                    row,
                    action + "_requested",
                    "active" if row.state in commands.ACTIVE else "result_unknown",
                    body.reason,
                )
                if row.state in {"queued", "paused"}:
                    apply_before_launch(session, row)
                elif row.state == "interrupted":
                    update_request(c, {**value, "effect": "unknown"})
                return {"run_id": row.id}
            if action == "continue" and never_launched(session, settings, row):
                # Resume the unexecuted attempt without making an extra attempt or clearing stop.
                if row.details["stop_requested"]:
                    raise BusinessError("CONTROL_BLOCKED", "已有停止请求，不能继续", 409)
                update_request(c, None)
                row.state = "queued"
                row.details = {
                    **row.details,
                    "ended_at": None,
                    "error_code": None,
                    "health": "not_started",
                    "message": "暂停后已明确继续，尚未执行的原尝试等待运行",
                }
                recovery.checkpoint(session, row, "continued", "not_executed", body.reason)
                return {"run_id": row.id}
            details = copy.deepcopy(row.details)
            details.update(
                reason=body.reason,
                argv=[],
                cwd=None,
                started_at=None,
                ended_at=None,
                exit_code=None,
                pid=None,
                instance_id=str(uuid4()),
                health="not_started",
                error_code=None,
                stop_requested=False,
                checks=[],
                artifacts=[],
                logs=[],
                log_status="available",
                stdout_bytes=0,
                stderr_bytes=0,
                message="安全重复已明确批准，等待关联的新尝试",
            )
            asset = session.get(CommandAsset, row.asset_id)
            with workspace_lock(settings, Path(asset.payload["root"])):
                child = CommandRun(
                    id=str(uuid4()),
                    project_id=row.project_id,
                    asset_id=row.asset_id,
                    state="queued",
                    details=details,
                )
                session.add(child)
                session.flush()
                initialize(session, child, c.root_id, row.id, c.attempt + 1)
                recovery.checkpoint(session, child, "queued", "not_executed", body.reason)
                recovery.checkpoint(
                    session,
                    row,
                    "retry_approved",
                    "paused" if row.state == "paused" else "completed",
                    body.reason,
                )
            audit(
                session,
                project_id,
                "command_new_attempt",
                {
                    "run_id": child.id,
                    "parent_id": row.id,
                    "root_id": c.root_id,
                    "attempt": c.attempt + 1,
                    "cost_micro": 0,
                },
            )
            return {"run_id": child.id}

        result = mutate(
            sessions, project_id, "control_" + action + ":" + run_id, body.model_dump(), key, operation
        )
        with sessions() as session:
            return public(
                session, settings, commands.owned(session, project_id, result["run_id"], CommandRun)
            )

    @app.post(prefix + "/runs/{run_id}/pause", response_model=Entry)
    def pause(project_id: str, run_id: str, body: Action, idempotency_key: str | None = Header(default=None)):
        return change(project_id, run_id, "pause", body, idempotency_key)

    @app.post(prefix + "/runs/{run_id}/cancel", response_model=Entry)
    def cancel(
        project_id: str, run_id: str, body: Action, idempotency_key: str | None = Header(default=None)
    ):
        return change(project_id, run_id, "cancel", body, idempotency_key)

    @app.post(prefix + "/runs/{run_id}/continue", response_model=Entry)
    def continue_run(
        project_id: str, run_id: str, body: Action, idempotency_key: str | None = Header(default=None)
    ):
        return change(project_id, run_id, "continue", body, idempotency_key)

    @app.post(prefix + "/runs/{run_id}/retry", response_model=Entry)
    def retry(project_id: str, run_id: str, body: Action, idempotency_key: str | None = Header(default=None)):
        return change(project_id, run_id, "retry", body, idempotency_key)
