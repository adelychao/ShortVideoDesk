"""Cloud OCR for deterministic local video keyframes."""

from __future__ import annotations

import html
import json
import hashlib
import math
from pathlib import Path

from ..models import StepStatus
from ..archive import _assert_contained
from ..repository import _artifact_from_row
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled, TaskStepSkipped
from .artifact_store import register_bytes, registered_media
from .media_processing import extract_ocr_frames, private_tempdir, probe_media
from .model_call import authorized_model_call, configured_model_id, configured_provider_id
from .reconcile_transcript import count_copy_words, reconcile_transcript, render_review_markdown


def ocr_media(context: StepContext) -> StepResult:
    if context.cancel_event.is_set():
        raise TaskCancelled
    if context.skip_event is not None and context.skip_event.is_set():
        raise TaskStepSkipped
    model_id = configured_model_id(context)
    provider_id = configured_provider_id(context)
    media = registered_media(context)
    path = Path(media.path)
    probe = probe_media(path)
    if not probe.has_video:
        raise RunnerError("video_stream_missing", "字幕 OCR 需要包含视频画面的素材。", status_code=422)

    media_duration = getattr(probe, "video_duration_seconds", None) or probe.duration_seconds
    expected_count = min(8, max(1, math.ceil(media_duration / 30.0)))
    saved_consent_ids = _saved_ocr_consent_ids(context)
    frame_artifacts = _existing_frames(context, media.id, media.sha256, saved_consent_ids)
    if saved_consent_ids and len(frame_artifacts) != len(saved_consent_ids):
        raise RunnerError(
            "consent_scope_changed",
            "原授权绑定的关键帧已不可用；请新建任务并重新确认材料范围。",
        )
    if not saved_consent_ids and len(frame_artifacts) != expected_count:
        context.progress(context.step.id, 2, "云端 OCR 正在本机准备关键帧")
        frame_artifacts = []
        with private_tempdir("ocr") as temp_path:
            frames = extract_ocr_frames(path, Path(temp_path), media_duration)
            for frame in frames:
                if context.cancel_event.is_set():
                    raise TaskCancelled
                if context.skip_event is not None and context.skip_event.is_set():
                    raise TaskStepSkipped
                frame_artifacts.append(register_bytes(
                    context,
                    "ocr_frame",
                    ".jpg",
                    frame.path.read_bytes(),
                    {
                        "media_type": "image/jpeg",
                        "frame_id": frame.frame_id,
                        "sampled_at_seconds": round(frame.sampled_at_seconds, 3),
                        "sampled_at_is_approximate": True,
                        "source_artifact_id": media.id,
                        "source_sha256": media.sha256,
                    },
                ))

    input_ids = [artifact.id for artifact in frame_artifacts]
    frame_ids = [str(artifact.metadata["frame_id"]) for artifact in frame_artifacts]
    frame_times = {
        frame_id: float(artifact.metadata["sampled_at_seconds"])
        for frame_id, artifact in zip(frame_ids, frame_artifacts)
    }
    context.progress(
        context.step.id,
        None,
        f"云端 OCR 正在批量识别 {len(frame_artifacts)} 张关键帧；服务商不提供逐张进度，剩余时间暂不可估算",
    )
    response = authorized_model_call(
        context,
        model_id=model_id,
        input_artifact_ids=input_ids,
        input_detail={
            "frame_ids": frame_ids,
            "frame_count": len(frame_ids),
            "sampled_at_seconds": frame_times,
            "sampling": "uniform_midpoints_up_to_eight",
            "source_artifact_id": media.id,
        },
        operation=lambda provider: provider.analyze_images(  # type: ignore[attr-defined]
            "逐帧提取画面中实际可见的字幕或文字；没有文字则返回空内容。保留原文，不要推断、翻译或补写。",
            [Path(artifact.path) for artifact in frame_artifacts],
            frame_ids,
            model_id,
        ),
    )
    if context.cancel_event.is_set():
        raise TaskCancelled
    if context.skip_event is not None and context.skip_event.is_set():
        raise TaskStepSkipped

    results = [
        {
            "frame_id": frame.frame_id,
            "artifact_id": next(
                artifact.id for artifact in frame_artifacts
                if artifact.metadata.get("frame_id") == frame.frame_id
            ),
            "sampled_at_seconds": frame_times[frame.frame_id],
            "text": frame.text,
        }
        for frame in response.frames  # type: ignore[attr-defined]
    ]
    raw_payload = {
        "schema_version": 1,
        "provider_id": provider_id,
        "model_id": model_id,
        "source_artifact_id": media.id,
        "input_frame_artifact_ids": input_ids,
        "request_id": response.request_id,  # type: ignore[attr-defined]
        "raw_response_text": getattr(response, "raw_text", None),
        "frames": results,
        "sampling_note": "关键帧由本机按时长均匀抽样；时间点为近似采样位置。",
    }
    raw_artifact = register_bytes(
        context,
        "ocr_raw",
        ".json",
        _json_bytes(raw_payload),
        {"media_type": "application/json", "source_artifact_id": media.id, "frame_count": len(results)},
    )
    lines = [
        "# 关键帧字幕 OCR",
        "",
        f"- 服务商：{provider_id} / {model_id}",
        f"- 输入关键帧：{len(results)} 张（本机均匀抽样，最多 8 张）",
        "- 采样时间为近似位置；OCR 结果未经人工校对。",
        "",
    ]
    for item in results:
        lines.extend([
            f"**{item['frame_id']} · 约 {item['sampled_at_seconds']:.1f} 秒**",
            "",
            html.escape(item["text"]).replace("`", "&#96;") or "（未识别到可见文字）",
            "",
        ])
    result_artifact = register_bytes(
        context,
        "ocr_result",
        ".md",
        "\n".join(lines).encode("utf-8"),
        {
            "media_type": "text/markdown",
            "source_artifact_id": media.id,
            "input_frame_artifact_ids": input_ids,
            "raw_response_artifact_id": raw_artifact.id,
            "frame_count": len(results),
            "model_id": model_id,
        },
    )
    asr_segments, asr_artifact_ids = _latest_asr_segments(context, media.id, media.sha256)
    asr_text = "\n".join(_asr_chunk_text(segment) for segment in asr_segments if _asr_chunk_text(segment))
    draft = reconcile_transcript(
        asr_text,
        ocr_frames=results,
        source_artifact_ids=[
            media.id,
            *asr_artifact_ids,
            *input_ids,
            raw_artifact.id,
            result_artifact.id,
        ],
        asr_segments=asr_segments,
    )
    draft_artifact = register_bytes(
        context,
        "transcript_draft",
        ".md",
        render_review_markdown(draft, ocr_frames=results),
        {
            "media_type": "text/markdown",
            "source_artifact_ids": draft.source_artifact_ids,
            "uncertain_terms": draft.uncertain_terms,
            "word_count": count_copy_words(draft.text),
            "word_count_rule": "Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded",
            "human_review_required": True,
            "ocr_is_separate_evidence": True,
            "ocr_guided_corrections": draft.corrections,
            "ocr_correction_count": len(draft.corrections),
        },
    )
    context.progress(context.step.id, 100, "OCR 结果已保存，仍需人工校对")
    return StepResult(
        status=StepStatus.COMPLETED.value,
        artifact_ids=[*input_ids, raw_artifact.id, result_artifact.id, draft_artifact.id],
        metadata={"result": {
            "ocr_artifact_id": result_artifact.id,
            "raw_artifact_id": raw_artifact.id,
            "draft_artifact_id": draft_artifact.id,
            "frame_artifact_ids": input_ids,
            "frame_count": len(results),
            "requires_manual_review": True,
            "review_word_count": count_copy_words(draft.text),
        }},
    )


def _asr_chunk_text(chunk: object) -> str:
    if not isinstance(chunk, dict):
        return ''
    for key in ('normalized_text', 'raw_response_text', 'text'):
        if isinstance(chunk.get(key), str):
            return chunk[key]
    return ''


def _latest_asr_text(context: StepContext, media_id: str, media_sha256: str) -> tuple[str, list[str]]:
    segments, ids = _latest_asr_segments(context, media_id, media_sha256)
    text = "\n".join(value for segment in segments if (value := _asr_chunk_text(segment)))
    return text, ids


def _latest_asr_segments(context: StepContext, media_id: str, media_sha256: str) -> tuple[list[dict], list[str]]:
    with context.repository.database.transaction() as connection:
        row = connection.execute(
            """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'transcript_raw'
               ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
            (context.task.id,),
        ).fetchone()
        transcript_row = connection.execute(
            """SELECT id FROM artifacts WHERE task_id = ? AND kind = 'transcript'
               ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
            (context.task.id,),
        ).fetchone()
    if row is None or context.archive_root is None:
        return [], []
    try:
        artifact = _artifact_from_row(row)
        if artifact.metadata.get("source_artifact_id") != media_id:
            return [], []
        source_sha = artifact.metadata.get("source_sha256")
        if not isinstance(source_sha, str) or source_sha.casefold() != media_sha256.casefold():
            return [], []
        registered = Path(artifact.path)
        if registered.is_symlink():
            return [], []
        path = _assert_contained(registered, context.archive_root, strict=True)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            return [], []
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest().casefold() != artifact.sha256.casefold():
            return "", []
        payload = json.loads(data.decode("utf-8"))
        chunks = payload.get("chunks") if isinstance(payload, dict) else None
        if not isinstance(chunks, list):
            return [], []
        segments = []
        for chunk in chunks:
            value = _asr_chunk_text(chunk)
            if not value:
                continue
            segment = {"text": value}
            try:
                start = float(chunk.get("start_seconds")) if isinstance(chunk, dict) else None
                end = float(chunk.get("end_seconds")) if isinstance(chunk, dict) else None
            except (TypeError, ValueError, OverflowError):
                start = end = None
            if start is not None and end is not None and math.isfinite(start) and math.isfinite(end):
                segment["start_seconds"] = start
                segment["end_seconds"] = end
            segments.append(segment)
        ids = [artifact.id]
        if transcript_row is not None:
            ids.append(str(transcript_row["id"]))
        return segments, ids
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return [], []


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _saved_ocr_consent_ids(context: StepContext) -> list[str]:
    with context.repository.database.transaction() as connection:
        row = connection.execute(
            "SELECT artifact_ids_json FROM consents WHERE task_id = ? AND step_kind = 'ocr' AND authorized = 1",
            (context.task.id,),
        ).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row["artifact_ids_json"])
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _existing_frames(
    context: StepContext,
    source_id: str,
    source_sha256: str,
    exact_ids: list[str],
):
    with context.repository.database.transaction() as connection:
        if exact_ids:
            placeholders = ",".join("?" for _ in exact_ids)
            rows = connection.execute(
                f"SELECT * FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'ocr_frame' AND id IN ({placeholders})",
                (context.task.id, context.step.id, *exact_ids),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'ocr_frame'
                   ORDER BY created_at, version, id""",
                (context.task.id, context.step.id),
            ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    if exact_ids:
        ordered_rows = [by_id[artifact_id] for artifact_id in exact_ids if artifact_id in by_id]
    else:
        ordered_rows = rows
    artifacts = []
    seen_frames: set[str] = set()
    for row in ordered_rows:
        artifact = _artifact_from_row(row)
        frame_id = artifact.metadata.get("frame_id")
        if (
            artifact.metadata.get("source_artifact_id") != source_id
            or artifact.metadata.get("source_sha256") != source_sha256
            or not isinstance(frame_id, str)
            or frame_id in seen_frames
        ):
            continue
        try:
            path = _assert_contained(Path(artifact.path), context.archive_root, strict=True)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest().casefold() != artifact.sha256.casefold():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        seen_frames.add(frame_id)
        artifacts.append(artifact)
    return artifacts


__all__ = ["ocr_media"]
