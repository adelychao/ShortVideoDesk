from types import SimpleNamespace
from pathlib import Path
import threading

from app.workbench.steps import analyze_structure as structure


def test_structure_retains_unverified_items_without_discarding_verified(monkeypatch):
    context = SimpleNamespace(cancel_event=threading.Event(), step=SimpleNamespace(id="step"), progress=lambda *_: None)
    monkeypatch.setattr(structure, "configured_model_id", lambda _: "model")
    monkeypatch.setattr(structure, "text_inputs", lambda *_, **__: [SimpleNamespace(id="text")])
    monkeypatch.setattr(structure, "read_artifact_text", lambda *_: "这是可核对的原文。")
    monkeypatch.setattr(structure, "register_bytes", lambda *_, **__: SimpleNamespace(id="raw"))
    monkeypatch.setattr(structure, "authorized_model_call", lambda *_, **__: SimpleNamespace(text='{"sections":[{"kind":"开头","present":true,"source_excerpt":"可核对的原文","analysis":"说明"},{"kind":"观点","present":true,"source_excerpt":"不存在的引用","analysis":"观点"}]}'))
    result = structure.analyze_structure(context, ["text"])
    assert [item.verified for item in result.sections] == [True, False]
    assert result.sections[1].source_excerpt == "不存在的引用"


def test_new_task_does_not_offer_structure_checkbox():
    from pathlib import Path
    selector = Path("frontend/src/components/StepSelector.vue").read_text(encoding="utf-8")
    assert "title: '拆解内容结构'" not in selector


def test_followup_runs_only_selected_step_on_partially_blocked_task(tmp_path, monkeypatch):
    import hashlib
    import json
    import time
    import app.workbench.runner as runner_module
    import app.workbench.migration as migration_module
    from app.workbench.config import AppConfig
    from app.workbench.database import Database
    from app.workbench.models import StepKind
    from app.workbench.runner import StepResult, TaskRunner
    from app.workbench.runner import RunnerError
    import pytest

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "data.sqlite")
    config = AppConfig(db)
    root = config.set_archive_root(tmp_path / "archive")
    config.update_settings({"model_provider": "zhipu", "model_id": "glm-5.3-flash"})
    runner = TaskRunner(db, config)
    calls = []

    class AnalysisHandler:
        def run(self, context):
            calls.append(context.step.input_artifact_ids)
            with db.transaction(write=True) as connection:
                connection.execute("INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES ('analysis-test', ?, ?, 'analysis', ?, ?, 1, '{}', ?)", (context.task.id, context.step.id, str(file), digest, "2026-01-01T00:00:00+00:00"))
            return StepResult("completed", ["analysis-test"])

    runner._handlers[StepKind.ANALYZE_STRUCTURE] = AnalysisHandler()
    try:
        _, task = runner.create_download_task("https://www.douyin.com/video/123", step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE])
        steps = {step.kind: step for step in task.steps}
        file = root / "transcript.md"
        file.write_text("正文", encoding="utf-8")
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        with db.transaction(write=True) as connection:
            connection.execute("UPDATE tasks SET status = 'partial' WHERE id = ?", (task.id,))
            connection.execute("UPDATE task_steps SET status = 'completed' WHERE id IN (?, ?)", (steps[StepKind.DOWNLOAD].id, steps[StepKind.TRANSCRIBE].id))
            connection.execute("UPDATE task_steps SET status = 'blocked' WHERE id = ?", (steps[StepKind.COMMENTS].id,))
            connection.execute("INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, 'transcript', ?, ?, 1, '{}', ?)", ("text", task.id, steps[StepKind.TRANSCRIBE].id, str(file), digest, "2026-01-01T00:00:00+00:00"))
        with pytest.raises(RunnerError, match="不属于本次任务"):
            runner.enqueue_followup_structure(task.id, transcript_artifact_id="other-task-text", ocr_artifact_id=None, provider_id="zhipu", model_id="glm-5.3-flash")
        step_id = runner.enqueue_followup_structure(task.id, transcript_artifact_id="text", ocr_artifact_id=None, provider_id="zhipu", model_id="glm-5.3-flash")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and next(s for s in current.steps if s.id == step_id).status == 'completed':
                break
            time.sleep(.02)
        assert calls == [["text"]]
        assert next(s for s in current.steps if s.id == step_id).status == 'completed'
        assert next(s for s in current.steps if s.kind == StepKind.COMMENTS).status == 'blocked'
        with db.transaction() as connection:
            row = connection.execute("SELECT artifact_ids_json FROM consents WHERE task_id = ? AND step_kind = 'analyze_structure'", (task.id,)).fetchone()
        assert json.loads(row[0]) == ['text']
    finally:
        runner.stop()


def test_followup_creates_partial_results_and_preserves_exact_ocr_scope(tmp_path, monkeypatch):
    import hashlib
    import json
    import time
    import app.workbench.runner as runner_module
    import app.workbench.migration as migration_module
    from app.workbench.steps import model_call
    from app.workbench.config import AppConfig
    from app.workbench.database import Database
    from app.workbench.models import StepKind
    from app.workbench.runner import TaskRunner

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner_module, "classify_failure", lambda exc: ("test_error", repr(exc)))
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    sent = []
    class Provider:
        def set_client_request_id(self, _): pass
        def analyze_text(self, prompt, text, model):
            sent.append(text)
            return SimpleNamespace(text=json.dumps({"sections": [{"kind":"开头", "present":True, "source_excerpt":"不存在", "analysis":"待核对"}]}), request_id="test")
    monkeypatch.setattr(model_call, "get_model_api_key", lambda: "test")
    monkeypatch.setattr(model_call, "create_provider", lambda *_, **__: Provider())
    db = Database(tmp_path / "data.sqlite")
    config = AppConfig(db)
    root = config.set_archive_root(tmp_path / "archive")
    config.update_settings({"model_provider": "zhipu", "model_id": "glm-5.3-flash"})
    runner = TaskRunner(db, config)
    try:
        _, task = runner.create_download_task("https://www.douyin.com/video/123", step_kinds=[StepKind.DOWNLOAD, StepKind.TRANSCRIBE, StepKind.OCR])
        steps = {s.kind: s for s in task.steps}
        folder = root / 'asset'
        folder.mkdir()
        with db.transaction(write=True) as conn:
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task.id,))
            conn.execute("UPDATE task_steps SET status = 'completed' WHERE task_id = ?", (task.id,))
            for artifact_id, kind, content, owner in [('video','video','media',StepKind.DOWNLOAD), ('text','transcript','语音原文',StepKind.TRANSCRIBE), ('ocr','ocr_result','画面文字',StepKind.OCR)]:
                path = folder / (artifact_id + ('.mp4' if kind == 'video' else '.md'))
                path.write_text(content, encoding='utf-8')
                conn.execute("INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, '{}', ?)", (artifact_id, task.id, steps[owner].id, kind, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), '2026-01-01'))
        step_id = runner.enqueue_followup_structure(task.id, transcript_artifact_id='text', ocr_artifact_id='ocr', provider_id='zhipu', model_id='glm-5.3-flash')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            step = next(s for s in current.steps if s.id == step_id)
            if step.status in {'partial', 'completed', 'failed', 'blocked'}: break
            time.sleep(.02)
        assert step.status == 'partial', (step.status, step.message)
        assert sent == ['语音原文', '画面文字']
        with db.transaction() as conn:
            result = conn.execute("SELECT path FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'structure_analysis_json'", (task.id, step_id)).fetchone()
            consent = conn.execute("SELECT artifact_ids_json FROM consents WHERE task_id = ? AND step_kind = 'analyze_structure'", (task.id,)).fetchone()
        assert json.loads(consent[0]) == ['text', 'ocr']
        assert all(not item['verified'] for item in json.loads(Path(result[0]).read_text(encoding='utf-8'))['sections'])
    finally:
        runner.stop()
