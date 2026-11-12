"""Recognize samples across the entire video locally using RapidOCR."""
from __future__ import annotations

import hashlib
import html
import json
import math
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import time
from datetime import datetime, timezone

from .. import local_ocr
from ..models import StepStatus
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled, TaskStepSkipped
from .artifact_store import register_bytes, registered_media
from .media_processing import iter_video_samples, private_tempdir, probe_media, sample_frame_count
from .ocr import _latest_asr_segments
from .reconcile_transcript import count_copy_words, reconcile_transcript, render_review_markdown


def merge_ocr_samples(samples: list[dict], *, interval: float, duration: float) -> list[dict]:
    result: list[dict] = []
    for index, sample in enumerate(samples):
        text = str(sample.get("text", "")).strip()
        start = round(index * interval, 3)
        end = round(min(duration, (index + 1) * interval), 3)
        if not text:
            continue
        if result and result[-1]["text"] == text and result[-1]["end_seconds"] == start:
            result[-1]["end_seconds"] = end
            continue
        result.append({"frame_id": sample["frame_id"] if "frame_id" in sample else f"frame-{index + 1:05d}",
                       "sampled_at_seconds": float(sample["sampled_at_seconds"]), "start_seconds": start,
                       "end_seconds": end, "text": text})
    return result


def _duration_label(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, remaining_seconds = divmod(rounded, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {remaining_minutes} 分钟"
    if minutes:
        return f"{minutes} 分 {remaining_seconds} 秒"
    return f"{remaining_seconds} 秒"


def _clear_checkpoints(context: StepContext) -> None:
    with context.repository.database.transaction(write=True) as connection:
        connection.execute(
            "DELETE FROM local_ocr_checkpoints WHERE task_id = ? AND step_id = ?",
            (context.task.id, context.step.id),
        )


def _load_checkpoints(
    context: StepContext,
    *,
    source_artifact_id: str,
    source_sha256: str,
    interval: float,
    total: int,
    scan_profile: str,
) -> tuple[list[dict], bool]:
    database = context.repository.database
    with database.transaction(write=True) as connection:
        rows = connection.execute(
            """SELECT frame_index, frame_id, sampled_at_seconds, source_artifact_id,
                      source_sha256, interval_seconds, text, processing_seconds,
                      scan_profile, sampling_version, extraction_seconds, inference_seconds
               FROM local_ocr_checkpoints
               WHERE task_id = ? AND step_id = ? ORDER BY frame_index""",
            (context.task.id, context.step.id),
        ).fetchall()
        valid = len(rows) <= total
        samples = []
        for expected_index, row in enumerate(rows, start=1):
            try:
                frame_index = int(row["frame_index"])
                sampled_at = float(row["sampled_at_seconds"])
                saved_interval = float(row["interval_seconds"])
                processing_seconds = float(row["processing_seconds"])
                extraction_seconds = float(row["extraction_seconds"])
                inference_seconds = float(row["inference_seconds"])
                sampling_version = int(row["sampling_version"])
            except (TypeError, ValueError, OverflowError):
                valid = False
                break
            if (
                frame_index != expected_index
                or row["source_artifact_id"] != source_artifact_id
                or str(row["source_sha256"]).casefold() != source_sha256.casefold()
                or not math.isclose(saved_interval, interval, rel_tol=0, abs_tol=1e-9)
                or not math.isfinite(sampled_at)
                or not math.isfinite(processing_seconds)
                or processing_seconds < 0
                or not math.isfinite(extraction_seconds)
                or extraction_seconds < 0
                or not math.isfinite(inference_seconds)
                or inference_seconds < 0
                or row["scan_profile"] != scan_profile
                or sampling_version != local_ocr.OCR_SAMPLING_VERSION
                or not isinstance(row["frame_id"], str)
                or not isinstance(row["text"], str)
            ):
                valid = False
                break
            samples.append({
                "frame_id": str(row["frame_id"]),
                "sampled_at_seconds": sampled_at,
                "text": str(row["text"]),
                "processing_seconds": processing_seconds,
                "extraction_seconds": extraction_seconds,
                "inference_seconds": inference_seconds,
            })
        if not valid:
            connection.execute(
                "DELETE FROM local_ocr_checkpoints WHERE task_id = ? AND step_id = ?",
                (context.task.id, context.step.id),
            )
            return [], True
        return samples, False


def _save_checkpoint(context: StepContext, media, interval: float, scan_profile: str, frame, text: str,
                     extraction_seconds: float, inference_seconds: float) -> bool:
    with context.repository.database.transaction(write=True) as connection:
        step = connection.execute(
            "SELECT status FROM task_steps WHERE task_id = ? AND id = ?",
            (context.task.id, context.step.id),
        ).fetchone()
        if step is None or step["status"] != "running":
            return False
        connection.execute(
            """INSERT INTO local_ocr_checkpoints (
                   task_id, step_id, frame_index, frame_id, sampled_at_seconds,
                   source_artifact_id, source_sha256, interval_seconds, text,
                   processing_seconds, created_at, scan_profile, sampling_version,
                   extraction_seconds, inference_seconds
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, step_id, frame_index) DO UPDATE SET
                   frame_id = excluded.frame_id,
                   sampled_at_seconds = excluded.sampled_at_seconds,
                   source_artifact_id = excluded.source_artifact_id,
                   source_sha256 = excluded.source_sha256,
                   interval_seconds = excluded.interval_seconds,
                   text = excluded.text,
                   processing_seconds = excluded.processing_seconds,
                   scan_profile = excluded.scan_profile,
                   sampling_version = excluded.sampling_version,
                   extraction_seconds = excluded.extraction_seconds,
                   inference_seconds = excluded.inference_seconds,
                   created_at = excluded.created_at""",
            (
                context.task.id,
                context.step.id,
                frame.index,
                frame.frame_id,
                frame.sampled_at_seconds,
                media.id,
                media.sha256,
                interval,
                text,
                max(0.0, extraction_seconds + inference_seconds),
                datetime.now(timezone.utc).isoformat(),
                scan_profile,
                local_ocr.OCR_SAMPLING_VERSION,
                max(0.0, extraction_seconds),
                max(0.0, inference_seconds),
            ),
        )
        return True


def _progress_message(samples: list[dict], total: int, *, started_at: float | None = None,
                      resumed_count: int = 0, workers: int = 1) -> tuple[float, str]:
    completed = len(samples)
    if completed == 0:
        return 1, f"本地 OCR 正在启动连续抽帧与 {workers} 路并行，准备识别约 {total} 张画面"
    current_run_count = max(0, completed - resumed_count)
    elapsed = max(0.0, time.monotonic() - started_at) if started_at is not None else 0.0
    if current_run_count and elapsed > 0:
        seconds_per_frame = elapsed / current_run_count
    else:
        saved_work_seconds = sum(float(sample.get("processing_seconds", 0)) for sample in samples)
        seconds_per_frame = saved_work_seconds / max(1, completed * workers)
    estimated_remaining = seconds_per_frame * max(0, total - completed)
    extraction_seconds = sum(float(sample.get("extraction_seconds", 0)) for sample in samples)
    inference_seconds = sum(float(sample.get("inference_seconds", 0)) for sample in samples)
    message = (
        f"本地 OCR 已处理 {completed}/{total} 张；抽帧累计 {extraction_seconds:.1f} 秒、"
        f"OCR 计算累计 {inference_seconds:.1f} 秒，{workers} 路并行；预计剩余约 {_duration_label(estimated_remaining)}"
    )
    return min(95, completed / total * 95), message


def _recognize(engine, frame) -> tuple[str, float]:
    started = time.monotonic()
    result = engine(str(frame.path))
    texts = getattr(result, "txts", None)
    text = "\n".join(str(item).strip() for item in texts if str(item).strip()) if texts is not None else ""
    return text, max(0.0, time.monotonic() - started)


def ocr_local(context: StepContext, options: dict) -> StepResult:
    if context.cancel_event.is_set():
        raise TaskCancelled
    if context.skip_event is not None and context.skip_event.is_set():
        raise TaskStepSkipped
    if not local_ocr.status()["ready"]:
        raise RunnerError("local_ocr_missing", "本地 OCR 依赖或模型未准备完成，请重新安装项目依赖。", status_code=409)
    interval = local_ocr.validate_interval(options["interval"])
    scan_profile = local_ocr.validate_scan_profile(options.get("scan_profile", "full"))
    media = registered_media(context)
    probe = probe_media(Path(media.path))
    if not probe.has_video:
        raise RunnerError("video_stream_missing", "字幕 OCR 需要包含视频画面的素材。", status_code=422)
    media_duration = getattr(probe, "video_duration_seconds", None) or probe.duration_seconds
    total = sample_frame_count(media_duration, interval)
    if context.ocr_resume_mode == "restart":
        _clear_checkpoints(context)
    samples, stale_checkpoints = _load_checkpoints(
        context,
        source_artifact_id=media.id,
        source_sha256=media.sha256,
        interval=interval,
        total=total,
        scan_profile=scan_profile,
    )
    resumed_count = len(samples)
    workers = local_ocr.OCR_WORKERS
    progress, message = _progress_message(samples, total, resumed_count=resumed_count, workers=workers)
    if stale_checkpoints:
        message = f"继续条件不匹配，旧记录已清除；从 0/{total} 张开始识别"
    elif context.ocr_resume_mode == "restart":
        message = f"已按重新开始清空记录，准备识别 {total} 张画面"
    elif context.ocr_resume_mode == "continue" and resumed_count == 0:
        message = f"没有可复用的已识别画面；从 0/{total} 张开始继续"
    context.progress(context.step.id, progress, message)
    engine = None
    if resumed_count < total:
        try:
            from rapidocr import RapidOCR
            engine = RapidOCR(params=local_ocr.engine_params())
        except Exception as exc:
            raise RunnerError("local_ocr_init_failed", "本地 OCR 初始化失败，请检查依赖和模型文件。", status_code=503) from exc

    with private_tempdir("local-ocr") as directory:
        frames = iter_video_samples(
            Path(media.path), Path(directory), media_duration, interval,
            start_index=resumed_count, profile=scan_profile,
        )
        pending: deque[tuple[object, float, Future, bool]] = deque()
        last_digest: bytes | None = None
        last_future: Future | None = None
        deduplicated_count = 0
        run_started = time.monotonic()

        def persist_result(frame, extraction_seconds: float, future: Future, duplicate: bool) -> None:
            if context.cancel_event.is_set():
                raise TaskCancelled
            try:
                text, inference_seconds = future.result()
            except Exception as exc:
                raise RunnerError("local_ocr_failed", f"本地 OCR 识别第 {frame.index} 张画面时失败，请检查模型和环境。", status_code=503) from exc
            if context.cancel_event.is_set():
                raise TaskCancelled
            if context.skip_event is not None and context.skip_event.is_set():
                raise TaskStepSkipped
            if duplicate:
                inference_seconds = 0.0
            sample = {
                "frame_id": frame.frame_id,
                "sampled_at_seconds": frame.sampled_at_seconds,
                "text": text,
                "processing_seconds": extraction_seconds + inference_seconds,
                "extraction_seconds": extraction_seconds,
                "inference_seconds": inference_seconds,
            }
            if not _save_checkpoint(context, media, interval, scan_profile, frame, text,
                                    extraction_seconds, inference_seconds):
                raise TaskCancelled
            if frame.index != len(samples) + 1:
                raise RunnerError("local_ocr_checkpoint_order", "本地 OCR 续跑记录顺序不连续，无法安全合并。", status_code=409)
            samples.append(sample)
            progress, message = _progress_message(samples, total, started_at=run_started,
                                                  resumed_count=resumed_count, workers=workers)
            context.progress(context.step.id, progress, message)

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-ocr") as executor:
                while True:
                    if context.cancel_event.is_set():
                        raise TaskCancelled
                    if context.skip_event is not None and context.skip_event.is_set():
                        raise TaskStepSkipped
                    extraction_started = time.monotonic()
                    try:
                        frame = next(frames)
                    except StopIteration:
                        break
                    except Exception:
                        # Preserve completed OCR for the final partial batch if FFmpeg
                        # fails after yielding frames but before reaching clean EOF.
                        while pending:
                            persist_result(*pending.popleft())
                        raise
                    extraction_seconds = max(0.0, time.monotonic() - extraction_started)
                    if engine is None:
                        raise RunnerError("local_ocr_checkpoint_order", "本地 OCR 检查点数量与画面数不一致，无法继续。", status_code=409)
                    digest = hashlib.sha256(frame.path.read_bytes()).digest()
                    duplicate = digest == last_digest and last_future is not None
                    if duplicate:
                        future = last_future
                        deduplicated_count += 1
                    else:
                        future = executor.submit(_recognize, engine, frame)
                    pending.append((frame, extraction_seconds, future, duplicate))
                    last_digest, last_future = digest, future
                    if len(pending) >= workers * 2:
                        persist_result(*pending.popleft())
                while pending:
                    persist_result(*pending.popleft())
        finally:
            close_frames = getattr(frames, "close", None)
            if callable(close_frames):
                close_frames()
    if len(samples) != total:
        raise RunnerError("local_ocr_incomplete", "本地 OCR 未处理完全部画面；已完成结果已保存，可选择继续。", status_code=503)
    context.progress(context.step.id, 96, "本地 OCR 画面识别完成，正在整理识别结果")
    merged = merge_ocr_samples(samples, interval=interval, duration=media_duration)
    metadata = {"provider_id": "local-rapidocr", "model_id": "PP-OCRv6 small", "source_artifact_id": media.id,
                "source_sha256": media.sha256, "duration_seconds": media_duration,
                "interval_seconds": interval, "scan_profile": scan_profile,
                "sampled_at": "interval_start", "sampling_version": local_ocr.OCR_SAMPLING_VERSION,
                "device": local_ocr.runtime_info()["device"], "worker_count": workers,
                "extraction_seconds": round(sum(float(sample.get("extraction_seconds", 0)) for sample in samples), 3),
                "inference_seconds": round(sum(float(sample.get("inference_seconds", 0)) for sample in samples), 3),
                "deduplicated_sample_count": deduplicated_count,
                "sample_count": len(samples), "segments": len(merged)}
    output_samples = [{key: sample[key] for key in ("frame_id", "sampled_at_seconds", "text")} for sample in samples]
    raw = register_bytes(context, "ocr_raw", ".json", json.dumps({**metadata, "samples": output_samples}, ensure_ascii=False).encode("utf-8"),
                         {"media_type": "application/json", **metadata})
    scan_label = "全画面" if scan_profile == "full" else "画面下方约 42% 字幕区域"
    lines = ["# 本地视频画面文字识别", "", f"- 识别范围：{scan_label}；抽帧间隔：{interval:g} 秒；连续解码覆盖视频全时长，共 {len(samples)} 张。",
             "- 时间范围为抽样估计，间隔内短暂出现的文字可能遗漏；识别结果需人工核对。", ""]
    for item in merged:
        lines.extend([f"**约 {item['start_seconds']:.1f}–{item['end_seconds']:.1f} 秒**", "", html.escape(item["text"]), ""])
    if not merged:
        lines.extend(["（采样画面中未识别到文字）", ""])
    result = register_bytes(context, "ocr_result", ".md", "\n".join(lines).encode("utf-8"),
                            {"media_type": "text/markdown", **metadata, "raw_response_artifact_id": raw.id})
    asr_segments, asr_ids = _latest_asr_segments(context, media.id, media.sha256)
    asr_text = "\n".join(
        str(segment.get("text", "")) for segment in asr_segments if str(segment.get("text", "")).strip()
    )
    draft = reconcile_transcript(
        asr_text,
        # Keep individual samples here so repeated evidence is counted as
        # independent frames; the reader-facing OCR artifact stays merged.
        ocr_frames=samples,
        source_artifact_ids=[media.id, *asr_ids, raw.id, result.id],
        asr_segments=asr_segments,
    )
    review = register_bytes(context, "transcript_draft", ".md", render_review_markdown(draft, ocr_frames=merged),
                            {"media_type": "text/markdown", "source_artifact_ids": draft.source_artifact_ids,
                             "uncertain_terms": draft.uncertain_terms, "word_count": count_copy_words(draft.text),
                             "human_review_required": True, "ocr_is_separate_evidence": True,
                             "ocr_guided_corrections": draft.corrections,
                             "ocr_correction_count": len(draft.corrections)})
    context.progress(context.step.id, 100, "本地 OCR 已完成，结果待人工核对")
    return StepResult(status=StepStatus.COMPLETED.value, artifact_ids=[raw.id, result.id, review.id],
                      metadata={"result": {"ocr_artifact_id": result.id, "sample_count": len(samples), "segment_count": len(merged)}})
