from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict


class EkylibreError(Exception):
    """Base for any Ekylibre integration error."""


class EkylibreAuthError(EkylibreError):
    """Token rejected (401) by Ekylibre API."""


class EkylibreTenantError(EkylibreError):
    """Tenant not found (404) on Ekylibre API."""


class EkylibreUnavailableError(EkylibreError):
    """Network error or 5xx from Ekylibre API."""


class EkylibreBadRequestError(EkylibreError):
    """4xx other than 401/404."""


@dataclass(frozen=True)
class EkylibreCredentials:
    email: str
    token: str
    tenant: str
    base_url: str


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    email: str
    full_name: str
    locale: str = "fr"
    role: str | None = None


class CreatedIntervention(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    url: str | None = None


class ProcedureCategory(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    human_name: str | None = None


class ProcedureAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    human_name: str | None = None


class ProcedureCardinality(BaseModel):
    model_config = ConfigDict(extra="ignore")
    minimum: int | None = None
    maximum: int | None = None


class ProcedureParameter(BaseModel):
    """One parameter slot in a Procedo procedure (target, input, tool, doer, …).

    Procedo nests parameters arbitrarily via `GroupParameter`; this schema keeps
    the recursion shallow by typing `parameters` as a list of dicts. Callers
    that need the deep tree can re-parse, but the canonicalization logic only
    needs the flat fields (`name`, `type`, `required`).
    """

    model_config = ConfigDict(extra="allow")
    name: str
    human_name: str | None = None
    type: str | None = None
    # Procedo filter expression (e.g. "is motorized_vehicle and can tow(equipment)").
    # Used to route a resolved tool/doer to the most specific matching slot.
    filter: str | None = None
    cardinality: ProcedureCardinality | None = None
    required: bool | None = None


class ProcedureSpec(BaseModel):
    """Snapshot of a Procedo procedure as exposed by `/api/v2/procedures`.

    Only the fields Duke actually consumes are typed; the rest are passed
    through via `extra="allow"` so a future caller can reach `varieties`,
    `categories`, etc. without a schema change.
    """

    model_config = ConfigDict(extra="allow")
    name: str
    human_name: str | None = None
    position: int | None = None
    deprecated: bool = False
    hidden: bool = False
    categories: list[ProcedureCategory] = []
    mandatory_actions: list[ProcedureAction] = []
    optional_actions: list[ProcedureAction] = []
    activity_families: list[str] = []
    varieties: list[str] = []
    parameters: list[ProcedureParameter] = []


class EkylibreApiClient:
    def __init__(self, creds: EkylibreCredentials, http: httpx.AsyncClient) -> None:
        self._creds = creds
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"simple-token {self._creds.email} {self._creds.token}".strip(),
            "X-Tenant": self._creds.tenant,
            "Accept": "application/json",
            "Accept-Language": "fr",
        }

    async def validate_token(self) -> User:
        try:
            resp = await self._http.get(
                "/api/v2/users/me",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise EkylibreUnavailableError(str(exc)) from exc

        if resp.status_code == 401:
            raise EkylibreAuthError("invalid token")
        if resp.status_code == 404:
            raise EkylibreTenantError("unknown tenant")
        if 500 <= resp.status_code < 600:
            raise EkylibreUnavailableError(f"upstream {resp.status_code}")
        if not resp.is_success:
            raise EkylibreBadRequestError(f"unexpected {resp.status_code}: {resp.text[:200]}")

        return User.model_validate(resp.json())

    async def create_intervention(self, payload: dict[str, Any]) -> CreatedIntervention:
        try:
            resp = await self._http.post(
                "/api/v2/interventions",
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise EkylibreUnavailableError(str(exc)) from exc

        if resp.status_code == 401:
            raise EkylibreAuthError("invalid token")
        if resp.status_code == 404:
            raise EkylibreTenantError("unknown tenant")
        if 500 <= resp.status_code < 600:
            raise EkylibreUnavailableError(f"upstream {resp.status_code}")
        if not resp.is_success:
            raise EkylibreBadRequestError(f"unexpected {resp.status_code}: {resp.text[:200]}")

        return CreatedIntervention.model_validate(resp.json())

    async def list_procedures(
        self,
        *,
        category: str | None = None,
        procedure_action: str | None = None,
        activity_family: str | None = None,
        include_deprecated: bool = False,
        include_hidden: bool = False,
    ) -> list[ProcedureSpec]:
        """`GET /api/v2/procedures`.

        Returns the full Procedo registry (filtered server-side). Use this to
        canonicalize procedure names and feed the lexicon — Ekylibre's
        Procedo registry is the source of truth, the static defaults in
        `lexicon_repo.DEFAULT_PROCEDURES` are only a bootstrap fallback.

        `procedure_action` filters by Procedo action name. The query-string
        key is also `procedure_action` (not `action`) because Rails reserves
        `params[:action]` for the routed controller action.
        """
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        if procedure_action:
            params["procedure_action"] = procedure_action
        if activity_family:
            params["activity_family"] = activity_family
        if include_deprecated:
            params["include_deprecated"] = "true"
        if include_hidden:
            params["include_hidden"] = "true"

        try:
            resp = await self._http.get(
                "/api/v2/procedures",
                headers=self._headers(),
                params=params or None,
            )
        except httpx.HTTPError as exc:
            raise EkylibreUnavailableError(str(exc)) from exc

        self._raise_for_status(resp)
        body = resp.json()
        if not isinstance(body, list):
            raise EkylibreBadRequestError(f"expected array, got {type(body).__name__}")
        return [ProcedureSpec.model_validate(item) for item in body]

    async def get_procedure(self, name: str) -> ProcedureSpec | None:
        """`GET /api/v2/procedures/:id`. Returns None on 404 (unknown procedure)."""
        try:
            resp = await self._http.get(
                f"/api/v2/procedures/{name}",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise EkylibreUnavailableError(str(exc)) from exc

        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        return ProcedureSpec.model_validate(resp.json())

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise EkylibreAuthError("invalid token")
        if resp.status_code == 404:
            raise EkylibreTenantError("unknown tenant")
        if 500 <= resp.status_code < 600:
            raise EkylibreUnavailableError(f"upstream {resp.status_code}")
        if not resp.is_success:
            raise EkylibreBadRequestError(f"unexpected {resp.status_code}: {resp.text[:200]}")
