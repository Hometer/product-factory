import hashlib
import hmac
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException

from . import (
    command_controls,
    commands,
    contexts,
    evaluations,
    execution,
    models,
    patches,
    planning,
    provider_capabilities,
    recovery,
    requirements,
    verification,
)
from .core import ROOT, Settings, load_settings
from .db import AuditEvent, Preview, Project, RequestRecord, connect
from .scanner import BusinessError, canonical, digest, scan
from .schemas import (
    ConfirmInput,
    HealthOutput,
    PreviewOutput,
    ProjectInput,
    ProjectList,
    ProjectOutput,
    SettingsOutput,
)

PREFIX = "/api/v1/factory"


def project_output(project: Project) -> dict:
    return {
        **project.payload,
        "id": project.id,
        "path": project.canonical_path,
        "created_at": project.created_at.replace(tzinfo=UTC).isoformat(),
        "revision": project.revision,
        "status": "registered",
    }


def planned(payload: dict) -> dict[str, bytes]:
    readme = (
        f"# {payload['name']}\n\n{payload['description']}\n\n"
        f"目标用户：{payload['target_user']}\n\n核心任务：{payload['core_task']}\n\n"
        "当前仅完成项目创建，需求、代码、真实验证和验收尚未完成。\n"
    )
    return {
        "README.md": readme.encode(),
        "AGENTS.md": (ROOT / "AGENTS.md").read_bytes(),
        "docs/PRD/PRD模板.md": (ROOT / "docs/PRD/PRD模板.md").read_bytes(),
        ".gitignore": (ROOT / ".gitignore").read_bytes(),
    }


def create_manifest(payload: dict) -> dict:
    return {
        "files": [
            {
                "path": name,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "status": "planned",
                "chars": 0,
            }
            for name, raw in planned(payload).items()
        ],
        "warnings": [],
        "git_present": False,
        "git_changes": "not_checked",
    }


def rollback_created(target: Path, written: dict[str, bytes]) -> None:
    # Never delete external files or modified contents, even during rollback.
    for name, raw in written.items():
        file = target / name
        if file.is_file() and not file.is_symlink() and file.read_bytes() == raw:
            file.unlink()
    for directory in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and not directory.is_symlink():
            try:
                directory.rmdir()
            except OSError:
                pass
    try:
        target.rmdir()
    except OSError:
        pass


def create_app(settings: Settings | None = None, model_provider=None) -> FastAPI:
    settings = settings or load_settings()
    engine, sessions = connect(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app):
        migration = Config(str(ROOT / "backend/alembic.ini"))
        migration.set_main_option("script_location", str(ROOT / "backend/alembic"))
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "head")
        commands.reconcile(sessions, settings)
        patches.recover_pending(sessions, settings)
        app.state.command_manager = commands.Manager(sessions, settings).start()
        stop, worker = models.start_worker(sessions, settings, model_provider or models.Provider())
        app.state.analysis_worker = worker
        try:
            yield
        finally:
            app.state.command_manager.close()
            stop.set()
            worker.join(timeout=65)
            engine.dispose()

    app = FastAPI(title="产品工厂", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions

    @app.middleware("http")
    async def security(request: Request, call_next):
        def reject(code, message, status):
            return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

        if request.headers.get("host") not in settings.hosts:
            return reject("FORBIDDEN", "访问地址不在本机允许范围", 403)
        token = request.headers.get("authorization", "")
        if not hmac.compare_digest(token.encode(), f"Bearer {settings.token}".encode()):
            return reject("UNAUTHORIZED", "请通过本机工作台访问", 401)
        origin = request.headers.get("origin")
        if origin and origin not in settings.origins:
            return reject("FORBIDDEN", "请求来源不在允许范围", 403)
        if request.method not in {"GET", "HEAD"} and origin not in settings.origins:
            return reject("FORBIDDEN", "写请求需要工作台来源", 403)
        try:
            response = await call_next(request)
        except Exception:
            # Do not log exception repr/traceback: it may contain original documents.
            response = reject("INTERNAL_ERROR", "操作未完成，请重试或检查本机服务", 500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(BusinessError)
    async def business_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "FILE_UNAVAILABLE",
                    "message": "本机文件暂不可安全操作，输入与历史保留，请核对文件权限和目录",
                }
            },
            status_code=409,
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "STORAGE_UNAVAILABLE",
                    "message": "本机记录暂不可用，请查询恢复状态后处理；不会自动重复写入",
                }
            },
            status_code=503,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"error": {"code": "INPUT_INVALID", "message": "填写内容格式不正确，请检查必填项"}},
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "NOT_FOUND" if exc.status_code == 404 else "REQUEST_FAILED",
                    "message": "请求地址不存在" if exc.status_code == 404 else "请求未完成",
                }
            },
            status_code=exc.status_code,
        )

    @app.get(f"{PREFIX}/health", response_model=HealthOutput)
    def health():
        if app.state.command_manager.failure_code:
            raise BusinessError("SERVICE_UNAVAILABLE", "命令记录存储暂不可用，状态保留", 503)
        if getattr(app.state.analysis_worker, "failure_code", None):
            raise BusinessError("SERVICE_UNAVAILABLE", "需求分析存储暂不可用，任务状态保留", 503)
        return {"status": "ok", "schema_version": "0008", "model_enabled": models.available(settings)}

    @app.get(f"{PREFIX}/settings", response_model=SettingsOutput)
    def get_settings():
        return {
            "allowed_roots": [str(p) for p in settings.allowed_roots],
            "default_path": str(settings.allowed_roots[0] / "新产品"),
            "model_enabled": models.available(settings),
        }

    @app.get(f"{PREFIX}/projects", response_model=ProjectList)
    def list_projects(
        offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), q: str = Query("", max_length=60)
    ):
        with sessions() as session:
            stmt = select(Project)
            projects = session.scalars(stmt.order_by(Project.created_at.desc())).all()
            # SQLite JSON/name matching is small and deterministic in this first slice.
            selected = [p for p in projects if q.casefold() in p.payload["name"].casefold()]
            return {
                "items": [project_output(p) for p in selected[offset : offset + limit]],
                "total": len(selected),
            }

    @app.get(f"{PREFIX}/projects/{{project_id}}", response_model=ProjectOutput)
    def detail(project_id: str):
        with sessions() as session:
            project = session.get(Project, project_id)
            if not project:
                raise BusinessError("NOT_FOUND", "项目不存在", 404)
            return project_output(project)

    def preview(payload: ProjectInput, kind: str):
        path = canonical(payload.path, settings.allowed_roots)
        data = payload.model_dump()
        data["path"] = str(path)
        if kind == "create":
            if path.exists() or path.is_symlink():
                raise BusinessError("DIRECTORY_CONFLICT", "目录已有内容，请改用接入", 409)
            if not path.parent.is_dir():
                raise BusinessError("INPUT_INVALID", "上级目录不存在，请选择已有上级目录")
            manifest = create_manifest(data)
        else:
            manifest = scan(path)
        content_hash = digest({"kind": kind, "request": data, "manifest": manifest})
        item = Preview(id=str(uuid4()), kind=kind, payload=data, manifest=manifest, content_hash=content_hash)
        with sessions.begin() as session:
            session.add(item)
        return {
            "preview_id": item.id,
            "content_hash": content_hash,
            "kind": kind,
            "request": data,
            "manifest": manifest,
            "planned_files": list(planned(data)) if kind == "create" else [],
        }

    @app.post(f"{PREFIX}/projects/create-preview", response_model=PreviewOutput)
    def preview_create(payload: ProjectInput):
        return preview(payload, "create")

    @app.post(f"{PREFIX}/projects/import-preview", response_model=PreviewOutput)
    def preview_import(payload: ProjectInput):
        # Third-party parsers must never print full input/exception strings.
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        return preview(payload, "import")

    @app.post(f"{PREFIX}/projects/confirm", response_model=ProjectOutput)
    def confirm(
        payload: ConfirmInput,
        idempotency_key: str = Header(..., min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        request_hash = digest(payload.model_dump())
        target = None
        written: dict[str, bytes] = {}
        try:
            with sessions() as session:
                # Serialize writes before querying idempotency, including filesystem landing.
                session.connection().exec_driver_sql("UPDATE requests SET key=key WHERE 0")
                record = session.get(RequestRecord, idempotency_key)
                if record:
                    if record.request_hash != request_hash:
                        raise BusinessError("IDEMPOTENCY_CONFLICT", "相同请求编号对应不同内容", 409)
                    return record.response
                item = session.get(Preview, payload.preview_id)
                if not item:
                    raise BusinessError("NOT_FOUND", "预览不存在，请重新预览", 404)
                if item.content_hash != payload.expected_hash:
                    raise BusinessError("PREVIEW_CHANGED", "预览内容不匹配，请重新预览", 409)
                path = canonical(item.payload["path"], settings.allowed_roots)
                if str(path) != item.payload["path"]:
                    raise BusinessError("PREVIEW_CHANGED", "目录目标发生变化，请重新预览", 409)
                if item.confirmed_project_id:
                    project = session.get(Project, item.confirmed_project_id)
                    if project is None:
                        raise BusinessError("NOT_FOUND", "已登记项目不存在", 404)
                else:
                    created = item.created_at.replace(tzinfo=UTC)
                    if datetime.now(UTC) - created > timedelta(hours=24):
                        raise BusinessError("PREVIEW_EXPIRED", "预览已过期，请重新预览", 409)
                    if item.kind == "create":
                        if path.exists():
                            raise BusinessError("DIRECTORY_CONFLICT", "目录已存在，请重新预览或接入", 409)
                        manifest = create_manifest(item.payload)
                    else:
                        manifest = scan(path)
                    current_hash = digest({"kind": item.kind, "request": item.payload, "manifest": manifest})
                    if current_hash != item.content_hash:
                        raise BusinessError("PREVIEW_CHANGED", "资料发生变化，请重新预览", 409)
                    project = session.scalar(select(Project).where(Project.canonical_path == str(path)))
                    if not project:
                        if item.kind == "create":
                            content = planned(item.payload)
                            with tempfile.TemporaryDirectory(
                                prefix=".factory-stage-", dir=path.parent
                            ) as staging:
                                for name, raw in content.items():
                                    file = Path(staging) / name
                                    file.parent.mkdir(parents=True, exist_ok=True)
                                    file.write_bytes(raw)
                                path.mkdir(mode=0o700)  # Exclusive creation; never overwrite.
                                target = path
                                for name, raw in content.items():
                                    file = path / name
                                    file.parent.mkdir(parents=True, exist_ok=True)
                                    if not file.parent.resolve().is_relative_to(path):
                                        raise BusinessError("PATH_FORBIDDEN", "目标目录出现越界链接")
                                    # Atomic, exclusive landing of already completed content.
                                    # A failure cannot leave a partly written destination file.
                                    os.link(Path(staging) / name, file)
                                    written[name] = raw
                        project = Project(id=str(uuid4()), canonical_path=str(path), payload=item.payload)
                        session.add(project)
                        session.flush()
                        session.add(
                            AuditEvent(
                                id=str(uuid4()),
                                project_id=project.id,
                                action="project_created" if item.kind == "create" else "project_imported",
                                details={
                                    "preview_id": item.id,
                                    "content_hash": item.content_hash,
                                    "actor": "local_operator",
                                    "role": "product_owner",
                                },
                            )
                        )
                    item.confirmed_project_id = project.id
                response = project_output(project)
                session.add(RequestRecord(key=idempotency_key, request_hash=request_hash, response=response))
                session.commit()
                return response
        except BusinessError:
            if target is not None:
                rollback_created(target, written)
            raise
        except (IntegrityError, FileExistsError):
            if target is not None:
                rollback_created(target, written)
            raise BusinessError("DIRECTORY_CONFLICT", "目录或登记记录发生冲突，请重新预览", 409) from None
        except (OSError, SQLAlchemyError):
            if target is not None:
                rollback_created(target, written)
            raise BusinessError(
                "OPERATION_FAILED", "操作未完成，原项目保留，请检查权限后重新预览", 503
            ) from None

    execution.install(app, sessions, settings)
    patches.install(app, sessions, settings)
    commands.install(app, sessions, settings)
    recovery.install(app, sessions, settings)
    command_controls.install(app, sessions, settings)
    provider_capabilities.install(app, sessions, settings)
    contexts.install(app, sessions, settings)
    verification.install(app, sessions, settings)
    from . import acceptance

    acceptance.install(app, sessions, settings)
    evaluations.install(app, sessions, settings)
    planning.install(app, sessions, settings)
    requirements.install(app, sessions)
    models.install(app, sessions, settings, model_provider or models.Provider())
    return app
