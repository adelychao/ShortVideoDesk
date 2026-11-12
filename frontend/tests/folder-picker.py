"""Run with the project venv: python frontend/tests/folder-picker.py."""
import json
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(
    ["node", str(root / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
    cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        config = {"archive_root": "F:/old", "obsidian_reference_root": None, "obsidian_output_root": None,
                  "model_provider": "zhipu", "model_id": "glm-5.3-flash", "asr_model_id": "glm-asr-2512",
                  "model_base_url": None, "vector_search_enabled": False}
        response = {"settings": config, "model_base_url_default": None, "model_providers": [],
                    "directory_conflicts": [], "credential": {"configured": False, "available": True}}
        choices = iter(["F:/新归档", None, "error", "F:/参考", "F:/输出"])
        saved = []

        def handle(route):
            request = route.request
            path = request.url.split("/api/v1/", 1)[-1].split("?", 1)[0]
            if path == "folders/select":
                choice = next(choices)
                if choice == "error":
                    route.fulfill(status=503, content_type="application/json", body='{"detail":"选择窗口不可用"}')
                    return
                body = {"path": choice}
            elif path == "config" and request.method == "PUT":
                saved.append(request.post_data_json)
                body = {**response, "settings": request.post_data_json}
            elif path == "config":
                body = response
            elif path in ("diagnostics", "platforms"):
                body = []
            else:
                body = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

        page.route("**/api/v1/**", handle)
        for attempt in range(60):
            try:
                page.goto(f"http://127.0.0.1:{port}/settings")
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(0.2)
        page.get_by_role("button", name="选择媒体归档目录").click()
        expect(page.locator("#archive-root")).to_have_text("F:/新归档")
        page.get_by_role("button", name="选择媒体归档目录").click()
        expect(page.get_by_role("button", name="选择媒体归档目录")).to_be_enabled()
        expect(page.locator("#archive-root")).to_have_text("F:/新归档")
        page.get_by_role("button", name="选择知识库风格参考目录").click()
        expect(page.get_by_role("alert")).to_contain_text("选择窗口不可用")
        expect(page.locator("#obsidian-reference-root")).to_have_text("尚未选择文件夹")
        page.get_by_role("button", name="选择知识库风格参考目录").click()
        expect(page.locator("#obsidian-reference-root")).to_have_text("F:/参考")
        page.get_by_role("button", name="选择知识库输出目录").click()
        expect(page.locator("#obsidian-output-root")).to_have_text("F:/输出")
        assert not saved
        page.get_by_role("button", name="检查并保存设置").click()
        page.get_by_text("本机设置已保存。").wait_for()
        assert saved[0]["archive_root"] == "F:/新归档"
        assert saved[0]["obsidian_reference_root"] == "F:/参考"
        assert saved[0]["obsidian_output_root"] == "F:/输出"
        browser.close()
        print("Folder selection, cancellation and explicit save passed")
finally:
    server.terminate()
    server.wait(timeout=10)
