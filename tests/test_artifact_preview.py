import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.workbench import api
from app.workbench.database import Database


@pytest.mark.parametrize('kind', ['video_notes', 'note', 'comment_analysis'])
def test_archived_summary_documents_can_be_previewed_with_hash_check(tmp_path, monkeypatch, kind):
    db = Database(tmp_path / 'db.sqlite3')
    db.migrate()
    root = tmp_path / 'archive'
    root.mkdir()
    file = root / 'text.md'
    file.write_text('hello', encoding='utf-8')
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','url','now')")
        c.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','completed','now','now')")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','download',0,'completed','[]','[]','[]')")
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('item','t','s',?,?,?,1,'{}','now')", (kind,str(file),hashlib.sha256(file.read_bytes()).hexdigest()))
    monkeypatch.setattr(api, '_runner', lambda _: SimpleNamespace(database=db, config=SimpleNamespace(archive_root=root)))
    assert api.read_text_artifact('item', None)['content'] == 'hello'
    file.write_text('changed', encoding='utf-8')
    with pytest.raises(HTTPException) as error:
        api.read_text_artifact('item', None)
    assert error.value.status_code == 404
