import sys

import pytest

from app.workbench import api
from app.workbench import collection_sources as cs
from app.workbench import uploads as uploads_module
from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench.models import StepKind
from app.workbench.runner import TaskRunner
from app.workbench.steps.import_media import import_media

# steps 包把 import_media 函数重导出为包属性；从 sys.modules 取真正的模块再打补丁。
import_media_module = sys.modules['app.workbench.steps.import_media']
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_runner(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    config = AppConfig(database)
    config.update_settings({'archive_root': str(tmp_path / 'archive')})
    runner = TaskRunner(database, config)
    # 任务创建测试只验证登记与来源记录，不启动后台工作线程。
    monkeypatch.setattr(runner, 'start', lambda: None)
    monkeypatch.setattr(runner, 'require_archive_root', lambda: None)
    return database, config, runner, tmp_path / 'archive'


def test_register_collection_media_keeps_original_without_copies(tmp_path, monkeypatch):
    database, _config, _runner, archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(uploads_module, 'UPLOAD_ROOT', tmp_path / 'data' / 'workbench' / 'uploads')
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / '视频素材.mp4'
    original.write_bytes(b'original media')

    source = cs.register_collection_media_source(database, original)
    assert source['sourceType'] == 'local'
    assert source['filename'] == '视频素材.mp4'
    assert source['detectedMediaType'] == 'video'
    assert original.exists()
    assert not uploads_module.UPLOAD_ROOT.exists()

    receipt = cs.get_collection_media_source(database, source['uploadId'])
    assert receipt.path == original.resolve()
    assert receipt.path.read_bytes() == b'original media'
    assert receipt.size_bytes == len(b'original media')

    assert cs.delete_collection_media_source(database, source['uploadId']) is True
    assert cs.delete_collection_media_source(database, source['uploadId']) is False
    assert original.exists()


def test_register_collection_audio_media(tmp_path, monkeypatch):
    database, _config, _runner, _archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'audio')
    original = tmp_path / '访谈录音.mp3'
    original.write_bytes(b'audio bytes')

    source = cs.register_collection_media_source(database, original)
    assert source['detectedMediaType'] == 'audio'
    receipt = cs.get_collection_media_source(database, source['uploadId'])
    assert receipt.detected_media_type == 'audio'


def test_unsupported_extension_is_rejected(tmp_path, monkeypatch):
    database, _config, _runner, _archive = _make_runner(tmp_path, monkeypatch)
    not_media = tmp_path / 'notes.txt'
    not_media.write_bytes(b'text')
    with pytest.raises(cs.CollectionMediaError, match='MP4'):
        cs.register_collection_media_source(database, not_media)


def test_changed_or_missing_original_is_rejected(tmp_path, monkeypatch):
    database, _config, _runner, _archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'video.mp4'
    original.write_bytes(b'original media')
    source = cs.register_collection_media_source(database, original)

    original.write_bytes(b'modified media')
    with pytest.raises(cs.CollectionMediaError, match='变化'):
        cs.get_collection_media_source(database, source['uploadId'], verify_hash=True)

    original.unlink()
    with pytest.raises(cs.CollectionMediaError, match='移动'):
        cs.get_collection_media_source(database, source['uploadId'])


def test_over_limit_requires_explicit_confirmation(tmp_path, monkeypatch):
    database, _config, _runner, _archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    monkeypatch.setattr(cs, 'MAX_SOFT_LIMIT_BYTES', 4)
    original = tmp_path / 'large.mp4'
    original.write_bytes(b'x' * 10)

    with pytest.raises(cs.CollectionMediaError) as excinfo:
        cs.register_collection_media_source(database, original)
    assert excinfo.value.code == 'over_limit'
    assert excinfo.value.size_bytes == 10

    source = cs.register_collection_media_source(database, original, confirm_over_limit=True)
    receipt = cs.get_collection_media_source(database, source['uploadId'])
    assert receipt.confirmed_over_limit is True


def test_create_import_task_references_original_and_import_copies_to_archive(tmp_path, monkeypatch):
    database, _config, runner, archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    monkeypatch.setattr(import_media_module, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'video.mp4'
    original.write_bytes(b'original media')
    source = cs.register_collection_media_source(database, original)

    asset, task, receipt = runner.create_import_task(
        local_source_id=source['uploadId'], step_kinds=[StepKind.IMPORT]
    )
    assert task.source.value == f"workbench-local-media:{source['uploadId']}"
    step = task.steps[0]
    assert runner._transient_source_for_task(task, step, override=None) == str(original.resolve())

    imported = import_media(
        receipt.path,
        archive,
        task.id,
        confirm_over_limit=receipt.confirmed_over_limit,
        database=database,
        expected_sha256=receipt.sha256,
    )
    assert imported.artifact.sha256 == receipt.sha256
    assert archive.exists() and any(archive.rglob('*.mp4'))
    assert original.read_bytes() == b'original media'


def test_create_import_task_rejects_double_source(tmp_path, monkeypatch):
    _database, _config, runner, _archive = _make_runner(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='exactly one'):
        runner.create_import_task(upload_id='a' * 32, local_source_id='b' * 32)


def test_native_selection_and_discard_endpoints(tmp_path, monkeypatch):
    database, config, runner, _archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'video.mp4'
    original.write_bytes(b'original media')
    monkeypatch.setattr(api, 'choose_local_media', lambda: str(original))
    application = FastAPI()
    application.state.workbench_runner = runner
    application.include_router(api._router)
    client = TestClient(application)

    response = client.post(
        '/api/v1/collection/local-media/select',
        json={'confirm_over_limit': False},
        headers={'origin': 'http://testserver'},
    )
    assert response.status_code == 200, response.text
    source = response.json()['source']
    assert source['sourceType'] == 'local'
    assert source['sourcePath'] == str(original.resolve())

    monkeypatch.setattr(api, 'choose_local_media', lambda: None)
    cancelled = client.post(
        '/api/v1/collection/local-media/select',
        json={},
        headers={'origin': 'http://testserver'},
    )
    assert cancelled.json() == {'source': None}
    monkeypatch.setattr(api, 'choose_local_media', lambda: str(original))

    # 未完成任务引用期间禁止移除登记。
    asset, task, _receipt = runner.create_import_task(
        local_source_id=source['uploadId'], step_kinds=[StepKind.IMPORT]
    )
    guarded = client.delete(
        f"/api/v1/collection/local-media/{source['uploadId']}",
        headers={'origin': 'http://testserver'},
    )
    assert guarded.status_code == 409

    with database.transaction(write=True) as connection:
        connection.execute("UPDATE tasks SET status='completed' WHERE id=?", (task.id,))
    removed = client.delete(
        f"/api/v1/collection/local-media/{source['uploadId']}",
        headers={'origin': 'http://testserver'},
    )
    assert removed.status_code == 200
    assert removed.json() == {'deleted': True}
    assert client.delete(
        '/api/v1/collection/local-media/' + '0' * 32,
        headers={'origin': 'http://testserver'},
    ).json() == {'deleted': False}


def test_select_endpoint_reports_over_limit_without_registering(tmp_path, monkeypatch):
    database, config, runner, _archive = _make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, '_validate_media_header', lambda *args: 'video')
    monkeypatch.setattr(cs, 'MAX_SOFT_LIMIT_BYTES', 4)
    original = tmp_path / 'large.mp4'
    original.write_bytes(b'x' * 10)
    monkeypatch.setattr(api, 'choose_local_media', lambda: str(original))
    application = FastAPI()
    application.state.workbench_runner = runner
    application.include_router(api._router)
    client = TestClient(application)

    response = client.post(
        '/api/v1/collection/local-media/select',
        json={'confirm_over_limit': False},
        headers={'origin': 'http://testserver'},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['source'] is None
    assert payload['overLimit'] is True
    assert payload['sizeBytes'] == 10
    with database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM collection_media_sources").fetchone()['n'] == 0

    confirmed = client.post(
        '/api/v1/collection/local-media/select',
        json={'confirm_over_limit': True},
        headers={'origin': 'http://testserver'},
    )
    assert confirmed.json()['source']['sourceType'] == 'local'


def test_select_endpoint_rejects_unsupported_file(tmp_path, monkeypatch):
    _database, _config, runner, _archive = _make_runner(tmp_path, monkeypatch)
    not_media = tmp_path / 'notes.txt'
    not_media.write_bytes(b'text')
    monkeypatch.setattr(api, 'choose_local_media', lambda: str(not_media))
    application = FastAPI()
    application.state.workbench_runner = runner
    application.include_router(api._router)
    client = TestClient(application)

    response = client.post(
        '/api/v1/collection/local-media/select',
        json={},
        headers={'origin': 'http://testserver'},
    )
    assert response.status_code == 422
    assert 'MP4' in response.json()['detail']
