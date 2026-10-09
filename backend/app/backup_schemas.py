from typing import Annotated

from pydantic import ConfigDict, Field, StrictBool, model_validator

from .defect_schemas import Confirmed
from .patch_schemas import Contract
from .schemas import ProjectInput


class BackupContract(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)


Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Export(Confirmed):
    expected_state_hash: Hash
    include_code: StrictBool = False


class Archive(Confirmed):
    expected_state_hash: Hash
    archived: StrictBool


class Inspect(Confirmed):
    filename: str = Field(max_length=150, pattern=r"^[^/\\\x00]+\.pfbackup\.json$")
    content: str = Field(max_length=8 * 1024 * 1024)
    target_path: str = Field(min_length=1, max_length=4096)


class Restore(Confirmed):
    asset_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: Hash


class File(BackupContract):
    path: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=0)
    sha256: Hash | None = None
    included: bool
    reason: str = Field(max_length=200)
    content: str | None = Field(default=None, max_length=512 * 1024)


class Snapshot(BackupContract):
    format: str
    version: int
    database_version: str
    source_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    project: ProjectInput
    created_at: str
    include_code: bool
    git: dict
    tables: dict[str, list[dict]]
    files: list[File] = Field(max_length=500)
    exclusions: list[dict] = Field(max_length=10000)

    @model_validator(mode="after")
    def counts(self):
        if sum(map(len, self.tables.values())) > 10000:
            raise ValueError("history limit")
        return self


class Envelope(BackupContract):
    snapshot: Snapshot
    sha256: Hash


class Asset(BackupContract):
    id: str
    kind: str
    content_hash: str
    created_at: str
    payload: dict


class Workspace(BackupContract):
    project_id: str
    archived: bool
    state_hash: str
    history: list[Asset]
    source: dict | None
    notice: str
