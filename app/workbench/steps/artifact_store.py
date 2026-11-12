"""Safe registration helpers for generated local workbench artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from ..archive import _assert_contained
from ..models import ArtifactRecord
from ..repository import _artifact_from_row
from ..runner import StepContext
from .artifact_names import artifact_filename


_ARTIFACT_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
_INTERNAL_KINDS = {"ocr_frame", "asr_audio_chunk"}


def is_internal_artifact(kind: str) -> bool:
    return kind in _INTERNAL_KINDS or kind.endswith("_raw") or kind.endswith("_json")


def registered_media(context: StepContext) -> ArtifactRecord:
    """Return the current task's verified video/audio artifact."""
    if context.media_artifact is not None:
        return context.media_artifact
    with context.repository.database.transaction() as connection:
        rows = connection.execute(
            """SELECT * FROM artifacts
               WHERE task_id = ? AND kind IN ('video', 'media')
               ORDER BY created_at DESC, version DESC, id DESC""",
            (context.task.id,),
        ).fetchall()
    for row in rows:
        artifact = _artifact_from_row(row)
        path = Path(artifact.path)
        try:
            safe = _assert_contained(path, context.archive_root, strict=True)
            if safe.is_symlink() or not safe.is_file():
                continue
            with safe.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    continue
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            if (
                digest.casefold() != artifact.sha256.casefold()
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ino != after.st_ino
            ):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        context.media_artifact = artifact
        return artifact
    raise ValueError("本任务没有可用且校验通过的归档媒体。")


def register_bytes(
    context: StepContext,
    kind: str,
    extension: str,
    content: bytes,
    metadata: dict[str, object],
) -> ArtifactRecord:
    """Write a create-only artifact beside the registered media and index it."""
    if not _ARTIFACT_KIND.fullmatch(kind):
        raise ValueError("artifact kind is invalid")
    extension = extension.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        raise ValueError("artifact extension is invalid")
    if not isinstance(content, bytes) or not content or len(content) > _MAX_ARTIFACT_BYTES:
        raise ValueError("generated artifact size is invalid")
    media = registered_media(context)
    media_path = Path(media.path)
    parent = _assert_contained(media_path.parent, context.archive_root, strict=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("artifact directory is unavailable")

    if is_internal_artifact(kind):
        internal = parent / ".workbench"
        if internal.is_symlink():
            raise ValueError("internal artifact directory is unavailable")
        internal.mkdir(exist_ok=True)
        parent = _assert_contained(internal, context.archive_root, strict=True)
        if internal.is_symlink() or not parent.is_dir():
            raise ValueError("internal artifact directory is unavailable")
    artifact_id = uuid.uuid4().hex
    path = parent / artifact_filename(kind, extension, artifact_id, metadata)
    digest = hashlib.sha256(content).hexdigest()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_contained(path, context.archive_root, strict=True)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(content):
            raise ValueError("generated artifact changed while being saved")

        database = context.repository.database
        with database.transaction(write=True) as connection:
            task_row = connection.execute(
                "SELECT asset_id FROM tasks WHERE id = ?", (context.task.id,)
            ).fetchone()
            step_row = connection.execute(
                "SELECT kind, status, output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
                (context.step.id, context.task.id),
            ).fetchone()
            if task_row is None or step_row is None or step_row["status"] != "running":
                raise ValueError("artifact task state changed")
            version_row = connection.execute(
                """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                   WHERE task_id = ? AND step_id = ? AND kind = ?""",
                (context.task.id, context.step.id, kind),
            ).fetchone()
            version = int(version_row["version"])
            now = _utc_now_iso()
            connection.execute(
                """INSERT INTO artifacts (
                       id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    context.task.id,
                    context.step.id,
                    kind,
                    str(path),
                    digest,
                    version,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            try:
                prior_outputs = json.loads(step_row["output_artifact_ids_json"])
            except (TypeError, json.JSONDecodeError):
                prior_outputs = []
            if not isinstance(prior_outputs, list):
                prior_outputs = []
            outputs = [item for item in prior_outputs if isinstance(item, str)]
            if artifact_id not in outputs:
                outputs.append(artifact_id)
            connection.execute(
                "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                (json.dumps(outputs, ensure_ascii=False, separators=(",", ":")), context.step.id, context.task.id),
            )
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (now, context.task.id)
            )
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            assert row is not None
            return _artifact_from_row(row)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass
        raise


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = ["register_bytes", "registered_media"]
