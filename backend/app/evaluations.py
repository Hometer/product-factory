"""Append-only human effect evaluation; never dispatch models or accept a stage."""

import json
from pathlib import Path

from fastapi import Header
from sqlalchemy import select

from . import contexts, models, provider_capabilities, requirements
from .db import RequirementRecord
from .evaluation_schemas import (
    Asset,
    Plan,
    PlanPayload,
    Sample,
    Score,
    ScorePayload,
    Standard,
    StandardPayload,
    Workspace,
)
from .scanner import BusinessError, digest

KINDS = ("evaluation_plan", "evaluation_score", "evaluation_standard")
SOURCES = ("mock", "real_local", "real_cloud", "fallback_template")
NOTICE = "样例资料与模型来源分别记录；人工来源未由平台独立运行。Mock、合成、本地人工报告与降级不冒充真实云验证。均分不能抵消硬错误；效果记录不批准阶段验收、费用或外发。"


def rows(session, pid, kind=None):
    return list(
        session.scalars(
            select(RequirementRecord)
            .where(
                RequirementRecord.project_id == pid,
                RequirementRecord.kind == kind if kind else RequirementRecord.kind.in_(KINDS),
            )
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        )
    )


def latest(session, pid, kind):
    return next(iter(rows(session, pid, kind)), None)


def owned(session, pid, identifier):
    requirements.project(session, pid)
    row = session.get(RequirementRecord, identifier)
    if not row or row.project_id != pid or row.kind not in KINDS:
        raise BusinessError("NOT_FOUND", "效果资产不属于本项目", 404)
    return row


def basis(session, settings, pid):
    revision, data, _ = requirements.latest(session, pid)
    baseline = latest(session, pid, "baseline")
    _, base = models.config(settings)
    return basis_value(revision, data, baseline, base)


def basis_value(revision, data, baseline, base):
    return dict(
        draft_revision=revision,
        draft_hash=digest(data),
        baseline_id=baseline.id if baseline else None,
        baseline_hash=digest(baseline.payload) if baseline else None,
        model=models.MODEL,
        base_url=base,
        prompt_version=models.PROMPT_VERSION,
        prompt_hash=digest((Path(__file__).parent / "prompts/requirements.txt").read_text()),
        adapter_hash=provider_capabilities.adapter_hash(),
    )


def receipts(session, pid, run_id):
    return [r for r in rows(session, pid, "provider_receipt") if r.payload["run_id"] == run_id]


def snapshot_run(session, settings, pid, sample):
    run = requirements.owned_record(session, sample.run_id, pid, "run")
    p = run.payload
    if p["state"] != "succeeded" or not p["candidate"] or p["source"] not in ("mock", "real_cloud"):
        raise BusinessError("INPUT_INVALID", "只能引用本项目成功的结构化需求分析运行", 409)
    preview = requirements.owned_record(session, p["preview_id"], pid, "analysis_preview")
    observed = sorted(receipts(session, pid, run.id), key=lambda r: r.payload["attempt"])
    last = observed[-1].payload if observed else None
    b = basis(session, settings, pid)
    verified = bool(
        last
        and p["source"] == "real_cloud"
        and last["source"] == "real_cloud"
        and last["sent"]
        and last["text_valid"]
        and last["structure_valid"]
        and last["usage_valid"]
        and not last["error_code"]
        and last["model"] == p["model"] == b["model"]
        and last["base_url"] == preview.payload["base_url"] == b["base_url"]
        and last["adapter_hash"] == b["adapter_hash"]
        and last["contract_version"] == provider_capabilities.VERSION
        and last["attempt"] == len(p["attempts"])
        and [r.payload["attempt"] for r in observed] == list(range(1, len(p["attempts"]) + 1))
        and all(
            r.payload["sent"]
            and r.payload["usage_valid"]
            and r.payload["base_url"] == b["base_url"]
            and r.payload["contract_version"] == provider_capabilities.VERSION
            for r in observed
        )
        and sum(r.payload["cost_micro"] or 0 for r in observed) == p["settled_micro"]
        and all(
            r.payload["source"] == p["source"]
            and r.payload["model"] == p["model"]
            and r.payload["adapter_hash"] == b["adapter_hash"]
            for r in observed
        )
        and preview.payload["prompt_hash"] == b["prompt_hash"]
        and p["prompt_version"] == b["prompt_version"]
        and p["revision"] == b["draft_revision"]
    )
    return Sample(
        id=sample.id,
        title=sample.title,
        data_source=sample.data_source,
        source_ref=sample.source_ref,
        expected=sample.expected,
        mode="platform_run",
        input_text=json.dumps(preview.payload["selected_fields"], ensure_ascii=False),
        output_text=json.dumps(p["candidate"], ensure_ascii=False),
        model_source=p["source"],
        model=p["model"],
        prompt_version=p["prompt_version"] + " · " + preview.payload["prompt_hash"],
        knowledge_version="原需求分析仅显式字段；无知识快照消费",
        run_id=run.id,
        run_hash=digest(p),
        preview_hash=digest(preview.payload),
        receipts_hash=digest([{"id": r.id, "payload": r.payload} for r in observed]),
        verified_real=verified,
        first_structure_valid=observed[0].payload["structure_valid"]
        if observed and observed[0].payload["attempt"] == 1
        else None,
        final_structure_valid=last["structure_valid"] if last else None,
        attempts=len(p["attempts"]),
        cost_micro=p["settled_micro"],
        input_tokens=sum(r.payload["input_tokens"] or 0 for r in observed) if observed else None,
        output_tokens=sum(r.payload["output_tokens"] or 0 for r in observed) if observed else None,
        provenance_notice="后端引用持久运行与回执；未启动新调用。"
        + ("真实云依据已核对" if verified else "不能作为当前真实云标准确认依据"),
    )


def snapshot_manual(sample):
    return Sample(
        **{
            k: getattr(sample, k)
            for k in (
                "id",
                "title",
                "data_source",
                "source_ref",
                "expected",
                "mode",
                "input_text",
                "output_text",
                "model_source",
                "model",
                "prompt_version",
                "knowledge_version",
            )
        },
        run_id=None,
        run_hash=None,
        preview_hash=None,
        receipts_hash=None,
        verified_real=False,
        first_structure_valid=None,
        final_structure_valid=None,
        attempts=None,
        cost_micro=None,
        input_tokens=None,
        output_tokens=None,
        provenance_notice="人工来源报告，平台未独立执行或核对本地模型；不计本平台真实云标准确认",
    )


def plan_issues(session, settings, pid, plan):
    if latest(session, pid, "evaluation_plan").id != plan.id:
        return ["样例/模型/提示词/知识/标准已有新计划，旧评分需复验；历史保留"]
    p = plan.payload
    if basis(session, settings, pid) != p["basis"]:
        return ["需求/基线/模型/提示词或适配器版本变化，需复验"]
    issues = []
    if p["context_id"]:
        try:
            context = contexts.owned(session, pid, p["context_id"])
            if digest(context.payload) != p["context_hash"] or contexts.issues(
                session, settings, pid, context
            ):
                issues.append("选中知识快照或其来源变化，需复验")
        except BusinessError:
            issues.append("知识来源不可核对，需复验")
    for sample in p["samples"]:
        if sample["mode"] == "platform_run":
            try:
                from .evaluation_schemas import SampleInput

                current = snapshot_run(
                    session,
                    settings,
                    pid,
                    SampleInput(
                        **{
                            k: sample[k]
                            for k in (
                                "id",
                                "title",
                                "data_source",
                                "source_ref",
                                "expected",
                                "mode",
                                "run_id",
                            )
                        }
                    ),
                ).model_dump()
                if current != sample:
                    issues.append("样例 " + sample["id"] + " 的来源运行或回执变化，需复验")
            except BusinessError:
                issues.append("样例 " + sample["id"] + " 的来源运行不可核对，需复验")
    return issues


def public(session, settings, row):
    if not contexts.safe(json.dumps(row.payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "效果记录可能含当前受保护资料，不能返回原文", 409)
    plan = row if row.kind == "evaluation_plan" else owned(session, row.project_id, row.payload["plan_id"])
    issues = plan_issues(session, settings, row.project_id, plan)
    from . import defects

    issues.extend(defects.conclusion_issues(session, settings, row.project_id, row.id))
    if row.kind == "evaluation_standard":
        current_scores = newest_scores(session, row.project_id, plan.id)
        if set(row.payload["score_ids"]) != {
            s.id for s in current_scores.values() if eligible(plan, s.payload["sample_id"])
        }:
            issues.append("评分已有新记录，原标准确认需重新核对")
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=row.payload,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        current=not issues,
        issues=issues,
    )


def newest_scores(session, pid, plan_id):
    values: dict[str, RequirementRecord] = {}
    for row in rows(session, pid, "evaluation_score"):
        if row.payload["plan_id"] == plan_id:
            values.setdefault(row.payload["sample_id"], row)
    return values


def eligible(plan, sample_id):
    s = next(s for s in plan.payload["samples"] if s["id"] == sample_id)
    return s["verified_real"] and s["data_source"] != "synthetic"


def blockers(plan, scores, current):
    if not plan:
        return ["尚未保存5—10条代表样例的评测计划"]
    reasons = [] if current else ["计划依据已变化，先另存当前计划并重新评分"]
    real = [s for s in plan.payload["samples"] if eligible(plan, s["id"])]
    if len({s["run_id"] for s in real}) != len(real):
        reasons.append("重复引用同一运行不能算多条真实样例")
    if len({s["input_text"] for s in real}) != len(real):
        reasons.append("重复输入不能算不同代表样例")
    if len(real) < 5:
        reasons.append("不足5条可核对的真实业务/公开资料真实云运行；Mock、合成、降级与本地人工报告不计入")
    if any(s["id"] not in scores for s in real):
        reasons.append("真实样例尚未由本机产品负责人逐条评分")
    if plan.payload["minimum_score"] is None:
        reasons.append("效果阈值尚未提议并确认")
    return reasons


def current_plan(session, settings, pid, body):
    p = owned(session, pid, body.plan_id)
    if p.kind != "evaluation_plan":
        raise BusinessError("INPUT_INVALID", "需引用评测计划", 422)
    view = public(session, settings, p)
    if view["content_hash"] != body.expected_hash:
        raise BusinessError("REVISION_CONFLICT", "计划摘要不一致，输入保留", 409)
    if not view["current"]:
        raise BusinessError("EVALUATION_STALE", "效果依据已变化，先另存当前计划；原评分保留", 409)
    return p


def append(session, settings, pid, kind, payload):
    if not contexts.safe(json.dumps(payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "样例或评分含受保护资料，请先脱敏", 409)
    r = requirements.record(session, pid, kind, payload)
    requirements.audit(session, pid, kind + "_saved", {"asset_id": r.id, "kind": kind})
    return public(session, settings, r)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/evaluation"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            b = basis(session, settings, project_id)
            history = [public(session, settings, r) for r in rows(session, project_id)]
            p = latest(session, project_id, "evaluation_plan")
            plan = public(session, settings, p) if p else None
            plan_current = bool(plan and plan["current"])
            scored = newest_scores(session, project_id, p.id) if p else {}
            standard_row = next(
                (
                    r
                    for r in rows(session, project_id, "evaluation_standard")
                    if p and r.payload["plan_id"] == p.id
                ),
                None,
            )
            standard = public(session, settings, standard_row) if standard_row else None
            groups = []
            for source in SOURCES:
                samples = [s for s in p.payload["samples"] if s["model_source"] == source] if p else []
                scores = [scored[s["id"]].payload for s in samples if s["id"] in scored]
                hard = sum(bool(s["hard_errors"]) for s in scores)
                confirmed = bool(standard and standard["current"])
                status = (
                    "needs_reverification"
                    if samples and not plan_current
                    else "failed"
                    if hard
                    else "pending"
                )
                if (
                    status == "pending"
                    and source == "real_cloud"
                    and confirmed
                    and bool(samples)
                    and all(eligible(p, s["id"]) for s in samples)
                    and len(scores) == len(samples)
                ):
                    status = (
                        "passed" if all(s["mean"] >= p.payload["minimum_score"] for s in scores) else "failed"
                    )
                groups.append(
                    dict(
                        model_source=source,
                        total=len(samples),
                        rated=len(scores),
                        hard_failed=hard,
                        stale=len(scores) if plan and not plan["current"] else 0,
                        quantity_compliant=sum(s["quantity_compliant"] is True for s in scores),
                        quantity_noncompliant=sum(s["quantity_compliant"] is False for s in scores),
                        quantity_pending=sum(s["quantity_compliant"] is None for s in scores),
                        mean=round(sum(s["mean"] for s in scores) / len(scores), 3) if scores else None,
                        status=status,
                    )
                )
            pending = blockers(p, scored, plan["current"] if plan else False)
            if not standard or not standard["current"]:
                pending.append("项目标准尚未有效确认；均分不代表验收通过")
            return dict(
                basis=b,
                basis_hash=digest(b),
                plan=plan,
                scores=[public(session, settings, r) for r in scored.values()],
                standard=standard,
                groups=groups,
                eligible_real_count=min(
                    len({s["run_id"] for s in p.payload["samples"] if eligible(p, s["id"])}),
                    len({s["input_text"] for s in p.payload["samples"] if eligible(p, s["id"])}),
                )
                if p and plan_current
                else 0,
                blockers=pending,
                history=history,
                runs=[
                    dict(
                        id=r.id,
                        source=r.payload["source"],
                        model=r.payload["model"],
                        revision=r.payload["revision"],
                    )
                    for r in rows(session, project_id, "run")
                    if r.payload["state"] == "succeeded"
                ],
                contexts=[
                    dict(
                        id=r.id,
                        stage=r.payload["stage"],
                        current=not contexts.issues(session, settings, project_id, r),
                    )
                    for r in contexts.rows(session, project_id)
                ],
                notice=NOTICE,
            )

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/plans", response_model=Asset)
    def plan(project_id: str, body: Plan, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            b = basis(session, settings, project_id)
            old = latest(session, project_id, "evaluation_plan")
            if (old.payload["revision"] if old else 0) != body.expected_revision or digest(
                b
            ) != body.expected_basis_hash:
                raise BusinessError("REVISION_CONFLICT", "评测来源或计划版本变化，输入保留", 409)
            context = contexts.owned(session, project_id, body.context_id) if body.context_id else None
            if context:
                view = contexts.public(session, settings, context)
                if not view["current"]:
                    raise BusinessError("EVALUATION_STALE", "知识快照已失效，先重新选择", 409)
            samples = [
                snapshot_run(session, settings, project_id, s)
                if s.mode == "platform_run"
                else snapshot_manual(s)
                for s in body.samples
            ]
            payload = PlanPayload(
                schema_version=1,
                revision=body.expected_revision + 1,
                previous_id=old.id if old else None,
                basis=b,
                context_id=body.context_id,
                context_hash=digest(context.payload) if context else None,
                samples=samples,
                dimensions=body.dimensions,
                minimum_score=body.minimum_score,
                reason=body.reason,
            ).model_dump()
            return append(session, settings, project_id, "evaluation_plan", payload)

        return requirements.mutate(
            sessions, project_id, "evaluation_plan", body.model_dump(), idempotency_key, operation
        )

    @app.post(prefix + "/scores", response_model=Asset)
    def score(project_id: str, body: Score, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            p = current_plan(session, settings, project_id, body)
            if not any(s["id"] == body.sample_id for s in p.payload["samples"]) or set(body.scores) != set(
                p.payload["dimensions"]
            ):
                raise BusinessError("INPUT_INVALID", "样例需属于本计划且完整填写全部维度", 422)
            previous = newest_scores(session, project_id, p.id).get(body.sample_id)
            if (previous.id if previous else None) != body.previous_score_id:
                raise BusinessError("REVISION_CONFLICT", "本样例评分已有新记录，输入保留", 409)
            payload = ScorePayload(
                schema_version=1,
                plan_id=p.id,
                sample_id=body.sample_id,
                previous_score_id=body.previous_score_id,
                scores=body.scores,
                hard_errors=body.hard_errors,
                mean=sum(body.scores.values()) / len(body.scores),
                quantity_compliant=body.quantity_compliant,
                quantity_reason=body.quantity_reason,
                reviewer="local_operator",
                reason=body.reason,
            ).model_dump()
            return append(session, settings, project_id, "evaluation_score", payload)

        return requirements.mutate(
            sessions, project_id, "evaluation_score", body.model_dump(), idempotency_key, operation
        )

    @app.post(prefix + "/standards", response_model=Asset)
    def standard(project_id: str, body: Standard, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            p = current_plan(session, settings, project_id, body)
            scored = newest_scores(session, project_id, p.id)
            ids = [r.id for id, r in scored.items() if eligible(p, id)]
            if set(ids) != set(body.expected_score_ids) or len(body.expected_score_ids) != len(
                set(body.expected_score_ids)
            ):
                raise BusinessError("REVISION_CONFLICT", "评分版本已变化，先核对再确认标准；输入保留", 409)
            if blockers(p, scored, True):
                raise BusinessError("REAL_SAMPLES_PENDING", "真实样例、评分或阈值仍待确认；标准未生效", 409)
            payload = StandardPayload(
                schema_version=1,
                plan_id=p.id,
                minimum_score=p.payload["minimum_score"],
                score_ids=[r.id for id, r in scored.items() if eligible(p, id)],
                reviewer="local_operator",
                reason=body.reason,
            ).model_dump()
            return append(session, settings, project_id, "evaluation_standard", payload)

        return requirements.mutate(
            sessions, project_id, "evaluation_standard", body.model_dump(), idempotency_key, operation
        )
