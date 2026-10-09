"""Locked local evidence packages and bounded, explicit owner conclusions."""

import json

from fastapi import Header, Query
from sqlalchemy import select

from . import contexts, evaluations, planning, requirements, verification
from .acceptance_schemas import (
    Asset,
    DecisionInput,
    DecisionPayload,
    PackageInput,
    PackagePayload,
    Workspace,
)
from .db import RequirementRecord
from .scanner import BusinessError, digest

NOTICE = "本机产品负责人验收；锁定记录不等于通过。人工工程报告不是独立运行，未选代码不在覆盖范围；Mock、合成、降级与本地人工报告不能替代真实效果。不会执行命令、外发、部署或自动开工。"
LABELS = dict(
    tests="自动化测试",
    types="类型检查",
    lint="Lint",
    build="生产构建",
    api="接口",
    recovery="重启恢复",
    ui="桌面与移动界面",
)


def rows(session, pid, kind=None):
    q = select(RequirementRecord).where(RequirementRecord.project_id == pid)
    q = (
        q.where(RequirementRecord.kind == kind)
        if kind
        else q.where(RequirementRecord.kind.in_(["acceptance_package", "acceptance_decision"]))
    )
    return list(session.scalars(q.order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())))


def latest(session, pid, stage_id, kind="acceptance_package", package_id=None):
    return next(
        (
            r
            for r in rows(session, pid, kind)
            if (r.payload["stage"]["id"] if kind == "acceptance_package" else r.payload["stage_id"])
            == stage_id
            and (package_id is None or r.payload.get("package_id") == package_id)
        ),
        None,
    )


def owned(session, pid, identifier, kind=None):
    requirements.project(session, pid)
    r = session.get(RequirementRecord, identifier)
    if (
        not r
        or r.project_id != pid
        or r.kind not in {"acceptance_package", "acceptance_decision"}
        or (kind and r.kind != kind)
    ):
        raise BusinessError("NOT_FOUND", "验收记录不属于本项目", 404)
    return r


def stage_scope(plan, stage_id):
    stages = plan.payload["text"]["stages"]
    stage = next((s for s in stages if s["id"] == stage_id), None)
    if not stage:
        raise BusinessError("NOT_FOUND", "阶段不属于当前项目计划", 404)
    scope = {stage_id}
    while True:
        added = {s["id"] for s in stages if s["parent_id"] in scope} - scope
        if not added:
            break
        scope.update(added)
    tasks = [t for t in plan.payload["text"]["tasks"] if t["stage_id"] in scope]
    return stage, scope, tasks


def safe_view(value, settings):
    if not contexts.safe(json.dumps(value, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "验收资料包含当前受保护内容，先脱敏再核对", 409)


def evidence_view(row, view, pid):
    p = row.payload
    source = p.get("source", "versioned_record")
    run_id = p.get("run_id")
    section = "verification" if row.kind.startswith("verification_") else "evaluation"
    return dict(
        id=row.id,
        kind=row.kind,
        content_hash=digest(p),
        current=view["current"],
        issues=view["issues"],
        source=source,
        run_id=run_id,
        href=f"/api/factory/projects/{pid}/{section}/assets/{row.id}",
    )


def effect_check(session, settings, pid):
    p = evaluations.latest(session, pid, "evaluation_plan")
    scored = evaluations.newest_scores(session, pid, p.id) if p else {}
    std = next(
        (
            r
            for r in evaluations.rows(session, pid, "evaluation_standard")
            if p and r.payload["plan_id"] == p.id
        ),
        None,
    )
    current = bool(p and evaluations.public(session, settings, p)["current"])
    reasons = evaluations.blockers(p, scored, current)
    valid_std = bool(std and evaluations.public(session, settings, std)["current"])
    if not valid_std:
        reasons.append("效果标准尚未有效确认")
    hard = any(s.payload["hard_errors"] for s in scored.values())
    low = bool(
        p
        and p.payload["minimum_score"] is not None
        and any(s.payload["mean"] < p.payload["minimum_score"] for s in scored.values())
    )
    # All samples in the accepted plan must be eligible, never hide mock/fallback rows.
    if p and any(not evaluations.eligible(p, s["id"]) for s in p.payload["samples"]):
        reasons.append("本计划包含不能作为真实验收依据的样例来源")
    if hard:
        reasons.append("样例存在硬错误，均分不能抵消")
    if low:
        reasons.append("样例未达到已提议逐条阈值")
    status = (
        "needs_reverification"
        if p and not current
        else "failed"
        if hard or low
        else "pending"
        if reasons
        else "passed"
    )
    return dict(
        key="real_effect",
        task_id=None,
        label="真实模型与效果",
        status=status,
        criterion="5—10条不同真实输入、平台真实运行、逐条评分与有效标准，无硬错误且逐条达标",
        source="platform_provenance_and_owner_scores",
        source_id=std.id if std else None,
        reason="；".join(reasons) or "实际来源回执及负责人标准一致",
        responsible="local_operator" if valid_std else None,
    )


def collect(session, settings, pid, stage_id, declarations=None):
    requirements.project(session, pid)
    plan = planning.latest(session, pid, "plan")
    if not plan:
        raise BusinessError("PLAN_REQUIRED", "先保存阶段计划，再提交阶段证据", 409)
    safe_view(plan.payload, settings)
    stage, scope, tasks = stage_scope(plan, stage_id)
    revision, draft, _ = requirements.latest(session, pid)
    bases = planning.baselines(session, pid)
    base = next((r for r in bases if r.id == plan.baseline_id), None)
    approval = planning.approval(session, pid, "plan_approval", plan.id)
    tech = planning.owned(session, pid, plan.payload["technical_id"], "technical")
    ta = planning.approval(session, pid, "technical_approval", tech.id)
    matrix, evidence, indexes = [], [], {}
    seen = set()

    def add(row, public):
        if row.id not in seen:
            view = public(session, settings, row)
            evidence.append(evidence_view(row, view, pid))
            seen.add(row.id)

    for task in tasks:
        index, _ = contexts.collect(session, settings, pid, "execution", task["id"])
        indexes[task["id"]] = index["index_hash"]
        vp = verification.latest(session, pid, task["id"])
        results = [
            r
            for r in verification.rows(session, pid, "verification_result")
            if vp and r.payload["plan_id"] == vp.id
        ]
        if vp:
            add(vp, verification.public)
        for r in results:
            add(r, verification.public)
        for kind in verification.KINDS:
            r = next((r for r in results if r.payload["kind"] == kind), None)
            view = verification.public(session, settings, r) if r else None
            p = r.payload if r else {}
            stated_status = p.get("status", "pending")
            incomplete = stated_status in {"passed", "not_applicable"} and any(
                len(p.get(key, "").strip()) < minimum
                for key, minimum in (("reason", 4), ("responsible", 2), ("method", 4), ("environment", 4))
            )
            matrix.append(
                dict(
                    key=kind,
                    task_id=task["id"],
                    label=LABELS[kind],
                    status="needs_reverification"
                    if view and not view["current"]
                    else "pending"
                    if incomplete
                    else stated_status,
                    criterion=vp.payload["criteria"][kind] if vp else "尚未保存此任务检查要求",
                    source=p.get("source", "missing"),
                    source_id=r.id if r else None,
                    reason="；".join(view["issues"])
                    if view and not view["current"]
                    else "报告缺少有意义的依据、责任人、方法或环境，需补充核对；原记录保留"
                    if incomplete
                    else p.get("reason", "尚无当前检查报告，待验"),
                    responsible=p.get("responsible"),
                    passed=p.get("passed"),
                    total=p.get("total"),
                    failed=p.get("failed"),
                )
            )
    ep = evaluations.latest(session, pid, "evaluation_plan")
    current_standard = next(
        (
            r
            for r in evaluations.rows(session, pid, "evaluation_standard")
            if ep and r.payload["plan_id"] == ep.id
        ),
        None,
    )
    for r in evaluations.rows(session, pid):
        if (
            ep
            and (r.id == ep.id or r.payload.get("plan_id") == ep.id)
            and (
                r.kind in {"evaluation_plan", "evaluation_score"}
                or (current_standard and r.id == current_standard.id)
            )
        ):
            add(r, evaluations.public)
    matrix.append(effect_check(session, settings, pid))
    requirements_in_stage = sorted({rid for t in tasks for rid in t["requirement_ids"]})
    items = {r["id"]: r for r in base.payload["items"]} if base else {}
    coverage = [
        dict(
            requirement_id=rid,
            acceptance=items.get(rid, {}).get("acceptance", "冻结基线缺少此需求"),
            task_ids=[t["id"] for t in tasks if rid in t["requirement_ids"]],
            status="linked" if rid in items else "missing",
        )
        for rid in requirements_in_stage
    ]
    reasons = []
    if not tasks:
        reasons.append("阶段范围没有可核对任务")
    if not base or not bases or bases[0].id != base.id:
        reasons.append("阶段未绑定当前冻结需求基线")
    if base and revision != base.payload["revision"]:
        reasons.append("需求草稿已有变更，先冻结并更新计划")
    if not approval:
        reasons.append("当前阶段计划尚未负责人确认")
    reasons.extend(i["message"] for i in planning.plan_issues(session, settings, pid, plan))
    if any(c["status"] == "missing" for c in coverage) or any(
        t["kind"] == "requirement" and not t["requirement_ids"] for t in tasks
    ):
        reasons.append("存在未关联有效冻结需求的任务")
    matrix.append(
        dict(
            key="coverage",
            task_id=None,
            label="需求覆盖与范围",
            status="pending" if reasons else "passed",
            criterion="当前冻结基线、已确认计划、阶段全部任务与需求关联；环境依据仍有效",
            source="frozen_baseline_and_approved_plan",
            source_id=plan.id,
            reason="；".join(reasons) or "逐任务需求定位完整（关联不等于实现效果已通过）",
            responsible="local_operator" if approval else None,
        )
    )
    matrix.append(
        dict(
            key="evidence",
            task_id=None,
            label="来源证据",
            status="needs_reverification"
            if any(not e["current"] for e in evidence)
            else "passed"
            if evidence
            else "pending",
            criterion="锁定同项目来源资产与运行；缺失、失效、不匹配不得通过",
            source="linked_records",
            source_id=None,
            reason="存在异常来源，先复验并另存当前依据"
            if any(not e["current"] for e in evidence)
            else "来源摘要及已有关联运行可核对；人工证据文字未独立执行"
            if evidence
            else "没有检查或评分来源，待验",
            responsible="local_operator",
        )
    )
    declaration_names = {
        "scope": "范围实现与后续边界",
        "secrets": "密钥与秘密排除",
        "readme": "README启动与验证说明",
        "project_state": "项目状态更新",
    }
    for key, label in declaration_names.items():
        d = (declarations or {}).get(key, {})
        matrix.append(
            dict(
                key=key,
                task_id=None,
                label=label,
                status=d.get("status", "pending"),
                criterion="负责人核对实际交付与具体证据；不能以声明冒充平台独立检查",
                source="operator_report",
                source_id=None,
                reason=d.get("reason", "尚无本次负责人交付声明，待验")
                + ("；定位：" + "、".join(d.get("evidence", [])) if d.get("evidence") else ""),
                responsible="local_operator" if d else None,
            )
        )
    # Dependencies include child stages and out-of-scope task/stage prerequisites.
    stage_map = {s["id"]: s for s in plan.payload["text"]["stages"]}
    task_map = {t["id"]: t for t in plan.payload["text"]["tasks"]}
    dependency_ids = set(scope - {stage_id})
    for s in stage_map.values():
        if s["id"] in scope:
            for ref in s["depends_on"]:
                if ref in stage_map and ref not in scope:
                    dependency_ids.add(ref)
                elif ref.startswith("V"):
                    dependency_ids.update(
                        x["id"] for x in stage_map.values() if x["version_id"] == ref and x["id"] not in scope
                    )
    for t in tasks:
        for ref in t["depends_on"]:
            if ref in task_map and task_map[ref]["stage_id"] not in scope:
                dependency_ids.add(task_map[ref]["stage_id"])
    dependencies = {}
    for sid in sorted(dependency_ids):
        d = latest(session, pid, sid, "acceptance_decision")
        dependencies[sid] = digest({"id": d.id, "payload": d.payload}) if d else None
    snapshot = dict(
        draft_revision=revision,
        draft_hash=digest(draft),
        baseline_id=base.id if base else None,
        baseline_hash=digest(base.payload) if base else None,
        planning_id=plan.id,
        planning_hash=digest(plan.payload),
        approval_id=approval.id if approval else None,
        approval_hash=digest(approval.payload) if approval else None,
        technical_id=tech.id,
        technical_hash=digest(tech.payload),
        technical_approval_id=ta.id if ta else None,
        technical_approval_hash=digest(ta.payload) if ta else None,
        task_indexes=indexes,
        evidence_hashes={e["id"]: e["content_hash"] for e in evidence},
        evaluation_basis=evaluations.basis(session, settings, pid),
        dependencies=dependencies,
    )
    result = dict(
        stage=stage,
        tasks=tasks,
        snapshot=snapshot,
        snapshot_hash=digest(snapshot),
        matrix=matrix,
        coverage=coverage,
        evidence=evidence,
    )
    # Task fields not part of the acceptance contract are intentionally omitted.
    result["tasks"] = [
        {k: t[k] for k in ("id", "title", "stage_id", "kind", "requirement_ids", "depends_on")} for t in tasks
    ]
    safe_view(result, settings)
    return result


def gate_blockers(session, settings, pid, data, visited=None):
    result = [
        f"{c['task_id'] + ' · ' if c['task_id'] else ''}{c['label']}：{c['reason']}"
        for c in data["matrix"]
        if c["status"] not in {"passed", "not_applicable"}
    ]
    visited = (visited or set()) | {data["stage"]["id"]}
    for sid in data["snapshot"]["dependencies"]:
        d = latest(session, pid, sid, "acceptance_decision")
        if sid in visited or not d or d.payload["outcome"] != "passed":
            result.append("关联阶段 " + sid + " 尚无有效通过结论")
            continue
        p = owned(session, pid, d.payload["package_id"], "acceptance_package")
        try:
            dep = collect(session, settings, pid, sid, p.payload["declarations"])
            latest_p = latest(session, pid, sid)
            if (
                not latest_p
                or latest_p.id != p.id
                or d.payload["package_hash"] != digest(p.payload)
                or dep["snapshot_hash"] != p.payload["snapshot_hash"]
                or gate_blockers(session, settings, pid, dep, visited)
            ):
                result.append("关联阶段 " + sid + " 结论需复验")
        except BusinessError:
            result.append("关联阶段 " + sid + " 来源异常")
    return result


def package_state(session, settings, pid, p):
    safe_view(p.payload, settings)
    issues, blockers = [], []
    latest_p = latest(session, pid, p.payload["stage"]["id"])
    if not latest_p or latest_p.id != p.id:
        issues.append("证据包已有新版本，原包需复验；历史保留")
    try:
        data = collect(session, settings, pid, p.payload["stage"]["id"], p.payload["declarations"])
        if data["snapshot_hash"] != p.payload["snapshot_hash"]:
            issues.append("需求、代码、标准或来源版本变化，原包需复验")
        if any(not e["current"] for e in data["evidence"]):
            issues.append("证据来源失效、缺失或不匹配，需复验")
        blockers = gate_blockers(session, settings, pid, data)
    except BusinessError as exc:
        issues.append("当前来源不可核对：" + exc.message)
        blockers.append("来源异常，禁止通过")
    return issues, blockers


def public(session, settings, row):
    safe_view(row.payload, settings)
    p = (
        row
        if row.kind == "acceptance_package"
        else owned(session, row.project_id, row.payload["package_id"], "acceptance_package")
    )
    issues, blockers = package_state(session, settings, row.project_id, p)
    if row.kind == "acceptance_decision":
        d = latest(session, row.project_id, row.payload["stage_id"], "acceptance_decision")
        if not d or d.id != row.id:
            issues.append("已有新的责任人结论，历史保留")
        if row.payload["package_hash"] != digest(p.payload):
            issues.append("结论与证据包摘要不匹配")
        if row.payload["outcome"] == "passed" and blockers:
            issues.append("原通过条件已不满足，需复验")
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=row.payload,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        current=not issues,
        issues=issues,
        blockers=blockers,
    )


def append(session, settings, pid, kind, payload):
    safe_view(payload, settings)
    r = requirements.record(session, pid, kind, payload)
    requirements.audit(
        session,
        pid,
        kind + "_saved",
        {
            "asset_id": r.id,
            "stage_id": payload.get("stage_id") or payload["stage"]["id"],
            "outcome": payload.get("outcome"),
        },
    )
    return public(session, settings, r)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/acceptance"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str, stage_id: str | None = Query(default=None, pattern=r"^S[0-9]{1,4}$")):
        with sessions() as session:
            requirements.project(session, project_id)
            plan = planning.latest(session, project_id, "plan")
            stages = plan.payload["text"]["stages"] if plan else []
            safe_view(stages, settings)
            selected = stage_id or (stages[0]["id"] if stages else None)
            if stage_id and not stages:
                raise BusinessError("NOT_FOUND", "阶段不属于当前项目计划", 404)
            history = [public(session, settings, r) for r in rows(session, project_id)]
            if not selected:
                return dict(
                    stages=[],
                    stage=None,
                    tasks=[],
                    snapshot=None,
                    snapshot_hash=None,
                    matrix=[],
                    coverage=[],
                    evidence=[],
                    blockers=["尚无阶段计划，先在技术规划页准备阶段"],
                    package=None,
                    decision=None,
                    history=history,
                    notice=NOTICE,
                )
            p = latest(session, project_id, selected)
            data = collect(session, settings, project_id, selected, p.payload["declarations"] if p else None)
            d = latest(session, project_id, selected, "acceptance_decision")
            return dict(
                **data,
                stages=stages,
                blockers=gate_blockers(session, settings, project_id, data),
                package=public(session, settings, p) if p else None,
                decision=public(session, settings, d) if d else None,
                history=history,
                notice=NOTICE,
            )

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/packages", response_model=Asset)
    def submit(project_id: str, body: PackageInput, idempotency_key: str | None = Header(default=None)):
        def action(session):
            data = collect(
                session,
                settings,
                project_id,
                body.stage_id,
                {k: v.model_dump() for k, v in body.declarations.items()},
            )
            previous = latest(session, project_id, body.stage_id)
            revision = previous.payload["revision"] if previous else 0
            if body.expected_revision != revision or data["snapshot_hash"] != body.expected_snapshot_hash:
                raise BusinessError(
                    "REVISION_CONFLICT", "提交依据已有变化；输入保留，核对当前来源后另存", 409
                )
            payload = PackagePayload(
                schema_version=1,
                revision=revision + 1,
                previous_id=previous.id if previous else None,
                **data,
                steps=body.steps,
                limitations=body.limitations,
                reason=body.reason,
                declarations=body.declarations,
                submitted_by="local_operator",
                role="product_owner",
            ).model_dump()
            return append(session, settings, project_id, "acceptance_package", payload)

        return requirements.mutate(
            sessions, project_id, "acceptance_package", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/decisions", response_model=Asset)
    def decide(project_id: str, body: DecisionInput, idempotency_key: str | None = Header(default=None)):
        def action(session):
            p = owned(session, project_id, body.package_id, "acceptance_package")
            view = public(session, settings, p)
            sid = p.payload["stage"]["id"]
            previous = latest(session, project_id, sid, "acceptance_decision")
            if digest(p.payload) != body.expected_hash or body.previous_decision_id != (
                previous.id if previous else None
            ):
                raise BusinessError("REVISION_CONFLICT", "包摘要或前次结论变化，输入保留", 409)
            if not view["current"]:
                raise BusinessError(
                    "ACCEPTANCE_STALE", "证据包版本或来源已失效，先另存并核对当前包；历史保留", 409
                )
            if body.outcome == "passed" and view["blockers"]:
                raise BusinessError("GATE_BLOCKED", "必需项未通过或待验，不能通过阶段；请核对矩阵与来源", 409)
            payload = DecisionPayload(
                schema_version=1,
                package_id=p.id,
                package_hash=digest(p.payload),
                stage_id=sid,
                previous_decision_id=previous.id if previous else None,
                outcome=body.outcome,
                reason=body.reason,
                reviewer="local_operator",
                role="product_owner",
            ).model_dump()
            return append(session, settings, project_id, "acceptance_decision", payload)

        return requirements.mutate(
            sessions, project_id, "acceptance_decision", body.model_dump(), idempotency_key, action
        )
