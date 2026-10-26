"""Pure tool-gateway policy/schema helpers."""

import pytest

from agentforge_harness.gateway import (
    ToolInputError,
    ToolPolicyError,
    compute_invocation_key,
    merge_policy,
    validate_schema,
)

TOOL_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["metric"],
    "properties": {"metric": {"type": "string", "enum": ["gmv"]}},
}


def test_invocation_key_is_stable_and_request_sensitive():
    first = compute_invocation_key("execute_metrics:0", {"b": 2, "a": 1})
    reordered = compute_invocation_key("execute_metrics:0", {"a": 1, "b": 2})
    changed = compute_invocation_key("execute_metrics:0", {"a": 1, "b": 3})
    assert first == reordered
    assert first != changed
    assert first.startswith("execute_metrics:0:")
    assert len(first.rsplit(":", 1)[-1]) == 64


def test_policy_override_may_only_tighten():
    assert merge_policy(
        {"max_calls": 5, "allowed_metrics": ["gmv", "orders"]},
        {"max_calls": 3, "allowed_metrics": ["gmv"]},
    ) == {"max_calls": 3, "allowed_metrics": ["gmv"]}


def test_require_override_may_tighten_false_to_true():
    assert merge_policy(
        {"require_approval": False}, {"require_approval": True}
    ) == {"require_approval": True}


def test_require_override_may_keep_true():
    assert merge_policy(
        {"require_approval": True}, {"require_approval": True}
    ) == {"require_approval": True}


@pytest.mark.parametrize(
    "base,override",
    [
        (False, False),
        (True, False),
        ("false", True),
        (False, "true"),
    ],
)
def test_require_override_must_be_a_true_boolean(base, override):
    with pytest.raises(ToolPolicyError):
        merge_policy({"require_approval": base}, {"require_approval": override})


@pytest.mark.parametrize(
    "override",
    [
        {"max_calls": 6},
        {"allowed_metrics": ["free_sql"]},
        {"max_calls": "3"},
        {"unknown_key": 1},
    ],
)
def test_policy_override_loosening_or_unknown_is_rejected(override):
    with pytest.raises(ToolPolicyError):
        merge_policy({"max_calls": 5, "allowed_metrics": ["gmv"]}, override)


def test_schema_validation_accepts_valid_input():
    validate_schema({"metric": "gmv"}, TOOL_INPUT_SCHEMA, error_code="TOOL_INPUT_INVALID")


def test_schema_validation_rejects_invalid_input_with_details():
    with pytest.raises(ToolInputError) as raised:
        validate_schema(
            {"metric": "free_sql", "sql": "select 1"},
            TOOL_INPUT_SCHEMA,
            error_code="TOOL_INPUT_INVALID",
        )
    assert raised.value.code == "TOOL_INPUT_INVALID"
    assert raised.value.details


def test_metric_query_plan_schema_defers_registration_to_semantic_validator():
    from agentforge_harness.schemas import PLAN_SCHEMA

    validate_schema(
        {
            "metric": "unregistered_metric",
            "metric_version": 1,
            "dimensions": ["region_code"],
            "filters": {"region_code": ["CN_EAST"]},
            "time_range": {
                "start": "2025-03-31",
                "end_exclusive": "2025-04-07",
                "timezone": "UTC",
            },
            "comparison": "previous_period",
            "limit": 100,
        },
        PLAN_SCHEMA,
        error_code="MODEL_OUTPUT_INVALID",
    )
