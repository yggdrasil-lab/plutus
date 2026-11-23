"""Owned Muninn and Jev AI Gateway package and FastAPI dependency provider."""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import Request

from app.services.ai_gateway._types import (
    DEFAULT_AI_USER_AGENT,
    DEFAULT_JEV_USER_AGENT,
    AiCircuitOpenError,
    AiConfigError,
    AiGatewayError,
    AiInvalidResponseError,
    AiJsonResponse,
    AiTextRequest,
    AiUpstreamError,
    AiVisionRequest,
    JevGateRequest,
    JevGateResponse,
)
from app.services.ai_gateway.circuit_breaker import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    CircuitBreaker,
)
from app.services.ai_gateway.memory_adapter import InMemoryAiGateway
from app.services.ai_gateway.muninn_adapter import MuninnHttpAdapter
from app.services.ai_gateway.port import AiGateway

_DEFAULT_GATEWAY_LOCK = threading.Lock()
_DEFAULT_AI_GATEWAY: Optional[MuninnHttpAdapter] = None


def get_default_ai_gateway() -> MuninnHttpAdapter:
    """Return the process-wide lazy MuninnHttpAdapter fallback for background callers."""
    global _DEFAULT_AI_GATEWAY
    if _DEFAULT_AI_GATEWAY is None:
        with _DEFAULT_GATEWAY_LOCK:
            if _DEFAULT_AI_GATEWAY is None:
                _DEFAULT_AI_GATEWAY = MuninnHttpAdapter.from_settings()
    return _DEFAULT_AI_GATEWAY


def get_ai_gateway(request: Request) -> AiGateway:
    """FastAPI dependency returning app.state.ai_gateway or the lazy default adapter."""
    gateway = getattr(request.app.state, "ai_gateway", None)
    if gateway is not None:
        return gateway
    return get_default_ai_gateway()


__all__ = [
    "AiCircuitOpenError",
    "AiConfigError",
    "AiGateway",
    "AiGatewayError",
    "AiInvalidResponseError",
    "AiJsonResponse",
    "AiTextRequest",
    "AiUpstreamError",
    "AiVisionRequest",
    "CircuitBreaker",
    "DEFAULT_AI_USER_AGENT",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_JEV_USER_AGENT",
    "InMemoryAiGateway",
    "JevGateRequest",
    "JevGateResponse",
    "MuninnHttpAdapter",
    "get_ai_gateway",
    "get_default_ai_gateway",
]
