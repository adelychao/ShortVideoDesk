from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sys
import threading
from unittest.mock import Mock

import pytest

from app.workbench import api
from app.workbench.database import Database


def test_local_ocr_does_not_require_cloud_configuration(monkeypatch):
    runner = SimpleNamespace(config=SimpleNamespace(get_settings=lambda: {}))
    monkeypatch.setattr(api, "_model_configuration_ready", lambda *_a, **_kw: False)
    assert api._validate_step_consents(runner, ["download", "ocr"], [], ocr_mode="local") == []


def test_ocr_options_survive_database_restart(tmp_path: Path):
    from app.workbench.local_ocr import get_task_options, save_task_options
    from app.workbench.models import SourceKind, SourceRecord, StepKind, StepRecord
    from app.workbench.repository import TaskRepository

    db = Database(tmp_path / "data.sqlite3")
    db.migrate()
    _, task = TaskRepository(db).create_task(
        SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"),
        [StepRecord(id="s", kind=StepKind.DOWNLOAD, ordinal=0, status="queued")],
    )
    save_task_options(db, task.id, mode="local", interval=2.0)
    assert get_task_options(Database(db.path), task.id) == {"mode": "local", "interval": 2.0}
    assert get_task_options(db, "legacy") == {"mode": "cloud", "interval": 2.0}


def test_contiguous_duplicate_text_is_merged_but_later_reappearance_is_kept():
    from app.workbench.steps.local_ocr import merge_ocr_samples

    samples = [
        {"sampled_at_seconds": 1.0, "text": "第一句"},
        {"sampled_at_seconds": 3.0, "text": "第一句"},
        {"sampled_at_seconds": 5.0, "text": "第二句"},
        {"sampled_at_seconds": 7.0, "text": "第一句"},
    ]
    merged = merge_ocr_samples(samples, interval=2.0, duration=8.0)
    assert [item["text"] for item in merged] == ["第一句", "第二句", "第一句"]
    assert (merged[0]["start_seconds"], merged[0]["end_seconds"]) == (0.0, 4.0)


@pytest.mark.parametrize("interval", [0, -1, 0.1, 61])
def test_reject_invalid_sampling_interval(interval):
    from app.workbench.local_ocr import validate_interval

    with pytest.raises(ValueError):
        validate_interval(interval)


@pytest.mark.parametrize(("duration", "interval", "expected"), [
    (0.2, 2.0, 1),
    (4.0, 2.0, 2),
    (4.000001, 2.0, 3),
    (5.0, 3.0, 2),
    (7.0, 0.7, 10),
    (256.766667, 2.0, 129),
])
def test_sample_frame_count_handles_boundaries_without_float_drift(duration, interval, expected):
    from app.workbench.steps.media_processing import sample_frame_count

    assert sample_frame_count(duration, interval) == expected


def test_local_ocr_streams_full_duration_and_keeps_original_samples(tmp_path, monkeypatch):
    from app.workbench.steps import local_ocr as processor
    from app.workbench.steps.media_processing import OcrFrameFile

    frames = [OcrFrameFile(tmp_path / f'frame-{i}.jpg', f'f{i}', t, i) for i, t in enumerate([1.0, 3.0, 4.5], 1)]
    for frame in frames:
        frame.path.write_bytes(frame.frame_id.encode())
    observed = []
    texts = iter(['第一句', '第一句', '第二句'])
    class Engine:
        def __init__(self, **_kwargs):
            pass
        def __call__(self, image):
            observed.append(image)
            return SimpleNamespace(txts=[next(texts)])
    monkeypatch.setitem(sys.modules, 'rapidocr', SimpleNamespace(RapidOCR=Engine))
    monkeypatch.setattr(processor.local_ocr, 'status', lambda: {'installed': True, 'ready': True})
    monkeypatch.setattr(processor.local_ocr, 'model_paths', lambda: {})
    monkeypatch.setattr(processor, 'registered_media', lambda _: SimpleNamespace(id='media', path='video.mp4', sha256='hash'))
    monkeypatch.setattr(processor, 'probe_media', lambda _: SimpleNamespace(has_video=True, duration_seconds=5.0))
    monkeypatch.setattr(processor, 'iter_video_samples', lambda *_args, **_kwargs: iter(frames))
    monkeypatch.setattr(processor, '_load_checkpoints', lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(processor, '_save_checkpoint', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(processor, '_latest_asr_segments', lambda *_args: ([{'text': '口播'}], ['asr']))
    outputs = {}
    def register(_context, kind, _extension, content, metadata):
        outputs[kind] = (content, metadata)
        return SimpleNamespace(id=kind)
    monkeypatch.setattr(processor, 'register_bytes', register)
    context = SimpleNamespace(cancel_event=threading.Event(), skip_event=None, ocr_resume_mode='continue',
                              progress=Mock(), step=SimpleNamespace(id='ocr'))
    result = processor.ocr_local(context, {'interval': 2.0})
    assert result.status == 'completed'
    assert len(observed) == 3
    assert len(json.loads(outputs['ocr_raw'][0])['samples']) == 3
    markdown = outputs['ocr_result'][0].decode()
    assert '约 0.0–4.0 秒' in markdown and '约 4.0–5.0 秒' in markdown
    assert '**约 0.0–4.0 秒**' in markdown
    assert markdown.count('\n## ') == 0
    assert outputs['ocr_result'][1]['provider_id'] == 'local-rapidocr'


def test_local_ocr_persists_tail_frames_before_reraising_extraction_error(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from app.workbench.steps import local_ocr as processor
    from app.workbench.steps.media_processing import OcrFrameFile

    saved_indices = []
    frames = [OcrFrameFile(tmp_path / f"frame-{index}.jpg", f"f{index}", index * 2.0, index)
              for index in range(1, 4)]
    for frame in frames:
        frame.path.write_bytes(b"frame")

    def broken_frames(*_args, **_kwargs):
        yield from frames
        raise ValueError("ffmpeg 只生成了 128/129 张视频帧。")

    class Engine:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, _image):
            return SimpleNamespace(txts=["识别文本"])

    @contextmanager
    def tempdir(_prefix):
        yield tmp_path

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=Engine))
    monkeypatch.setattr(processor.local_ocr, "status", lambda: {"installed": True, "ready": True})
    monkeypatch.setattr(processor.local_ocr, "model_paths", lambda: {})
    monkeypatch.setattr(processor, "registered_media", lambda _: SimpleNamespace(id="media", path="video.mp4", sha256="hash"))
    monkeypatch.setattr(processor, "probe_media", lambda _: SimpleNamespace(
        has_video=True, duration_seconds=8.0, video_duration_seconds=6.0
    ))
    monkeypatch.setattr(processor, "iter_video_samples", broken_frames)
    monkeypatch.setattr(processor, "_load_checkpoints", lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(processor, "_save_checkpoint", lambda _context, _media, _interval, _profile, frame, *_rest:
                        saved_indices.append(frame.index) or True)
    monkeypatch.setattr(processor, "private_tempdir", tempdir)
    context = SimpleNamespace(
        cancel_event=threading.Event(), skip_event=None, progress=Mock(),
        step=SimpleNamespace(id="ocr"), ocr_resume_mode="continue",
    )

    with pytest.raises(ValueError, match="128/129"):
        processor.ocr_local(context, {"interval": 2.0})

    assert saved_indices == [1, 2, 3]


def test_local_ocr_uses_video_stream_duration(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from app.workbench.steps import local_ocr as processor
    from app.workbench.runner import RunnerError

    observed = {}

    class Engine:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, _image):
            return SimpleNamespace(txts=[])

    @contextmanager
    def tempdir(_prefix):
        yield tmp_path

    def samples(_path, _destination, duration, interval, **_kwargs):
        observed["duration"] = duration
        observed["interval"] = interval
        return iter(())

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=Engine))
    monkeypatch.setattr(processor.local_ocr, "status", lambda: {"installed": True, "ready": True})
    monkeypatch.setattr(processor.local_ocr, "model_paths", lambda: {})
    monkeypatch.setattr(processor, "registered_media", lambda _: SimpleNamespace(id="media", path="video.mp4", sha256="hash"))
    monkeypatch.setattr(processor, "probe_media", lambda _: SimpleNamespace(
        has_video=True, duration_seconds=10.0, video_duration_seconds=6.0
    ))
    monkeypatch.setattr(processor, "iter_video_samples", samples)
    monkeypatch.setattr(processor, "_load_checkpoints", lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(processor, "private_tempdir", tempdir)
    context = SimpleNamespace(
        cancel_event=threading.Event(), skip_event=None, progress=Mock(),
        step=SimpleNamespace(id="ocr"), ocr_resume_mode="continue",
    )

    with pytest.raises(RunnerError, match="未处理完全部画面"):
        processor.ocr_local(context, {"interval": 2.0})

    assert observed == {"duration": 6.0, "interval": 2.0}


def test_probe_media_keeps_audio_and_video_stream_durations(tmp_path, monkeypatch):
    from app.workbench.steps import media_processing

    response = {
        "format": {"duration": "10.0"},
        "streams": [
            {"codec_type": "audio", "duration": "10.0"},
            {"codec_type": "video", "duration_ts": 180, "time_base": "1/30"},
        ],
    }
    monkeypatch.setattr(media_processing.shutil, "which", lambda _name: "ffprobe")
    monkeypatch.setattr(media_processing.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(response)
    ))

    probe = media_processing.probe_media(tmp_path / "video.mp4")

    assert probe.duration_seconds == 10.0
    assert probe.audio_duration_seconds == 10.0
    assert probe.video_duration_seconds == 6.0


def test_ffmpeg_failure_message_does_not_blame_unrelated_chrome_dependency():
    from multi_sources import classify_failure

    code, message = classify_failure(ValueError("ffmpeg 只生成了 128/129 张视频帧。"))

    assert code == "local_dependency"
    assert "Chrome" not in message
    assert "视频" in message or "媒体" in message


def test_ffmpeg_missing_dependency_message_suggests_ffmpeg_install():
    from multi_sources import classify_failure

    _, message = classify_failure(ValueError("未检测到 ffmpeg。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。"))

    assert "安装" in message
    assert "Chrome" not in message


def test_video_sample_extractor_requests_last_frame_at_eof(tmp_path, monkeypatch):
    from app.workbench.steps import media_processing

    jpeg = b"\xff\xd8frame\xff\xd9"

    class Stdout:
        def __init__(self):
            self.parts = iter([jpeg * 3, b""])

        def read1(self, _size):
            return next(self.parts)

        def close(self):
            pass

    class Process:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.stdout = Stdout()
            self.returncode = 0

        def wait(self, **_kwargs):
            return 0

        def poll(self):
            return 0

    processes = []

    def popen(command, **kwargs):
        process = Process(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(media_processing.shutil, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(media_processing.subprocess, "Popen", popen)

    frames = list(media_processing.iter_video_samples(tmp_path / "video.mp4", tmp_path / "frames", 4.001, 2.0))

    assert len(frames) == 3
    command = processes[0].command
    filter_arg = command[command.index("-vf") + 1]
    assert "tpad=stop_mode=clone:stop_duration=2" in filter_arg
    assert "fps=1/2:start_time=0:round=near" in filter_arg
    assert command[command.index("-frames:v") + 1] == "3"


def test_missing_or_corrupt_model_is_not_reported_ready(tmp_path, monkeypatch):
    from app.workbench import local_ocr

    package = tmp_path / 'rapidocr'
    (package / 'models').mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    monkeypatch.setattr(local_ocr.importlib.util, 'find_spec', lambda name: SimpleNamespace(origin=str(package / '__init__.py')))
    assert local_ocr.status()['ready'] is False
    for filename, _ in local_ocr._MODELS.values():
        (package / 'models' / filename).write_bytes(b'not a valid model')
    assert local_ocr.status()['ready'] is False
