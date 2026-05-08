"""In-memory cache of Ekylibre's Procedo registry.

The static `DEFAULT_PROCEDURES` list shipped with Duke only knows about a
handful of procedures and gets out of sync as Ekylibre adds new ones. We pull
the live registry once per service lifetime via `GET /api/v2/procedures` and:

1. Re-seed the lexicon so spaCy/LLM hints use the canonical Procedo names
   (e.g. `generic_tillage` for "labour" rather than the bogus `ploughing`).
2. Keep the full `ProcedureSpec` (with parameters) in `_specs_by_name` so the
   mapper can pick the right `reference_name` per Procedo slot when building
   intervention payloads.

Hydration is best-effort: if the API call fails (network, auth, 5xx), we keep
serving with the static defaults. Subsequent requests retry on next session.
"""

from __future__ import annotations

import asyncio

import structlog

from duke.integration.ekylibre.api_client import EkylibreApiClient, ProcedureSpec
from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    Lexicon,
    LexiconRepository,
    ProcedureEntry,
)

log = structlog.get_logger(__name__)


class ProcedureRegistry:
    """Process-wide cache of `ProcedureSpec` keyed by procedure name."""

    def __init__(self) -> None:
        self._specs_by_name: dict[str, ProcedureSpec] = {}
        self._hydrated = False
        self._lock = asyncio.Lock()

    @property
    def hydrated(self) -> bool:
        return self._hydrated

    def get(self, name: str) -> ProcedureSpec | None:
        return self._specs_by_name.get(name)

    def all(self) -> list[ProcedureSpec]:
        return list(self._specs_by_name.values())

    async def get_or_fetch(
        self, name: str, api: EkylibreApiClient
    ) -> ProcedureSpec | None:
        """Return the cached spec for `name`, falling back to a one-shot
        `GET /api/v2/procedures/:id` if the registry hasn't been hydrated
        yet (or if Ekylibre added the procedure after our last hydrate).
        """
        cached = self._specs_by_name.get(name)
        if cached is not None:
            return cached
        try:
            spec = await api.get_procedure(name)
        except Exception as exc:
            log.warning(
                "procedure_registry.fetch_failed", name=name, error=str(exc)
            )
            return None
        if spec is not None:
            self._specs_by_name[spec.name] = spec
        return spec

    async def hydrate(
        self,
        api: EkylibreApiClient,
        lexicon_repo: LexiconRepository,
        *,
        force: bool = False,
    ) -> int:
        """Fetch procedures from `api` and re-seed `lexicon_repo`.

        Idempotent — only hits the API the first time unless `force=True`.
        Returns the number of procedures hydrated (0 if it already happened
        or if the call failed).
        """
        async with self._lock:
            if self._hydrated and not force:
                return 0

            try:
                specs = await api.list_procedures()
            except Exception as exc:
                log.warning(
                    "procedure_registry.hydrate_failed",
                    error=str(exc),
                    fallback="default_procedures",
                )
                return 0

            if not specs:
                log.warning("procedure_registry.hydrate_empty")
                return 0

            self._specs_by_name = {spec.name: spec for spec in specs}

            # Preserve curated French aliases for procedures we already knew
            # — the API only returns the human label, no aliases. Without this
            # merge the spaCy/LLM hints would lose "labour", "vendange", etc.
            default_aliases = {p.name: p.aliases for p in DEFAULT_PROCEDURES}
            entries = [
                ProcedureEntry(
                    name=spec.name,
                    label=spec.human_name or spec.name,
                    aliases=default_aliases.get(spec.name, ()),
                )
                for spec in specs
            ]

            current = lexicon_repo.lexicon
            new_lexicon = Lexicon(
                products=current.products,
                procedures=entries,
                units=current.units,
            )
            # `_index` is private but it's the only seed path on the base
            # repo. Calling it directly is acceptable since both the in-memory
            # and Postgres impls inherit from `_BaseLexiconRepository`.
            lexicon_repo._index(new_lexicon)  # type: ignore[attr-defined]

            self._hydrated = True
            log.info(
                "procedure_registry.hydrated",
                count=len(specs),
                kept_aliases_for=len(default_aliases),
            )
            return len(specs)


def parameter_name_for_role(spec: ProcedureSpec | None, role: str) -> str | None:
    """Return the first procedure parameter whose `type` matches `role`.

    `role` is one of "target", "input", "tool", "doer", "output". Returns
    None if `spec` is missing, has no parameters, or no parameter of that
    type exists. Top-level GroupParameters are skipped — most procedures
    expose target/input/tool/doer at the top level.
    """
    if spec is None:
        return None
    for param in spec.parameters:
        if param.type == role:
            return param.name
    return None
