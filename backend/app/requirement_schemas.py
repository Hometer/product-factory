from typing import Literal

from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel

FieldKey = Literal[
    "user_task", "input_output", "workflow", "quality", "priority", "model_cost", "data", "launch"
]
FIELD_LABELS = {
    "user_task": "目标用户与核心任务",
    "input_output": "输入、过程和产物",
    "workflow": "Agent 主链路",
    "quality": "效果判断",
    "priority": "交付阶段与优先级",
    "model_cost": "模型与成本约束",
    "data": "数据与非功能要求",
    "launch": "上线与账号",
}


class RequirementField(StrictModel):
    key: FieldKey
    status: Literal["known", "unknown", "not_applicable"] = "unknown"
    content: str = Field(default="", max_length=3000)
    source: Literal["user", "project", "ai_suggestion"] = "user"
    source_ref: str = Field(default="", max_length=160)
    confirmed_by: Literal["", "local_operator"] = ""

    @model_validator(mode="after")
    def meaningful(self):
        if self.status != "unknown" and len(self.content.strip()) < 3:
            raise ValueError("已知或不适用需填写依据")
        return self


class RequirementItem(StrictModel):
    id: str = Field(pattern=r"^R[0-9]{1,4}$")
    value: str = Field(min_length=3, max_length=600)
    trigger: str = Field(min_length=3, max_length=600)
    rule: str = Field(min_length=3, max_length=1000)
    exception: str = Field(min_length=3, max_length=600)
    acceptance: str = Field(min_length=3, max_length=1000)
    priority: Literal["P0", "P1", "P2"] = "P0"
    target_version: str = Field(min_length=1, max_length=60)
    source_ref: str = Field(min_length=1, max_length=160)

    @field_validator("value", "trigger", "rule", "exception", "acceptance")
    @classmethod
    def meaningful_item(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("条目内容不足")
        return value.strip()

    @field_validator("target_version", "source_ref")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("版本与来源不能为空")
        return value.strip()


class DraftBody(StrictModel):
    fields: list[RequirementField] = Field(min_length=8, max_length=8)
    items: list[RequirementItem] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_keys(self):
        if {f.key for f in self.fields} != set(FIELD_LABELS):
            raise ValueError("八字段需唯一且齐全")
        if len({i.id for i in self.items}) != len(self.items):
            raise ValueError("条目编号重复")
        return self


class SaveDraft(DraftBody):
    expected_revision: int = Field(ge=0)


class RevisionInput(StrictModel):
    expected_revision: int = Field(ge=0)


class Gap(StrictModel):
    id: str
    field: str
    message: str
    severity: Literal["blocking", "deferred"]
    stage: str
    recommendation: str
    owner: Literal["local_operator"] = "local_operator"
    status: Literal["open", "deferred"]


class DraftOutput(DraftBody):
    project_id: str
    revision: int
    content_hash: str
    saved_at: str | None
    gaps: list[Gap]


class Question(StrictModel):
    id: FieldKey
    question: str
    recommendation: str
    example: str
    impact: str
    recommended_status: Literal["unknown", "not_applicable"] = "unknown"


class RoundOutput(StrictModel):
    id: str
    project_id: str
    revision: int
    version: str
    questions: list[Question] = Field(max_length=5)
    answered: bool


class Answer(StrictModel):
    question_id: FieldKey
    mode: Literal["custom", "recommended", "uncertain"]
    content: str = Field(default="", max_length=3000)


class AnswerInput(RevisionInput):
    round_id: str
    question_version: str
    answers: list[Answer] = Field(min_length=1, max_length=5)


class FreezeInput(RevisionInput):
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=3, max_length=500)


class BaselineOutput(StrictModel):
    id: str
    project_id: str
    revision: int
    content_hash: str
    fields: list[RequirementField]
    items: list[RequirementItem]
    deferred: list[Gap]
    approved_by: Literal["local_operator"]
    reason: str
    created_at: str


class DecisionOutput(StrictModel):
    id: str
    question: str
    conclusion: str
    status: Literal["pending", "confirmed", "replaced"]
    revision: int
    source: str
    created_at: str


class WorkspaceOutput(StrictModel):
    draft: DraftOutput
    baselines: list[BaselineOutput]
    round: RoundOutput | None
    decisions: list[DecisionOutput]
    revisions: list[int]


class ModelConfigInput(StrictModel):
    api_key: str = Field(min_length=20, max_length=256, repr=False)
    base_url: str = Field(max_length=300)

    @field_validator("api_key")
    @classmethod
    def safe_key(cls, value):
        if not value.isascii() or any(c.isspace() for c in value) or any(c in value for c in "'\"\\"):
            raise ValueError("配置格式无效")
        return value


class ModelStatus(StrictModel):
    configured: bool
    model: str
    base_url: str
    quote_valid: bool
    quote_date: str
    spent_micro: int
    reserved_micro: int
    requests_used: int
    requests_reserved: int


class AnalysisPreviewInput(RevisionInput):
    selected_fields: list[FieldKey] = Field(min_length=1, max_length=8)


class SelectedField(StrictModel):
    key: FieldKey
    content: str
    status: Literal["known", "unknown", "not_applicable"]


class AnalysisPreviewOutput(StrictModel):
    id: str
    project_id: str
    revision: int
    content_hash: str
    model: str
    base_url: str
    selected_fields: list[SelectedField]
    omitted_fields: list[str]
    max_attempts: int
    reserve_micro: int
    expires_at: str


class AuthorizationInput(StrictModel):
    preview_id: str
    expected_hash: str
    nonsensitive_confirmed: Literal[True]

    @field_validator("nonsensitive_confirmed", mode="before")
    @classmethod
    def explicit_true(cls, value):
        if value is not True:
            raise ValueError("需明确确认资料范围")
        return value


class AuthorizationOutput(StrictModel):
    id: str
    project_id: str
    preview_id: str
    content_hash: str
    max_attempts: int
    reserve_micro: int
    expires_at: str
    approved_by: Literal["local_operator"]
    revoked: bool


class RunInput(RevisionInput):
    preview_id: str
    authorization_id: str


class Suggestion(StrictModel):
    field: FieldKey
    content: str = Field(min_length=3, max_length=3000)
    rationale: str = Field(min_length=3, max_length=600)
    source_fields: list[FieldKey] = Field(min_length=1, max_length=8)


class AIQuestion(StrictModel):
    question: str = Field(min_length=3, max_length=600)
    recommendation: str = Field(min_length=3, max_length=600)
    example: str = Field(min_length=3, max_length=600)
    impact: str = Field(min_length=3, max_length=600)


class AnalysisCandidate(StrictModel):
    summary: str = Field(min_length=3, max_length=2000)
    suggestions: list[Suggestion] = Field(max_length=8)
    questions: list[AIQuestion] = Field(max_length=5)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({s.field for s in self.suggestions}) != len(self.suggestions):
            raise ValueError("建议字段重复")
        return self


class AttemptOutput(StrictModel):
    request_id: str | None = None
    elapsed_ms: int | None = None
    attempt: int
    state: Literal["dispatching", "settled", "unknown", "not_sent"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_micro: int | None = None
    error_code: str | None = None


class RunOutput(StrictModel):
    id: str
    project_id: str
    revision: int
    state: Literal["queued", "running", "succeeded", "failed", "result_unknown"]
    source: Literal["real_cloud", "mock"]
    model: str
    prompt_version: str
    authorization_id: str
    candidate: AnalysisCandidate | None
    error: dict[str, str] | None
    attempts: list[AttemptOutput]
    reserved_micro: int
    settled_micro: int
    started_at: str | None
    finished_at: str | None


class AdoptInput(RevisionInput):
    fields: list[FieldKey] = Field(min_length=1, max_length=8)


class RunList(StrictModel):
    items: list[RunOutput]
