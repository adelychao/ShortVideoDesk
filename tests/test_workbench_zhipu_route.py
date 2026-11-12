from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from litellm import get_llm_provider

from app.workbench.providers.litellm_adapter import LiteLLMModelProvider


def test_zhipu_text_uses_supported_openai_compatible_route(monkeypatch):
    requests = []

    def fake_completion(**kwargs):
        requests.append(kwargs)
        return {"id": "response-1", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("litellm.completion", fake_completion)
    provider = LiteLLMModelProvider("test-key", provider_id="zhipu")
    result = provider.analyze_text("system", "test", "glm-5.3-flash")

    assert result.text == "ok"
    assert requests[0]["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    model, backend, *_ = get_llm_provider(requests[0]["model"])
    assert model == "glm-5.3-flash"
    assert backend in {"custom_openai", "openai"}


def test_zhipu_route_sends_openai_compatible_request_to_configured_endpoint(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, payload["model"], self.headers.get("Authorization")))
            response = json.dumps({"id": "local-test", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            return

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = LiteLLMModelProvider("test-key", provider_id="zhipu", base_url=f"http://127.0.0.1:{server.server_port}/v4", timeout_seconds=5)
        response = provider.analyze_text("system", "sample", "glm-5.3-flash")
        assert response.text == "ok"
        assert received == [("/v4/chat/completions", "glm-5.3-flash", "Bearer test-key")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

