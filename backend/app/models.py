"""Explicit outbound previews, durable budget holds and bounded model analysis."""

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import Header
from sqlalchemy import select

from . import provider_capabilities
from .core import ROOT
from .db import ModelBudget, RequirementRecord
from .requirement_schemas import (
    AdoptInput,
    AnalysisCandidate,
    AnalysisPreviewInput,
    AnalysisPreviewOutput,
    AuthorizationInput,
    AuthorizationOutput,
    DraftOutput,
    ModelConfigInput,
    ModelStatus,
    RunInput,
    RunList,
    RunOutput,
)
from .requirements import (
    audit,
    field_decision,
    locked,
    mutate,
    owned_record,
    project,
    record,
    require_revision,
    save_revision,
    timestamp,
)
from .scanner import SECRET, BusinessError, digest

MODEL = os.getenv("FACTORY_MODEL_NAME", "qwen-plus-2025-12-01")
DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QUOTE_DATE = "2026-10-08"
PROMPT_VERSION = "requirements-v1"
MAX_OUTPUT = 8192
MAX_CALLS = 2
BUDGET_ID = "factory-development-stage-2"
ENDPOINT = re.compile(
    r"^https://(?:dashscope\.aliyuncs\.com|[a-zA-Z0-9_-]+\.cn-beijing\.maas\.aliyuncs\.com)/compatible-mode/v1$"
)
PII = re.compile(r"\b1[3-9][0-9]{9}\b|\b[0-9]{17}[0-9Xx]\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
config_lock = threading.Lock()


def env_path(settings):
    return settings.model_env or ROOT / ".env"


def config(settings):
    key = ""
    base = DEFAULT_BASE
    file = env_path(settings)
    if file.exists():
        if (
            file.is_symlink()
            or not file.is_file()
            or file.stat().st_size > 65536
            or file.stat().st_mode & 0o077
        ):
            raise BusinessError("CONFIG_INVALID", "本机模型配置权限或格式无效", 503)
        for line in file.read_text().splitlines():
            if line.startswith("FACTORY_MODEL_KEY="):
                key = line.partition("=")[2].strip()
            if line.startswith("FACTORY_MODEL_BASE_URL="):
                base = line.partition("=")[2].strip()
    if not ENDPOINT.fullmatch(base):
        raise BusinessError("CONFIG_INVALID", "仅支持已批准的北京模型接口", 503)
    return key, base


def quote_valid():
    age = (date.today() - date.fromisoformat(QUOTE_DATE)).days
    return 0 <= age <= 30


def available(settings):
    try:
        key, _ = config(settings)
        return bool(key) and quote_valid() and MODEL == "qwen-plus-2025-12-01"
    except (OSError, BusinessError, UnicodeError):
        return False


def require_config(settings):
    key, base = config(settings)
    if not key:
        raise BusinessError("MODEL_NOT_CONFIGURED", "AI 未启用；可先保存手动需求草稿", 409)
    if not quote_valid():
        raise BusinessError("QUOTE_UNAVAILABLE", "模型报价已过期，请由执行者核对后恢复", 409)
    if MODEL != "qwen-plus-2025-12-01":
        raise BusinessError("CONFIG_INVALID", "模型变化需重新核对契约及授权", 409)
    return key, base


def money(input_tokens, output_tokens):
    # micro-CNY: 0.8 CNY / million input; 2 CNY / million output.
    return (input_tokens * 8 + output_tokens * 20 + 9) // 10


def budget(session):
    item = session.get(ModelBudget, BUDGET_ID)
    if not item:
        item = ModelBudget(
            id=BUDGET_ID, spent_micro=0, reserved_micro=0, requests_used=0, requests_reserved=0
        )
        session.add(item)
        session.flush()
    return item


def public_record(item):
    return {**item.payload, "id": item.id, "project_id": item.project_id}


def expired(value):
    return datetime.fromisoformat(value) <= datetime.now(UTC)


def validate_preview(session, settings, project_id, identifier):
    item = owned_record(session, identifier, project_id, "analysis_preview")
    payload = item.payload
    require_revision(session, project_id, payload["revision"])
    _, base = require_config(settings)
    if (
        expired(payload["expires_at"])
        or base != payload["base_url"]
        or payload["model"] != MODEL
        or payload["prompt_hash"] != digest((Path(__file__).parent / "prompts/requirements.txt").read_text())
    ):
        raise BusinessError("PREVIEW_CHANGED", "分析预览过期或配置变化，请重新预览", 409)
    return item


class ProviderError(Exception):
    def __init__(self, code, sent):
        self.code, self.sent = code, sent


class Provider:
    source = "real_cloud"

    def send(self, key, base, selected, correction=False):
        prompt = (Path(__file__).parent / "prompts/requirements.txt").read_text()
        if correction:
            prompt += "\n上次输出未通过结构校验；重新按规定 JSON 结构输出。不添加新资料。"
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps({"untrusted_selected_fields": selected}, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "max_tokens": MAX_OUTPUT,
            "temperature": 0.2,
            "stream": False,
        }
        try:
            client = httpx.Client(
                timeout=httpx.Timeout(60, connect=10), trust_env=False, follow_redirects=False
            )
        except Exception:
            raise ProviderError("MODEL_INIT_FAILED", False) from None
        try:
            started = time.monotonic()
            with client:
                with client.stream(
                    "POST", base + "/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body
                ) as response:
                    if response.status_code != 200:
                        # Do not expose provider response bodies or assume rejected requests cost zero.
                        raise ProviderError(
                            "MODEL_AUTH_FAILED"
                            if response.status_code in (401, 403)
                            else "MODEL_REQUEST_FAILED",
                            True,
                        )
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 262144 or time.monotonic() - started > 65:
                            raise ProviderError("MODEL_RESPONSE_INVALID", True)
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ProviderError("MODEL_RESPONSE_INVALID", True)
                value["_factory_request_id"] = provider_capabilities.safe_id(
                    response.headers.get("x-request-id"), (key,)
                )
                return value
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("MODEL_RESULT_UNKNOWN", True) from None


def settle(session, item, state, error=None, candidate=None):
    payload = item.payload
    if state != "result_unknown":
        account = budget(session)
        account.reserved_micro -= payload["reserved_micro"]
        account.requests_reserved -= MAX_CALLS
        account.requests_used += sum(a["state"] != "not_sent" for a in payload["attempts"])
        account.spent_micro += payload["settled_micro"]
    item.payload = {
        **payload,
        "state": state,
        "error": error,
        "candidate": candidate,
        "finished_at": timestamp(),
    }
    audit(session, item.project_id, "analysis_finished", {"run_id": item.id, "state": state})


def process_run(sessions, settings, provider, identifier, stop=None):
    with locked(sessions) as session:
        item = session.get(RequirementRecord, identifier)
        if not item or item.kind != "run" or item.payload["state"] != "queued":
            return
        item.payload = {**item.payload, "state": "running", "started_at": timestamp()}
        pid = item.project_id
        preview_id = item.payload["preview_id"]
    for attempt_number in range(1, MAX_CALLS + 1):
        try:
            if stop is not None and stop.is_set():
                raise BusinessError("WORKER_STOPPED", "服务正在停止，未开始新的模型调用", 409)
            with locked(sessions) as session:
                item = owned_record(session, identifier, pid, "run")
                preview = validate_preview(session, settings, pid, preview_id)
                authorization = owned_record(session, item.payload["authorization_id"], pid, "authorization")
                if authorization.payload["revoked"] or expired(authorization.payload["expires_at"]):
                    raise BusinessError("AUTHORIZATION_REQUIRED", "资料授权已失效", 409)
                key, base = require_config(settings)
                selected = preview.payload["selected_fields"]
                input_bound = preview.payload["input_bound"]
                attempts = [*item.payload["attempts"], {"attempt": attempt_number, "state": "dispatching"}]
                item.payload = {**item.payload, "attempts": attempts}
            call_started = time.monotonic()
            try:
                receipt = provider.send(key, base, selected, correction=attempt_number > 1)
                elapsed_ms = max(0, int((time.monotonic() - call_started) * 1000))
            except ProviderError as exc:
                with locked(sessions) as session:
                    item = owned_record(session, identifier, pid, "run")
                    attempts = item.payload["attempts"]
                    elapsed_ms = max(0, int((time.monotonic() - call_started) * 1000))
                    provider_capabilities.observe(
                        session,
                        item,
                        preview,
                        attempt_number,
                        elapsed_ms,
                        secrets=(key, settings.token),
                        sent=exc.sent,
                        cost=None if exc.sent else 0,
                        error=exc.code,
                    )
                    attempts[-1].update(
                        state="unknown" if exc.sent else "not_sent",
                        error_code=exc.code,
                        elapsed_ms=elapsed_ms,
                    )
                    item.payload = {**item.payload, "attempts": attempts}
                    settle(
                        session,
                        item,
                        "result_unknown" if exc.sent else "failed",
                        {
                            "code": exc.code,
                            "message": "模型结果或费用需核对，未自动重试"
                            if exc.sent
                            else "模型初始化失败，没有发送资料",
                        },
                    )
                return
            try:
                if isinstance(receipt, dict) and receipt.get("model") not in (None, MODEL):
                    raise ValueError("model receipt mismatch")
                usage = receipt["usage"]
                ins, outs = usage["prompt_tokens"], usage["completion_tokens"]
                if (
                    type(ins) is not int
                    or type(outs) is not int
                    or not 0 <= ins <= input_bound
                    or not 0 <= outs <= MAX_OUTPUT
                ):
                    raise ValueError("invalid usage")
                cost = money(ins, outs)
            except (KeyError, TypeError, ValueError):
                with locked(sessions) as session:
                    item = owned_record(session, identifier, pid, "run")
                    request_id = provider_capabilities.observe(
                        session,
                        item,
                        preview,
                        attempt_number,
                        elapsed_ms,
                        receipt,
                        secrets=(key, settings.token),
                        error="USAGE_UNKNOWN",
                    )
                    attempts = item.payload["attempts"]
                    attempts[-1].update(
                        state="unknown",
                        error_code="USAGE_UNKNOWN",
                        request_id=request_id,
                        elapsed_ms=elapsed_ms,
                    )
                    item.payload = {**item.payload, "attempts": attempts}
                    settle(
                        session,
                        item,
                        "result_unknown",
                        {"code": "USAGE_UNKNOWN", "message": "用量回执无法核对，预留额度保留"},
                    )
                return
            candidate = None
            try:
                if receipt["choices"][0].get("finish_reason") not in (None, "stop"):
                    raise ValueError("incomplete output")
                candidate = AnalysisCandidate.model_validate_json(receipt["choices"][0]["message"]["content"])
                allowed = {f["key"] for f in selected}
                if any(
                    s.field not in allowed or not set(s.source_fields) <= allowed
                    for s in candidate.suggestions
                ):
                    raise ValueError("unselected source")
                if SECRET.search(candidate.model_dump_json()) or PII.search(candidate.model_dump_json()):
                    raise ValueError("sensitive output")
            except (ValueError, KeyError, IndexError, TypeError):
                candidate = None
            with locked(sessions) as session:
                item = owned_record(session, identifier, pid, "run")
                attempts = item.payload["attempts"]
                request_id = provider_capabilities.observe(
                    session,
                    item,
                    preview,
                    attempt_number,
                    elapsed_ms,
                    receipt,
                    secrets=(key, settings.token),
                    candidate=candidate,
                    ins=ins,
                    outs=outs,
                    cost=cost,
                    error=None if candidate else "MODEL_STRUCTURE_INVALID",
                )
                attempts[-1].update(
                    state="settled",
                    request_id=request_id,
                    elapsed_ms=elapsed_ms,
                    input_tokens=ins,
                    output_tokens=outs,
                    cost_micro=cost,
                    error_code=None if candidate else "MODEL_STRUCTURE_INVALID",
                )
                item.payload = {
                    **item.payload,
                    "attempts": attempts,
                    "settled_micro": item.payload["settled_micro"] + cost,
                }
                if candidate:
                    settle(session, item, "succeeded", candidate=candidate.model_dump())
                    return
                if attempt_number == MAX_CALLS:
                    settle(
                        session,
                        item,
                        "failed",
                        {
                            "code": "MODEL_STRUCTURE_INVALID",
                            "message": "一次修正后输出仍不合规，没有采用需求",
                        },
                    )
                    return
        except BusinessError as exc:
            with locked(sessions) as session:
                item = owned_record(session, identifier, pid, "run")
                settle(session, item, "failed", {"code": exc.code, "message": exc.message})
            return
        except Exception:
            with locked(sessions) as session:
                item = owned_record(session, identifier, pid, "run")
                # Unknown side effects retain all reservations, never repeat blindly.
                settle(
                    session,
                    item,
                    "result_unknown",
                    {"code": "MODEL_RESULT_UNKNOWN", "message": "运行中断，需核对结果与费用"},
                )
            return


def start_worker(sessions, settings, provider):
    stop = threading.Event()
    with locked(sessions) as session:
        running = session.scalars(select(RequirementRecord).where(RequirementRecord.kind == "run")).all()
        for item in running:
            if item.payload["state"] == "running":
                settle(
                    session,
                    item,
                    "result_unknown",
                    {"code": "RESTART_RECONCILIATION", "message": "重启前外部结果无法确认，未自动重发"},
                )

    def loop():
        reported = False
        while not stop.is_set():
            try:
                with sessions() as session:
                    rows = session.scalars(
                        select(RequirementRecord).where(RequirementRecord.kind == "run")
                    ).all()
                    identifier = next((r.id for r in rows if r.payload["state"] == "queued"), None)
                if identifier:
                    process_run(sessions, settings, provider, identifier, stop)
                setattr(thread, "failure_code", None)
                reported = False
            except Exception:
                setattr(thread, "failure_code", "ANALYSIS_STORAGE_UNAVAILABLE")
                if not reported:
                    logging.getLogger(__name__).error("分析存储操作失败，排队任务未标为完成；等待恢复")
                    reported = True
            stop.wait(0.25)

    thread = threading.Thread(target=loop, daemon=True, name="factory-demand-analysis")
    thread.start()
    return stop, thread


def install(app, sessions, settings, provider):
    prefix = "/api/v1/factory"

    @app.get(prefix + "/model/status", response_model=ModelStatus)
    def status():
        key, base = config(settings)
        with locked(sessions) as session:
            account = budget(session)
            return {
                "configured": bool(key),
                "model": MODEL,
                "base_url": base,
                "quote_valid": quote_valid(),
                "quote_date": QUOTE_DATE,
                "spent_micro": account.spent_micro,
                "reserved_micro": account.reserved_micro,
                "requests_used": account.requests_used,
                "requests_reserved": account.requests_reserved,
            }

    @app.post(prefix + "/model/config", response_model=ModelStatus)
    def configure(body: ModelConfigInput):
        if not ENDPOINT.fullmatch(body.base_url):
            raise BusinessError("CONFIG_INVALID", "仅允许北京百炼接口地址", 422)
        path = env_path(settings)
        with config_lock:
            if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_size > 65536):
                raise BusinessError("CONFIG_INVALID", "模型配置文件不可写", 409)
            lines = path.read_text().splitlines() if path.exists() else []
            keep = [
                line
                for line in lines
                if not line.startswith(("FACTORY_MODEL_KEY=", "FACTORY_MODEL_BASE_URL="))
            ]
            raw = "\n".join(
                [*keep, f"FACTORY_MODEL_KEY={body.api_key}", f"FACTORY_MODEL_BASE_URL={body.base_url}", ""]
            )
            fd, temp = tempfile.mkstemp(prefix=".factory-config-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as file:
                    file.write(raw)
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        with locked(sessions) as session:
            audit(session, "platform", "model_configured", {"model": MODEL})
        return status()

    @app.post(prefix + "/projects/{project_id}/analysis/preview", response_model=AnalysisPreviewOutput)
    def preview(
        project_id: str, body: AnalysisPreviewInput, idempotency_key: str | None = Header(default=None)
    ):
        def operation(session):
            _, base = require_config(settings)
            revision, data = require_revision(session, project_id, body.expected_revision)
            if len(set(body.selected_fields)) != len(body.selected_fields):
                raise BusinessError("INPUT_INVALID", "选择字段不能重复", 422)
            selected = [
                {k: f[k] for k in ("key", "content", "status")}
                for f in data["fields"]
                if f["key"] in body.selected_fields
            ]
            raw = json.dumps(selected, ensure_ascii=False)
            if SECRET.search(raw) or PII.search(raw):
                raise BusinessError("SENSITIVE_INPUT", "所选需求疑似含秘密或个人信息，请先脱敏", 409)
            if len(raw.encode()) > 48000:
                raise BusinessError("INPUT_INVALID", "所选内容过长，请缩小分析范围", 413)
            input_bound = len(raw.encode()) + 8192
            reserve = money(input_bound, MAX_OUTPUT) * MAX_CALLS
            payload = {
                "revision": revision,
                "model": MODEL,
                "base_url": base,
                "selected_fields": selected,
                "omitted_fields": [f["key"] for f in data["fields"] if f["key"] not in body.selected_fields],
                "max_attempts": MAX_CALLS,
                "reserve_micro": reserve,
                "input_bound": input_bound,
                "expires_at": timestamp(datetime.now(UTC) + timedelta(minutes=15)),
                "prompt_hash": digest((Path(__file__).parent / "prompts/requirements.txt").read_text()),
            }
            payload["content_hash"] = digest(payload)
            item = record(session, project_id, "analysis_preview", payload)
            # Explicit projection: public previews omit internal bound and prompt hash.
            return {k: v for k, v in public_record(item).items() if k not in ("input_bound", "prompt_hash")}

        return mutate(sessions, project_id, "analysis_preview", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/projects/{project_id}/analysis/authorize", response_model=AuthorizationOutput)
    def authorize(
        project_id: str, body: AuthorizationInput, idempotency_key: str | None = Header(default=None)
    ):
        def operation(session):
            preview = validate_preview(session, settings, project_id, body.preview_id)
            data = preview.payload
            if data["content_hash"] != body.expected_hash:
                raise BusinessError("PREVIEW_CHANGED", "授权内容与预览不一致", 409)
            payload = {
                "preview_id": preview.id,
                "content_hash": data["content_hash"],
                "max_attempts": MAX_CALLS,
                "reserve_micro": data["reserve_micro"],
                "expires_at": data["expires_at"],
                "approved_by": "local_operator",
                "revoked": False,
            }
            item = record(session, project_id, "authorization", payload)
            audit(
                session,
                project_id,
                "analysis_authorized",
                {"authorization_id": item.id, "hash": data["content_hash"]},
            )
            return public_record(item)

        return mutate(sessions, project_id, "authorize", body.model_dump(), idempotency_key, operation)

    @app.post(prefix + "/projects/{project_id}/analysis/runs", response_model=RunOutput, status_code=202)
    def run(project_id: str, body: RunInput, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            require_revision(session, project_id, body.expected_revision)
            preview = validate_preview(session, settings, project_id, body.preview_id)
            authorization = owned_record(session, body.authorization_id, project_id, "authorization")
            permission = authorization.payload
            if (
                permission["revoked"]
                or expired(permission["expires_at"])
                or permission["preview_id"] != preview.id
                or permission["content_hash"] != preview.payload["content_hash"]
            ):
                raise BusinessError("AUTHORIZATION_REQUIRED", "当前分析没有有效资料授权", 409)
            rows = session.scalars(select(RequirementRecord).where(RequirementRecord.kind == "run")).all()
            if any(
                r.project_id == project_id and r.payload["state"] in ("queued", "running", "result_unknown")
                for r in rows
            ):
                raise BusinessError("PROJECT_BUSY", "已有分析未结束或结果待核对，不能重复调用", 409)
            if any(r.payload["authorization_id"] == authorization.id for r in rows):
                raise BusinessError("AUTHORIZATION_REQUIRED", "此授权已用于分析，请重新审查预览", 409)
            account = budget(session)
            amount = preview.payload["reserve_micro"]
            if (
                amount > 2_000_000
                or account.spent_micro + account.reserved_micro + amount > 100_000_000
                or account.requests_used + account.requests_reserved + MAX_CALLS > 100
            ):
                raise BusinessError("BUDGET_EXCEEDED", "单次或开发阶段额度不足，未调用模型", 409)
            account.reserved_micro += amount
            account.requests_reserved += MAX_CALLS
            payload = {
                "revision": body.expected_revision,
                "state": "queued",
                "source": provider.source,
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
                "candidate": None,
                "error": None,
                "attempts": [],
                "reserved_micro": amount,
                "settled_micro": 0,
                "started_at": None,
                "finished_at": None,
                "preview_id": preview.id,
                "authorization_id": authorization.id,
            }
            item = record(session, project_id, "run", payload)
            audit(session, project_id, "analysis_queued", {"run_id": item.id, "reserved_micro": amount})
            return {k: v for k, v in public_record(item).items() if k != "preview_id"}

        return mutate(sessions, project_id, "run", body.model_dump(), idempotency_key, operation)

    @app.post(
        prefix + "/projects/{project_id}/analysis/authorizations/{authorization_id}/revoke",
        response_model=AuthorizationOutput,
    )
    def revoke(project_id: str, authorization_id: str, idempotency_key: str | None = Header(default=None)):
        def operation(session):
            item = owned_record(session, authorization_id, project_id, "authorization")
            item.payload = {**item.payload, "revoked": True}
            audit(session, project_id, "analysis_authorization_revoked", {"authorization_id": item.id})
            return public_record(item)

        return mutate(sessions, project_id, "revoke", {"id": authorization_id}, idempotency_key, operation)

    @app.get(prefix + "/projects/{project_id}/analysis/runs/{run_id}", response_model=RunOutput)
    def get_run(project_id: str, run_id: str):
        with sessions() as session:
            item = owned_record(session, run_id, project_id, "run")
            return {k: v for k, v in public_record(item).items() if k != "preview_id"}

    @app.get(prefix + "/projects/{project_id}/analysis/runs", response_model=RunList)
    def list_runs(project_id: str):
        with sessions() as session:
            project(session, project_id)
            rows = session.scalars(
                select(RequirementRecord)
                .where(RequirementRecord.project_id == project_id, RequirementRecord.kind == "run")
                .order_by(RequirementRecord.created_at.desc())
                .limit(20)
            ).all()
            return {"items": [{k: v for k, v in public_record(r).items() if k != "preview_id"} for r in rows]}

    @app.post(prefix + "/projects/{project_id}/analysis/runs/{run_id}/adopt", response_model=DraftOutput)
    def adopt(
        project_id: str, run_id: str, body: AdoptInput, idempotency_key: str | None = Header(default=None)
    ):
        def operation(session):
            revision, data = require_revision(session, project_id, body.expected_revision)
            item = owned_record(session, run_id, project_id, "run")
            if item.payload["state"] != "succeeded" or item.payload["revision"] != revision:
                raise BusinessError("RESULT_STALE", "结果未成功或需求版本变化，请核对后重新分析", 409)
            candidate = AnalysisCandidate.model_validate(item.payload["candidate"])
            selected = {s.field: s for s in candidate.suggestions}
            if len(set(body.fields)) != len(body.fields) or not set(body.fields) <= set(selected):
                raise BusinessError("INPUT_INVALID", "采用字段不属于当前候选", 422)
            fields = [dict(f) for f in data["fields"]]
            for field in fields:
                if field["key"] in body.fields:
                    field.update(
                        content=selected[field["key"]].content,
                        status="known",
                        source="ai_suggestion",
                        source_ref=f"analysis:{run_id}:{field['key']}",
                        confirmed_by="local_operator",
                    )
                    field_decision(session, project_id, field, revision + 1)
            audit(session, project_id, "analysis_adopted", {"run_id": run_id, "fields": body.fields})
            return save_revision(session, project_id, revision, {**data, "fields": fields})

        return mutate(sessions, project_id, "adopt", body.model_dump(), idempotency_key, operation)

    return provider
