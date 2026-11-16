"""知识库输出目录（多目录 + 审核选择）的后端行为测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workbench import api
from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench.runner import TaskRunner


def _make_config(tmp_path, name="settings.sqlite3"):
    return AppConfig(Database(tmp_path / name))


def test_output_directories_roundtrip_and_derived_primary(tmp_path):
    config = _make_config(tmp_path)
    first = tmp_path / "lib-a"
    second = tmp_path / "lib-b"
    first.mkdir()
    second.mkdir()
    config.update_settings({"obsidian_output_directories": [
        {"name": "甲", "directory": str(first)},
        {"name": "乙", "directory": str(second)},
    ]})
    saved = config.get_settings()
    assert [entry["name"] for entry in saved["obsidian_output_directories"]] == ["甲", "乙"]
    assert saved["obsidian_output_root"] == str(first)


def test_duplicate_names_or_directories_are_rejected(tmp_path):
    config = _make_config(tmp_path)
    first = tmp_path / "lib-a"
    first.mkdir()
    with pytest.raises(ValueError):
        config.update_settings({"obsidian_output_directories": [
            {"name": "甲", "directory": str(first)},
            {"name": "甲", "directory": str(tmp_path / "other")},
        ]})
    with pytest.raises(ValueError):
        config.update_settings({"obsidian_output_directories": [
            {"name": "甲", "directory": str(first)},
            {"name": "乙", "directory": str(first)},
        ]})


def test_legacy_single_root_write_maps_to_single_library(tmp_path):
    config = _make_config(tmp_path)
    folder = tmp_path / "legacy"
    folder.mkdir()
    config.update_settings({"obsidian_output_root": str(folder)})
    saved = config.get_settings()
    assert saved["obsidian_output_directories"] == [{"name": "默认知识库", "directory": str(folder)}]
    assert saved["obsidian_output_root"] == str(folder)


def test_legacy_stored_root_seeds_library_list_on_read(tmp_path):
    database = Database(tmp_path / "legacy.sqlite3")
    database.migrate()
    folder = tmp_path / "old-root"
    folder.mkdir()
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES ('obsidian_output_root', ?, '2026-01-01')",
            (json.dumps(str(folder)),),
        )
    settings = AppConfig(database).get_settings()
    assert settings["obsidian_output_directories"] == [{"name": "默认知识库", "directory": str(folder)}]
    assert settings["obsidian_output_root"] == str(folder)


def test_saved_empty_list_is_not_resurrected_from_legacy_root(tmp_path):
    database = Database(tmp_path / "empty.sqlite3")
    database.migrate()
    folder = tmp_path / "old-root"
    folder.mkdir()
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES ('obsidian_output_root', ?, '2026-01-01')",
            (json.dumps(str(folder)),),
        )
    AppConfig(database).update_settings({"obsidian_output_directories": []})
    settings = AppConfig(database).get_settings()
    assert settings["obsidian_output_directories"] == []
    assert settings["obsidian_output_root"] is None


def test_conflict_messages_name_the_library(tmp_path):
    config = _make_config(tmp_path)
    archive = tmp_path / "archive"
    shared = tmp_path / "shared"
    archive.mkdir()
    shared.mkdir()
    config.update_settings({
        "archive_root": str(archive),
        "obsidian_reference_root": str(shared),
        "obsidian_output_directories": [{"name": "乙", "directory": str(shared)}],
    })
    conflicts = config.get_directory_conflicts()
    assert any(item["code"] == "reference_equals_output" and "乙" in item["message"] for item in conflicts)


def _make_client(tmp_path, name="api.sqlite3"):
    database = Database(tmp_path / name)
    app = FastAPI()
    app.state.workbench_runner = TaskRunner(database, AppConfig(database))
    app.include_router(api._router)
    app.add_exception_handler(api.RequestValidationError, api._validation_error_handler)
    return TestClient(app, base_url="http://localhost"), app.state.workbench_runner


def _insert_card(runner: TaskRunner, archive: Path, artifact_id: str, title: str = "测试标题") -> tuple[str, bytes]:
    content = f"# {title}\n\n正文内容\n".encode("utf-8")
    card_path = archive / f"{artifact_id}.md"
    card_path.write_bytes(content)
    now = "2026-01-01T00:00:00+00:00"
    with runner.database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO assets (id, identity_key, source_kind, platform, title, created_at)
               VALUES (?, ?, 'url', 'douyin', ?, ?)""",
            (f"asset-{artifact_id}", f"key-{artifact_id}", title, now),
        )
        connection.execute(
            """INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (f"task-{artifact_id}", f"asset-{artifact_id}", '{"kind":"url","value":"https://example.com/a"}', now, now),
        )
        connection.execute(
            """INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json,
                                      input_artifact_ids_json, output_artifact_ids_json)
               VALUES (?, ?, 'knowledge_cards', 0, 'completed', '[]', '[]', '[]')""",
            (f"step-{artifact_id}", f"task-{artifact_id}"),
        )
        connection.execute(
            """INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at)
               VALUES (?, ?, ?, 'knowledge_card', ?, ?, 1, '{}', ?)""",
            (artifact_id, f"task-{artifact_id}", f"step-{artifact_id}", str(card_path),
             hashlib.sha256(content).hexdigest(), now),
        )
    return hashlib.sha256(content).hexdigest(), content


def _configure(client, *, archive: Path, libraries: list[tuple[str, Path]]) -> None:
    payload = {
        "archive_root": str(archive),
        "obsidian_output_directories": [{"name": name, "directory": str(path)} for name, path in libraries],
    }
    response = client.put("/api/v1/config", json=payload)
    assert response.status_code == 200, response.text


def test_review_writes_selected_library_and_records_root(tmp_path, monkeypatch):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    lib_a = tmp_path / "lib-a"
    lib_b = tmp_path / "lib-b"
    for folder in (archive, lib_a, lib_b):
        folder.mkdir()
    _configure(client, archive=archive, libraries=[("甲", lib_a), ("乙", lib_b)])
    sha, _ = _insert_card(runner, archive, "card-1")

    response = client.post("/api/v1/artifacts/card-1/review", json={
        "expected_sha256": sha,
        "library_name": "乙",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "knowledge_card_approved"
    assert body["vault_write"]["written"] is True
    assert body["vault_write"]["library_name"] == "乙"
    assert (lib_b / "测试标题.md").is_file()
    assert not (lib_a / "测试标题.md").exists()
    metadata = body["metadata"]
    assert metadata["vault_library_name"] == "乙"
    assert metadata["vault_output_root"] == str(lib_b)

    opened = []
    monkeypatch.setattr(api, "_open_local_path", lambda path, reveal=False: opened.append(path))
    assert client.post("/api/v1/artifacts/card-1/open-vault-note").status_code == 200
    assert opened == [lib_b / "测试标题.md"]


def test_review_without_selection_skips_write_when_multiple_libraries(tmp_path):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    lib_a = tmp_path / "lib-a"
    lib_b = tmp_path / "lib-b"
    for folder in (archive, lib_a, lib_b):
        folder.mkdir()
    _configure(client, archive=archive, libraries=[("甲", lib_a), ("乙", lib_b)])
    sha, _ = _insert_card(runner, archive, "card-2")

    response = client.post("/api/v1/artifacts/card-2/review", json={"expected_sha256": sha})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "knowledge_card_approved"
    assert body["vault_write"]["written"] is False
    assert "未指定目标" in body["vault_write"]["reason"]
    assert not (lib_a / "测试标题.md").exists()
    assert not (lib_b / "测试标题.md").exists()


def test_review_with_unknown_library_name_skips_write(tmp_path):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    lib_a = tmp_path / "lib-a"
    for folder in (archive, lib_a):
        folder.mkdir()
    _configure(client, archive=archive, libraries=[("甲", lib_a)])
    sha, _ = _insert_card(runner, archive, "card-3")

    response = client.post("/api/v1/artifacts/card-3/review", json={
        "expected_sha256": sha,
        "library_name": "不存在的库",
    })
    assert response.status_code == 200, response.text
    assert response.json()["vault_write"]["written"] is False


def test_review_single_library_writes_without_selection(tmp_path):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    lib_a = tmp_path / "lib-a"
    for folder in (archive, lib_a):
        folder.mkdir()
    _configure(client, archive=archive, libraries=[("甲", lib_a)])
    sha, _ = _insert_card(runner, archive, "card-4")

    response = client.post("/api/v1/artifacts/card-4/review", json={"expected_sha256": sha})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vault_write"]["written"] is True
    assert (lib_a / "测试标题.md").is_file()


def test_manual_vault_write_uses_selected_library(tmp_path):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    lib_a = tmp_path / "lib-a"
    lib_b = tmp_path / "lib-b"
    for folder in (archive, lib_a, lib_b):
        folder.mkdir()
    # obsidian 写入要求相对路径的上级文件夹已存在。
    (lib_a / "定稿").mkdir()
    _configure(client, archive=archive, libraries=[("甲", lib_a), ("乙", lib_b)])
    sha, _ = _insert_card(runner, archive, "card-5")

    assert client.post("/api/v1/artifacts/card-5/review", json={"expected_sha256": sha, "library_name": "乙"}).status_code == 200
    preview = client.post("/api/v1/obsidian/preview-write", json={
        "artifact_id": "card-5",
        "relative_path": "定稿/第二篇.md",
        "library_name": "甲",
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["library_name"] == "甲"
    write = client.post("/api/v1/obsidian/write", json={
        "artifact_id": "card-5",
        "relative_path": "定稿/第二篇.md",
        "library_name": "甲",
    })
    assert write.status_code == 200, write.text
    assert write.json()["written"] is True
    assert (lib_a / "定稿" / "第二篇.md").is_file()
    assert not (lib_b / "定稿").exists()


def test_review_without_libraries_configured_only_approves(tmp_path):
    client, runner = _make_client(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    response = client.put("/api/v1/config", json={"archive_root": str(archive)})
    assert response.status_code == 200, response.text
    sha, _ = _insert_card(runner, archive, "card-6")

    response = client.post("/api/v1/artifacts/card-6/review", json={"expected_sha256": sha})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vault_write"]["written"] is False
    assert "未配置" in body["vault_write"]["reason"]
