from __future__ import annotations

import asyncio
import contextlib
import re
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
from duke.domain.entities import (
    Ambiguity,
    ResolvedDoer,
    ResolvedInput,
    ResolvedTarget,
    ResolvedTool,
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
from duke.integration.ekylibre.procedure_registry import ProcedureRegistry
from duke.integration.ekylibre.read_db import EkylibreReadDb
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
    read_db: EkylibreReadDb | None = None
    procedure_registry: ProcedureRegistry | None = None
    lexicon_repo: object | None = None
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
                read_db=getattr(websocket.app.state, "read_db", None),
                procedure_registry=getattr(websocket.app.state, "procedure_registry", None),
                lexicon_repo=getattr(websocket.app.state, "lexicon_repo", None),
            )

            # Lazy-hydrate the Procedo registry on first auth. Subsequent
            # sessions short-circuit via the in-class guard. The task runs
            # in the background — not awaited — so auth_ok responses aren't
            # delayed by the API call. We don't keep a reference to the task;
            # the registry's internal lock prevents concurrent hydrations.
            if ctx.procedure_registry is not None and ctx.lexicon_repo is not None:
                _hydration_task = asyncio.create_task(
                    ctx.procedure_registry.hydrate(ctx.api, ctx.lexicon_repo)
                )
                del _hydration_task

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
    except ValidationError as exc:
        # Don't log the raw payload (token is in there). The error tells us which
        # fields are missing/wrong without exposing the secret.
        log.info(
            "ws.auth_payload_invalid",
            session_id=session_id,
            errors=[
                {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"]}
                for e in exc.errors()
            ],
        )
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
        email=msg.email,
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
        await _handle_clarify(websocket, ctx, msg)
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
        draft = await _enrich_with_parcel_options(ctx, result.draft)
        draft = await _enrich_with_input_options(ctx, draft)
        draft = await _resolve_tools(ctx, draft)
        draft = await _resolve_doers(ctx, draft)
        user_messages_total.labels(outcome="draft").inc()
        await _emit_draft(websocket, ctx, msg.id, draft)
        outcome = TurnOutcome.AMBIGUITY if draft.ambiguities else TurnOutcome.OK
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
                        draft=draft.model_dump(mode="json"),
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


async def _handle_clarify(
    websocket: WebSocket,
    ctx: _SessionContext,
    msg: ClarifyMessage,
) -> None:
    """Resolve an open ambiguity by either picking a candidate option or
    re-extracting on top of the combined phrase.

    Selection path (no LLM call): if `msg.answer` matches one of the parcel
    options Duke previously suggested, we patch the unresolved target with
    the chosen parcel's id and drop that ambiguity. Cheap and deterministic.

    Free-text fallback: append `Précision : <answer>` to the original phrase
    and rerun the recorder so the LLM gets a second chance with the extra
    context. Used when the user typed a clarification rather than clicking.

    The new draft replaces the previous one under the same `id` and is
    re-emitted as `intervention_draft` so the widget swaps the existing card
    and re-evaluates the Validate gate.
    """
    existing = ctx.drafts.get(msg.id)
    if existing is None:
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.INTERNAL,
                message="Aucun brouillon en cours pour cette clarification.",
                retryable=False,
            ),
        )
        return

    answer = msg.answer.strip()

    # Option-pick fast path: see if the user clicked a parcel candidate…
    selected = await _apply_parcel_selection(ctx, existing, answer)
    if selected is not None:
        # The parcel pick may have left input ambiguities still open; keep
        # their options fresh (the recorder doesn't run again here).
        selected = await _enrich_with_input_options(ctx, selected)
        selected = await _resolve_tools(ctx, selected)
        selected = await _resolve_doers(ctx, selected)
        await _emit_draft(websocket, ctx, msg.id, selected)
        return

    # …or a product candidate for an inputs ambiguity.
    selected_input = await _apply_input_selection(ctx, existing, answer)
    if selected_input is not None:
        # Re-enrich both directions — picking one input shouldn't blow away
        # the parcel options if the parcels ambiguity is still pending.
        selected_input = await _enrich_with_parcel_options(ctx, selected_input)
        selected_input = await _enrich_with_input_options(ctx, selected_input)
        selected_input = await _resolve_tools(ctx, selected_input)
        selected_input = await _resolve_doers(ctx, selected_input)
        await _emit_draft(websocket, ctx, msg.id, selected_input)
        return

    # Free-text fallback: re-extract with the answer appended.
    base = (existing.raw_text or "").strip()
    combined = f"{base}. Précision : {answer}" if base else answer

    await _send(websocket, ThinkingMessage(id=msg.id))

    try:
        new_draft = await ctx.recorder.draft_from_text(combined)
    except LLMUnavailableError as exc:
        log.warning("ws.clarify_llm_unavailable", session_id=ctx.ws_session_id, error=str(exc))
        errors_total.labels(code=ErrorCode.LLM_UNAVAILABLE.value).inc()
        await _send(
            websocket,
            ErrorMessage(
                id=msg.id,
                code=ErrorCode.LLM_UNAVAILABLE,
                message="Service NLU indisponible, réessaie plus tard.",
                retryable=True,
            ),
        )
        return
    except Exception as exc:
        log.exception("ws.clarify_error", session_id=ctx.ws_session_id, error=str(exc))
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
        return

    # The recorder set `raw_text` on the new draft to the combined phrase
    # ("…. Précision : <answer>"). Restore the user's original message so the
    # `description` we send to Ekylibre matches what the user actually typed.
    if existing.raw_text:
        new_draft = new_draft.model_copy(update={"raw_text": existing.raw_text})
    new_draft = await _enrich_with_parcel_options(ctx, new_draft)
    new_draft = await _enrich_with_input_options(ctx, new_draft)
    new_draft = await _resolve_tools(ctx, new_draft)
    new_draft = await _resolve_doers(ctx, new_draft)
    await _emit_draft(websocket, ctx, msg.id, new_draft)


async def _emit_draft(
    websocket: WebSocket,
    ctx: _SessionContext,
    msg_id: str,
    draft: InterventionDraft,
) -> None:
    ctx.drafts[msg_id] = draft
    await _send(
        websocket,
        InterventionDraftMessage(
            id=msg_id,
            fields=draft.model_dump(
                mode="json",
                exclude={"ambiguities", "confidence", "raw_text"},
            ),
            ambiguities=[a.model_dump() for a in draft.ambiguities],
            confidence=draft.confidence,
        ),
    )


_PARCEL_OPTION_LIMIT = 5
_PARCEL_FUZZY_THRESHOLD = 50
# Cap for the multi-select UI: scrollable but not overwhelming. The list is
# capped alphabetically; tenants with many more parcels need to refine via
# free-text in the textarea instead.
_PARCEL_MULTI_OPTION_LIMIT = 100

# Same shape as parcels — fuzzy top-N for the per-input single-select UI,
# with a larger fallback list when the user's raw_name doesn't fuzzy-match
# anything well enough.
_INPUT_OPTION_LIMIT = 8
_INPUT_FUZZY_THRESHOLD = 50
_INPUT_FALLBACK_LIMIT = 100

# Equipment (tool) auto-resolution. Unlike parcels/inputs we don't surface a
# pick list — a tool reference like "533" usually maps to exactly one piece of
# equipment ("Tracteur CASE-IH 533"). We only auto-resolve when a single
# equipment is a confident match; ambiguous/no matches stay unresolved and are
# dropped by the mapper (same as today) rather than guessed.
_TOOL_FUZZY_THRESHOLD = 80
_TOOL_FALLBACK_LIMIT = 500


async def _load_tenant_parcels(ctx: _SessionContext) -> list[dict[str, Any]]:
    """Best-effort lookup of land parcels for the active tenant.

    Returns an empty list if the read DB isn't wired or the query fails — the
    caller falls back to LLM-based clarification, which is slower but still
    correct.
    """
    if ctx.read_db is None:
        return []
    try:
        async with ctx.read_db.with_tenant(ctx.creds.tenant) as reader:
            return await reader.list_land_parcels(limit=500)
    except Exception as exc:
        log.warning(
            "ws.parcel_lookup_failed", session_id=ctx.ws_session_id, error=str(exc)
        )
        return []


async def _enrich_with_parcel_options(
    ctx: _SessionContext, draft: InterventionDraft
) -> InterventionDraft:
    """Attach parcel candidates to each unresolved-target ambiguity.

    For per-raw-target ambiguities (`multi=False`) we fuzzy-match the user's
    raw value against the tenant's parcels and surface the top-N names as
    single-select buttons.

    For the catch-all multi ambiguity (`multi=True`, raised when the user
    didn't name any parcel), the fuzzy match has nothing to bite on, so we
    return the full alphabetical list (capped) and let the widget render a
    checkbox list.
    """
    target_ambiguities = [a for a in draft.ambiguities if a.field == "targets"]
    if not target_ambiguities:
        return draft

    parcels = await _load_tenant_parcels(ctx)
    if not parcels:
        return draft

    from rapidfuzz import fuzz, process

    parcel_names = [p["name"] for p in parcels]
    # Names of parcels already resolved on the draft — surfaced as pre-ticked
    # checkboxes in the widget so the user doesn't accidentally drop them
    # when answering a multi-select.
    resolved_names = [
        t.resolved_name
        for t in draft.targets
        if t.resolved_id is not None and t.resolved_name
    ]
    enriched: list[Ambiguity] = []
    for amb in draft.ambiguities:
        if amb.field != "targets" or amb.options:
            enriched.append(amb)
            continue

        if amb.multi:
            options = parcel_names[:_PARCEL_MULTI_OPTION_LIMIT]
            # Make sure the pre-checked names are visible in the option set
            # (they're always real parcel names, but the cap might exclude
            # them if the tenant has many more).
            for name in resolved_names:
                if name not in options:
                    options.append(name)
            selected = [n for n in resolved_names if n in options]
        else:
            matches = process.extract(
                amb.raw_value or "",
                parcel_names,
                scorer=fuzz.WRatio,
                limit=_PARCEL_OPTION_LIMIT,
            )
            options = [name for name, score, _idx in matches if score >= _PARCEL_FUZZY_THRESHOLD]
            selected = []

        if not options:
            enriched.append(amb)
            continue
        enriched.append(amb.model_copy(update={"options": options, "selected": selected}))

    return draft.model_copy(update={"ambiguities": enriched})


async def _apply_parcel_selection(
    ctx: _SessionContext, draft: InterventionDraft, answer: str
) -> InterventionDraft | None:
    """Patch a target ambiguity directly when the user picked one (or several)
    of the suggested parcel names. Returns the updated draft, or None if no
    option matched (caller falls back to LLM combine).

    Two flavours:
    - Multi-select (`amb.multi=True`): `answer` is a comma-separated list of
      parcel names; every one must be in the option set. We replace all
      unresolved targets with one ResolvedTarget per pick and drop every
      `targets` ambiguity in one shot.
    - Single-select (per-raw-target): `answer` is one option name; we find
      the matching unresolved target by raw_name (or take the first one)
      and resolve it.
    """
    target_ambs = [a for a in draft.ambiguities if a.field == "targets"]
    if not target_ambs:
        return None

    multi_amb = next((a for a in target_ambs if a.multi), None)

    if multi_amb is not None:
        names = [p.strip() for p in answer.split(",") if p.strip()]
        if not names:
            return None
        option_set = set(multi_amb.options)
        if not all(n in option_set for n in names):
            return None

        parcels = await _load_tenant_parcels(ctx)
        by_name = {str(p["name"]): p for p in parcels}
        if not all(n in by_name for n in names):
            return None

        # The user's tick list is authoritative: it covers what they want
        # for the whole intervention (already-resolved targets are surfaced
        # as pre-ticked checkboxes via `Ambiguity.selected`, so unticking is
        # an explicit "remove this parcel" gesture).
        new_targets = [
            ResolvedTarget(
                kind="land_parcel",
                raw_name=name,
                resolved_id=int(by_name[name]["id"]),
                resolved_name=str(by_name[name]["name"]),
                confidence=1.0,
            )
            for name in names
        ]
        new_ambiguities = [a for a in draft.ambiguities if a.field != "targets"]
        return draft.model_copy(
            update={"targets": new_targets, "ambiguities": new_ambiguities}
        )

    # Single-select path (existing behaviour).
    candidate_names = {opt for amb in target_ambs for opt in amb.options}
    if answer not in candidate_names:
        return None

    parcels = await _load_tenant_parcels(ctx)
    matching = [p for p in parcels if p["name"] == answer]
    if len(matching) != 1:
        # Either the parcel disappeared since options were emitted, or the
        # name now collides — let the LLM path sort it out.
        return None
    chosen = matching[0]

    new_targets: list[ResolvedTarget] = []
    consumed_raw: str | None = None
    for tgt in draft.targets:
        if tgt.resolved_id is None and consumed_raw is None:
            new_targets.append(
                ResolvedTarget(
                    kind=tgt.kind,
                    raw_name=tgt.raw_name,
                    resolved_id=int(chosen["id"]),
                    resolved_name=str(chosen["name"]),
                    confidence=1.0,
                )
            )
            consumed_raw = tgt.raw_name
        else:
            new_targets.append(tgt)

    if consumed_raw is None:
        return None

    new_ambiguities = [
        a
        for a in draft.ambiguities
        if not (a.field == "targets" and a.raw_value == consumed_raw)
    ]
    return draft.model_copy(update={"targets": new_targets, "ambiguities": new_ambiguities})


# Per-area unit → absolute unit. Lets us turn "2 L/ha on 1.5 ha" into
# "3 L total", which the mapper records under handler="population" without
# triggering Procedo's net_volume/net_mass division (which fails on any
# product without those indicators set, e.g. mixtures like bouillie
# bordelaise).
_DENSITY_TO_ABSOLUTE_UNIT: dict[str, str] = {
    "liter_per_hectare":      "liter",
    "hectoliter_per_hectare": "hectoliter",
    "liter_per_square_meter": "liter",
    "kilogram_per_hectare":   "kilogram",
    "gram_per_hectare":       "gram",
}


async def _sum_parcel_area_ha(ctx: _SessionContext, ids: list[int]) -> float:
    """Cumulative LandParcel area in hectares for the given product ids."""
    if ctx.read_db is None or not ids:
        return 0.0
    try:
        async with ctx.read_db.with_tenant(ctx.creds.tenant) as reader:
            return await reader.sum_parcel_area_in_hectare(ids)
    except Exception as exc:
        log.warning(
            "ws.parcel_area_lookup_failed",
            session_id=ctx.ws_session_id,
            error=str(exc),
        )
        return 0.0


async def _convert_density_inputs_to_absolute(
    ctx: _SessionContext, draft: InterventionDraft
) -> InterventionDraft:
    """Rewrite per-area inputs (2 L/ha) into absolute totals (rate × area)."""
    has_density = any(
        (inp.quantity_unit or "") in _DENSITY_TO_ABSOLUTE_UNIT
        for inp in draft.inputs
    )
    if not has_density:
        return draft

    target_ids = [t.resolved_id for t in draft.targets if t.resolved_id is not None]
    total_area_ha = await _sum_parcel_area_ha(ctx, target_ids)
    # `liter_per_square_meter` rates apply to surfaces sized in m², not ha.
    total_area_m2 = total_area_ha * 10_000

    new_inputs: list[ResolvedInput] = []
    for inp in draft.inputs:
        density_unit = inp.quantity_unit or ""
        if density_unit not in _DENSITY_TO_ABSOLUTE_UNIT or inp.quantity_value is None:
            new_inputs.append(inp)
            continue
        # Rate × area — pick the right area unit per density.
        if density_unit == "liter_per_square_meter":
            total = inp.quantity_value * total_area_m2
        else:
            total = inp.quantity_value * total_area_ha
        if total_area_ha == 0:
            # No usable shape on the targets — keep the rate as-is and let
            # the mapper send it along; Ekylibre will surface the error
            # rather than silently record a zero quantity.
            new_inputs.append(inp)
            continue
        new_inputs.append(
            inp.model_copy(
                update={
                    "quantity_value": round(float(total), 4),
                    "quantity_unit": _DENSITY_TO_ABSOLUTE_UNIT[density_unit],
                }
            )
        )

    return draft.model_copy(update={"inputs": new_inputs})


async def _load_tenant_input_products(ctx: _SessionContext) -> list[dict[str, Any]]:
    """Best-effort lookup of consumable products (intrants) for the tenant.

    Same contract as `_load_tenant_parcels`: returns [] on any failure so
    the caller falls back to the LLM clarify path.
    """
    if ctx.read_db is None:
        return []
    try:
        async with ctx.read_db.with_tenant(ctx.creds.tenant) as reader:
            return await reader.list_input_products(limit=_INPUT_FALLBACK_LIMIT)
    except Exception as exc:
        log.warning(
            "ws.input_lookup_failed", session_id=ctx.ws_session_id, error=str(exc)
        )
        return []


async def _enrich_with_input_options(
    ctx: _SessionContext, draft: InterventionDraft
) -> InterventionDraft:
    """Attach product candidates to each unresolved-input ambiguity.

    Fuzzy-matches the input's raw_name against the tenant's `Matter` and
    `Plant` products. When no fuzzy match clears the threshold we still
    surface the (capped) full list so the user can pick something — the
    confirm step would otherwise crash on `resolved_product_id is None`.
    """
    input_ambiguities = [a for a in draft.ambiguities if a.field == "inputs"]
    if not input_ambiguities:
        return draft

    products = await _load_tenant_input_products(ctx)
    if not products:
        return draft

    from rapidfuzz import fuzz, process

    product_names = [str(p["name"]) for p in products]
    enriched: list[Ambiguity] = []
    for amb in draft.ambiguities:
        if amb.field != "inputs" or amb.options:
            enriched.append(amb)
            continue
        matches = process.extract(
            amb.raw_value or "",
            product_names,
            scorer=fuzz.WRatio,
            limit=_INPUT_OPTION_LIMIT,
        )
        options = [name for name, score, _idx in matches if score >= _INPUT_FUZZY_THRESHOLD]
        if not options:
            options = product_names[:_INPUT_FALLBACK_LIMIT]
        if not options:
            enriched.append(amb)
            continue
        enriched.append(amb.model_copy(update={"options": options}))

    return draft.model_copy(update={"ambiguities": enriched})


async def _apply_input_selection(
    ctx: _SessionContext, draft: InterventionDraft, answer: str
) -> InterventionDraft | None:
    """Resolve the first unresolved input whose ambiguity exposes `answer`
    as one of its options. Returns None if no match — the LLM clarify path
    takes over.
    """
    input_ambs = [a for a in draft.ambiguities if a.field == "inputs"]
    if not input_ambs:
        return None

    matching_ambs = [a for a in input_ambs if answer in a.options]
    if not matching_ambs:
        return None
    target_amb = matching_ambs[0]

    products = await _load_tenant_input_products(ctx)
    matching = [p for p in products if str(p["name"]) == answer]
    if len(matching) != 1:
        return None
    chosen = matching[0]

    new_inputs: list[ResolvedInput] = []
    consumed_raw: str | None = None
    for inp in draft.inputs:
        if (
            inp.resolved_product_id is None
            and inp.raw_name == target_amb.raw_value
            and consumed_raw is None
        ):
            new_inputs.append(
                ResolvedInput(
                    raw_name=inp.raw_name,
                    resolved_product_id=int(chosen["id"]),
                    resolved_product_name=str(chosen["name"]),
                    quantity_value=inp.quantity_value,
                    quantity_unit=inp.quantity_unit,
                    confidence=1.0,
                )
            )
            consumed_raw = inp.raw_name
        else:
            new_inputs.append(inp)

    if consumed_raw is None:
        return None

    new_ambiguities = [
        a
        for a in draft.ambiguities
        if not (a.field == "inputs" and a.raw_value == consumed_raw)
    ]
    return draft.model_copy(update={"inputs": new_inputs, "ambiguities": new_ambiguities})


async def _load_tenant_equipments(ctx: _SessionContext) -> list[dict[str, Any]]:
    """Best-effort lookup of equipment products (matériel) for the tenant.

    Same contract as `_load_tenant_input_products`: returns [] on any failure
    so the caller leaves tools unresolved (the mapper then drops them).
    """
    if ctx.read_db is None:
        return []
    try:
        async with ctx.read_db.with_tenant(ctx.creds.tenant) as reader:
            return await reader.list_equipments(limit=_TOOL_FALLBACK_LIMIT)
    except Exception as exc:
        log.warning(
            "ws.equipment_lookup_failed", session_id=ctx.ws_session_id, error=str(exc)
        )
        return []


async def _load_tenant_workers(ctx: _SessionContext) -> list[dict[str, Any]]:
    """Best-effort lookup of worker products (intervenants) for the tenant."""
    if ctx.read_db is None:
        return []
    try:
        async with ctx.read_db.with_tenant(ctx.creds.tenant) as reader:
            return await reader.list_workers(limit=_TOOL_FALLBACK_LIMIT)
    except Exception as exc:
        log.warning(
            "ws.worker_lookup_failed", session_id=ctx.ws_session_id, error=str(exc)
        )
        return []


def _match_product(raw: str, products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single product that confidently matches `raw`, else None.

    Tools/doers are auto-resolved (no pick list), so we only commit to a match
    when it is unambiguous. Tried in order:
      1. exact (case-insensitive) name match;
      2. whole-word token match — the raw value appears as a token in the name
         ("533" in "Tracteur CASE-IH 533");
      3. fuzzy WRatio, but only if a *single* candidate clears the threshold.
    A tie at any stage returns None — we'd rather drop it than guess (e.g. two
    workers sharing a name stay unresolved instead of picking the wrong one).
    """
    raw_norm = raw.strip().lower()
    if not raw_norm:
        return None

    exact = [p for p in products if str(p["name"]).strip().lower() == raw_norm]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    token_pat = re.compile(rf"(?<!\w){re.escape(raw_norm)}(?!\w)")
    token_hits = [p for p in products if token_pat.search(str(p["name"]).lower())]
    if len(token_hits) == 1:
        return token_hits[0]
    if len(token_hits) > 1:
        return None

    from rapidfuzz import fuzz, process

    names = [str(p["name"]) for p in products]
    matches = process.extract(raw, names, scorer=fuzz.WRatio, limit=3)
    good = [(name, score) for name, score, _idx in matches if score >= _TOOL_FUZZY_THRESHOLD]
    if len(good) == 1:
        return next(p for p in products if str(p["name"]) == good[0][0])
    return None


async def _resolve_tools(
    ctx: _SessionContext, draft: InterventionDraft
) -> InterventionDraft:
    """Resolve each unresolved tool's raw_name to a tenant equipment product_id.

    Without this every tool stays `resolved_id=None` and the mapper drops it,
    so the equipment never reaches the saved Ekylibre intervention. We only
    commit confident, unambiguous matches; anything else is left unresolved.
    The product `variety` is carried through so the mapper can route the tool
    to the right Procedo slot (tractor → `tractor`, plough → `soil_tool`).
    """
    if not any(t.resolved_id is None and t.raw_name for t in draft.tools):
        return draft

    equipments = await _load_tenant_equipments(ctx)
    if not equipments:
        return draft

    new_tools: list[ResolvedTool] = []
    for tool in draft.tools:
        if tool.resolved_id is not None or not tool.raw_name:
            new_tools.append(tool)
            continue
        chosen = _match_product(tool.raw_name, equipments)
        if chosen is None:
            log.info(
                "ws.tool_unresolved", session_id=ctx.ws_session_id, raw=tool.raw_name
            )
            new_tools.append(tool)
            continue
        log.info(
            "ws.tool_resolved",
            session_id=ctx.ws_session_id,
            raw=tool.raw_name,
            resolved_id=int(chosen["id"]),
            variety=chosen.get("variety"),
        )
        new_tools.append(
            ResolvedTool(
                raw_name=tool.raw_name,
                resolved_id=int(chosen["id"]),
                resolved_name=str(chosen["name"]),
                variety=chosen.get("variety"),
            )
        )

    return draft.model_copy(update={"tools": new_tools})


async def _resolve_doers(
    ctx: _SessionContext, draft: InterventionDraft
) -> InterventionDraft:
    """Resolve each unresolved doer's raw_name to a tenant worker product_id.

    Mirror of `_resolve_tools` for the `doer` role: without resolution the
    mapper drops the doer and the operator never reaches the saved
    intervention. Variety ("worker") is carried for slot routing.
    """
    if not any(d.resolved_id is None and d.raw_name for d in draft.doers):
        return draft

    workers = await _load_tenant_workers(ctx)
    if not workers:
        return draft

    new_doers: list[ResolvedDoer] = []
    for doer in draft.doers:
        if doer.resolved_id is not None or not doer.raw_name:
            new_doers.append(doer)
            continue
        chosen = _match_product(doer.raw_name, workers)
        if chosen is None:
            log.info(
                "ws.doer_unresolved", session_id=ctx.ws_session_id, raw=doer.raw_name
            )
            new_doers.append(doer)
            continue
        log.info(
            "ws.doer_resolved",
            session_id=ctx.ws_session_id,
            raw=doer.raw_name,
            resolved_id=int(chosen["id"]),
            variety=chosen.get("variety"),
        )
        new_doers.append(
            ResolvedDoer(
                raw_name=doer.raw_name,
                resolved_id=int(chosen["id"]),
                resolved_name=str(chosen["name"]),
                variety=chosen.get("variety"),
            )
        )

    return draft.model_copy(update={"doers": new_doers})


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

    # Clients don't echo `raw_text` back on confirm (it's excluded from the
    # `intervention_draft` payload), so the validated draft has none. Re-attach
    # the user's original phrase from the server-side cache so the mapper can
    # surface it as the Ekylibre intervention `description`.
    server_draft = ctx.drafts.get(msg.id)
    if server_draft and server_draft.raw_text and not draft.raw_text:
        draft = draft.model_copy(update={"raw_text": server_draft.raw_text})

    # Convert per-area inputs ("2 L/ha") to absolute totals here, before
    # the mapper runs. Procedo's density `forward` formulas divide by
    # `PRODUCT.net_volume(liter)` (or `net_mass(kilogram)`) and explode on
    # products without those readings — common for fertilisers / mixtures
    # like "bouillie bordelaise". Absolute totals + handler="population"
    # bypass the formula path entirely.
    draft = await _convert_density_inputs_to_absolute(ctx, draft)

    procedure_spec = None
    if ctx.procedure_registry and draft.procedure_name:
        # `get_or_fetch` returns the cached spec when the registry is
        # already hydrated and falls back to a single `GET /api/v2/procedures/:id`
        # otherwise — important so the Procedo-aware mapping works even
        # when a session confirms before the background hydration finishes.
        procedure_spec = await ctx.procedure_registry.get_or_fetch(
            draft.procedure_name, ctx.api
        )

    try:
        created = await ctx.recorder.confirm(ctx.api, draft, procedure_spec=procedure_spec)
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
