from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.workbench import api
from app.workbench.database import Database
from app.workbench.models import SourceKind, SourceRecord, StepKind, StepRecord
from app.workbench.runner import TaskRunner
from app.workbench.steps import ocr


def test_ocr_only_task_records_model_selection_without_extra_confirmation(monkeypatch):
    settings = {"model_provider": "zhipu", "model_id": "test-model"}
    runner = SimpleNamespace(config=SimpleNamespace(get_settings=lambda: settings))
    monkeypatch.setattr(api, "_model_configuration_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(api, "lookup_model_capability", lambda *_args: (None, None, None))
    records = api._validate_step_consents(runner, ["download", "ocr"], [])
    assert len(records) == 1
    assert (records[0].step_kind, records[0].provider_id, records[0].model_id) == ("ocr", "zhipu", "test-model")


def test_ocr_processes_existing_frames_without_individual_selection(monkeypatch, tmp_path):
    media = SimpleNamespace(id="media", path=str(tmp_path / "video.mp4"), sha256="hash")
    frame = SimpleNamespace(id="frame", path=str(tmp_path / "frame.jpg"), metadata={"frame_id": "f1", "sampled_at_seconds": 1.0})
    context = SimpleNamespace(cancel_event=threading.Event(), skip_event=None, progress=Mock(), step=SimpleNamespace(id="ocr"))
    monkeypatch.setattr(ocr, "configured_model_id", lambda _: "test-model")
    monkeypatch.setattr(ocr, "configured_provider_id", lambda _: "zhipu")
    monkeypatch.setattr(ocr, "registered_media", lambda _: media)
    monkeypatch.setattr(ocr, "probe_media", lambda _: SimpleNamespace(has_video=True, duration_seconds=20))
    monkeypatch.setattr(ocr, "_saved_ocr_consent_ids", lambda _: [])
    monkeypatch.setattr(ocr, "_existing_frames", lambda *_args: [frame])
    monkeypatch.setattr(ocr, "_latest_asr_segments", lambda *_args: ([], []))
    monkeypatch.setattr(ocr, "register_bytes", lambda _c, kind, *_args: SimpleNamespace(id=kind))
    call = Mock(return_value=SimpleNamespace(frames=[SimpleNamespace(frame_id="f1", text="内容")], request_id="r"))
    monkeypatch.setattr(ocr, "authorized_model_call", call)
    result = ocr.ocr_media(context)
    assert result.status == "completed"
    assert call.call_args.kwargs["input_artifact_ids"] == ["frame"]


def test_resuming_old_waiting_ocr_creates_missing_model_record(tmp_path: Path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.migrate()
    settings = {"model_provider": "zhipu", "model_id": "test-model"}
    runner = TaskRunner(db, SimpleNamespace(get_settings=lambda: settings))
    runner._started = True
    runner.require_archive_root = lambda: tmp_path
    _, task = runner.repository.create_task(
        SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"),
        [StepRecord(id="source", kind=StepKind.DOWNLOAD, ordinal=0, status="completed"),
         StepRecord(id="ocr", kind=StepKind.OCR, ordinal=1, status="awaiting_authorization", depends_on=["source"])],
    )
    with db.transaction(write=True) as connection:
        connection.execute("UPDATE tasks SET status = 'awaiting_authorization' WHERE id = ?", (task.id,))
    runner.retry_step(task.id, "ocr")
    with db.transaction() as connection:
        record = connection.execute("SELECT provider_id,model_id,authorized FROM consents WHERE task_id = ? AND step_kind = 'ocr'", (task.id,)).fetchone()
    assert record is not None
    assert tuple(record) == ("zhipu", "test-model", 1)
