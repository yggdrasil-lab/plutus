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
    user_id: str | None = None,
) -> str | None:
    """Publishes a wealth update or ledger digest thread to the Discord #wealth forum channel."""
    resolved_channel = channel_id or settings.discord_wealth_channel_id
    resolved_user = user_id or settings.discord_user_id

    if not resolved_channel or not settings.discord_bot_token:
        logger.info("Discord integration not configured; skipping wealth thread creation")
        return None

    mention = f"<@{resolved_user}>\n\n" if resolved_user else ""
    full_content = f"{mention}{content}"

    return _create_forum_thread(
        title=title,
        content=full_content,
        channel_id=resolved_channel,
    )


def post_wealth_reply(
    thread_id: str,
    content: str,
) -> bool:
    """Posts an update reply into an existing Discord #wealth thread."""
    if not thread_id:
        return False

    bot_token = settings.discord_bot_token
    if not bot_token:
        logger.warning("DISCORD_BOT_TOKEN not configured; cannot post reply to thread %s", thread_id)
        return False

    payload = json.dumps({"content": content[:4000]}).encode("utf-8")
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = _get_auth_headers(bot_token)
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS):
            logger.info("Successfully posted reply to thread %s via bot token", thread_id)
            return True
    except Exception as exc:
        logger.warning("Failed posting reply to thread %s via bot token: %s", thread_id, exc)
        return False


def _create_forum_thread(
    title: str,
    content: str,
    channel_id: str,
) -> str | None:
    bot_token = settings.discord_bot_token
    if not bot_token:
        return None

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
        return None
