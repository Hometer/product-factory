"""Local, append-only financial facts; no provider, resource or billing calls."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi import Header
from sqlalchemy import select

from . import artifacts, planning, requirements
from .cost_schemas import Asset, Policy, Reconciliation, Report, Workspace
from .db import ModelBudget, RequirementRecord
from .scanner import BusinessError, digest

DEFAULT = dict(per_task_micro=2_000_000, stage_micro=100_000_000, request_limit=100)
SCOPES = ("platform_model", "product_development", "product_runtime")
CATEGORIES = ("model", "execution", "storage", "deployment")
LOCAL = ZoneInfo("Asia/Shanghai")


def rows(session, kind, pid=None):
    query = select(RequirementRecord).where(RequirementRecord.kind == kind)
    if pid is not None:
        query = query.where(RequirementRecord.project_id == pid)
    return list(
        session.scalars(query.order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc()))
    )


def policy(session):
    history = rows(session, "cost_policy")
    if history:
        row = history[0]
        return dict(id=row.id, content_hash=digest(row.payload), payload=row.payload)
    return dict(id=None, content_hash=digest(DEFAULT), payload=DEFAULT)


def enforce(session, amount, *, reserving=False):
    # The account is global, including holds on other projects and earlier sub-stages.
    from . import models

    limits = policy(session)["payload"]
    account = session.get(ModelBudget, models.BUDGET_ID)
    spent, held, used, pending = counters(account)
    if min(spent, held, used, pending) < 0 or (
        not reserving and (account is None or held < amount or pending < models.MAX_CALLS)
    ):
        raise BusinessError("COST_ACCOUNT_INVALID", "账务与预留不一致，未发送请求，需人工核对", 409)
    if (
        amount > limits["per_task_micro"]
        or spent + held + (amount if reserving else 0) > limits["stage_micro"]
        or used + pending + (models.MAX_CALLS if reserving else 0) > limits["request_limit"]
        or limits["per_task_micro"] == 0
        or limits["stage_micro"] == 0
        or limits["request_limit"] == 0
    ):
        raise BusinessError("BUDGET_EXCEEDED", "全局单次、阶段金额或请求额度不足，未发送模型请求", 409)


def counters(account):
    return (
        (account.spent_micro, account.reserved_micro, account.requests_used, account.requests_reserved)
        if account
        else (0, 0, 0, 0)
    )


def latest_reconciliation(session, pid, run_id):
    return next((r for r in rows(session, "cost_reconciliation", pid) if r.payload["run_id"] == run_id), None)


def latest_report(session, pid, root_id):
    return next((r for r in rows(session, "cost_report", pid) if r.payload["root_id"] == root_id), None)


def public(session, row):
    p = row.payload
    current = True
    if row.kind == "cost_policy":
        current = policy(session)["id"] == row.id
    elif row.kind == "cost_report":
        latest = latest_report(session, row.project_id, p["root_id"])
        current = bool(latest and latest.id == row.id)
    else:
        latest = latest_reconciliation(session, row.project_id, p["run_id"])
        current = bool(latest and latest.id == row.id)
    plan = planning.latest(session, row.project_id, "plan") if p.get("planning_id") else None
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=p,
        content_hash=digest(p),
        created_at=row.created_at.replace(tzinfo=UTC).isoformat(),
        current=current,
        association_current=not p.get("planning_id") or bool(plan and plan.id == p["planning_id"]),
    )


def append(session, pid, kind, payload):
    row = requirements.record(
        session, pid, kind, dict(schema_version=1, actor="local_operator", role="product_owner", **payload)
    )
    if kind == "cost_report" and not payload["root_id"]:
        row.payload = {**row.payload, "root_id": row.id}
        session.flush()
    requirements.audit(session, pid, kind + "_saved", {"asset_id": row.id})
    return public(session, row)


def day(row):
    return row.created_at.replace(tzinfo=UTC).astimezone(LOCAL).date().isoformat()


def automatic(session, pid):
    result = []
    receipts = rows(session, "provider_receipt", pid)
    for run in rows(session, "run", pid):
        p = run.payload
        rec = latest_reconciliation(session, pid, run.id)
        final = rec and rec.payload["outcome"] == "reconciled"
        unresolved = p["state"] in ("queued", "running", "result_unknown") and not final
        actual = rec.payload["actual_micro"] if final else (None if unresolved else p["settled_micro"])
        result.append(
            dict(
                id=run.id,
                content_hash=digest(p),
                state=p["state"],
                source=p["source"],
                synthetic=p["source"] != "real_cloud",
                scope="platform_model",
                category="model",
                occurred_on=day(run),
                estimated_micro=p["reserved_micro"],
                usage_cost_micro=p["settled_micro"],
                actual_micro=actual,
                held_micro=p["reserved_micro"] if unresolved else 0,
                requests_count=rec.payload["requests_count"]
                if final
                else sum(a["state"] != "not_sent" for a in p["attempts"]),
                accounting="manual_bill_unverified" if final else "usage_quote_calculation",
                reconciliation=public(session, rec) if rec else None,
                attempts=[
                    dict(
                        attempt=a["attempt"],
                        state=a["state"],
                        cost_micro=a.get("cost_micro"),
                        input_tokens=a.get("input_tokens"),
                        output_tokens=a.get("output_tokens"),
                    )
                    for a in p["attempts"]
                ],
                receipt_ids=[r.id for r in receipts if r.payload["run_id"] == run.id],
            )
        )
    return result


def aggregate(facts):
    return dict(
        estimated_micro=sum(f["estimated_micro"] or 0 for f in facts),
        actual_micro=sum(f["actual_micro"] or 0 for f in facts),
        held_micro=sum(f.get("held_micro", 0) for f in facts),
        unknown_estimates=sum(f["estimated_micro"] is None for f in facts),
        pending_count=sum(f["actual_micro"] is None for f in facts),
        fact_count=len(facts),
    )


def workspace(session, pid):
    from . import models

    requirements.project(session, pid)
    spent, held, used, pending = counters(session.get(ModelBudget, models.BUDGET_ID))
    limits = policy(session)
    auto = automatic(session, pid)
    reports = [public(session, r) for r in rows(session, "cost_report", pid)]
    facts = [
        *auto,
        *[
            {**r["payload"], "synthetic": r["payload"]["source"] == "synthetic"}
            for r in reports
            if r["current"]
        ],
    ]
    today = datetime.now(LOCAL).date().isoformat()
    plan = planning.latest(session, pid, "plan")
    return dict(
        policy=limits,
        policy_history=[public(session, r) for r in rows(session, "cost_policy")],
        budget=dict(
            scope="global_development_stage_2",
            spent_micro=spent,
            reserved_micro=held,
            requests_used=used,
            requests_reserved=pending,
            remaining_micro=max(0, limits["payload"]["stage_micro"] - spent - held),
            remaining_requests=max(0, limits["payload"]["request_limit"] - used - pending),
            over_limit=spent + held > limits["payload"]["stage_micro"]
            or used + pending > limits["payload"]["request_limit"],
        ),
        automatic=auto,
        reports=reports,
        reconciliations=[public(session, r) for r in rows(session, "cost_reconciliation", pid)],
        totals=[
            dict(
                scope=s,
                category=c,
                synthetic=synthetic,
                **aggregate(
                    [
                        f
                        for f in facts
                        if f["scope"] == s and f["category"] == c and f["synthetic"] == synthetic
                    ]
                ),
            )
            for synthetic in (False, True)
            for s in SCOPES
            for c in CATEGORIES
        ],
        periods=dict(
            timezone="Asia/Shanghai",
            execution="statistics_only",
            today=today,
            month=today[:7],
            day=aggregate([f for f in facts if not f["synthetic"] and f["occurred_on"] == today]),
            month_totals=aggregate(
                [f for f in facts if not f["synthetic"] and f["occurred_on"][:7] == today[:7]]
            ),
        ),
        stages=plan.payload["text"]["stages"] if plan else [],
        tasks=plan.payload["text"]["tasks"] if plan else [],
        notice="全项目共用阶段2平台模型预算，重试占额度、重启不重置。估算与实费分开；未知不等于零。"
        "当日/月度只统计。人工外部账单未独立验证、不发起调用或预留；勿重复报告已自动核算的费用。"
        "合成资料单列，不入真实汇总。模型用量按报价核算，不是提供方最终账单。",
    )


def association(session, pid, body):
    if not body.stage_id and not body.task_id:
        return None
    plan = planning.latest(session, pid, "plan")
    if not plan:
        raise BusinessError("PLAN_REQUIRED", "关联阶段/任务需要本项目当前计划", 409)
    stages, tasks = plan.payload["text"]["stages"], plan.payload["text"]["tasks"]
    task = next((t for t in tasks if t["id"] == body.task_id), None)
    if (body.stage_id and not any(s["id"] == body.stage_id for s in stages)) or (
        body.task_id and (not task or task["stage_id"] != body.stage_id)
    ):
        raise BusinessError("COST_ASSOCIATION_INVALID", "阶段、任务或所属关系不匹配", 409)
    return plan.id


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/costs"

    @app.get(prefix, response_model=Workspace)
    def get(project_id: str):
        with sessions() as session:
            return workspace(session, project_id)

    @app.post(prefix + "/policies", response_model=Asset)
    def save_policy(project_id: str, body: Policy, idempotency_key: str | None = Header(default=None)):
        def action(session):
            requirements.project(session, project_id)
            artifacts.safe(body.model_dump(), settings)
            current = policy(session)
            if (body.previous_id, body.expected_hash) != (current["id"], current["content_hash"]):
                raise BusinessError("REVISION_CONFLICT", "全局预算规则变化，输入保留", 409)
            return append(
                session, project_id, "cost_policy", body.model_dump(exclude={"confirmed", "expected_hash"})
            )

        return requirements.mutate(
            sessions, project_id, "cost_policy", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/reports", response_model=Asset)
    def report(project_id: str, body: Report, idempotency_key: str | None = Header(default=None)):
        def action(session):
            requirements.project(session, project_id)
            artifacts.safe(body.model_dump(), settings)
            try:
                occurred = date.fromisoformat(body.occurred_on)
            except ValueError as exc:
                raise BusinessError("COST_DATE_INVALID", "发生日期无效") from exc
            if occurred > datetime.now(LOCAL).date():
                raise BusinessError("COST_DATE_INVALID", "费用事实不能记在未来日期")
            root_id = None
            if body.corrects_id:
                old = requirements.owned_record(session, body.corrects_id, project_id, "cost_report")
                root_id = old.payload["root_id"]
                current = latest_report(session, project_id, root_id)
                if not current or current.id != old.id or body.expected_hash != digest(old.payload):
                    raise BusinessError("REVISION_CONFLICT", "账单已有更正或摘要不符，输入保留", 409)
                if body.source != old.payload["source"]:
                    raise BusinessError("COST_SOURCE_INVALID", "合成/人工来源不可通过更正互换", 409)
            planning_id = association(session, project_id, body)
            return append(
                session,
                project_id,
                "cost_report",
                dict(
                    **body.model_dump(exclude={"confirmed", "expected_hash"}),
                    root_id=root_id,
                    planning_id=planning_id,
                ),
            )

        return requirements.mutate(
            sessions, project_id, "cost_report", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/reconciliations", response_model=Asset)
    def reconcile(project_id: str, body: Reconciliation, idempotency_key: str | None = Header(default=None)):
        def action(session):
            from . import models

            artifacts.safe(body.model_dump(), settings)
            run = requirements.owned_record(session, body.run_id, project_id, "run")
            p = run.payload
            if p["state"] != "result_unknown" or body.expected_run_hash != digest(p):
                raise BusinessError("COST_RUN_INVALID", "仅能核对同项目当前结果未知运行", 409)
            previous = latest_reconciliation(session, project_id, run.id)
            if (body.previous_id, body.expected_previous_hash) != (
                previous.id if previous else None,
                digest(previous.payload) if previous else None,
            ):
                raise BusinessError("REVISION_CONFLICT", "前次对账变化，输入保留", 409)
            was_final = bool(previous and previous.payload["outcome"] == "reconciled")
            if was_final and body.outcome != "reconciled":
                raise BusinessError("COST_FINALIZED", "已结算只能追加金额/次数更正，不能退回待对账", 409)
            if body.outcome == "reconciled":
                minimum = sum(a["state"] != "not_sent" for a in p["attempts"])
                amount, count = body.actual_micro, body.requests_count
                assert amount is not None and count is not None
                if count < minimum or (amount > 0 and count == 0):
                    raise BusinessError(
                        "COST_REQUESTS_INVALID", "请求次数不得少于已发送或可能发送的尝试", 409
                    )
                account = session.get(ModelBudget, models.BUDGET_ID)
                if account is None:
                    raise BusinessError("COST_ACCOUNT_INVALID", "预算账户缺失，不能释放预留", 409)
                if was_final:
                    assert previous is not None
                    delta_amount = amount - previous.payload["actual_micro"]
                    delta_count = count - previous.payload["requests_count"]
                else:
                    if (
                        account.reserved_micro < p["reserved_micro"]
                        or account.requests_reserved < models.MAX_CALLS
                    ):
                        raise BusinessError("COST_ACCOUNT_INVALID", "预留与账务不一致，停止结算", 409)
                    account.reserved_micro -= p["reserved_micro"]
                    account.requests_reserved -= models.MAX_CALLS
                    delta_amount, delta_count = amount, count
                if account.spent_micro + delta_amount < 0 or account.requests_used + delta_count < 0:
                    raise BusinessError("COST_ACCOUNT_INVALID", "账务差额不一致，停止结算", 409)
                account.spent_micro += delta_amount
                account.requests_used += delta_count
            return append(
                session,
                project_id,
                "cost_reconciliation",
                dict(
                    **body.model_dump(exclude={"confirmed", "expected_previous_hash"}),
                    source="manual_report",
                    run_hash=digest(p),
                    financial_only=True,
                ),
            )

        return requirements.mutate(
            sessions, project_id, "cost_reconciliation", body.model_dump(), idempotency_key, action
        )
