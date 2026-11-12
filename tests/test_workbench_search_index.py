from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

from app.workbench.database import Database
from app.workbench.models import SourceKind, SourceRecord, StepKind, StepRecord
from app.workbench.repository import TaskRepository
from app.workbench.search import refresh_local_index, search_local


def test_registered_media_and_task_text_are_indexed_with_current_schema(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "work"
    folder.mkdir(parents=True)
    video = folder / "video.mp4"
    video.write_bytes(b"video-content")
    note = folder / "text.md"
    note.write_text("检索测试独特内容", encoding="utf-8")
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    asset, task = TaskRepository(database).create_task(
        SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123", title="归档标题"),
        [StepRecord(id="download", kind=StepKind.DOWNLOAD, ordinal=0, status="completed")],
    )
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE assets SET archive_path=?, archived_at=?, sha256=? WHERE id=?",
                           (str(video), now, hashlib.sha256(video.read_bytes()).hexdigest(), asset.id))
        for artifact_id, kind, path in [("media", "video", video), ("note", "note", note)]:
            connection.execute("""INSERT INTO artifacts(id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at)
                                  VALUES (?, ?, 'download', ?, ?, ?, 1, '{}', ?)""",
                               (artifact_id, task.id, kind, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), now))
    result = refresh_local_index(database, root)
    hits, _ = search_local(database, "检索测试独特内容")
    assert result["documents"] >= 2
    assert any(hit.artifact_id == "note" and hit.task_id == task.id for hit in hits)


def test_index_error_is_identified_as_index_failure(tmp_path, monkeypatch):
    from app.workbench import migration
    from app.workbench.config import AppConfig
    from app.workbench.runner import StepResult, TaskRunner

    monkeypatch.setattr(migration, 'import_legacy_jobs', lambda *_: None)
    database = Database(tmp_path / 'state.sqlite3')
    config = AppConfig(database)
    config.set_archive_root(tmp_path / 'archive')
    runner = TaskRunner(database, config)
    class Download:
        def run(self, context):
            with database.transaction(write=True) as connection:
                connection.execute("INSERT INTO artifacts(id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('a',?,?,'video','test','hash',1,'{}','now')", (context.task.id, context.step.id))
            return StepResult('completed', ['a'])
    class BrokenIndex:
        def run(self, _context):
            raise RuntimeError('simulated index error')
    runner._handlers[StepKind.DOWNLOAD] = Download()
    runner._handlers[StepKind.INDEX] = BrokenIndex()
    try:
        _, task = runner.create_download_task('https://www.douyin.com/video/123', step_kinds=[StepKind.DOWNLOAD, StepKind.INDEX])
        runner.enqueue(task.id, transient_source_url='https://www.douyin.com/video/123')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            latest = runner.repository.get_task(task.id)
            step = next(item for item in latest.steps if item.kind is StepKind.INDEX)
            if step.status == 'failed':
                break
            time.sleep(0.02)
        assert step.status == 'failed'
        assert step.error_code == 'index_failed'
        assert '索引' in step.message
    finally:
        runner.stop()
