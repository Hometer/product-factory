from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .execution_schemas import Review


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def utf8(cls, value):
        if isinstance(value, str):
            value.encode("utf-8")
        return value


class Candidate(Contract):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=65536)


class Preview(Review):
    files: list[Candidate] = Field(min_length=1, max_length=5)
    validation_plan: str = Field(min_length=4, max_length=4000)

    @field_validator("validation_plan")
    @classmethod
    def utf8(cls, value):
        value.encode("utf-8")
        return value

    @field_validator("files")
    @classmethod
    def unique(cls, files):
        if len({f.path for f in files}) != len(files):
            raise ValueError("文件重复")
        return files


class Apply(Contract):
    patch_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: StrictBool
    reason: str = Field(min_length=4, max_length=600)


class Recover(Contract):
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    confirmed: StrictBool


class FilePatch(Contract):
    path: str
    before: str | None
    after: str
    diff: str
    mode: int
    parent_identity: list[int]


class Payload(Contract):
    source: Literal["manual_input"]
    task_id: str
    execution_hash: str
    authorization_hash: str
    inputs: dict
    acceptance: dict[str, str]
    required_checks: list[str]
    validation_plan: str
    root: str
    root_identity: list[int]
    files: list[FilePatch]


class Asset(Contract):
    id: str
    project_id: str
    execution_id: str
    payload: Payload
    content_hash: str
    created_at: str
    can_apply: bool
    blockers: list[str]


class RunDetails(Contract):
    message: str
    completed: list[str]
    reason: str
    authorization_id: str | None


class Run(Contract):
    id: str
    project_id: str
    patch_id: str
    state: Literal["preparing", "applying", "applied", "failed", "rolled_back", "rollback_required"]
    details: RunDetails
    created_at: str


class Workspace(Contract):
    assets: list[Asset]
    runs: list[Run]
