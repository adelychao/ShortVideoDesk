"""Cloud ASR step with locally extracted bounded audio chunks."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

from ..models import StepKind, StepStatus
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled
from .artifact_store import register_bytes, registered_media
from .media_processing import ASR_CHUNK_SECONDS, extract_audio_chunks, private_tempdir, probe_media
from .model_call import authorized_model_call, configured_model_id, configured_provider_id
from .reconcile_transcript import count_copy_words, reconcile_transcript, render_review_markdown


def transcribe_media(context: StepContext) -> StepResult:
    if context.cancel_event.is_set():
        raise TaskCancelled
    model_id = configured_model_id(context, audio=True)
    provider_id = configured_provider_id(context)
    media = registered_media(context)
    path = Path(media.path)
    probe = probe_media(path)
    if not probe.has_audio:
        raise RunnerError("audio_track_missing", "所选媒体没有可转写的音轨。", status_code=422)

    chunks: list[dict[str, object]] = []
    raw_calls: list[dict[str, object]] = []
    total_chunks = max(1, math.ceil(probe.duration_seconds / ASR_CHUNK_SECONDS))
    context.progress(context.step.id, 1, f"媒体时长约 {probe.duration_seconds:.1f} 秒；将在本机切分音频")
    with private_tempdir("asr") as temp_path:
        for chunk in extract_audio_chunks(path, Path(temp_path), probe.duration_seconds):
            if context.cancel_event.is_set():
                raise TaskCancelled
            context.progress(
                context.step.id,
                min(94.0, (chunk.index - 1) / total_chunks * 90.0 + 2.0),
                f"正在转写第 {chunk.index}/{total_chunks} 个音频片段",
            )
            detail = {
                "chunk_index": chunk.index,
                "chunk_count": total_chunks,
                "start_seconds": round(chunk.start_seconds, 3),
                "end_seconds": round(chunk.end_seconds, 3),
                "timing_granularity": "approximate_local_chunk_boundary",
                "source_artifact_id": media.id,
                "source_sha256": media.sha256,
                "chunk_sha256": hashlib.sha256(chunk.path.read_bytes()).hexdigest(),
                "chunk_size_bytes": chunk.path.stat().st_size,
            }
            try:
                response = authorized_model_call(
                    context,
                    model_id=model_id,
                    input_artifact_ids=[media.id],
                    input_detail=detail,
                    operation=lambda provider, audio_path=chunk.path: provider.transcribe_audio(  # type: ignore[attr-defined]
                        audio_path, model_id
                    ),
                )
            except RunnerError as exc:
                raise RunnerError(
                    exc.code,
                    f"第 {chunk.index}/{total_chunks} 个音频片段转写失败：{exc}",
                    status_code=exc.status_code,
                ) from exc
            raw_text = response.text  # type: ignore[attr-defined]
            segment = {
                "start_seconds": round(chunk.start_seconds, 3),
                "end_seconds": round(chunk.end_seconds, 3),
                "text": raw_text,
                "timing_granularity": "approximate_local_chunk_boundary",
            }
            chunks.append(segment)
            raw_calls.append({
                **detail,
                "request_id": response.request_id,  # type: ignore[attr-defined]
                "raw_response_text": raw_text,
            })

    transcript_text = "\n".join(str(item["text"]) for item in chunks if item["text"])
    metadata = {
        "provider_id": provider_id,
        "model_id": model_id,
        "source_artifact_id": media.id,
        "source_sha256": media.sha256,
        "source_duration_seconds": probe.duration_seconds,
        "timing_note": "语音服务响应不含片段级时间戳；下方时间范围仅表示本地音频切分边界，属于近似范围。",
        "chunk_count": len(chunks),
    }
    raw_payload = {
        "schema_version": 1,
        **metadata,
        "chunks": raw_calls,
    }
    raw_artifact = register_bytes(
        context,
        "transcript_raw",
        ".json",
        _json_bytes(raw_payload),
        {"media_type": "application/json", **metadata},
    )
    markdown_lines = [
        "# 语音转写",
        "",
        f"- 服务商：{provider_id} / {model_id}",
        f"- 输入素材：{media.id}",
        f"- 片段数：{len(chunks)}",
        f"- 时间说明：{metadata['timing_note']}",
        "",
    ]
    for chunk in chunks:
        markdown_lines.extend([
            f"**{chunk['start_seconds']:.1f}–{chunk['end_seconds']:.1f} 秒（近似片段范围）**",
            "",
            str(chunk["text"]).strip() or "（未识别到语音文本）",
            "",
        ])
    transcript_artifact = register_bytes(
        context,
        "transcript",
        ".md",
        "\n".join(markdown_lines).encode("utf-8"),
        {
            "media_type": "text/markdown",
            **metadata,
            "raw_response_artifact_id": raw_artifact.id,
        },
    )
    draft = reconcile_transcript(
        transcript_text,
        source_artifact_ids=[media.id, raw_artifact.id, transcript_artifact.id],
    )
    draft_artifact = register_bytes(
        context,
        "transcript_draft",
        ".md",
        render_review_markdown(draft),
        {
            "media_type": "text/markdown",
            "source_artifact_ids": draft.source_artifact_ids,
            "uncertain_terms": draft.uncertain_terms,
            "word_count": count_copy_words(draft.text),
            "word_count_rule": "Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded",
            "human_review_required": True,
        },
    )
    context.progress(context.step.id, 100, "语音转写已保存；片段时间范围为本地切分边界")
    return StepResult(
        status=StepStatus.COMPLETED.value,
        artifact_ids=[raw_artifact.id, transcript_artifact.id, draft_artifact.id],
        metadata={"result": {
            "transcript_artifact_id": transcript_artifact.id,
            "raw_artifact_id": raw_artifact.id,
            "draft_artifact_id": draft_artifact.id,
            "chunk_count": len(chunks),
            "timing_is_approximate": True,
            "character_count": len(transcript_text),
            "review_word_count": count_copy_words(transcript_text),
        }},
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = ["transcribe_media"]
