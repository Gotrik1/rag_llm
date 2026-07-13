"""ABAC policy evaluator with explicit-deny precedence."""
from __future__ import annotations

import fnmatch
from typing import Any, Mapping

from persistence import get_store
from security import Principal


def _value(spec: Any, data: Mapping[str, Any]) -> Any:
    if not isinstance(spec, dict) or "ref" not in spec:
        return spec
    value: Any = data
    for part in str(spec["ref"]).split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def evaluate_condition(condition: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
    if not condition: return True
    if "all" in condition: return all(evaluate_condition(item, data) for item in condition["all"])
    if "any" in condition: return any(evaluate_condition(item, data) for item in condition["any"])
    if "not" in condition: return not evaluate_condition(condition["not"], data)
    left = _value({"ref": condition.get("attribute")}, data)
    right = _value(condition.get("value"), data)
    operator = condition.get("operator", "eq")
    if operator == "eq": return left == right
    if operator == "ne": return left != right
    if operator == "in": return left in (right or [])
    if operator == "contains": return right in (left or [])
    if operator == "exists": return (left is not None) is bool(right)
    return False


class PolicyProvider:
    def decide(self, principal: Principal, action: str, resource: str, context: Mapping[str, Any] | None = None) -> bool | None:
        data = {
            "principal": {"subject": principal.subject, "roles": list(principal.roles), "permissions": list(principal.permissions), **dict(principal.attributes)},
            "resource": {"id": resource, **dict((context or {}).get("resource", {}))},
            "context": dict(context or {}),
        }
        effects = [row["effect"] for row in get_store().active_policies(action) if fnmatch.fnmatch(resource, row["resource"]) and evaluate_condition(row["condition"], data)]
        if "deny" in effects: return False
        if "allow" in effects: return True
        return None


_provider = PolicyProvider()


def policy_provider() -> PolicyProvider:
    return _provider
