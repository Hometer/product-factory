"""FR19 local artifact registry; adoption and previews do not grant execution or acceptance."""

import difflib
import json
import os
from pathlib import Path

from fastapi import Header
from sqlalchemy import select

from . import contexts, planning, requirements, version_files
from .artifact_schemas import Asset, Decision, Entry, Rollback, Version, Workspace
from .db import CommandAsset, CommandRun, ExecutionAsset, PatchAsset, RequirementRecord, RequirementRevision
from .scanner import BusinessError, canonical, digest

KINDS = {"artifact_entry", "artifact_decision", "code_version", "rollback_preview"}
NATIVE = {
    "baseline",
    "verification_plan",
    "verification_result",
    "evaluation_plan",
    "evaluation_score",
    "evaluation_standard",
    "acceptance_package",
    "acceptance_decision",
    "run",
}
NOTICE = "采用状态只管理产物，不代表工程、模型或阶段验收通过。业务版本与文件版本分开；Git 仅只读所列安全文件。回退预览不修改代码、Git 或原始数据，数据兼容性未知时继续待复核。"
PATHS = {
    "draft": {"candidate", "unavailable"},
    "candidate": {"adopted", "draft", "unavailable"},
    "adopted": {"superseded", "candidate", "unavailable"},
    "superseded": {"candidate", "unavailable"},
    "unavailable": {"candidate"},
}


def safe(value, settings):
    if not contexts.safe(json.dumps(value, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "资料含受保护内容，请脱敏后保存", 409)


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


def owned(session, pid, identifier, kind=None):
    requirements.project(session, pid)
    row = session.get(RequirementRecord, identifier)
    if not row or row.project_id != pid or row.kind not in KINDS or (kind and row.kind != kind):
        raise BusinessError("NOT_FOUND", "记录不属于当前项目", 404)
    return row


def state(session, pid, entry):
    return next(
        (r for r in rows(session, pid, "artifact_decision") if r.payload["entry_id"] == entry.id), entry
    )


def association(session, pid, body):
    plan = planning.latest(session, pid, "plan")
    if not plan:
        raise BusinessError("PLANNING_REQUIRED", "请先保存包含版本、任务与需求的阶段计划", 409)
    text = plan.payload["text"]
    task = next((t for t in text["tasks"] if t["id"] == body.task_id), None)
    stage = next((s for s in text["stages"] if task and s["id"] == task["stage_id"]), None)
    if (
        not task
        or not stage
        or stage["version_id"] != body.version_id
        or not set(body.requirement_ids).issubset(task["requirement_ids"])
    ):
        raise BusinessError("ASSOCIATION_INVALID", "版本、任务及需求必须属于当前项目同一计划范围", 409)
    return dict(planning_id=plan.id, draft_revision=requirements.latest(session, pid)[0])


def catalog(session, settings, pid):
    index, source_map, git, index_hash = version_files.index(session, settings, pid)
    sources = {}
    for identifier, item in source_map.items():
        s = item["source"]
        if s["status"] == "ready":
            sources[identifier] = dict(
                id=identifier,
                kind=s["kind"],
                title=s["title"],
                locator=s["locator"],
                sha256=s["sha256"],
                task_id=identifier[5:] if identifier.startswith("task:") else None,
                run_id=None,
                current=True,
            )

    def add(identifier, kind, row, current=True, task_id=None, run_id=None):
        try:
            safe(row.payload if hasattr(row, "payload") else row.details, settings)
        except BusinessError:
            return
        sources[identifier] = dict(
            id=identifier,
            kind=kind,
            title=kind + " · " + row.id[:8],
            locator=kind + " / " + row.id,
            sha256=digest(
                row.payload if hasattr(row, "payload") else dict(state=row.state, details=row.details)
            ),
            task_id=task_id,
            run_id=run_id,
            current=current,
        )

    for row in session.scalars(select(RequirementRevision).where(RequirementRevision.project_id == pid)):
        add(
            "revision:" + row.id,
            "requirement_revision",
            row,
            row.revision == index["basis"]["draft_revision"],
        )
    for row in session.scalars(
        select(RequirementRecord).where(
            RequirementRecord.project_id == pid, RequirementRecord.kind.in_(NATIVE)
        )
    ):
        current = True
        if (
            row.kind.startswith("verification_")
            or row.kind.startswith("evaluation_")
            or row.kind.startswith("acceptance_")
        ):
            from . import acceptance, evaluations, verification

            module = (
                verification
                if row.kind.startswith("verification_")
                else evaluations
                if row.kind.startswith("evaluation_")
                else acceptance
            )
            try:
                current = module.public(session, settings, row)["current"]
            except BusinessError:
                current = False
        elif row.kind == "run":
            add(
                "model-run:" + row.id,
                "analysis_run",
                row,
                row.payload.get("state") in {"succeeded", "failed"}
                and row.payload.get("revision") == index["basis"]["draft_revision"],
                run_id=row.id,
            )
            if "model-run:" + row.id in sources:
                sources["model-run:" + row.id]["provenance"] = row.payload.get("source", "unknown")
            continue
        elif row.kind == "baseline":
            current = bool(
                planning.baselines(session, pid) and planning.baselines(session, pid)[0].id == row.id
            )
        add("record:" + row.id, row.kind, row, current, row.payload.get("task_id"), row.payload.get("run_id"))
    for row in session.scalars(select(ExecutionAsset).where(ExecutionAsset.project_id == pid)):
        from . import execution

        current = execution.latest(session, pid, row.task_id, row.kind)
        add(
            "execution:" + row.id,
            "execution_" + row.kind,
            row,
            bool(current and current.id == row.id),
            row.task_id,
        )
    for row in session.scalars(select(PatchAsset).where(PatchAsset.project_id == pid)):
        from . import patches

        try:
            patches.sources(session, settings, pid, row)
            current = True
        except BusinessError:
            current = False
        add("patch:" + row.id, "code_change", row, current, row.payload.get("task_id"))
    for row in session.scalars(select(CommandRun).where(CommandRun.project_id == pid)):
        asset = session.get(CommandAsset, row.asset_id)
        if asset and asset.project_id == pid:
            add(
                "run:" + row.id,
                "command_run",
                row,
                row.state in {"succeeded", "failed", "stopped", "cancelled"},
                asset.payload["task_id"],
                row.id,
            )
    root = canonical(requirements.project(session, pid).canonical_path, settings.allowed_roots)
    # Metadata-only image inventory, bounded by the same traversal limits as context scans.
    count = 0
    for directory_path, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in version_files.patch_files.EXCLUDED
            and not contexts.private_name(d)
            and not (Path(directory_path) / d).is_symlink()
        ]
        count += len(dirs) + len(names)
        if count > 5000:
            raise BusinessError("SCAN_LIMIT", "资料目录超过 5000 项，请缩小项目范围")
        for name in names:
            if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"}:
                rel = (Path(directory_path) / name).relative_to(root).as_posix()
                try:
                    item, _ = version_files.file_source(session, settings, pid, root, rel)
                    item["run_id"] = None
                    sources[item["id"]] = item
                except BusinessError:
                    pass
    fingerprint = digest(dict(index_hash=index_hash, sources=sources))
    for row in rows(session, pid, "code_version"):
        add(
            "version:" + row.id,
            "code_version",
            row,
            row.payload["index_hash"] == fingerprint,
            row.payload["task_id"],
        )
    return index, sources, git, fingerprint


def source(session, settings, pid, identifier, sources):
    if identifier.startswith("file:"):
        root = canonical(requirements.project(session, pid).canonical_path, settings.allowed_roots)
        item, _ = version_files.file_source(session, settings, pid, root, identifier[5:])
        item["run_id"] = None
        return item
    item = sources.get(identifier)
    if not item:
        raise BusinessError("SOURCE_UNAVAILABLE", "来源不属于当前项目或不可安全读取", 404)
    return item


def problems(session, settings, row, data):
    _, sources, _, index_hash = data
    p = row.payload
    issues = []
    if row.kind == "artifact_decision":
        row = owned(session, row.project_id, p["entry_id"], "artifact_entry")
        p = row.payload
    if row.kind == "artifact_entry":
        try:
            s = source(session, settings, row.project_id, p["source"]["id"], sources)
            if not s["current"] or s["sha256"] != p["source"]["sha256"]:
                issues.append("来源版本已变化；历史保留，请重新登记或重新定位")
        except BusinessError:
            issues.append("文件被移走、删除或来源不可安全核对，请重新定位")
        plan = planning.latest(session, row.project_id, "plan")
        if (
            not plan
            or plan.id != p["basis"]["planning_id"]
            or requirements.latest(session, row.project_id)[0] != p["basis"]["draft_revision"]
        ):
            issues.append("业务需求或计划版本已变化，请核对关联")
    elif row.kind == "code_version":
        if p["index_hash"] != index_hash:
            issues.append("当前文件、Git 或业务索引已变化；快照仍保留，可供回退前对比")
    elif row.kind == "rollback_preview" and p["index_hash"] != index_hash:
        issues.append("预览后当前文件或业务索引已变化，请另存新的对比")
    return issues


def public(session, settings, row, data=None):
    safe(row.payload, settings)
    data = data or catalog(session, settings, row.project_id)
    issues = problems(session, settings, row, data)
    root = (
        owned(session, row.project_id, row.payload["entry_id"], "artifact_entry")
        if row.kind == "artifact_decision"
        else row
    )
    s = state(session, row.project_id, root) if root.kind == "artifact_entry" else None
    if row.kind == "artifact_decision" and s and s.id != row.id:
        issues.append("已有新的采用判断；这份原判断保持，不替代当前状态")
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=row.payload,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        current=not issues,
        issues=issues,
        status=(
            row.payload["status"] if row.kind == "artifact_decision" else s.payload.get("status", "draft")
        )
        if s
        else None,
        state_id=s.id if s else None,
        state_hash=digest(s.payload) if s else None,
    )


def append(session, settings, pid, record_kind, **payload):
    data = dict(schema_version=1, actor="local_operator", role="product_owner", **payload)
    safe(data, settings)
    row = requirements.record(session, pid, record_kind, data)
    requirements.audit(
        session, pid, record_kind + "_saved", {"asset_id": row.id, "content_hash": digest(data)}
    )
    return public(session, settings, row)


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/artifacts"

    @app.get(prefix, response_model=Workspace)
    def get(project_id: str):
        with sessions() as session:
            requirements.project(session, project_id)
            data = catalog(session, settings, project_id)
            plan = planning.latest(session, project_id, "plan")
            history = [public(session, settings, r, data) for r in rows(session, project_id)]
            snapshot_files = []
            for identifier in data[1]:
                if identifier.startswith("file:"):
                    try:
                        version_files.patch_files.name_check(identifier[5:])
                        snapshot_files.append(identifier[5:])
                    except BusinessError:
                        pass
            return dict(
                snapshot_files=sorted(snapshot_files),
                tasks=[
                    dict(
                        t,
                        version_id=next(
                            s["version_id"]
                            for s in plan.payload["text"]["stages"]
                            if s["id"] == t["stage_id"]
                        ),
                    )
                    for t in plan.payload["text"]["tasks"]
                ]
                if plan
                else [],
                versions=plan.payload["text"]["versions"] if plan else [],
                sources=list(data[1].values()),
                index_hash=data[3],
                git=data[2],
                code_revision=len(rows(session, project_id, "code_version")),
                entries=[r for r in history if r["kind"] == "artifact_entry"],
                history=history,
                code_versions=[r for r in history if r["kind"] == "code_version"],
                rollback_previews=[r for r in history if r["kind"] == "rollback_preview"],
                notice=NOTICE,
            )

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/entries", response_model=Asset)
    def entry(project_id: str, body: Entry, idempotency_key: str | None = Header(default=None)):
        def action(session):
            basis = association(session, project_id, body)
            data = catalog(session, settings, project_id)
            item = source(session, settings, project_id, body.source_id, data[1])
            if item["sha256"] != body.source_hash:
                raise BusinessError("REVISION_CONFLICT", "来源已变化，输入保留，请核对新版", 409)
            if item.get("task_id") and item["task_id"] != body.task_id:
                raise BusinessError("ASSOCIATION_INVALID", "来源任务与本次关联任务不一致", 409)
            if body.run_id != item.get("run_id"):
                raise BusinessError(
                    "RUN_ASSOCIATION_INVALID", "运行来源必须来自所选同项目运行记录，不能附会", 409
                )
            if body.run_id:
                run = session.get(CommandRun, body.run_id)
                analysis = session.get(RequirementRecord, body.run_id)
                if run:
                    command = session.get(CommandAsset, run.asset_id)
                    if (
                        run.project_id != project_id
                        or not command
                        or command.project_id != project_id
                        or command.payload.get("task_id") != body.task_id
                    ):
                        raise BusinessError("RUN_ASSOCIATION_INVALID", "运行不属于同项目同任务", 409)
                elif not analysis or analysis.project_id != project_id or analysis.kind != "run":
                    raise BusinessError("RUN_ASSOCIATION_INVALID", "分析运行不属于当前项目", 409)
            if body.kind == "screenshot" and item.get("mime") not in {"image/png", "image/jpeg"}:
                raise BusinessError("SOURCE_TYPE_INVALID", "截图需选择经真实类型检查的图片", 409)
            if body.kind == "delivery_package" and item["kind"] not in {"acceptance_package", "file"}:
                raise BusinessError(
                    "SOURCE_TYPE_INVALID", "首版仅登记已有证据包或文本清单，不生成交付压缩包", 409
                )
            if body.relocates_id:
                previous = owned(session, project_id, body.relocates_id, "artifact_entry")
                if any(
                    previous.payload[k] != getattr(body, k)
                    for k in ("kind", "task_id", "version_id", "requirement_ids")
                ):
                    raise BusinessError("ASSOCIATION_INVALID", "重新定位保留原产物类型和业务关联", 409)
            return append(
                session,
                settings,
                project_id,
                "artifact_entry",
                **body.model_dump(),
                basis=basis,
                source=item,
                status="draft",
            )

        return requirements.mutate(
            sessions, project_id, "artifact_entry", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/decisions", response_model=Asset)
    def decide(project_id: str, body: Decision, idempotency_key: str | None = Header(default=None)):
        def action(session):
            root = owned(session, project_id, body.entry_id, "artifact_entry")
            previous = state(session, project_id, root)
            if previous.id != body.previous_id or digest(previous.payload) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "采用状态已变化，输入保留", 409)
            if body.status not in PATHS[previous.payload["status"]]:
                raise BusinessError("STATE_TRANSITION_INVALID", "采用状态路径无效", 409)
            if body.status == "adopted" and not public(session, settings, root)["current"]:
                raise BusinessError("SOURCE_STALE", "当前来源或业务版本已变化，不能采用", 409)
            if body.status == "superseded":
                replacement = owned(session, project_id, body.replacement_id, "artifact_entry")
                if (
                    replacement.id == root.id
                    or state(session, project_id, replacement).payload["status"] != "adopted"
                    or not public(session, settings, replacement)["current"]
                    or any(
                        replacement.payload[k] != root.payload[k]
                        for k in ("kind", "version_id", "task_id", "requirement_ids")
                    )
                ):
                    raise BusinessError(
                        "REPLACEMENT_INVALID", "需同项目同业务范围、当前有效已采用的新产物", 409
                    )
                visited = {root.id}
                cursor = replacement
                while cursor:
                    if cursor.id in visited:
                        raise BusinessError("REPLACEMENT_INVALID", "替代关系不能形成循环", 409)
                    visited.add(cursor.id)
                    link = next(
                        (
                            r.payload.get("replacement_id")
                            for r in rows(session, project_id, "artifact_decision")
                            if r.payload["entry_id"] == cursor.id and r.payload.get("replacement_id")
                        ),
                        None,
                    )
                    cursor = owned(session, project_id, link, "artifact_entry") if link else None
            elif body.replacement_id:
                raise BusinessError("REPLACEMENT_INVALID", "只有替代状态可关联新产物", 409)
            return append(session, settings, project_id, "artifact_decision", **body.model_dump())

        return requirements.mutate(
            sessions, project_id, "artifact_decision", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/versions", response_model=Asset)
    def version(project_id: str, body: Version, idempotency_key: str | None = Header(default=None)):
        def action(session):
            basis = association(session, project_id, body)
            data = catalog(session, settings, project_id)
            if (
                data[3] != body.expected_index_hash
                or len(rows(session, project_id, "code_version")) != body.expected_revision
            ):
                raise BusinessError("REVISION_CONFLICT", "当前文件索引或快照修订已变化，输入保留", 409)
            files = version_files.selected(session, settings, project_id, body.files)
            if any(data[1].get("file:" + f["path"], {}).get("sha256") != f["sha256"] for f in files):
                raise BusinessError("REVISION_CONFLICT", "选中文件读取时发生变化，请重新核对", 409)
            return append(
                session,
                settings,
                project_id,
                "code_version",
                **body.model_dump(exclude={"files"}),
                files=files,
                basis=basis,
                index_hash=data[3],
                revision=body.expected_revision + 1,
                git=data[2],
            )

        return requirements.mutate(
            sessions, project_id, "code_version", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/rollback-previews", response_model=Asset)
    def rollback(project_id: str, body: Rollback, idempotency_key: str | None = Header(default=None)):
        def action(session):
            target = owned(session, project_id, body.version_id, "code_version")
            data = catalog(session, settings, project_id)
            if digest(target.payload) != body.expected_hash or data[3] != body.expected_index_hash:
                raise BusinessError("REVISION_CONFLICT", "目标版本或当前索引已变化，输入保留", 409)
            root = canonical(requirements.project(session, project_id).canonical_path, settings.allowed_roots)
            differences = []
            for f in target.payload["files"]:
                try:
                    s, current = version_files.file_source(
                        session, settings, project_id, root, f["path"], True
                    )
                except BusinessError:
                    differences.append(
                        dict(
                            path=f["path"],
                            status="unavailable",
                            current_hash=None,
                            target_hash=f["sha256"],
                            diff="文件缺失或受保护，不能推断可安全恢复。",
                        )
                    )
                    continue
                diff = "".join(
                    difflib.unified_diff(
                        current.splitlines(True),
                        f["content"].splitlines(True),
                        fromfile="当前/" + f["path"],
                        tofile="目标/" + f["path"],
                    )
                )
                differences.append(
                    dict(
                        path=f["path"],
                        status="unchanged" if s["sha256"] == f["sha256"] else "changed",
                        current_hash=s["sha256"],
                        target_hash=f["sha256"],
                        diff=diff[:131072],
                    )
                )
            additions = sorted(
                s[5:]
                for s in data[1]
                if s.startswith("file:") and s[5:] not in {f["path"] for f in target.payload["files"]}
            )
            return append(
                session,
                settings,
                project_id,
                "rollback_preview",
                **body.model_dump(),
                index_hash=data[3],
                target_hash=digest(target.payload),
                differences=differences,
                outside_target=additions,
                writes_files=False,
                compatibility_source="operator_review",
                limitation="只对所选文件做对比；其他文件不在目标快照中。兼容性是人工复核意见，数据库和原始数据均不修改。",
            )

        return requirements.mutate(
            sessions, project_id, "rollback_preview", body.model_dump(), idempotency_key, action
        )
