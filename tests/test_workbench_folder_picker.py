from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
import sys

from app.workbench import api as workbench_api
from app.workbench import folder_picker


def make_client():
    app = FastAPI()
    app.include_router(workbench_api._router)
    return TestClient(app, base_url="http://testserver")


def test_folder_picker_returns_selected_path_without_saving(monkeypatch, tmp_path):
    monkeypatch.setattr(workbench_api, "choose_local_directory", lambda: str(tmp_path), raising=False)
    response = make_client().post(
        "/api/v1/folders/select", json={}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200
    assert response.json() == {"path": str(tmp_path)}


def test_folder_picker_cancel_returns_null(monkeypatch):
    monkeypatch.setattr(workbench_api, "choose_local_directory", lambda: None, raising=False)
    response = make_client().post(
        "/api/v1/folders/select", json={}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200
    assert response.json() == {"path": None}


def test_folder_picker_rejects_missing_or_cross_origin(monkeypatch):
    calls = []
    monkeypatch.setattr(workbench_api, "choose_local_directory", lambda: calls.append(True), raising=False)
    client = make_client()
    assert client.post("/api/v1/folders/select", json={}).status_code == 403
    assert client.post("/api/v1/folders/select", json={}, headers={"Origin": "https://example.com"}).status_code == 403
    assert not calls


def test_folder_picker_failure_is_reported_without_internal_details(monkeypatch):
    def fail():
        raise RuntimeError("internal path or error")

    monkeypatch.setattr(workbench_api, "choose_local_directory", fail, raising=False)
    response = make_client().post(
        "/api/v1/folders/select", json={}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 503
    assert "internal path or error" not in response.text


def test_picker_releases_window_and_lock_after_cancel(monkeypatch):
    events = []
    root = SimpleNamespace(withdraw=lambda: events.append("withdraw"),
                           attributes=lambda *args: events.append("topmost"),
                           destroy=lambda: events.append("destroy"))
    dialog = SimpleNamespace(askdirectory=lambda **kwargs: "")
    monkeypatch.setattr(folder_picker.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=lambda: root, filedialog=dialog))
    assert folder_picker.choose_local_directory() is None
    assert events == ["withdraw", "topmost", "destroy"]
    assert folder_picker._picker_lock.acquire(blocking=False)
    folder_picker._picker_lock.release()


def test_folder_picker_rejects_requests_with_extra_data(monkeypatch):
    monkeypatch.setattr(workbench_api, "choose_local_directory", lambda: None)
    response = make_client().post("/api/v1/folders/select", json={"path": "C:/"},
                                  headers={"Origin": "http://testserver"})
    assert response.status_code == 422


def test_selected_folders_can_be_validated_and_saved_to_workbench_settings(tmp_path):
    from app.workbench.config import AppConfig
    from app.workbench.database import Database

    config = AppConfig(Database(tmp_path / "settings.sqlite3"))
    folders = [tmp_path / name for name in ("archive", "reference", "output")]
    for folder in folders:
        folder.mkdir()
    config.update_settings(dict(zip(
        ("archive_root", "obsidian_reference_root", "obsidian_output_root"),
        map(str, folders),
    )))
    saved = config.get_settings()
    assert [saved[key] for key in ("archive_root", "obsidian_reference_root", "obsidian_output_root")] == list(map(str, folders))
