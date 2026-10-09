"""Deterministic requirements, append-only revisions and explicit confirmations."""

from contextlib import contextmanager
from datetime import UTC
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select, text

from .db import AuditEvent, Project, RequestRecord, RequirementRecord, RequirementRevision, now
from .requirement_schemas import (
    FIELD_LABELS,
    AnswerInput,
    BaselineOutput,
    DraftBody,
    DraftOutput,
    FreezeInput,
    RevisionInput,
    RoundOutput,
    SaveDraft,
    WorkspaceOutput,
)
from .scanner import BusinessError, digest


def timestamp(value=None):
    return (value or now()).replace(tzinfo=UTC).isoformat()


def record(session, project_id, kind, payload):
    identifier = str(uuid4())
    item = RequirementRecord(id=identifier, project_id=project_id, kind=kind, payload=payload)
    session.add(item)
    session.flush()
    return item


def audit(session, project_id, action, details):
    session.add(
        AuditEvent(
            id=str(uuid4()),
            project_id=project_id,
            action=action,
            details={**details, "actor": "local_operator", "role": "product_owner"},
        )
    )


def project(session, project_id):
    item = session.get(Project, project_id)
    if not item:
        raise BusinessError("NOT_FOUND", "项目不存在", 404)
    return item


def latest(session, project_id):
    owner = project(session, project_id)
    item = session.scalar(
        select(RequirementRevision)
        .where(RequirementRevision.project_id == project_id)
        .order_by(RequirementRevision.revision.desc())
        .limit(1)
    )
    if item:
        return item.revision, item.payload, timestamp(item.created_at)
    fields = [
        {
            "key": key,
            "status": "unknown",
            "content": "",
            "source": "user",
            "source_ref": "",
            "confirmed_by": "",
        }
        for key in FIELD_LABELS
    ]
    fields[0].update(
        status="known",
        content=f"目标用户：{owner.payload['target_user']}\n核心任务：{owner.payload['core_task']}",
        source="project",
        source_ref=f"project:{project_id}@{owner.revision}",
    )
    return 0, {"fields": fields, "items": []}, None


def gaps(body):
    result = []
    for field in body["fields"]:
        key = field["key"]
        if field["status"] == "unknown":
            deferred = key == "quality"
            result.append(
                {
                    "id": f"gap:{key}",
                    "field": key,
                    "message": f"{FIELD_LABELS[key]}尚未确认",
                    "severity": "deferred" if deferred else "blocking",
                    "stage": "首次真实效果验收" if deferred else "需求准备",
                    "recommendation": "首次真实冒烟用 5—10 条样例评分"
                    if deferred
                    else "填写实际结论；不确定时保持待定",
                    "owner": "local_operator",
                    "status": "deferred" if deferred else "open",
                }
            )
        if field["status"] == "not_applicable" and key in {"user_task", "input_output", "priority", "data"}:
            result.append(
                {
                    "id": f"gap:na:{key}",
                    "field": key,
                    "message": f"{FIELD_LABELS[key]}不能省略",
                    "severity": "blocking",
                    "stage": "需求准备",
                    "recommendation": "补充该项目实际需求与边界",
                    "owner": "local_operator",
                    "status": "open",
                }
            )
    if not body["items"]:
        result.append(
            {
                "id": "gap:items",
                "field": "items",
                "message": "至少添加一条可验收的需求条目",
                "severity": "blocking",
                "stage": "需求准备",
                "recommendation": "明确用户价值、触发、规则、异常与验收",
                "owner": "local_operator",
                "status": "open",
            }
        )
    return result


def draft_output(session, project_id):
    revision, body, saved = latest(session, project_id)
    return {
        **body,
        "project_id": project_id,
        "revision": revision,
        "content_hash": digest(body),
        "saved_at": saved,
        "gaps": gaps(body),
    }


def require_revision(session, project_id, expected):
    revision, body, _ = latest(session, project_id)
    if expected != revision:
        raise BusinessError("REVISION_CONFLICT", "需求已更新；输入已保留，请重新读取并核对版本", 409)
    return revision, body


def save_revision(session, project_id, revision, body):
    DraftBody.model_validate(body)
    session.add(
        RequirementRevision(id=str(uuid4()), project_id=project_id, revision=revision + 1, payload=body)
    )
    session.flush()
    audit(session, project_id, "requirement_saved", {"revision": revision + 1, "hash": digest(body)})
    return draft_output(session, project_id)


def field_decision(session, project_id, field, revision):
    record(
        session,
        project_id,
        "decision",
        {
            "question": f"请确认{FIELD_LABELS[field['key']]}",
            "conclusion": field["content"] or "不确定",
            "status": "pending" if field["status"] == "unknown" else "confirmed",
            "revision": revision,
            "source": field["source_ref"] or f"draft:{revision}:{field['key']}",
            "created_at": timestamp(),
        },
    )


def owned_record(session, identifier, project_id, kind):
    item = session.get(RequirementRecord, identifier)
    if not item or item.project_id != project_id or item.kind != kind:
        raise BusinessError("NOT_FOUND", "记录不存在或不属于此项目", 404)
    return item


@contextmanager
def locked(sessions):
    with sessions() as session:
        # Obtain the SQLite writer lock before inspecting revisions or budgets.
        session.execute(text("UPDATE requests SET key=key WHERE 0"))
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def mutate(sessions, project_id, action, body, key, callback):
    if (
        not key
        or not 8 <= len(key) <= 120
        or not all(c.isascii() and (c.isalnum() or c in "_-") for c in key)
    ):
        raise BusinessError("INPUT_INVALID", "确认请求缺少有效幂等标识", 422)
    marker = f"a2:{project_id}:{key}"
    request_hash = digest({"action": action, "body": body})
    with locked(sessions) as session:
        project(session, project_id)
        existing = session.get(RequestRecord, marker)
        if existing:
            if existing.request_hash != request_hash:
                raise BusinessError("IDEMPOTENCY_CONFLICT", "同一确认标识对应不同内容", 409)
            return existing.response
        from .backups import KINDS, archived

        if action not in KINDS and archived(session, project_id):
            raise BusinessError("PROJECT_ARCHIVED", "项目已归档，请先取消归档；旧任务不会自动运行", 409)
        result = callback(session)
        session.add(RequestRecord(key=marker, request_hash=request_hash, response=result))
        return result


def round_payload(item):
    return {**item.payload, "id": item.id, "project_id": item.project_id}


def workspace(session, project_id):
    draft = draft_output(session, project_id)
    records = session.scalars(
        select(RequirementRecord)
        .where(
            RequirementRecord.project_id == project_id,
            RequirementRecord.kind.in_(["baseline", "round", "decision"]),
        )
        .order_by(RequirementRecord.created_at.desc())
    ).all()
    rounds = [r for r in records if r.kind == "round" and r.payload["revision"] == draft["revision"]]
    decisions = [r for r in records if r.kind == "decision"]
    seen = set()
    active: list[dict] = []
    for r in decisions:
        question = r.payload["question"]
        if question not in seen and len(active) < 10:
            active.append({**r.payload, "id": r.id})
            seen.add(question)
    return {
        "draft": draft,
        "baselines": [{**r.payload, "id": r.id} for r in records if r.kind == "baseline"],
        "round": round_payload(rounds[0]) if rounds else None,
        "decisions": active,
        "revisions": list(
            session.scalars(
                select(RequirementRevision.revision)
                .where(RequirementRevision.project_id == project_id)
                .order_by(RequirementRevision.revision.desc())
            )
        ),
    }


def install(app, sessions):
    prefix = "/api/v1/factory/projects/{project_id}/requirements"

    @app.get(prefix, response_model=WorkspaceOutput)
    def get_requirements(project_id: str):
        with sessions() as session:
            return workspace(session, project_id)

    @app.get(prefix + "/revisions/{revision}", response_model=DraftOutput)
    def get_revision(project_id: str, revision: int):
        with sessions() as session:
            project(session, project_id)
            item = session.scalar(
                select(RequirementRevision).where(
                    RequirementRevision.project_id == project_id, RequirementRevision.revision == revision
                )
            )
            if not item:
                raise BusinessError("NOT_FOUND", "需求修订不存在", 404)
            return {
                **item.payload,
                "project_id": project_id,
                "revision": item.revision,
                "content_hash": digest(item.payload),
                "saved_at": timestamp(item.created_at),
                "gaps": gaps(item.payload),
            }

    @app.post(prefix + "/revisions", response_model=DraftOutput)
    def save(project_id: str, body: SaveDraft, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            revision, old = require_revision(session, project_id, body.expected_revision)
            data = body.model_dump(exclude={"expected_revision"})
            previous = {f["key"]: f for f in old["fields"]}
            for field in data["fields"]:
                prior = previous[field["key"]]
                if field["content"] != prior["content"] or field["source"] != prior["source"]:
                    field.update(source="user", source_ref=f"draft:{revision + 1}:{field['key']}")
                else:
                    field["source_ref"] = prior["source_ref"]
                field["confirmed_by"] = "local_operator" if field["status"] != "unknown" else ""
                if (
                    field["content"] != prior["content"]
                    or field["status"] != prior["status"]
                    or field["confirmed_by"] != prior["confirmed_by"]
                ):
                    field_decision(session, project_id, field, revision + 1)
            return save_revision(session, project_id, revision, data)

        return mutate(sessions, project_id, "save", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/check", response_model=RoundOutput)
    def check(project_id: str, body: RevisionInput, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            revision, data = require_revision(session, project_id, body.expected_revision)
            project(session, project_id)
            questions: list[dict] = []
            for gap in sorted(gaps(data), key=lambda g: g["severity"] != "blocking"):
                key = gap["field"]
                if key not in FIELD_LABELS or any(q["id"] == key for q in questions):
                    continue
                questions.append(
                    {
                        "id": key,
                        "question": f"请确认{FIELD_LABELS[key]}",
                        "recommendation": "本机交付，无云上线账号要求"
                        if key == "launch"
                        else gap["recommendation"],
                        "example": {
                            "input_output": "输入需求文本，核对草稿，保存需求基线",
                            "workflow": "先整理草稿，人工确认后才能进入开发",
                            "priority": "第一版优先完成需求整理，多人协作后做",
                            "model_cost": "不指定厂商，每任务最多 2 元",
                            "data": "仅非敏感资料，本机保存，不外发个人信息",
                        }.get(key, "填写实际情况；暂时不确定也可以"),
                        "impact": "未确认会阻止当前需求基线冻结"
                        if gap["severity"] == "blocking"
                        else "真实效果验收前仍需样例打分",
                        "recommended_status": "not_applicable" if key == "launch" else "unknown",
                    }
                )
                if len(questions) == 5:
                    break
            payload = {
                "revision": revision,
                "version": digest({"revision": revision, "questions": questions}),
                "questions": questions,
                "answered": False,
            }
            item = record(session, project_id, "round", payload)
            audit(session, project_id, "requirement_checked", {"revision": revision, "round_id": item.id})
            return round_payload(item)

        return mutate(sessions, project_id, "check", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/answers", response_model=DraftOutput)
    def answer(project_id: str, body: AnswerInput, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            revision, data = require_revision(session, project_id, body.expected_revision)
            item = owned_record(session, body.round_id, project_id, "round")
            round_data = item.payload
            if (
                round_data["answered"]
                or round_data["version"] != body.question_version
                or round_data["revision"] != revision
            ):
                raise BusinessError("QUESTION_STALE", "问题版本已失效，请重新体检", 409)
            questions = {q["id"]: q for q in round_data["questions"]}
            if len({a.question_id for a in body.answers}) != len(body.answers):
                raise BusinessError("INPUT_INVALID", "同一问题只能回答一次", 422)
            fields = {f["key"]: f for f in data["fields"]}
            for answer in body.answers:
                if answer.question_id not in questions:
                    raise BusinessError("QUESTION_STALE", "回答不属于当前问题版本", 409)
                q = questions[answer.question_id]
                status = (
                    "known"
                    if answer.mode == "custom"
                    else q["recommended_status"]
                    if answer.mode == "recommended"
                    else "unknown"
                )
                value = (
                    answer.content.strip()
                    if answer.mode == "custom"
                    else q["recommendation"]
                    if answer.mode == "recommended"
                    else "不确定"
                )
                if value in {"不确定", "不知道", "待确认", "未确定"}:
                    status = "unknown"
                if status == "known" and len(value) < 3:
                    raise BusinessError("INPUT_INVALID", "实际结论需至少三个字", 422)
                fields[answer.question_id].update(
                    status=status,
                    content=value,
                    source="user",
                    source_ref=f"round:{item.id}:{answer.question_id}",
                    confirmed_by="local_operator" if status != "unknown" else "",
                )
                record(
                    session,
                    project_id,
                    "decision",
                    {
                        "question": q["question"],
                        "conclusion": value,
                        "status": "confirmed" if status != "unknown" else "pending",
                        "revision": revision + 1,
                        "source": f"round:{item.id}@{round_data['version']}",
                        "created_at": timestamp(),
                    },
                )
            item.payload = {**round_data, "answered": True}
            audit(
                session,
                project_id,
                "question_answered",
                {"round_id": item.id, "question_ids": [a.question_id for a in body.answers]},
            )
            return save_revision(session, project_id, revision, {**data, "fields": list(fields.values())})

        return mutate(sessions, project_id, "answer", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/freeze", response_model=BaselineOutput)
    def freeze(project_id: str, body: FreezeInput, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            revision, data = require_revision(session, project_id, body.expected_revision)
            if digest(data) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "待确认内容摘要已变化", 409)
            pending = gaps(data)
            if revision == 0 or any(g["severity"] == "blocking" for g in pending):
                raise BusinessError("BASELINE_BLOCKED", "当前仍有阻断缺口；请补齐后再冻结", 409)
            existing = session.scalars(
                select(RequirementRecord).where(
                    RequirementRecord.project_id == project_id, RequirementRecord.kind == "baseline"
                )
            ).all()
            for prior in existing:
                if prior.payload["revision"] == revision:
                    return {**prior.payload, "id": prior.id}
            payload = {
                **data,
                "project_id": project_id,
                "revision": revision,
                "content_hash": digest(data),
                "deferred": pending,
                "approved_by": "local_operator",
                "reason": body.reason,
                "created_at": timestamp(),
            }
            item = record(session, project_id, "baseline", payload)
            audit(
                session,
                project_id,
                "baseline_frozen",
                {"baseline_id": item.id, "revision": revision, "hash": digest(data)},
            )
            return {**payload, "id": item.id}

        return mutate(sessions, project_id, "freeze", body.model_dump(), idempotency_key, operation)

    @app.get(prefix + "/baselines/{baseline_id}", response_model=BaselineOutput)
    def baseline(project_id: str, baseline_id: str):
        with sessions() as session:
            item = owned_record(session, baseline_id, project_id, "baseline")
            return {**item.payload, "id": item.id}
