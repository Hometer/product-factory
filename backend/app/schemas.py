from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectInput(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=2000)
    target_user: str = Field(min_length=1, max_length=300)
    core_task: str = Field(min_length=1, max_length=1000)
    path: str = Field(min_length=1, max_length=4096)
    template_version: Literal["starter-v1"] = "starter-v1"
    delivery_target: Literal["local"] = "local"

    @field_validator("name", "target_user", "core_task", "path")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class ConfirmInput(StrictModel):
    preview_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectOutput(StrictModel):
    id: str
    name: str
    description: str
    target_user: str
    core_task: str
    path: str
    template_version: str
    delivery_target: str
    created_at: str
    revision: int
    status: Literal["registered"] = "registered"
    archived: bool = False


class ProjectList(StrictModel):
    items: list[ProjectOutput]
    total: int


class FileInfo(StrictModel):
    path: str
    size: int
    sha256: str
    status: str
    chars: int


class Manifest(StrictModel):
    files: list[FileInfo]
    warnings: list[str]
    git_present: bool
    git_changes: Literal["not_checked"] = "not_checked"


class PreviewOutput(StrictModel):
    preview_id: str
    content_hash: str
    kind: Literal["create", "import"]
    request: ProjectInput
    manifest: Manifest
    planned_files: list[str]


class SettingsOutput(StrictModel):
    allowed_roots: list[str]
    default_path: str
    model_enabled: bool


class HealthOutput(StrictModel):
    status: str
    schema_version: str
    model_enabled: bool
