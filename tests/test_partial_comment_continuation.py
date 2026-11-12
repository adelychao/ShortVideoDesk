import hashlib
import json
import time
import uuid
from types import SimpleNamespace
import pytest

from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench.models import StepKind
from app.workbench.runner import StepResult, TaskRunner


def test_partial_collection_continues_first_authorized_analysis(tmp_path, monkeypatch):
    import app.workbench.migration as migration
    import app.workbench.runner as module
    from app.workbench.steps import model_call
    from app.workbench.steps.analyze_comments import _comment_note_artifact
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(migration, 'import_legacy_jobs', lambda *_: None)
    db = Database(tmp_path / 'workbench.sqlite3')
    config = AppConfig(db)
    config.set_archive_root(tmp_path / 'archive')
    config.update_settings({'model_provider': 'zhipu', 'model_id': 'glm-5.3-flash'})
    runner = TaskRunner(db, config)
    executed = []
    monkeypatch.setattr(model_call, 'get_model_api_key', lambda: 'test')
    monkeypatch.setattr(model_call, 'create_provider', lambda *_, **__: SimpleNamespace(set_client_request_id=lambda _: None))

    class Handler:
        def __init__(self, kind): self.kind = kind
        def run(self, context):
            executed.append(self.kind)
            if self.kind is StepKind.ANALYZE_COMMENTS:
                source = _comment_note_artifact(context)
                model_call.authorized_model_call(context, model_id='glm-5.3-flash', input_artifact_ids=[source.id], operation=lambda _: SimpleNamespace(text='ok'))
            artifact_id = uuid.uuid4().hex
            path = context.archive_root / artifact_id
            path.write_bytes(b'test')
            with db.transaction(write=True) as connection:
                connection.execute("INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)", (artifact_id, context.task.id, context.step.id, 'note' if self.kind is StepKind.COMMENTS else 'test', str(path), hashlib.sha256(b'test').hexdigest(), json.dumps({'scope':'comments'}), '2026-01-01'))
            return StepResult('partial' if self.kind is StepKind.COMMENTS else 'completed', [artifact_id])

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task('https://www.douyin.com/video/123', step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS])
        with db.transaction(write=True) as connection:
            connection.execute("INSERT INTO consents (id, task_id, step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at) VALUES (?, ?, 'analyze_comments', 'zhipu', 'glm-5.3-flash', '[]', 1, ?)", (uuid.uuid4().hex, task.id, '2026-01-01'))
        runner.enqueue(task.id, transient_source_url='https://www.douyin.com/video/123')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            analysis = next(s for s in current.steps if s.kind is StepKind.ANALYZE_COMMENTS)
            if analysis.status in ('completed', 'failed', 'blocked'): break
            time.sleep(.02)
        assert analysis.status == 'completed', (analysis.status, analysis.error_code)
        assert executed.count(StepKind.ANALYZE_COMMENTS) == 1
        with db.transaction() as connection:
            scope = json.loads(connection.execute("SELECT artifact_ids_json FROM consents WHERE task_id = ? AND step_kind = 'analyze_comments'", (task.id,)).fetchone()[0])
            note = connection.execute("SELECT id FROM artifacts WHERE task_id = ? AND kind = 'note'", (task.id,)).fetchone()[0]
            assert scope == [note]
            assert connection.execute("SELECT COUNT(*) FROM model_runs WHERE task_id = ? AND status = 'completed'", (task.id,)).fetchone()[0] == 1
    finally:
        runner.stop()


@pytest.mark.parametrize('previous_call', [False, True])
def test_retrying_partial_collection_unblocks_only_analysis_without_prior_model_call(tmp_path, monkeypatch, previous_call):
    import app.workbench.migration as migration
    import app.workbench.runner as module
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(migration, 'import_legacy_jobs', lambda *_: None)
    db = Database(tmp_path / 'workbench.sqlite3')
    config = AppConfig(db)
    config.set_archive_root(tmp_path / 'archive')
    runner = TaskRunner(db, config)
    executed = []
    class Handler:
        def __init__(self, kind): self.kind = kind
        def run(self, context):
            executed.append(self.kind)
            artifact_id = uuid.uuid4().hex
            path = context.archive_root / artifact_id
            path.write_bytes(b'test')
            with db.transaction(write=True) as connection:
                connection.execute("INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)", (artifact_id, context.task.id, context.step.id, 'note' if self.kind is StepKind.COMMENTS else 'test', str(path), hashlib.sha256(b'test').hexdigest(), json.dumps({'scope':'comments'}), '2026-01-01'))
            return StepResult('partial' if self.kind is StepKind.COMMENTS else 'completed', [artifact_id])
    runner._handlers[StepKind.COMMENTS] = Handler(StepKind.COMMENTS)
    runner._handlers[StepKind.ANALYZE_COMMENTS] = Handler(StepKind.ANALYZE_COMMENTS)
    try:
        _, task = runner.create_download_task('https://www.douyin.com/video/123', step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS])
        steps = {s.kind: s for s in task.steps}
        with db.transaction(write=True) as connection:
            connection.execute("UPDATE tasks SET status = 'partial' WHERE id = ?", (task.id,))
            connection.execute("UPDATE assets SET platform = 'douyin', source_id = '123' WHERE id = ?", (task.asset_id,))
            connection.execute("UPDATE task_steps SET status = 'completed' WHERE id = ?", (steps[StepKind.DOWNLOAD].id,))
            connection.execute("UPDATE task_steps SET status = 'partial' WHERE id = ?", (steps[StepKind.COMMENTS].id,))
            connection.execute("UPDATE task_steps SET status = 'blocked', error_code = 'comments_analysis_stale' WHERE id = ?", (steps[StepKind.ANALYZE_COMMENTS].id,))
            connection.execute("INSERT INTO consents (id, task_id, step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at) VALUES (?, ?, 'analyze_comments', 'zhipu', 'model', '[]', 1, ?)", (uuid.uuid4().hex, task.id, '2026-01-01'))
            if previous_call:
                connection.execute("INSERT INTO model_runs (id, task_id, step_id, provider_id, model_id, artifact_ids_json, usage_json, status, created_at) VALUES ('prior', ?, ?, 'zhipu', 'model', '[]', '{}', 'failed', '2026-01-01')", (task.id, steps[StepKind.ANALYZE_COMMENTS].id))
        runner.retry_step(task.id, steps[StepKind.COMMENTS].id)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            analysis = next(s for s in current.steps if s.kind is StepKind.ANALYZE_COMMENTS)
            if analysis.status == 'completed' or (current.status == 'partial' and StepKind.COMMENTS in executed): break
            time.sleep(.02)
        assert analysis.status == ('blocked' if previous_call else 'completed'), (analysis.status, analysis.error_code)
        assert executed.count(StepKind.COMMENTS) == 1
        assert executed.count(StepKind.ANALYZE_COMMENTS) == (0 if previous_call else 1)
    finally:
        runner.stop()
