"""Read-only capabilities and append-only, sanitized provider observations."""

import re
from pathlib import Path

from sqlalchemy import select

from .db import ModelBudget, RequirementRecord
from .provider_schemas import ProviderView
from .requirements import record, timestamp
from .scanner import SECRET, digest

VERSION = "bailian-demand-v1"
CAPABILITIES = [
    ("text", "文本输出", True, "非流式需求候选；离线协议通过，真实效果需单独验收"),
    ("structured", "结构输出", True, "后端严格解析与显式引用校验；一次修正仍占授权次数与预算"),
    ("streaming", "流式输出", False, "本平台未接入流式契约，不能运行；当前分析使用提交与轮询"),
    ("code", "代码生成", False, "本平台未接入代码生成契约，不能运行；已有本机配方不等于模型生成代码"),
    ("usage", "费用统计", True, "按有效用量回执及有效报价核算，非服务商最终账单；未知保留预算"),
    ("query", "提供方请求查询", False, "本平台没有提供方查询接口；保存请求编号不等于查到外部执行或费用"),
]


def _calculate_adapter_hash():
    folder = Path(__file__).parent
    return digest(
        {
            p: (folder / p).read_text()
            for p in [
                "models.py",
                "provider_capabilities.py",
                "requirement_schemas.py",
                "prompts/requirements.txt",
            ]
        }
    )


_ADAPTER_HASH = _calculate_adapter_hash()


def adapter_hash():
    return _ADAPTER_HASH


def safe_id(value, secrets=()):
    from .models import PII

    return (
        value
        if isinstance(value, str)
        and re.fullmatch(
            r"(?:chatcmpl-[A-Za-z0-9_-]{1,96}|[a-fA-F0-9]{32}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})",
            value,
        )
        and not any(secret and secret in value for secret in secrets)
        and not SECRET.search(value)
        and not PII.search(value)
        else None
    )


def observe(
    session,
    item,
    preview,
    attempt,
    elapsed_ms,
    receipt=None,
    *,
    sent=True,
    candidate=None,
    ins=None,
    outs=None,
    cost=None,
    error=None,
    secrets=(),
):
    from .models import PII

    value = receipt if isinstance(receipt, dict) else {}
    try:
        content = value["choices"][0]["message"]["content"]
        text_valid = (
            isinstance(content, str)
            and 0 < len(content.encode()) <= 262144
            and not SECRET.search(content)
            and not PII.search(content)
        )
    except (KeyError, TypeError, IndexError):
        text_valid = False
    request_id = safe_id(value.get("_factory_request_id"), secrets) or safe_id(value.get("id"), secrets)
    payload = {
        "run_id": item.id,
        "attempt": attempt,
        "source": item.payload["source"],
        "model": item.payload["model"],
        "base_url": preview.payload["base_url"],
        "contract_version": VERSION,
        "adapter_hash": adapter_hash(),
        "checked_at": timestamp(),
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "sent": sent,
        "text_valid": bool(text_valid),
        "structure_valid": candidate is not None,
        "usage_valid": ins is not None and outs is not None and cost is not None,
        "input_tokens": ins,
        "output_tokens": outs,
        "cost_micro": cost,
        "error_code": error,
    }
    record(session, item.project_id, "provider_receipt", payload)
    return request_id


def install(app, sessions, settings):
    from . import models

    @app.get("/api/v1/factory/model/provider", response_model=ProviderView)
    def view():
        key, base = models.config(settings)
        blockers = []
        if not key:
            blockers.append("尚未配置密钥；保存配置不验证能力或发送资料")
        if not models.quote_valid():
            blockers.append("报价过期，不能开始调用；请由执行者重新核对")
        if models.MODEL != "qwen-plus-2025-12-01":
            blockers.append("模型变化，当前接入契约与原授权不能继承")
        current_hash = adapter_hash()
        with sessions() as session:
            rows = list(
                session.scalars(
                    select(RequirementRecord)
                    .where(RequirementRecord.kind == "provider_receipt")
                    .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id.desc())
                )
            )
            matching = [
                r
                for r in rows
                if r.payload["model"] == models.MODEL
                and r.payload["base_url"] == base
                and r.payload["adapter_hash"] == current_hash
                and r.payload["contract_version"] == VERSION
                and r.payload["source"] == "real_cloud"
                and r.payload["error_code"] is None
                and r.payload["usage_valid"]
            ]
            capabilities = []
            for id, name, implemented, explanation in CAPABILITIES:
                validation = {
                    "text": "text_valid",
                    "structured": "structure_valid",
                    "usage": "usage_valid",
                }.get(id)
                capabilities.append(
                    {
                        "id": id,
                        "name": name,
                        "implemented": implemented,
                        "offline_contract": "verified"
                        if implemented and models.MODEL == "qwen-plus-2025-12-01"
                        else "not_supported",
                        "real_verified_count": sum(bool(r.payload.get(validation)) for r in matching)
                        if validation
                        else 0,
                        "can_run": implemented and not blockers,
                        "explanation": explanation,
                    }
                )
            account = session.get(ModelBudget, models.BUDGET_ID)
            return {
                "provider": "百炼 · 北京",
                "model": models.MODEL,
                "base_url": base,
                "configured": bool(key),
                "key_mask": "••••••••" if key else "未配置",
                "contract_version": VERSION,
                "adapter_hash": current_hash,
                "automatic_switch": False,
                "max_attempts": 2,
                "blockers": blockers,
                "capabilities": capabilities,
                "receipts": [{**r.payload, "id": r.id, "project_id": r.project_id} for r in rows[:100]],
                "receipt_total": len(rows),
                "quote_date": models.QUOTE_DATE,
                "quote_valid": models.quote_valid(),
                **{
                    name: getattr(account, name) if account else 0
                    for name in ["spent_micro", "reserved_micro", "requests_used", "requests_reserved"]
                },
                "message": "离线契约不代替真实验证；能力只观察匹配当前模型、地址和适配器版本的真实回执。配置不产生调用，调用仍需项目资料与费用明确批准。费用统计为用量估算，未知须外部核对。",
            }
