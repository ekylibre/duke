from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class ErrorCode(StrEnum):
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_TENANT_UNKNOWN = "AUTH_TENANT_UNKNOWN"
    EKYLIBRE_UNAVAILABLE = "EKYLIBRE_UNAVAILABLE"
    EKYLIBRE_API_ERROR = "EKYLIBRE_API_ERROR"
    EKYLIBRE_DB_UNAVAILABLE = "EKYLIBRE_DB_UNAVAILABLE"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- Client → Server ----------


class AuthMessage(_StrictModel):
    type: Literal["auth"] = "auth"
    email: str
    token: str
    tenant: str
    locale: str = "fr"


class UserMessage(_StrictModel):
    type: Literal["user_message"] = "user_message"
    id: str
    text: str


class ConfirmInterventionMessage(_StrictModel):
    type: Literal["confirm_intervention"] = "confirm_intervention"
    id: str
    draft: dict[str, Any]


class ClarifyMessage(_StrictModel):
    type: Literal["clarify"] = "clarify"
    id: str
    answer: str


class CancelMessage(_StrictModel):
    type: Literal["cancel"] = "cancel"
    id: str


class PingMessage(_StrictModel):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[
    AuthMessage
    | UserMessage
    | ConfirmInterventionMessage
    | ClarifyMessage
    | CancelMessage
    | PingMessage,
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---------- Server → Client ----------


class AuthOkMessage(_StrictModel):
    type: Literal["auth_ok"] = "auth_ok"
    user: dict[str, Any]
    tenant_label: str
    capabilities: list[str] = Field(default_factory=list)
    llm_provider: str | None = None


class AuthErrorMessage(_StrictModel):
    type: Literal["auth_error"] = "auth_error"
    code: ErrorCode
    message: str


class ThinkingMessage(_StrictModel):
    type: Literal["thinking"] = "thinking"
    id: str


class AssistantTokenMessage(_StrictModel):
    type: Literal["assistant_token"] = "assistant_token"
    id: str
    delta: str


class AssistantMessage(_StrictModel):
    type: Literal["assistant_message"] = "assistant_message"
    id: str
    text: str
    final: bool = True


class InterventionDraftMessage(_StrictModel):
    type: Literal["intervention_draft"] = "intervention_draft"
    id: str
    fields: dict[str, Any]
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float


class ClarificationNeededMessage(_StrictModel):
    type: Literal["clarification_needed"] = "clarification_needed"
    id: str
    question: str
    options: list[str] | None = None


class InterventionCreatedMessage(_StrictModel):
    type: Literal["intervention_created"] = "intervention_created"
    id: str
    ekylibre_id: int
    url: str | None = None


class OutOfScopeMessage(_StrictModel):
    type: Literal["out_of_scope"] = "out_of_scope"
    id: str
    reason: str
    suggestion: str | None = None


class ErrorMessage(_StrictModel):
    type: Literal["error"] = "error"
    id: str | None = None
    code: ErrorCode
    message: str
    retryable: bool = False


class PongMessage(_StrictModel):
    type: Literal["pong"] = "pong"


ServerMessage = Annotated[
    AuthOkMessage
    | AuthErrorMessage
    | ThinkingMessage
    | AssistantTokenMessage
    | AssistantMessage
    | InterventionDraftMessage
    | ClarificationNeededMessage
    | InterventionCreatedMessage
    | OutOfScopeMessage
    | ErrorMessage
    | PongMessage,
    Field(discriminator="type"),
]
server_message_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
