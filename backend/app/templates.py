"""Pinned, append-only local template snapshots. No file writes or arbitrary recipes."""

import copy
import difflib
import json
from typing import Any

from fastapi import Header
from sqlalchemy import select

from . import artifacts, planning, requirements
from .db import CommandRun, PatchRun, RequirementRecord
from .scanner import BusinessError, digest
from .template_schemas import (
    Asset,
    Customization,
    Decision,
    Draft,
    Preview,
    Publication,
    Reference,
    Review,
    Workspace,
)

LABELS = dict(
    rule_card="规则卡",
    prd_structure="PRD结构",
    stage_strategy="阶段策略",
    technical_manual="技术手册摘要",
    task_recipes="任务配方",
    acceptance_checks="验收清单",
    output_schema="输出结构",
)
IMPACTS = dict(
    rule_card=["需求", "规划", "执行", "验证", "验收", "交付"],
    prd_structure=["需求", "规划", "验收"],
    stage_strategy=["规划", "执行", "验收"],
    technical_manual=["规划", "执行", "验证"],
    task_recipes=["执行", "验证"],
    acceptance_checks=["验证", "验收", "交付"],
    output_schema=["需求", "执行", "验证", "验收"],
)
BUILTIN_ID = "builtin-local-1.0.0"
ENGINE = "template-check-1"
KINDS = (
    "template_draft",
    "template_validation",
    "template_publication",
    "template_binding",
    "template_customization",
    "template_preview",
    "template_decision",
    "template_review",
)
BUILTIN: dict[str, Any] = dict(
    version="local-1.0.0",
    compatibility="local_factory_v1",
    change_notes="内置首版安全摘要，完整手册仍按当前阶段读取。",
    module_policy="requirements_triggered",
    storage_policy="project_choice",
    terminal_policy="project_choice",
    components=dict(
        rule_card="# 核心底线摘要\n先文档，再开发验证，最后提交。秘密仅后端保存；业务状态持久化；接口与模型输出严格校验。高风险动作需明确授权与预算。原功能、测试和数据保留。未知结果不重发；真实模型无配置时待验；阶段未验不能通过。",
        prd_structure=json.dumps(list(requirements.FIELD_LABELS), ensure_ascii=False),
        stage_strategy="# 阶段策略\n按PRD依赖拆解，每阶段明确范围、文档、测试、证据及人工闸门。未触发的技术模块不安装。后续阶段不提前实现。",
        technical_manual="# 技术手册摘要\n沿用项目已验证技术与持久化方案。业务核心与终端分开，存储、模块和终端按需求适配。确定性授权、路径、错误、超时、有限重试及恢复约束不能由模板解除。完整手册仍按AGENTS阶段加载。",
        task_recipes=json.dumps(["python_syntax", "json_validate", "static_preview"]),
        acceptance_checks=json.dumps(planning.CHECKS, ensure_ascii=False),
        output_schema=json.dumps(
            dict(
                schema_version=1,
                error=dict(code="string", message="string"),
                model_candidate="AnalysisCandidate",
                extra="forbid",
            )
        ),
    ),
)


def rows(session, kind=None, pid=None):
    q = select(RequirementRecord).where(RequirementRecord.kind.in_([kind] if kind else KINDS))
    if pid is not None:
        q = q.where(RequirementRecord.project_id == pid)
    return list(session.scalars(q.order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())))


def latest(session, pid, kind):
    return next(iter(rows(session, kind, pid)), None)


def public(row):
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=row.payload,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
    )


def append(session, pid, kind, payload):
    row = requirements.record(
        session, pid, kind, dict(schema_version=1, actor="local_operator", role="product_owner", **payload)
    )
    requirements.audit(session, pid, kind + "_saved", dict(asset_id=row.id))
    return public(row)


def target(session, identifier):
    if identifier == BUILTIN_ID:
        return dict(id=BUILTIN_ID, payload=BUILTIN, content_hash=digest(BUILTIN), builtin=True)
    r = session.get(RequirementRecord, identifier)
    if not r or r.kind != "template_publication":
        raise BusinessError("NOT_FOUND", "已发布模板版本不存在", 404)
    return dict(id=r.id, payload=r.payload["bundle"], content_hash=digest(r.payload["bundle"]), builtin=False)


def validate(bundle):
    checks = []

    def check(name, ok, reason):
        checks.append(dict(name=name, passed=bool(ok), reason=reason))

    components = bundle.get("components") if isinstance(bundle, dict) else None
    shape = (
        isinstance(components, dict)
        and set(components) == set(LABELS)
        and all(isinstance(s, str) and 4 <= len(s.strip()) <= 6000 for s in components.values())
    )
    check("七类有界正文", shape, "缺失、额外或损坏组件不能发布")
    if not shape:
        return dict(engine=ENGINE, passed=False, checks=checks)
    check(
        "需求与项目选择策略",
        bundle.get("module_policy") == "requirements_triggered"
        and bundle.get("storage_policy") == "project_choice"
        and bundle.get("terminal_policy") == "project_choice",
        "模板不强制未触发模块，不锁定全部项目的存储与终端",
    )

    check(
        "兼容范围",
        bundle.get("compatibility") == "local_factory_v1",
        "仅支持本机工厂v1契约；不是任意项目兼容性证明",
    )
    for key, content in bundle["components"].items():
        ok = True
        try:
            if key == "prd_structure":
                data = json.loads(content)
                ok = isinstance(data, list) and len(data) == 8 and set(data) == set(requirements.FIELD_LABELS)
            elif key == "task_recipes":
                data = json.loads(content)
                ok = (
                    isinstance(data, list)
                    and bool(data)
                    and len(set(data)) == len(data)
                    and set(data) <= {"python_syntax", "json_validate", "static_preview"}
                )
            elif key == "acceptance_checks":
                data = json.loads(content)
                ok = (
                    isinstance(data, list)
                    and all(isinstance(s, str) and 4 <= len(s) <= 160 for s in data)
                    and len(set(data)) == len(data)
                    and len(data) <= 20
                    and set(planning.CHECKS) <= set(data)
                )
            elif key == "output_schema":
                data = json.loads(content)
                ok = (
                    isinstance(data, dict)
                    and type(data.get("schema_version")) is int
                    and data == json.loads(BUILTIN["components"][key])
                )
            else:
                ok = content.lstrip().startswith("#") and len(content.strip()) >= 20
        except (ValueError, TypeError, RecursionError, OverflowError):
            ok = False
        check(LABELS[key], ok, "结构与支持范围校验；自由文字仍需负责人审核")
    return dict(engine=ENGINE, passed=all(c["passed"] for c in checks), checks=checks)


def pin_initial(session, pid):
    append(
        session,
        pid,
        "template_binding",
        dict(
            components=copy.deepcopy(BUILTIN["components"]),
            origins={k: BUILTIN_ID for k in LABELS},
            target_id=BUILTIN_ID,
            selected_components=list(LABELS),
            effective_changed=False,
            old_acceptance_ids=[],
            reason="新登记项目固定内置安全摘要版本，原文件保留",
        ),
    )


def effective(session, pid):
    binding = latest(session, pid, "template_binding")
    custom = latest(session, pid, "template_customization")
    components = copy.deepcopy(binding.payload["components"] if binding else BUILTIN["components"])
    origins = copy.deepcopy(binding.payload["origins"] if binding else {k: BUILTIN_ID for k in LABELS})
    overrides = custom.payload["overrides"] if custom else {}
    components.update(overrides)
    for key in overrides:
        origins[key] = "project_customization"
    history = rows(session, pid=pid)
    changed = next(
        (
            r
            for r in history
            if r.kind in ("template_binding", "template_customization") and r.payload["effective_changed"]
        ),
        None,
    )
    stamp = dict(content_hash=digest(components), revision_id=changed.id) if changed else None
    state_hash = digest(
        dict(binding=public(binding) if binding else None, customization=public(custom) if custom else None)
    )
    return components, origins, state_hash, stamp, changed


def references(session, pid):
    plan = planning.latest(session, pid, "plan")
    acceptance = list(
        session.scalars(
            select(RequirementRecord).where(
                RequirementRecord.project_id == pid,
                RequirementRecord.kind.in_(["acceptance_package", "acceptance_decision"]),
            )
        )
    )
    return dict(
        plan_id=plan.id if plan else None,
        plan_hash=digest(plan.payload) if plan else None,
        acceptance={r.id: digest(r.payload) for r in acceptance},
    )


def review_basis(session, pid):
    _, _, _, stamp, changed = effective(session, pid)
    r = references(session, pid)
    value = dict(
        stamp=stamp,
        plan_id=r["plan_id"],
        plan_hash=r["plan_hash"],
        old_acceptance_ids=changed.payload["old_acceptance_ids"] if changed else [],
        old_acceptance_hashes={
            identifier: r["acceptance"].get(identifier)
            for identifier in (changed.payload["old_acceptance_ids"] if changed else [])
        },
    )
    return value, digest(value)


def blockers(session, pid):
    _, _, _, stamp, _ = effective(session, pid)
    if not stamp:
        return []
    plan = planning.latest(session, pid, "plan")
    review = latest(session, pid, "template_review")
    _, h = review_basis(session, pid)
    if not review or review.payload["review_hash"] != h:
        return ["生效模板已变化，当前任务与旧验收需要明确复核"]
    return (
        [
            t["task_id"] + " 模板兼容性" + ("需复验" if t["status"] == "needs_reverification" else "未知")
            for t in review.payload["tasks"]
            if t["status"] != "inheritable"
        ]
        if plan
        else []
    )


def busy(session, pid):
    runs = session.scalars(select(CommandRun).where(CommandRun.project_id == pid)).all()
    patches = session.scalars(select(PatchRun).where(PatchRun.project_id == pid)).all()
    if (
        any(r.state in ("queued", "starting", "running", "serving", "interrupted", "paused") for r in runs)
        or any(
            r.state in ("preparing", "applying", "rollback_required", "result_unknown", "interrupted")
            for r in patches
        )
        or any(
            r.payload["state"] in ("queued", "running", "result_unknown") for r in rows(session, "run", pid)
        )
    ):
        raise BusinessError("TEMPLATE_BUSY", "活动或结果未知运行尚未核对，不能变更生效模板", 409)


def state_expected(session, pid, expected):
    if effective(session, pid)[2] != expected:
        raise BusinessError("REVISION_CONFLICT", "项目模板或定制已变化，输入保留", 409)


def workspace(session, pid):
    requirements.project(session, pid)
    components, origins, h, stamp, _ = effective(session, pid)
    _, rh = review_basis(session, pid)
    plan = planning.latest(session, pid, "plan")
    b = latest(session, pid, "template_binding")
    c = latest(session, pid, "template_customization")
    return dict(
        catalogue=[
            target(session, BUILTIN_ID),
            *[target(session, r.id) for r in rows(session, "template_publication")],
        ],
        binding=public(b) if b else None,
        customization=public(c) if c else None,
        components=components,
        origins=origins,
        state_hash=h,
        stamp=stamp,
        review_hash=rh,
        source_hash=digest(references(session, pid)),
        tasks=[
            dict(id=t["id"], title=t["title"], stage_id=t["stage_id"]) for t in plan.payload["text"]["tasks"]
        ]
        if plan
        else [],
        blockers=blockers(session, pid),
        history=[public(r) for r in rows(session, pid=pid)],
        notice="模板生效内容存库，磁盘定制文件保持。内置技术手册为安全摘要，完整手册按阶段读取。公共发布不自动升级；结构校验不证明业务语义或真实效果。升级复核不授予执行权限，不把旧验收改为通过。",
    )


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/templates"

    @app.get(prefix, response_model=Workspace)
    def get(project_id: str):
        with sessions() as s:
            return workspace(s, project_id)

    def mutate(pid, kind, body, key, action):
        def safe_action(s):
            artifacts.safe(body.model_dump(), settings)
            return action(s)

        return requirements.mutate(sessions, pid, kind, body.model_dump(), key, safe_action)

    @app.post(prefix + "/drafts", response_model=Asset)
    def draft(project_id: str, body: Draft, idempotency_key: str | None = Header(default=None)):
        return mutate(
            project_id,
            "template_draft",
            body,
            idempotency_key,
            lambda s: append(
                s,
                project_id,
                "template_draft",
                dict(bundle=body.model_dump(exclude={"confirmed", "reason"}), reason=body.reason),
            ),
        )

    @app.post(prefix + "/validations", response_model=Asset)
    def validation(project_id: str, body: Reference, idempotency_key: str | None = Header(default=None)):
        def action(s):
            r = requirements.owned_record(s, body.asset_id, project_id, "template_draft")
            if body.expected_hash != digest(r.payload):
                raise BusinessError("TEMPLATE_STALE", "草案摘要变化", 409)
            return append(
                s,
                project_id,
                "template_validation",
                dict(
                    draft_id=r.id,
                    draft_hash=body.expected_hash,
                    result=validate(r.payload["bundle"]),
                    reason=body.reason,
                ),
            )

        return mutate(project_id, "template_validation", body, idempotency_key, action)

    @app.post(prefix + "/publications", response_model=Asset)
    def publication(project_id: str, body: Publication, idempotency_key: str | None = Header(default=None)):
        def action(s):
            r = requirements.owned_record(s, body.asset_id, project_id, "template_draft")
            v = requirements.owned_record(s, body.validation_id, project_id, "template_validation")
            bundle = r.payload["bundle"]
            check = validate(bundle)
            if (
                body.expected_hash != digest(r.payload)
                or body.validation_hash != digest(v.payload)
                or v.payload["draft_id"] != r.id
                or v.payload["draft_hash"] != digest(r.payload)
            ):
                raise BusinessError("TEMPLATE_STALE", "草案或校验摘要不匹配", 409)
            if not check["passed"] or v.payload["result"] != check:
                raise BusinessError("TEMPLATE_INVALID", "模板校验未通过，不能发布", 409)
            if bundle["version"] == BUILTIN["version"] or any(
                p.payload["bundle"]["version"] == bundle["version"] for p in rows(s, "template_publication")
            ):
                raise BusinessError("TEMPLATE_VERSION_EXISTS", "发布版本已存在，不能覆盖", 409)
            return append(
                s,
                project_id,
                "template_publication",
                dict(
                    bundle=bundle,
                    validation_id=v.id,
                    validation_hash=digest(v.payload),
                    draft_id=r.id,
                    result=check,
                    reason=body.reason,
                ),
            )

        return mutate(project_id, "template_publication", body, idempotency_key, action)

    @app.post(prefix + "/customizations", response_model=Asset)
    def custom(project_id: str, body: Customization, idempotency_key: str | None = Header(default=None)):
        def action(s):
            state_expected(s, project_id, body.expected_state_hash)
            busy(s, project_id)
            old = latest(s, project_id, "template_customization")
            overrides = dict(old.payload["overrides"]) if old else {}
            before = effective(s, project_id)[0]
            if body.action == "set":
                overrides[body.component] = body.content
            else:
                overrides.pop(body.component, None)
            binding = latest(s, project_id, "template_binding")
            merged = dict(binding.payload["components"] if binding else BUILTIN["components"])
            merged.update(overrides)
            test = validate({**BUILTIN, "components": merged})
            if not test["passed"]:
                raise BusinessError("TEMPLATE_INVALID", "项目定制损坏结构或白名单，不能生效", 409)
            return append(
                s,
                project_id,
                "template_customization",
                dict(
                    overrides=overrides,
                    previous_id=old.id if old else None,
                    effective_changed=merged != before,
                    old_acceptance_ids=sorted(references(s, project_id)["acceptance"]),
                    component=body.component,
                    action=body.action,
                    reason=body.reason,
                ),
            )

        return mutate(project_id, "template_customization", body, idempotency_key, action)

    @app.post(prefix + "/previews", response_model=Asset)
    def preview(project_id: str, body: Preview, idempotency_key: str | None = Header(default=None)):
        def action(s):
            state_expected(s, project_id, body.expected_state_hash)
            t = target(s, body.target_id)
            if body.target_hash != t["content_hash"]:
                raise BusinessError("TEMPLATE_STALE", "目标发布摘要不符", 409)
            components, _, _, _, _ = effective(s, project_id)
            c = latest(s, project_id, "template_customization")
            overrides = c.payload["overrides"] if c else {}
            diffs = []
            affected = set()
            for key in body.selected_components:
                new = t["payload"]["components"][key]
                changed = components[key] != new
                preserved = key in overrides
                if changed and not preserved:
                    affected.update(IMPACTS[key])
                diffs.append(
                    dict(
                        component=key,
                        label=LABELS[key],
                        changed=changed,
                        preserved_custom=preserved,
                        diff="".join(
                            difflib.unified_diff(
                                [line + "\n" for line in components[key].splitlines()],
                                [line + "\n" for line in new.splitlines()],
                                fromfile="项目当前",
                                tofile="目标公共版",
                            )
                        )[:16000],
                    )
                )
            return append(
                s,
                project_id,
                "template_preview",
                dict(
                    target_id=t["id"],
                    target_hash=t["content_hash"],
                    state_hash=body.expected_state_hash,
                    source_hash=digest(references(s, project_id)),
                    selected_components=body.selected_components,
                    diffs=diffs,
                    affected_domains=sorted(affected),
                    affected_stages=[
                        dict(id=stage["id"], name=stage["name"])
                        for stage in (
                            planning.latest(s, project_id, "plan").payload["text"]["stages"]
                            if affected and planning.latest(s, project_id, "plan")
                            else []
                        )
                    ],
                    migration_steps=[
                        "核对逐项差异与保留定制",
                        "明确选择全部或部分继承，未选组件保持",
                        "生效变化后复核所有已有任务，旧验收另行复验",
                        "任何文件合并仍走原补丁预览与批准",
                    ],
                    reason=body.reason,
                ),
            )

        return mutate(project_id, "template_preview", body, idempotency_key, action)

    @app.post(prefix + "/decisions", response_model=Asset)
    def decision(project_id: str, body: Decision, idempotency_key: str | None = Header(default=None)):
        def action(s):
            p = requirements.owned_record(s, body.asset_id, project_id, "template_preview")
            v = p.payload
            if body.expected_hash != digest(v) or v["source_hash"] != digest(references(s, project_id)):
                raise BusinessError("TEMPLATE_STALE", "预览或任务/验收来源变化，输入保留", 409)
            state_expected(s, project_id, v["state_hash"])
            t = target(s, v["target_id"])
            if t["content_hash"] != v["target_hash"]:
                raise BusinessError("TEMPLATE_STALE", "发布来源变化", 409)
            if any(r.payload["preview_id"] == p.id for r in rows(s, "template_decision", project_id)):
                raise BusinessError("TEMPLATE_DECIDED", "预览已决定，请另存预览", 409)
            binding_id = None
            if body.outcome == "apply":
                busy(s, project_id)
                if not validate(t["payload"])["passed"]:
                    raise BusinessError("TEMPLATE_INVALID", "目标模板损坏，不能生效", 409)
                before = effective(s, project_id)[0]
                b = latest(s, project_id, "template_binding")
                components = dict(b.payload["components"] if b else BUILTIN["components"])
                origins = dict(b.payload["origins"] if b else {k: BUILTIN_ID for k in LABELS})
                for k in v["selected_components"]:
                    components[k] = t["payload"]["components"][k]
                    origins[k] = t["id"]
                c = latest(s, project_id, "template_customization")
                after = {**components, **(c.payload["overrides"] if c else {})}
                b = append(
                    s,
                    project_id,
                    "template_binding",
                    dict(
                        components=components,
                        origins=origins,
                        target_id=t["id"],
                        selected_components=v["selected_components"],
                        previous_id=b.id if b else None,
                        effective_changed=after != before,
                        old_acceptance_ids=sorted(references(s, project_id)["acceptance"]),
                        reason=body.reason,
                    ),
                )
                binding_id = b["id"]
            return append(
                s,
                project_id,
                "template_decision",
                dict(preview_id=p.id, outcome=body.outcome, binding_id=binding_id, reason=body.reason),
            )

        return mutate(project_id, "template_decision", body, idempotency_key, action)

    @app.post(prefix + "/reviews", response_model=Asset)
    def review(project_id: str, body: Review, idempotency_key: str | None = Header(default=None)):
        def action(s):
            basis, h = review_basis(s, project_id)
            plan = planning.latest(s, project_id, "plan")
            tasks = plan.payload["text"]["tasks"] if plan else []
            if body.expected_review_hash != h:
                raise BusinessError("TEMPLATE_STALE", "模板或计划复核依据变化，输入保留", 409)
            if not basis["stamp"]:
                raise BusinessError("TEMPLATE_REVIEW_NOT_REQUIRED", "尚无生效变化，不能虚构复核", 409)
            if len(body.tasks) != len(tasks) or {t.task_id for t in body.tasks} != {t["id"] for t in tasks}:
                raise BusinessError("TEMPLATE_REVIEW_INCOMPLETE", "须逐项复核当前全部任务", 409)
            return append(
                s,
                project_id,
                "template_review",
                dict(
                    review_hash=h,
                    basis=basis,
                    tasks=[t.model_dump() for t in body.tasks],
                    old_acceptance_acknowledged=True,
                    old_acceptance_outcome="needs_reverification",
                    reason=body.reason,
                ),
            )

        return mutate(project_id, "template_review", body, idempotency_key, action)
