from typing import Literal

from pydantic import Field, StrictInt, field_validator

from .execution_schemas import Review
from .patch_schemas import Contract

RecipeId = Literal["python_syntax", "json_validate", "static_preview"]
State = Literal[
    "queued", "starting", "running", "serving", "succeeded", "failed", "timed_out", "stopped", "interrupted"
]


class Preview(Review):
    recipe_id: RecipeId
    step_id: str | None = Field(default=None, pattern=r"^E[0-9]{1,3}$")
    files: list[str] = Field(min_length=1, max_length=5)
    timeout_seconds: StrictInt = Field(default=10, ge=1, le=30)
    port: StrictInt = Field(default=4200, ge=4200, le=4299)

    @field_validator("files")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value) or any(len(v) > 200 for v in value):
            raise ValueError("文件范围无效")
        return value


class Confirmation(Contract):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("必须明确确认")
        return value


class Start(Confirmation):
    asset_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]
    reason: str = Field(min_length=4, max_length=500)


class Stop(Confirmation):
    confirmed: Literal[True]


class Recipe(Contract):
    id: RecipeId
    name: str
    description: str
    limitations: str
    supported: Literal[True]


class FileFact(Contract):
    path: str
    size: int
    sha256: str


class Asset(Contract):
    id: str
    project_id: str
    execution_id: str
    task_id: str
    content_hash: str
    created_at: str
    recipe: Recipe
    step: dict[str, str]
    argv: list[str]
    files: list[FileFact]
    inputs: dict
    acceptance: dict[str, str]
    required_checks: list[str]
    timeout_seconds: int
    port: int | None
    root: str
    limitations: list[str]
    can_start: bool
    blockers: list[str]


class Log(Contract):
    at: str
    message: str = Field(max_length=160)


class Check(Contract):
    path: str
    valid: bool
    line: int | None
    column: int | None
    error_code: str | None


class Details(Contract):
    root: str
    task_id: str
    step_id: str
    step_instruction: str
    recipe_id: RecipeId
    argv: list[str]
    cwd: str | None
    reason: str
    authorization_id: str
    started_at: str | None
    ended_at: str | None
    exit_code: int | None
    pid: int | None
    port: int | None
    instance_id: str
    health: Literal["not_started", "starting", "healthy", "stopped", "unknown", "conflict"]
    error_code: str | None
    message: str
    stop_requested: bool
    checks: list[Check]
    artifacts: list[FileFact]
    logs: list[Log] = Field(max_length=50)
    log_status: Literal["available", "expired"]
    stdout_bytes: int
    stderr_bytes: int


class Run(Contract):
    id: str
    project_id: str
    asset_id: str
    state: State
    created_at: str
    details: Details


class Workspace(Contract):
    recipes: list[Recipe]
    assets: list[Asset]
    runs: list[Run]


class Result(Contract):
    checks: list[Check]
    input_files: list[FileFact]
    exit_code: int | None
    error_code: str | None
