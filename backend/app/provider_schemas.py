from typing import Literal

from pydantic import Field

from .patch_schemas import Contract


class Receipt(Contract):
    id: str
    project_id: str
    run_id: str
    attempt: int = Field(ge=1, le=2)
    source: Literal["mock", "real_cloud"]
    model: str
    base_url: str
    contract_version: str
    adapter_hash: str
    checked_at: str
    request_id: str | None
    elapsed_ms: int = Field(ge=0)
    sent: bool
    text_valid: bool
    structure_valid: bool
    usage_valid: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_micro: int | None
    error_code: str | None


class Capability(Contract):
    id: Literal["text", "structured", "streaming", "code", "usage", "query"]
    name: str
    implemented: bool
    offline_contract: Literal["verified", "not_supported"]
    real_verified_count: int
    can_run: bool
    explanation: str


class ProviderView(Contract):
    provider: str
    model: str
    base_url: str
    configured: bool
    key_mask: str
    contract_version: str
    adapter_hash: str
    automatic_switch: Literal[False]
    max_attempts: Literal[2]
    blockers: list[str]
    capabilities: list[Capability]
    receipts: list[Receipt]
    receipt_total: int
    quote_date: str
    quote_valid: bool
    spent_micro: int
    reserved_micro: int
    requests_used: int
    requests_reserved: int
    message: str
