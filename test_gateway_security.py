from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent


class GatewaySecurityTests(unittest.TestCase):
    def test_tls_gateway_uses_application_auth_without_basic_challenge(self):
        config = (ROOT / "deploy/nginx/tls.conf.template").read_text(encoding="utf-8")

        health = config.split("location = /nginx-health", 1)[1].split("location /", 1)[0]
        protected = config.split("location /", 1)[1]
        self.assertNotIn("auth_basic", health)
        self.assertNotIn("auth_basic", protected)
        self.assertIn("location ~ ^/api/auth/(login|register)$", config)
        self.assertIn("limit_req zone=rag_auth", config)
        self.assertIn('proxy_set_header Authorization "";', protected)
        self.assertIn('proxy_set_header Proxy-Authorization "";', protected)

    def test_compose_does_not_mount_a_shared_gateway_password(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

        self.assertNotIn("secrets", compose["services"]["tls-proxy"])
        self.assertNotIn("gateway_htpasswd", compose["secrets"])
        self.assertEqual(compose["x-app-environment"]["RAG_AUTH_MODE"], "${RAG_AUTH_MODE:-local}")
        self.assertEqual(compose["x-app-environment"]["RAG_SESSION_COOKIE_SECURE"], "${RAG_SESSION_COOKIE_SECURE:-true}")


if __name__ == "__main__":
    unittest.main()
