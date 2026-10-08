"""Reviewed, zero-cost text patches with a durable pre-write journal and compensation."""

import difflib
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from sqlalchemy import select

from . import execution
from .db import PatchAsset, PatchRun, Project
from .patch_files import Files, identity, root_check, text_check, workspace_lock
from .patch_schemas import Apply, Asset, Payload, Preview, Recover, Run, Workspace
from .requirements import audit, locked, mutate, project, timestamp
from .scanner import EXCLUDED, BusinessError, canonical, digest

UNFINISHED = {"preparing", "applying", "rollback_required"}


def owned(session, pid, identifier, cls=PatchAsset):
    row = session.get(cls, identifier)
    if not row or row.project_id != pid:
        raise BusinessError("NOT_FOUND", "补丁或运行不属于此项目", 404)
    return row


def runs(session, pid=None):
    query = select(PatchRun)
    if pid:
        query = query.where(PatchRun.project_id == pid)
    return list(session.scalars(query.order_by(PatchRun.created_at.desc(), PatchRun.id)))


def run_public(row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "patch_id": row.patch_id,
        "state": row.state,
        "details": row.details,
        "created_at": timestamp(row.created_at),
    }


def safe_root(session, settings, pid):
    root = canonical(project(session, pid).canonical_path, settings.allowed_roots)
    root_check(root, settings)
    # A descendant project may never be touched by a parent patch. Excluded areas of the parent
    # (e.g. a synthetic project under factory data/) are outside that parent's file scope.
    for other in session.scalars(select(Project).where(Project.id != pid)):
        path = Path(other.canonical_path)
        if path.exists() and root.samefile(path):
            raise BusinessError("PROJECT_OVERLAP", "同一目录存在其他项目登记，需核对项目归属", 409)
        if root.is_relative_to(path) and not any(p in EXCLUDED for p in root.relative_to(path).parts):
            raise BusinessError("PROJECT_OVERLAP", "登记目录与另一项目重叠，需独立项目范围", 409)
    return root


def target_scope(session, pid, root, names):
    for other in session.scalars(select(Project).where(Project.id != pid)):
        path = Path(other.canonical_path)
        if any(
            (root / name).is_relative_to(path)
            and not any(p in EXCLUDED for p in (root / name).relative_to(path).parts)
            for name in names
        ):
            raise BusinessError("PROJECT_OVERLAP", "目标文件属于另一登记项目，已停止操作", 409)


def sources(session, settings, pid, asset):
    row = execution.owned(session, pid, asset.execution_id, "plan")
    if (
        execution.latest(session, pid, row.task_id, "plan").id != row.id
        or digest(row.payload) != asset.payload["execution_hash"]
    ):
        raise BusinessError("INPUT_STALE", "执行版本已变化，旧补丁保留，请重新预览", 409)
    execution.source(session, settings, pid, row.planning_id)
    text, inputs = row.payload["text"], row.payload["inputs"]
    if (
        text["estimate_micro"]
        or any(s["operation"] not in {"read_only", "code_write"} for s in text["steps"])
        or not any(s["operation"] == "code_write" for s in text["steps"])
    ):
        raise BusinessError("RECIPE_UNSUPPORTED", "仅支持本地零费代码写入范围", 409)
    if any(inputs[k] for k in ("task_dependencies", "stage_dependencies", "version_dependencies")):
        raise BusinessError("DEPENDENCIES_PENDING", "前置任务或阶段结果尚未交付，不能应用补丁", 409)
    grant = execution.authorization(session, pid, row)
    if not grant:
        raise BusinessError("AUTHORIZATION_REQUIRED", "本地文件范围尚未授权或已撤销", 409)
    root = safe_root(session, settings, pid)
    if str(root) != asset.payload["root"]:
        raise BusinessError("INPUT_STALE", "项目目录已变化，不能应用旧补丁", 409)
    target_scope(session, pid, root, [f["path"] for f in asset.payload["files"]])
    patch_names = {f["path"] for f in asset.payload["files"]}
    untouched = [f for f in row.payload["files"] if f["path"] not in patch_names]
    if execution.file_facts(root, [f["path"] for f in untouched]) != untouched:
        raise BusinessError("FILE_CONFLICT", "选中但未写入的文件发生外部变化，已停止", 409)
    return row, grant, root


def file_match(files, asset, allow_after=False):
    if files.root_identity != asset.payload["root_identity"]:
        raise BusinessError("FILE_CONFLICT", "项目目录已被替换，已停止操作", 409)
    for item in asset.payload["files"]:
        if identity(os.fstat(files.parents[item["path"]])) != item["parent_identity"]:
            raise BusinessError("FILE_CONFLICT", "文件目录已被替换，已停止操作", 409)
        current, mode = files.read(item["path"])
        allowed = [item["before"], item["after"]] if allow_after else [item["before"]]
        if current not in allowed or current is not None and mode != item["mode"]:
            raise BusinessError("FILE_CONFLICT", "外部文件与补丁冲突，外部内容及候选保留", 409)


def eligible(session, settings, pid, asset):
    row, grant, root = sources(session, settings, pid, asset)
    if execution.source_file_issues(session, settings, pid, row):
        raise BusinessError("FILE_CONFLICT", "选中文件发生外部变化，请核对后另存执行版本和补丁", 409)
    if any(
        r.state in UNFINISHED
        for r in runs(session)
        if session.get(PatchAsset, r.patch_id).payload["root"] == str(root)
    ):
        raise BusinessError("RECOVERY_REQUIRED", "本项目有未结束运行，请先查询并核对恢复", 409)
    if any(r.patch_id == asset.id for r in runs(session, pid)):
        raise BusinessError("PATCH_ALREADY_ATTEMPTED", "此补丁已有运行记录，不重复写入；修改后另存预览", 409)
    with Files(root, [f["path"] for f in asset.payload["files"]]) as files:
        file_match(files, asset)
    return grant, root


def public(session, settings, row):
    problems = []
    try:
        eligible(session, settings, row.project_id, row)
        with workspace_lock(settings, Path(row.payload["root"])):
            pass
    except (BusinessError, OSError) as exc:
        problems.append(exc.message if isinstance(exc, BusinessError) else "文件或项目锁暂不可安全核对")
    return {
        "id": row.id,
        "project_id": row.project_id,
        "execution_id": row.execution_id,
        "payload": row.payload,
        "content_hash": digest(row.payload),
        "created_at": timestamp(row.created_at),
        "can_apply": not problems,
        "blockers": problems,
    }


def preview(session, settings, pid, body):
    row = execution.selected_current(session, pid, body)
    execution.source(session, settings, pid, row.planning_id)
    if execution.source_file_issues(session, settings, pid, row):
        raise BusinessError("INPUT_STALE", "输入或文件已失效，请核对后保存新执行修订", 409)
    root = safe_root(session, settings, pid)
    if not body.validation_plan.strip() or execution.contains_secret(body.validation_plan):
        raise BusinessError("INPUT_INVALID", "验证计划不能为空或包含秘密")
    if any(f.path not in row.payload["text"]["files"] for f in body.files):
        raise BusinessError("PATH_FORBIDDEN", "补丁文件必须属于已保存的执行文件范围", 409)
    target_scope(session, pid, root, [f.path for f in body.files])
    total, result = 0, []
    with Files(root, [f.path for f in body.files]) as files:
        for candidate in body.files:
            text_check(candidate.content)
            before, mode = files.read(candidate.path)
            total += len(candidate.content.encode()) + len((before or "").encode())
            if total > 262144:
                raise BusinessError("INPUT_INVALID", "原文与候选总量超过 256 KiB")
            if before == candidate.content:
                raise BusinessError("NO_CHANGE", "文件没有变化，请核对候选内容")
            # Full-context unified diff, including explicit EOF marker for newline-only changes.
            diff = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    candidate.content.splitlines(keepends=True),
                    fromfile="a/" + candidate.path,
                    tofile="b/" + candidate.path,
                    n=65536,
                )
            )
            if before is None and not candidate.content:
                diff = "新增空文件（0 字节）"
            result.append(
                {
                    "path": candidate.path,
                    "before": before,
                    "after": candidate.content,
                    "diff": diff,
                    "mode": mode,
                    "parent_identity": identity(os.fstat(files.parents[candidate.path])),
                }
            )
        payload = Payload.model_validate(
            {
                "source": "manual_input",
                "task_id": row.task_id,
                "execution_hash": digest(row.payload),
                "authorization_hash": row.payload["authorization_hash"],
                "inputs": row.payload["inputs"],
                "acceptance": row.payload["acceptance"],
                "required_checks": row.payload["required_checks"],
                "validation_plan": body.validation_plan,
                "root": str(root),
                "root_identity": files.root_identity,
                "files": result,
            }
        ).model_dump()
    asset = PatchAsset(id=str(uuid4()), project_id=pid, execution_id=row.id, payload=payload)
    session.add(asset)
    session.flush()
    audit(
        session,
        pid,
        "patch_preview",
        {"patch_id": asset.id, "file_count": len(result), "hash": digest(payload)},
    )
    return public(session, settings, asset)


def update(session, run, state, message, completed=None):
    run.state = state
    run.details = {
        **run.details,
        "message": message,
        "completed": completed if completed is not None else run.details["completed"],
    }
    audit(
        session,
        run.project_id,
        "patch_" + state,
        {"run_id": run.id, "patch_id": run.patch_id, "completed_count": len(run.details["completed"])},
    )


def backup(settings, run, asset):
    # File contents belong to private business assets, never to logs/audit events.
    directory = settings.data_dir / "patch-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory = directory / run.id
    directory.mkdir(mode=0o700)
    with Files(directory, ["manifest.json"]) as files:
        for i, item in enumerate(asset.payload["files"]):
            for suffix in ("before", "after"):
                if item[suffix] is None:
                    continue
                fd = os.open(
                    directory / f"{i}.{suffix}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(item[suffix].encode())
                    stream.flush()
                    os.fsync(stream.fileno())
        os.fsync(files.root_fd)
    fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compensate(sessions, settings, run_id):
    try:
        with sessions() as session:
            run = session.get(PatchRun, run_id)
            asset = session.get(PatchAsset, run.patch_id)
            if run.state == "preparing":
                with locked(sessions) as write:
                    update(
                        write, write.get(PatchRun, run_id), "failed", "写入尚未开始；不自动重发，原文件保持"
                    )
                return
            root = safe_root(session, settings, run.project_id)
            target_scope(session, run.project_id, root, [f["path"] for f in asset.payload["files"]])
            if str(root) != asset.payload["root"]:
                raise BusinessError("FILE_CONFLICT", "项目目录变化，需人工核对恢复", 409)
        with Files(root, [f["path"] for f in asset.payload["files"]]) as files:
            file_match(files, asset, True)
            for item in reversed(asset.payload["files"]):
                current = files.read(item["path"])[0]
                if current == item["after"]:
                    with locked(sessions) as session:
                        safe_root(session, settings, run.project_id)
                        target_scope(
                            session, run.project_id, root, [f["path"] for f in asset.payload["files"]]
                        )
                        files.write(item["path"], item["before"], item["mode"], current)
            file_match(files, asset)
        with locked(sessions) as session:
            update(
                session,
                session.get(PatchRun, run_id),
                "rolled_back",
                "文件写入未完成，已恢复到应用前文本；仍需核对",
            )
    except Exception:
        with locked(sessions) as session:
            update(
                session,
                session.get(PatchRun, run_id),
                "rollback_required",
                "自动恢复遇到外部变化或文件问题；原文、候选与外部内容保留，需人工核对",
            )


def perform(sessions, settings, run_id):
    with sessions() as session:
        run = session.get(PatchRun, run_id)
        asset = session.get(PatchAsset, run.patch_id)
    try:
        backup(settings, run, asset)
        with locked(sessions) as session:
            # Persist applying before the first rename. A crash can never lose the operation's intent.
            row = session.get(PatchRun, run_id)
            sources(session, settings, run.project_id, asset)
            update(session, row, "applying", "正在应用已审阅的文件补丁")
        with Files(Path(asset.payload["root"]), [f["path"] for f in asset.payload["files"]]) as files:
            file_match(files, asset)
            completed = []
            for item in asset.payload["files"]:
                with locked(sessions) as session:
                    # Revocation and source changes serialize against each individual side effect.
                    sources(session, settings, run.project_id, asset)
                    file_match(files, asset, True)
                    files.write(item["path"], item["after"], item["mode"], item["before"])
                    completed.append(item["path"])
                    update(
                        session, session.get(PatchRun, run_id), "applying", "文件步骤已落盘", list(completed)
                    )
            with locked(sessions) as session:
                sources(session, settings, run.project_id, asset)
                for item in asset.payload["files"]:
                    if files.read(item["path"])[0] != item["after"]:
                        raise BusinessError("FILE_CONFLICT", "写入后出现外部变化，需核对", 409)
                update(
                    session,
                    session.get(PatchRun, run_id),
                    "applied",
                    "文件补丁已写入；仍需执行验证计划，不能据此宣布阶段通过",
                )
    except Exception:
        compensate(sessions, settings, run_id)


def apply(sessions, settings, pid, body, key):
    if not body.confirmed or not body.reason.strip() or execution.contains_secret(body.reason):
        raise BusinessError("CONFIRMATION_REQUIRED", "请审阅具体补丁并明确确认，不填写秘密")
    with sessions() as session:
        asset = owned(session, pid, body.patch_id)
        if digest(asset.payload) != body.expected_hash:
            raise BusinessError("REVISION_CONFLICT", "补丁指纹不匹配，不能应用", 409)
        root = Path(asset.payload["root"])
    with workspace_lock(settings, root):

        def prepare(session):
            previous = next((r for r in runs(session, pid) if r.patch_id == asset.id), None)
            if previous:
                return {"run_id": previous.id, "new": False}
            grant, _ = eligible(session, settings, pid, asset)
            run = PatchRun(
                id=str(uuid4()),
                project_id=pid,
                patch_id=asset.id,
                state="preparing",
                details={
                    "message": "已确认，正在准备耐久备份",
                    "completed": [],
                    "reason": body.reason,
                    "authorization_id": grant.id,
                },
            )
            session.add(run)
            session.flush()
            audit(
                session,
                pid,
                "patch_apply_confirmed",
                {"run_id": run.id, "patch_id": asset.id, "authorization_id": grant.id},
            )
            return {"run_id": run.id, "new": True}

        result = mutate(sessions, pid, "patch_apply", body.model_dump(), key, prepare)
        with sessions() as session:
            state = session.get(PatchRun, result["run_id"]).state
        if result["new"] and state == "preparing":
            perform(sessions, settings, result["run_id"])
        with sessions() as session:
            return run_public(session.get(PatchRun, result["run_id"]))


def recover_pending(sessions, settings):
    with sessions() as session:
        pending = [
            (r.id, session.get(PatchAsset, r.patch_id).payload["root"])
            for r in runs(session)
            if r.state in {"preparing", "applying"}
        ]
    for run_id, root in pending:
        try:
            with workspace_lock(settings, Path(root)):
                compensate(sessions, settings, run_id)
        except (BusinessError, OSError):
            with locked(sessions) as session:
                update(
                    session,
                    session.get(PatchRun, run_id),
                    "rollback_required",
                    "恢复范围暂不可核对，需人工处理；未重复应用",
                )


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/patches"

    @app.get(prefix, response_model=Workspace)
    def workspace(project_id: str):
        with sessions() as session:
            project(session, project_id)
            assets = session.scalars(
                select(PatchAsset)
                .where(PatchAsset.project_id == project_id)
                .order_by(PatchAsset.created_at.desc(), PatchAsset.id)
            )
            return {
                "assets": [public(session, settings, a) for a in assets],
                "runs": [run_public(r) for r in runs(session, project_id)],
            }

    @app.get(prefix + "/assets/{asset_id}", response_model=Asset)
    def get_asset(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/preview", response_model=Asset)
    def get_preview(project_id: str, body: Preview, idempotency_key: str | None = Header(default=None)):
        return mutate(
            sessions,
            project_id,
            "patch_preview",
            body.model_dump(),
            idempotency_key,
            lambda session: preview(session, settings, project_id, body),
        )

    @app.post(prefix + "/apply", response_model=Run)
    def apply_patch(project_id: str, body: Apply, idempotency_key: str | None = Header(default=None)):
        return apply(sessions, settings, project_id, body, idempotency_key)

    @app.post(prefix + "/recover", response_model=Run)
    def recover(project_id: str, body: Recover, idempotency_key: str | None = Header(default=None)):
        if not body.confirmed:
            raise BusinessError("CONFIRMATION_REQUIRED", "请先核对恢复说明")
        with sessions() as session:
            run = owned(session, project_id, body.run_id, PatchRun)
            asset = owned(session, project_id, run.patch_id)
        with workspace_lock(settings, Path(asset.payload["root"])):
            mutate(
                sessions,
                project_id,
                "patch_recover",
                body.model_dump(),
                idempotency_key,
                lambda session: {"run_id": body.run_id},
            )
            with sessions() as session:
                current = session.get(PatchRun, body.run_id)
                state = current.state
            if state in UNFINISHED:
                compensate(sessions, settings, body.run_id)
            with sessions() as session:
                return run_public(session.get(PatchRun, body.run_id))
