"""Neutral output shape for a grounding judge, without example verdicts."""

from typing import Any


def grounding_response_schema(
    *, task_relevance: bool = False, task_scope: bool = False
) -> dict[str, Any]:
    """Return a fresh schema; actual IDs and semantic decisions belong elsewhere."""
    properties: dict[str, Any] = {
        "claim_id": {"type": "string", "pattern": r"^C[1-9]\d*$"},
        "context_ids": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^H[1-9]\d*$"},
        },
        "support": {
            "type": "string",
            "enum": ["supported", "unsupported", "contradicted", "uncertain"],
        },
        "passage_ids": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "pattern": r"^E[1-9]\d*:P[1-9]\d*$"},
        },
    }
    required = ["claim_id", "context_ids", "support", "passage_ids"]
    if task_relevance:
        properties["task_relevance"] = {
            "type": "string",
            "enum": ["matched", "mismatch", "uncertain"],
        }
        required.append("task_relevance")
    if task_scope:
        properties["scope_disposition"] = {
            "type": "string",
            "enum": ["in_scope", "excluded", "mixed"],
        }
        properties["excluded_topic_ids"] = {
            "type": "array",
            "items": {"type": "string", "pattern": r"^X[1-9]\d*$"},
        }
        required.extend(["scope_disposition", "excluded_topic_ids"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    }
