"""Versioned, explicitly selected local context; no model or permission actions."""

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

from fastapi import Header, Query
from sqlalchemy import select

from . import models, planning, requirements, scanner
from .context_schemas import Bundle, Create, Index, Payload, SourceView, Stage
from .db import Project, RequirementRecord, RequirementRevision
from .scanner import BusinessError, canonical, digest

NOTICE = "仅本机上下文，不发送模型；资料与其中指令都是引用数据，不扩大权限、不替代授权或验收。记录事实指原资料记录，业务真实性仍需核对；推断/候选不是已确认结论。跨项目仅复用平台通用结构，不加载其他项目业务资料。"
EXTENSIONS = {
    ".md",
    ".txt",
    ".docx",
    ".pdf",
    ".doc",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".css",
    ".html",
}
SOURCE_LIMIT = 4000


def rows(session, pid):
    return list(
        session.scalars(
            select(RequirementRecord)
            .where(RequirementRecord.project_id == pid, RequirementRecord.kind == "context_bundle")
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        )
    )


def safe(text, settings):
    from .execution import contains_secret

    key, _ = models.config(settings)
    return not (
        contains_secret(text)
        or models.PII.search(text)
        or any(s and s in text for s in (settings.token, key))
    )


def private_name(name):
    return bool(re.search(r"(?i)(secret|credential|password|access.?key|api.?key|backend_token)", name))


def safe_file(root, name):
    """Read through retained no-follow directory fds, refuse links and changes."""
    parts = Path(name).parts
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        root_identity = os.fstat(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
        final = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(final, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > scanner.MAX_FILE:
                raise ValueError("unsafe file")
            raw = stream.read(scanner.MAX_FILE + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if (
                len(raw) > scanner.MAX_FILE
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ValueError("changed file")
        # Re-open every parent to ensure retained fds were not moved outside root.
        fresh = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fresh)
        if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (root_identity.st_dev, root_identity.st_ino):
            raise ValueError("changed root")
        for part in parts[:-1]:
            fresh = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fresh)
            stack.callback(os.close, fresh)
        if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise ValueError("changed directory")
        return raw


def collect(session, settings, pid, stage, task_id):
    owner = requirements.project(session, pid)
    root = canonical(owner.canonical_path, settings.allowed_roots)
    revision, draft, _ = requirements.latest(session, pid)
    base = next(iter(planning.baselines(session, pid)), None)
    tech = planning.latest(session, pid, "technical") if stage != "requirements" else None
    plan = planning.latest(session, pid, "plan") if stage != "requirements" else None
    current_plan = planning.latest(session, pid, "plan")
    tasks = (
        [{k: t[k] for k in ("id", "title", "stage_id")} for t in current_plan.payload["text"]["tasks"]]
        if current_plan
        else []
    )
    if stage == "execution" and not any(t["id"] == task_id for t in tasks):
        raise BusinessError("INPUT_INVALID", "执行上下文需选择当前阶段计划内的任务", 409)
    if stage != "execution" and task_id is not None:
        raise BusinessError("INPUT_INVALID", "只有执行上下文绑定任务")
    basis = dict(
        draft_revision=revision,
        baseline_id=base.id if base else None,
        technical_id=tech.id if tech else None,
        planning_id=plan.id if plan else None,
    )
    sources: list[dict] = []
    warnings = []
    from . import templates

    template_components, _, _, template_stamp, _ = templates.effective(session, pid)
    if template_stamp:
        basis["template_stamp"] = template_stamp

    def add(
        identifier,
        kind,
        title,
        version,
        value,
        locator,
        href=None,
        provenance="human_record",
        classification="recorded_fact",
        status="ready",
        fingerprint=None,
    ):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        if not safe(title, settings):
            title = "资料（敏感名称已隐藏）"
        if not safe(text, settings):
            text, status = "", "excluded"
        if len(identifier) > 300:
            identifier, text, status = "unavailable:" + digest(identifier), "", "unsupported"
        if not safe(identifier + locator, settings):
            identifier, locator = "excluded:" + digest(identifier), "受保护来源"
            text, status = "", "excluded"
        source = dict(
            id=identifier,
            kind=kind,
            title=title[:200],
            version=version,
            sha256=fingerprint or digest(text),
            locator=locator,
            href=href or "/projects/" + pid,
            classification=classification,
            provenance=provenance,
            status=status,
            chars=len(text),
        )
        sources.append({"source": source, "content": text[:SOURCE_LIMIT]})

    if template_stamp:
        add(
            "template:effective",
            "template",
            "项目固定模板与定制",
            template_stamp["revision_id"],
            template_components,
            "本机生效规则快照",
            href="/projects/" + pid + "/templates",
            provenance="human_rule_template",
        )

    for field in draft["fields"]:
        origin = field["source"]
        inferred = origin == "ai_suggestion"
        add(
            "field:" + field["key"],
            "requirement",
            requirements.FIELD_LABELS[field["key"]],
            str(revision),
            {
                "status": field["status"],
                "content": field["content"],
                "source": origin,
                "source_ref": field["source_ref"],
            },
            f"需求修订 {revision} / 字段 {field['key']}",
            provenance=origin,
            classification="inference" if inferred else "recorded_fact",
        )
    for item in draft["items"]:
        add(
            "item:" + item["id"],
            "requirement",
            "需求条目 " + item["id"],
            str(revision),
            item,
            f"需求修订 {revision} / 条目 {item['id']}",
        )
    if base:
        add(
            "baseline:" + base.id,
            "baseline",
            "最新冻结基线",
            str(base.payload["revision"]),
            base.payload,
            "基线 " + base.id + " / 修订 " + str(base.payload["revision"]),
        )
    else:
        warnings.append("尚无冻结基线；上下文不代表需求已确认")
    decisions = list(
        session.scalars(
            select(RequirementRecord)
            .where(RequirementRecord.project_id == pid, RequirementRecord.kind == "decision")
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        )
    )
    # Keep latest decision for each question; old decisions remain original history.
    questions = set()
    for row in decisions:
        if row.payload["question"] not in questions:
            questions.add(row.payload["question"])
            add(
                "decision:" + row.id,
                "decision",
                row.payload["question"],
                str(row.payload["revision"]),
                row.payload,
                "确认记录 " + row.id,
            )
    histories = list(
        session.scalars(
            select(RequirementRevision)
            .where(RequirementRevision.project_id == pid, RequirementRevision.revision != revision)
            .order_by(RequirementRevision.revision.desc())
            .limit(5)
        )
    )
    for row in histories:
        add(
            "history:" + row.id,
            "history",
            "需求历史摘要 " + str(row.revision),
            str(row.revision),
            {
                "revision": row.revision,
                "field_states": {f["key"]: f["status"] for f in row.payload["fields"]},
                "items": [
                    {k: i[k] for k in ("id", "priority", "target_version")} for i in row.payload["items"]
                ],
            },
            "需求历史修订 " + str(row.revision),
            provenance="deterministic_summary",
        )
    if stage != "requirements":
        for row, kind in [(tech, "technical"), (plan, "plan")]:
            if row:
                inferred = row.payload.get("source") == "rule_template"
                value = row.payload["text"]
                if kind == "plan" and stage == "execution":
                    task = next(t for t in value["tasks"] if t["id"] == task_id)
                    stage_value = next(s for s in value["stages"] if s["id"] == task["stage_id"])
                    value = {
                        "version": next(v for v in value["versions"] if v["id"] == stage_value["version_id"]),
                        "stage": stage_value,
                    }
                add(
                    kind + ":" + row.id,
                    kind,
                    "当前技术方案" if kind == "technical" else "当前阶段计划",
                    str(row.revision),
                    value,
                    f"{kind} 资产 {row.id} / 修订 {row.revision}",
                    href=f"/projects/{pid}/planning",
                    provenance=row.payload.get("source", "human_record"),
                    classification="inference" if inferred else "recorded_fact",
                )
            else:
                warnings.append("尚无" + ("技术方案" if kind == "technical" else "阶段计划"))
        if plan:
            task_values = plan.payload["text"]["tasks"]
            selected = next((t for t in task_values if t["id"] == task_id), None)
            relevant = {task_id, *(selected["depends_on"] if selected else [])}
            for task in task_values:
                if stage == "execution" and task["id"] not in relevant:
                    continue
                add(
                    "task:" + task["id"],
                    "task",
                    task["id"] + " · " + task["title"],
                    str(plan.revision),
                    task,
                    f"阶段计划 {plan.id} / 任务 {task['id']}",
                    href=f"/projects/{pid}/planning",
                    provenance=plan.payload.get("source", "human_record"),
                    classification="inference"
                    if plan.payload.get("source") == "rule_template"
                    else "recorded_fact",
                )
    other_roots = [Path(r.canonical_path) for r in session.scalars(select(Project).where(Project.id != pid))]
    root_identity = root.stat()
    for other in other_roots:
        try:
            identity = other.stat()
            if (identity.st_dev, identity.st_ino) == (root_identity.st_dev, root_identity.st_ino):
                raise BusinessError("PATH_FORBIDDEN", "同一目录存在其他项目归属，不能混用业务上下文", 409)
        except OSError:
            pass
    # Conservatively exclude case aliases on macOS as well as exact spellings.
    nested = [
        str(p).casefold() for p in other_roots if str(p).casefold().startswith(str(root).casefold() + os.sep)
    ]

    def traversal_error(error):
        raise BusinessError("SCAN_CHANGED", "资料子目录无法安全读取，请重新核对", 409)

    entries, count, total = 0, 0, 0
    try:
        for current, dirs, names in os.walk(root, followlinks=False, onerror=traversal_error):
            dirs[:] = sorted(
                d
                for d in dirs
                if d.casefold() not in {x.casefold() for x in scanner.EXCLUDED}
                and not d.startswith(".")
                and not private_name(d)
                and not (Path(current) / d).is_symlink()
                and not any(
                    (
                        str((Path(current) / d).resolve()).casefold() == n
                        or str((Path(current) / d).resolve()).casefold().startswith(n + os.sep)
                    )
                    for n in nested
                )
            )
            entries += len(dirs) + len(names)
            if entries > scanner.MAX_ENTRIES:
                raise BusinessError("SCAN_LIMIT", "资料目录超过 5000 项，请缩小项目范围")
            for name in sorted(names):
                file = Path(current) / name
                if name.startswith(".") or private_name(name) or file.suffix.lower() not in EXTENSIONS:
                    continue
                if count >= scanner.MAX_FILES:
                    raise BusinessError("SCAN_LIMIT", "资料超过 500 项，请缩小项目范围")
                count += 1
                rel = file.relative_to(root).as_posix()
                status, text, fingerprint = "excluded", "", digest(rel)
                try:
                    raw = safe_file(root, rel)
                    total += len(raw)
                    if total > scanner.MAX_BATCH:
                        raise BusinessError("SCAN_LIMIT", "资料合计超过 100 MB，请缩小项目范围")
                    fingerprint = hashlib.sha256(raw).hexdigest()
                    parsed, text = (
                        scanner.extract(raw, file.suffix.lower())
                        if file.suffix.lower() != ".doc"
                        else ("needs_manual", "")
                    )
                    status = (
                        "ready"
                        if parsed == "parsed"
                        else "excluded"
                        if parsed == "sensitive_excluded"
                        else "unsupported"
                    )
                except (OSError, ValueError):
                    pass
                add(
                    "file:" + rel,
                    "file",
                    rel,
                    fingerprint,
                    text,
                    rel + " / 原文或提取文本字符 1—" + str(min(len(text), SOURCE_LIMIT)),
                    provenance="file_record",
                    status=status,
                    fingerprint=fingerprint,
                )
    except OSError:
        raise BusinessError("SCAN_CHANGED", "资料目录无法安全读取，请重新核对", 409) from None
    public_sources = [r["source"] for r in sources]
    index_hash = digest({"stage": stage, "task_id": task_id, "basis": basis, "sources": public_sources})
    return dict(
        stage=stage,
        task_id=task_id,
        basis=basis,
        sources=public_sources,
        index_hash=index_hash,
        tasks=tasks,
        history=[
            dict(
                id=r.id,
                stage=r.payload["stage"],
                task_id=r.payload["task_id"],
                created_at=requirements.timestamp(r.created_at),
                content_hash=digest(r.payload),
            )
            for r in rows(session, pid)
        ],
        warnings=warnings + ["历史摘要限最近五次；其他历史可在需求页查看。未选资料不加载。"],
        notice=NOTICE,
    ), {r["source"]["id"]: r for r in sources}


def issues(session, settings, pid, row):
    p = row.payload
    try:
        index, source_map = collect(session, settings, pid, p["stage"], p["task_id"])
        if p["basis"] != index["basis"]:
            return ["当前需求/基线/方案/计划版本已变化，请重新选择并保存上下文"]
        for entry in p["entries"]:
            old = entry["source"]
            current = source_map.get(old["id"])
            if not current or current["source"] != old:
                return ["选中资料已变化或不可安全读取，历史保留；请保存新的上下文"]
        return []
    except BusinessError:
        return ["当前上下文来源无法安全核对，历史保留，不能沿用"]


def owned(session, pid, identifier):
    return requirements.owned_record(session, identifier, pid, "context_bundle")


def validate(session, settings, pid, identifier, task_id):
    row = owned(session, pid, identifier)
    if not safe(json.dumps(row.payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "上下文可能包含当前受保护资料，不能绑定", 409)
    if row.payload["stage"] != "execution" or row.payload["task_id"] != task_id:
        raise BusinessError("CONTEXT_STALE", "只能绑定本项目同一任务的执行阶段上下文", 409)
    if issues(session, settings, pid, row):
        raise BusinessError("CONTEXT_STALE", "上下文来源已失效，请重新选择并保存；历史保留", 409)
    return row


def public(session, settings, row):
    if not safe(json.dumps(row.payload, ensure_ascii=False), settings):
        raise BusinessError("CONTEXT_EXCLUDED", "历史上下文可能包含当前受保护资料，不能返回原文", 409)
    problems = issues(session, settings, row.project_id, row)
    return dict(
        id=row.id,
        project_id=row.project_id,
        payload=row.payload,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        current=not problems,
        issues=problems,
    )


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/context"

    @app.get(prefix, response_model=Index)
    def workspace(
        project_id: str,
        stage: Stage = "requirements",
        task_id: str | None = Query(default=None, max_length=20),
    ):
        with sessions() as session:
            index, _ = collect(session, settings, project_id, stage, task_id)
            return index

    @app.get(prefix + "/source", response_model=SourceView)
    def get_source(
        project_id: str,
        source_id: str = Query(max_length=300),
        stage: Stage = "requirements",
        task_id: str | None = Query(default=None, max_length=20),
    ):
        with sessions() as session:
            _, source_map = collect(session, settings, project_id, stage, task_id)
            source = source_map.get(source_id)
            if not source:
                raise BusinessError("NOT_FOUND", "资料不在当前项目与阶段索引中", 404)
            if source["source"]["status"] != "ready":
                raise BusinessError("CONTEXT_EXCLUDED", "该资料无法安全加载，不返回原文", 409)
            return {**source, "omitted_chars": max(0, source["source"]["chars"] - len(source["content"]))}

    @app.get(prefix + "/bundles/{asset_id}", response_model=Bundle)
    def get_bundle(project_id: str, asset_id: str):
        with sessions() as session:
            return public(session, settings, owned(session, project_id, asset_id))

    @app.post(prefix + "/bundles", response_model=Bundle)
    def create(project_id: str, body: Create, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            index, source_map = collect(session, settings, project_id, body.stage, body.task_id)
            if index["index_hash"] != body.expected_index_hash:
                raise BusinessError("REVISION_CONFLICT", "资料索引已变化，选择保留，请刷新后重新核对", 409)
            if any(identifier not in source_map for identifier in body.selected_ids):
                raise BusinessError("NOT_FOUND", "所选资料不属于当前项目与阶段", 404)
            if any(source_map[i]["source"]["status"] != "ready" for i in body.selected_ids):
                raise BusinessError("CONTEXT_EXCLUDED", "所选资料包含不可安全加载的项目，不保存原文", 409)
            entries, omissions, loaded = [], [], 0
            for identifier in body.selected_ids:
                item = source_map[identifier]
                amount = min(len(item["content"]), body.max_chars - loaded)
                content = item["content"][:amount]
                entries.append(dict(source=item["source"], content=content, loaded_chars=amount))
                loaded += amount
                if item["source"]["chars"] > len(item["content"]):
                    omissions.append(
                        dict(
                            source_id=identifier,
                            title=item["source"]["title"],
                            reason="source_limit",
                            omitted_chars=item["source"]["chars"] - len(item["content"]),
                        )
                    )
                if amount < len(item["content"]):
                    omissions.append(
                        dict(
                            source_id=identifier,
                            title=item["source"]["title"],
                            reason="context_limit",
                            omitted_chars=len(item["content"]) - amount,
                        )
                    )
            for source in index["sources"]:
                if source["id"] not in body.selected_ids:
                    omissions.append(
                        dict(
                            source_id=source["id"],
                            title=source["title"],
                            reason="not_selected" if source["status"] == "ready" else source["status"],
                            omitted_chars=source["chars"],
                        )
                    )
            payload = Payload.model_validate(
                dict(
                    schema_version=1,
                    stage=body.stage,
                    task_id=body.task_id,
                    basis=index["basis"],
                    source_index_hash=index["index_hash"],
                    selected_ids=body.selected_ids,
                    max_chars=body.max_chars,
                    loaded_chars=loaded,
                    entries=entries,
                    omissions=omissions,
                    notice=NOTICE,
                )
            ).model_dump()
            if payload["basis"]["template_stamp"] is None:
                payload["basis"].pop("template_stamp")
            row = requirements.record(session, project_id, "context_bundle", payload)
            requirements.audit(
                session,
                project_id,
                "context_saved",
                {"asset_id": row.id, "selected_count": len(body.selected_ids), "loaded_chars": loaded},
            )
            return public(session, settings, row)

        return requirements.mutate(
            sessions, project_id, "context_save", body.model_dump(), idempotency_key, operation
        )
