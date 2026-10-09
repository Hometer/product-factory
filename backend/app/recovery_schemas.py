from typing import Literal

from pydantic import Field

from .command_schemas import Confirmation, FileFact, Run
from .patch_schemas import Contract

Verdict = Literal[
    "not_executed", "result_unknown", "completed", "active", "conflict", "stopped", "paused", "cancelled"
]


class Review(Confirmation):
    reason: str = Field(min_length=4, max_length=500)


class Resume(Review):
    checkpoint_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Checkpoint(Contract):
    id: str
    created_at: str
    phase: Literal[
        "queued",
        "launch_intent",
        "executing",
        "finished",
        "reconciled",
        "reviewed",
        "resumed",
        "pause_requested",
        "cancel_requested",
        "paused",
        "cancelled",
        "continued",
        "retry_approved",
    ]
    state: str
    verdict: Verdict
    run_hash: str
    reason: str
    exit_code: int | None
    artifacts: list[FileFact]


class Task(Contract):
    run: Run
    verdict: Verdict
    message: str
    completed_steps: int
    total_steps: Literal[1]
    step_state: Literal["pending", "running", "succeeded", "failed", "unknown", "stopped"]
    checkpoint_id: str | None
    expected_hash: str
    can_resume: bool
    blockers: list[str]
    checkpoints: list[Checkpoint]
    checkpoint_count: int
    asset_hash: str
    authorization_id: str


class Attempt(Contract):
    attempt: int
    state: str


class Analysis(Contract):
    id: str
    state: str
    attempts: list[Attempt]
    reserved_micro: int
    settled_micro: int
    message: str
    can_resume: Literal[False]


class Workspace(Contract):
    tasks: list[Task]
    analyses: list[Analysis]
    task_count: int
    analysis_count: int
    message: str
