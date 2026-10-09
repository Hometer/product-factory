from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from .defect_schemas import Confirmed
from .patch_schemas import Contract

Money = Annotated[StrictInt, Field(ge=0, le=1_000_000_000_000)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Id = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
Scope = Literal["platform_model", "product_development", "product_runtime"]
Category = Literal["model", "execution", "storage", "deployment"]


class Policy(Confirmed):
    previous_id: Id | None
    expected_hash: Hash
    per_task_micro: Annotated[StrictInt, Field(ge=0, le=2_000_000)]
    stage_micro: Annotated[StrictInt, Field(ge=0, le=100_000_000)]
    request_limit: Annotated[StrictInt, Field(ge=0, le=100)]


class Evidence(Confirmed):
    evidence: list[str] = Field(max_length=10)

    @field_validator("evidence")
    @classmethod
    def specific(cls, values):
        if any(not 4 <= len(v.strip()) <= 500 for v in values):
            raise ValueError("证据定位需4至500字符")
        for value in values:
            value.encode("utf-8")
        return values


class Report(Evidence):
    title: str = Field(min_length=4, max_length=160)
    scope: Scope
    category: Category
    occurred_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    stage_id: Annotated[str, Field(pattern=r"^S[0-9]{1,4}$")] | None
    task_id: Annotated[str, Field(pattern=r"^T[0-9]{1,4}$")] | None
    source: Literal["manual_report", "synthetic"]
    status: Literal["pending", "reported_actual"]
    estimated_micro: Money | None
    actual_micro: Money | None
    corrects_id: Id | None
    expected_hash: Hash | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.corrects_id is None) != (self.expected_hash is None):
            raise ValueError("更正需原记录及摘要")
        if self.status == "pending" and self.actual_micro is not None:
            raise ValueError("待对账不得填写实费")
        if self.status == "reported_actual" and (self.actual_micro is None or not self.evidence):
            raise ValueError("人工实费含零也需金额及证据")
        if len(self.title.strip()) < 4:
            raise ValueError("需具体费用标题")
        return self


class Reconciliation(Evidence):
    run_id: Id
    expected_run_hash: Hash
    previous_id: Id | None
    expected_previous_hash: Hash | None
    outcome: Literal["pending", "reconciled"]
    actual_micro: Money | None
    requests_count: Annotated[StrictInt, Field(ge=0, le=2)] | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.previous_id is None) != (self.expected_previous_hash is None):
            raise ValueError("需前次记录及摘要")
        if self.outcome == "pending" and (self.actual_micro is not None or self.requests_count is not None):
            raise ValueError("待对账保留预留，不得结算")
        if self.outcome == "reconciled" and (
            self.actual_micro is None or self.requests_count is None or not self.evidence
        ):
            raise ValueError("对账需金额、已发请求次数和证据")
        return self


class Asset(Contract):
    id: str
    project_id: str
    kind: str
    payload: dict
    content_hash: str
    created_at: str
    current: bool
    association_current: bool


class Workspace(Contract):
    policy: dict
    policy_history: list[Asset]
    budget: dict
    automatic: list[dict]
    reports: list[Asset]
    reconciliations: list[Asset]
    totals: list[dict]
    periods: dict
    stages: list[dict]
    tasks: list[dict]
    notice: str
