from typing import Literal

from pydantic import Field, StrictBool, StrictInt, field_validator

from .patch_schemas import Contract

Status = Literal["passed", "failed", "warning", "pending", "not_applicable", "needs_reverification"]


DeclarationKind = Literal["scope", "secrets", "readme", "project_state"]


class Declaration(Contract):
    status: Literal["passed", "failed", "pending"]
    reason: str = Field(min_length=4, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("声明需具体依据")
        return value

    @field_validator("evidence")
    @classmethod
    def locators(cls, value):
        if any(not 4 <= len(s.strip()) <= 500 for s in value):
            raise ValueError("证据定位需具体")
        return value


class PackageInput(Contract):
    stage_id: str = Field(pattern=r"^S[0-9]{1,4}$")
    expected_revision: StrictInt = Field(ge=0)
    expected_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    declarations: dict[DeclarationKind, Declaration]
    confirmed: StrictBool
    steps: list[str] = Field(min_length=1, max_length=10)
    limitations: str = Field(min_length=4, max_length=3000)
    reason: str = Field(min_length=4, max_length=500)

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("提交声明需明确确认")
        return value

    @field_validator("declarations")
    @classmethod
    def complete(cls, value):
        if set(value) != {"scope", "secrets", "readme", "project_state"}:
            raise ValueError("必须保留四项交付声明")
        if any(d.status != "pending" and not d.evidence for d in value.values()):
            raise ValueError("已检查声明必须有证据定位")
        return value

    @field_validator("steps")
    @classmethod
    def meaningful_steps(cls, value):
        if any(not 8 <= len(s.strip()) <= 1000 for s in value):
            raise ValueError("核对步骤需要可操作内容")
        return value

    @field_validator("reason", "limitations")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("理由和限制需具体填写")
        return value


class DecisionInput(Contract):
    package_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_decision_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    outcome: Literal["passed", "rejected", "deferred"]
    confirmed: StrictBool
    reason: str = Field(min_length=4, max_length=1000)

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("需要责任人明确确认")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体验收依据")
        return value


class Stage(Contract):
    id: str
    name: str
    scope: str
    out_of_scope: str
    deliverable: str
    parent_id: str | None
    version_id: str
    depends_on: list[str]


class Task(Contract):
    id: str
    title: str
    stage_id: str
    kind: str
    requirement_ids: list[str]
    depends_on: list[str]


class Check(Contract):
    key: str
    task_id: str | None
    label: str
    status: Status
    criterion: str
    source: str
    source_id: str | None
    reason: str
    responsible: str | None
    passed: int | None = None
    total: int | None = None
    failed: int | None = None


class Coverage(Contract):
    requirement_id: str
    acceptance: str
    task_ids: list[str]
    status: Literal["linked", "missing"]


class Evidence(Contract):
    id: str
    kind: str
    content_hash: str
    current: bool
    issues: list[str]
    source: str
    run_id: str | None
    href: str


class Snapshot(Contract):
    draft_revision: int
    draft_hash: str
    baseline_id: str | None
    baseline_hash: str | None
    planning_id: str
    planning_hash: str
    approval_id: str | None
    approval_hash: str | None
    technical_id: str
    technical_hash: str
    technical_approval_id: str | None
    technical_approval_hash: str | None
    task_indexes: dict[str, str]
    evidence_hashes: dict[str, str]
    evaluation_basis: dict
    dependencies: dict[str, str | None]


class PackagePayload(Contract):
    schema_version: Literal[1]
    revision: int
    previous_id: str | None
    stage: Stage
    tasks: list[Task]
    snapshot: Snapshot
    snapshot_hash: str
    matrix: list[Check]
    coverage: list[Coverage]
    evidence: list[Evidence]
    steps: list[str]
    limitations: str
    reason: str
    declarations: dict[DeclarationKind, Declaration]
    submitted_by: Literal["local_operator"]
    role: Literal["product_owner"]


class DecisionPayload(Contract):
    schema_version: Literal[1]
    package_id: str
    package_hash: str
    stage_id: str
    previous_decision_id: str | None
    outcome: Literal["passed", "rejected", "deferred"]
    reason: str
    reviewer: Literal["local_operator"]
    role: Literal["product_owner"]


class Asset(Contract):
    id: str
    project_id: str
    kind: Literal["acceptance_package", "acceptance_decision"]
    payload: PackagePayload | DecisionPayload
    content_hash: str
    created_at: str
    current: bool
    issues: list[str]
    blockers: list[str]


class Workspace(Contract):
    stages: list[Stage]
    stage: Stage | None
    tasks: list[Task]
    snapshot: Snapshot | None
    snapshot_hash: str | None
    matrix: list[Check]
    coverage: list[Coverage]
    evidence: list[Evidence]
    blockers: list[str]
    package: Asset | None
    decision: Asset | None
    history: list[Asset]
    notice: str
