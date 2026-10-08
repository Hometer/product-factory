"""Bounded, local, append-only preflight. There is deliberately no execute route."""

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select

from . import models, planning
from .db import ExecutionAsset, ModelBudget
from .execution_schemas import (
    Authorize,
    CheckPayload,
    ExecutionText,
    ExecutionWorkspace,
    Generate,
    PlanPayload,
    Review,
    Revoke,
    Save,
)
from .execution_schemas import (
    ExecutionAsset as AssetOutput,
)
from .requirements import audit, mutate, owned_record, project, timestamp
from .scanner import EXCLUDED, SECRET, BusinessError, canonical, digest

LIMITATIONS = [
    "只核对显式文件与已绑定环境；未选文件、完整 Git 差异和外部进程未覆盖。",
    "检查是当时快照，历史记录不构成当前开工许可；后续执行前必须重新核对并取得写锁。",
    "自动任务配方、任务依赖结果与通用占用登记尚未交付；有限文本写入须在补丁页单独审阅确认。",
]
OPERATIONS = {
    "read_only": "本地只读",
    "code_write": "代码写入",
    "model_call": "模型外发",
    "dependency_install": "依赖安装",
    "deploy": "发布",
    "irreversible": "不可逆动作",
}


def rows(session, pid, task_id=None, kind=None):
    stmt = select(ExecutionAsset).where(ExecutionAsset.project_id == pid)
    if task_id is not None:
        stmt = stmt.where(ExecutionAsset.task_id == task_id)
    if kind is not None:
        stmt = stmt.where(ExecutionAsset.kind == kind)
    return list(session.scalars(stmt.order_by(ExecutionAsset.revision.desc(), ExecutionAsset.id)))


def latest(session, pid, task_id, kind):
    return next(iter(rows(session, pid, task_id, kind)), None)


def owned(session, pid, identifier, kind=None):
    row = session.get(ExecutionAsset, identifier)
    if not row or row.project_id != pid or kind and row.kind != kind:
        raise BusinessError("NOT_FOUND", "执行准备记录不存在或不属于此项目", 404)
    return row


def public(row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "task_id": row.task_id,
        "kind": row.kind,
        "revision": row.revision,
        "planning_id": row.planning_id,
        "payload": row.payload,
        "content_hash": digest(row.payload),
        "created_at": timestamp(row.created_at),
    }


def append(session, pid, task_id, kind, planning_id, payload):
    prior = latest(session, pid, task_id, kind)
    row = ExecutionAsset(
        id=str(uuid4()),
        project_id=pid,
        task_id=task_id,
        kind=kind,
        revision=prior.revision + 1 if prior else 1,
        planning_id=planning_id,
        payload=payload,
    )
    session.add(row)
    session.flush()
    audit(
        session, pid, "execution_" + kind, {"asset_id": row.id, "revision": row.revision, "task_id": task_id}
    )
    return row


def source(session, settings, pid, identifier):
    row = planning.owned(session, pid, identifier, "plan")
    current = planning.latest(session, pid, "plan")
    if current.id != row.id or not planning.approval(session, pid, "plan_approval", row.id):
        raise BusinessError("PLAN_BLOCKED", "需核对并确认当前阶段计划，旧来源不能开工", 409)
    found = planning.plan_issues(session, settings, pid, row)
    if found:
        raise BusinessError("INPUT_STALE", found[0]["message"], 409)
    return row


def contains_secret(content):
    return bool(
        SECRET.search(content)
        or re.search(
            r"""(?i)["']?(?:api[_-]?key|access[_-]?key|password|secret|token)["']?\s*[:=]\s*["']?[^\s"']+""",
            content,
        )
    )


def file_facts(root, names):
    facts = []
    total = 0
    for name in names:
        rel = Path(name)
        if (
            rel.is_absolute()
            or "\\" in name
            or name != rel.as_posix()
            or any(
                p in ("..", ".")
                or p in EXCLUDED
                or p.startswith(".env")
                or re.search(r"(?i)(secret|credential|password|access.?key|api.?key|backend_token)", p)
                for p in rel.parts
            )
            or not rel.parts
            or rel.suffix.lower() in {".pem", ".key", ".p12", ".sqlite", ".sqlite3", ".db"}
        ):
            raise BusinessError("PATH_FORBIDDEN", "选中文件含越界或受保护路径，请核对相对文件列表")
        try:
            with ExitStack() as stack:
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                missing = False
                for part in rel.parts[:-1]:
                    try:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except FileNotFoundError:
                        missing = True
                        break
                    stack.callback(os.close, child)
                    fd = child
                raw = b""
                if not missing:
                    try:
                        final = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    except FileNotFoundError:
                        missing = True
                    else:
                        with os.fdopen(final, "rb") as stream:
                            before = os.fstat(stream.fileno())
                            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                                raise ValueError("unsafe")
                            raw = stream.read(262145)
                            after = os.fstat(stream.fileno())
                            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise ValueError("changed during read")
                if len(raw) > 262144:
                    raise ValueError("too large")
                total += len(raw)
                if total > 2097152:
                    raise ValueError("batch too large")
                content = raw.decode("utf-8", errors="ignore")
                if contains_secret(content):
                    raise ValueError("secret")
                facts.append(
                    {
                        "path": name,
                        "exists": not missing,
                        "size": len(raw),
                        "sha256": None if missing else hashlib.sha256(raw).hexdigest(),
                    }
                )
        except (OSError, ValueError):
            raise BusinessError(
                "FILE_UNAVAILABLE", "选中文件无法安全核对，可能受保护、非普通文件或超过限制", 409
            ) from None
    return facts


def prepare(session, settings, pid, body, generated):
    plan = source(session, settings, pid, body.planning_id)
    task = next((t for t in plan.payload["text"]["tasks"] if t["id"] == body.task_id), None)
    if not task:
        raise BusinessError("NOT_FOUND", "任务不在当前阶段计划内", 404)
    tech = planning.owned(session, pid, plan.payload["technical_id"], "technical")
    base = owned_record(session, plan.baseline_id, pid, "baseline")
    stage = next(s for s in plan.payload["text"]["stages"] if s["id"] == task["stage_id"])
    version = next(v for v in plan.payload["text"]["versions"] if v["id"] == stage["version_id"])
    text = ExecutionText.model_validate(
        {
            "modifications": "核对任务范围并明确拟修改文件；规则草稿不写入工程",
            "files": [],
            "steps": [
                {
                    "id": "E1",
                    "operation": "code_write" if task["kind"] == "requirement" else "read_only",
                    "instruction": "按已冻结需求处理任务：" + task["title"],
                    "output": "需求关联的候选变更或核对记录；尚未产生",
                    "validation": "保留原验收要求及十项闸门，完成对应工程和人工验证",
                }
            ],
            "estimate_micro": 0,
            "recovery": "当前准备不修改原工程；实际写入前需补丁回退，数据迁移另需备份恢复",
            "risks": "自动任务执行器与通用占用核对未交付；有限补丁另行审阅，零费准备不代表外部报价",
        }
        if generated
        else body.text.model_dump()
    ).model_dump()
    if contains_secret(str(text)):
        raise BusinessError("INPUT_INVALID", "执行描述可能包含秘密，请移除后保存")
    root = canonical(project(session, pid).canonical_path, settings.allowed_roots)
    facts = file_facts(root, text["files"])
    relevant = [r for r in base.payload["items"] if r["id"] in task["requirement_ids"]]
    # Verification wording does not enlarge permissions. Operational changes always do.
    authorization_hash = digest(
        {
            "project_id": pid,
            "task_id": body.task_id,
            "requirements": relevant,
            "basis": [task["title"], task["kind"], task["defect_ref"], task["reason"]],
            "stage_scope": [stage["scope"], stage["out_of_scope"]],
            "modifications": text["modifications"],
            "files": facts,
            "steps": [{k: s[k] for k in ("id", "operation", "instruction", "output")} for s in text["steps"]],
            "estimate_micro": text["estimate_micro"],
            "recovery": text["recovery"],
            "risks": text["risks"],
        }
    )
    return PlanPayload.model_validate(
        {
            "source": "rule_template" if generated else "user_edited",
            "inputs": {
                "baseline_id": base.id,
                "baseline_revision": base.payload["revision"],
                "technical_id": tech.id,
                "technical_revision": tech.revision,
                "environment_id": tech.payload["environment_id"],
                "planning_id": plan.id,
                "planning_revision": plan.revision,
                "stage_id": stage["id"],
                "task_title": task["title"],
                "requirement_ids": task["requirement_ids"],
                "task_dependencies": task["depends_on"],
                "stage_dependencies": stage["depends_on"],
                "version_dependencies": version["depends_on"],
            },
            "text": text,
            "files": facts,
            "acceptance": plan.payload["acceptance"],
            "required_checks": plan.payload["required_checks"],
            "authorization_hash": authorization_hash,
        }
    ).model_dump()


def authorization(session, pid, row):
    revoked = {r.payload["authorization_id"] for r in rows(session, pid, row.task_id, "revoke")}
    return next(
        (
            r
            for r in rows(session, pid, row.task_id, "grant")
            if r.id not in revoked and r.payload["authorization_hash"] == row.payload["authorization_hash"]
        ),
        None,
    )


def selected_current(session, pid, body):
    row = owned(session, pid, body.execution_id, "plan")
    if latest(session, pid, row.task_id, "plan").id != row.id or digest(row.payload) != body.expected_hash:
        raise BusinessError("REVISION_CONFLICT", "执行计划已变化，当前输入保留，请核对最新修订", 409)
    return row


def source_file_issues(session, settings, pid, row):
    problems = []
    try:
        source(session, settings, pid, row.planning_id)
    except BusinessError as exc:
        problems.append(("source", exc.message))
    try:
        root = canonical(project(session, pid).canonical_path, settings.allowed_roots)
        current = file_facts(root, row.payload["text"]["files"])
        if current != row.payload["files"]:
            problems.append(("files", "选中文件发生外部变化，需核对后另存新执行修订；原记录保留"))
    except BusinessError:
        problems.append(("files", "选中文件无法安全核对；解除路径或文件问题后重新保存"))
    return problems


def matrix(session, settings, pid, row):
    checks = []

    def add(key, label, status, result, method, guidance=""):
        checks.append(
            dict(key=key, label=label, status=status, result=result, method=method, guidance=guidance)
        )

    problems = dict(source_file_issues(session, settings, pid, row))
    add(
        "source",
        "输入版本与确认",
        "blocked" if "source" in problems else "passed",
        problems.get("source", "当前基线、方案、阶段计划确认与环境有效"),
        "后端重新核对当前不可变来源与15分钟环境有效期",
        "回到技术与计划页重新核对当前基线、环境、方案和计划",
    )
    add(
        "files",
        "显式文件外部变更",
        "blocked" if "files" in problems else "passed",
        problems.get("files", "选中文件与执行计划保存时相同；未选择文件时核对范围为空"),
        "仅选中文件存在性、大小和SHA-256，未保存正文",
        "核对外部修改后明确保存新的执行计划",
    )
    text = row.payload["text"]
    ops = {s["operation"] for s in text["steps"]}
    scope = authorization(session, pid, row)
    unsupported = ops - {"read_only", "code_write"} or text["estimate_micro"] != 0
    if unsupported:
        add(
            "authorization",
            "操作范围授权",
            "blocked",
            "外发、安装、发布、不可逆或付费能力未开放，准备描述不是执行授权",
            "确定性操作分类及费用",
            "后续形成可审查结果并接入对应能力与授权",
        )
    elif "code_write" in ops:
        add(
            "authorization",
            "本地修改范围授权",
            "passed" if scope else "blocked",
            "本地零费范围授权有效" if scope else "当前修改范围尚未有效确认，或已撤销",
            "权限影响哈希、任务归属和撤销记录",
            "核对完整范围并人工确认；变更操作范围须重新确认",
        )
    else:
        add(
            "authorization",
            "本地只读边界",
            "passed",
            "已登记目录内只读核对无需新增外发/付费授权",
            "受保护的本机项目路径；描述仍不会被执行",
        )
    add(
        "write_scope",
        "修改文件清单",
        "blocked" if "code_write" in ops and not text["files"] else "passed",
        "代码修改需明确选择相对文件"
        if "code_write" in ops and not text["files"]
        else "已限定所选文件；不是补丁批准",
        "路径保护与清单校验",
        "选择拟修改文件并保存",
    )
    dep = row.payload["inputs"]
    dependencies = dep["task_dependencies"] + dep["stage_dependencies"] + dep["version_dependencies"]
    add(
        "dependencies",
        "任务与阶段前置结果",
        "pending" if dependencies else "passed",
        "前置依赖无已验证执行结果：" + "、".join(dependencies) if dependencies else "该任务无声明的前置依赖",
        "来源依赖图；不把计划确认当作任务完成",
        "后续取得关联任务与阶段的有效结果",
    )
    add(
        "acceptance",
        "原验收与阶段闸门",
        "passed",
        "原验收文本与十项闸门保留，未执行仍待验",
        "服务端来源快照；不能编辑删减",
    )
    budget = session.get(ModelBudget, models.BUDGET_ID)
    facts = dict(
        spent_micro=budget.spent_micro if budget else 0,
        reserved_micro=budget.reserved_micro if budget else 0,
        requests_used=budget.requests_used if budget else 0,
        requests_reserved=budget.requests_reserved if budget else 0,
        stage_limit_micro=100_000_000,
        task_limit_micro=2_000_000,
        request_limit=100,
    )
    add(
        "budget",
        "费用与阶段预算",
        "blocked" if unsupported else "passed",
        "非本地零费操作的估算不是可靠报价；本轮不预留、不收费，实际额度见预算事实"
        if unsupported
        else "本地准备零费用，不消费模型预算；不代表未来外发零费",
        "只读既有阶段预算，不创建或重置",
        "未来付费前可靠报价、预留和授权必须同时满足",
    )
    if text["estimate_micro"] > 2_000_000 or (
        text["estimate_micro"]
        and (
            facts["spent_micro"] + facts["reserved_micro"] + text["estimate_micro"] > 100_000_000
            or facts["requests_used"] + facts["requests_reserved"] >= 100
        )
    ):
        add(
            "budget_limit",
            "预算上限",
            "blocked",
            "估算超出任务或阶段费用/请求上限",
            "单任务2元、阶段100元/100次，重试计入",
            "降低受授权范围的成本或另行取得业务决定",
        )
    add(
        "occupancy",
        "项目与工作区占用",
        "pending",
        "通用任务占用登记未交付，外部进程未核对；补丁写入独占锁在补丁页单独检查",
        "当前平台能力",
        "后续建立持久化占用登记与同项目/工作区单写控制",
    )
    add(
        "executor",
        "执行能力",
        "blocked",
        "执行器未交付：手动执行待核对，不能自动开工",
        "固定能力声明，不以模型或客户端字段判断",
        "自动任务配方交付后重新检查；当前有限文本补丁请到补丁页核对",
    )
    return CheckPayload.model_validate(
        {
            "execution_id": row.id,
            "content_hash": digest(row.payload),
            "checks": checks,
            "authorization_id": scope.id if scope else None,
            "budget": facts,
            "limitations": LIMITATIONS,
            "can_start": False,
            "execution_supported": False,
            "status": "blocked",
            "checked_at": timestamp(),
        }
    ).model_dump()


def workspace(session, settings, pid):
    project(session, pid)
    plan = planning.latest(session, pid, "plan")
    problems = []
    if plan:
        try:
            source(session, settings, pid, plan.id)
        except BusinessError as exc:
            problems.append(exc.message)
    else:
        problems.append("请先在技术与计划页确认阶段计划")
    plans: dict[str, ExecutionAsset] = {}
    for row in rows(session, pid, kind="plan"):
        plans.setdefault(row.task_id, row)
    return {
        "planning_id": plan.id if plan else None,
        "planning_revision": plan.revision if plan else None,
        "tasks": [{k: t[k] for k in ("id", "title", "stage_id")} for t in plan.payload["text"]["tasks"]]
        if plan
        else [],
        "source_issues": problems,
        "entries": [
            {
                "task_id": row.task_id,
                "plan": public(row),
                "check": public(check) if (check := latest(session, pid, row.task_id, "check")) else None,
                "authorization": public(grant) if (grant := authorization(session, pid, row)) else None,
            }
            for row in plans.values()
        ],
        "history": [
            {
                "id": r.id,
                "task_id": r.task_id,
                "kind": r.kind,
                "revision": r.revision,
                "created_at": timestamp(r.created_at),
            }
            for r in rows(session, pid)
        ],
        "execution_supported": False,
    }


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/execution"

    @app.get(prefix, response_model=ExecutionWorkspace)
    def get_workspace(project_id: str):
        with sessions() as session:
            return workspace(session, settings, project_id)

    @app.get(prefix + "/assets/{asset_id}", response_model=AssetOutput)
    def get_asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(owned(session, project_id, asset_id))

    def save(project_id, body, key, generated):
        def operation(session):
            old = latest(session, project_id, body.task_id, "plan")
            if body.expected_revision != (old.revision if old else 0):
                raise BusinessError(
                    "REVISION_CONFLICT", "执行修订已变化，当前输入保留，请核对服务器版本", 409
                )
            payload = prepare(session, settings, project_id, body, generated)
            return public(append(session, project_id, body.task_id, "plan", body.planning_id, payload))

        return mutate(
            sessions, project_id, "execution_save_" + str(generated), body.model_dump(), key, operation
        )

    @app.post(prefix + "/plans/generate", response_model=AssetOutput)
    def generate(project_id: str, body: Generate, idempotency_key: str | None = Header(default=None)):
        return save(project_id, body, idempotency_key, True)

    @app.post(prefix + "/plans/revisions", response_model=AssetOutput)
    def revise(project_id: str, body: Save, idempotency_key: str | None = Header(default=None)):
        return save(project_id, body, idempotency_key, False)

    @app.post(prefix + "/check", response_model=AssetOutput)
    def check(project_id: str, body: Review, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            row = selected_current(session, project_id, body)
            return public(
                append(
                    session,
                    project_id,
                    row.task_id,
                    "check",
                    row.planning_id,
                    matrix(session, settings, project_id, row),
                )
            )

        return mutate(sessions, project_id, "execution_check", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/authorize", response_model=AssetOutput)
    def authorize(project_id: str, body: Authorize, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            row = selected_current(session, project_id, body)
            text = row.payload["text"]
            if text["estimate_micro"] or any(
                s["operation"] not in {"read_only", "code_write"} for s in text["steps"]
            ):
                raise BusinessError(
                    "AUTHORIZATION_UNSUPPORTED", "本轮仅确认本地零费范围，外发/安装/发布等能力尚未开放", 409
                )
            if any(s["operation"] == "code_write" for s in text["steps"]) and not text["files"]:
                raise BusinessError("INPUT_STALE", "代码修改必须先明确文件清单", 409)
            if source_file_issues(session, settings, project_id, row):
                raise BusinessError("INPUT_STALE", "输入或选中文件已失效，请先重新核对并保存", 409)
            prior = authorization(session, project_id, row)
            if prior:
                return public(prior)
            return public(
                append(
                    session,
                    project_id,
                    row.task_id,
                    "grant",
                    row.planning_id,
                    {
                        "execution_id": row.id,
                        "authorization_hash": row.payload["authorization_hash"],
                        "reason": body.reason,
                        "approved_by": "local_operator",
                        "scope": "local_zero_cost",
                        "decision": "scope_granted",
                    },
                )
            )

        return mutate(
            sessions, project_id, "execution_authorize", body.model_dump(), idempotency_key, operation
        )

    @app.post(prefix + "/revoke", response_model=AssetOutput)
    def revoke(project_id: str, body: Revoke, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            grant = owned(session, project_id, body.authorization_id, "grant")
            prior = next(
                (
                    r
                    for r in rows(session, project_id, grant.task_id, "revoke")
                    if r.payload["authorization_id"] == grant.id
                ),
                None,
            )
            return public(
                prior
                or append(
                    session,
                    project_id,
                    grant.task_id,
                    "revoke",
                    grant.planning_id,
                    {
                        "authorization_id": grant.id,
                        "reason": body.reason,
                        "approved_by": "local_operator",
                    },
                )
            )

        return mutate(sessions, project_id, "execution_revoke", body.model_dump(), idempotency_key, operation)
