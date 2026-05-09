"""JSON Schemas exposed to the LLM as callable tools (function calling)."""

from __future__ import annotations

EXTRACT_INTERVENTION_TOOL_NAME = "extract_intervention"

EXTRACT_INTERVENTION_DESCRIPTION = (
    "Extract structured fields for an Ekylibre agricultural intervention from a "
    "French sentence. Use the provided hints (lexicon candidates, spaCy entities, "
    "temporal extraction) as priors. Leave any field you are not confident about "
    "as null and append an entry to `ambiguities` instead of guessing."
)

EXTRACT_INTERVENTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "procedure_name": {
            "type": ["string", "null"],
            "description": (
                "Procedo name in snake_case (e.g. 'spraying', 'vine_spraying', "
                "'sowing'). Must come from the lexicon procedures provided in the hints."
            ),
        },
        "started_at": {
            "type": ["string", "null"],
            "format": "date-time",
            "description": "ISO 8601 datetime with timezone.",
        },
        "stopped_at": {
            "type": ["string", "null"],
            "format": "date-time",
        },
        "working_duration_seconds": {
            "type": ["integer", "null"],
            "description": (
                "Working duration in seconds. Use only if the user explicitly mentions a duration."
            ),
        },
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "raw_name": {"type": "string"},
                    "resolved_id": {"type": ["integer", "null"]},
                    "resolved_name": {"type": ["string", "null"]},
                    "kind": {"type": "string", "enum": ["land_parcel", "activity"]},
                },
                "required": ["raw_name"],
            },
        },
        "inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "raw_name": {"type": "string"},
                    "resolved_product_id": {"type": ["integer", "null"]},
                    "resolved_product_name": {"type": ["string", "null"]},
                    "quantity_value": {"type": ["number", "null"]},
                    "quantity_unit": {
                        "type": ["string", "null"],
                        "description": (
                            "Unit name in snake_case. "
                            "Absolute quantities: 'liter', 'kilogram', 'gram', 'unit'. "
                            "Density quantities (when the user says 'X/ha', 'X par hectare', "
                            "'X par m²' or similar) MUST use the per-area form: "
                            "'liter_per_hectare', 'kilogram_per_hectare', "
                            "'liter_per_square_meter', 'hectoliter_per_hectare'. "
                            "Examples: '2 L/ha' → quantity_value=2, quantity_unit='liter_per_hectare'; "
                            "'5 L' → quantity_value=5, quantity_unit='liter'."
                        ),
                    },
                },
                "required": ["raw_name"],
            },
        },
        "doers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"raw_name": {"type": "string"}},
                "required": ["raw_name"],
            },
        },
        "tools": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"raw_name": {"type": "string"}},
                "required": ["raw_name"],
            },
        },
        "ambiguities": {
            "type": "array",
            "description": (
                "List of fields the model could not determine; the orchestrator will ask the user."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string"},
                    "raw_value": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "question": {"type": "string"},
                },
                "required": ["field", "raw_value", "question"],
            },
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
    },
    "required": ["procedure_name", "targets", "inputs", "ambiguities", "confidence"],
}
