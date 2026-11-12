from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workbench import api as workbench_api
from app.workbench.database import Database
from app.workbench.video_publishing import (
    SocialAutoUploadAdapter,
    add_video_publish_account,
    update_video_publish_account_status,
)


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da63fcff1f0000300301e9bb26ea0000000049454e44ae426082"
)


def _setup_login(tmp_path, monkeypatch):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    sau_project = tmp_path / "social-auto-upload"
    (sau_project / "cookies").mkdir(parents=True)
    (sau_project / "pyproject.toml").write_text("[project]\nname='sau'\n", encoding="utf-8")
    SocialAutoUploadAdapter(database).set_project_path(str(sau_project))
    account = add_video_publish_account(database, "douyin", "main_account", "主账号")
    update_video_publish_account_status(database, account["id"], "login_in_progress", "等待登录")

    app = FastAPI()
    app.include_router(workbench_api._router)
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database))
    return database, sau_project, account, TestClient(app, base_url="http://testserver")


def test_login_qr_endpoint_returns_newest_png_without_caching(tmp_path, monkeypatch):
    _database, sau_project, account, client = _setup_login(tmp_path, monkeypatch)
    try:
        cookies = sau_project / "cookies"
        older = cookies / "douyin_main_account_login_qrcode_20261102_080000.png"
        latest = cookies / "douyin_main_account_login_qrcode_20261102_080100.png"
        older.write_bytes(PNG_1X1)
        latest.write_bytes(PNG_1X1 + b"latest")
        os.utime(latest, ns=(older.stat().st_atime_ns + 1_000_000, older.stat().st_mtime_ns + 1_000_000))

        response = client.get(
            f"/api/v1/video-publish/accounts/{account['id']}/login-qr",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert response.content == PNG_1X1 + b"latest"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_login_qr_endpoint_requires_same_origin_and_active_login(tmp_path, monkeypatch):
    database, sau_project, account, client = _setup_login(tmp_path, monkeypatch)
    qr_path = sau_project / "cookies" / "douyin_main_account_login_qrcode_20261102_080100.png"
    qr_path.write_bytes(PNG_1X1)
    url = f"/api/v1/video-publish/accounts/{account['id']}/login-qr"
    try:
        assert client.get(url, headers={"Origin": "https://attacker.example"}).status_code == 403
        assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403

        update_video_publish_account_status(database, account["id"], "not_connected", "登录结束")
        response = client.get(
            url,
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
    finally:
        client.close()

    assert response.status_code == 404


def test_video_publish_create_view_displays_and_refreshes_login_qr():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "views" / "VideoPublishCreateView.vue").read_text(encoding="utf-8")

    assert "login-qr" in source
    assert "账号登录二维码" in source
    assert "正在刷新二维码" in source
    assert "qrRefreshKey" in source
