"""Evidence-based recovery, never infer a paid/unknown result or replay it."""

import os
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import func, select

from . import execution
from .db import CommandAsset, CommandCheckpoint, CommandRun, RequirementRecord
from .patch_files import workspace_lock
from .recovery_schemas import Resume, Review, Task, Workspace
from .requirements import audit, mutate, project, timestamp
from .scanner import BusinessError, digest


def history(session, run_id):
    return list(
        session.scalars(
            select(CommandCheckpoint)
            .where(CommandCheckpoint.run_id == run_id)
            .order_by(CommandCheckpoint.created_at.desc(), CommandCheckpoint.id.desc())
        )
    )


def run_hash(row):
    return digest({"id": row.id, "asset_id": row.asset_id, "state": row.state, "details": row.details})


def checkpoint(session, row, phase, verdict, reason=""):
    item = CommandCheckpoint(
        id=str(uuid4()),
        project_id=row.project_id,
        run_id=row.id,
        payload={
            "phase": phase,
            "state": row.state,
            "verdict": verdict,
            "run_hash": run_hash(row),
            "reason": reason,
            "exit_code": row.details["exit_code"],
            "artifacts": row.details["artifacts"],
        },
    )
    session.add(item)
    session.flush()
    audit(
        session,
        row.project_id,
        "command_checkpoint",
        {"run_id": row.id, "checkpoint_id": item.id, "phase": phase, "verdict": verdict},
    )
    return item


def inspect(session, settings, row):
    from . import commands

    d = row.details
    if row.state in commands.ACTIVE:
        return "active", "原任务仍在等待或执行；刷新只查询，不创建新任务"
    if row.state == "interrupted":
        journal = history(session, row.id)
        # Persisted queued is proof only when this version recorded its approval, no
        # launch-intent was ever committed, and no filesystem/process/result traces exist.
        known_queue = any(c.payload["phase"] == "queued" for c in journal)
        launched = any(c.payload["phase"] in {"launch_intent", "executing", "finished"} for c in journal)
        no_trace = not any(
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
        directory = settings.data_dir / "command-runs" / row.id if settings else None
        if known_queue and not launched and no_trace and directory and not os.path.lexists(directory):
            if d["stop_requested"]:
                return "stopped", "已请求停止且确定未执行；不能以恢复覆盖停止请求"
            return "not_executed", "审批入队检查点已保存，未提交启动意图且无执行痕迹；可审阅继续原任务"
        return "result_unknown", "无法确定是否已执行或已停止；保留占用，不按历史 PID 操作，不自动重发"
    if d["artifacts"]:
        try:
            asset = session.get(CommandAsset, row.asset_id)
            result = commands.read_artifact(settings, row)
            if (
                result.exit_code != d["exit_code"]
                or result.error_code != d["error_code"]
                or [c.model_dump() for c in result.checks] != d["checks"]
                or [f.model_dump() for f in result.input_files] != commands.facts(asset)
                or (
                    row.state == "succeeded"
                    and (
                        result.exit_code != 0 or not result.checks or not all(c.valid for c in result.checks)
                    )
                )
            ):
                return "conflict", "持久产物与运行结果不一致，不能作为通过或恢复依据"
        except (BusinessError, OSError, ValueError):
            return "conflict", "产物缺失、被修改或无法核对；原结论保留为历史，需排查"
    if row.state == "succeeded" and not d["artifacts"]:
        return "conflict", "成功记录缺少可核对产物，不能作为当前有效通过依据"
    return "completed", "原运行已终结，保留退出码和结果；不能用恢复重跑，成功不代表阶段验收"


def task_public(session, settings, row):
    from . import commands

    verdict, message = inspect(session, settings, row)
    records = history(session, row.id)
    blockers = []
    asset = session.get(CommandAsset, row.asset_id)
    if verdict == "not_executed":
        try:
            root, grant = commands.prerequisites(session, settings, row.project_id, asset, row.id)
            if grant.id != row.details["authorization_id"]:
                raise BusinessError("AUTHORIZATION_CHANGED", "原执行授权已变化，不能继承恢复", 409)
            with workspace_lock(settings, root):
                pass
            if asset.payload["recipe_id"] == "static_preview" and not commands.available(
                asset.payload["port"]
            ):
                raise BusinessError("PORT_IN_USE", "端口占用，无关程序保留", 409)
        except (BusinessError, OSError) as exc:
            blockers.append(exc.message if isinstance(exc, BusinessError) else "本机环境无法核对")
    else:
        blockers.append(message)
    latest = records[0] if records else None
    reviewed = bool(
        latest
        and latest.payload["phase"] in {"reconciled", "reviewed"}
        and latest.payload["run_hash"] == run_hash(row)
    )
    if verdict == "not_executed" and not reviewed:
        blockers.append("请先保存当前恢复核对，旧核对不能批准新事实")
    state = "unknown"
    if verdict == "not_executed":
        state = "pending"
    elif verdict == "stopped" or row.state == "stopped":
        state = "stopped"
    elif verdict == "active":
        state = "pending" if row.state == "queued" else "running"
    elif verdict == "completed":
        state = "succeeded" if row.state == "succeeded" else "failed"
    return {
        "run": commands.run_public(row),
        "verdict": verdict,
        "message": message,
        "completed_steps": int(verdict == "completed"),
        "total_steps": 1,
        "step_state": state,
        "checkpoint_id": latest.id if latest else None,
        "expected_hash": run_hash(row),
        "can_resume": verdict == "not_executed" and not blockers,
        "blockers": blockers,
        "checkpoints": [
            {"id": c.id, "created_at": timestamp(c.created_at), **c.payload} for c in records[:30]
        ],
        "checkpoint_count": len(records),
        "asset_hash": digest(asset.payload),
        "authorization_id": row.details["authorization_id"],
    }


def install(app, sessions, settings):
    from . import commands

    prefix = "/api/v1/factory/projects/{project_id}/recovery"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            project(session, project_id)
            query = select(CommandRun).where(CommandRun.project_id == project_id)
            runs = list(
                session.scalars(query.order_by(CommandRun.created_at.desc(), CommandRun.id).limit(100))
            )
            model_query = select(RequirementRecord).where(
                RequirementRecord.project_id == project_id, RequirementRecord.kind == "run"
            )
            models = session.scalars(model_query.order_by(RequirementRecord.created_at.desc()).limit(100))
            analyses = []
            for r in models:
                p = r.payload
                analyses.append(
                    {
                        "id": r.id,
                        "state": p["state"],
                        "attempts": [{"attempt": a["attempt"], "state": a["state"]} for a in p["attempts"]],
                        "reserved_micro": p["reserved_micro"]
                        if p["state"] in {"queued", "running", "result_unknown"}
                        else 0,
                        "settled_micro": p["settled_micro"],
                        "can_resume": False,
                        "message": "外部结果未知，保留预算；提供方查询/费用对账待人工核对，禁止重发"
                        if p["state"] == "result_unknown"
                        else "沿用原模型运行记录；本页只读，不发起调用",
                    }
                )
            return {
                "tasks": [task_public(session, settings, r) for r in runs],
                "analyses": analyses,
                "task_count": session.scalar(select(func.count()).select_from(query.subquery())),
                "analysis_count": session.scalar(select(func.count()).select_from(model_query.subquery())),
                "message": "最多显示最近100条各类运行和每次运行最近30个检查点；步骤进度不代表产品或阶段完成率",
            }

    @app.post(prefix + "/runs/{run_id}/check", response_model=Task)
    def check(project_id: str, run_id: str, body: Review, idempotency_key: str | None = Header(default=None)):
        validate_reason(body.reason)

        def operation(session):
            row = commands.owned(session, project_id, run_id, CommandRun)
            verdict, _ = inspect(session, settings, row)
            checkpoint(session, row, "reviewed", verdict, body.reason)
            return {"run_id": row.id}

        mutate(
            sessions, project_id, "recovery_check:" + run_id, body.model_dump(), idempotency_key, operation
        )
        with sessions() as session:
            return task_public(session, settings, commands.owned(session, project_id, run_id, CommandRun))

    @app.post(prefix + "/runs/{run_id}/resume", response_model=Task)
    def resume(
        project_id: str, run_id: str, body: Resume, idempotency_key: str | None = Header(default=None)
    ):
        validate_reason(body.reason)

        def operation(session):
            row = commands.owned(session, project_id, run_id, CommandRun)
            records = history(session, run_id)
            if (
                not records
                or records[0].id != body.checkpoint_id
                or run_hash(row) != body.expected_hash
                or records[0].payload["run_hash"] != body.expected_hash
            ):
                raise BusinessError("REVISION_CONFLICT", "运行或核对已变化，请刷新后重新核对", 409)
            task = task_public(session, settings, row)
            if not task["can_resume"]:
                raise BusinessError("RECOVERY_BLOCKED", "不能恢复：" + "；".join(task["blockers"]), 409)
            asset = session.get(CommandAsset, row.asset_id)
            with workspace_lock(settings, Path(asset.payload["root"])):
                row.state = "queued"
                row.details = {
                    **row.details,
                    "ended_at": None,
                    "error_code": None,
                    "health": "not_started",
                    "message": "恢复已确认，继续同一任务的确定未执行步骤",
                }
                checkpoint(session, row, "resumed", "not_executed", body.reason)
            return {"run_id": row.id}

        mutate(
            sessions, project_id, "recovery_resume:" + run_id, body.model_dump(), idempotency_key, operation
        )
        with sessions() as session:
            return task_public(session, settings, commands.owned(session, project_id, run_id, CommandRun))


def validate_reason(reason):
    if not reason.strip() or execution.contains_secret(reason):
        raise BusinessError("INPUT_INVALID", "核对依据必须明确，且不得填写秘密", 422)
