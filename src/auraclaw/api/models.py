from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator

from auraclaw.contracts.auth import AgentSessionAuthRequest


class AgentSessionAuthInput(BaseModel):
    """Optional Java AgentSession proof supplied by the UI bridge.

    The API layer accepts camelCase and snake_case names to match browser JSON
    clients while keeping Python code idiomatic. Sensitive fields are converted
    to an in-memory request object and must not be echoed in responses. Valid
    proofs are agentSessionId plus handoffCode, or accessToken without an
    agentSessionId for compatibility resolve.
    """

    agent_session_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agentSessionId", "agent_session_id"),
        min_length=1,
    )
    conversation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("conversationId", "conversation_id"),
        min_length=1,
    )
    handoff_code: str | None = Field(
        default=None,
        validation_alias=AliasChoices("handoffCode", "handoff_code"),
        min_length=1,
    )
    access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("accessToken", "access_token"),
        min_length=1,
    )

    def agent_auth_request(self) -> AgentSessionAuthRequest | None:
        request = AgentSessionAuthRequest(
            agent_session_id=self.agent_session_id,
            conversation_id=self.conversation_id,
            handoff_code=self.handoff_code,
            access_token=self.access_token,
        )
        return None if request.is_empty else request


class CreateTaskRequest(AgentSessionAuthInput):
    goal: str = Field(min_length=1, max_length=100_000)


class CancelTaskRequest(BaseModel):
    reason: str = Field(default="cancelled by user", max_length=2_000)


class CloseSessionRequest(BaseModel):
    reason: str = Field(default="closed by user", max_length=2_000)


class AppendMessageRequest(AgentSessionAuthInput):
    message: str = Field(min_length=1, max_length=100_000)


class AgentSessionCommandRequest(AgentSessionAuthInput):
    """Body for command endpoints that only need optional auth metadata."""

    pass


class ApprovalResponseRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    feedback: str | None = Field(default=None, max_length=10_000)


class TaskAcceptedResponse(BaseModel):
    session_id: str
    run_id: str
    status: str
    status_url: str
    result_url: str
    stream_url: str


class CommandResponse(BaseModel):
    session_id: str
    run_id: str | None
    status: str
    run_status: str | None = None


class ApprovalCommandResponse(CommandResponse):
    approval_id: str
    decision: str


class TaskView(BaseModel):
    tenant_id: str
    session_id: str
    root_session_id: str
    run_id: str | None
    status: str
    run_status: str | None = None
    goal: str
    progress: float
    current_stage: str
    result_summary: str | None
    result_ref: str | None
    artifact_refs: list[Any]
    error: dict[str, Any] | None
    delivery_status: str | None = None
    delivery_id: str | None = None
    delivery_attempt_count: int = 0
    delivery_response_summary: str | None = None
    projection_version: int
    projected_at: str

    @field_validator("error", mode="before")
    @classmethod
    def coerce_error(cls, value: Any) -> dict[str, Any] | None:
        if value is None or isinstance(value, dict):
            return value
        return {"message": str(value)}


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: str | None = None
