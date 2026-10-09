from typing import Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from .context_schemas import Index, Source
from .patch_schemas import Contract

Kind = Literal["tests", "types", "lint", "build", "api", "recovery", "ui"]
Status = Literal["passed", "failed", "warning", "pending", "not_applicable"]
KINDS = ("tests", "types", "lint", "build", "api", "recovery", "ui")


class Plan(Contract):
    task_id: str = Field(pattern=r"^T[0-9]{1,4}$")
    expected_revision: StrictInt = Field(ge=0)
    expected_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[str] = Field(min_length=1, max_length=20)
    criteria: dict[Kind, str]
    reason: str = Field(min_length=4, max_length=500)

    @model_validator(mode="after")
    def complete(self):
        if len(set(self.files)) != len(self.files) or any(len(f) > 240 for f in self.files):
            raise ValueError("文件范围无效")
        if set(self.criteria) != set(KINDS) or any(
            not 4 <= len(v.strip()) <= 500 for v in self.criteria.values()
        ):
            raise ValueError("需保留七项检查及具体要求")
        return self


class Reference(Contract):
    plan_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Report(Reference):
    kind: Kind
    status: Status
    executed: StrictBool
    confirmed: Literal[True]
    method: str = Field(min_length=4, max_length=500)
    environment: str = Field(min_length=4, max_length=500)
    responsible: str = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=4, max_length=500)
    passed: StrictInt | None = Field(default=None, ge=0, le=1000000)
    total: StrictInt | None = Field(default=None, ge=0, le=1000000)
    failed: StrictInt | None = Field(default=None, ge=0, le=1000000)
    failures: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("需明确确认报告")
        return value

    @model_validator(mode="after")
    def consistency(self):
        if type(self.confirmed) is not bool:
            raise ValueError("需明确确认报告")
        if any(not 1 <= len(s.strip()) <= 500 for s in self.failures + self.warnings + self.evidence):
            raise ValueError("说明长度无效")
        counts = (self.passed, self.total, self.failed)
        if any(n is not None for n in counts):
            if (
                self.passed is None
                or self.failed is None
                or self.total is None
                or self.passed + self.failed != self.total
            ):
                raise ValueError("计数需完整且通过加失败等于总数")
        if self.executed != (self.status not in {"pending", "not_applicable"}):
            raise ValueError("未运行只能待验或有依据的不适用")
        if not self.executed and (any(n not in (None, 0) for n in counts) or self.failures or self.warnings):
            raise ValueError("未运行不能填写执行结果")
        if self.status == "passed" and (self.failures or self.warnings or self.failed not in (None, 0)):
            raise ValueError("失败或警告不能写为通过")
        if self.status == "failed" and not self.failures:
            raise ValueError("失败需保留失败说明")
        if self.status == "warning" and (not self.warnings or self.failures or self.failed not in (None, 0)):
            raise ValueError("警告需说明，不能隐藏失败")
        if self.kind == "tests" and self.executed and (self.total is None or self.total == 0):
            raise ValueError("测试报告需实际通过数和总数")
        if self.kind == "tests" and self.status == "failed" and not self.failed:
            raise ValueError("失败测试需记录失败数量")
        if self.executed and not self.evidence:
            raise ValueError("已执行报告需证据定位")
        return self


class Import(Reference):
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class PlanPayload(Contract):
    schema_version: Literal[1]
    revision: int
    previous_id: str | None
    task_id: str
    execution_id: str | None
    basis: dict
    index_hash: str
    files: list[Source]
    criteria: dict[Kind, str]
    environment: dict[str, str | None]
    reason: str


class ResultPayload(Contract):
    schema_version: Literal[1]
    plan_id: str
    task_id: str
    source: Literal["operator_report", "platform_run"]
    kind: Literal["tests", "types", "lint", "build", "api", "recovery", "ui", "syntax"]
    status: Status
    executed: bool
    passed: int | None
    total: int | None
    failed: int | None
    failures: list[str]
    warnings: list[str]
    evidence: list[str]
    method: str
    environment: str
    responsible: str
    reason: str
    run_id: str | None = None
    run_hash: str | None = None
    run_hash_version: Literal[1, 2] = 1
    artifact_hash: str | None = None


class Asset(Contract):
    id: str
    project_id: str
    kind: Literal["verification_plan", "verification_result"]
    content_hash: str
    created_at: str
    payload: PlanPayload | ResultPayload
    current: bool
    issues: list[str]


class Matrix(Contract):
    kind: Kind
    criterion: str
    status: Status
    source: Literal["operator_report"] | None
    result_id: str | None
    current: bool
    issues: list[str]


class RunChoice(Contract):
    id: str
    task_id: str
    recipe_id: str
    state: str


class Workspace(Contract):
    index: Index
    plan: Asset | None
    matrix: list[Matrix]
    supplemental: list[Asset]
    history: list[Asset]
    runs: list[RunChoice]
    notice: str
