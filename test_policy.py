import unittest
from unittest.mock import patch

from policy import PolicyProvider, evaluate_condition
from security import AuthorizationError, Principal, authorize


class PolicyTests(unittest.TestCase):
    def test_nested_condition_resolves_principal_and_resource(self):
        data = {"principal": {"department": "finance"}, "resource": {"department": "finance"}}
        condition = {"all": [
            {"attribute": "principal.department", "operator": "eq", "value": {"ref": "resource.department"}},
            {"attribute": "principal.department", "operator": "in", "value": ["finance", "legal"]},
        ]}
        self.assertTrue(evaluate_condition(condition, data))

    def test_explicit_deny_overrides_permission(self):
        principal = Principal("employee", permissions=frozenset({"rag.access"}))
        with patch.object(PolicyProvider, "decide", return_value=False):
            with self.assertRaises(AuthorizationError): authorize(principal, "rag.access")

    def test_policy_can_grant_access_without_role_permission(self):
        principal = Principal("employee")
        with patch.object(PolicyProvider, "decide", return_value=True): authorize(principal, "rag.access")


if __name__ == "__main__": unittest.main()
