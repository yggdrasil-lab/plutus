"""Contract tests for the unified AI Gateway port, MuninnHttpAdapter, and InMemoryAiGateway."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.services.ai_gateway import (
    AiCircuitOpenError,
    AiConfigError,
    AiGateway,
    AiInvalidResponseError,
    AiJsonResponse,
    AiTextRequest,
    AiUpstreamError,
    AiVisionRequest,
    CircuitBreaker,
    InMemoryAiGateway,
    JevGateRequest,
    JevGateResponse,
    MuninnHttpAdapter,
    get_ai_gateway,
)


class FakeClock:
    """Deterministic monotonic clock for circuit breaker and latency tests."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold_and_recovers_after_cooldown(self) -> None:
        clock = FakeClock(0.0)
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)

        self.assertFalse(breaker.is_open())
        breaker.record_failure()
        breaker.record_failure()
        self.assertFalse(breaker.is_open())

        breaker.record_failure()
        self.assertTrue(breaker.is_open())

        clock.advance(59.9)
        self.assertTrue(breaker.is_open())

        clock.advance(0.1)
        self.assertFalse(breaker.is_open())

        # Failing probe immediately re-opens cooldown
        breaker.record_failure()
        self.assertTrue(breaker.is_open())

        breaker.record_success()
        self.assertFalse(breaker.is_open())

    def test_validates_thresholds(self) -> None:
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0)
        with self.assertRaises(ValueError):
            CircuitBreaker(cooldown_seconds=-1.0)


class TestMuninnHttpAdapter(unittest.TestCase):
    def _build_adapter(
        self,
        handler,
        *,
        clock: FakeClock | None = None,
        api_key: str = "muninn-secret",
        base_url: str = "http://muninn:4000/v1",
        text_model: str = "app-synthesize",
        vision_model: str = "gemini-4-hydrogen",
        jev_model: str = "jev-decision",
    ) -> MuninnHttpAdapter:
        return MuninnHttpAdapter(
            base_url=base_url,
            api_key=api_key,
            default_text_model=text_model,
            default_vision_model=vision_model,
            default_jev_model=jev_model,
            transport=httpx.MockTransport(handler),
            clock=clock or FakeClock(),
        )

    def test_complete_json_strips_markdown_fences_and_sends_bearer_auth(self) -> None:
        clock = FakeClock(10.0)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            clock.advance(0.25)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '```json\n{"headline": "Strong Runway", "weeks": 120}\n```'
                            }
                        }
                    ]
                },
            )

        adapter = self._build_adapter(handler, clock=clock)
        self.assertIsInstance(adapter, AiGateway)

        res = adapter.complete_json(
            AiTextRequest(
                system_prompt="Return JSON.",
                user_prompt="Analyze runway.",
                temperature=0.2,
                timeout=12.0,
                user_agent="Yggdrasil-Plutus/1.0 (Test)",
            )
        )

        self.assertEqual(res.data, {"headline": "Strong Runway", "weeks": 120})
        self.assertEqual(res.model, "app-synthesize")
        self.assertAlmostEqual(res.elapsed_seconds, 0.25)
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(str(req.url), "http://muninn:4000/v1/chat/completions")
        self.assertEqual(req.headers["authorization"], "Bearer muninn-secret")
        self.assertEqual(req.headers["user-agent"], "Yggdrasil-Plutus/1.0 (Test)")
        body = json.loads(req.content.decode("utf-8"))
        self.assertEqual(body["model"], "app-synthesize")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Return JSON."})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "Analyze runway."})

    def test_complete_vision_json_builds_multimodal_payload(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"merchant": "Costco", "total": 42.5}'}}]},
            )

        adapter = self._build_adapter(handler)
        res = adapter.complete_vision_json(
            AiVisionRequest(
                user_prompt="Parse receipt",
                image_b64="ZmFrZV9pbWFnZQ==",
                image_mime_type="image/png",
            )
        )

        self.assertEqual(res.data, {"merchant": "Costco", "total": 42.5})
        self.assertEqual(res.model, "gemini-4-hydrogen")
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertEqual(payload["model"], "gemini-4-hydrogen")
        self.assertNotIn("response_format", payload)
        user_msg = payload["messages"][0]
        self.assertEqual(user_msg["role"], "user")
        self.assertEqual(user_msg["content"][0], {"type": "text", "text": "Parse receipt"})
        self.assertEqual(
            user_msg["content"][1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZV9pbWFnZQ=="}},
        )

    def test_missing_config_raises_ai_config_error_without_http_calls(self) -> None:
        called = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        no_key = self._build_adapter(handler, api_key="")
        with self.assertRaises(AiConfigError):
            no_key.complete_json(AiTextRequest(user_prompt="Hi"))
        with self.assertRaises(AiConfigError):
            no_key.call_jev_gate(JevGateRequest(system_prompt="Sys", user_content="User"))

        no_url = self._build_adapter(handler, base_url="")
        with self.assertRaises(AiConfigError):
            no_url.complete_json(AiTextRequest(user_prompt="Hi"))

        no_model = self._build_adapter(handler, jev_model="")
        with self.assertRaises(AiConfigError):
            no_model.call_jev_gate(JevGateRequest(system_prompt="Sys", user_content="User"))

        self.assertFalse(called)

    def test_upstream_and_invalid_json_errors(self) -> None:
        def http_503(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        adapter_503 = self._build_adapter(http_503)
        with self.assertRaises(AiUpstreamError) as ctx:
            adapter_503.complete_json(AiTextRequest(user_prompt="Hello"))
        self.assertEqual(ctx.exception.status_code, 503)

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        adapter_timeout = self._build_adapter(timeout_handler)
        with self.assertRaises(AiUpstreamError):
            adapter_timeout.complete_json(AiTextRequest(user_prompt="Hello"))

        def non_object_json(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "[1, 2, 3]"}}]})

        adapter_array = self._build_adapter(non_object_json)
        with self.assertRaises(AiInvalidResponseError):
            adapter_array.complete_json(AiTextRequest(user_prompt="Hello"))

        def malformed_json(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

        adapter_bad = self._build_adapter(malformed_json)
        with self.assertRaises(AiInvalidResponseError):
            adapter_bad.complete_json(AiTextRequest(user_prompt="Hello"))

        def empty_choices(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        adapter_empty = self._build_adapter(empty_choices)
        with self.assertRaises(AiUpstreamError):
            adapter_empty.complete_json(AiTextRequest(user_prompt="Hello"))

    def test_jev_circuit_breaker_isolation(self) -> None:
        clock = FakeClock(50.0)
        calls = 0
        fail_jev = True
        fail_text = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            body = json.loads(request.content.decode("utf-8"))
            if body["model"] == "jev-decision":
                if fail_jev:
                    return httpx.Response(502, text="bad gateway")
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": '{"category": "Groceries"}'}}]},
                )
            if fail_text:
                return httpx.Response(500, text="text upstream error")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok": true}'}}]},
            )

        adapter = self._build_adapter(handler, clock=clock)

        # 3 consecutive text failures on adapter itself must NOT trip the Jev breaker
        fail_text = True
        for _ in range(3):
            with self.assertRaises(AiUpstreamError):
                adapter.complete_json(AiTextRequest(user_prompt="x"))
        self.assertFalse(adapter.is_jev_circuit_open())
        self.assertEqual(calls, 3)
        fail_text = False

        # 3 consecutive Jev failures open the circuit
        for _ in range(3):
            with self.assertRaises(AiUpstreamError):
                adapter.call_jev_gate(JevGateRequest(system_prompt="sys", user_content="user"))
        self.assertTrue(adapter.is_jev_circuit_open())
        self.assertEqual(calls, 6)

        # 4th Jev call fails fast with AiCircuitOpenError and zero HTTP traffic
        with self.assertRaises(AiCircuitOpenError):
            adapter.call_jev_gate(JevGateRequest(system_prompt="sys", user_content="user"))
        self.assertEqual(calls, 6)

        # Text and vision completions continue working while Jev breaker is open
        text_res = adapter.complete_json(AiTextRequest(user_prompt="runway"))
        vision_res = adapter.complete_vision_json(
            AiVisionRequest(user_prompt="receipt", image_b64="YQ==", image_mime_type="image/png")
        )
        self.assertEqual(text_res.data, {"ok": True})
        self.assertEqual(vision_res.data, {"ok": True})
        self.assertEqual(calls, 8)
        self.assertTrue(adapter.is_jev_circuit_open())

        # Advance clock past 60s cooldown -> probe succeeds and resets circuit
        fail_jev = False
        clock.advance(61.0)
        self.assertFalse(adapter.is_jev_circuit_open())
        jev_res = adapter.call_jev_gate(JevGateRequest(system_prompt="sys", user_content="user"))
        self.assertEqual(jev_res.content, '{"category": "Groceries"}')
        self.assertFalse(adapter.is_jev_circuit_open())

    def test_from_settings_reads_runtime_settings(self) -> None:
        custom = Settings(
            llm_base_url="http://muninn-custom:4000/v1",
            llm_api_key="live-key",
            llm_model="custom-text",
            receipt_llm_model="custom-vision",
            jev_model="custom-jev",
        )
        seen_models: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_models.append(json.loads(request.content.decode("utf-8"))["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

        adapter = MuninnHttpAdapter.from_settings(
            custom,
            transport=httpx.MockTransport(handler),
        )
        res = adapter.call_jev_gate(JevGateRequest(system_prompt="s", user_content="u"))
        self.assertEqual(res.model, "custom-jev")
        self.assertEqual(seen_models, ["custom-jev"])


class TestInMemoryAiGatewayAndFastApiDependency(unittest.TestCase):
    def test_in_memory_gateway_records_requests_and_simulates_breaker(self) -> None:
        gw = InMemoryAiGateway(
            text_responses=[{"headline": "OK"}],
            vision_responses=[AiJsonResponse(data={"items": []}, raw_text='{"items": []}', elapsed_seconds=0.1, model="v")],
            jev_responses=['{"category": "Dining"}', AiUpstreamError("down", status_code=503)],
            jev_failure_threshold=1,
        )
        self.assertIsInstance(gw, AiGateway)

        t_res = gw.complete_json(AiTextRequest(user_prompt="text"))
        v_res = gw.complete_vision_json(AiVisionRequest(user_prompt="vision", image_b64="YQ=="))
        j_res = gw.call_jev_gate(JevGateRequest(system_prompt="s", user_content="u"))
        self.assertEqual(t_res.data, {"headline": "OK"})
        self.assertEqual(v_res.data, {"items": []})
        self.assertEqual(j_res.content, '{"category": "Dining"}')

        with self.assertRaises(AiUpstreamError):
            gw.call_jev_gate(JevGateRequest(system_prompt="s", user_content="u2"))
        self.assertTrue(gw.is_jev_circuit_open())
        with self.assertRaises(AiCircuitOpenError):
            gw.call_jev_gate(JevGateRequest(system_prompt="s", user_content="u3"))

        gw.reset_jev_circuit()
        self.assertFalse(gw.is_jev_circuit_open())
        self.assertEqual(len(gw.text_requests), 1)
        self.assertEqual(len(gw.vision_requests), 1)
        self.assertEqual(len(gw.jev_requests), 2)

    def test_get_ai_gateway_uses_app_state_or_lazy_default(self) -> None:
        from fastapi import Depends

        mini_app = FastAPI()

        @mini_app.get("/gw")
        def inspect_gw(gw: AiGateway = Depends(get_ai_gateway)) -> dict[str, str]:
            return {"type": type(gw).__name__}

        client = TestClient(mini_app)
        res_default = client.get("/gw")
        self.assertEqual(res_default.status_code, 200)
        self.assertEqual(res_default.json()["type"], "MuninnHttpAdapter")

        mini_app.state.ai_gateway = InMemoryAiGateway()
        res_state = client.get("/gw")
        self.assertEqual(res_state.status_code, 200)
        self.assertEqual(res_state.json()["type"], "InMemoryAiGateway")


if __name__ == "__main__":
    unittest.main()
