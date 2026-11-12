"""Shared, hash-checked text inputs for local AI analysis steps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ..archive import _assert_contained
from ..models import ArtifactRecord
from ..repository import _artifact_from_row
from ..runner import RunnerError, StepContext


_SAFE_CHUNK_BYTES = 800_000
_MAX_TEXT_BYTES = 5_000_000


def latest_transcript(context: StepContext) -> ArtifactRecord:
    with context.repository.database.transaction() as connection:
        rows = connection.execute(
            """SELECT * FROM artifacts WHERE task_id = ?
               AND kind IN ('transcript_revision', 'transcript_draft', 'transcript')
               ORDER BY created_at DESC, version DESC, id DESC""",
            (context.task.id,),
        ).fetchall()
    artifacts = [_artifact_from_row(row) for row in rows]
    reviewed = [
        item for item in artifacts
        if item.kind == "transcript_revision"
        and item.metadata.get("review_copy") is True
        and item.metadata.get("human_edited") is True
    ]
    draft = [item for item in artifacts if item.kind == "transcript_draft"]
    candidates = reviewed or draft or artifacts
    if not candidates:
        raise RunnerError("transcript_missing", "所选文本步骤没有可用的语音转写文本。", status_code=409)
    return candidates[0]


def text_inputs(context: StepContext, *, include_ocr: bool = True, include_comments: bool = True) -> list[ArtifactRecord]:
    selected = [latest_transcript(context)]
    with context.repository.database.transaction() as connection:
        if include_ocr:
            row = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'ocr_result'
                   ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
                (context.task.id,),
            ).fetchone()
            if row is not None:
                selected.append(_artifact_from_row(row))
        if include_comments:
            row = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'comment_analysis'
                   ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
                (context.task.id,),
            ).fetchone()
            if row is not None:
                selected.append(_artifact_from_row(row))
    return selected


def read_artifact_text(context: StepContext, artifact_id: str) -> str:
    artifact = _artifact_by_id(context, artifact_id)
    path = _verified_path(context, artifact)
    if path.suffix.casefold() not in {".md", ".txt", ".json"} or path.stat().st_size > _MAX_TEXT_BYTES:
        raise RunnerError("text_artifact_invalid", "AI 输入文本产物格式无效或超过 5 MB。", status_code=409)
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise RunnerError("text_artifact_invalid", "AI 输入文本产物不可读。", status_code=409) from None


def read_artifact_json(context: StepContext, artifact_id: str) -> dict[str, object]:
    try:
        value = json.loads(read_artifact_text(context, artifact_id))
    except json.JSONDecodeError:
        raise RunnerError("text_artifact_invalid", "JSON 产物内容无效。", status_code=409) from None
    if not isinstance(value, dict):
        raise RunnerError("text_artifact_invalid", "JSON 产物结构无效。", status_code=409)
    return value


def artifact_path(context: StepContext, artifact_id: str) -> Path:
    return _verified_path(context, _artifact_by_id(context, artifact_id))


def task_artifact(context: StepContext, artifact_id: str) -> ArtifactRecord:
    return _artifact_by_id(context, artifact_id)


def partition_text(text: str, *, max_bytes: int = _SAFE_CHUNK_BYTES) -> list[tuple[int, int, str]]:
    """Split complete text at character boundaries, preserving every character."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if not text:
        return [(0, 0, "")]
    result: list[tuple[int, int, str]] = []
    start = 0
    current_bytes = 0
    for index, character in enumerate(text):
        width = len(character.encode("utf-8"))
        if current_bytes and current_bytes + width > max_bytes:
            result.append((start, index, text[start:index]))
            start = index
            current_bytes = 0
        current_bytes += width
    result.append((start, len(text), text[start:]))
    return result


def parse_json_object(text: str) -> dict[str, object]:
    candidates = [text.strip()]
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RunnerError("invalid_analysis_output", "文本分析返回格式无效；原始响应已保存为本地产物。")


def string_field(value: object, *, maximum: int = 20_000) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned[:maximum] if cleaned else None


def excerpt_in_source(excerpt: str, source: str) -> bool:
    """Match exact excerpt text while ignoring whitespace used to format transcript lines."""
    normalize = lambda value: "".join(value.split())
    candidate = normalize(excerpt)
    return bool(candidate) and candidate in normalize(source)


def markdown_safe(value: str) -> str:
    return value.replace("\x00", "").replace("\r", " ").replace("\n", " ").replace("`", "&#96;")[:20_000]


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _artifact_by_id(context: StepContext, artifact_id: str) -> ArtifactRecord:
    with context.repository.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND id = ?",
            (context.task.id, artifact_id),
        ).fetchone()
    if row is None:
        raise RunnerError("text_artifact_invalid", "AI 输入产物不属于本次任务。", status_code=422)
    return _artifact_from_row(row)


def _verified_path(context: StepContext, artifact: ArtifactRecord) -> Path:
    path = Path(artifact.path)
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        safe = _assert_contained(path, context.archive_root, strict=True)
        if not safe.is_file():
            raise ValueError("not_file")
        digest = hashlib.sha256(safe.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        raise RunnerError("text_artifact_invalid", "AI 输入产物不可用或路径校验失败。", status_code=409) from None
    if digest.casefold() != artifact.sha256.casefold():
        raise RunnerError("text_artifact_changed", "AI 输入文本产物哈希已变化。", status_code=409)
    return safe


__all__ = [
    "artifact_path",
    "excerpt_in_source",
    "json_bytes",
    "latest_transcript",
    "markdown_safe",
    "parse_json_object",
    "partition_text",
    "read_artifact_json",
    "read_artifact_text",
    "string_field",
    "task_artifact",
    "text_inputs",
]
