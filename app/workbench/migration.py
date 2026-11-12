"""Idempotent import of the two explicitly supported legacy JSON stores."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import Database, PROJECT_ROOT
from .models import (
    MigrationSummary,
    SourceKind,
    SourceRecord,
    StepKind,
    StepRecord,
    sanitize_source_url,
)
from .repository import TaskRepository


_ALLOWED_ROOT_NAMES = {"anonymous-jobs", "state"}
_MEDIA_DIRECTORY = PROJECT_ROOT / "downloads"
_MEDIA_ROOT = _MEDIA_DIRECTORY.resolve()
_MEDIA_EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".wav"},
    "cover": {".jpg", ".jpeg", ".png", ".webp", ".image"},
    "metadata": {".json"},
    "note": {".md"},
}
_STATUS_MAP = {
    "排队": "queued",
    "queued": "queued",
    "pending": "queued",
    "进行中": "interrupted",
    "running": "interrupted",
    "已完成": "completed",
    "completed": "completed",
    "success": "completed",
    "succeeded": "completed",
    "部分完成": "partial",
    "partial": "partial",
    "partially_completed": "partial",
    "失败": "failed",
    "failed": "failed",
    "error": "failed",
    "needs_auth": "blocked",
    "auth_required": "blocked",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
_STEP_STATUS = {
    "queued": "queued",
    "interrupted": "interrupted",
    "completed": "completed",
    "partial": "partial",
    "failed": "failed",
    "blocked": "blocked",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class _LegacyArtifact:
    artifact_id: str
    step_id: str
    kind: str
    path: str
    sha256: str
    version: int
    metadata_json: str


def import_legacy_jobs(database: Database, roots: list[Path]) -> MigrationSummary:
    """Import allowlisted fields from data/anonymous-jobs and data/state only."""
    allowed_roots = _validate_roots(roots)
    if not allowed_roots:
        return MigrationSummary(imported=0, skipped=0, invalid=0, warnings=[])

    database.migrate()
    repository = TaskRepository(database)
    imported = 0
    skipped = 0
    invalid = 0
    warnings: list[str] = []

    for root in allowed_roots:
        for source_path in sorted(root.glob("*.json")):
            safe_name = _masked_filename(source_path.name)
            if source_path.is_symlink() or not _is_inside(source_path.resolve(strict=False), root):
                invalid += 1
                warnings.append(f"{safe_name}: rejected_path")
                continue
            try:
                legacy_path = str(source_path.resolve(strict=True))
                data = json.loads(source_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                invalid += 1
                warnings.append(f"{safe_name}: invalid_json")
                continue
            except RecursionError:
                invalid += 1
                warnings.append(f"{safe_name}: invalid_json_depth")
                continue
            except (OSError, UnicodeError):
                invalid += 1
                warnings.append(f"{safe_name}: unreadable_file")
                continue

            if not isinstance(data, dict):
                invalid += 1
                warnings.append(f"{safe_name}: invalid_record")
                continue

            try:
                source, task_status, legacy_status, created_at, updated_at = _legacy_record(data)
                task_id, step_id = _stable_ids(legacy_path)
                step_status = _STEP_STATUS.get(task_status, "queued")
                artifacts = _collect_artifacts(data, source_path.parent.name, task_id, step_id)
                step = StepRecord(
                    id=step_id,
                    kind=StepKind.DOWNLOAD,
                    ordinal=0,
                    status=step_status,
                    output_artifact_ids=[artifact.artifact_id for artifact in artifacts],
                )
            except (TypeError, ValueError):
                invalid += 1
                warnings.append(f"{safe_name}: invalid_allowed_fields")
                continue

            try:
                with database.transaction(write=True) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM tasks WHERE legacy_path = ?", (legacy_path,)
                    ).fetchone()
                    if exists:
                        skipped += 1
                        continue
                    repository._create_task_in_transaction(
                        connection,
                        source,
                        [step],
                        task_id=task_id,
                        task_status=task_status,
                        created_at=created_at,
                        updated_at=updated_at,
                        legacy_path=legacy_path,
                        legacy_status=legacy_status,
                        step_status=step_status,
                    )
                    for artifact in artifacts:
                        connection.execute(
                            """INSERT INTO artifacts (
                                id, task_id, step_id, kind, path, sha256, version,
                                metadata_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                artifact.artifact_id,
                                task_id,
                                step_id,
                                artifact.kind,
                                artifact.path,
                                artifact.sha256,
                                artifact.version,
                                artifact.metadata_json,
                                _utc_now_iso(),
                            ),
                        )
                imported += 1
            except sqlite3.IntegrityError:
                invalid += 1
                warnings.append(f"{safe_name}: database_record_conflict")

    return MigrationSummary(
        imported=imported,
        skipped=skipped,
        invalid=invalid,
        warnings=warnings,
    )


def _validate_roots(roots: list[Path]) -> list[Path]:
    if not roots:
        return []
    project_root = PROJECT_ROOT.resolve()
    data_root = PROJECT_ROOT / "data"
    if data_root.is_symlink():
        raise ValueError("legacy data directory must not be a symlink")
    expected = {
        (PROJECT_ROOT / "data" / name).resolve(): name
        for name in _ALLOWED_ROOT_NAMES
    }
    allowed: list[Path] = []
    seen: set[Path] = set()
    for value in roots:
        root = Path(value)
        if root.is_symlink():
            raise ValueError("legacy source directories must not be symlinks")
        resolved = root.resolve(strict=False)
        if resolved not in expected or not _is_inside(resolved, project_root):
            raise ValueError("legacy migration accepts only data/anonymous-jobs and data/state")
        if resolved not in seen:
            allowed.append(resolved)
            seen.add(resolved)
    return sorted(allowed, key=lambda item: expected[item])


def _legacy_record(
    data: dict[str, object],
) -> tuple[SourceRecord, str, str, datetime, datetime]:
    result = data.get("result")
    metadata = result.get("metadata") if isinstance(result, dict) else None
    if not isinstance(metadata, dict):
        metadata = {}

    raw_source = _first_string(
        data.get("source"),
        data.get("source_url"),
        metadata.get("source_url"),
        metadata.get("original_share_url"),
    )
    platform = _safe_platform(_first_string(data.get("platform"), metadata.get("platform")))
    source_id = _safe_source_id(
        _first_string(
            metadata.get("platform_id"),
            metadata.get("media_id"),
            metadata.get("source_id"),
            data.get("resolved_source_id"),
            metadata.get("id"),
        )
    )
    title = _safe_text(_first_string(metadata.get("title"), data.get("title")), 512)
    author = _safe_text(_first_string(metadata.get("author"), data.get("author")), 256)
    published_at = _parse_datetime(metadata.get("published_at"))
    source_url = _sanitize_url(raw_source) if raw_source else None
    if source_url:
        source = SourceRecord(
            kind=SourceKind.URL,
            value=source_url,
            platform=platform,
            source_id=source_id,
            title=title,
            author=author,
            published_at=published_at,
        )
    else:
        legacy_kind = data.get("source_kind")
        if legacy_kind != "file" or not raw_source:
            raise ValueError("legacy record has no valid public source")
        local_identity = hashlib.sha256(raw_source.encode("utf-8", errors="replace")).hexdigest()
        source = SourceRecord(
            kind=SourceKind.FILE,
            value=f"legacy-file:{local_identity}",
            platform=platform,
            source_id=source_id or local_identity,
            title=title,
            author=author,
            published_at=published_at,
        )

    raw_status = _safe_status(data.get("status"))
    task_status = _STATUS_MAP.get(raw_status.casefold())
    if task_status is None:
        raw_status = "unknown"
        task_status = "unknown"
    legacy_status = raw_status
    created_at = _parse_datetime(data.get("created_at")) or _parse_datetime(data.get("started_at"))
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    updated_at = (
        _parse_datetime(data.get("updated_at"))
        or _parse_datetime(data.get("finished_at"))
        or created_at
    )
    return source, task_status, legacy_status, created_at, updated_at


def _collect_artifacts(
    data: dict[str, object], legacy_root: str, task_id: str, step_id: str
) -> list[_LegacyArtifact]:
    candidates: list[tuple[str, str, object, object]] = []
    result = data.get("result")
    if isinstance(result, dict):
        output_dir = result.get("output_dir")
        files = result.get("files")
        if isinstance(files, dict):
            for role in ("video", "cover", "metadata", "note"):
                if role in files:
                    candidates.append((role, role, files[role], output_dir))

    output_dir = data.get("output_dir")
    item_list = data.get("items")
    if isinstance(item_list, list):
        for item in item_list:
            if not isinstance(item, dict):
                continue
            item_role = item.get("kind")
            for role in ("video", "cover", "metadata", "note"):
                field = f"{role}_path"
                if field in item:
                    candidates.append((role, role, item[field], output_dir))
            if isinstance(item_role, str) and item_role in _MEDIA_EXTENSIONS:
                candidates.append((item_role, item_role, item.get("path"), output_dir))

    found: dict[str, _LegacyArtifact] = {}
    for role, kind, raw_path, base in candidates:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = _resolve_media_path(raw_path, base)
        if path is None or path.suffix.casefold() not in _MEDIA_EXTENSIONS[role]:
            continue
        try:
            digest = _sha256_file(path)
        except OSError:
            continue
        relative_path = path.as_posix()
        artifact_id = "legacy-artifact-" + hashlib.sha256(
            f"{task_id}\0{kind}\0{relative_path}".encode("utf-8")
        ).hexdigest()[:32]
        metadata_json = json.dumps(
            {"legacy": True, "legacy_root": legacy_root, "file_role": role, "sha256_verified": True},
            sort_keys=True,
            separators=(",", ":"),
        )
        found[f"{kind}:{relative_path}"] = _LegacyArtifact(
            artifact_id=artifact_id,
            step_id=step_id,
            kind=f"legacy_{kind}",
            path=relative_path,
            sha256=digest,
            version=1,
            metadata_json=metadata_json,
        )
    versions: dict[str, int] = {}
    artifacts: list[_LegacyArtifact] = []
    for artifact in found.values():
        version = versions.get(artifact.kind, 0) + 1
        versions[artifact.kind] = version
        artifacts.append(
            _LegacyArtifact(
                artifact_id=artifact.artifact_id,
                step_id=artifact.step_id,
                kind=artifact.kind,
                path=artifact.path,
                sha256=artifact.sha256,
                version=version,
                metadata_json=artifact.metadata_json,
            )
        )
    return artifacts


def _resolve_media_path(raw_path: str, base: object) -> Path | None:
    project_root = PROJECT_ROOT.resolve()
    if _MEDIA_DIRECTORY.is_symlink() or not _is_inside(_MEDIA_ROOT, project_root):
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        if not isinstance(base, str) or not base.strip():
            return None
        base_path = Path(base)
        if not base_path.is_absolute():
            base_path = PROJECT_ROOT / base_path
        try:
            base_path = base_path.resolve(strict=True)
        except OSError:
            return None
        if not _is_inside(base_path, _MEDIA_ROOT) or not base_path.is_dir():
            return None
        candidate = base_path / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not _is_inside(resolved, _MEDIA_ROOT):
        return None
    return resolved


def _sanitize_url(raw: str) -> str | None:
    try:
        return sanitize_source_url(raw)
    except ValueError:
        return None


def _safe_platform(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().casefold()
    return candidate if re.fullmatch(r"[a-z0-9_-]{1,32}", candidate) else None


def _safe_source_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate) else None


def _safe_status(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > 80 or not re.fullmatch(r"[A-Za-z0-9_ -\u4e00-\u9fff]+", candidate):
        return ""
    return candidate


def _safe_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.replace("\x00", " ").split())
    return cleaned[:limit] or None


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stable_ids(legacy_path: str) -> tuple[str, str]:
    digest = hashlib.sha256(legacy_path.encode("utf-8")).hexdigest()
    task_id = f"legacy-{digest[:32]}"
    return task_id, f"{task_id}-download"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _masked_filename(filename: str) -> str:
    digest = hashlib.sha256(filename.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"legacy-file-{digest}.json"


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
