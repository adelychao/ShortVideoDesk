import hashlib
import json
from types import SimpleNamespace

import pytest

from app.workbench import api
from app.workbench.database import Database
from app.workbench.runner import RunnerError, TaskRunner


def setup_task(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    db.migrate()
    source = json.dumps({'kind': 'url', 'value': 'https://www.douyin.com/video/123', 'platform': 'douyin', 'source_id': '123'})
    note = tmp_path / 'comments.md'
    note.write_text('comments', encoding='utf-8')
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id, identity_key, source_kind, created_at) VALUES ('a','a','url','2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t','a',?,'completed','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')", (source,))
        c.execute("INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json) VALUES ('s','t','comments',0,'completed','[]','[]','[]')")
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('note','t','s','note',?,?,1,?,'2026-01-01T00:00:00+00:00')", (str(note), hashlib.sha256(note.read_bytes()).hexdigest(), json.dumps({'scope': 'comments', 'collected_count': 1})))
        c.execute("INSERT INTO comment_collection_state (task_id,asset_id,collected_count,status,updated_at) VALUES ('t','a',1,'completed','now')")
        c.execute("INSERT INTO comments (id,asset_id,task_id,platform_comment_id,text,like_count,collected_at,raw_fields_json) VALUES ('c','a','t','platform-1','question',3,'now','{}')")
        c.execute("INSERT INTO comment_collection_items (task_id,comment_id,collected_at) VALUES ('t','c','now')")
    return db


def test_insight_page_joins_classified_comments(tmp_path, monkeypatch):
    db = setup_task(tmp_path)
    data = {'schema_version': 2, 'selected': [], 'classifications': [{'comment_id': 'platform-1', 'labels': ['观众提问']}], 'processed_comment_ids': ['platform-1']}
    path = tmp_path / 'analysis.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('analysis','t','s','comment_analysis_json',?,?,1,'{}','now')", (str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    fake = SimpleNamespace(database=db, config=SimpleNamespace(archive_root=tmp_path), repository=SimpleNamespace(get_task=lambda _: SimpleNamespace(asset_id='a')))
    monkeypatch.setattr(api, '_runner', lambda _: fake)
    result = api.task_comment_insights('t', None, category='观众提问', limit=50, offset=0)
    assert result['classification_total'] == 1
    assert result['classification_page'][0]['text'] == 'question'


def test_preprocessing_retains_comment_with_invalid_optional_metadata(tmp_path):
    from app.workbench.steps.analyze_comments import _load_collected_comments
    db = setup_task(tmp_path)
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    task = runner.repository.get_task('t')
    comments = _load_collected_comments(SimpleNamespace(task=task, repository=runner.repository))
    assert len(comments) == 1 and comments[0].text == 'question'


def test_followup_analysis_binds_current_note_and_consent(tmp_path, monkeypatch):
    db = setup_task(tmp_path)
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    monkeypatch.setattr(runner, 'start', lambda: None)
    monkeypatch.setattr('app.workbench.model_profiles.task_model_settings', lambda *_: {'model_provider': 'zhipu', 'model_id': 'model'})
    step_id = runner.enqueue_followup_comment_analysis('t')
    with db.transaction() as c:
        step = c.execute('SELECT * FROM task_steps WHERE id = ?', (step_id,)).fetchone()
        consent = c.execute("SELECT * FROM consents WHERE task_id='t' AND step_kind='analyze_comments'").fetchone()
    assert json.loads(step['input_artifact_ids_json']) == ['note']
    assert json.loads(consent['artifact_ids_json']) == ['note']
    assert runner._queue.get_nowait().selected_step_id == step_id


def test_followup_analysis_rejects_stale_comment_note(tmp_path, monkeypatch):
    db = setup_task(tmp_path)
    with db.transaction(write=True) as c:
        c.execute("UPDATE comment_collection_state SET collected_count=2 WHERE task_id='t'")
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    monkeypatch.setattr(runner, 'start', lambda: None)
    with pytest.raises(RunnerError, match='评论资料'):
        runner.enqueue_followup_comment_analysis('t')


def test_new_budgeted_analysis_keeps_interrupted_legacy_step(tmp_path, monkeypatch):
    db = setup_task(tmp_path)
    with db.transaction(write=True) as c:
        c.execute("UPDATE tasks SET status='partial' WHERE id='t'")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('old','t','analyze_comments',1,'blocked','[]','[]','[]')")
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    monkeypatch.setattr(runner, 'start', lambda: None)
    monkeypatch.setattr('app.workbench.model_profiles.task_model_settings', lambda *_: {'model_provider': 'zhipu', 'model_id': 'model'})
    step_id = runner.enqueue_followup_comment_analysis('t', options={'mode': 'economy'})
    with db.transaction() as c:
        assert c.execute("SELECT status FROM task_steps WHERE id='old'").fetchone()['status'] == 'blocked'
        assert c.execute('SELECT status FROM task_steps WHERE id=?', (step_id,)).fetchone()['status'] == 'queued'
        assert json.loads(c.execute('SELECT options_json FROM comment_analysis_options WHERE step_id=?', (step_id,)).fetchone()['options_json'])['mode'] == 'economy'


def test_continue_raises_budget_without_changing_batch_settings(tmp_path, monkeypatch):
    db = setup_task(tmp_path)
    options = {'mode': 'economy', 'batch_size': 200, 'candidate_limit': 2000, 'max_output_tokens': 32768, 'budget_cny': 1.0, 'token_budget': 300000}
    with db.transaction(write=True) as c:
        c.execute("UPDATE tasks SET status='partial' WHERE id='t'")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('analysis','t','analyze_comments',1,'partial','[]','[]','[]')")
        c.execute("INSERT INTO comment_analysis_options (step_id,task_id,options_json,created_at) VALUES ('analysis','t',?,'now')", (json.dumps(options),))
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    called = []
    monkeypatch.setattr(runner, 'retry_step', lambda task_id, step_id: called.append((task_id, step_id)))
    monkeypatch.setattr(api, '_runner', lambda _: runner)
    monkeypatch.setattr(api, 'task_detail', lambda *_: {'task': {'id': 't'}})
    api.continue_task_comment_analysis('t', 'analysis', api.ContinueCommentAnalysisRequest(authorized=True, budget_cny=2.0, token_budget=400000), None)
    with db.transaction() as c:
        saved = json.loads(c.execute("SELECT options_json FROM comment_analysis_options WHERE step_id='analysis'").fetchone()[0])
    assert saved['budget_cny'] == 2.0 and saved['token_budget'] == 400000
    assert saved['batch_size'] == 200 and called == [('t', 'analysis')]


def test_economy_step_persists_partial_and_final_results(tmp_path, monkeypatch):
    import threading
    from app.workbench.runner import StepContext
    from app.workbench.steps import analyze_comments as legacy
    from app.workbench.steps import analyze_comments_economy as economy
    db = setup_task(tmp_path)
    video = tmp_path / 'media' / 'video.mp4'
    video.parent.mkdir()
    video.write_bytes(b'video')
    opts = {'mode': 'economy', 'batch_size': 200, 'candidate_limit': 2000, 'budget_cny': 1.0, 'token_budget': 300000, 'input_price_per_million': 0.8, 'output_price_per_million': 2.8, 'max_output_tokens': 32768}
    with db.transaction(write=True) as c:
        c.execute("UPDATE tasks SET status='running' WHERE id='t'")
        c.execute("UPDATE comments SET collected_at='2026-01-01T00:00:00+00:00' WHERE id='c'")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('analysis','t','analyze_comments',1,'running','[]','[]','[]')")
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('video','t','s','video',?,?,1,'{}','2026-01-01T00:00:00+00:00')", (str(video), hashlib.sha256(video.read_bytes()).hexdigest()))
        c.execute("INSERT INTO comment_analysis_options (step_id,task_id,options_json,created_at) VALUES ('analysis','t',?,'now')", (json.dumps(opts),))
    runner = TaskRunner(db, SimpleNamespace(archive_root=tmp_path))
    task = runner.repository.get_task('t')
    step = next(item for item in task.steps if item.id == 'analysis')
    context = StepContext(task=task, step=step, repository=runner.repository, archive_root=tmp_path, cancel_event=threading.Event(), progress=lambda *_: None, transient_source_url='')
    for module in (legacy, economy):
        monkeypatch.setattr(module, 'configured_model_id', lambda _: 'model')
        monkeypatch.setattr(module, 'configured_provider_id', lambda _: 'provider')
    calls = []
    def call(_context, **kwargs):
        calls.append(1)
        with db.transaction(write=True) as c:
            c.execute("INSERT INTO model_runs (id,task_id,step_id,provider_id,model_id,artifact_ids_json,usage_json,cost_json,status,created_at) VALUES ('run','t','analysis','provider','model','[]',?,'{}','completed','now')", (json.dumps({'input_tokens': 100, 'output_tokens': 200}),))
        return SimpleNamespace(text=json.dumps({'items': [{'n': 1, 'labels': ['Q']}]}))
    monkeypatch.setattr(economy, 'authorized_model_call', call)
    result = legacy.analyze_collected_comments(context)
    assert result.status == 'completed'
    with db.transaction() as c:
        rows = c.execute("SELECT path FROM artifacts WHERE task_id='t' AND kind='comment_analysis_json' ORDER BY version").fetchall()
    assert len(rows) == 2
    payload = json.loads(__import__('pathlib').Path(rows[-1]['path']).read_text(encoding='utf-8'))
    assert payload['coverage']['analyzed_original_count'] == 1
    assert payload['coverage']['output_tokens'] == 200
    with db.transaction(write=True) as c:
        opts['budget_cny'] = 2.0
        c.execute("UPDATE comment_analysis_options SET options_json=? WHERE step_id='analysis'", (json.dumps(opts),))
    legacy.analyze_collected_comments(context)
    assert len(calls) == 1
