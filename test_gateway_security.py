from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent


class GatewaySecurityTests(unittest.TestCase):
    def test_tls_gateway_requires_basic_auth_but_health_stays_probeable(self):
        config = (ROOT / "deploy/nginx/tls.conf.template").read_text(encoding="utf-8")

        health = config.split("location = /nginx-health", 1)[1].split("location /", 1)[0]
        protected = config.split("location /", 1)[1]
        self.assertNotIn("auth_basic", health)
        self.assertIn('auth_basic "RAG Assistant";', protected)
        self.assertIn("auth_basic_user_file /run/secrets/gateway_htpasswd;", protected)

    def test_compose_mounts_gateway_password_as_a_secret(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

        mounted_secret = compose["services"]["tls-proxy"]["secrets"][0]
        self.assertEqual(mounted_secret["source"], "gateway_htpasswd")
        self.assertEqual(mounted_secret["target"], "gateway_htpasswd")
        self.assertEqual(mounted_secret["mode"], 0o444)
        secret_file = compose["secrets"]["gateway_htpasswd"]["file"]
        self.assertIn("RAG_GATEWAY_HTPASSWD_FILE", secret_file)


if __name__ == "__main__":
    unittest.main()
