"""Immutable request/response contracts and typed exceptions for the AI Gateway port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_AI_USER_AGENT = "Yggdrasil-Plutus/1.0"
DEFAULT_JEV_USER_AGENT = "Yggdrasil-Plutus/1.0 (PlutusApi)"


class AiGatewayError(RuntimeError):
    """Base exception raised across the owned Muninn/Jev AI Gateway seam."""


class AiConfigError(AiGatewayError):
    """Raised when required AI gateway base URL, API key, or model config is missing."""


class AiUpstreamError(AiGatewayError):
    """Raised when the upstream AI gateway fails, times out, or returns an error response."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AiInvalidResponseError(AiGatewayError):
    """Raised when an AI gateway response cannot be parsed as the expected JSON contract."""


class AiCircuitOpenError(AiGatewayError):
    """Raised when a Jev fast-gate request is short-circuited by an open circuit breaker."""


@dataclass(frozen=True)
class AiTextRequest:
    """Immutable request contract for structured JSON text completion."""

    system_prompt: Optional[str] = None
    user_prompt: str = ""
    model: Optional[str] = None
    temperature: float = 0.0
    timeout: float = 60.0
    response_format_json: bool = True
    user_agent: str = DEFAULT_AI_USER_AGENT


@dataclass(frozen=True)
class AiVisionRequest:
    """Immutable request contract for multimodal vision JSON completion."""

    system_prompt: Optional[str] = None
    user_prompt: str = ""
    image_b64: str = ""
    image_mime_type: str = "image/jpeg"
    model: Optional[str] = None
    temperature: float = 0.1
    timeout: float = 105.0
    response_format_json: bool = False
    user_agent: str = DEFAULT_AI_USER_AGENT


@dataclass(frozen=True)
class JevGateRequest:
    """Immutable request contract for TypeSafe Jev System One gating."""

    system_prompt: str
    user_content: str
    model: Optional[str] = None
    timeout: float = 4.0
    response_format: Optional[Mapping[str, Any]] = None
    user_agent: str = DEFAULT_JEV_USER_AGENT


@dataclass(frozen=True)
class AiJsonResponse:
    """Immutable structured JSON response returned by an AI gateway."""

    data: dict[str, Any]
    raw_text: str
    elapsed_seconds: float
    model: str


@dataclass(frozen=True)
class JevGateResponse:
    """Immutable verdict content returned by TypeSafe Jev System One."""

    content: str
    elapsed_seconds: float
    model: str
