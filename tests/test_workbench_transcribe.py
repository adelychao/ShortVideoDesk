from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.workbench.runner import RunnerError
from app.workbench.steps import transcribe
from app.workbench.steps.media_processing import AudioChunk, MediaProbe


def test_transcription_uses_temporary_chunks_and_identifies_failed_chunk(monkeypatch, tmp_path):
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"media")
    media = SimpleNamespace(id="media-id", path=str(media_path), sha256="source-sha")
    context = SimpleNamespace(cancel_event=threading.Event(), step=SimpleNamespace(id="step"), progress=lambda *_: None,
                              app_config=SimpleNamespace(get_settings=lambda: {"model_provider": "zhipu", "asr_model_id": "glm-asr-2512"}))
    monkeypatch.setattr(transcribe, "registered_media", lambda _: media)
    monkeypatch.setattr(transcribe, "probe_media", lambda _: MediaProbe(56, True, True))
    temporary_paths = []

    def chunks(_path, directory, _duration):
        for index in (1, 2):
            path = directory / f"audio-{index}.wav"
            path.write_bytes(b"audio-" + str(index).encode())
            temporary_paths.append(path)
            yield AudioChunk(path, (index - 1) * 28, index * 28, index)

    calls = []
    def model_call(_context, **kwargs):
        calls.append(kwargs)
        assert kwargs["input_artifact_ids"] == ["media-id"]
        assert kwargs["input_detail"]["chunk_sha256"] == hashlib.sha256(b"audio-1").hexdigest()
        assert kwargs["operation"](SimpleNamespace(transcribe_audio=lambda path, _: path.read_bytes())) == b"audio-1"
        raise RunnerError("rate_limited", "模型服务暂时达到请求限制，请稍后再试。")

    monkeypatch.setattr(transcribe, "extract_audio_chunks", chunks)
    monkeypatch.setattr(transcribe, "authorized_model_call", model_call)
    monkeypatch.setattr(transcribe, "register_bytes", lambda *_args, **_kwargs: pytest.fail("temporary audio must not be archived"))
    with pytest.raises(RunnerError, match="第 1/2 个音频片段") as failure:
        transcribe.transcribe_media(context)
    assert failure.value.code == "rate_limited"
    assert len(calls) == 1
    assert len(temporary_paths) == 1
    assert all(not path.exists() for path in temporary_paths)


def test_failed_model_run_preserves_input_detail(tmp_path):
    import json
    import sqlite3
    from contextlib import contextmanager
    from app.workbench.steps.model_call import _finish_run

    class Database:
        @contextmanager
        def transaction(self, write=False):
            connection = sqlite3.connect(tmp_path / "test.sqlite3")
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

    database = Database()
    with database.transaction(write=True) as connection:
        connection.execute("""CREATE TABLE model_runs (
            id TEXT PRIMARY KEY, task_id TEXT, step_id TEXT, provider_id TEXT, model_id TEXT,
            artifact_ids_json TEXT, usage_json TEXT, cost_json TEXT, status TEXT, created_at TEXT,
            request_id TEXT, error_code TEXT, finished_at TEXT
        )""")
        connection.execute(
            """INSERT INTO model_runs (id, task_id, step_id, provider_id, model_id,
                   artifact_ids_json, usage_json, cost_json, status, created_at)
               VALUES ('run', 'task', 'step', 'zhipu', 'asr', '[]', ?, '{}', 'running', 'now')""",
            (json.dumps({"input_detail": {"chunk_index": 4, "chunk_count": 15}}),),
        )
    _finish_run(database, "run", status="failed", error_code="rate_limited")
    with database.transaction() as connection:
        run = connection.execute("SELECT usage_json, error_code FROM model_runs WHERE id = 'run'").fetchone()
    assert json.loads(run["usage_json"])["input_detail"]["chunk_index"] == 4
    assert run["error_code"] == "rate_limited"


def test_successful_transcription_retains_only_text_outputs(monkeypatch, tmp_path):
    import json
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"media")
    media = SimpleNamespace(id="media-id", path=str(media_path), sha256="source-sha")
    context = SimpleNamespace(cancel_event=threading.Event(), step=SimpleNamespace(id="step"), progress=lambda *_: None,
                              app_config=SimpleNamespace(get_settings=lambda: {"model_provider": "zhipu", "asr_model_id": "glm-asr-2512"}))
    monkeypatch.setattr(transcribe, "registered_media", lambda _: media)
    monkeypatch.setattr(transcribe, "probe_media", lambda _: MediaProbe(56, True, True))
    paths = []
    def chunks(_path, directory, _duration):
        for index in (1, 2):
            path = directory / f"audio-{index}.wav"
            path.write_bytes(f"audio-{index}".encode())
            paths.append(path)
            yield AudioChunk(path, (index - 1) * 28, index * 28, index)
    monkeypatch.setattr(transcribe, "extract_audio_chunks", chunks)
    calls = []
    def model_call(_context, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=f"text {len(calls)}", request_id=f"request-{len(calls)}")
    monkeypatch.setattr(transcribe, "authorized_model_call", model_call)
    outputs = {}
    def register(_context, kind, _extension, content, _metadata):
        outputs[kind] = content
        return SimpleNamespace(id=kind)
    monkeypatch.setattr(transcribe, "register_bytes", register)

    result = transcribe.transcribe_media(context)
    assert result.artifact_ids == ["transcript_raw", "transcript", "transcript_draft"]
    assert len(calls) == 2
    assert all(not path.exists() for path in paths)
    raw = json.loads(outputs["transcript_raw"])
    assert [c["chunk_index"] for c in raw["chunks"]] == [1, 2]
    assert all(c["source_artifact_id"] == "media-id" and len(c["chunk_sha256"]) == 64 for c in raw["chunks"])
