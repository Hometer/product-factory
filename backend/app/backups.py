"""Bounded portable data, inert historical restores, and transactional archiving."""

import hashlib
import json
import os
import stat
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Header
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select

from . import contexts, requirements, scanner, templates, version_files
from .backup_schemas import Archive, Asset, Envelope, Export, Inspect, Restore, Snapshot, Workspace
from .core import ROOT
from .db import Base, Project, RequirementRecord
from .scanner import BusinessError, canonical, digest

KINDS = ("backup_export", "backup_preview", "backup_restore", "project_archive", "restore_source")
MAX_PACKAGE = 8 * 1024 * 1024
CODE = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".json"}
TEXT = CODE | {".md", ".txt"}
TABLES = {n: t for n, t in Base.metadata.tables.items() if "project_id" in t.c}
NOTICE = "备份保留安全业务历史及清单；二进制、大文件、秘密明确未包含。恢复到全新目录，旧历史只读、原授权和验收不生效、费用不重放；请重新配置秘密并核对需求。归档不删除，不自动恢复运行。"


def latest(session, pid, kind):
    return session.scalar(
        select(RequirementRecord)
        .where(RequirementRecord.project_id == pid, RequirementRecord.kind == kind)
        .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        .limit(1)
    )


def archived(session, pid):
    r = latest(session, pid, "project_archive")
    return bool(r and r.payload["archived"])


def public(row):
    return dict(
        id=row.id,
        kind=row.kind,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        payload=metadata(row),
    )


def metadata(row):
    result = {k: v for k, v in row.payload.items() if k not in {"package", "snapshot"}}
    if row.kind == "restore_source":
        result["files"] = [
            {k: v for k, v in f.items() if k != "content"} for f in row.payload["snapshot"]["files"]
        ]
        result["git"] = row.payload["snapshot"]["git"]
        result["include_code"] = row.payload["snapshot"]["include_code"]
    return result


def append(session, pid, kind, payload):
    row = requirements.record(session, pid, kind, dict(schema_version=1, **payload))
    requirements.audit(session, pid, kind, {"asset_id": row.id, "reason": payload.get("reason", "")})
    return public(row)


def name_check(name):
    path = Path(name)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != name
        or "\\" in name
        or len(name) > 240
        or any(
            p.startswith(".")
            or contexts.private_name(p)
            or p.casefold() in {x.casefold() for x in scanner.EXCLUDED}
            for p in path.parts
        )
    ):
        raise BusinessError("BACKUP_PATH", "备份包含不安全或受保护路径", 409)
    return path


def inventory(session, settings, pid, include_code):
    root = canonical(requirements.project(session, pid).canonical_path, settings.allowed_roots)
    files, omitted, code_names = [], [], []
    seen = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        seen += len(dirs)
        if seen > 500:
            raise BusinessError("BACKUP_LIMIT", "目录或文件清单超过 500 项，请缩小项目范围", 409)
        safe_dirs = []
        for name in sorted(dirs):
            if name.startswith(".") or contexts.private_name(name) or name in scanner.EXCLUDED:
                omitted.append(dict(kind="directory", reason="受保护区域未包含"))
            elif (Path(current) / name).is_symlink():
                omitted.append(dict(kind="directory", reason="链接区域未包含"))
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(names):
            seen += 1
            if seen > 500:
                raise BusinessError("BACKUP_LIMIT", "文件清单超过 500 项，请缩小项目范围", 409)
            p = Path(current) / name
            rel = p.relative_to(root).as_posix()
            try:
                name_check(rel)
                if not contexts.safe(rel, settings):
                    raise ValueError("private name")
            except (BusinessError, ValueError):
                omitted.append(dict(kind="file", reason="敏感或受保护路径未包含"))
                continue
            meta = p.lstat()
            item = dict(
                path=rel,
                size=meta.st_size,
                sha256=None,
                included=False,
                reason="二进制、格式不可安全核验或大文件未包含",
                content=None,
            )
            if stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_size <= 512 * 1024:
                try:
                    raw = contexts.safe_file(root, rel)
                    text = raw.decode("utf-8")
                    if p.suffix.lower() not in TEXT or "\x00" in text:
                        raise ValueError("type")
                    if not contexts.safe(text, settings):
                        item["reason"] = "疑似秘密或敏感内容未包含"
                    else:
                        item["sha256"] = hashlib.sha256(raw).hexdigest()
                        if p.suffix.lower() in CODE:
                            code_names.append(rel)
                        item["included"] = p.suffix.lower() not in CODE or include_code
                        item["reason"] = "安全文本已包含" if item["included"] else "代码仅摘要引用"
                        item["content"] = text if item["included"] else None
                except (OSError, ValueError, UnicodeError):
                    pass
            elif p.is_symlink():
                item["reason"] = "符号链接未包含"
            files.append(item)
    return files, omitted, version_files.git_view(root, code_names, settings)


def serialize(row):
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}


def snapshot(session, settings, pid, include_code):
    owner = requirements.project(session, pid)
    files, exclusions, git = inventory(session, settings, pid, include_code)
    tables = {}
    for name, table in TABLES.items():
        rows = []
        for raw in session.execute(select(table).where(table.c.project_id == pid)).mappings():
            row = serialize(raw)
            if name == "requirement_records" and row["kind"] == "restore_source":
                continue  # Added once below with its inert source history.
            if name == "requirement_records" and row["kind"] in {"backup_export", "backup_preview"}:
                row["payload"] = {k: v for k, v in row["payload"].items() if k != "package"}
            if name == "requirement_records" and "zip_base64" in row["payload"]:
                row["payload"] = {k: v for k, v in row["payload"].items() if k != "zip_base64"}
                exclusions.append(
                    dict(
                        kind="field", table=name, id=row["id"], reason="旧二进制交付包仅保留清单，内容未包含"
                    )
                )
            if not contexts.safe(json.dumps(row, ensure_ascii=False), settings):
                exclusions.append(dict(kind="record", table=name, id=row.get("id"), reason="敏感历史未包含"))
            else:
                rows.append(row)
        tables[name] = sorted(rows, key=lambda r: str(r.get("id", r.get("run_id", ""))))
    value = dict(
        format="product-factory-backup",
        version=1,
        database_version="0008",
        source_id=pid,
        project=owner.payload,
        created_at=requirements.timestamp(),
        include_code=include_code,
        git=git,
        tables=tables,
        files=files,
        exclusions=exclusions,
    )
    source = latest(session, pid, "restore_source")
    if source:
        # Retain source history without making it live in any execution table.
        value["tables"]["requirement_records"].append(
            serialize(
                {
                    "id": source.id,
                    "project_id": pid,
                    "kind": "restore_source",
                    "payload": source.payload,
                    "created_at": source.created_at,
                }
            )
        )
    try:
        return Snapshot.model_validate(value).model_dump()
    except ValidationError:
        raise BusinessError("BACKUP_LIMIT", "项目历史超出便携备份限制，不能冒充完整备份", 409) from None


def basis(session, settings, pid):
    v = snapshot(session, settings, pid, False)
    v.pop("created_at")
    return digest(dict(snapshot=v, archived=archived(session, pid)))


def expected(session, settings, pid, h):
    if basis(session, settings, pid) != h:
        raise BusinessError("REVISION_CONFLICT", "资料或归档状态已变化，输入保留，请重新核对", 409)


def parse(content, settings):
    try:
        if len(content.encode()) > MAX_PACKAGE:
            raise ValueError("size")

        def unique(pairs):
            obj = {}
            for k, v in pairs:
                if k in obj:
                    raise ValueError("duplicate")
                obj[k] = v
            return obj

        raw = json.loads(
            content, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
        value = Envelope.model_validate(raw).model_dump()
    except (ValueError, ValidationError, UnicodeError, RecursionError):
        raise BusinessError("BACKUP_INVALID", "备份不是有效限量 JSON 或结构不符合要求", 409) from None
    s = value["snapshot"]
    if s["format"] != "product-factory-backup" or s["version"] != 1 or s["database_version"] != "0008":
        raise BusinessError("BACKUP_VERSION", "备份版本不兼容，不能自动恢复", 409)
    if digest(s) != value["sha256"]:
        raise BusinessError("BACKUP_INTEGRITY", "备份摘要不一致，原项目保持", 409)
    if set(s["tables"]) != set(TABLES) or not contexts.safe(content, settings):
        raise BusinessError("BACKUP_UNSAFE", "业务表范围或隐私核对未通过", 409)
    for name, rows in s["tables"].items():
        seen_ids = set()
        for row in rows:
            identifier = row.get("id", row.get("run_id"))
            if (
                set(row) != {c.name for c in TABLES[name].c}
                or row.get("project_id") != s["source_id"]
                or not isinstance(identifier, str)
                or identifier in seen_ids
            ):
                raise BusinessError("BACKUP_INVALID", "历史记录结构、归属或身份重复", 409)
            seen_ids.add(identifier)
    names: set[str] = set()
    for f in s["files"]:
        name_check(f["path"])
        key = f["path"].casefold()
        if key in names or any(key.startswith(n + "/") or n.startswith(key + "/") for n in names):
            raise BusinessError("BACKUP_PATH", "备份路径重复或存在文件目录冲突", 409)
        names.add(key)
        if f["included"]:
            if f["content"] is None or Path(f["path"]).suffix.lower() not in TEXT:
                raise BusinessError("BACKUP_INVALID", "恢复仅接受安全文本内容", 409)
            raw = f["content"].encode()
            if (
                len(raw) > 512 * 1024
                or len(raw) != f["size"]
                or hashlib.sha256(raw).hexdigest() != f["sha256"]
                or "\x00" in f["content"]
            ):
                raise BusinessError("BACKUP_INTEGRITY", "文件摘要或大小不一致", 409)
        elif f["content"] is not None:
            raise BusinessError("BACKUP_INVALID", "排除项不能携带内容", 409)
    return value


def target_check(session, settings, raw):
    target = canonical(raw, settings.allowed_roots)
    if str(target) != raw or target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise BusinessError("DIRECTORY_CONFLICT", "恢复需要真实父目录中的全新目录，不能替换或跟随链接", 409)
    parent = target.parent
    while parent != parent.parent:
        if parent.is_symlink():
            raise BusinessError("DIRECTORY_CONFLICT", "恢复父目录不能包含链接", 409)
        parent = parent.parent
    if target == settings.data_dir or settings.data_dir.is_relative_to(target):
        raise BusinessError("PATH_FORBIDDEN", "不能覆盖工厂资料目录", 409)
    for p in session.scalars(select(Project)):
        root = Path(p.canonical_path)
        # The factory itself may be registered read-only. Its ignored data directory
        # is a managed container, excluded from its business scan and patch scope.
        if root == ROOT and target.is_relative_to(settings.data_dir):
            continue
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            raise BusinessError("DIRECTORY_CONFLICT", "目标与已登记项目有重叠", 409)
    return target


def workspace(session, settings, pid):
    requirements.project(session, pid)
    r = latest(session, pid, "restore_source")
    return dict(
        project_id=pid,
        archived=archived(session, pid),
        state_hash=basis(session, settings, pid),
        history=[
            public(x)
            for x in session.scalars(
                select(RequirementRecord)
                .where(RequirementRecord.project_id == pid, RequirementRecord.kind.in_(KINDS))
                .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
            )
        ],
        source=metadata(r) if r else None,
        notice=NOTICE,
    )


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/backups"

    @app.get(prefix, response_model=Workspace)
    def get(project_id: str):
        with sessions() as s:
            return workspace(s, settings, project_id)

    @app.get(prefix + "/source", response_model=Snapshot)
    def source(project_id: str):
        with sessions() as s:
            requirements.project(s, project_id)
            row = latest(s, project_id, "restore_source")
            if not row:
                raise BusinessError("NOT_FOUND", "没有恢复来源历史", 404)
            return row.payload["snapshot"]

    def mutate(pid, kind, body, key, action):
        def checked(s):
            if not contexts.safe(body.reason, settings):
                raise BusinessError("BACKUP_UNSAFE", "理由不能包含秘密或敏感信息", 409)
            return action(s)

        return requirements.mutate(sessions, pid, kind, body.model_dump(), key, checked)

    @app.post(prefix + "/exports", response_model=Asset)
    def export(project_id: str, body: Export, idempotency_key: str | None = Header(default=None)):
        def action(s):
            expected(s, settings, project_id, body.expected_state_hash)
            value = snapshot(s, settings, project_id, body.include_code)
            package = dict(snapshot=value, sha256=digest(value))
            content = json.dumps(package, ensure_ascii=False)
            parse(content, settings)
            return append(
                s,
                project_id,
                "backup_export",
                dict(
                    package=package,
                    reason=body.reason,
                    source_id=project_id,
                    sha256=package["sha256"],
                    size=len(content.encode()),
                    files=[{k: v for k, v in f.items() if k != "content"} for f in value["files"]],
                    counts={k: len(v) for k, v in value["tables"].items()},
                    exclusions=value["exclusions"],
                    include_code=body.include_code,
                    git=value["git"],
                ),
            )

        return mutate(project_id, "backup_export", body, idempotency_key, action)

    @app.get(prefix + "/exports/{asset_id}/download")
    def download(project_id: str, asset_id: str):
        with sessions() as s:
            row = requirements.owned_record(s, asset_id, project_id, "backup_export")
            content = json.dumps(row.payload["package"], ensure_ascii=False)
            parse(content, settings)
            return Response(
                content,
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="backup-{row.id}.pfbackup.json"'},
            )

    @app.post(prefix + "/previews", response_model=Asset)
    def preview(project_id: str, body: Inspect, idempotency_key: str | None = Header(default=None)):
        def action(s):
            package = parse(body.content, settings)
            target = target_check(s, settings, body.target_path)
            value = package["snapshot"]
            return append(
                s,
                project_id,
                "backup_preview",
                dict(
                    package=package,
                    target_path=str(target),
                    reason=body.reason,
                    source_id=value["source_id"],
                    sha256=package["sha256"],
                    added=[f["path"] for f in value["files"] if f["included"]],
                    replaced=[],
                    omitted=[
                        {k: v for k, v in f.items() if k != "content"}
                        for f in value["files"]
                        if not f["included"]
                    ],
                    counts={k: len(v) for k, v in value["tables"].items()},
                    exclusions=value["exclusions"],
                    checks=["结构与版本", "包与文件摘要", "安全目录与零替换", "秘密核对"],
                    notice=NOTICE,
                ),
            )

        return mutate(project_id, "backup_preview", body, idempotency_key, action)

    @app.post(prefix + "/restores", response_model=Asset)
    def restore(project_id: str, body: Restore, idempotency_key: str | None = Header(default=None)):
        target, written = None, {}
        created_identity = None

        def action(s):
            nonlocal target, created_identity
            p = requirements.owned_record(s, body.asset_id, project_id, "backup_preview")
            if digest(p.payload) != body.expected_hash:
                raise BusinessError("REVISION_CONFLICT", "恢复预检版本不一致", 409)
            package = parse(json.dumps(p.payload["package"], ensure_ascii=False), settings)
            path = target_check(s, settings, p.payload["target_path"])
            value = package["snapshot"]
            contents = {f["path"]: f["content"].encode() for f in value["files"] if f["included"]}
            with tempfile.TemporaryDirectory(prefix=".factory-restore-", dir=path.parent) as staging:
                for name, raw in contents.items():
                    f = Path(staging) / name
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_bytes(raw)
                    f.chmod(0o600)
                path.mkdir(mode=0o700)
                target = path
                identity = path.stat()
                created_identity = (identity.st_dev, identity.st_ino)
                for name, raw in contents.items():
                    with ExitStack() as stack:
                        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        stack.callback(os.close, fd)
                        for part in Path(name).parts[:-1]:
                            try:
                                os.mkdir(part, mode=0o700, dir_fd=fd)
                            except FileExistsError:
                                pass
                            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            stack.callback(os.close, fd)
                        if path.is_symlink() or (path.stat().st_dev, path.stat().st_ino) != (
                            identity.st_dev,
                            identity.st_ino,
                        ):
                            raise BusinessError("DIRECTORY_CONFLICT", "恢复目录发生变化", 409)
                        os.link(Path(staging) / name, Path(name).name, dst_dir_fd=fd, follow_symlinks=False)
                        written[name] = raw
                        fresh = os.open((path / name).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        stack.callback(os.close, fresh)
                        if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (
                            os.fstat(fd).st_dev,
                            os.fstat(fd).st_ino,
                        ):
                            raise BusinessError("DIRECTORY_CONFLICT", "恢复文件父目录发生变化", 409)
            pid = str(uuid4())
            payload = {**value["project"], "path": str(path)}
            s.add(Project(id=pid, canonical_path=str(path), payload=payload))
            s.flush()
            templates.pin_initial(s, pid)
            source = dict(
                source_id=value["source_id"],
                sha256=package["sha256"],
                snapshot=value,
                counts=p.payload["counts"],
                files=p.payload["added"],
                reason=body.reason,
                notice=NOTICE,
                historical_only=True,
            )
            append(s, pid, "restore_source", source)
            return append(
                s,
                project_id,
                "backup_restore",
                dict(
                    preview_id=p.id,
                    restored_project_id=pid,
                    source_id=value["source_id"],
                    sha256=package["sha256"],
                    target_path=str(path),
                    reason=body.reason,
                ),
            )

        try:
            return mutate(project_id, "backup_restore", body, idempotency_key, action)
        except Exception:
            if target is not None:
                from .main import rollback_created

                try:
                    current = target.lstat()
                    if (
                        stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino) == created_identity
                        and all(
                            (target / name).parent.resolve() == (target / name).parent for name in written
                        )
                    ):
                        rollback_created(target, written)
                except OSError:
                    pass  # Leave an externally changed/unavailable directory for explicit inspection.
            raise

    @app.post(prefix + "/archives", response_model=Asset)
    def archive(project_id: str, body: Archive, idempotency_key: str | None = Header(default=None)):
        def action(s):
            expected(s, settings, project_id, body.expected_state_hash)
            if body.archived:
                try:
                    templates.busy(s, project_id)
                except BusinessError:
                    raise BusinessError(
                        "ARCHIVE_BUSY", "活动、暂停或结果未知运行需先核对，不能归档", 409
                    ) from None
            return append(s, project_id, "project_archive", dict(archived=body.archived, reason=body.reason))

        return mutate(project_id, "project_archive", body, idempotency_key, action)
