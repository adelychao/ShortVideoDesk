"""Safe archive path creation and media-identity lookup for the workbench."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from .database import Database
from .models import AssetRecord, ArtifactRecord
from .repository import _asset_from_row

if TYPE_CHECKING:
    from sqlite3 import Connection


_INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "conin$",
    "conout$",
    *(f"com{number}" for number in "123456789¹²³"),
    *(f"lpt{number}" for number in "123456789¹²³"),
}
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_ARCHIVE_SEQUENCE = 1_000_000
_ARCHIVE_SEQUENCE_LOCK = threading.Lock()
_ARCHIVE_SEQUENCE_PATTERN = re.compile(r"^(\d{4,})_")
_TOPIC_TAG_PATTERN = re.compile(r"(?<!\b[CcFf])#")


def safe_title(title: str, max_length: int = 80) -> str:
    """Return a non-empty Windows-safe path component derived from a title."""
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
        raise ValueError("max_length must be a positive integer")

    normalized = unicodedata.normalize("NFKC", title)
    cleaned = _INVALID_WINDOWS_CHARACTERS.sub("_", normalized)
    cleaned = " ".join(cleaned.split()).strip().rstrip(" .")
    cleaned = cleaned[:max_length].rstrip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "untitled"[:max_length]

    device_stem = cleaned.split(".", 1)[0].rstrip(" .").casefold()
    if device_stem in _DEVICE_NAMES:
        cleaned = f"_{cleaned}"[:max_length].rstrip(" .")
    if not cleaned:
        cleaned = "untitled"[:max_length]
    return cleaned


def local_archive_time(value: datetime) -> datetime:
    return value.astimezone()


def archive_path(
    root: Path,
    archived_at: datetime,
    title: str,
    platform: str,
    source_id: str,
) -> Path:
    """Atomically reserve and create a date/sequence archive directory."""
    if not isinstance(archived_at, datetime):
        raise TypeError("archived_at must be a datetime")
    if not isinstance(platform, str) or not isinstance(source_id, str):
        raise TypeError("platform and source_id must be strings")

    resolved_root = _prepare_archive_root(root)
    day_name = local_archive_time(archived_at).strftime("%Y-%m-%d")
    day_directory = resolved_root / day_name
    _assert_contained(day_directory, resolved_root)
    try:
        day_directory.mkdir()
    except FileExistsError:
        if day_directory.is_symlink() or not day_directory.is_dir():
            raise ValueError("archive date path is not a safe directory")
    _assert_contained(day_directory, resolved_root, strict=True)

    if not isinstance(title, str):
        raise TypeError("title must be a string")
    title_without_topics = _TOPIC_TAG_PATTERN.split(unicodedata.normalize("NFKC", title), maxsplit=1)[0].strip()
    title_part = safe_title(title_without_topics or "未命名素材").rstrip(" _.") or "未命名素材"
    # 作者：zhw；日期：2026-10-30 00:30:01。工作台的发布流程在数据库写事务内调用此函数；线程锁覆盖同进程的其他调用。
    with _ARCHIVE_SEQUENCE_LOCK:
        highest = 0
        with os.scandir(day_directory) as entries:
            for entry in entries:
                match = _ARCHIVE_SEQUENCE_PATTERN.match(entry.name)
                if match and entry.is_dir(follow_symlinks=False):
                    highest = max(highest, int(match.group(1)))
        for sequence in range(highest + 1, _MAX_ARCHIVE_SEQUENCE + 1):
            candidate = day_directory / f"{sequence:04d}_{title_part}"
            _assert_contained(candidate, resolved_root)
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            _assert_contained(candidate, resolved_root, strict=True)
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError("archive target is not a safe directory")
            return candidate.resolve(strict=True)
        raise FileExistsError("no available archive sequence remains for this date")


def find_duplicate(
    platform: str | None,
    source_id: str | None,
    sha256: str | None,
    *,
    database: Database | None = None,
) -> AssetRecord | None:
    """Find an existing asset by platform identity or verified content hash."""
    db = database or Database()
    db.migrate()
    with db.transaction() as connection:
        row = _find_duplicate_row(connection, platform, source_id, sha256)
        return _asset_from_row(row) if row is not None else None


def _find_duplicate_row(
    connection: Connection,
    platform: str | None,
    source_id: str | None,
    sha256: str | None,
) -> sqlite3.Row | None:
    if sha256 is not None:
        if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
    if isinstance(platform, str) and platform.strip() and isinstance(source_id, str) and source_id.strip():
        row = connection.execute(
            """SELECT * FROM assets
               WHERE lower(platform) = ? AND source_id = ?
               ORDER BY CASE WHEN archive_path IS NULL THEN 1 ELSE 0 END,
                        archived_at ASC, created_at ASC, id ASC
               LIMIT 1""",
            (platform.strip().casefold(), source_id.strip()),
        ).fetchone()
        if row is not None:
            return row
    if sha256 is not None:
        return connection.execute(
            """SELECT * FROM assets WHERE sha256 = ?
               ORDER BY CASE WHEN archive_path IS NULL THEN 1 ELSE 0 END,
                        archived_at ASC, created_at ASC, id ASC
               LIMIT 1""",
            (sha256.casefold(),),
        ).fetchone()
    return None


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ArtifactRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        step_id=str(row["step_id"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        sha256=str(row["sha256"]),
        version=int(row["version"]),
        metadata=metadata,
    )


def _latest_media_artifact(
    connection: Connection, asset_id: str
) -> ArtifactRecord | None:
    row = connection.execute(
        """SELECT artifacts.* FROM artifacts
           JOIN tasks ON tasks.id = artifacts.task_id
           WHERE tasks.asset_id = ?
             AND artifacts.kind IN ('media', 'video', 'audio', 'legacy_video', 'legacy_audio')
           ORDER BY artifacts.created_at DESC, artifacts.version DESC, artifacts.id DESC
           LIMIT 1""",
        (asset_id,),
    ).fetchone()
    return _artifact_from_row(row) if row is not None else None


def _prepare_archive_root(root: Path) -> Path:
    root_path = _validated_local_path(root, label="archive root")
    if root_path.is_symlink():
        raise ValueError("archive root must not be a symbolic link")
    try:
        root_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("archive root cannot be created") from exc
    if root_path.is_symlink() or not root_path.is_dir():
        raise ValueError("archive root must be a regular directory")
    resolved = root_path.resolve(strict=True)
    _validate_resolved_local_path(resolved, label="archive root")
    return resolved


def _validated_local_path(value: Path, *, label: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise TypeError(f"{label} must be a filesystem path") from exc
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a non-empty local path")
    normalized = raw.replace("/", "\\").casefold()
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.IGNORECASE) or raw.casefold().startswith("file:"):
        raise ValueError(f"{label} must not be a URL")
    if normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\device\\")):
        raise ValueError(f"{label} must not use a Windows device path")
    if raw.startswith(("\\\\", "//")):
        raise ValueError(f"{label} must not use a UNC path")

    path = Path(raw).expanduser()
    windows_parts = PureWindowsPath(raw).parts
    if ".." in path.parts or ".." in windows_parts:
        raise ValueError(f"{label} must not contain path traversal")
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _validate_resolved_local_path(path: Path, *, label: str) -> None:
    raw = str(path)
    normalized = raw.replace("/", "\\").casefold()
    if normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\device\\")):
        raise ValueError(f"{label} resolves to a Windows device path")
    if raw.startswith(("\\\\", "//")):
        raise ValueError(f"{label} resolves to a UNC path")


def _assert_contained(candidate: Path, root: Path, *, strict: bool = False) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=strict)
    _validate_resolved_local_path(resolved_candidate, label="archive target")
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("archive target escapes the configured root") from exc
    if resolved_candidate == resolved_root:
        raise ValueError("archive target must be below the configured root")
    return resolved_candidate


def _publish_staged_file(staged: Path, target: Path, root: Path) -> Path:
    """Publish a staged file atomically without replacing an existing path."""
    resolved_root = root.resolve(strict=True)
    staged_path = _assert_contained(staged, resolved_root, strict=True)
    target_path = _assert_contained(target, resolved_root)
    if staged_path.is_symlink() or not staged_path.is_file():
        raise ValueError("staged media must be a regular file")
    if target_path.exists() or target_path.is_symlink():
        raise FileExistsError("archive media target already exists")
    if staged_path.stat().st_dev != target_path.parent.stat().st_dev:
        raise ValueError("staged media and archive target must be on the same filesystem")

    if os.name == "nt":
        os.rename(staged_path, target_path)
    else:
        os.link(staged_path, target_path)
        staged_path.unlink()

    published = _assert_contained(target_path, resolved_root, strict=True)
    if published.is_symlink() or not published.is_file():
        raise ValueError("published media is not a regular file")
    return published


def _safe_component(value: str, *, fallback: str, limit: int) -> str:
    cleaned = _INVALID_WINDOWS_CHARACTERS.sub("_", unicodedata.normalize("NFKC", value.strip()))
    cleaned = cleaned.strip().rstrip(" .")[:limit].rstrip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    device_stem = cleaned.split(".", 1)[0].rstrip(" .").casefold()
    if device_stem in _DEVICE_NAMES:
        cleaned = f"_{cleaned}"[:limit].rstrip(" .")
    return cleaned or fallback
