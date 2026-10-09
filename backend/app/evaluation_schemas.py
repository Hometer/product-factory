from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator

from .patch_schemas import Contract

ModelSource = Literal["mock", "real_local", "real_cloud", "fallback_template"]
DataSource = Literal["real_business", "public", "synthetic"]
HardKind = Literal["incorrect_fact", "privacy_leak", "illegal_action", "unacceptable"]


class Basis(Contract):
    draft_revision: int
    draft_hash: str
    baseline_id: str | None
    baseline_hash: str | None
    model: str
    base_url: str
    prompt_version: str
    prompt_hash: str
    adapter_hash: str


class SampleInput(Contract):
    id: str = Field(pattern=r"^S(?:[1-9]|10)$")
    title: str = Field(min_length=3, max_length=100)
    data_source: DataSource
    source_ref: str = Field(min_length=4, max_length=400)
    expected: str = Field(min_length=4, max_length=3000)
    mode: Literal["operator_report", "platform_run"]
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    input_text: str = Field(default="", max_length=6000)
    output_text: str = Field(default="", max_length=6000)
    model_source: Literal["mock", "real_local", "fallback_template"] = "mock"
    model: str = Field(default="", max_length=120)
    prompt_version: str = Field(default="", max_length=160)
    knowledge_version: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def provenance(self):
        if len(self.title.strip()) < 3 or len(self.source_ref.strip()) < 4 or len(self.expected.strip()) < 4:
            raise ValueError("样例名称来源与期待不能空白")
        manual = (self.input_text, self.output_text, self.model, self.prompt_version, self.knowledge_version)
        if self.mode == "platform_run":
            if not self.run_id or any(manual) or self.model_source != "mock":
                raise ValueError("平台来源只能引用运行，不接受手工结果或来源标签")
        elif self.run_id or any(len(s.strip()) < 3 for s in manual):
            raise ValueError("人工来源需要完整输入产物与模型提示词知识版本")
        return self


class Plan(Contract):
    expected_revision: StrictInt = Field(ge=0)
    expected_basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    samples: list[SampleInput] = Field(min_length=5, max_length=10)
    dimensions: list[str] = Field(min_length=1, max_length=5)
    minimum_score: StrictFloat | None = Field(default=None, ge=1, le=3)
    context_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    reason: str = Field(min_length=4, max_length=500)

    @field_validator("dimensions")
    @classmethod
    def trimmed(cls, value):
        return [v.strip() for v in value]

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需填写有意义的变更理由")
        return value

    @model_validator(mode="after")
    def unique(self):
        if len({s.id for s in self.samples}) != len(self.samples):
            raise ValueError("样例编号重复")
        if len(set(self.dimensions)) != len(self.dimensions) or any(
            not 2 <= len(d.strip()) <= 60 for d in self.dimensions
        ):
            raise ValueError("维度需要唯一且有意义")
        return self


class Reference(Contract):
    plan_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: StrictBool
    reason: str = Field(min_length=4, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需填写判断依据")
        return value

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("需要产品负责人明确确认")
        return value


class Standard(Reference):
    expected_score_ids: list[str] = Field(max_length=10)


class HardError(Contract):
    kind: HardKind
    reason: str = Field(min_length=4, max_length=500)


class Score(Reference):
    sample_id: str = Field(pattern=r"^S(?:[1-9]|10)$")
    previous_score_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    scores: dict[str, StrictInt]
    hard_errors: list[HardError] = Field(default_factory=list, max_length=4)
    quantity_compliant: StrictBool | None = None
    quantity_reason: str = Field(default="数量约束尚未评估，不能推断遵守", min_length=4, max_length=500)

    @model_validator(mode="after")
    def valid_scores(self):
        if not 1 <= len(self.scores) <= 5 or any(not 1 <= s <= 3 for s in self.scores.values()):
            raise ValueError("分数必须严格1至3")
        if len({e.kind for e in self.hard_errors}) != len(self.hard_errors):
            raise ValueError("硬错误类别重复")
        return self


class Sample(Contract):
    id: str
    title: str
    data_source: DataSource
    source_ref: str
    expected: str
    mode: Literal["operator_report", "platform_run"]
    input_text: str
    output_text: str
    model_source: ModelSource
    model: str
    prompt_version: str
    knowledge_version: str
    run_id: str | None
    run_hash: str | None
    preview_hash: str | None
    receipts_hash: str | None
    verified_real: bool
    first_structure_valid: bool | None
    final_structure_valid: bool | None
    attempts: int | None
    cost_micro: int | None
    input_tokens: int | None
    output_tokens: int | None
    provenance_notice: str


class PlanPayload(Contract):
    schema_version: Literal[1]
    revision: int
    previous_id: str | None
    basis: Basis
    context_id: str | None
    context_hash: str | None
    samples: list[Sample]
    dimensions: list[str]
    minimum_score: float | None
    reason: str


class ScorePayload(Contract):
    schema_version: Literal[1]
    plan_id: str
    sample_id: str
    previous_score_id: str | None
    scores: dict[str, int]
    hard_errors: list[HardError]
    mean: float
    quantity_compliant: bool | None
    quantity_reason: str
    reviewer: Literal["local_operator"]
    reason: str


class StandardPayload(Contract):
    schema_version: Literal[1]
    plan_id: str
    minimum_score: float
    score_ids: list[str]
    reviewer: Literal["local_operator"]
    reason: str


class Asset(Contract):
    id: str
    project_id: str
    kind: Literal["evaluation_plan", "evaluation_score", "evaluation_standard"]
    payload: PlanPayload | ScorePayload | StandardPayload
    content_hash: str
    created_at: str
    current: bool
    issues: list[str]


class Group(Contract):
    model_source: ModelSource
    total: int
    rated: int
    hard_failed: int
    stale: int
    quantity_compliant: int
    quantity_noncompliant: int
    quantity_pending: int
    mean: float | None
    status: Literal["pending", "failed", "passed", "needs_reverification"]


class RunChoice(Contract):
    id: str
    source: str
    model: str
    revision: int


class ContextChoice(Contract):
    id: str
    stage: str
    current: bool


class Workspace(Contract):
    basis: Basis
    basis_hash: str
    plan: Asset | None
    scores: list[Asset]
    standard: Asset | None
    groups: list[Group]
    eligible_real_count: int
    blockers: list[str]
    history: list[Asset]
    runs: list[RunChoice]
    contexts: list[ContextChoice]
    notice: str
