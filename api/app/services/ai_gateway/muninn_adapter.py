"""Production httpx adapter for owned Muninn and Jev System One endpoints."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
import time
from typing import Any, Callable, Optional

import httpx

from app.config import settings as global_settings
from app.services.ai_gateway._types import (
    AiCircuitOpenError,
    AiConfigError,
    AiInvalidResponseError,
    AiJsonResponse,
    AiTextRequest,
    AiUpstreamError,
    AiVisionRequest,
    JevGateRequest,
    JevGateResponse,
)
from app.services.ai_gateway.circuit_breaker import CircuitBreaker

_MARKDOWN_FENCE_PREFIX_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*")
_MARKDOWN_FENCE_SUFFIX_RE = re.compile(r"\s*```$")


class MuninnHttpAdapter:
    """httpx-backed AiGateway adapter for Muninn LLM and Jev System One."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        default_text_model: str = "",
        default_vision_model: str = "",
        default_jev_model: str = "",
        transport: Optional[httpx.BaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
        jev_breaker: Optional[CircuitBreaker] = None,
        settings_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_text_model = default_text_model
        self._default_vision_model = default_vision_model
        self._default_jev_model = default_jev_model
        self._clock = clock
        self._jev_breaker = jev_breaker or CircuitBreaker(clock=clock)
        self._settings_provider = settings_provider
        self._client = httpx.Client(transport=transport)

    @classmethod
    def from_settings(
        cls,
        settings_obj: Any = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
        jev_breaker: Optional[CircuitBreaker] = None,
    ) -> "MuninnHttpAdapter":
        """Construct an adapter reading configuration lazily from Plutus Settings."""
        provider = (lambda: settings_obj) if settings_obj is not None else _get_global_settings
        return cls(
            transport=transport,
            clock=clock,
            jev_breaker=jev_breaker,
            settings_provider=provider,
        )

    def close(self) -> None:
        """Close the underlying httpx client transport pool."""
        self._client.close()

    def is_jev_circuit_open(self) -> bool:
        """Return True when the Jev fast-gate circuit breaker is currently open."""
        return self._jev_breaker.is_open()

    def reset_jev_circuit(self) -> None:
        """Reset the Jev fast-gate circuit breaker."""
        self._jev_breaker.reset()

    def complete_json(self, request: AiTextRequest) -> AiJsonResponse:
        """Execute a structured JSON text completion without touching the Jev breaker."""
        endpoint, api_key, model = self._resolve_runtime_config(
            request.model,
            "resolved_llm_model",
            self._default_text_model,
        )
        payload = _build_text_payload(request, model)
        content, elapsed = self._post_chat_completions(
            endpoint=endpoint,
            api_key=api_key,
            payload=payload,
            timeout=request.timeout,
            user_agent=request.user_agent,
        )
        parsed = _parse_json_object(content)
        return AiJsonResponse(data=parsed, raw_text=content, elapsed_seconds=elapsed, model=model)

    def complete_vision_json(self, request: AiVisionRequest) -> AiJsonResponse:
        """Execute a multimodal vision completion without touching the Jev breaker."""
        endpoint, api_key, model = self._resolve_runtime_config(
            request.model,
            "resolved_receipt_llm_model",
            self._default_vision_model,
        )
        payload = _build_vision_payload(request, model)
        content, elapsed = self._post_chat_completions(
            endpoint=endpoint,
            api_key=api_key,
            payload=payload,
            timeout=request.timeout,
            user_agent=request.user_agent,
        )
        parsed = _parse_json_object(content)
        return AiJsonResponse(data=parsed, raw_text=content, elapsed_seconds=elapsed, model=model)

    def call_jev_gate(self, request: JevGateRequest) -> JevGateResponse:
        """Invoke TypeSafe Jev System One under the Jev-only circuit breaker."""
        if self._jev_breaker.is_open():
            raise AiCircuitOpenError("Jev circuit breaker is open.")
        endpoint, api_key, model = self._resolve_runtime_config(
            request.model,
            "resolved_jev_model",
            self._default_jev_model,
        )
        payload = _build_jev_payload(request, model)
        try:
            content, elapsed = self._post_chat_completions(
                endpoint=endpoint,
                api_key=api_key,
                payload=payload,
                timeout=request.timeout,
                user_agent=request.user_agent,
            )
        except (AiUpstreamError, AiInvalidResponseError):
            self._jev_breaker.record_failure()
            raise
        self._jev_breaker.record_success()
        return JevGateResponse(content=content, elapsed_seconds=elapsed, model=model)

    def _resolve_runtime_config(
        self,
        requested_model: Optional[str],
        default_attr: str,
        explicit_default: str,
    ) -> tuple[str, str, str]:
        if self._settings_provider is not None:
            cfg = self._settings_provider()
            base_url = (getattr(cfg, "resolved_llm_base_url", "") or "").strip()
            api_key = (getattr(cfg, "resolved_llm_api_key", "") or "").strip()
            default_model = (getattr(cfg, default_attr, "") or "").strip()
        else:
            base_url = self._base_url.strip()
            api_key = self._api_key.strip()
            default_model = explicit_default.strip()

        resolved_model = (requested_model or default_model).strip()
        if not base_url:
            raise AiConfigError("AI gateway base_url is not configured.")
        if not api_key:
            raise AiConfigError("AI gateway api_key is not configured.")
        if not resolved_model:
            raise AiConfigError(f"AI gateway model ({default_attr}) is not configured.")
        return f"{base_url.rstrip('/')}/chat/completions", api_key, resolved_model

    def _post_chat_completions(
        self,
        *,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float,
        user_agent: str,
    ) -> tuple[str, float]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": user_agent,
        }
        t0 = self._clock()
        try:
            response = self._client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise AiUpstreamError(f"AI gateway HTTP request failed: {exc}") from exc
        elapsed = max(0.0, self._clock() - t0)
        if response.status_code != 200:
            raise AiUpstreamError(
                f"AI gateway returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        content = _extract_message_content(response)
        return content, elapsed


def _get_global_settings() -> Any:
    return global_settings


def _to_plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _to_plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_json(item) for item in value]
    return value


def _build_text_payload(request: AiTextRequest, model: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append({"role": "user", "content": request.user_prompt})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
    }
    if request.response_format_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _build_vision_payload(request: AiVisionRequest, model: str) -> dict[str, Any]:
    data_uri = f"data:{request.image_mime_type};base64,{request.image_b64}"
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": request.user_prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
    }
    if request.response_format_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _build_jev_payload(request: JevGateRequest, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_content},
        ],
        "temperature": 0.0,
    }
    if request.response_format is not None:
        payload["response_format"] = _to_plain_json(request.response_format)
    return payload


def _extract_message_content(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError as exc:
        raise AiInvalidResponseError(f"AI gateway returned invalid JSON body: {exc}") from exc
    if not isinstance(body, Mapping):
        raise AiInvalidResponseError("AI gateway response body must be a JSON object.")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiUpstreamError("AI gateway returned empty choices.", status_code=response.status_code)
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    cleaned = str(content or "").strip()
    if not cleaned:
        raise AiUpstreamError(
            "AI gateway returned empty message content.",
            status_code=response.status_code,
        )
    return cleaned


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = _MARKDOWN_FENCE_PREFIX_RE.sub("", cleaned)
        cleaned = _MARKDOWN_FENCE_SUFFIX_RE.sub("", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError as exc:
        raise AiInvalidResponseError(f"AI gateway returned malformed JSON content: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AiInvalidResponseError("AI gateway response content must decode to a JSON object.")
    return parsed
