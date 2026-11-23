"""Protocol defining Plutus's unified AI Gateway port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.ai_gateway._types import (
    AiJsonResponse,
    AiTextRequest,
    AiVisionRequest,
    JevGateRequest,
    JevGateResponse,
)


@runtime_checkable
class AiGateway(Protocol):
    """Port unifying owned Muninn JSON/vision completions and TypeSafe Jev gating."""

    def complete_json(self, request: AiTextRequest) -> AiJsonResponse:
        """Execute a text completion and return a parsed JSON object response."""

    def complete_vision_json(self, request: AiVisionRequest) -> AiJsonResponse:
        """Execute a multimodal vision completion and return a parsed JSON object response."""

    def call_jev_gate(self, request: JevGateRequest) -> JevGateResponse:
        """Invoke TypeSafe Jev System One under the Jev-only circuit breaker."""

    def is_jev_circuit_open(self) -> bool:
        """Return True when the Jev fast-gate circuit breaker is currently open."""

    def reset_jev_circuit(self) -> None:
        """Reset the Jev fast-gate circuit breaker state."""
