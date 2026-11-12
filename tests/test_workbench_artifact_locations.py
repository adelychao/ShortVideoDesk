from pathlib import Path
from unittest.mock import Mock

import pytest

from app.workbench import api


@pytest.mark.skipif(api.os.name != 'nt', reason='Windows file manager behavior')
def test_open_containing_directory_handles_commas_and_spaces(monkeypatch, tmp_path):
    launch = Mock()
    monkeypatch.setattr(api.os, 'startfile', launch)
    artifact = tmp_path / '视频标题, 需要打开' / '视频资料.md'
    api._open_local_path(artifact, reveal=True)
    launch.assert_called_once_with(str(artifact.parent))


def test_internal_artifact_is_separate_from_user_facing_output(tmp_path):
    import hashlib
    import threading
    from app.workbench.config import AppConfig
    from app.workbench.database import Database
    from app.workbench.models import StepKind
    from app.workbench.runner import TaskRunner, StepContext
    from app.workbench.steps.artifact_store import register_bytes

    db = Database(tmp_path / 'data.sqlite')
    config = AppConfig(db)
    root = config.set_archive_root(tmp_path / 'archive')
    runner = TaskRunner(db, config)
    try:
        _, task = runner.create_download_task('https://www.douyin.com/video/123', step_kinds=[StepKind.DOWNLOAD])
        step = task.steps[0]
        folder = root / 'video'
        folder.mkdir()
        media = folder / 'video.mp4'
        media.write_bytes(b'video')
        with db.transaction(write=True) as connection:
            connection.execute("UPDATE task_steps SET status = 'running' WHERE id = ?", (step.id,))
            connection.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('media',?,?,'video',?,?,1,'{}','2026-01-01')", (task.id, step.id, str(media), hashlib.sha256(b'video').hexdigest()))
        context = StepContext(task, step, runner.repository, root, threading.Event(), lambda *_: None, '')
        raw = register_bytes(context, 'transcript_raw', '.json', b'{}', {})
        output = register_bytes(context, 'transcript', '.md', b'text', {})
        assert Path(raw.path).parent == folder / '.workbench'
        assert Path(output.path).parent == folder
        assert Path(raw.path).read_bytes() == b'{}'
    finally:
        runner.stop()


def test_organize_existing_records_preserves_database_references_and_modified_files(tmp_path):
    import hashlib
    from app.workbench.database import Database
    from app.workbench.steps.artifact_housekeeping import organize_internal_records

    root = tmp_path / 'archive'
    folder = root / 'video'
    folder.mkdir(parents=True)
    db = Database(tmp_path / 'data.sqlite')
    db.migrate()
    first = folder / 'transcript_raw-first.json'
    changed = folder / 'ocr_raw-changed.json'
    result = folder / 'transcript.md'
    for path in (first, changed, result): path.write_bytes(b'original')
    digest = hashlib.sha256(b'original').hexdigest()
    with db.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id, identity_key, source_kind, created_at) VALUES ('a','a','url','now')")
        connection.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t','a','{}','partial','now','now')")
        connection.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','transcribe',0,'completed','[]','[]','[]')")
        for artifact_id, kind, path in [('first','transcript_raw',first),('changed','ocr_raw',changed),('result','transcript',result)]:
            connection.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES (?, 't','s',?,?,?,1,'{}','now')", (artifact_id,kind,str(path),digest))
    changed.write_bytes(b'user-edited')
    counts = organize_internal_records(db, root, folder)
    assert counts['moved'] == 1 and counts['skipped'] == 1
    with db.transaction() as connection:
        rows = {r['id']: Path(r['path']) for r in connection.execute("SELECT id,path FROM artifacts")}
    assert rows['first'] == folder / '.workbench' / first.name
    assert rows['first'].read_bytes() == b'original'
    assert not first.exists()
    assert rows['changed'] == changed and changed.read_bytes() == b'user-edited'
    assert rows['result'] == result and result.exists()


def test_organize_skips_records_of_running_tasks(tmp_path):
    import hashlib
    from app.workbench.database import Database
    from app.workbench.steps.artifact_housekeeping import organize_internal_records
    folder = tmp_path / 'archive' / 'video'
    folder.mkdir(parents=True)
    file = folder / 'ocr_raw-running.json'
    file.write_bytes(b'data')
    db = Database(tmp_path / 'data.sqlite')
    db.migrate()
    with db.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','url','now')")
        connection.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','running','now','now')")
        connection.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','ocr',0,'running','[]','[]','[]')")
        connection.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('x','t','s','ocr_raw',?,?,1,'{}','now')", (str(file),hashlib.sha256(b'data').hexdigest()))
    assert organize_internal_records(db, folder.parent, folder) == {'moved': 0, 'skipped': 1}
    assert file.exists()
