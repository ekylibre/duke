from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
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
from duke.integration.store.hashing import tenant_hash, user_hash
from duke.integration.store.models import TurnOutcome, TurnRole
from duke.integration.store.repositories import ConversationRepository
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
from duke.transport.rate_limit import RateLimiter

log = structlog.get_logger(__name__)

AUTH_TIMEOUT_S = 10.0
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_GRACE_S = 60.0

WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_INTERNAL_ERROR = 1011


@dataclass
class _SessionContext:
    """Per-WS-connection state, threaded through the dispatcher."""

    ws_session_id: str
    creds: EkylibreCredentials
    api: EkylibreApiClient
    recorder: InterventionRecorder
    orchestrator: ConversationOrchestrator
    repo: ConversationRepository | None
    rate_limiter: RateLimiter
    db_session_id: uuid.UUID | None = None
    drafts: dict[str, InterventionDraft] = field(default_factory=dict)
    draft_db_ids: dict[str, uuid.UUID] = field(default_factory=dict)


async def _safe(coro):
    """Best-effort persistence: log on failure, never propagate."""
    try:
        return await coro
    except Exception as exc:
        log.warning("persistence.error", error=str(exc))
        return None


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

        settings = websocket.app.state.settings
        ws_sessions_active.inc()
        try:
            ctx = _SessionContext(
                ws_session_id=session_id,
                creds=creds,
                api=EkylibreApiClient(creds, websocket.app.state.http_client),
                recorder=websocket.app.state.intervention_recorder,
                orchestrator=websocket.app.state.orchestrator,
                repo=getattr(websocket.app.state, "conversation_repo", None),
                rate_limiter=RateLimiter(limit=settings.rate_limit_per_min),
            )

            if ctx.repo is not None:
                ctx.db_session_id = await _safe(
                    ctx.repo.start_session(
                        tenant_hash=tenant_hash(creds.tenant, secret=settings.hash_secret),
                        user_hash=user_hash(creds.email, secret=settings.hash_secret),
                        llm_provider=settings.llm_default_provider,
                    )
                )

            try:
                await _run_session(websocket, ctx)
            finally:
                if ctx.repo is not None and ctx.db_session_id is not None:
                    await _safe(ctx.repo.end_session(ctx.db_session_id))
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


async def _run_session(websocket: WebSocket, ctx: _SessionContext) -> None:
    last_pong = asyncio.get_running_loop().time()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket, lambda: last_pong))

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

            await _dispatch(websocket, ctx, msg)
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


async def _dispatch(websocket: WebSocket, ctx: _SessionContext, msg: object) -> None:
    if isinstance(msg, UserMessage):
        if not ctx.rate_limiter.try_acquire():
            errors_total.labels(code=ErrorCode.RATE_LIMITED.value).inc()
            await _send(
                websocket,
                ErrorMessage(
                    id=msg.id,
                    code=ErrorCode.RATE_LIMITED,
                    message="Trop de messages, attends quelques secondes.",
                    retryable=True,
                ),
            )
            return
        await _handle_user_message(websocket, ctx, msg)
        return

    if isinstance(msg, ConfirmInterventionMessage):
        await _handle_confirm(websocket, ctx, msg)
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
        ctx.drafts.pop(msg.id, None)
        await _send(
            websocket,
            AssistantMessage(id=msg.id, text="Annulé.", final=True),
        )
        return

    log.warning("ws.unhandled_message", session_id=ctx.ws_session_id, type=type(msg).__name__)


async def _handle_user_message(
    websocket: WebSocket,
    ctx: _SessionContext,
    msg: UserMessage,
) -> None:
    started = time.monotonic()

    if ctx.repo is not None and ctx.db_session_id is not None:
        await _safe(
            ctx.repo.record_turn(
                session_id=ctx.db_session_id,
                role=TurnRole.USER,
                text=msg.text,
            )
        )

    await _send(websocket, ThinkingMessage(id=msg.id))

    async def _record_assistant(
        text: str | None,
        outcome: TurnOutcome,
        intent: str | None = None,
    ) -> None:
        if ctx.repo is None or ctx.db_session_id is None:
            return
        latency_ms = int((time.monotonic() - started) * 1000)
        await _safe(
            ctx.repo.record_turn(
                session_id=ctx.db_session_id,
                role=TurnRole.ASSISTANT,
                text=text,
                intent=intent,
                latency_ms=latency_ms,
                outcome=outcome,
            )
        )

    try:
        result = await ctx.orchestrator.handle(msg.text, tenant_schema=ctx.creds.tenant)
    except LLMUnavailableError as exc:
        log.warning("ws.llm_unavailable", session_id=ctx.ws_session_id, error=str(exc))
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
        await _record_assistant(None, TurnOutcome.ERROR)
        return
    except Exception as exc:
        log.exception("ws.orchestrator_error", session_id=ctx.ws_session_id, error=str(exc))
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
        await _record_assistant(None, TurnOutcome.ERROR)
        return

    if isinstance(result, DraftReady):
        ctx.drafts[msg.id] = result.draft
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
        outcome = TurnOutcome.AMBIGUITY if result.draft.ambiguities else TurnOutcome.OK
        await _record_assistant(None, outcome, intent="record_intervention")

        if ctx.repo is not None and ctx.db_session_id is not None:
            user_turn_id = await _safe(
                ctx.repo.record_turn(
                    session_id=ctx.db_session_id,
                    role=TurnRole.SYSTEM,
                    text=None,
                    intent="record_intervention",
                )
            )
            if user_turn_id is not None:
                draft_id = await _safe(
                    ctx.repo.record_intervention_draft(
                        session_id=ctx.db_session_id,
                        turn_id=user_turn_id,
                        draft=result.draft.model_dump(mode="json"),
                    )
                )
                if draft_id is not None:
                    ctx.draft_db_ids[msg.id] = draft_id
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
            log.warning("ws.qa_stream_unavailable", session_id=ctx.ws_session_id, error=str(exc))
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
            await _record_assistant(None, TurnOutcome.ERROR)
            return

        user_messages_total.labels(outcome="qa_answer").inc()
        full = "".join(accumulated)
        await _send(websocket, AssistantMessage(id=msg.id, text=full, final=True))
        await _record_assistant(full, TurnOutcome.OK, intent="qa")
        return

    if isinstance(result, OutOfScope):
        user_messages_total.labels(outcome="out_of_scope").inc()
        await _send(
            websocket,
            OutOfScopeMessage(id=msg.id, reason=result.reason, suggestion=result.suggestion),
        )
        await _record_assistant(result.reason, TurnOutcome.OK, intent="out_of_scope")
        return

    if isinstance(result, UnknownIntent):
        user_messages_total.labels(outcome="unknown").inc()
        await _send(
            websocket,
            AssistantMessage(id=msg.id, text=result.message, final=True),
        )
        await _record_assistant(result.message, TurnOutcome.OK, intent="unknown")
        return


async def _handle_confirm(
    websocket: WebSocket,
    ctx: _SessionContext,
    msg: ConfirmInterventionMessage,
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
        created = await ctx.recorder.confirm(ctx.api, draft)
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
        log.warning("ws.confirm_auth_error", session_id=ctx.ws_session_id, error=str(exc))
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
        log.warning("ws.confirm_unavailable", session_id=ctx.ws_session_id, error=str(exc))
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
        log.warning("ws.confirm_bad_request", session_id=ctx.ws_session_id, error=str(exc))
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

    ctx.drafts.pop(msg.id, None)
    draft_db_id = ctx.draft_db_ids.pop(msg.id, None)

    if ctx.repo is not None and draft_db_id is not None:
        await _safe(
            ctx.repo.mark_draft_confirmed(
                draft_id=draft_db_id,
                ekylibre_intervention_id=created.id,
            )
        )

    if ctx.repo is not None and ctx.db_session_id is not None:
        await _safe(
            ctx.repo.record_audit(
                event_type="intervention.confirmed",
                session_id=ctx.db_session_id,
                details={"ekylibre_id": created.id},
            )
        )

    await _send(
        websocket,
        InterventionCreatedMessage(id=msg.id, ekylibre_id=created.id, url=created.url),
    )


async def _send(websocket: WebSocket, msg: ServerMessage | Any) -> None:
    if hasattr(msg, "model_dump_json"):
        await websocket.send_text(msg.model_dump_json())
    else:
        await websocket.send_json(msg)
