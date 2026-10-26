"""JSON Schemas for the two model-produced artifacts in the P0 graph."""

from __future__ import annotations

from typing import Any

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "metric",
        "metric_version",
        "dimensions",
        "filters",
        "time_range",
        "comparison",
        "limit",
    ],
    "properties": {
        "metric": {"type": "string", "enum": ["gmv"]},
        "metric_version": {"type": "integer", "minimum": 1},
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
        },
        "filters": {"type": "object"},
        "time_range": {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end_exclusive", "timezone"],
            "properties": {
                "start": {"type": "string"},
                "end_exclusive": {"type": "string"},
                "timezone": {"type": "string"},
            },
        },
        "comparison": {"type": "string", "enum": ["previous_period"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}

COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "findings",
        "hypotheses",
        "recommendations",
        "limitations",
    ],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "findings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "evidence_ids"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "evidence_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "status"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["unverified"]},
                },
            },
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}
