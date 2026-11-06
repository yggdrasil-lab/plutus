"""Unit tests for Plutus Discord wealth notification service."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add api directory to sys.path so app can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.discord import (
    _get_auth_headers,
    post_wealth_reply,
    post_wealth_update_thread,
)


class TestDiscordService(unittest.TestCase):
    """Test Discord thread creation and reply formatting."""

    def test_auth_headers_with_and_without_token(self):
        headers_no_token = _get_auth_headers(None)
        self.assertEqual(headers_no_token["Content-Type"], "application/json")
        self.assertIn("Yggdrasil-Plutus", headers_no_token["User-Agent"])
        self.assertNotIn("Authorization", headers_no_token)

        headers_with_token = _get_auth_headers("mock_token_123")
        self.assertEqual(headers_with_token["Authorization"], "Bot mock_token_123")

    def test_post_wealth_update_skipped_without_channel_or_webhook(self):
        with patch("app.services.discord.settings") as mock_settings:
            mock_settings.discord_wealth_channel_id = None
            mock_settings.discord_webhook_url = None
            mock_settings.discord_user_id = None
            result = post_wealth_update_thread(
                title="Monthly Digest",
                content="Net worth update",
                channel_id=None,
                webhook_url=None,
            )
            self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_post_wealth_update_via_bot_token(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"id": "thread_999"}).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with patch("app.services.discord.settings") as mock_settings:
            mock_settings.discord_bot_token = "bot_token_abc"
            mock_settings.discord_wealth_channel_id = "channel_123"
            mock_settings.discord_webhook_url = None
            mock_settings.discord_user_id = "user_456"

            thread_id = post_wealth_update_thread(
                title="Monthly Net Worth",
                content="Runway is 52 weeks.",
            )
            self.assertEqual(thread_id, "thread_999")
            self.assertTrue(mock_urlopen.called)

    @patch("urllib.request.urlopen")
    def test_post_wealth_reply_via_webhook_fallback(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with patch("app.services.discord.settings") as mock_settings:
            mock_settings.discord_bot_token = None
            mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/mock"

            success = post_wealth_reply(
                thread_id="thread_999",
                content="Rebalance executed successfully.",
            )
            self.assertTrue(success)
            self.assertTrue(mock_urlopen.called)


if __name__ == "__main__":
    unittest.main()
