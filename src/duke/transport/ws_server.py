from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import structlog
from fastapi import WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from duke.application.intervention_recorder import InterventionRecorder
from duke.application.orchestrator import (
    ConversationOrchestrator,
    DraftReady,
    OutOfScope,
    QaStream,
    UnknownIntent,
)
from duke.domain.intervention import InterventionDraft
from duke.integration.ekylibre.api_client import (
    EkylibreApiClient,
    EkylibreAuthError,
    EkylibreBadRequestError,
    EkylibreCredentials,
    EkylibreTenantError,
    EkylibreUnavailableError,
)
from duke.nlu.llm.base import LLMUnavailableError
from duke.observability.metrics import errors_total, user_messages_total, ws_sessions_active
from duke.transport.messages import (
    AssistantMessage,
    AssistantTokenMessage,
    AuthErrorMessage,
    AuthMessage,
    AuthOkMessage,
    CancelMessage,
    ClarifyMessage,
    ConfirmInterventionMessage,
    ErrorCode,
    ErrorMessage,
    InterventionCreatedMessage,
    InterventionDraftMessage,
    OutOfScopeMessage,
    PingMessage,
    PongMessage,
    ServerMessage,
    ThinkingMessage,
    UserMessage,
    client_message_adapter,
)

log = structlog.get_logger(__name__)

AUTH_TIMEOUT_S = 10.0
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_GRACE_S = 60.0

WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_INTERNAL_ERROR = 1011


async def ws_endpoint(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    allowed_origins: list[str] = settings.allowed_ws_origins

    if allowed_origins:
        origin = websocket.headers.get("origin")
        if not origin or origin not in allowed_origins:
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
            return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    log.info("ws.connected", session_id=session_id)

    try:
        creds = await _await_auth(websocket, session_id)
        if creds is None:
            return

        ws_sessions_active.inc()
        try:
            api = EkylibreApiClient(creds, websocket.app.state.http_client)
            recorder: InterventionRecorder = websocket.app.state.intervention_recorder
            orchestrator: ConversationOrchestrator = websocket.app.state.orchestrator
            await _run_session(websocket, session_id, creds, api, recorder, orchestrator)
        finally:
            ws_sessions_active.dec()

    except WebSocketDisconnect:
        log.info("ws.disconnected", session_id=session_id)
    except Exception as exc:
        log.exception("ws.unexpected_error", session_id=session_id, error=str(exc))
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR)


async def _await_auth(websocket: WebSocket, session_id: str) -> EkylibreCredentials | None:
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=AUTH_TIMEOUT_S)
    except TimeoutError:
        log.info("ws.auth_timeout", session_id=session_id)
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None

    try:
        msg = client_message_adapter.validate_python(raw)
    except ValidationError:
        await _send(
            websocket,
            AuthErrorMessage(code=ErrorCode.INTERNAL, message="Invalid auth payload"),
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None

    if not isinstance(msg, AuthMessage):
        await _send(
            websocket,
            AuthErrorMessage(code=ErrorCode.INTERNAL, message="First message must be auth"),
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None

    settings = websocket.app.state.settings
    creds = EkylibreCredentials(
        email="",
        token=msg.token,
        tenant=msg.tenant,
        base_url=settings.ekylibre_api_base_url,
    )

    http_client = websocket.app.state.http_client
    api = EkylibreApiClient(creds, http_client)
    try:
        user = await api.validate_token()
    except EkylibreAuthError:
        errors_total.labels(code=ErrorCode.AUTH_INVALID_TOKEN.value).inc()
        await _send(
            websocket,
            AuthErrorMessage(code=ErrorCode.AUTH_INVALID_TOKEN, message="Invalid token"),
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None
    except EkylibreTenantError:
        errors_total.labels(code=ErrorCode.AUTH_TENANT_UNKNOWN.value).inc()
        await _send(
            websocket,
            AuthErrorMessage(code=ErrorCode.AUTH_TENANT_UNKNOWN, message="Unknown tenant"),
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None
    except EkylibreUnavailableError:
        errors_total.labels(code=ErrorCode.EKYLIBRE_UNAVAILABLE.value).inc()
        await _send(
            websocket,
            AuthErrorMessage(
                code=ErrorCode.EKYLIBRE_UNAVAILABLE,
                message="Ekylibre API unavailable",
            ),
        )
        await websocket.close(code=WS_CLOSE_INTERNAL_ERROR)
        return None

    creds = EkylibreCredentials(
        email=user.email,
        token=msg.token,
        tenant=msg.tenant,
        base_url=settings.ekylibre_api_base_url,
    )

    await _send(
        websocket,
        AuthOkMessage(
            user={"id": user.id, "email": user.email, "full_name": user.full_name},
            tenant_label=msg.tenant,
            capabilities=["intervention_record", "qa_read"],
            llm_provider=None,
        ),
    )
    log.info("ws.auth_ok", session_id=session_id, tenant=msg.tenant)
    return creds


async def _run_session(
    websocket: WebSocket,
    session_id: str,
    creds: EkylibreCredentials,
    api: EkylibreApiClient,
    recorder: InterventionRecorder,
    orchestrator: ConversationOrchestrator,
) -> None:
    last_pong = asyncio.get_running_loop().time()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket, lambda: last_pong))

    drafts: dict[str, InterventionDraft] = {}

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                return

            try:
                msg = client_message_adapter.validate_python(raw)
            except ValidationError as exc:
                detail = exc.errors()[0]["msg"]
                await _send(
                    websocket,
                    ErrorMessage(
                        code=ErrorCode.INTERNAL,
                        message=f"Invalid message: {detail}",
                    ),
                )
                continue

            if isinstance(msg, PingMessage):
                await _send(websocket, PongMessage())
                last_pong = asyncio.get_running_loop().time()
                continue

            await _dispatch(websocket, session_id, msg, creds, api, recorder, orchestrator, drafts)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task


async def _heartbeat_loop(websocket: WebSocket, last_pong: Any) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        elapsed = asyncio.get_running_loop().time() - last_pong()
        if elapsed > HEARTBEAT_GRACE_S:
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
            return


async def _dispatch(
    websocket: WebSocket,
    session_id: str,
    msg: object,
    creds: EkylibreCredentials,
    api: EkylibreApiClient,
    recorder: InterventionRecorder,
    orchestrator: ConversationOrchestrator,
    drafts: dict[str, InterventionDraft],
) -> None:
    if isinstance(msg, UserMessage):
        await _handle_user_message(websocket, session_id, msg, creds, orchestrator, drafts)
        return

    if isinstance(msg, ConfirmInterventionMessage):
        await _handle_confirm(websocket, session_id, msg, api, recorder, drafts)
        return

    if isinstance(msg, ClarifyMessage):
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.INTERNAL,
                message="Clarification not implemented yet (iteration 3)",
                retryable=False,
            ),
        )
        return

    if isinstance(msg, CancelMessage):
        drafts.pop(msg.id, None)
        await _send(
            websocket,
            AssistantMessage(id=msg.id, text="Annulé.", final=True),
        )
        return

    log.warning("ws.unhandled_message", session_id=session_id, type=type(msg).__name__)


async def _handle_user_message(
    websocket: WebSocket,
    session_id: str,
    msg: UserMessage,
    creds: EkylibreCredentials,
    orchestrator: ConversationOrchestrator,
    drafts: dict[str, InterventionDraft],
) -> None:
    await _send(websocket, ThinkingMessage(id=msg.id))
    try:
        result = await orchestrator.handle(msg.text, tenant_schema=creds.tenant)
    except LLMUnavailableError as exc:
        log.warning("ws.llm_unavailable", session_id=session_id, error=str(exc))
        errors_total.labels(code=ErrorCode.LLM_UNAVAILABLE.value).inc()
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.LLM_UNAVAILABLE,
                message=(
                    "Le service de compréhension est indisponible, réessaie dans quelques instants."
                ),
                retryable=True,
            ),
        )
        user_messages_total.labels(outcome="llm_error").inc()
        return
    except Exception as exc:
        log.exception("ws.orchestrator_error", session_id=session_id, error=str(exc))
        errors_total.labels(code=ErrorCode.INTERNAL.value).inc()
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.INTERNAL,
                message="Une erreur interne est survenue.",
                retryable=False,
            ),
        )
        user_messages_total.labels(outcome="error").inc()
        return

    if isinstance(result, DraftReady):
        drafts[msg.id] = result.draft
        user_messages_total.labels(outcome="draft").inc()
        await _send(
            websocket,
            InterventionDraftMessage(
                id=msg.id,
                fields=result.draft.model_dump(
                    mode="json",
                    exclude={"ambiguities", "confidence", "raw_text"},
                ),
                ambiguities=[a.model_dump() for a in result.draft.ambiguities],
                confidence=result.draft.confidence,
            ),
        )
        return

    if isinstance(result, QaStream):
        accumulated: list[str] = []
        try:
            async for token in result.stream:
                if not token:
                    continue
                accumulated.append(token)
                await _send(websocket, AssistantTokenMessage(id=msg.id, delta=token))
        except LLMUnavailableError as exc:
            log.warning("ws.qa_stream_unavailable", session_id=session_id, error=str(exc))
            errors_total.labels(code=ErrorCode.LLM_UNAVAILABLE.value).inc()
            await _send(
                websocket,
                ErrorMessage(
                    id=msg.id,
                    code=ErrorCode.LLM_UNAVAILABLE,
                    message="Service de réponse indisponible, réessaie plus tard.",
                    retryable=True,
                ),
            )
            user_messages_total.labels(outcome="qa_error").inc()
            return

        user_messages_total.labels(outcome="qa_answer").inc()
        await _send(
            websocket,
            AssistantMessage(id=msg.id, text="".join(accumulated), final=True),
        )
        return

    if isinstance(result, OutOfScope):
        user_messages_total.labels(outcome="out_of_scope").inc()
        await _send(
            websocket,
            OutOfScopeMessage(id=msg.id, reason=result.reason, suggestion=result.suggestion),
        )
        return

    if isinstance(result, UnknownIntent):
        user_messages_total.labels(outcome="unknown").inc()
        await _send(
            websocket,
            AssistantMessage(id=msg.id, text=result.message, final=True),
        )
        return


async def _handle_confirm(
    websocket: WebSocket,
    session_id: str,
    msg: ConfirmInterventionMessage,
    api: EkylibreApiClient,
    recorder: InterventionRecorder,
    drafts: dict[str, InterventionDraft],
) -> None:
    try:
        draft = InterventionDraft.model_validate(msg.draft)
    except ValidationError as exc:
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.INTERNAL,
                message=f"Draft invalide: {exc.errors()[0]['msg']}",
                retryable=False,
            ),
        )
        return

    try:
        created = await recorder.confirm(api, draft)
    except ValueError as exc:
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.INTERNAL,
                message=str(exc),
                retryable=False,
            ),
        )
        return
    except (EkylibreAuthError, EkylibreTenantError) as exc:
        log.warning("ws.confirm_auth_error", session_id=session_id, error=str(exc))
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.AUTH_INVALID_TOKEN,
                message="Authentification expirée, reconnecte-toi.",
                retryable=False,
            ),
        )
        return
    except EkylibreUnavailableError as exc:
        log.warning("ws.confirm_unavailable", session_id=session_id, error=str(exc))
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.EKYLIBRE_UNAVAILABLE,
                message="Ekylibre est indisponible, réessaie dans quelques instants.",
                retryable=True,
            ),
        )
        return
    except EkylibreBadRequestError as exc:
        log.warning("ws.confirm_bad_request", session_id=session_id, error=str(exc))
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.EKYLIBRE_API_ERROR,
                message="Ekylibre a refusé la requête, vérifie les champs.",
                retryable=False,
            ),
        )
        return

    drafts.pop(msg.id, None)
    await _send(
        websocket,
        InterventionCreatedMessage(id=msg.id, ekylibre_id=created.id, url=created.url),
    )


async def _send(websocket: WebSocket, msg: ServerMessage | Any) -> None:
    if hasattr(msg, "model_dump_json"):
        await websocket.send_text(msg.model_dump_json())
    else:
        await websocket.send_json(msg)
