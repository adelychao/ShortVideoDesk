from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench.models import StepKind
from app.workbench.runner import AwaitingAuthorization, RunnerError, StepResult, TaskRunner


def test_independent_steps_run_concurrently_after_media_and_failure_is_isolated(tmp_path, monkeypatch):
    import app.workbench.runner as runner_module
    import app.workbench.migration as migration_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    speech_started = threading.Event()
    comment_started = threading.Event()
    should_fail = [True]
    index_runs = []

    class Handler:
        def __init__(self, kind):
            self.kind = kind

        def run(self, context):
            if self.kind == StepKind.COMMENTS:
                comment_started.set()
                assert speech_started.wait(3), "speech did not run alongside comments"
                if should_fail[0]:
                    raise RunnerError("comment_failure", "评论采集失败")
            if self.kind == StepKind.TRANSCRIBE:
                speech_started.set()
                assert comment_started.wait(3), "comments did not run alongside speech"
            if self.kind == StepKind.ANALYZE_COMMENTS and should_fail[0]:
                raise AssertionError("comment analysis ran without comments")
            if self.kind == StepKind.INDEX:
                index_runs.append(1)
            artifact_id = uuid.uuid4().hex
            with db.transaction(write=True) as connection:
                if self.kind == StepKind.DOWNLOAD:
                    connection.execute("UPDATE assets SET platform = 'douyin', source_id = '123' WHERE id = ?", (context.task.asset_id,))
                connection.execute(
                    "INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, context.task.id, context.step.id, "test_" + artifact_id, str(tmp_path / artifact_id), "0" * 64, "{}", "2026-01-01T00:00:00+00:00"),
                )
            return StepResult(status="completed", artifact_ids=[artifact_id])

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE, StepKind.ANALYZE_COMMENTS, StepKind.INDEX):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task(
            "https://www.douyin.com/video/123",
            step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS, StepKind.TRANSCRIBE, StepKind.INDEX],
        )
        steps = {step.kind: step for step in task.steps}
        assert steps[StepKind.TRANSCRIBE].depends_on == [steps[StepKind.DOWNLOAD].id]
        assert steps[StepKind.ANALYZE_COMMENTS].depends_on == [steps[StepKind.COMMENTS].id]
        runner.enqueue(task.id, transient_source_url="https://www.douyin.com/video/123")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and current.status in {"partial", "blocked", "failed", "completed"}:
                break
            time.sleep(0.02)
        assert current is not None
        steps = {step.kind: step for step in current.steps}
        assert steps[StepKind.TRANSCRIBE].status == "completed"
        assert steps[StepKind.COMMENTS].status == "blocked"
        assert steps[StepKind.ANALYZE_COMMENTS].status != "completed"
        assert current.status == "partial"
        assert index_runs == [1]
        should_fail[0] = False
        while task.id in runner._queued_ids and time.monotonic() < deadline:
            time.sleep(0.02)
        runner.retry_step(task.id, steps[StepKind.COMMENTS].id)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and current.status == "completed":
                break
            time.sleep(0.02)
        assert current is not None and current.status == "completed", [(s.kind, s.status, s.message) for s in current.steps]
        assert index_runs == [1, 1]
    finally:
        runner.stop()


def test_waiting_for_ocr_authorization_does_not_stop_comments(tmp_path, monkeypatch):
    import app.workbench.migration as migration_module
    import app.workbench.runner as runner_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    ocr_started = threading.Event()
    comments_started = threading.Event()

    class Handler:
        def __init__(self, kind):
            self.kind = kind

        def run(self, context):
            if self.kind is StepKind.OCR:
                ocr_started.set()
                assert comments_started.wait(3)
                raise AwaitingAuthorization("等待图片授权")
            if self.kind is StepKind.COMMENTS:
                comments_started.set()
                assert ocr_started.wait(3)
            artifact_id = uuid.uuid4().hex
            with db.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, context.task.id, context.step.id, "test_" + artifact_id, str(tmp_path / artifact_id), "0" * 64, "{}", "2026-01-01T00:00:00+00:00"),
                )
            return StepResult(status="completed", artifact_ids=[artifact_id])

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.OCR):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task("https://www.douyin.com/video/123", step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.OCR])
        runner.enqueue(task.id, transient_source_url="https://www.douyin.com/video/123")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and current.status == "awaiting_authorization":
                break
            time.sleep(0.02)
        assert current is not None and current.status == "awaiting_authorization"
        steps = {step.kind: step for step in current.steps}
        assert steps[StepKind.COMMENTS].status == "completed"
        assert steps[StepKind.OCR].status == "awaiting_authorization"
    finally:
        runner.stop()


def test_blocked_comments_can_be_manually_retried_while_peer_step_is_running(tmp_path, monkeypatch):
    import app.workbench.migration as migration_module
    import app.workbench.runner as runner_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    comments_failed = threading.Event()
    transcript_started = threading.Event()
    release_transcript = threading.Event()
    comment_attempts = []

    def save_artifact(context):
        artifact_id = uuid.uuid4().hex
        with db.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES (?,?,?,?,?,?,1,'{}','now')",
                (artifact_id, context.task.id, context.step.id, "test_" + artifact_id, str(tmp_path / artifact_id), "0" * 64),
            )
        return StepResult(status="completed", artifact_ids=[artifact_id])

    class Handler:
        def __init__(self, kind):
            self.kind = kind
        def run(self, context):
            if self.kind is StepKind.DOWNLOAD:
                with db.transaction(write=True) as connection:
                    connection.execute("UPDATE assets SET platform='douyin', source_id='123' WHERE id=?", (context.task.asset_id,))
                return save_artifact(context)
            if self.kind is StepKind.TRANSCRIBE:
                transcript_started.set()
                assert release_transcript.wait(5)
                return save_artifact(context)
            comment_attempts.append(1)
            if len(comment_attempts) == 1:
                assert transcript_started.wait(3)
                comments_failed.set()
                raise RunnerError("comments_access_limited", "评论暂时不可访问")
            return save_artifact(context)

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task(
            "https://www.douyin.com/video/123",
            step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE],
        )
        comment_step = next(step for step in task.steps if step.kind is StepKind.COMMENTS)
        runner.enqueue(task.id, transient_source_url="https://www.douyin.com/video/123")
        assert comments_failed.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            current_comment = next(step for step in current.steps if step.kind is StepKind.COMMENTS)
            if current_comment.status == "blocked":
                break
            time.sleep(0.02)
        assert current_comment.status == "blocked"
        runner.retry_step(task.id, comment_step.id)
        release_transcript.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and current.status == "completed":
                break
            time.sleep(0.02)
        assert current is not None and current.status == "completed"
        assert len(comment_attempts) == 2
    finally:
        release_transcript.set()
        runner.stop()


def test_two_tasks_can_run_at_the_same_time(tmp_path, monkeypatch):
    import app.workbench.migration as migration_module
    import app.workbench.runner as runner_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    barrier = threading.Barrier(2, timeout=3)

    class Handler:
        def run(self, context):
            barrier.wait()
            artifact_id = uuid.uuid4().hex
            with db.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, context.task.id, context.step.id, "test_" + artifact_id, str(tmp_path / artifact_id), "0" * 64, "{}", "2026-01-01T00:00:00+00:00"),
                )
            return StepResult(status="completed", artifact_ids=[artifact_id])

    runner._handlers[StepKind.DOWNLOAD] = Handler()
    try:
        _, first = runner.create_download_task("https://www.douyin.com/video/123")
        _, second = runner.create_download_task("https://www.douyin.com/video/456")
        runner.enqueue(first.id, transient_source_url="https://www.douyin.com/video/123")
        runner.enqueue(second.id, transient_source_url="https://www.douyin.com/video/456")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            states = [runner.repository.get_task(task.id).status for task in (first, second)]
            if all(status in {"completed", "failed", "blocked"} for status in states):
                break
            time.sleep(0.02)
        assert states == ["completed", "completed"]
    finally:
        runner.stop()


def test_cancel_marks_all_running_steps_and_does_not_resurrect_task(tmp_path, monkeypatch):
    import app.workbench.migration as migration_module
    import app.workbench.runner as runner_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    both_started = threading.Barrier(3, timeout=3)
    release = threading.Event()

    class Handler:
        def __init__(self, kind):
            self.kind = kind

        def run(self, context):
            if self.kind is not StepKind.DOWNLOAD:
                both_started.wait()
                assert release.wait(3)
            artifact_id = uuid.uuid4().hex
            with db.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, context.task.id, context.step.id, "test_" + artifact_id, str(tmp_path / artifact_id), "0" * 64, "{}", "2026-01-01T00:00:00+00:00"),
                )
            return StepResult(status="completed", artifact_ids=[artifact_id])

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task("https://www.douyin.com/video/123", step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.TRANSCRIBE])
        runner.enqueue(task.id, transient_source_url="https://www.douyin.com/video/123")
        both_started.wait()
        runner.cancel(task.id)
        release.set()
        deadline = time.monotonic() + 8
        while task.id in runner._queued_ids and time.monotonic() < deadline:
            time.sleep(0.02)
        current = runner.repository.get_task(task.id)
        assert current is not None and current.status == "cancelled"
        steps = {step.kind: step for step in current.steps}
        assert steps[StepKind.DOWNLOAD].status == "completed"
        assert steps[StepKind.COMMENTS].status == "cancelled"
        assert steps[StepKind.TRANSCRIBE].status == "cancelled"
    finally:
        release.set()
        runner.stop()


def test_partial_comments_require_explicit_authorization_before_analysis(tmp_path, monkeypatch):
    import app.workbench.migration as migration_module
    import app.workbench.runner as runner_module

    monkeypatch.setattr(runner_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_module, "import_legacy_jobs", lambda *_: None)
    db = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(db)
    config.set_archive_root(tmp_path / "archive")
    runner = TaskRunner(db, config)
    sent = []

    class Handler:
        def __init__(self, kind):
            self.kind = kind

        def run(self, context):
            if self.kind is StepKind.ANALYZE_COMMENTS:
                sent.append(True)
            artifact_id = uuid.uuid4().hex
            path = context.archive_root / artifact_id
            path.write_bytes(b"comments")
            with db.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (artifact_id, context.task.id, context.step.id, "note" if self.kind is StepKind.COMMENTS else "test_" + artifact_id, str(path), hashlib.sha256(b"comments").hexdigest(), '{"scope":"comments"}', "2026-01-01T00:00:00+00:00"),
                )
            return StepResult(status="partial" if self.kind is StepKind.COMMENTS else "completed", artifact_ids=[artifact_id])

    for kind in (StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS, StepKind.TRANSCRIBE):
        runner._handlers[kind] = Handler(kind)
    try:
        _, task = runner.create_download_task("https://www.douyin.com/video/123", step_kinds=[StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS, StepKind.TRANSCRIBE])
        runner.enqueue(task.id, transient_source_url="https://www.douyin.com/video/123")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            if current and current.status == "partial":
                break
            time.sleep(0.02)
        assert current is not None and current.status == "partial"
        steps = {step.kind: step for step in current.steps}
        assert steps[StepKind.TRANSCRIBE].status == "completed"
        assert steps[StepKind.ANALYZE_COMMENTS].error_code == "comments_analysis_stale"
        assert sent == []
        while task.id in runner._queued_ids and time.monotonic() < deadline:
            time.sleep(0.02)
        with db.transaction(write=True) as connection:
            note = connection.execute("SELECT id FROM artifacts WHERE task_id = ? AND kind = 'note'", (task.id,)).fetchone()
            connection.execute(
                "INSERT INTO consents (id, task_id, step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at) VALUES (?, ?, 'analyze_comments', 'zhipu', 'test', ?, 1, ?)",
                (uuid.uuid4().hex, task.id, json.dumps([note["id"]]), "2026-01-01T00:00:00+00:00"),
            )
        runner.retry_step(task.id, steps[StepKind.ANALYZE_COMMENTS].id)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = runner.repository.get_task(task.id)
            analysis = next(step for step in current.steps if step.kind is StepKind.ANALYZE_COMMENTS)
            if analysis.status == "completed":
                break
            time.sleep(0.02)
        assert analysis.status == "completed"
        assert sent == [True]
    finally:
        runner.stop()
