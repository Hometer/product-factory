"""Transactional, bounded ZIP deliveries. No deployment, extraction or code execution."""

import base64
import hashlib
import io
import json
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from fastapi import Header
from fastapi.responses import Response
from sqlalchemy import select

from . import acceptance, artifacts, contexts, planning, requirements, version_files
from .db import RequirementRecord
from .delivery_schemas import Asset, Package, Preview, Release, Workspace
from .scanner import BusinessError, digest

KINDS = ("delivery_preview", "delivery_package", "release_record")
CHECK_LABELS = dict(
    isolation="认证隔离",
    persistence="生产持久化",
    secrets="秘密管理",
    errors="错误处理",
    monitoring="监控",
    recovery="备份恢复",
)
MAX_ZIP = 1024 * 1024
DOCS = {
    "manifest.json",
    "requirements.json",
    "acceptance.json",
    "startup.md",
    "config.example.json",
    "limitations.md",
    "backup-recovery.md",
}
CODE = {".py", ".ts", ".tsx", ".js", ".jsx", ".html"}


def rows(session, pid, kind=None):
    return list(
        session.scalars(
            select(RequirementRecord)
            .where(
                RequirementRecord.project_id == pid,
                RequirementRecord.kind.in_([kind] if kind else KINDS),
            )
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
        )
    )


def owned(session, pid, identifier, kind):
    row = session.get(RequirementRecord, identifier)
    if not row or row.project_id != pid or row.kind != kind:
        raise BusinessError("NOT_FOUND", "同项目交付记录不存在", 404)
    return row


def effective(session, settings, pid, sid):
    """Current deterministic gate, including dependencies and unresolved defects."""
    package = acceptance.latest(session, pid, sid)
    decision = acceptance.latest(session, pid, sid, "acceptance_decision")
    blockers = []
    if not package or not decision:
        blockers.append(sid + " 缺少有效验收包或负责人结论")
    else:
        pv = acceptance.public(session, settings, package)
        dv = acceptance.public(session, settings, decision)
        if (
            not pv["current"]
            or not dv["current"]
            or pv["blockers"]
            or decision.payload["outcome"] != "passed"
            or decision.payload["package_id"] != package.id
        ):
            blockers.append(sid + " 验收未通过、待验或需复验")
    return dict(
        stage_id=sid,
        package_id=package.id if package else None,
        package_hash=digest(package.payload) if package else None,
        decision_id=decision.id if decision else None,
        decision_hash=digest(decision.payload) if decision else None,
        effective=not blockers,
        blockers=blockers,
    )


def basis(session, settings, pid, sid):
    requirements.project(session, pid)
    plan = planning.latest(session, pid, "plan")
    if not plan:
        raise BusinessError("PLAN_REQUIRED", "请先保存阶段计划", 409)
    stage, _, tasks = acceptance.stage_scope(plan, sid)
    base = requirements.owned_record(session, plan.baseline_id, pid, "baseline")
    _, _, _, index_hash = artifacts.catalog(session, settings, pid)
    revision, _, _ = requirements.latest(session, pid)
    return (
        dict(
            index_hash=index_hash,
            planning_id=plan.id,
            planning_hash=digest(plan.payload),
            draft_revision=revision,
            baseline_id=base.id,
            baseline_hash=digest(base.payload),
            stage=stage,
            acceptance=effective(session, settings, pid, sid),
        ),
        base,
        tasks,
    )


def blockers(payload):
    reasons = list(payload["basis"]["acceptance"]["blockers"])
    if not any(Path(f["path"]).suffix.lower() in CODE for f in payload["files"]):
        reasons.append("未选择可运行代码；本包仅文档，部分交付")
    return reasons


def public(session, settings, row):
    p = {k: v for k, v in row.payload.items() if k != "zip_base64"}
    # Text snapshots stay private; the UI only needs the explicit immutable manifest.
    if "files" in p:
        p["files"] = [{k: v for k, v in f.items() if k != "content"} for f in p["files"]]
    artifacts.safe(p, settings)
    root = (
        owned(session, row.project_id, p["package_id"], "delivery_package")
        if row.kind == "release_record"
        else row
    )
    issues = []
    try:
        current, _, _ = basis(session, settings, row.project_id, root.payload["basis"]["stage"]["id"])
        if digest(current) != digest(root.payload["basis"]):
            issues.append("需求、文件或验收来源已变化；历史包保持，需重新核对")
        b = list(current["acceptance"]["blockers"])
    except BusinessError:
        issues.append("当前来源不可核对，历史保留")
        b = ["当前来源不可核对"]
    if not any(Path(f["path"]).suffix.lower() in CODE for f in root.payload["files"]):
        b.append("未选择可运行代码；仅文档交付")
    if row.kind == "release_record":
        previous = next(
            (
                r
                for r in rows(session, row.project_id, "release_record")
                if r.payload["package_id"] == root.id
            ),
            None,
        )
        if previous and previous.id != row.id:
            issues.append("已有新的人工部署记录；原报告保持")
        for key, check in p["checks"].items():
            if check["status"] != "passed":
                b.append(CHECK_LABELS[key] + " 生产检查未通过或待验")
        if p["outcome"] == "reported_release":
            b.extend(production_blockers(session, settings, row.project_id, root))
            if b:
                issues.append("原人工发布报告的条件已不满足，需复验")
    return dict(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=p,
        content_hash=digest(row.payload),
        created_at=requirements.timestamp(row.created_at),
        current=not issues,
        issues=issues,
        blockers=list(dict.fromkeys(b)),
    )


def append(session, settings, pid, kind, payload):
    row = requirements.record(
        session, pid, kind, dict(schema_version=1, actor="local_operator", role="product_owner", **payload)
    )
    requirements.audit(session, pid, kind + "_saved", {"asset_id": row.id})
    return public(session, settings, row)


def production_blockers(session, settings, pid, package):
    plan = planning.latest(session, pid, "plan")
    if not plan:
        return ["缺少当前完整计划"]
    result = []
    for stage in plan.payload["text"]["stages"]:
        result.extend(effective(session, settings, pid, stage["id"])["blockers"])
    result.extend(blockers(package.payload))
    return list(dict.fromkeys(result))


def documents(payload):
    def js(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)

    return {
        "manifest.json": js(
            dict(
                schema_version=1,
                basis=payload["basis"],
                files=[{k: v for k, v in f.items() if k != "content"} for f in payload["files"]],
                status="partial_delivery" if blockers(payload) else "local_ready",
                blockers=blockers(payload),
                excluded="未选文件、秘密、原始数据、依赖、二进制均不包含",
            )
        ),
        "requirements.json": js(payload["requirements"]),
        "acceptance.json": js(payload["basis"]["acceptance"]),
        "startup.md": "# 启动步骤（人工提供，平台未运行）\n\n"
        + "\n".join(f"{i}. {step}" for i, step in enumerate(payload["startup_steps"], 1)),
        "config.example.json": js(
            dict(
                environment=[dict(name=n, value=None) for n in payload["config_names"]],
                instructions="秘密在目标环境重新配置，禁止写入交付包",
            )
        ),
        "limitations.md": payload["limitations"]
        + "\n\n仅所选安全文本；未包含二进制与依赖。"
        + "\n"
        + "\n".join(blockers(payload)),
        "backup-recovery.md": payload["backup_recovery"]
        + "\n\n平台不自动恢复、覆盖原项目或重放运行；恢复前核对清单、版本和冲突，秘密重新配置。",
    }


def pack(payload, settings):
    entries = {"files/" + f["path"]: f["content"] for f in payload["files"]}
    entries.update({"delivery/" + name: content for name, content in documents(payload).items()})
    if sum(len(v.encode()) for v in entries.values()) > 800 * 1024:
        raise BusinessError("DELIVERY_LIMIT", "包内文本合计不能超过800KiB", 409)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            if not contexts.safe(content, settings):
                raise BusinessError("SENSITIVE_CONTENT", "所选资料或生成文档含秘密/隐私，不能打包", 409)
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, content.encode("utf-8"))
    raw = buffer.getvalue()
    if len(raw) > MAX_ZIP:
        raise BusinessError("DELIVERY_LIMIT", "交付包不能超过1MiB", 409)
    return raw


def zip_bytes(row, settings):
    """No trusting persisted compressed data: bound, hash, decode and scan every member."""
    p = row.payload
    try:
        if len(p["zip_base64"]) > 4 * MAX_ZIP // 3 + 4:
            raise ValueError("large")
        raw = base64.b64decode(p["zip_base64"], validate=True)
        if len(raw) != p["zip_size"] or hashlib.sha256(raw).hexdigest() != p["zip_hash"]:
            raise ValueError("hash")
        expected = {"files/" + f["path"]: f["sha256"] for f in p["files"]}
        expected.update(
            {"delivery/" + n: hashlib.sha256(t.encode()).hexdigest() for n, t in documents(p).items()}
        )
        with ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(expected) or set(names) != set(expected):
                raise ValueError("members")
            if sum(i.file_size for i in archive.infolist()) > 800 * 1024:
                raise ValueError("uncompressed size")
            for name in names:
                member = archive.read(name)
                if hashlib.sha256(member).hexdigest() != expected[name] or not contexts.safe(
                    member.decode("utf-8"), settings
                ):
                    raise ValueError("unsafe")
        return raw
    except (KeyError, TypeError, ValueError, BadZipFile, OSError, UnicodeError, RuntimeError, zlib.error):
        raise BusinessError("DELIVERY_UNAVAILABLE", "交付包完整性或安全检查失败，请另建新包", 409) from None


def install(app, sessions, settings):
    prefix = "/api/v1/factory/projects/{project_id}/delivery"

    @app.get(prefix, response_model=Workspace)
    def get(project_id: str):
        with sessions() as session:
            requirements.project(session, project_id)
            data = artifacts.catalog(session, settings, project_id)
            plan = planning.latest(session, project_id, "plan")
            files = []
            for name in data[1]:
                if name.startswith("file:"):
                    try:
                        version_files.selected(session, settings, project_id, [name[5:]])
                        files.append(name[5:])
                    except BusinessError:
                        pass
            history = [public(session, settings, r) for r in rows(session, project_id)]
            return dict(
                stages=plan.payload["text"]["stages"] if plan else [],
                files=files,
                index_hash=data[3],
                previews=[r for r in history if r["kind"] == KINDS[0]],
                packages=[r for r in history if r["kind"] == KINDS[1]],
                releases=[r for r in history if r["kind"] == KINDS[2]],
                notice="本机选择性交付；部署检查为人工报告。缺关键验证显示部分交付，地址不代表发布完成。",
            )

    @app.post(prefix + "/previews", response_model=Asset)
    def preview(project_id: str, body: Preview, idempotency_key: str | None = Header(default=None)):
        def action(session):
            artifacts.safe(body.model_dump(), settings)
            b, base, tasks = basis(session, settings, project_id, body.stage_id)
            if b["index_hash"] != body.expected_index_hash:
                raise BusinessError("REVISION_CONFLICT", "来源已变化，输入保留，请明确采用当前版本", 409)
            files = version_files.selected(session, settings, project_id, body.files)
            ids = {rid for t in tasks for rid in t["requirement_ids"]}
            reqs = dict(
                baseline_id=base.id,
                baseline_hash=digest(base.payload),
                revision=base.payload["revision"],
                items=[r for r in base.payload["items"] if r["id"] in ids],
            )
            payload = dict(
                basis=b,
                files=files,
                requirements=reqs,
                **{
                    k: v
                    for k, v in body.model_dump().items()
                    if k in {"startup_steps", "config_names", "limitations", "backup_recovery", "reason"}
                },
            )
            # Ensure generated documents fit and are safe before retaining the preview.
            pack(payload, settings)
            return append(session, settings, project_id, "delivery_preview", payload)

        return requirements.mutate(
            sessions, project_id, "delivery_preview", body.model_dump(), idempotency_key, action
        )

    @app.post(prefix + "/packages", response_model=Asset)
    def package(project_id: str, body: Package, idempotency_key: str | None = Header(default=None)):
        def action(session):
            artifacts.safe(body.model_dump(), settings)
            row = owned(session, project_id, body.preview_id, "delivery_preview")
            if body.expected_hash != digest(row.payload) or not public(session, settings, row)["current"]:
                raise BusinessError("DELIVERY_STALE", "预览或来源已变化，请另存新预览；输入保留", 409)
            selected = version_files.selected(
                session, settings, project_id, [f["path"] for f in row.payload["files"]]
            )
            if digest(selected) != digest(row.payload["files"]):
                raise BusinessError("DELIVERY_STALE", "所选文件已变化，请另存新预览", 409)
            raw = pack(row.payload, settings)
            return append(
                session,
                settings,
                project_id,
                "delivery_package",
                dict(
                    **{k: v for k, v in row.payload.items() if k not in {"actor", "role", "schema_version"}},
                    preview_id=row.id,
                    preview_hash=digest(row.payload),
                    package_reason=body.reason,
                    zip_base64=base64.b64encode(raw).decode(),
                    zip_hash=hashlib.sha256(raw).hexdigest(),
                    zip_size=len(raw),
                    status="partial_delivery" if blockers(row.payload) else "local_ready",
                ),
            )

        return requirements.mutate(
            sessions, project_id, "delivery_package", body.model_dump(), idempotency_key, action
        )

    @app.get(prefix + "/packages/{package_id}/download")
    def download(project_id: str, package_id: str):
        with sessions() as session:
            row = owned(session, project_id, package_id, "delivery_package")
            raw = zip_bytes(row, settings)
            return Response(
                raw,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="delivery-{row.id}.zip"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    @app.post(prefix + "/releases", response_model=Asset)
    def release(project_id: str, body: Release, idempotency_key: str | None = Header(default=None)):
        def action(session):
            artifacts.safe(body.model_dump(), settings)
            row = owned(session, project_id, body.package_id, "delivery_package")
            previous = next(
                (r for r in rows(session, project_id, "release_record") if r.payload["package_id"] == row.id),
                None,
            )
            if body.expected_hash != digest(row.payload) or body.previous_id != (
                previous.id if previous else None
            ):
                raise BusinessError("REVISION_CONFLICT", "包或前次人工记录变化，输入保留", 409)
            if body.outcome == "reported_release":
                v = public(session, settings, row)
                if (
                    not v["current"]
                    or production_blockers(session, settings, project_id, row)
                    or not body.address
                    or any(c.status != "passed" for c in body.checks.values())
                ):
                    raise BusinessError(
                        "RELEASE_BLOCKED", "有效验收或六项生产条件缺失，不能记录发布完成", 409
                    )
                zip_bytes(row, settings)
            return append(
                session,
                settings,
                project_id,
                "release_record",
                dict(
                    **body.model_dump(exclude={"confirmed", "expected_hash"}),
                    package_hash=digest(row.payload),
                    source="manual_report",
                    deployed_by_platform=False,
                ),
            )

        return requirements.mutate(
            sessions, project_id, "release_record", body.model_dump(), idempotency_key, action
        )
