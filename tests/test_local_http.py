from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
import threading
import urllib.request

import pytest
from fastapi.testclient import TestClient

import launcher
import run
import stop


def test_open_local_url_bypasses_configured_proxy(monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            import json

            body = json.dumps({
                "status": "ok",
                "app_id": launcher.APP_ID,
                "version": launcher.APP_VERSION,
                "build_id": launcher.BUILD_ID,
                "source_build_id": launcher.BUILD_ID,
                "restart_required": False,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:9")
    monkeypatch.setenv("http_proxy", "http://192.0.2.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:9")
    monkeypatch.setenv("all_proxy", "http://192.0.2.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(urllib.request, "_opener", None)

    try:
        health = launcher._fetch_health(server.server_port, timeout=1.0)
        assert health == {
            "status": "ok",
            "app_id": launcher.APP_ID,
            "version": launcher.APP_VERSION,
            "build_id": launcher.BUILD_ID,
            "source_build_id": launcher.BUILD_ID,
            "restart_required": False,
        }
        assert launcher._health_matches_current_build(health)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.mark.parametrize("module_name", ["anonymous_poc", "app.main"])
def test_runtime_health_probes_skip_browser_account_check(monkeypatch, module_name) -> None:
    backend = import_module(module_name)
    browser_checks = []

    def check_browser_status() -> bool:
        browser_checks.append(True)
        return True

    monkeypatch.setattr(backend, "browser_automation_paused", check_browser_status)
    client = TestClient(backend.app, base_url="http://127.0.0.1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            response = client.get(self.path)
            self.send_response(response.status_code)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *_args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert client.get("/api/health").json()["browser_automation_paused"] is True
            for fetch_health in (launcher._fetch_health, run._fetch_health, stop._fetch_health):
                health = fetch_health(server.server_port, timeout=2.0)
                assert launcher._health_matches_current_build(health)
                assert "browser_automation_paused" not in health
            assert browser_checks == [True]
        finally:
            server.shutdown()
            thread.join(timeout=1.0)
            client.close()
