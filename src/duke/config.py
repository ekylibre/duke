from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    duke_bind: str = "0.0.0.0:8000"

    ekylibre_api_base_url: str
    ekylibre_api_timeout_s: float = 10.0

    ekylibre_db_dsn: str
    ekylibre_db_pool_min: int = 2
    ekylibre_db_pool_max: int = 20

    duke_db_dsn: str

    log_level: str = "INFO"
    log_verbose_payloads: bool = False

    allowed_ws_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    migrate_on_boot: bool = False
    session_idle_timeout_s: int = 1800
    rate_limit_per_min: int = 30

    llm_default_provider: str = "claude"
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-4-7"
    mistral_api_key: str | None = None
    mistral_model: str = "mistral-large-latest"
    llm_max_tokens_out: int = 1024
    llm_budget_tokens_per_session: int = 50000

    spacy_model: str = "fr_core_news_lg"

    hash_secret: str = "change-me-in-prod"
    retention_days_turn_text: int = 90

    @field_validator("allowed_ws_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
