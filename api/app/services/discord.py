"""Discord notification service for Plutus wealth tracking, ledger rollups, and thread lifecycle."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = "Yggdrasil-Plutus/1.0 (DiscordBot)"
HTTP_TIMEOUT_SECONDS = 4.0


def _get_auth_headers(bot_token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    token = bot_token or settings.discord_bot_token
    if token:
        headers["Authorization"] = f"Bot {token}"
    return headers


def post_wealth_update_thread(
    title: str,
    content: str,
    channel_id: str | None = None,
    webhook_url: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Publishes a wealth update or ledger digest thread to the Discord #wealth forum channel."""
    resolved_channel = channel_id or settings.discord_wealth_channel_id
    resolved_webhook = webhook_url or settings.discord_webhook_url
    resolved_user = user_id or settings.discord_user_id

    if not resolved_channel and not resolved_webhook:
        logger.info("Discord integration not configured; skipping wealth thread creation")
        return None

    mention = f"<@{resolved_user}>\n\n" if resolved_user else ""
    full_content = f"{mention}{content}"

    return _create_forum_thread(
        title=title,
        content=full_content,
        channel_id=resolved_channel,
        webhook_url=resolved_webhook,
    )


def post_wealth_reply(
    thread_id: str,
    content: str,
    webhook_url: str | None = None,
) -> bool:
    """Posts an update reply into an existing Discord #wealth thread."""
    if not thread_id:
        return False

    resolved_webhook = webhook_url or settings.discord_webhook_url
    bot_token = settings.discord_bot_token

    payload = json.dumps({"content": content[:4000]}).encode("utf-8")

    # Prefer bot token if present
    if bot_token:
        url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
        headers = _get_auth_headers(bot_token)
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS):
                logger.info("Successfully posted reply to thread %s via bot token", thread_id)
                return True
        except Exception as exc:
            logger.warning("Failed posting reply to thread %s via bot token: %s", thread_id, exc)

    # Fallback to webhook if available
    if resolved_webhook:
        url = f"{resolved_webhook}?thread_id={thread_id}&wait=true"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS):
                logger.info("Successfully posted reply to thread %s via webhook", thread_id)
                return True
        except Exception as exc:
            logger.warning("Failed posting reply to thread %s via webhook: %s", thread_id, exc)

    return False


def _create_forum_thread(
    title: str,
    content: str,
    channel_id: str | None,
    webhook_url: str | None,
) -> str | None:
    bot_token = settings.discord_bot_token

    # 1. Bot token channel thread creation (Discord Forum Channel type 15)
    if bot_token and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/threads"
        headers = _get_auth_headers(bot_token)
        payload = json.dumps({
            "name": title[:100],
            "message": {"content": content[:4000]},
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                thread_id = str(data.get("id"))
                logger.info("Successfully created Discord forum thread %s ('%s')", thread_id, title)
                return thread_id
        except Exception as exc:
            logger.warning("Discord API bot token thread creation failed: %s", exc)

    # 2. Webhook forum thread creation fallback
    if webhook_url:
        url = f"{webhook_url}?wait=true"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        payload = json.dumps({
            "thread_name": title[:100],
            "content": content[:4000],
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                thread_id = str(data.get("channel_id") or data.get("id"))
                logger.info("Successfully created Discord thread %s via webhook ('%s')", thread_id, title)
                return thread_id
        except Exception as exc:
            logger.warning("Discord webhook thread creation failed: %s", exc)

    return None
