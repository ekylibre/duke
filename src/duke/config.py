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

    # Default provider when the client doesn't pick one. Also the head of the
    # fallback chain. One of "claude" | "mistral" | "ollama".
    llm_default_provider: str = "claude"
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-4-7"
    mistral_api_key: str | None = None
    mistral_model: str = "mistral-large-latest"
    # Local Ollama provider (opt-in). `ollama_base_url` empty/None disables it.
    # The model must be pulled in the Ollama server (see docker compose
    # `local-llm` profile + the `ollama-pull` sidecar).
    ollama_base_url: str | None = None
    ollama_model: str = "mistral-nemo"
    llm_max_tokens_out: int = 1024
    llm_budget_tokens_per_session: int = 50000

    spacy_model: str = "fr_core_news_lg"
    # Path to a Duke-trained spaCy model produced by `duke.cli.train_ner`. When
    # set, this directory is loaded instead of `spacy_model` — it carries the
    # custom DUKE_* NER labels. Empty/None falls back to the base model.
    duke_ner_model_path: str | None = None

    hash_secret: str = "change-me-in-prod"
    retention_days_turn_text: int = 90

    # Server-side STT (Whisper) — opt-in fallback for browsers without
    # working Web Speech API (Firefox, some mobile contexts). When disabled,
    # the /api/v1/stt/transcribe endpoint returns 503.
    enable_server_stt: bool = False
    # faster-whisper model identifier. `small` (~480 MB) is a good fr-FR
    # tradeoff on CPU; bump to `medium` if quality is insufficient and you
    # have GPU. Loaded lazily on first transcription request.
    whisper_model: str = "small"
    # `cpu` (default) | `cuda` | `auto`. `cuda` requires the matching CUDA
    # libraries on the host and a `compute_type` like `float16`.
    whisper_device: str = "cpu"
    # `int8` is a good CPU default (smaller memory + fast); `float16` for GPU.
    whisper_compute_type: str = "int8"
    whisper_language: str = "fr"
    # Reject uploads larger than this (bytes). 10 MB ≈ 5 minutes of WebM/Opus
    # at typical browser bitrates — generous for a single utterance.
    stt_max_audio_bytes: int = 10 * 1024 * 1024
    # Optional cache dir override for the model weights (HF cache otherwise).
    whisper_cache_dir: str | None = None

    @field_validator("allowed_ws_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
