"""Strict contracts for local, append-only artifact/version records."""

from typing import Literal

from pydantic import Field, StrictBool, field_validator

from .defect_schemas import Confirmed
from .patch_schemas import Contract

Kind = Literal[
    "requirement",
    "solution",
    "development_document",
    "api_document",
    "code_change",
    "build_artifact",
    "screenshot",
    "evaluation_set",
    "delivery_package",
]
Status = Literal["draft", "candidate", "adopted", "superseded", "unavailable"]


class Association(Confirmed):
    version_id: str = Field(pattern=r"^V[0-9]{1,4}$")
    task_id: str = Field(pattern=r"^T[0-9]{1,4}$")
    requirement_ids: list[str] = Field(min_length=1, max_length=30)

    @field_validator("requirement_ids")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value) or any(not v.startswith("R") for v in value):
            raise ValueError("需求编号无效或重复")
        return value


class Entry(Association):
    kind: Kind
    title: str = Field(min_length=4, max_length=160)
    source_id: str = Field(min_length=1, max_length=300)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    relocates_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    sensitive_checked: StrictBool

    @field_validator("title")
    @classmethod
    def title_specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体标题")
        return value

    @field_validator("sensitive_checked")
    @classmethod
    def privacy(cls, value):
        if not value:
            raise ValueError("需检查隐私")
        return value


class Decision(Confirmed):
    entry_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    previous_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Status
    replacement_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class Version(Association):
    files: list[str] = Field(min_length=1, max_length=20)
    label: str = Field(min_length=4, max_length=160)
    expected_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)

    @field_validator("label")
    @classmethod
    def label_specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体版本说明")
        return value

    @field_validator("files")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("文件重复")
        return value


class Rollback(Confirmed):
    version_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility: Literal["unknown", "compatible", "incompatible", "not_applicable"]
    compatibility_reason: str = Field(min_length=4, max_length=1000)

    @field_validator("compatibility_reason")
    @classmethod
    def evidence_specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("需具体兼容性依据")
        return value


class Asset(Contract):
    id: str
    project_id: str
    kind: str
    payload: dict
    content_hash: str
    created_at: str
    current: bool
    issues: list[str]
    status: Status | None
    state_id: str | None
    state_hash: str | None


class Workspace(Contract):
    tasks: list[dict]
    versions: list[dict]
    sources: list[dict]
    snapshot_files: list[str]
    index_hash: str
    code_revision: int
    git: dict
    entries: list[Asset]
    history: list[Asset]
    code_versions: list[Asset]
    rollback_previews: list[Asset]
    notice: str
