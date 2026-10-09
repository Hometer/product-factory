from typing import Literal

from pydantic import Field

from .command_schemas import Confirmation, Run
from .patch_schemas import Contract


class Action(Confirmation):
    reason: str = Field(min_length=4, max_length=500)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Request(Contract):
    kind: Literal["pause", "cancel"]
    reason: str
    requested_at: str
    effect: Literal["pending", "applied", "step_completed", "unknown"]
    effective_at: str | None
    process_started: bool | None


class Entry(Contract):
    run: Run
    root_id: str
    parent_id: str | None
    attempt: int
    max_attempts: Literal[3]
    cost_micro: Literal[0]
    request: Request | None
    expected_hash: str
    can_pause: bool
    can_cancel: bool
    can_continue: bool
    can_retry: bool
    blockers: list[str]


class Workspace(Contract):
    entries: list[Entry]
    total: int
    message: str
