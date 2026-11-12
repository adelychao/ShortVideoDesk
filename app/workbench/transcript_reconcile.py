"""Regenerate a transcript review draft from already registered evidence."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive import _assert_contained
from .runner import RunnerError, TaskRunner, _hash_file
from .steps.artifact_names import artifact_filename
from .steps.reconcile_transcript import count_copy_words, reconcile_transcript, render_review_markdown


_ACTIVE_TASK_STATUSES = {"queued", "running"}
_ACTIVE_STEP_STATUSES = {"queued", "running"}
_MAX_SOURCE_BYTES = 25 * 1024 * 1024


def regenerate_transcript_draft(runner: TaskRunner, task_id: str) -> str:
    """Create a new draft version without rerunning ASR or OCR recognition."""
    runner.start()
    archive_root = runner.require_archive_root()

    # Serialize against task enqueue/cancel actions so a draft never captures
    # a moving set of OCR/ASR artifacts while the task is still processing.
    with runner._lock:
        task = runner.repository.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        if task_id in runner._queued_ids or task.status in _ACTIVE_TASK_STATUSES:
            raise RunnerError("task_active", "任务仍在执行，请结束当前处理后再重新校对。", status_code=409)
        if any(step.status in _ACTIVE_STEP_STATUSES for step in task.steps):
            raise RunnerError("task_active", "仍有处理步骤正在执行，请结束后再重新校对。", status_code=409)

        with runner.database.transaction() as connection:
            media_row = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind IN ('video', 'media')
                   ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            asr_rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'transcript_raw'
                   ORDER BY created_at DESC, version DESC, id DESC""",
                (task_id,),
            ).fetchall()
            ocr_rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'ocr_raw'
                   ORDER BY created_at DESC, version DESC, id DESC""",
                (task_id,),
            ).fetchall()
            transcript_rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'transcript'
                   ORDER BY created_at DESC, version DESC, id DESC""",
                (task_id,),
            ).fetchall()
            ocr_result_rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'ocr_result'
                   ORDER BY created_at DESC, version DESC, id DESC""",
                (task_id,),
            ).fetchall()

        if media_row is None:
            raise RunnerError("transcript_reconcile_media_missing", "任务没有已归档的视频或媒体文件。", status_code=409)
        media_id = str(media_row["id"])
        media_sha256 = str(media_row["sha256"])
        media_path = _verified_media_path(media_row, archive_root)

        asr_row, asr_payload = _latest_source_json(asr_rows, media_id, media_sha256, archive_root, "语音转写")
        ocr_row, ocr_payload = _latest_source_json(ocr_rows, media_id, media_sha256, archive_root, "OCR")
        asr_segments = _asr_segments(asr_payload)
        asr_text = "\n".join(segment["text"] for segment in asr_segments if segment["text"])
        ocr_frames = _ocr_frames(ocr_payload)
        if not asr_text or not asr_segments:
            raise RunnerError("transcript_reconcile_asr_missing", "没有找到可用的 ASR 原始分段，请先完成语音转写。", status_code=409)
        if not ocr_frames:
            raise RunnerError("transcript_reconcile_ocr_missing", "没有找到可用的 OCR 识别结果，请先完成 OCR。", status_code=409)

        transcript_row = _related_text_artifact(transcript_rows, asr_row, media_id)
        ocr_result_row = _related_text_artifact(ocr_result_rows, ocr_row, media_id)
        display_frames = _display_ocr_frames(ocr_payload, ocr_frames)
        source_ids = [media_id, str(asr_row["id"]), str(ocr_row["id"])]
        frame_artifact_ids = ocr_payload.get("input_frame_artifact_ids")
        if isinstance(frame_artifact_ids, list):
            source_ids.extend(item for item in frame_artifact_ids if isinstance(item, str))
        if transcript_row is not None:
            source_ids.append(str(transcript_row["id"]))
        if ocr_result_row is not None:
            source_ids.append(str(ocr_result_row["id"]))

        draft = reconcile_transcript(
            asr_text,
            ocr_frames=ocr_frames,
            source_artifact_ids=source_ids,
            asr_segments=asr_segments,
        )
        content = render_review_markdown(draft, ocr_frames=display_frames)
        metadata: dict[str, object] = {
            "media_type": "text/markdown",
            "source_artifact_ids": draft.source_artifact_ids,
            "uncertain_terms": draft.uncertain_terms,
            "word_count": count_copy_words(draft.text),
            "word_count_rule": "Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded",
            "human_review_required": True,
            "ocr_is_separate_evidence": True,
            "manual_reconciliation": True,
            "ocr_guided_corrections": draft.corrections,
            "ocr_correction_count": len(draft.corrections),
        }

        step_id = str(ocr_row["step_id"])
        if not _step_belongs_to_task(runner, task_id, step_id):
            raise RunnerError("transcript_reconcile_step_missing", "OCR 来源步骤已不可用，请刷新任务详情后重试。", status_code=409)
        artifact_id = _write_draft_artifact(
            runner,
            task_id=task_id,
            task_status=task.status,
            step_id=step_id,
            source_rows=[media_row, asr_row, ocr_row],
            media_path=media_path,
            archive_root=archive_root,
            content=content,
            metadata=metadata,
        )
        return artifact_id


def get_transcript_comparison_data(runner: TaskRunner, task_id: str, artifact_id: str) -> dict[str, Any]:
    """Return the raw ASR baseline and OCR references for one review version."""
    archive_root = runner.require_archive_root()
    with runner.database.transaction() as connection:
        selected = connection.execute(
            "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
            (artifact_id, task_id),
        ).fetchone()
        all_artifacts = connection.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at DESC, version DESC, id DESC",
            (task_id,),
        ).fetchall()
    if selected is None:
        raise KeyError("artifact not found")
    selected_kind = str(selected["kind"])
    selected_metadata = _metadata(selected)
    if selected_kind != "transcript_draft" and selected_metadata.get("review_copy") is not True:
        raise RunnerError("transcript_comparison_unavailable", "所选版本不是可对比的校对文案。", status_code=422)

    artifacts_by_id = {str(row["id"]): row for row in all_artifacts}
    source_ids = selected_metadata.get("source_artifact_ids")
    source_ids = [item for item in source_ids if isinstance(item, str)] if isinstance(source_ids, list) else []
    raw_row = next((artifacts_by_id[item] for item in source_ids
                    if item in artifacts_by_id and str(artifacts_by_id[item]["kind"]) == "transcript_raw"), None)
    parent_ids = [selected_metadata.get("source_artifact_id")]
    for parent_id in parent_ids:
        if isinstance(parent_id, str) and parent_id in artifacts_by_id:
            parent = artifacts_by_id[parent_id]
            parent_metadata = _metadata(parent)
            parent_source_ids = parent_metadata.get("source_artifact_ids")
            if isinstance(parent_source_ids, list):
                source_ids.extend(item for item in parent_source_ids if isinstance(item, str))
            if raw_row is None:
                raw_row = next((artifacts_by_id[item] for item in source_ids
                                if item in artifacts_by_id and str(artifacts_by_id[item]["kind"]) == "transcript_raw"), None)
    if raw_row is None:
        raise RunnerError("transcript_comparison_asr_missing", "此校对版本没有关联到原始 ASR 分段。", status_code=409)

    raw_metadata = _metadata(raw_row)
    media_id = raw_metadata.get("source_artifact_id")
    if not isinstance(media_id, str):
        raise RunnerError("transcript_comparison_asr_invalid", "原始 ASR 来源信息无效。", status_code=409)
    media_row = artifacts_by_id.get(media_id)
    if media_row is None or str(media_row["kind"]) not in {"video", "media"}:
        raise RunnerError("transcript_comparison_media_missing", "原始 ASR 对应的媒体文件不可用。", status_code=409)
    media_sha256 = str(media_row["sha256"])
    _, raw_payload = _latest_source_json([raw_row], media_id, media_sha256, archive_root, "语音转写")
    segments = _asr_segments(raw_payload)
    if not segments:
        raise RunnerError("transcript_comparison_asr_missing", "没有找到原始 ASR 分段文本。", status_code=409)

    corrections = _inherited_metadata_value(selected, artifacts_by_id, "ocr_guided_corrections")
    corrections = corrections if isinstance(corrections, list) else []
    ocr_result_id = next((item for item in source_ids
                          if item in artifacts_by_id and str(artifacts_by_id[item]["kind"]) == "ocr_result"), None)
    return {
        "artifact_id": artifact_id,
        "source_segments": segments,
        "ocr_corrections": [item for item in corrections if isinstance(item, dict)],
        "ocr_result_artifact_id": ocr_result_id,
    }


def _latest_source_json(
    rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...],
    media_id: str,
    media_sha256: str,
    archive_root: Path,
    label: str,
) -> tuple[sqlite3.Row, dict[str, Any]]:
    for row in rows:
        metadata = _metadata(row)
        if metadata.get("source_artifact_id") != media_id:
            continue
        source_sha = metadata.get("source_sha256")
        if isinstance(source_sha, str) and source_sha.casefold() != media_sha256.casefold():
            continue
        path = _verified_artifact_path(row, archive_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RunnerError("transcript_reconcile_source_invalid", f"已有{label}文件无法读取，请重新生成对应步骤。", status_code=409) from None
        if not isinstance(payload, dict) or payload.get("source_artifact_id") not in {None, media_id}:
            raise RunnerError("transcript_reconcile_source_invalid", f"已有{label}文件与当前媒体不匹配，请刷新后重试。", status_code=409)
        payload_sha = payload.get("source_sha256")
        if isinstance(payload_sha, str) and payload_sha.casefold() != media_sha256.casefold():
            continue
        return row, payload
    raise RunnerError("transcript_reconcile_source_missing", f"没有找到与当前媒体对应的{label}原始成果。", status_code=409)


def _asr_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        return []
    segments: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = next((chunk[key] for key in ("normalized_text", "raw_response_text", "text")
                     if isinstance(chunk.get(key), str) and chunk[key].strip()), "")
        if not text:
            continue
        segment: dict[str, Any] = {"text": text.strip()}
        start, end = chunk.get("start_seconds"), chunk.get("end_seconds")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            segment["start_seconds"] = start
            segment["end_seconds"] = end
        segments.append(segment)
    return segments


def _ocr_frames(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = payload.get("samples")
    if not isinstance(source, list):
        source = payload.get("frames")
    if not isinstance(source, list):
        return []
    frames: list[dict[str, Any]] = []
    for frame in source:
        if not isinstance(frame, dict) or not isinstance(frame.get("text"), str):
            continue
        frame_id = frame.get("frame_id")
        sampled = frame.get("sampled_at_seconds")
        normalized = {"text": frame["text"]}
        if isinstance(frame_id, str):
            normalized["frame_id"] = frame_id
        if isinstance(sampled, (int, float)):
            normalized["sampled_at_seconds"] = sampled
        frames.append(normalized)
    return frames


def _display_ocr_frames(payload: dict[str, Any], frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload.get("samples"), list):
        interval, duration = payload.get("interval_seconds"), payload.get("duration_seconds")
        if isinstance(interval, (int, float)) and interval > 0 and isinstance(duration, (int, float)) and duration > 0:
            from .steps.local_ocr import merge_ocr_samples

            samples = [dict(frame) for frame in frames]
            return merge_ocr_samples(samples, interval=float(interval), duration=float(duration))
    return frames


def _related_text_artifact(
    rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...],
    source_row: sqlite3.Row,
    media_id: str,
) -> sqlite3.Row | None:
    for row in rows:
        metadata = _metadata(row)
        if metadata.get("source_artifact_id") == media_id and metadata.get("raw_response_artifact_id") == str(source_row["id"]):
            return row
    return None


def _metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _inherited_metadata_value(
    row: sqlite3.Row,
    artifacts_by_id: dict[str, sqlite3.Row],
    key: str,
) -> Any:
    seen: set[str] = set()
    current = row
    for _ in range(8):
        current_id = str(current["id"])
        if current_id in seen:
            break
        seen.add(current_id)
        metadata = _metadata(current)
        value = metadata.get(key)
        if value is not None:
            return value
        parent_id = metadata.get("source_artifact_id")
        if not isinstance(parent_id, str) or parent_id not in artifacts_by_id:
            break
        current = artifacts_by_id[parent_id]
    return None


def _verified_artifact_path(row: sqlite3.Row, archive_root: Path) -> Path:
    try:
        registered = Path(str(row["path"]))
        if registered.is_symlink():
            raise ValueError("symbolic link")
        path = _assert_contained(registered, archive_root, strict=True)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SOURCE_BYTES:
            raise ValueError("artifact unavailable")
        digest, _ = _hash_file(path)
        if digest.casefold() != str(row["sha256"]).casefold():
            raise ValueError("artifact changed")
        return path
    except (OSError, RuntimeError, ValueError):
        raise RunnerError("transcript_reconcile_source_invalid", "语音转写或 OCR 成果不可用或已变化，请刷新任务详情。", status_code=409) from None


def _verified_media_path(row: sqlite3.Row, archive_root: Path) -> Path:
    try:
        registered = Path(str(row["path"]))
        if registered.is_symlink():
            raise ValueError("symbolic link")
        path = _assert_contained(registered, archive_root, strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("media unavailable")
        return path
    except (OSError, RuntimeError, ValueError):
        raise RunnerError("transcript_reconcile_media_missing", "任务归档媒体文件不可用，无法保存新校对稿。", status_code=409) from None


def _step_belongs_to_task(runner: TaskRunner, task_id: str, step_id: str) -> bool:
    with runner.database.transaction() as connection:
        row = connection.execute(
            "SELECT 1 FROM task_steps WHERE id = ? AND task_id = ? AND kind = 'ocr'",
            (step_id, task_id),
        ).fetchone()
    return row is not None


def _write_draft_artifact(
    runner: TaskRunner,
    *,
    task_id: str,
    task_status: str,
    step_id: str,
    source_rows: list[sqlite3.Row],
    media_path: Path,
    archive_root: Path,
    content: bytes,
    metadata: dict[str, object],
) -> str:
    if not content or len(content) > _MAX_SOURCE_BYTES or b"\x00" in content:
        raise RunnerError("transcript_reconcile_output_invalid", "重新校对生成的内容格式无效。", status_code=500)
    try:
        folder = _assert_contained(media_path.parent, archive_root, strict=True)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("archive unavailable")
    except (OSError, RuntimeError, ValueError):
        raise RunnerError("transcript_reconcile_archive_unavailable", "任务归档目录不可用，无法保存新校对稿。", status_code=409) from None

    artifact_id = uuid.uuid4().hex
    target = folder / artifact_filename("transcript_draft", ".md", artifact_id, metadata)
    digest = hashlib.sha256(content).hexdigest()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_contained(target, archive_root, strict=True)
        if target.is_symlink() or not target.is_file() or target.stat().st_size != len(content):
            raise ValueError("artifact changed")

        now = datetime.now(timezone.utc).isoformat()
        with runner.database.transaction(write=True) as connection:
            current_task = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            active_steps = connection.execute(
                "SELECT 1 FROM task_steps WHERE task_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (task_id,),
            ).fetchone()
            current_step = connection.execute(
                "SELECT output_artifact_ids_json, status FROM task_steps WHERE id = ? AND task_id = ? AND kind = 'ocr'",
                (step_id, task_id),
            ).fetchone()
            if (
                current_task is None
                or current_task["status"] != task_status
                or current_task["status"] in _ACTIVE_TASK_STATUSES
                or active_steps is not None
                or current_step is None
                or current_step["status"] in _ACTIVE_STEP_STATUSES
            ):
                raise ValueError("task state changed")
            for source in source_rows:
                current_source = connection.execute(
                    "SELECT sha256, path FROM artifacts WHERE id = ? AND task_id = ?",
                    (str(source["id"]), task_id),
                ).fetchone()
                if (
                    current_source is None
                    or current_source["sha256"] != source["sha256"]
                    or Path(str(current_source["path"])) != Path(str(source["path"]))
                ):
                    raise ValueError("source artifact changed")
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'transcript_draft'",
                (task_id, step_id),
            ).fetchone()
            version = int(version_row["version"])
            connection.execute(
                """INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at)
                   VALUES (?, ?, ?, 'transcript_draft', ?, ?, ?, ?, ?)""",
                (artifact_id, task_id, step_id, str(target), digest, version,
                 json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now),
            )
            try:
                output_ids = json.loads(current_step["output_artifact_ids_json"])
            except (TypeError, json.JSONDecodeError):
                output_ids = []
            if not isinstance(output_ids, list):
                output_ids = []
            outputs = [item for item in output_ids if isinstance(item, str)]
            if artifact_id not in outputs:
                outputs.append(artifact_id)
            connection.execute(
                "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                (json.dumps(outputs, ensure_ascii=False, separators=(",", ":")), step_id, task_id),
            )
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))
    except ValueError:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        _remove_created_file(target)
        raise RunnerError("transcript_reconcile_state_changed", "任务或来源文件状态已变化；请刷新任务详情后重试。", status_code=409) from None
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        _remove_created_file(target)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return artifact_id


def _remove_created_file(target: Path) -> None:
    try:
        if target.is_file() and not target.is_symlink():
            target.unlink()
    except OSError:
        pass


__all__ = ["get_transcript_comparison_data", "regenerate_transcript_draft"]
