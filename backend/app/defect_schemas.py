from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from .patch_schemas import Contract

State = Literal["new", "confirmed", "in_progress", "awaiting_retest", "closed", "reopened", "false_positive"]
Area = Literal["files", "interfaces", "data", "tests", "samples", "authorization", "delivery"]
AREAS = ("files", "interfaces", "data", "tests", "samples", "authorization", "delivery")
Identifier = str


class Confirmed(Contract):
    confirmed: StrictBool
    reason: str = Field(min_length=4, max_length=1000)

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if not value:
            raise ValueError("需明确确认")
        return value

    @field_validator("*")
    @classmethod
    def meaningful(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("内容不能为空白")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体理由")
        return value


class Issue(Confirmed):
    task_id: str = Field(pattern=r"^T[0-9]{1,4}$")
    version_id: str = Field(pattern=r"^V[0-9]{1,4}$")
    requirement_ids: list[str] = Field(min_length=1, max_length=30)
    title: str = Field(min_length=4, max_length=160)
    severity: Literal["critical", "high", "medium", "low"]
    reproduction_input: str = Field(min_length=4, max_length=3000)
    expected: str = Field(min_length=4, max_length=3000)
    actual: str = Field(min_length=4, max_length=3000)
    responsible: str = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def specific(self):
        for key, minimum in [
            ("title", 4),
            ("reproduction_input", 4),
            ("expected", 4),
            ("actual", 4),
            ("responsible", 2),
        ]:
            if len(getattr(self, key).strip()) < minimum:
                raise ValueError("需具体缺陷信息")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("关联需求重复")
        return self


class Reference(Confirmed):
    defect_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    previous_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Transition(Reference):
    status: State


class Retest(Reference):
    report_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduction_input: str = Field(min_length=4, max_length=3000)
    expected: str = Field(min_length=4, max_length=3000)
    actual: str = Field(min_length=4, max_length=3000)

    @field_validator("actual")
    @classmethod
    def actual_specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体复测实际结果")
        return value


class AreaReview(Contract):
    status: Literal["affected", "not_affected", "unknown"]
    reason: str = Field(min_length=4, max_length=1000)

    @field_validator("reason")
    @classmethod
    def specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体复核依据")
        return value


class ConclusionReview(Contract):
    asset_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    status: Literal["invalid", "needs_reverification", "inheritable", "unknown"]
    reason: str = Field(min_length=4, max_length=1000)

    @field_validator("reason")
    @classmethod
    def specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体结论依据")
        return value


class Impact(Confirmed):
    source_kind: Literal["requirement_revision", "patch"]
    source_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_id: str | None
    areas: dict[Area, AreaReview]
    conclusions: list[ConclusionReview] = Field(max_length=500)

    @model_validator(mode="after")
    def complete(self):
        if set(self.areas) != set(AREAS):
            raise ValueError("七类影响不能省略")
        if len({c.asset_id for c in self.conclusions}) != len(self.conclusions):
            raise ValueError("结论引用重复")
        return self


class Payload(Contract):
    schema_version: Literal[1]
    defect_id: str | None = None
    previous_id: str | None = None
    issue: dict | None = None
    status: State | None = None
    counterexample_hash: str | None = None
    report_id: str | None = None
    report_hash: str | None = None
    report_source: str | None = None
    report_status: str | None = None
    reproduction_input: str | None = None
    expected: str | None = None
    actual: str | None = None
    index_hash: str | None = None
    retest_id: str | None = None
    source_kind: str | None = None
    source_id: str | None = None
    snapshot_hash: str | None = None
    snapshot: dict | None = None
    areas: dict[Area, AreaReview] | None = None
    conclusions: list[ConclusionReview] | None = None
    reason: str
    actor: Literal["local_operator"]
    role: Literal["product_owner"]


class Asset(Contract):
    id: str
    project_id: str
    kind: Literal["defect_issue", "defect_transition", "defect_retest", "impact_review"]
    payload: Payload
    content_hash: str
    created_at: str
    current: bool
    issues: list[str]


class DefectView(Contract):
    id: str
    issue: dict
    state: Asset
    retests: list[Asset]
    history: list[Asset]
    current: bool
    issues: list[str]


class ImpactView(Contract):
    source_kind: Literal["requirement_revision", "patch"]
    source_id: str
    label: str
    snapshot: dict
    snapshot_hash: str
    files: list[str]
    requirement_ids: list[str]
    areas: dict[Area, AreaReview]
    conclusions: list[dict]
    review: Asset | None
    current: bool
    issues: list[str]


class Workspace(Contract):
    tasks: list[dict]
    stages: list[dict]
    versions: list[dict]
    requirement_ids: list[str]
    defects: list[DefectView]
    reports: list[dict]
    impacts: list[ImpactView]
    history: list[Asset]
    notice: str
