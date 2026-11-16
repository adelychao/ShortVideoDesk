"""Run with the project venv: python frontend/tests/knowledge-card-library-picker.py.

验证：配置多个知识库输出目录时，知识卡“审核通过”会先弹窗让用户选择目标知识库，
选择后才携带 library_name 提交审核；取消弹窗不会提交。
"""
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

CARD_SHA = "a" * 64
TASK = {
    "task": {
        "id": "task-1",
        "asset_id": "asset-1",
        "source": {"kind": "url", "value": "公开链接（已隐藏地址）"},
        "status": "completed",
        "created_at": "2026-11-13T10:00:00+00:00",
        "updated_at": "2026-11-13T10:10:00+00:00",
        "steps": [],
    },
    "asset": {
        "id": "asset-1",
        "source_kind": "url",
        "platform": "douyin",
        "source_id": "7682665543739936010",
        "title": "成功靠运气",
        "description": None,
        "author": "觉醒职场洞察",
        "published_at": "2026-09-07T13:46:07+00:00",
        "archived_at": "2026-11-13T11:48:16+00:00",
        "created_at": "2026-11-13T11:48:16+00:00",
        "sha256": "0" * 64,
        "archive_relative_path": "2026-11-13/0001_成功靠运气/video.mp4",
    },
    "transcription_config": {
        "mode": "cloud",
        "profile_id": None,
        "profile_name": None,
        "provider_id": "zhipu",
        "provider_name": "智谱",
        "asr_model_id": "glm-asr-2512",
        "local_model": None,
        "local_language": None,
        "local_device": None,
    },
    "artifacts": [
        {
            "id": "card-1",
            "task_id": "task-1",
            "step_id": "step-1",
            "kind": "knowledge_card",
            "name": "知识卡.md",
            "relative_path": "2026-11-13/0001/card.md",
            "sha256": CARD_SHA,
            "version": 1,
            "metadata": {"title": "成功靠运气：提升运气的三个方法"},
            "created_at": "2026-11-13T11:50:00+00:00",
            "mime_type": "text/markdown",
            "file_url": "/api/v1/artifacts/card-1/file",
            "file_available": True,
        }
    ],
    "consents": [],
    "model_runs": [],
}
CONFIG = {
    "settings": {
        "archive_root": "F:/archive",
        "obsidian_reference_root": None,
        "obsidian_output_root": "F:/knowledge/a",
        "obsidian_output_directories": [
            {"name": "职场干货", "directory": "F:/knowledge/a"},
            {"name": "AI 工具", "directory": "F:/knowledge/b"},
        ],
    },
    "model_base_url_default": None,
    "model_providers": [],
    "directory_conflicts": [],
    "credential": {"configured": False, "available": True},
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        review_calls = []

        def handle(route):
            request = route.request
            path = request.url.split("/api/v1/", 1)[-1].split("?", 1)[0]
            if path == "tasks/task-1":
                body = TASK
            elif path == "artifacts/card-1/text":
                body = {"artifact_id": "card-1", "sha256": CARD_SHA, "content": "# 成功靠运气\n\n正文\n"}
            elif path == "config":
                body = CONFIG
            elif path == "artifacts/card-1/review":
                review_calls.append(request.post_data_json)
                body = {
                    **TASK["artifacts"][0],
                    "kind": "knowledge_card_approved",
                    "metadata": {"title": "成功靠运气：提升运气的三个方法", "vault_library_name": "AI 工具", "vault_note_path": "成功靠运气.md"},
                    "vault_write": {"written": True, "path": "成功靠运气.md", "reason": None, "library_name": "AI 工具"},
                }
            elif path in ("diagnostics", "platforms"):
                body = []
            elif path == "health":
                body = {"status": "ok"}
            else:
                body = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

        page.route("**/api/v1/**", handle)
        for attempt in range(60):
            try:
                page.goto(f"http://127.0.0.1:{port}/task/task-1")
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(0.2)

        page.get_by_role("tab", name="知识卡").click()
        expect(page.get_by_role("heading", name="成功靠运气：提升运气的三个方法")).to_be_visible()

        # 多个知识库输出目录：先弹窗，取消时不提交审核。
        page.get_by_role("button", name="审核通过").click()
        dialog = page.locator(".card-library-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_text("职场干货")).to_be_visible()
        expect(dialog.get_by_text("F:/knowledge/a")).to_be_visible()
        dialog.get_by_role("button", name="取消").click()
        expect(dialog).not_to_be_visible()
        assert review_calls == []

        # 重新打开，选择“AI 工具”后提交。
        page.get_by_role("button", name="审核通过").click()
        expect(dialog).to_be_visible()
        dialog.get_by_text("AI 工具").click()
        dialog.get_by_role("button", name="审核并写入").click()
        expect(page.get_by_text("已审核并写入「AI 工具」：成功靠运气.md")).to_be_visible()
        assert review_calls == [{"expected_sha256": CARD_SHA, "library_name": "AI 工具"}]

        browser.close()
        print("Knowledge card approval library picker passed")
finally:
    server.terminate()
    server.wait(timeout=10)
