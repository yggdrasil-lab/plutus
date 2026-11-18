"""Unit tests for integration token loading and fail-closed auth."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.routers.integrations import verify_integration_token


class TestIntegrationsAuth(unittest.TestCase):
    def test_settings_loads_mcp_token_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write("super-secret-token\n")
            tmp_path = tmp.name
        try:
            loaded = Settings(mcp_token="", mcp_token_file=tmp_path)
            self.assertEqual(loaded.mcp_token, "super-secret-token")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_verify_integration_token_fails_closed_in_production(self):
        prod_settings = Settings(environment="production", mcp_token="", webhook_secret="")
        with patch("app.routers.integrations.settings", prod_settings):
            with self.assertRaises(HTTPException) as ctx:
                verify_integration_token(
                    authorization=None,
                    secret=None,
                    x_authentik_username=None,
                    x_authentik_email=None,
                )
            self.assertEqual(ctx.exception.status_code, 503)

    def test_verify_integration_token_enforces_bearer_token(self):
        prod_settings = Settings(environment="production", mcp_token="abc123", webhook_secret="")
        with patch("app.routers.integrations.settings", prod_settings):
            self.assertTrue(
                verify_integration_token(
                    authorization="Bearer abc123",
                    secret=None,
                    x_authentik_username=None,
                    x_authentik_email=None,
                )
            )
            with self.assertRaises(HTTPException) as ctx:
                verify_integration_token(
                    authorization="Bearer wrong",
                    secret=None,
                    x_authentik_username=None,
                    x_authentik_email=None,
                )
            self.assertEqual(ctx.exception.status_code, 401)
            with self.assertRaises(HTTPException) as ctx2:
                verify_integration_token(
                    authorization=None,
                    secret=None,
                    x_authentik_username=None,
                    x_authentik_email=None,
                )
            self.assertEqual(ctx2.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
