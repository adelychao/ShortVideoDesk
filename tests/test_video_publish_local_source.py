from datetime import datetime, timedelta, timezone
import asyncio
from io import BytesIO

import pytest

from app.workbench.database import Database
from app.workbench import video_publishing as video
from app.workbench import api
from app.workbench.config import AppConfig
from app.workbench.runner import TaskRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


def test_local_video_draft_and_batch_reference_original_without_copies(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    monkeypatch.setattr(video, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(video, '_PUBLISH_MEDIA_ROOT', tmp_path / 'batches')
    monkeypatch.setattr(video, '_VIDEO_PUBLISH_DRAFT_ROOT', tmp_path / 'drafts')
    monkeypatch.setattr(video, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original video')
    source = video.register_local_video_source(database, original)
    draft = video.save_video_publish_draft(database, draft_id=None, upload_id=None,
        local_source_id=source['uploadId'], cover_upload_ids={}, form={'title': 'Test'})
    assert video.get_video_publish_draft_media_path(database, draft['id']) == original
    assert not list((tmp_path / 'drafts').rglob('*.mp4'))
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO video_publish_accounts (id, platform_id, account_alias, status, created_at, updated_at) VALUES ('a', 'kuaishou', 'creator', 'connected', '2026-11-09', '2026-11-09')")
    batch = video.create_video_publish_batch(database, archive_root=None, idempotency_key='local-source-test',
        asset_id=None, upload_id=None, local_source_id=source['uploadId'], title='Test', description='',
        tags=[], schedule_at=None, targets=[{'account_id': 'a', 'options': {}}])
    assert not list((tmp_path / 'batches').rglob('*.mp4'))
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_targets SET status='failed', finished_at='2020-01-01T00:00:00+00:00' WHERE batch_id=?", (batch['id'],))
    video.cleanup_expired_video_publish_media(database, now=datetime.now(timezone.utc) + timedelta(days=30))
    assert original.read_bytes() == b'original video'


def test_changed_local_video_is_rejected(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    monkeypatch.setattr(video, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original video')
    source = video.register_local_video_source(database, original)
    original.write_bytes(b'modified video')
    with pytest.raises(video.VideoPublishError, match='变化'):
        video.get_local_video_source(database, source['uploadId'], verify_hash=True)


def test_native_selection_preview_and_draft_recovery_when_original_missing(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    config = AppConfig(database)
    application = FastAPI()
    application.state.workbench_runner = TaskRunner(database, config)
    application.include_router(api._router)
    client = TestClient(application)
    original = tmp_path / 'video.mp4'
    original.write_bytes(b'original video')
    monkeypatch.setattr(api, 'choose_local_video', lambda: str(original))
    monkeypatch.setattr(video, '_validate_media_header', lambda *args: 'video')
    response = client.post('/api/v1/video-publish/local-sources/select', json={}, headers={'origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    source = response.json()['source']
    preview = client.get(source['previewUrl'], headers={'sec-fetch-site': 'same-origin', 'range': 'bytes=0-3'})
    assert preview.status_code == 206
    assert preview.content == b'orig'
    assert client.get(source['previewUrl'], headers={'origin': 'https://other.example'}).status_code == 403
    draft = video.save_video_publish_draft(database, draft_id=None, upload_id=None,
        local_source_id=source['uploadId'], cover_upload_ids={}, form={'title': 'Keep my text'})
    original.unlink()
    restored = asyncio.run(video.restore_video_publish_draft(database, draft['id']))
    assert restored['media_upload'] is None
    assert restored['draft']['form']['title'] == 'Keep my text'


def test_disabling_scheduled_cleanup_keeps_expired_covers(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    config = AppConfig(database)
    config.update_settings({'video_publish_auto_cleanup_enabled': False})
    monkeypatch.setattr(video, '_cleanup_unused_video_covers', lambda *args: pytest.fail('cleanup ran while disabled'))
    assert video.cleanup_expired_video_publish_media(database)['bytes_freed'] == 0


def test_local_batch_cleanup_removes_only_unshared_configured_covers(tmp_path, monkeypatch):
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    config = AppConfig(database)
    cover_root = tmp_path / 'custom-covers'
    config.update_settings({'video_publish_cover_root': str(cover_root), 'video_publish_retention_days': 1})
    monkeypatch.setattr(video, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(video, '_PUBLISH_MEDIA_ROOT', tmp_path / 'batches')
    monkeypatch.setattr(video, '_validate_media_header', lambda *args: 'video')
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original video')
    source = video.register_local_video_source(database, original)
    buffer = BytesIO()
    Image.new('RGB', (160, 120), 'red').save(buffer, format='PNG')
    cover = video._store_video_cover(database, 'cover.png', '.png', buffer.getvalue())
    unrelated = cover_root / 'personal.png'
    unrelated.write_bytes(b'personal file')
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO video_publish_accounts (id, platform_id, account_alias, status, created_at, updated_at) VALUES ('a', 'kuaishou', 'creator', 'connected', '2026-11-09', '2026-11-09')")
    batch = video.create_video_publish_batch(database, archive_root=None, idempotency_key='local-cover-test',
        asset_id=None, upload_id=None, local_source_id=source['uploadId'], title='Test', description='', tags=[],
        schedule_at=None, targets=[{'account_id': 'a', 'options': {'cover_upload_id': cover.upload_id}}])
    future = datetime.now(timezone.utc) + timedelta(days=3)
    video.cleanup_expired_video_publish_media(database, now=future)
    assert cover.path.exists()
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_targets SET status='failed', finished_at='2020-01-01T00:00:00+00:00' WHERE batch_id=?", (batch['id'],))
    original.write_bytes(b'modified video')
    with pytest.raises(video.VideoPublishError, match='变化'):
        video.request_video_publish_retry(database, batch['targets'][0]['id'], confirmed=True)
    video.cleanup_expired_video_publish_media(database, now=future)
    assert not cover.path.exists()
    assert unrelated.read_bytes() == b'personal file'
    assert original.read_bytes() == b'modified video'
