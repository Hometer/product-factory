"""Contracts for local environment facts and manually approved planning assets."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from .schemas import StrictModel

Reference = Annotated[str, Field(max_length=16)]

Kind = Literal["environment", "technical", "plan", "technical_approval", "plan_approval"]


class EmptyInput(StrictModel):
    pass


class Check(StrictModel):
    key: str
    label: str
    status: Literal["passed", "blocked", "pending", "info"]
    result: str
    method: str
    guidance: str = ""


class EnvironmentBody(StrictModel):
    fingerprint: str
    frameworks: list[str]
    checks: list[Check]
    limitations: list[str]


class TechnicalText(StrictModel):
    understanding: str = Field(min_length=3, max_length=3000)
    recommendation: str = Field(min_length=3, max_length=3000)
    retained: str = Field(min_length=3, max_length=2000)
    modules: str = Field(min_length=3, max_length=2000)
    deviations: str = Field(min_length=3, max_length=2000)
    costs: str = Field(min_length=3, max_length=2000)
    deferred: str = Field(min_length=3, max_length=2000)
    data_flow: str = Field(min_length=3, max_length=2000)
    criteria: str = Field(min_length=3, max_length=2000)
    risks: str = Field(min_length=3, max_length=2000)

    @field_validator("*")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("方案内容需完整")
        return value.strip()


class TechnicalBody(StrictModel):
    environment_id: str
    source: Literal["rule_template", "user_edited"]
    text: TechnicalText


class Version(StrictModel):
    id: str = Field(pattern=r"^V[0-9]{1,4}$")
    name: str = Field(min_length=1, max_length=60)
    goal: str = Field(min_length=3, max_length=1000)
    depends_on: list[Reference] = Field(default_factory=list, max_length=100)


class Stage(StrictModel):
    id: str = Field(pattern=r"^S[0-9]{1,4}$")
    version_id: str = Field(max_length=16)
    name: str = Field(min_length=3, max_length=100)
    scope: str = Field(min_length=3, max_length=2000)
    out_of_scope: str = Field(min_length=3, max_length=1000)
    deliverable: str = Field(min_length=3, max_length=1000)
    depends_on: list[Reference] = Field(default_factory=list, max_length=100)
    parent_id: str | None = Field(default=None, max_length=16)


class Task(StrictModel):
    id: str = Field(pattern=r"^T[0-9]{1,4}$")
    stage_id: str = Field(max_length=16)
    title: str = Field(min_length=3, max_length=200)
    kind: Literal["requirement", "defect", "infrastructure"] = "requirement"
    requirement_ids: list[Reference] = Field(default_factory=list, max_length=100)
    defect_ref: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=1000)
    priority: Literal["P0", "P1", "P2"] = "P0"
    depends_on: list[Reference] = Field(default_factory=list, max_length=100)


class PlanText(StrictModel):
    versions: list[Version] = Field(max_length=100)
    stages: list[Stage] = Field(max_length=200)
    tasks: list[Task] = Field(max_length=300)


class PlanBody(StrictModel):
    acceptance: dict[str, str]
    required_checks: list[str]
    technical_id: str
    source: Literal["rule_template", "user_edited"]
    text: PlanText


class ApprovalBody(StrictModel):
    asset_id: str
    content_hash: str
    reason: str
    approved_by: Literal["local_operator"]


class Asset(StrictModel):
    id: str
    project_id: str
    kind: Kind
    revision: int
    baseline_id: str | None
    content_hash: str
    created_at: str
    payload: EnvironmentBody | TechnicalBody | PlanBody | ApprovalBody


class GenerateTechnical(StrictModel):
    expected_revision: int = Field(ge=0)
    baseline_id: str
    environment_id: str


class SaveTechnical(GenerateTechnical):
    text: TechnicalText


class GeneratePlan(StrictModel):
    expected_revision: int = Field(ge=0)
    technical_id: str


class SavePlan(GeneratePlan):
    text: PlanText


class ConfirmPlanning(StrictModel):
    expected_revision: int = Field(ge=1)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("需明确确认说明")
        return value.strip()


class Issue(StrictModel):
    code: str
    message: str
    object_id: str


class AssetSummary(StrictModel):
    id: str
    kind: Kind
    revision: int
    baseline_id: str | None
    created_at: str


class BaselineSummary(StrictModel):
    id: str
    revision: int
    created_at: str


class PlanningWorkspace(StrictModel):
    baselines: list[BaselineSummary]
    environment: Asset | None
    technical: Asset | None
    plan: Asset | None
    technical_approval: Asset | None
    plan_approval: Asset | None
    history: list[AssetSummary]
    technical_issues: list[Issue]
    plan_issues: list[Issue]
    notices: list[str]
    required_checks: list[str]
    acceptance: dict[str, str]
    handoff_ready: bool
    execution_supported: Literal[False]
