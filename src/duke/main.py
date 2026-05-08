from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import httpx
import structlog
from fastapi import FastAPI, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from duke.application.intervention_recorder import InterventionRecorder
from duke.application.orchestrator import ConversationOrchestrator
from duke.application.query_answerer import QueryAnswerer
from duke.config import Settings, get_settings
from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    DEFAULT_UNITS,
    InMemoryLexiconRepository,
    Lexicon,
    LexiconRepository,
)
from duke.integration.ekylibre.procedure_registry import ProcedureRegistry
from duke.integration.ekylibre.read_db import EkylibreReadDb
from duke.integration.store.repositories import ConversationRepository
from duke.nlu.llm.base import LLMProvider
from duke.nlu.llm.router import LLMRouter
from duke.nlu.pipeline import NlpPipeline
from duke.observability.logging import RequestContextMiddleware, configure_logging
from duke.observability.metrics import render_metrics
from duke.transport.ws_server import ws_endpoint

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.log_verbose_payloads)
    log.info("startup.begin", duke_bind=settings.duke_bind)

    if settings.migrate_on_boot:
        await _run_migrations(settings.duke_db_dsn)

    duke_engine: AsyncEngine = create_async_engine(settings.duke_db_dsn, pool_pre_ping=True)
    duke_sessionmaker = async_sessionmaker(duke_engine, expire_on_commit=False)

    ekylibre_pool = await asyncpg.create_pool(
        dsn=settings.ekylibre_db_dsn,
        min_size=settings.ekylibre_db_pool_min,
        max_size=settings.ekylibre_db_pool_max,
        command_timeout=settings.ekylibre_api_timeout_s,
    )

    http_client = httpx.AsyncClient(
        base_url=settings.ekylibre_api_base_url,
        timeout=settings.ekylibre_api_timeout_s,
    )

    lexicon_repo: LexiconRepository = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )
    pipeline = NlpPipeline.build(
        settings.spacy_model,
        lexicon_repo,
        ner_model_path=settings.duke_ner_model_path,
    )
    pipeline.install_entity_ruler()

    primary, secondary = _build_llm_providers(settings)
    llm_router = LLMRouter(primary=primary, secondary=secondary)
    recorder = InterventionRecorder(pipeline=pipeline, llm=llm_router)

    read_db = EkylibreReadDb(ekylibre_pool)
    query_answerer = QueryAnswerer(
        pipeline=pipeline,
        lexicon_repo=lexicon_repo,
        read_db=read_db,
        llm=llm_router,
    )
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=query_answerer)

    app.state.settings = settings
    app.state.duke_engine = duke_engine
    app.state.duke_sessionmaker = duke_sessionmaker
    app.state.ekylibre_pool = ekylibre_pool
    app.state.read_db = read_db
    app.state.http_client = http_client
    app.state.lexicon_repo = lexicon_repo
    app.state.nlp_pipeline = pipeline
    app.state.llm_router = llm_router
    app.state.intervention_recorder = recorder
    app.state.query_answerer = query_answerer
    app.state.orchestrator = orchestrator
    app.state.conversation_repo = ConversationRepository(duke_sessionmaker)
    # Live Procedo registry: empty at boot, hydrated lazily on first auth
    # using the user's session credentials. The static DEFAULT_PROCEDURES
    # acts as a fallback while it's empty.
    app.state.procedure_registry = ProcedureRegistry()

    log.info(
        "startup.ready",
        llm_primary=primary.name,
        llm_secondary=secondary.name if secondary else None,
    )

    try:
        yield
    finally:
        log.info("shutdown.begin")
        await http_client.aclose()
        await ekylibre_pool.close()
        await duke_engine.dispose()
        log.info("shutdown.done")


async def _run_migrations(duke_db_dsn: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", duke_db_dsn)
    command.upgrade(cfg, "head")


def _build_llm_providers(settings: Settings) -> tuple[LLMProvider, LLMProvider | None]:
    from duke.nlu.llm.claude import ClaudeProvider
    from duke.nlu.llm.mistral import MistralProvider

    def _claude() -> LLMProvider | None:
        if not settings.anthropic_api_key:
            return None
        return ClaudeProvider.from_api_key(
            settings.anthropic_api_key,
            model=settings.claude_model,
            max_tokens=settings.llm_max_tokens_out,
        )

    def _mistral() -> LLMProvider | None:
        if not settings.mistral_api_key:
            return None
        return MistralProvider.from_api_key(
            settings.mistral_api_key,
            model=settings.mistral_model,
            max_tokens=settings.llm_max_tokens_out,
        )

    if settings.llm_default_provider == "mistral":
        primary = _mistral() or _claude()
        secondary = _claude() if primary and primary.name == "mistral" else None
    else:
        primary = _claude() or _mistral()
        secondary = _mistral() if primary and primary.name == "claude" else None

    if primary is None:
        log.warning("llm.no_provider_configured")
        primary = _NullLLMProvider()
    return primary, secondary


class _NullLLMProvider:
    name = "null"

    async def extract_intervention(self, text: str, hints: dict) -> dict:
        from duke.nlu.llm.base import LLMUnavailableError

        raise LLMUnavailableError(
            "no LLM provider configured (set ANTHROPIC_API_KEY or MISTRAL_API_KEY)"
        )

    async def health(self) -> bool:
        return False


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Duke", version="0.1.0", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_ws_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        checks: dict[str, str] = {}

        try:
            sessionmaker = app.state.duke_sessionmaker
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            checks["duke_db"] = "ok"
        except Exception as exc:
            log.warning("readyz.duke_db_fail", error=str(exc))
            checks["duke_db"] = "fail"

        try:
            pool: asyncpg.Pool = app.state.ekylibre_pool
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["ekylibre_db"] = "ok"
        except Exception as exc:
            log.warning("readyz.ekylibre_db_fail", error=str(exc))
            checks["ekylibre_db"] = "fail"

        all_ok = all(v == "ok" for v in checks.values())
        if not all_ok:
            response.status_code = 503
        return {"checks": checks}

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await ws_endpoint(websocket)

    return app


app = create_app()
