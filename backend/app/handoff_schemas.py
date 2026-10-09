from pydantic import Field

from .backup_schemas import Hash
from .defect_schemas import Confirmed
from .patch_schemas import Contract


class SaveSummary(Confirmed):
    expected_state_hash: Hash


class Correction(Confirmed):
    event_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_event_hash: Hash
    expected_chain_hash: Hash
    note: str = Field(min_length=4, max_length=1000)


class Reference(Contract):
    id: str
    kind: str
    version: str
    href: str


class Summary(Contract):
    project_id: str
    product: str
    archived: bool
    requirement: Reference | None
    draft_revision: int
    completed: list[Reference]
    pending: list[str]
    tasks: list[dict]
    authorizations: list[Reference]
    sources: list[Reference]
    conflicts: list[str]
    next_step: str
    state_hash: Hash
    notice: str


class Asset(Contract):
    id: str
    content_hash: Hash
    created_at: str
    current: bool
    issues: list[str]
    summary: Summary
    reason: str


class Event(Contract):
    id: str
    action: str
    actor: str
    role: str
    object: str
    version: str
    created_at: str
    reason: str
    missing: list[str]
    event_hash: Hash
    chain_hash: Hash
    corrections: list[dict]


class Workspace(Contract):
    current: Summary
    history: list[Asset]
    events: list[Event]
    audit_total: int
    audit_omitted: int
    notice: str
