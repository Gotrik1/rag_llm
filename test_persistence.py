import unittest
from unittest.mock import patch

from policy import PolicyProvider
from persistence import Base, Persistence, sqlalchemy_database_url
from security import Principal


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.store = Persistence("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.store.engine)

    def test_bootstrap_superadmin_is_idempotent_and_enriches_identity(self):
        self.store.bootstrap_superadmin("root")
        self.store.bootstrap_superadmin("root")
        principal = self.store.enrich_principal(Principal("root", provider="development"))
        self.assertIn("superadmin", principal.roles)
        self.assertIn("*", principal.permissions)

    def test_job_state_and_cancellation_are_persistent(self):
        job = self.store.create_job("ingestion", {"path": "doc.docx"})
        self.assertTrue(self.store.request_cancel(job["id"]))
        loaded = self.store.get_job(job["id"])
        self.assertTrue(loaded["cancel_requested"])
        self.store.update_job(job["id"], status="cancelled", progress=100)
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")

    def test_role_grant_is_returned_when_identity_is_enriched(self):
        self.store.grant_role("employee", "rag-user", ["rag.access"])
        principal = self.store.enrich_principal(Principal("employee", provider="oidc"))
        self.assertIn("rag-user", principal.roles)
        self.assertIn("rag.access", principal.permissions)

    def test_persisted_abac_policy_evaluates_attributes(self):
        self.store.upsert_policy({
            "name": "finance-only", "effect": "allow", "action": "rag.access", "resource": "/api/*",
            "condition": {"attribute": "principal.department", "operator": "eq", "value": "finance"},
        })
        with patch("policy.get_store", return_value=self.store):
            self.assertTrue(PolicyProvider().decide(Principal("f", attributes={"department": "finance"}), "rag.access", "/api/ask"))
            self.assertIsNone(PolicyProvider().decide(Principal("e", attributes={"department": "it"}), "rag.access", "/api/ask"))


class PersistenceConfigurationTests(unittest.TestCase):
    def test_postgresql_url_uses_installed_psycopg_v3_driver(self) -> None:
        self.assertEqual(
            sqlalchemy_database_url("postgresql://rag:secret@postgres:5432/rag"),
            "postgresql+psycopg://rag:secret@postgres:5432/rag",
        )

    def test_explicit_sqlalchemy_driver_is_preserved(self) -> None:
        configured = "postgresql+psycopg://rag:secret@postgres:5432/rag"
        self.assertEqual(sqlalchemy_database_url(configured), configured)


if __name__ == "__main__":
    unittest.main()
