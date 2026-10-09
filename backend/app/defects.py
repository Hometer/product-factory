"""Append-only defect remediation and deterministic, conservative change review."""

from fastapi import Header
from sqlalchemy import select

from . import contexts, planning, requirements, verification
from .db import ExecutionAsset, PatchAsset, PatchRun, RequirementRecord, RequirementRevision
from .defect_schemas import AREAS, Asset, Impact, Issue, Payload, Retest, Transition, Workspace
from .scanner import BusinessError, digest

NOTICE = "缺陷、复测与影响逐次留档；人工报告不是平台独立执行。未知影响待复核，同项目保守阻塞阶段；可继承仅为复核意见，不恢复授权、不绕过原版本检查。"
PATHS = {
    "new": {"confirmed", "false_positive"},
    "confirmed": {"in_progress", "false_positive"},
    "in_progress": {"awaiting_retest"},
    "awaiting_retest": {"in_progress", "closed"},
    "closed": {"reopened"},
    "false_positive": {"reopened"},
    "reopened": {"confirmed", "in_progress"},
}
CONCLUSIONS = {
    "verification_result",
    "evaluation_score",
    "evaluation_standard",
    "acceptance_package",
    "acceptance_decision",
}


def safe(value, settings):
    import json

    if not contexts.safe(json.dumps(value, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "资料包含受保护内容，请脱敏后保存", 409)


def rows(session, pid):
    return list(
        session.scalars(
            select(RequirementRecord)
            .where(
                RequirementRecord.project_id == pid,
                RequirementRecord.kind.in_(
                    ["defect_issue", "defect_transition", "defect_retest", "impact_review"]
                ),
            )
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id)
        )
    )


def owned(session, pid, identifier, kind=None):
    requirements.project(session, pid)
    r = session.get(RequirementRecord, identifier)
    if (
        not r
        or r.project_id != pid
        or r.kind not in {"defect_issue", "defect_transition", "defect_retest", "impact_review"}
        or (kind and r.kind != kind)
    ):
        raise BusinessError("NOT_FOUND", "记录不属于当前项目", 404)
    return r


def state_row(session, pid, identifier):
    root = owned(session, pid, identifier, "defect_issue")
    return next(
        (
            r
            for r in rows(session, pid)
            if r.kind == "defect_transition" and r.payload["defect_id"] == identifier
        ),
        root,
    )


def root_issue(session, pid, row):
    return (
        owned(session, pid, row.payload["defect_id"], "defect_issue") if row.kind != "defect_issue" else row
    )


def task_index(session, settings, pid, task_id):
    return contexts.collect(session, settings, pid, "execution", task_id)[0]["index_hash"]


def report_problems(session, settings, pid, report, task_id):
    p = report.payload
    issues = verification.public(session, settings, report)["issues"][:]
    if p["task_id"] != task_id or not p["executed"] or p["status"] not in {"passed", "failed"}:
        issues.append("需同任务已执行的通过/失败报告")
    if not p["evidence"] or any(
        len(p.get(k, "").strip()) < n
        for k, n in [("method", 4), ("environment", 4), ("responsible", 2), ("reason", 4)]
    ):
        issues.append("报告方法、环境、责任或证据不完整")
    newest = next(
        (
            r
            for r in verification.rows(session, pid, "verification_result")
            if r.payload["plan_id"] == p["plan_id"] and r.payload["kind"] == p["kind"]
        ),
        None,
    )
    if not newest or newest.id != report.id:
        issues.append("同类检查已有更新，旧报告需复验")
    return issues


def retest_problems(session, settings, pid, row):
    p = row.payload
    root = root_issue(session, pid, row)
    issue = root.payload["issue"]
    problems = []
    try:
        report = verification.owned(session, pid, p["report_id"], "verification_result")
        problems += report_problems(session, settings, pid, report, issue["task_id"])
        if digest(report.payload) != p["report_hash"] or report.payload["status"] != p["report_status"]:
            problems.append("复测报告摘要或状态不匹配")
        if task_index(session, settings, pid, issue["task_id"]) != p["index_hash"]:
            problems.append("代码或任务上下文变化，需复验")
        if p["counterexample_hash"] != root.payload["counterexample_hash"]:
            problems.append("复测反例不匹配")
    except BusinessError:
        problems.append("复测来源无法安全核对")
    return problems


def source_rows(session, pid):
    bases = planning.baselines(session, pid)
    first_revision = min((r.payload["revision"] for r in bases), default=None)
    # Initial drafting before the first executable baseline is not a baseline change.
    revisions = list(
        session.scalars(
            select(RequirementRevision)
            .where(RequirementRevision.project_id == pid)
            .order_by(RequirementRevision.revision)
        )
    )
    result = [
        ("requirement_revision", r)
        for r in revisions
        if first_revision is not None and r.revision > first_revision
    ]
    result += [
        ("patch", r)
        for r in session.scalars(
            select(PatchAsset)
            .where(PatchAsset.project_id == pid)
            .order_by(PatchAsset.created_at, PatchAsset.id)
        )
    ]
    return result


def source_snapshot(session, settings, pid, kind, source):
    plan = planning.latest(session, pid, "plan")
    tasks = plan.payload["text"]["tasks"] if plan else []
    indexes = {}
    for t in tasks:
        try:
            indexes[t["id"]] = task_index(session, settings, pid, t["id"])
        except BusinessError:
            indexes[t["id"]] = "unavailable"
    old = list(
        session.scalars(
            select(RequirementRecord)
            .where(
                RequirementRecord.project_id == pid,
                RequirementRecord.kind.in_(CONCLUSIONS),
                RequirementRecord.created_at <= source.created_at,
            )
            .order_by(RequirementRecord.created_at, RequirementRecord.id)
        )
    )
    revision, draft, _ = requirements.latest(session, pid)
    runs = (
        list(
            session.scalars(
                select(PatchRun).where(PatchRun.project_id == pid, PatchRun.patch_id == source.id)
            )
        )
        if kind == "patch"
        else []
    )
    return dict(
        source_hash=digest(source.payload),
        draft_revision=revision,
        draft_hash=digest(draft),
        planning_hash=digest(plan.payload) if plan else None,
        indexes=indexes,
        authorization={
            r.id: digest(r.payload)
            for r in session.scalars(
                select(ExecutionAsset).where(
                    ExecutionAsset.project_id == pid, ExecutionAsset.kind.in_(["grant", "revoke"])
                )
            )
        },
        runs={r.id: digest({"state": r.state, "details": r.details}) for r in runs},
        conclusions={r.id: digest(r.payload) for r in old},
    )


def impact_views(session, settings, pid):
    result = []
    reviews = [r for r in rows(session, pid) if r.kind == "impact_review"]
    for kind, source in source_rows(session, pid):
        snapshot = source_snapshot(session, settings, pid, kind, source)
        review = next(
            (r for r in reviews if r.payload["source_kind"] == kind and r.payload["source_id"] == source.id),
            None,
        )
        current = bool(
            review
            and snapshot == review.payload["snapshot"]
            and "unavailable" not in snapshot["indexes"].values()
        )
        areas = (
            review.payload["areas"]
            if current and review
            else {k: dict(status="unknown", reason="来源变化或尚未复核，请明确核对影响") for k in AREAS}
        )
        classified = {c["asset_id"]: c for c in review.payload["conclusions"]} if current and review else {}
        conclusions = [
            dict(
                asset_id=i,
                content_hash=h,
                **{
                    k: classified.get(i, {}).get(k, default)
                    for k, default in [("status", "unknown"), ("reason", "关联尚未复核")]
                },
            )
            for i, h in snapshot["conclusions"].items()
        ]
        for c in conclusions:
            record = session.get(RequirementRecord, c["asset_id"])
            c["kind"] = record.kind
            section = (
                "verification"
                if record.kind.startswith("verification_")
                else "evaluation"
                if record.kind.startswith("evaluation_")
                else "acceptance"
            )
            c["href"] = f"/api/factory/projects/{pid}/{section}/assets/{record.id}"
        issues = []
        if not current:
            issues.append("影响尚未复核或来源已变化，需另存当前复核")
        if any(a["status"] == "unknown" for a in areas.values()) or any(
            c["status"] == "unknown" for c in conclusions
        ):
            issues.append("未知关联仍待复核")
        files = [f["path"] for f in source.payload["files"]] if kind == "patch" else []
        ids = [i["id"] for i in source.payload.get("items", [])] if kind == "requirement_revision" else []
        label = (
            f"需求修订 {source.revision}"
            if kind == "requirement_revision"
            else ("补丁候选（应用状态见运行摘要） " + source.id[:8])
        )
        value = dict(
            source_kind=kind,
            source_id=source.id,
            label=label,
            snapshot=snapshot,
            snapshot_hash=digest(snapshot),
            files=files,
            requirement_ids=ids,
            areas=areas,
            conclusions=conclusions,
            review=raw_public(review, [] if current else ["影响来源变化，需复验"]) if review else None,
            current=current,
            issues=issues,
        )
        safe(value, settings)
        result.append(value)
    return result


def raw_public(row, issues):
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


def public(session, settings, row):
    safe(row.payload, settings)
    issues = []
    if row.kind == "defect_retest":
        issues = retest_problems(session, settings, row.project_id, row)
    elif row.kind == "impact_review":
        view = next(
            (
                v
                for v in impact_views(session, settings, row.project_id)
                if v["source_kind"] == row.payload["source_kind"]
                and v["source_id"] == row.payload["source_id"]
            ),
            None,
        )
        if not view or not view["review"] or view["review"]["id"] != row.id or not view["current"]:
            issues.append("复核已有新版本或来源变化，原意见保留")
    else:
        root = root_issue(session, row.project_id, row)
        state = state_row(session, row.project_id, root.id)
        if state.id != row.id:
            issues.append("状态已有更新，原事实保留")
        if row.payload["status"] == "closed":
            proof = owned(session, row.project_id, row.payload["retest_id"], "defect_retest")
            issues += retest_problems(session, settings, row.project_id, proof)
    return raw_public(row, issues)


def defect_views(session, settings, pid):
    result = []
    all_rows = rows(session, pid)
    for root in [r for r in all_rows if r.kind == "defect_issue"]:
        state = public(session, settings, state_row(session, pid, root.id))
        history = [
            public(session, settings, r)
            for r in all_rows
            if r.id == root.id or r.payload.get("defect_id") == root.id
        ]
        result.append(
            dict(
                id=root.id,
                issue=root.payload["issue"],
                state=state,
                retests=[r for r in history if r["kind"] == "defect_retest"],
                history=history,
                current=state["current"],
                issues=state["issues"],
            )
        )
    return result


def conclusion_issues(session, settings, pid, identifier):
    issues = []
    for view in impact_views(session, settings, pid):
        for c in view["conclusions"]:
            if c["asset_id"] == identifier and c["status"] != "inheritable":
                label = {"invalid": "已失效", "needs_reverification": "需复验", "unknown": "待复核"}[
                    c["status"]
                ]
                issues.append(view["label"] + "关联结论" + label + "：" + c["reason"])
    return issues


def gate(session, settings, pid):
    ds = defect_views(session, settings, pid)
    impacts = impact_views(session, settings, pid)
    problems = [
        f"缺陷 {d['issue']['title']} 尚未有效关闭/审核误报"
        for d in ds
        if not d["current"] or d["state"]["payload"]["status"] not in {"closed", "false_positive"}
    ]
    problems += [v["label"] + "：" + "；".join(v["issues"]) for v in impacts if v["issues"]]
    facts = {r.id: digest(r.payload) for r in rows(session, pid)}
    source_hashes = {v["source_kind"] + ":" + v["source_id"]: v["snapshot_hash"] for v in impacts}
    return problems, dict(records=facts, sources=source_hashes)


def append(session, settings, pid, kind, **payload):
    data = Payload(schema_version=1, actor="local_operator", role="product_owner", **payload).model_dump(
        exclude_none=True
    )
    safe(data, settings)
    row = requirements.record(session, pid, kind, data)
    requirements.audit(
        session,
        pid,
        kind + "_saved",
        {"asset_id": row.id, "status": data.get("status"), "source_id": data.get("source_id")},
    )
    return public(session, settings, row)


def guard(session, pid, body):
    root = owned(session, pid, body.defect_id, "defect_issue")
    state = state_row(session, pid, root.id)
    if state.id != body.previous_id or digest(state.payload) != body.expected_hash:
        raise BusinessError("REVISION_CONFLICT", "缺陷状态已有变化，输入保留，请核对新版本", 409)
    return root, state


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/defects"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            requirements.project(session, project_id)
            plan = planning.latest(session, project_id, "plan")
            text = plan.payload["text"] if plan else dict(tasks=[], stages=[], versions=[])
            bases = planning.baselines(session, project_id)
            ids = [i["id"] for i in bases[0].payload["items"]] if bases else []
            reports = []
            for r in verification.rows(session, project_id, "verification_result"):
                v = verification.public(session, settings, r)
                reports.append(
                    dict(
                        id=r.id,
                        content_hash=digest(r.payload),
                        task_id=r.payload["task_id"],
                        kind=r.payload["kind"],
                        status=r.payload["status"],
                        source=r.payload["source"],
                        current=v["current"],
                        issues=v["issues"],
                    )
                )
            result = dict(
                tasks=text["tasks"],
                stages=text["stages"],
                versions=text["versions"],
                requirement_ids=ids,
                defects=defect_views(session, settings, project_id),
                reports=reports,
                impacts=impact_views(session, settings, project_id),
                history=[public(session, settings, r) for r in rows(session, project_id)],
                notice=NOTICE,
            )
            safe(result, settings)
            return result

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/issues", response_model=Asset)
    def create(project_id: str, body: Issue, idempotency_key: str | None = Header(default=None)):
        def action(session):
            plan = planning.latest(session, project_id, "plan")
            task = (
                next((t for t in plan.payload["text"]["tasks"] if t["id"] == body.task_id), None)
                if plan
                else None
            )
            stage = (
                next(
                    (s for s in plan.payload["text"]["stages"] if task and s["id"] == task["stage_id"]), None
                )
                if plan
                else None
            )
            bases = planning.baselines(session, project_id)
            ids = {i["id"] for i in bases[0].payload["items"]} if bases else set()
            if (
                not task
                or not stage
                or stage["version_id"] != body.version_id
                or not set(body.requirement_ids) <= ids
                or not set(body.requirement_ids) <= set(task["requirement_ids"])
            ):
                raise BusinessError("INPUT_INVALID", "需当前同项目任务、影响版本及关联需求", 409)
            issue = body.model_dump(exclude={"confirmed", "reason"})
            return append(
                session,
                settings,
                project_id,
                "defect_issue",
                issue=issue,
                status="new",
                counterexample_hash=digest({"input": body.reproduction_input, "expected": body.expected}),
                reason=body.reason,
            )

        return requirements.mutate(
            sessions, project_id, "defect_issue", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/retests", response_model=Asset)
    def retest(project_id: str, body: Retest, idempotency_key: str | None = Header(default=None)):
        def action(session):
            root, state = guard(session, project_id, body)
            if state.payload["status"] not in {"awaiting_retest", "closed"}:
                raise BusinessError("RETEST_NOT_READY", "仅待复测或已关闭缺陷可记录反例复测", 409)
            issue = root.payload["issue"]
            if (
                digest({"input": body.reproduction_input, "expected": body.expected})
                != root.payload["counterexample_hash"]
            ):
                raise BusinessError("COUNTEREXAMPLE_MISMATCH", "需原反例输入及预期，不得改换反例", 409)
            report = verification.owned(session, project_id, body.report_id, "verification_result")
            problems = report_problems(session, settings, project_id, report, issue["task_id"])
            if digest(report.payload) != body.report_hash or report.created_at <= state.created_at:
                problems.append("报告摘要变化或早于本次待复测/关闭")
            if report.payload["status"] == "passed" and body.actual != body.expected:
                problems.append("通过复测的此次实际必须与原预期一致")
            if report.payload["status"] == "failed" and body.actual == body.expected:
                problems.append("失败复测必须保留与预期不同的实际")
            if problems:
                raise BusinessError("RETEST_INVALID", "；".join(problems), 409)
            return append(
                session,
                settings,
                project_id,
                "defect_retest",
                defect_id=root.id,
                previous_id=state.id,
                counterexample_hash=root.payload["counterexample_hash"],
                report_id=report.id,
                report_hash=digest(report.payload),
                report_source=report.payload["source"],
                report_status=report.payload["status"],
                reproduction_input=body.reproduction_input,
                expected=body.expected,
                actual=body.actual,
                index_hash=task_index(session, settings, project_id, issue["task_id"]),
                reason=body.reason,
            )

        return requirements.mutate(
            sessions, project_id, "defect_retest", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/transitions", response_model=Asset)
    def transition(project_id: str, body: Transition, idempotency_key: str | None = Header(default=None)):
        def action(session):
            root, state = guard(session, project_id, body)
            if body.status not in PATHS[state.payload["status"]]:
                raise BusinessError(
                    "STATE_TRANSITION_INVALID", "状态路径无效；关闭需复测，误报需审核理由", 409
                )
            proof = None
            if body.status == "closed" or (body.status == "reopened" and state.payload["status"] == "closed"):
                proof = next(
                    (
                        r
                        for r in rows(session, project_id)
                        if r.kind == "defect_retest"
                        and r.payload["defect_id"] == root.id
                        and r.payload["previous_id"] == state.id
                    ),
                    None,
                )
                expected = "passed" if body.status == "closed" else "failed"
                if (
                    not proof
                    or proof.payload["report_status"] != expected
                    or retest_problems(session, settings, project_id, proof)
                ):
                    raise BusinessError(
                        "RETEST_REQUIRED", "需本次状态后的同反例当前通过复测才能关闭；失败复测才能重开", 409
                    )
            return append(
                session,
                settings,
                project_id,
                "defect_transition",
                defect_id=root.id,
                previous_id=state.id,
                status=body.status,
                counterexample_hash=root.payload["counterexample_hash"],
                retest_id=proof.id if proof else None,
                reason=body.reason,
            )

        return requirements.mutate(
            sessions, project_id, "defect_transition", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/impacts", response_model=Asset)
    def impact(project_id: str, body: Impact, idempotency_key: str | None = Header(default=None)):
        def action(session):
            view = next(
                (
                    v
                    for v in impact_views(session, settings, project_id)
                    if v["source_kind"] == body.source_kind and v["source_id"] == body.source_id
                ),
                None,
            )
            if not view:
                raise BusinessError("NOT_FOUND", "变更来源不属于当前项目", 404)
            previous = view["review"]
            if view["snapshot_hash"] != body.expected_snapshot_hash or body.previous_id != (
                previous["id"] if previous else None
            ):
                raise BusinessError("REVISION_CONFLICT", "变更来源或前次复核已变化，输入保留", 409)
            if {c.asset_id for c in body.conclusions} != set(view["snapshot"]["conclusions"]):
                raise BusinessError("IMPACT_INCOMPLETE", "需逐条复核全部同项目关联结论，未知不能省略", 409)
            return append(
                session,
                settings,
                project_id,
                "impact_review",
                source_kind=body.source_kind,
                source_id=body.source_id,
                previous_id=body.previous_id,
                snapshot=view["snapshot"],
                snapshot_hash=view["snapshot_hash"],
                areas={k: v.model_dump() for k, v in body.areas.items()},
                conclusions=[c.model_dump() for c in body.conclusions],
                reason=body.reason,
            )

        return requirements.mutate(
            sessions, project_id, "impact_review", body.model_dump(), idempotency_key, action
        )
