from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolvedTarget(_Strict):
    """A land parcel or activity that an intervention applies to."""

    kind: Literal["land_parcel", "activity"] = "land_parcel"
    raw_name: str
    resolved_id: int | None = None
    resolved_name: str | None = None
    confidence: float = 0.0


class ResolvedInput(_Strict):
    """A product input (chemical, fertilizer, seed, ...) used by the intervention."""

    raw_name: str
    resolved_product_id: int | None = None
    resolved_product_name: str | None = None
    quantity_value: float | None = None
    quantity_unit: str | None = None
    confidence: float = 0.0


class ResolvedDoer(_Strict):
    raw_name: str
    resolved_id: int | None = None


class ResolvedTool(_Strict):
    raw_name: str
    resolved_id: int | None = None


class Ambiguity(_Strict):
    """A clarification the user must resolve before the draft can be confirmed."""

    field: str
    raw_value: str
    options: list[str] = Field(default_factory=list)
    question: str
