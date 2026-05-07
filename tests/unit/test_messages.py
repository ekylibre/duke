from __future__ import annotations

import pytest
from pydantic import ValidationError

from duke.transport.messages import (
    AssistantMessage,
    AuthMessage,
    AuthOkMessage,
    ConfirmInterventionMessage,
    ErrorCode,
    ErrorMessage,
    InterventionDraftMessage,
    PingMessage,
    PongMessage,
    ServerMessage,
    UserMessage,
    client_message_adapter,
    server_message_adapter,
)


class TestClientMessageParsing:
    def test_auth_message_valid(self) -> None:
        msg = client_message_adapter.validate_python(
            {"type": "auth", "token": "tok", "tenant": "farm_a", "locale": "fr"}
        )
        assert isinstance(msg, AuthMessage)
        assert msg.token == "tok"
        assert msg.tenant == "farm_a"

    def test_auth_message_locale_default(self) -> None:
        msg = client_message_adapter.validate_python(
            {"type": "auth", "token": "tok", "tenant": "farm_a"}
        )
        assert isinstance(msg, AuthMessage)
        assert msg.locale == "fr"

    def test_user_message_valid(self) -> None:
        msg = client_message_adapter.validate_python(
            {"type": "user_message", "id": "abc", "text": "bonjour"}
        )
        assert isinstance(msg, UserMessage)
        assert msg.id == "abc"

    def test_confirm_intervention_valid(self) -> None:
        msg = client_message_adapter.validate_python(
            {"type": "confirm_intervention", "id": "abc", "draft": {"procedure_name": "spraying"}}
        )
        assert isinstance(msg, ConfirmInterventionMessage)
        assert msg.draft["procedure_name"] == "spraying"

    def test_ping_valid(self) -> None:
        msg = client_message_adapter.validate_python({"type": "ping"})
        assert isinstance(msg, PingMessage)

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            client_message_adapter.validate_python({"type": "nope"})

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            client_message_adapter.validate_python(
                {"type": "auth", "token": "t", "tenant": "x", "evil": True}
            )

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            client_message_adapter.validate_python({"type": "auth", "tenant": "x"})


class TestServerMessageRoundTrip:
    def test_auth_ok_round_trip(self) -> None:
        original = AuthOkMessage(
            user={"id": 1, "email": "a@b.c", "full_name": "Test"},
            tenant_label="farm_a",
            capabilities=["intervention_record"],
            llm_provider="claude",
        )
        parsed = server_message_adapter.validate_json(original.model_dump_json())
        assert isinstance(parsed, AuthOkMessage)
        assert parsed.user["email"] == "a@b.c"

    def test_error_round_trip(self) -> None:
        original = ErrorMessage(id="abc", code=ErrorCode.INTERNAL, message="Boom", retryable=False)
        parsed = server_message_adapter.validate_json(original.model_dump_json())
        assert isinstance(parsed, ErrorMessage)
        assert parsed.code == ErrorCode.INTERNAL

    def test_intervention_draft_round_trip(self) -> None:
        original = InterventionDraftMessage(
            id="abc", fields={"procedure_name": "spraying"}, ambiguities=[], confidence=0.9
        )
        parsed = server_message_adapter.validate_json(original.model_dump_json())
        assert isinstance(parsed, InterventionDraftMessage)

    def test_assistant_message_round_trip(self) -> None:
        original = AssistantMessage(id="abc", text="bonjour", final=True)
        parsed: ServerMessage = server_message_adapter.validate_json(original.model_dump_json())
        assert isinstance(parsed, AssistantMessage)
        assert parsed.text == "bonjour"

    def test_pong_round_trip(self) -> None:
        original = PongMessage()
        parsed = server_message_adapter.validate_json(original.model_dump_json())
        assert isinstance(parsed, PongMessage)
