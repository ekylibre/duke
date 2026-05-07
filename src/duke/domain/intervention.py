from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from duke.domain.entities import (
    Ambiguity,
    ResolvedDoer,
    ResolvedInput,
    ResolvedTarget,
    ResolvedTool,
)


class InterventionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    procedure_name: str | None = None
    nature: str = "record"

    started_at: datetime | None = None
    stopped_at: datetime | None = None
    working_duration: timedelta | None = None

    targets: list[ResolvedTarget] = Field(default_factory=list)
    inputs: list[ResolvedInput] = Field(default_factory=list)
    doers: list[ResolvedDoer] = Field(default_factory=list)
    tools: list[ResolvedTool] = Field(default_factory=list)

    ambiguities: list[Ambiguity] = Field(default_factory=list)
    confidence: float = 0.0

    raw_text: str | None = None

    def is_ready_for_post(self) -> bool:
        """Return True if the draft has the minimum required fields to be POSTed."""
        return (
            self.procedure_name is not None
            and self.started_at is not None
            and len(self.targets) > 0
            and not self.ambiguities
        )
