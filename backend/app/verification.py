"""Versioned engineering reports and verified references; never run project scripts."""

import json
import platform
import sys

from fastapi import Header, Query
from sqlalchemy import select

from . import commands, contexts, execution, planning, requirements
from .db import CommandAsset, CommandRun, RequirementRecord
from .scanner import BusinessError, digest
from .verification_schemas import KINDS, Asset, Import, Plan, PlanPayload, Report, ResultPayload, Workspace

NOTICE = "七项工程检查与补充语法/格式分开；人工报告不是平台独立执行。效果评测与阶段验收有独立入口，真实样例仍待评分；本页工程记录不批准验收、费用或执行。仅核对显式文件，未选代码不在覆盖范围。"


def rows(session, pid, kind=None):
    q = select(RequirementRecord).where(RequirementRecord.project_id == pid)
    q = (
        q.where(RequirementRecord.kind == kind)
        if kind
        else q.where(RequirementRecord.kind.in_(["verification_plan", "verification_result"]))
    )
    return list(session.scalars(q.order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())))


def latest(session, pid, task):
    return next((r for r in rows(session, pid, "verification_plan") if r.payload["task_id"] == task), None)


def owned(session, pid, identifier, kind=None):
    requirements.project(session, pid)
    row = session.get(RequirementRecord, identifier)
    if (
        not row
        or row.project_id != pid
        or row.kind not in ("verification_plan", "verification_result")
        or (kind and row.kind != kind)
    ):
        raise BusinessError("NOT_FOUND", "工程验证资产不属于本项目", 404)
    return row


def environment(session, pid):
    row = planning.latest(session, pid, "environment")
    return {
        "factory_python": sys.version.split()[0],
        "factory_system": platform.system(),
        "project_environment_id": row.id if row else None,
        "project_environment_hash": digest(row.payload) if row else None,
    }


def plan_issues(session, settings, pid, row):
    p = row.payload
    current = latest(session, pid, p["task_id"])
    if not current or current.id != row.id:
        return ["检查要求已有新版本，旧结果需复验；历史保留"]
    try:
        index, source_map = contexts.collect(session, settings, pid, "execution", p["task_id"])
        if index["basis"] != p["basis"] or environment(session, pid) != p["environment"]:
            return ["需求/方案/计划或环境依据变化，需复验"]
        ex = execution.latest(session, pid, p["task_id"], "plan")
        if (ex.id if ex else None) != p["execution_id"]:
            return ["执行准备已有新版本，需复验"]
        for old in p["files"]:
            item = source_map.get(old["id"])
            if not item or item["source"] != old:
                return ["显式代码文件变化或不可安全读取，需复验"]
        return []
    except BusinessError:
        return ["当前来源不能安全核对，需复验"]


def imported(session, settings, pid, plan, run_id, hash_version=2):
    run = commands.owned(session, pid, run_id, CommandRun)
    asset = commands.owned(session, pid, run.asset_id, CommandAsset)
    if run.state not in {"succeeded", "failed"} or asset.payload["recipe_id"] not in {
        "python_syntax",
        "json_validate",
    }:
        raise BusinessError("INPUT_INVALID", "只能引用已完成的语法或格式检查，不启动配方", 409)
    if (
        asset.execution_id != plan.payload["execution_id"]
        or run.details["task_id"] != plan.payload["task_id"]
    ):
        raise BusinessError("INPUT_INVALID", "运行必须关联同一任务与执行准备", 409)
    files = {f["id"].removeprefix("file:"): f for f in plan.payload["files"]}
    for f in commands.facts(asset):
        current = files.get(f["path"])
        if not current or current["sha256"] != f["sha256"]:
            raise BusinessError("INPUT_INVALID", "运行输入不匹配本计划显式文件版本", 409)
    try:
        result = commands.read_artifact(settings, run).model_dump()
    except OSError:
        raise BusinessError("ARTIFACT_CONFLICT", "运行产物丢失或不能安全读取", 409) from None
    if (
        result["input_files"] != commands.facts(asset)
        or result["checks"] != run.details["checks"]
        or result["exit_code"] != run.details["exit_code"]
        or result["error_code"] != run.details["error_code"]
    ):
        raise BusinessError("ARTIFACT_CONFLICT", "产物与原运行记录不匹配", 409)
    if len(result["checks"]) != len(result["input_files"]) or [c["path"] for c in result["checks"]] != [
        f["path"] for f in result["input_files"]
    ]:
        raise BusinessError("ARTIFACT_CONFLICT", "检查结果与输入数量或路径不匹配", 409)
    passed = sum(c["valid"] for c in result["checks"])
    good = result["exit_code"] == 0 and not result["error_code"] and passed == len(result["checks"])
    if (run.state == "succeeded") != good:
        raise BusinessError("ARTIFACT_CONFLICT", "运行终态与结果不匹配", 409)
    return (
        run,
        asset,
        result,
        digest(
            {
                "id": run.id,
                "state": run.state,
                "details": run.details
                if hash_version == 1
                else {
                    k: run.details[k]
                    for k in (
                        "task_id",
                        "recipe_id",
                        "argv",
                        "started_at",
                        "ended_at",
                        "exit_code",
                        "error_code",
                        "checks",
                        "artifacts",
                    )
                },
                "asset": asset.payload,
            }
        ),
        good,
        passed,
    )


def problems(session, settings, row):
    if row.kind == "verification_plan":
        return plan_issues(session, settings, row.project_id, row)
    plan = owned(session, row.project_id, row.payload["plan_id"], "verification_plan")
    issues = plan_issues(session, settings, row.project_id, plan)
    if row.payload["source"] == "platform_run":
        try:
            *_, run_hash, good, passed = imported(
                session,
                settings,
                row.project_id,
                plan,
                row.payload["run_id"],
                row.payload.get("run_hash_version", 1),
            )
            if run_hash != row.payload["run_hash"]:
                issues.append("关联运行或产物版本变化，证据异常")
        except BusinessError:
            issues.append("关联运行产物缺失、不匹配或不可读取，证据异常")
    return issues


def public(session, settings, row):
    if not contexts.safe(json.dumps(row.payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "验证历史可能包含当前受保护资料，不能返回原文", 409)
    issues = problems(session, settings, row)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "kind": row.kind,
        "created_at": requirements.timestamp(row.created_at),
        "payload": row.payload,
        "content_hash": digest(row.payload),
        "current": not issues,
        "issues": issues,
    }


def current_plan(session, settings, pid, body):
    row = owned(session, pid, body.plan_id, "verification_plan")
    if not contexts.safe(json.dumps(row.payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "计划可能包含当前受保护资料，不能沿用", 409)
    if digest(row.payload) != body.expected_hash:
        raise BusinessError("REVISION_CONFLICT", "计划摘要不一致，输入保留", 409)
    if plan_issues(session, settings, pid, row):
        raise BusinessError("VERIFICATION_STALE", "工程检查依据已变化，先另存当前计划；旧历史保留", 409)
    return row


def append(session, settings, pid, kind, payload):
    if not contexts.safe(json.dumps(payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "报告或要求含受保护资料，请先脱敏", 409)
    row = requirements.record(session, pid, kind, payload)
    requirements.audit(session, pid, kind + "_saved", {"asset_id": row.id, "kind": kind})
    return public(session, settings, row)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/verification"

    @app.get(prefix, response_model=Workspace, response_model_exclude_unset=True)
    def workspace(project_id: str, task_id: str | None = Query(default=None, pattern=r"^T[0-9]{1,4}$")):
        with sessions() as session:
            initial, _ = contexts.collect(session, settings, project_id, "planning", None)
            task = task_id or (initial["tasks"][0]["id"] if initial["tasks"] else None)
            index, _ = (
                contexts.collect(session, settings, project_id, "execution", task) if task else (initial, {})
            )
            plan = latest(session, project_id, task)
            history = [public(session, settings, r) for r in rows(session, project_id)]
            results = [
                r
                for r in history
                if r["kind"] == "verification_result" and plan and r["payload"]["plan_id"] == plan.id
            ]
            matrix = []
            for kind in KINDS:
                result = next((r for r in results if r["payload"]["kind"] == kind), None)
                matrix.append(
                    {
                        "kind": kind,
                        "criterion": plan.payload["criteria"][kind] if plan else "待保存检查计划",
                        "status": result["payload"]["status"] if result else "pending",
                        "source": result["payload"]["source"] if result else None,
                        "result_id": result["id"] if result else None,
                        "current": result["current"] if result else False,
                        "issues": result["issues"] if result else ["尚未执行或报告，待验"],
                    }
                )
            runs = []
            for run in commands.rows(session, project_id):
                if (
                    run.details["task_id"] == task
                    and run.details["recipe_id"] in {"python_syntax", "json_validate"}
                    and run.state in {"succeeded", "failed"}
                ):
                    runs.append(
                        {
                            "id": run.id,
                            "task_id": task,
                            "recipe_id": run.details["recipe_id"],
                            "state": run.state,
                        }
                    )
            return {
                "index": index,
                "plan": public(session, settings, plan) if plan else None,
                "matrix": matrix,
                "supplemental": [r for r in results if r["payload"]["kind"] == "syntax"],
                "history": history,
                "runs": runs,
                "notice": NOTICE,
            }

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset, response_model_exclude_unset=True)
    def read(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/plans", response_model=Asset, response_model_exclude_unset=True)
    def save_plan(project_id: str, body: Plan, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            index, source_map = contexts.collect(session, settings, project_id, "execution", body.task_id)
            previous = latest(session, project_id, body.task_id)
            if (previous.payload["revision"] if previous else 0) != body.expected_revision or index[
                "index_hash"
            ] != body.expected_index_hash:
                raise BusinessError(
                    "REVISION_CONFLICT", "计划版本或资料索引已变化，输入保留，请刷新核对", 409
                )
            files = []
            for name in body.files:
                item = source_map.get("file:" + name)
                if not item or item["source"]["status"] != "ready" or item["source"]["kind"] != "file":
                    raise BusinessError("INPUT_INVALID", "所选文件不在本项目安全索引中", 409)
                files.append(item["source"])
            ex = execution.latest(session, project_id, body.task_id, "plan")
            payload = PlanPayload(
                schema_version=1,
                revision=body.expected_revision + 1,
                previous_id=previous.id if previous else None,
                task_id=body.task_id,
                execution_id=ex.id if ex else None,
                basis=index["basis"],
                index_hash=index["index_hash"],
                files=files,
                criteria=body.criteria,
                environment=environment(session, project_id),
                reason=body.reason,
            ).model_dump()
            return append(session, settings, project_id, "verification_plan", payload)

        return requirements.mutate(
            sessions, project_id, "verification_plan", body.model_dump(), idempotency_key, operation
        )

    @app.post(prefix + "/reports", response_model=Asset, response_model_exclude_unset=True)
    def report(project_id: str, body: Report, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            plan = current_plan(session, settings, project_id, body)
            value = body.model_dump(exclude={"expected_hash", "confirmed"})
            payload = ResultPayload(
                schema_version=1, task_id=plan.payload["task_id"], source="operator_report", **value
            ).model_dump()
            return append(session, settings, project_id, "verification_result", payload)

        return requirements.mutate(
            sessions, project_id, "verification_report", body.model_dump(), idempotency_key, operation
        )

    @app.post(prefix + "/imports", response_model=Asset, response_model_exclude_unset=True)
    def import_run(project_id: str, body: Import, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            plan = current_plan(session, settings, project_id, body)
            run, asset, result, run_hash, good, passed = imported(
                session, settings, project_id, plan, body.run_id
            )
            failed = len(result["checks"]) - passed
            failures = [
                c["path"] + " · " + (c["error_code"] or "检查失败")
                for c in result["checks"]
                if not c["valid"]
            ]
            if not good and not failures:
                failures = [result["error_code"] or "配方退出失败"]
            payload = ResultPayload(
                schema_version=1,
                plan_id=plan.id,
                task_id=plan.payload["task_id"],
                source="platform_run",
                kind="syntax",
                status="passed" if good else "failed",
                executed=True,
                passed=passed,
                total=len(result["checks"]),
                failed=failed,
                failures=failures,
                warnings=[],
                evidence=[
                    "/projects/" + project_id + "/commands · run " + run.id + " · artifact " + digest(result)
                ],
                method=asset.payload["recipe_id"]
                + " · "
                + json.dumps(run.details["argv"], ensure_ascii=False),
                environment="既有受控隔离配方；原运行 Python 版本未记录，不以当前环境推断",
                responsible="platform_recipe",
                reason="后端核对既有运行产物；不是单元测试/类型/Lint/构建",
                run_id=run.id,
                run_hash=run_hash,
                run_hash_version=2,
                artifact_hash=digest(result),
            ).model_dump()
            return append(session, settings, project_id, "verification_result", payload)

        return requirements.mutate(
            sessions, project_id, "verification_import", body.model_dump(), idempotency_key, operation
        )
