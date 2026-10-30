"""Contract checks for the neutral grounding response schema."""

import json

import pytest

from grounding_schema import grounding_response_schema


FLAGS = [(False, False), (True, False), (False, True), (True, True)]


def _row(*, task_relevance=False, task_scope=False):
    row = {
        "claim_id": "C2",
        "context_ids": ["H1", "H3"],
        "support": "uncertain",
        "passage_ids": [],
    }
    if task_relevance:
        row["task_relevance"] = "uncertain"
    if task_scope:
        row.update(scope_disposition="in_scope", excluded_topic_ids=[])
    return row


@pytest.fixture
def validator_type():
    # Validation uses an existing optional package; shape tests still run without it.
    return pytest.importorskip("jsonschema").Draft202012Validator


@pytest.mark.parametrize("task_relevance,task_scope", FLAGS)
def test_schema_requires_exactly_the_enabled_fields(task_relevance, task_scope):
    schema = grounding_response_schema(
        task_relevance=task_relevance, task_scope=task_scope
    )
    assert schema["type"] == "object"
    assert schema["required"] == ["claims"]
    assert schema["additionalProperties"] is False
    claims = schema["properties"]["claims"]
    assert claims["type"] == "array" and claims["maxItems"] == 100
    row = claims["items"]
    expected = {"claim_id", "context_ids", "support", "passage_ids"}
    if task_relevance:
        expected.add("task_relevance")
    if task_scope:
        expected.update({"scope_disposition", "excluded_topic_ids"})
    assert row["type"] == "object"
    assert set(row["properties"]) == set(row["required"]) == expected
    assert row["additionalProperties"] is False
    assert row["properties"]["passage_ids"]["maxItems"] == 32
    assert "maxItems" not in row["properties"]["context_ids"]


def test_schema_contains_no_examples_defaults_constants_or_uniqueness_rules():
    schema = grounding_response_schema(task_relevance=True, task_scope=True)

    def walk(value):
        if isinstance(value, dict):
            assert not {"default", "example", "examples", "const", "uniqueItems", "minItems"} & value.keys()
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    serialized = json.dumps(schema)
    assert all(identifier not in serialized for identifier in ("C2", "H3", "E7:P1", "X4"))


def test_each_call_returns_independent_nested_schema_objects():
    first = grounding_response_schema(task_relevance=True, task_scope=True)
    other = grounding_response_schema(task_relevance=True, task_scope=True)
    original_other = json.dumps(other, sort_keys=True)
    first["properties"]["claims"]["items"]["properties"]["support"]["enum"].append("poisoned")
    first["properties"]["claims"]["items"]["required"].clear()
    assert json.dumps(other, sort_keys=True) == original_other
    assert grounding_response_schema(task_relevance=True, task_scope=True) == other


@pytest.mark.parametrize("task_relevance,task_scope", FLAGS)
def test_enabled_schema_validates_real_response_shapes(validator_type, task_relevance, task_scope):
    schema = grounding_response_schema(
        task_relevance=task_relevance, task_scope=task_scope
    )
    validator_type.check_schema(schema)
    validator = validator_type(schema)
    assert validator.is_valid({"claims": []})
    row = _row(task_relevance=task_relevance, task_scope=task_scope)
    assert validator.is_valid({"claims": [row]})
    row.update(context_ids=[], support="supported", passage_ids=["E7:P1", "E42:P12"])
    assert validator.is_valid({"claims": [row]})
    for field in row:
        missing = dict(row)
        del missing[field]
        assert not validator.is_valid({"claims": [missing]}), field


@pytest.mark.parametrize("task_relevance,task_scope", FLAGS)
def test_disabled_features_are_not_accepted_as_extra_fields(validator_type, task_relevance, task_scope):
    validator = validator_type(grounding_response_schema(
        task_relevance=task_relevance, task_scope=task_scope
    ))
    row = _row(task_relevance=task_relevance, task_scope=task_scope)
    assert not validator.is_valid({"claims": [row], "extra": True})
    assert not validator.is_valid({"claims": [{**row, "extra": True}]})
    if not task_relevance:
        assert not validator.is_valid({"claims": [{**row, "task_relevance": "matched"}]})
    if not task_scope:
        assert not validator.is_valid({"claims": [{**row, "scope_disposition": "in_scope"}]})
        assert not validator.is_valid({"claims": [{**row, "excluded_topic_ids": []}]})


@pytest.mark.parametrize("support", ["supported", "unsupported", "contradicted", "uncertain"])
def test_all_support_decisions_are_allowed_without_a_default(validator_type, support):
    row = _row()
    row["support"] = support
    assert validator_type(grounding_response_schema()).is_valid({"claims": [row]})


@pytest.mark.parametrize("value", ["matched", "mismatch", "uncertain"])
def test_all_relevance_decisions_are_allowed(validator_type, value):
    row = _row(task_relevance=True)
    row["task_relevance"] = value
    assert validator_type(grounding_response_schema(task_relevance=True)).is_valid({"claims": [row]})


@pytest.mark.parametrize("value", ["in_scope", "excluded", "mixed"])
def test_scope_enum_does_not_prejudge_topic_membership(validator_type, value):
    row = _row(task_scope=True)
    row.update(scope_disposition=value, excluded_topic_ids=["X2"])
    assert validator_type(grounding_response_schema(task_scope=True)).is_valid({"claims": [row]})


@pytest.mark.parametrize("field,value", [
    ("claim_id", 2), ("claim_id", "C0"), ("claim_id", "H1"), ("claim_id", "C1 extra"),
    ("context_ids", "H1"), ("context_ids", [1]), ("context_ids", ["H0"]),
    ("support", True), ("support", "yes"), ("support", "not_supported"),
    ("passage_ids", "E7:P1"), ("passage_ids", [7]), ("passage_ids", ["E7"]),
    ("passage_ids", ["E0:P1"]), ("passage_ids", ["E7:P0"]),
    ("passage_ids", ["E7:P1"] * 33),
    ("task_relevance", False), ("task_relevance", "relevant"),
    ("scope_disposition", []), ("scope_disposition", "out_of_scope"),
    ("excluded_topic_ids", "X1"), ("excluded_topic_ids", [False]),
    ("excluded_topic_ids", ["X0"]),
])
def test_invalid_row_types_ids_enums_and_passage_limit_are_rejected(validator_type, field, value):
    row = _row(task_relevance=True, task_scope=True)
    row[field] = value
    validator = validator_type(grounding_response_schema(task_relevance=True, task_scope=True))
    assert not validator.is_valid({"claims": [row]})


@pytest.mark.parametrize("document", [None, [], {}, {"claims": {}}, {"claims": [False]}])
def test_invalid_top_level_types_and_missing_claims_are_rejected(validator_type, document):
    assert not validator_type(grounding_response_schema()).is_valid(document)


def test_only_declared_array_bounds_are_enforced(validator_type):
    validator = validator_type(grounding_response_schema())
    row = _row()
    row.update(context_ids=["H1"] * 101, passage_ids=["E7:P1"] * 32)
    assert validator.is_valid({"claims": [row] * 100})
    assert not validator.is_valid({"claims": [row] * 101})
