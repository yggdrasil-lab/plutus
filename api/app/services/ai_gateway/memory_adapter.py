"""Deterministic in-memory AiGateway fake for unit and FastAPI integration tests."""

from __future__ import annotations

from collections import deque
import json
from typing import Any, Callable, Optional, Sequence

from app.services.ai_gateway._types import (
    AiCircuitOpenError,
    AiGatewayError,
    AiInvalidResponseError,
    AiJsonResponse,
    AiTextRequest,
    AiVisionRequest,
    JevGateRequest,
    JevGateResponse,
)

TextOutcome = AiJsonResponse | dict[str, Any] | AiGatewayError
VisionOutcome = AiJsonResponse | dict[str, Any] | AiGatewayError
JevOutcome = JevGateResponse | str | AiGatewayError


class InMemoryAiGateway:
    """In-memory test double recording requests and serving deterministic outcomes."""

    def __init__(
        self,
        *,
        text_responses: Sequence[TextOutcome] = (),
        vision_responses: Sequence[VisionOutcome] = (),
        jev_responses: Sequence[JevOutcome] = (),
        text_handler: Optional[Callable[[AiTextRequest], TextOutcome]] = None,
        vision_handler: Optional[Callable[[AiVisionRequest], VisionOutcome]] = None,
        jev_handler: Optional[Callable[[JevGateRequest], JevOutcome]] = None,
        jev_circuit_open: bool = False,
        jev_failure_threshold: Optional[int] = None,
    ) -> None:
        self._text_queue: deque[TextOutcome] = deque(text_responses)
        self._vision_queue: deque[VisionOutcome] = deque(vision_responses)
        self._jev_queue: deque[JevOutcome] = deque(jev_responses)
        self._text_handler = text_handler
        self._vision_handler = vision_handler
        self._jev_handler = jev_handler
        self._jev_circuit_open = jev_circuit_open
        self._jev_failure_threshold = jev_failure_threshold
        self._consecutive_jev_failures = 0
        self.requests: list[AiTextRequest | AiVisionRequest | JevGateRequest] = []

    @property
    def text_requests(self) -> list[AiTextRequest]:
        """Return recorded structured JSON text requests in call order."""
        return [req for req in self.requests if isinstance(req, AiTextRequest)]

    @property
    def vision_requests(self) -> list[AiVisionRequest]:
        """Return recorded multimodal vision requests in call order."""
        return [req for req in self.requests if isinstance(req, AiVisionRequest)]

    @property
    def jev_requests(self) -> list[JevGateRequest]:
        """Return recorded Jev fast-gate requests in call order."""
        return [req for req in self.requests if isinstance(req, JevGateRequest)]

    def queue_text(self, outcome: TextOutcome) -> None:
        """Append a deterministic outcome to the text completion queue."""
        self._text_queue.append(outcome)

    def queue_vision(self, outcome: VisionOutcome) -> None:
        """Append a deterministic outcome to the vision completion queue."""
        self._vision_queue.append(outcome)

    def queue_jev(self, outcome: JevOutcome) -> None:
        """Append a deterministic outcome to the Jev fast-gate queue."""
        self._jev_queue.append(outcome)

    def open_jev_circuit(self) -> None:
        """Force the simulated Jev circuit breaker open."""
        self._jev_circuit_open = True

    def is_jev_circuit_open(self) -> bool:
        """Return True when the simulated Jev circuit breaker is open."""
        return self._jev_circuit_open

    def reset_jev_circuit(self) -> None:
        """Close the simulated Jev circuit breaker and reset consecutive failures."""
        self._jev_circuit_open = False
        self._consecutive_jev_failures = 0

    def complete_json(self, request: AiTextRequest) -> AiJsonResponse:
        """Serve the next queued or handler-generated JSON response for a text request."""
        self.requests.append(request)
        outcome = _pop_or_invoke(
            self._text_queue,
            self._text_handler,
            request,
            label="text",
        )
        return _coerce_json_response(outcome, request.model or "in-memory-text")

    def complete_vision_json(self, request: AiVisionRequest) -> AiJsonResponse:
        """Serve the next queued or handler-generated JSON response for a vision request."""
        self.requests.append(request)
        outcome = _pop_or_invoke(
            self._vision_queue,
            self._vision_handler,
            request,
            label="vision",
        )
        return _coerce_json_response(outcome, request.model or "in-memory-vision")

    def call_jev_gate(self, request: JevGateRequest) -> JevGateResponse:
        """Serve the next queued or handler-generated Jev verdict under simulated breaker rules."""
        if self._jev_circuit_open:
            raise AiCircuitOpenError("Jev circuit breaker is open (in-memory).")
        self.requests.append(request)
        outcome = _pop_or_invoke(
            self._jev_queue,
            self._jev_handler,
            request,
            label="jev",
        )
        if isinstance(outcome, BaseException):
            self._record_jev_failure()
            raise outcome
        self._consecutive_jev_failures = 0
        if isinstance(outcome, JevGateResponse):
            return outcome
        if isinstance(outcome, str):
            return JevGateResponse(
                content=outcome,
                elapsed_seconds=0.01,
                model=request.model or "in-memory-jev",
            )
        raise AiInvalidResponseError("InMemoryAiGateway jev outcome must be str or JevGateResponse.")

    def _record_jev_failure(self) -> None:
        self._consecutive_jev_failures += 1
        if (
            self._jev_failure_threshold is not None
            and self._consecutive_jev_failures >= self._jev_failure_threshold
        ):
            self._jev_circuit_open = True


def _pop_or_invoke(
    queue: deque[Any],
    handler: Optional[Callable[[Any], Any]],
    request: Any,
    *,
    label: str,
) -> Any:
    if queue:
        return queue.popleft()
    if handler is not None:
        return handler(request)
    raise RuntimeError(f"InMemoryAiGateway has no queued {label} response or handler configured.")


def _coerce_json_response(outcome: Any, default_model: str) -> AiJsonResponse:
    if isinstance(outcome, BaseException):
        raise outcome
    if isinstance(outcome, AiJsonResponse):
        return outcome
    if isinstance(outcome, dict):
        return AiJsonResponse(
            data=outcome,
            raw_text=json.dumps(outcome),
            elapsed_seconds=0.01,
            model=default_model,
        )
    raise AiInvalidResponseError("InMemoryAiGateway JSON outcome must be dict or AiJsonResponse.")
