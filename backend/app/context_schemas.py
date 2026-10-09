"""Local context contracts. Context is data, never an authorization."""

from typing import Literal

from pydantic import Field, field_validator

from .schemas import StrictModel

Stage = Literal["requirements", "planning", "execution"]


class Basis(StrictModel):
    draft_revision: int
    baseline_id: str | None
    technical_id: str | None
    planning_id: str | None


class Source(StrictModel):
    id: str
    kind: Literal["requirement", "baseline", "decision", "technical", "plan", "task", "history", "file"]
    title: str
    version: str
    sha256: str
    locator: str
    href: str
    classification: Literal["recorded_fact", "inference"]
    provenance: str
    status: Literal["ready", "excluded", "unsupported"]
    chars: int


class Task(StrictModel):
    id: str
    title: str
    stage_id: str


class Summary(StrictModel):
    id: str
    stage: Stage
    task_id: str | None
    created_at: str
    content_hash: str


class Index(StrictModel):
    stage: Stage
    task_id: str | None
    basis: Basis
    sources: list[Source]
    index_hash: str
    tasks: list[Task]
    history: list[Summary]
    warnings: list[str]
    notice: str


class Create(StrictModel):
    stage: Stage
    task_id: str | None = Field(default=None, pattern=r"^T[0-9]{1,4}$")
    expected_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_ids: list[str] = Field(min_length=1, max_length=30)
    max_chars: int = Field(default=8000, ge=1000, le=20000)

    @field_validator("selected_ids")
    @classmethod
    def identifiers(cls, value):
        if len(set(value)) != len(value) or any(not v or len(v) > 300 for v in value):
            raise ValueError("资料选择重复或无效")
        return value


class Entry(StrictModel):
    source: Source
    content: str
    loaded_chars: int


class Omission(StrictModel):
    source_id: str
    title: str
    reason: Literal["not_selected", "excluded", "unsupported", "source_limit", "context_limit"]
    omitted_chars: int


class Payload(StrictModel):
    schema_version: Literal[1]
    stage: Stage
    task_id: str | None
    basis: Basis
    source_index_hash: str
    selected_ids: list[str]
    max_chars: int
    loaded_chars: int
    entries: list[Entry]
    omissions: list[Omission]
    notice: str


class Bundle(StrictModel):
    id: str
    project_id: str
    content_hash: str
    created_at: str
    payload: Payload
    current: bool
    issues: list[str]


class SourceView(StrictModel):
    source: Source
    content: str
    omitted_chars: int
