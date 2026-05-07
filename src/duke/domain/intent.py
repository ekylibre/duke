from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Intent(StrEnum):
    RECORD_INTERVENTION = "record_intervention"
    QA_STOCK = "qa_stock"
    QA_HISTORY = "qa_history"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    confidence: float
