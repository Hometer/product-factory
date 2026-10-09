from typing import Annotated, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from .defect_schemas import Confirmed
from .patch_schemas import Contract

Component = Literal[
    "rule_card",
    "prd_structure",
    "stage_strategy",
    "technical_manual",
    "task_recipes",
    "acceptance_checks",
    "output_schema",
]
Id = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9.-]+$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=4, max_length=6000)]


class Draft(Confirmed):
    version: str = Field(pattern=r"^local-\d{1,3}\.\d{1,3}\.\d{1,3}$")
    compatibility: str = Field(min_length=4, max_length=50)
    change_notes: Text
    module_policy: Literal["requirements_triggered"]
    storage_policy: Literal["project_choice"]
    terminal_policy: Literal["project_choice"]
    components: dict[Component, Text]

    @field_validator("components")
    @classmethod
    def utf8_components(cls, values):
        if len(values) != 7:
            raise ValueError("七类模板需完整")
        for s in values.values():
            s.encode("utf-8")
            if len(s.strip()) < 4:
                raise ValueError("模板正文不足")
        return values


class Reference(Confirmed):
    asset_id: Id
    expected_hash: Hash


class Publication(Reference):
    validation_id: Id
    validation_hash: Hash


class Customization(Confirmed):
    expected_state_hash: Hash
    component: Component
    action: Literal["set", "reset"]
    content: Text | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.action == "set") != (self.content is not None):
            raise ValueError("设置需正文；恢复公共版本不填正文")
        return self


class Preview(Confirmed):
    expected_state_hash: Hash
    target_id: Id
    target_hash: Hash
    selected_components: list[Component] = Field(min_length=1, max_length=7)

    @field_validator("selected_components")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("组件重复")
        return values


class Decision(Reference):
    outcome: Literal["apply", "defer"]


class TaskReview(Contract):
    task_id: str = Field(pattern=r"^T\d{1,4}$")
    status: Literal["inheritable", "needs_reverification", "unknown"]
    reason: Text

    @field_validator("reason")
    @classmethod
    def specific(cls, s):
        if len(s.strip()) < 4:
            raise ValueError("需具体复核依据")
        return s


class Review(Confirmed):
    expected_review_hash: Hash
    tasks: list[TaskReview] = Field(max_length=300)
    old_acceptance_acknowledged: StrictBool

    @field_validator("old_acceptance_acknowledged")
    @classmethod
    def explicit(cls, value):
        if not value:
            raise ValueError("需确认旧验收保留并另行复验")
        return value


class Asset(Contract):
    id: str
    project_id: str
    kind: str
    payload: dict
    content_hash: str
    created_at: str


class Workspace(Contract):
    catalogue: list[dict]
    binding: Asset | None
    customization: Asset | None
    components: dict[Component, str]
    origins: dict
    state_hash: str
    stamp: dict | None
    review_hash: str
    source_hash: str
    tasks: list[dict]
    blockers: list[str]
    history: list[Asset]
    notice: str
