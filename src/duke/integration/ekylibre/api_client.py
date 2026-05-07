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
