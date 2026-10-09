"""Reviewable execution preparation, never an executor."""

from typing import Literal

from pydantic import Field, field_validator

from .planning_schemas import Check
from .schemas import StrictModel

Operation = Literal[
    "read_only", "code_write", "local_command", "model_call", "dependency_install", "deploy", "irreversible"
]


class Step(StrictModel):
    id: str = Field(pattern=r"^E[0-9]{1,3}$")
    operation: Operation
    instruction: str = Field(min_length=3, max_length=1000)
    output: str = Field(min_length=3, max_length=1000)
    validation: str = Field(min_length=3, max_length=1000)

    @field_validator("instruction", "output", "validation")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("步骤需完整")
        return value.strip()


class ExecutionText(StrictModel):
    modifications: str = Field(min_length=3, max_length=2000)
    files: list[str] = Field(max_length=20)
    steps: list[Step] = Field(min_length=1, max_length=20)
    estimate_micro: int = Field(ge=0, le=100_000_000)
    recovery: str = Field(min_length=3, max_length=2000)
    risks: str = Field(min_length=3, max_length=2000)

    @field_validator("steps")
    @classmethod
    def unique_steps(cls, value):
        if len({s.id for s in value}) != len(value):
            raise ValueError("步骤编号不可重复")
        return value

    @field_validator("files")
    @classmethod
    def file_names(cls, value):
        if any(not p or len(p) > 200 for p in value) or len(value) != len(set(value)):
            raise ValueError("文件列表无效或重复")
        return value

    @field_validator("modifications", "recovery", "risks")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("执行计划需完整")
        return value.strip()


class FileFact(StrictModel):
    path: str
    exists: bool
    size: int
    sha256: str | None


class InputVersions(StrictModel):
    baseline_id: str
    baseline_revision: int
    technical_id: str
    technical_revision: int
    environment_id: str
    planning_id: str
    planning_revision: int
    stage_id: str
    task_title: str
    requirement_ids: list[str]
    task_dependencies: list[str]
    stage_dependencies: list[str]
    version_dependencies: list[str]


class PlanPayload(StrictModel):
    context_id: str | None = None
    context_hash: str | None = None
    source: Literal["rule_template", "user_edited"]
    inputs: InputVersions
    text: ExecutionText
    files: list[FileFact]
    acceptance: dict[str, str]
    required_checks: list[str]
    authorization_hash: str


class GrantPayload(StrictModel):
    execution_id: str
    authorization_hash: str
    reason: str
    approved_by: Literal["local_operator"]
    scope: Literal["local_zero_cost"]
    decision: Literal["scope_granted"]


class RevokePayload(StrictModel):
    authorization_id: str
    reason: str
    approved_by: Literal["local_operator"]


class BudgetFacts(StrictModel):
    spent_micro: int
    reserved_micro: int
    requests_used: int
    requests_reserved: int
    stage_limit_micro: Literal[100_000_000]
    task_limit_micro: Literal[2_000_000]
    request_limit: Literal[100]


class CheckPayload(StrictModel):
    execution_id: str
    content_hash: str
    checks: list[Check]
    authorization_id: str | None
    budget: BudgetFacts
    limitations: list[str]
    can_start: Literal[False]
    execution_supported: Literal[False]
    status: Literal["blocked"]
    checked_at: str


class ExecutionAsset(StrictModel):
    id: str
    project_id: str
    task_id: str
    kind: Literal["plan", "check", "grant", "revoke"]
    revision: int
    planning_id: str
    payload: PlanPayload | CheckPayload | GrantPayload | RevokePayload
    content_hash: str
    created_at: str


class Generate(StrictModel):
    context_id: str | None = Field(default=None, max_length=100)
    planning_id: str
    task_id: str = Field(pattern=r"^T[0-9]{1,4}$")
    expected_revision: int = Field(ge=0)


class Save(Generate):
    text: ExecutionText


class Review(StrictModel):
    execution_id: str
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Authorize(Review):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("需明确确认说明")
        return value.strip()


class Revoke(StrictModel):
    authorization_id: str
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("需明确撤销说明")
        return value.strip()


class TaskChoice(StrictModel):
    id: str
    title: str
    stage_id: str


class Entry(StrictModel):
    task_id: str
    plan: ExecutionAsset
    check: ExecutionAsset | None
    authorization: ExecutionAsset | None


class Summary(StrictModel):
    id: str
    task_id: str
    kind: str
    revision: int
    created_at: str


class ContextChoice(StrictModel):
    id: str
    task_id: str
    created_at: str


class ExecutionWorkspace(StrictModel):
    context_choices: list[ContextChoice] = Field(default_factory=list)
    planning_id: str | None
    planning_revision: int | None
    tasks: list[TaskChoice]
    source_issues: list[str]
    entries: list[Entry]
    history: list[Summary]
    execution_supported: Literal[False]
