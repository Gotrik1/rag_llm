import unittest

from security import AuthorizationError, DevelopmentIdentityProvider, RAG_ACCESS, authorize


class SecurityTests(unittest.TestCase):
    def test_bootstrap_principal_has_full_access(self):
        principal = DevelopmentIdentityProvider("bootstrap").authenticate(None)
        authorize(principal, RAG_ACCESS)
        self.assertEqual(principal.subject, "bootstrap")

    def test_permission_is_required_for_non_admin(self):
        principal = DevelopmentIdentityProvider("bootstrap").authenticate("Bearer employee:")
        with self.assertRaises(AuthorizationError):
            authorize(principal, RAG_ACCESS)

    def test_development_permission_token_maps_to_rbac_permission(self):
        principal = DevelopmentIdentityProvider("bootstrap").authenticate("Bearer employee:rag.access")
        authorize(principal, RAG_ACCESS)


if __name__ == "__main__":
    unittest.main()
