"""Deterministic, source-bound handoffs and immutable audit annotations."""

import json

from fastapi import Header, Query
from fastapi.responses import Response
from sqlalchemy import select

from . import acceptance, backups, contexts, execution, models, planning, requirements
from .db import (
    AuditEvent,
    CommandAsset,
    CommandRun,
    ExecutionAsset,
    PatchAsset,
    PatchRun,
    PlanningAsset,
    RequirementRecord,
)
from .handoff_schemas import Asset, Correction, Event, SaveSummary, Summary, Workspace
from .scanner import BusinessError, digest

KINDS = ("handoff_summary", "audit_correction")
NOTICE = "接续包仅含本版安全摘要与引用，不含聊天、原始文档或执行许可。冲突与待验必须核对；更正只追加说明，不改变业务事实、授权或验收。"


def safe(value, settings):
    if not contexts.safe(json.dumps(value, ensure_ascii=False), settings):
        raise BusinessError("HANDOFF_UNSAFE", "摘要或说明包含受保护内容，请先脱敏；原记录保留", 409)


def rows(session, pid, kind):
    return list(
        session.scalars(
            select(RequirementRecord)
            .where(RequirementRecord.project_id == pid, RequirementRecord.kind == kind)
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        )
    )


def reference(row, section, version=None):
    return dict(
        id=row.id,
        kind=getattr(row, "kind", "record"),
        version=str(version if version is not None else getattr(row, "revision", digest(row.payload))),
        href=f"/projects/{row.project_id}/{section}",
    )


def basis(session, settings, pid):
    owner = requirements.project(session, pid)
    files, omitted, git = backups.inventory(session, settings, pid, False)
    sources = {}
    for name, table in backups.TABLES.items():
        values = []
        for raw in session.execute(select(table).where(table.c.project_id == pid)).mappings():
            row = backups.serialize(raw)
            if name == "requirement_records" and row["kind"] in KINDS:
                continue
            if name == "audit_events" and row["action"].startswith("handoff_"):
                continue
            values.append(digest(row))
        sources[name] = sorted(values)
    return (
        digest(
            dict(
                project=owner.payload,
                revision=owner.revision,
                sources=sources,
                files=[{k: v for k, v in f.items() if k != "content"} for f in files],
                omitted=omitted,
                git=git,
            )
        ),
        files,
        omitted,
    )


def collect(session, settings, pid):
    owner = requirements.project(session, pid)
    h, files, omitted = basis(session, settings, pid)
    draft = requirements.draft_output(session, pid)
    bases = planning.baselines(session, pid)
    base = bases[0] if bases else None
    pending = [g["message"] for g in draft["gaps"]]
    conflicts, completed, grants, sources, tasks = [], [], [], [], []
    if not base:
        pending.append("尚无冻结需求基线，不能把草稿当作有效需求")
    else:
        sources.append(reference(base, "", base.payload["revision"]))
        pending.extend(g["message"] for g in base.payload.get("deferred", []))
        if draft["revision"] != base.payload["revision"]:
            conflicts.append("当前草稿与冻结需求修订不同；保留两者，不能按较新时间自动替换有效基线")
    plan = planning.latest(session, pid, "plan")
    if plan:
        sources.append(reference(plan, "planning"))
        try:
            execution.source(session, settings, pid, plan.id)
        except BusinessError as exc:
            conflicts.append("规划来源需核对：" + exc.message)
        for stage in plan.payload["text"]["stages"]:
            decision = acceptance.latest(session, pid, stage["id"], "acceptance_decision")
            if not decision:
                pending.append(stage["id"] + " 尚无有效验收通过结论")
                continue
            view = acceptance.public(session, settings, decision)
            sources.append(reference(decision, "acceptance"))
            pending.extend(stage["id"] + "：" + issue for issue in view["blockers"])
            if decision.payload["outcome"] == "passed" and view["current"] and not view["blockers"]:
                completed.append(reference(decision, "acceptance"))
            else:
                pending.append(stage["id"] + " 验收待核对或尚未通过")
                conflicts.extend(view["issues"])
        for task in plan.payload["text"]["tasks"]:
            row = execution.latest(session, pid, task["id"], "plan")
            runs = list(
                session.scalars(
                    select(CommandRun)
                    .where(CommandRun.project_id == pid)
                    .order_by(CommandRun.created_at.desc(), CommandRun.id.desc())
                )
            )
            # Include every attempt; a newer success cannot erase an older unknown result.
            from .db import CommandAsset

            own_runs = [
                r
                for r in runs
                if (a := session.get(CommandAsset, r.asset_id))
                and (ep := session.get(execution.ExecutionAsset, a.execution_id))
                and ep.task_id == task["id"]
            ]
            tasks.append(
                dict(
                    id=task["id"],
                    title=task["title"],
                    stage_id=task["stage_id"],
                    status="准备已保存，执行及验收仍需核对" if row else "尚无执行准备",
                    runs=[dict(id=r.id, state=r.state) for r in own_runs],
                )
            )
            if row:
                sources.append(reference(row, "execution"))
                grant = execution.authorization(session, pid, row)
                problems = execution.source_file_issues(session, settings, pid, row)
                if grant and not problems and not backups.archived(session, pid):
                    grants.append(reference(grant, "execution"))
                elif grant:
                    conflicts.append(task["id"] + " 原范围授权来源变化或项目归档，不能视为当前有效授权")
                conflicts.extend(msg for _, msg in problems)
            for run in own_runs:
                if run.state in {"interrupted", "paused", "starting", "running", "serving", "queued"}:
                    pending.append(
                        task["id"] + " 运行 " + run.id + " 状态 " + run.state + "，先核对，不自动重发"
                    )
    else:
        pending.append("尚无阶段计划，先确认需求和技术规划")
    model_runs = rows(session, pid, "run")
    used = {r.payload.get("authorization_id") for r in model_runs}
    for grant in rows(session, pid, "authorization"):
        if grant.payload.get("revoked") or grant.id in used or models.expired(grant.payload["expires_at"]):
            continue
        try:
            preview = models.validate_preview(session, settings, pid, grant.payload["preview_id"])
            if preview.payload["content_hash"] == grant.payload["content_hash"] and not backups.archived(
                session, pid
            ):
                grants.append(reference(grant, ""))
        except BusinessError:
            conflicts.append("原模型资料授权预览已失效；不能作为当前有效外发授权")
    source = backups.latest(session, pid, "restore_source")
    if source:
        sources.append(reference(source, "backups"))
        conflicts.append("此项目来自恢复的新身份；原授权、执行和验收只作历史资料，必须重新确认")
    # A replaced plan must not hide active/unknown attempts belonging to its old tasks.
    for run in session.scalars(select(CommandRun).where(CommandRun.project_id == pid)):
        if run.state in {"interrupted", "paused", "starting", "running", "serving", "queued"}:
            pending.append(
                "历史命令运行 " + run.id + " 状态 " + run.state + "，即使当前计划已变化也必须先核对"
            )
    for run in session.scalars(select(PatchRun).where(PatchRun.project_id == pid)):
        if run.state in {"preparing", "applying", "rollback_required", "result_unknown", "interrupted"}:
            pending.append("补丁运行 " + run.id + " 状态 " + run.state + "，必须先核对恢复")
    for run in rows(session, pid, "run"):
        if run.payload.get("state") in {"queued", "running", "result_unknown"}:
            pending.append("模型运行 " + run.id + " 状态 " + run.payload["state"] + "，禁止未核对重发")
    unchecked = sum(f["sha256"] is None for f in files) + len(omitted)
    if unchecked:
        pending.append(f"有 {unchecked} 项文件或目录未安全核验；接续包不包含原文件，不能视为全工程完成")
    archived = backups.archived(session, pid)
    pending = list(dict.fromkeys(pending))
    conflicts = list(dict.fromkeys(conflicts))
    next_step = (
        "项目已归档；只读核对历史，需要开展任务时先明确取消归档并重新检查来源"
        if archived
        else "先核对冲突与待验，回到对应来源页面确认；摘要不自动执行下一任务"
        if conflicts or pending
        else "由产品负责人核对当前通过结论与具体下一任务，取得相应授权后再执行"
    )
    result = Summary.model_validate(
        dict(
            project_id=pid,
            product=owner.payload["name"],
            archived=archived,
            requirement=reference(base, "", base.payload["revision"]) if base else None,
            draft_revision=draft["revision"],
            completed=completed,
            pending=pending,
            tasks=tasks,
            authorizations=grants,
            sources=sources,
            conflicts=conflicts,
            next_step=next_step,
            state_hash=h,
            notice=NOTICE,
        )
    ).model_dump()
    result["state_hash"] = digest(result)  # Also binds validity checks, including time expiry.
    safe(result, settings)
    return result


def owned(session, pid, identifier):
    requirements.project(session, pid)
    row = session.get(RequirementRecord, identifier)
    if not row or row.project_id != pid or row.kind != "handoff_summary":
        raise BusinessError("NOT_FOUND", "接续摘要不属于本项目", 404)
    return row


def public(row, h, settings):
    safe(row.payload, settings)
    current = row.payload["summary"]["state_hash"] == h
    return Asset.model_validate(
        dict(
            id=row.id,
            content_hash=digest(row.payload),
            created_at=requirements.timestamp(row.created_at),
            current=current,
            issues=[] if current else ["业务资料或安全文件清单已变化，原摘要保留，接续前须重新核对"],
            summary=row.payload["summary"],
            reason=row.payload["reason"],
        )
    ).model_dump()


def event_hash(row):
    return digest(
        dict(
            id=row.id,
            project_id=row.project_id,
            action=row.action,
            details=row.details,
            created_at=requirements.timestamp(row.created_at),
        )
    )


def audit_view(session, settings, pid, row):
    chain = sorted(
        [r for r in rows(session, pid, "audit_correction") if r.payload["event_id"] == row.id],
        key=lambda r: r.payload["revision"],
    )
    fields = {k: str(row.details.get(k, "未记录")) for k in ("actor", "role", "object", "version", "reason")}
    reason_ref = row.details.get("reason_ref")
    if isinstance(reason_ref, dict):
        table = {
            t.__tablename__: t
            for t in (RequirementRecord, PlanningAsset, ExecutionAsset, PatchAsset, CommandAsset)
        }.get(str(reason_ref.get("table", "")))
        source = session.get(table, reason_ref.get("id")) if table else None
        if source and source.project_id == pid and digest(source.payload) == reason_ref.get("hash"):
            fields["reason"] = str(source.payload["reason"])
        else:
            fields["reason"] = "具体理由的版本引用需核对，原审计保留"
    missing = [k for k, v in fields.items() if v == "未记录"]
    # Expose only the allowlisted audit metadata; never raw details/document content.
    for k, v in fields.items():
        if not contexts.safe(v, settings):
            fields[k] = "受保护字段已排除"
    corrections = []
    preceding: list[dict] = []
    for index, r in enumerate(chain):
        if (
            r.payload["revision"] != index + 1
            or r.payload["event_hash"] != event_hash(row)
            or r.payload["parent_chain_hash"] != digest(preceding)
        ):
            raise BusinessError("AUDIT_CONFLICT", "更正链或原事件存在冲突，不能按时间替代事实核查", 409)
        preceding.append(dict(id=r.id, payload_hash=digest(r.payload)))
        safe(r.payload, settings)
        corrections.append(dict(id=r.id, created_at=requirements.timestamp(r.created_at), **r.payload))
    return Event.model_validate(
        dict(
            id=row.id,
            action=row.action if contexts.safe(row.action, settings) else "受保护动作名已排除",
            **fields,
            created_at=requirements.timestamp(row.created_at),
            missing=missing,
            event_hash=event_hash(row),
            chain_hash=digest([dict(id=r.id, payload_hash=digest(r.payload)) for r in chain]),
            corrections=corrections,
        )
    ).model_dump()


def append(session, pid, kind, payload):
    row = requirements.record(
        session, pid, kind, dict(schema_version=1, actor="local_operator", role="product_owner", **payload)
    )
    requirements.audit(
        session,
        pid,
        "handoff_" + kind,
        dict(object=row.id, version=digest(row.payload), reason=payload["reason"]),
    )
    return row


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/handoff"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str, audit_offset: int = Query(default=0, ge=0, le=1000000)):
        with sessions() as session:
            current = collect(session, settings, project_id)
            all_events = list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.project_id == project_id)
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                )
            )
            return dict(
                current=current,
                history=[
                    public(r, current["state_hash"], settings)
                    for r in rows(session, project_id, "handoff_summary")
                ],
                events=[
                    audit_view(session, settings, project_id, e)
                    for e in all_events[audit_offset : audit_offset + 200]
                ],
                audit_total=len(all_events),
                audit_omitted=max(0, len(all_events) - len(all_events[audit_offset : audit_offset + 200])),
                notice=NOTICE,
            )

    @app.get(prefix + "/events/{event_id}", response_model=Event)
    def event(project_id: str, event_id: str):
        with sessions() as session:
            requirements.project(session, project_id)
            row = session.get(AuditEvent, event_id)
            if not row or row.project_id != project_id:
                raise BusinessError("NOT_FOUND", "审计事件不属于本项目", 404)
            return audit_view(session, settings, project_id, row)

    @app.post(prefix + "/summaries", response_model=Asset)
    def save(project_id: str, body: SaveSummary, idempotency_key: str | None = Header(default=None)):
        def action(session):
            current = collect(session, settings, project_id)
            if current["state_hash"] != body.expected_state_hash:
                raise BusinessError(
                    "REVISION_CONFLICT", "来源已变化；输入与历史保留，请核对后明确采用当前事实", 409
                )
            safe(body.model_dump(), settings)
            row = append(session, project_id, "handoff_summary", dict(summary=current, reason=body.reason))
            return public(row, current["state_hash"], settings)

        return requirements.mutate(
            sessions, project_id, "handoff_summary", body.model_dump(), idempotency_key, action
        )

    @app.get(prefix + "/summaries/{summary_id}", response_model=Asset)
    def asset(project_id: str, summary_id: str):
        with sessions() as session:
            row = owned(session, project_id, summary_id)
            return public(row, collect(session, settings, project_id)["state_hash"], settings)

    @app.get(prefix + "/summaries/{summary_id}/download")
    def download(project_id: str, summary_id: str):
        with sessions() as session:
            row = owned(session, project_id, summary_id)
            value = public(row, collect(session, settings, project_id)["state_hash"], settings)
            content = json.dumps(
                dict(format="product-factory-handoff", version=1, asset=value, notice=NOTICE),
                ensure_ascii=False,
            ).encode()
            if len(content) > 1024 * 1024:
                raise BusinessError("HANDOFF_LIMIT", "接续包超过1MiB，不能冒充完整摘要", 409)
            return Response(
                content,
                media_type="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="handoff-{row.id}.json"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    @app.post(prefix + "/corrections", response_model=Event)
    def correct(project_id: str, body: Correction, idempotency_key: str | None = Header(default=None)):
        def action(session):
            event = session.get(AuditEvent, body.event_id)
            if not event or event.project_id != project_id:
                raise BusinessError("NOT_FOUND", "审计事件不属于本项目", 404)
            view = audit_view(session, settings, project_id, event)
            if (
                view["event_hash"] != body.expected_event_hash
                or view["chain_hash"] != body.expected_chain_hash
            ):
                raise BusinessError("REVISION_CONFLICT", "原事件或更正链已变化；输入保留，请重新核对", 409)
            safe(body.model_dump(), settings)
            append(
                session,
                project_id,
                "audit_correction",
                dict(
                    event_id=event.id,
                    event_hash=view["event_hash"],
                    parent_chain_hash=view["chain_hash"],
                    revision=len(view["corrections"]) + 1,
                    note=body.note,
                    reason=body.reason,
                ),
            )
            return audit_view(session, settings, project_id, event)

        return requirements.mutate(
            sessions, project_id, "audit_correction", body.model_dump(), idempotency_key, action
        )
