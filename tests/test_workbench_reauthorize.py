from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.workbench import api
from app.workbench.database import Database
from app.workbench.models import StepKind


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / 'state.sqlite3')
    db.migrate()
    now = datetime.now(timezone.utc).isoformat()
    note = tmp_path / 'current.md'
    note.write_text('new comments', encoding='utf-8')
    old = tmp_path / 'old.md'
    old.write_text('old comments', encoding='utf-8')
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id, identity_key, source_kind, created_at) VALUES ('a', 'a', 'url', ?)", (now,))
        c.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t', 'a', '{}', 'blocked', ?, ?)", (now, now))
        c.execute("INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json, error_code) VALUES ('s', 't', 'analyze_comments', 1, 'blocked', '[]', '[]', '[]', 'comments_analysis_stale')")
        for artifact_id, path, version in [('old', old, 1), ('new', note, 2)]:
            c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES (?, 't', 's', 'note', ?, ?, ?, ?, ?)", (artifact_id, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), version, json.dumps({'scope':'comments', 'collected_count': version}), now))
        c.execute("INSERT INTO comment_collection_state (task_id,asset_id,page_count,collected_count,missing_like_count,missing_fields_json,stop_reason,status,updated_at,sort_order) VALUES ('t','a',2,2,0,'{}','done','completed',?,'likes_desc')", (now,))
        c.execute("INSERT INTO consents (id,task_id,step_kind,provider_id,model_id,artifact_ids_json,authorized,authorized_at) VALUES ('c','t','analyze_comments','zhipu','glm-5.3-flash','[\"old\"]',1,?)", (now,))
    step = SimpleNamespace(id='s', kind=StepKind.ANALYZE_COMMENTS, status='blocked', error_code='comments_analysis_stale')
    runner = SimpleNamespace(database=db, config=SimpleNamespace(archive_root=tmp_path, get_settings=lambda: {'model_id':'glm-5.3-flash'}), repository=SimpleNamespace(get_task=lambda _: SimpleNamespace(steps=[step],status='blocked')), retry_step=Mock(), _lock=threading.RLock())
    monkeypatch.setattr(api, '_runner', lambda _: runner)
    monkeypatch.setattr(api, '_model_configuration_ready', lambda _: True)
    return db, runner, note


def test_reauthorize_new_comments_binds_exact_current_note_before_retry(tmp_path, monkeypatch):
    db, runner, note = _setup(tmp_path, monkeypatch)
    result = api.authorize_comment_analysis('t', 's', api.AuthorizeCommentAnalysisRequest(artifact_id='new', authorized=True), None)
    with db.transaction() as c:
        consent = c.execute("SELECT artifact_ids_json FROM consents WHERE task_id='t' ORDER BY authorized_at DESC, rowid DESC LIMIT 1").fetchone()
    assert json.loads(consent['artifact_ids_json']) == ['new']
    assert result['authorized_artifact_ids'] == ['new']
    runner.retry_step.assert_called_once_with('t', 's')


@pytest.mark.parametrize('artifact_id,authorized', [('old', True), ('new', False)])
def test_reauthorize_rejects_old_material_or_missing_consent(tmp_path, monkeypatch, artifact_id, authorized):
    db, runner, _ = _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        api.authorize_comment_analysis('t', 's', api.AuthorizeCommentAnalysisRequest(artifact_id=artifact_id, authorized=authorized), None)
    with db.transaction() as c:
        consent = c.execute("SELECT artifact_ids_json FROM consents WHERE task_id='t' ORDER BY authorized_at DESC, rowid DESC LIMIT 1").fetchone()
    assert json.loads(consent['artifact_ids_json']) == ['old']
    runner.retry_step.assert_not_called()


def test_reauthorize_rejects_modified_note(tmp_path, monkeypatch):
    db, runner, note = _setup(tmp_path, monkeypatch)
    note.write_text('changed after approval', encoding='utf-8')
    with pytest.raises(HTTPException):
        api.authorize_comment_analysis('t', 's', api.AuthorizeCommentAnalysisRequest(artifact_id='new', authorized=True), None)
    runner.retry_step.assert_not_called()


def test_reauthorize_rejects_note_older_than_saved_comments(tmp_path, monkeypatch):
    db, runner, _ = _setup(tmp_path, monkeypatch)
    with db.transaction(write=True) as c:
        c.execute("UPDATE comment_collection_state SET collected_count=3 WHERE task_id='t'")
    with pytest.raises(HTTPException):
        api.authorize_comment_analysis('t', 's', api.AuthorizeCommentAnalysisRequest(artifact_id='new', authorized=True), None)
    runner.retry_step.assert_not_called()
