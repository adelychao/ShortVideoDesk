"""Repository methods for workbench tasks, assets, and ordered steps."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from .database import Database
from .models import (
    AssetRecord,
    ArtifactRecord,
    SourceRecord,
    SourceKind,
    StepKind,
    StepRecord,
    StepUpdate,
    TaskRecord,
    TaskStatus,
    sanitize_source_url,
)


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_task(self, source: SourceRecord, steps: list[StepRecord]) -> tuple[AssetRecord, TaskRecord]:
        with self.database.transaction(write=True) as connection:
            return self._create_task_in_transaction(connection, source, steps)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._task_from_row(connection, row) if row else None

    def list_tasks(
        self,
        limit: int,
        offset: int,
        status: str | None = None,
        *,
        platform: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        sort: str = "newest",
        include_removed: bool = False,
    ) -> list[TaskRecord]:
        if limit < 1:
            raise ValueError("limit must be greater than zero")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if sort not in {"newest", "oldest", "title"}:
            raise ValueError("unsupported task sort")
        where, parameters = _task_filter(status, platform, date_from, date_to, search, include_removed)
        order_by = {
            "newest": "coalesce(a.archived_at, t.created_at) DESC, t.id DESC",
            "oldest": "coalesce(a.archived_at, t.created_at) ASC, t.id ASC",
            "title": "lower(coalesce(a.title, '')) ASC, coalesce(a.archived_at, t.created_at) DESC, t.id DESC",
        }[sort]
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT t.* FROM tasks AS t JOIN assets AS a ON a.id = t.asset_id
                    {where} ORDER BY {order_by} LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
            return [self._task_from_row(connection, row) for row in rows]

    def count_tasks(
        self,
        status: str | None = None,
        *,
        platform: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        include_removed: bool = False,
    ) -> int:
        where, parameters = _task_filter(status, platform, date_from, date_to, search, include_removed)
        with self.database.transaction() as connection:
            row = connection.execute(
                f"SELECT count(*) AS total FROM tasks AS t JOIN assets AS a ON a.id = t.asset_id {where}",
                parameters,
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def update_step(self, task_id: str, step_id: str, update: StepUpdate) -> StepRecord:
        changes: dict[str, object] = {}
        for field_name in (
            "status",
            "progress",
            "message",
            "error_code",
            "retry_count",
            "started_at",
            "finished_at",
        ):
            value = getattr(update, field_name)
            if value is not None:
                if isinstance(value, Enum):
                    value = value.value
                if isinstance(value, datetime):
                    value = _datetime_to_db(value)
                changes[field_name] = value
        if not changes:
            raise ValueError("step update must contain at least one field")
        if "progress" in changes and not 0 <= float(changes["progress"]) <= 100:
            raise ValueError("progress must be between 0 and 100")
        if "retry_count" in changes and int(changes["retry_count"]) < 0:
            raise ValueError("retry_count must be non-negative")

        with self.database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT id FROM task_steps WHERE task_id = ? AND id = ?",
                (task_id, step_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"step {step_id!r} does not belong to task {task_id!r}")
            assignments = ", ".join(f"{name} = ?" for name in changes)
            connection.execute(
                f"UPDATE task_steps SET {assignments} WHERE task_id = ? AND id = ?",
                (*changes.values(), task_id, step_id),
            )
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (_utc_now_iso(), task_id),
            )
            updated = connection.execute(
                "SELECT * FROM task_steps WHERE task_id = ? AND id = ?",
                (task_id, step_id),
            ).fetchone()
            return _step_from_row(updated)

    def _create_task_in_transaction(
        self,
        connection: sqlite3.Connection,
        source: SourceRecord,
        steps: list[StepRecord],
        *,
        task_id: str | None = None,
        task_status: str | TaskStatus = TaskStatus.QUEUED,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        legacy_path: str | None = None,
        legacy_status: str | None = None,
        step_status: str = "queued",
    ) -> tuple[AssetRecord, TaskRecord]:
        _validate_steps(steps)
        now = _utc_now()
        created = created_at or now
        updated = updated_at or created
        task_id = task_id or uuid.uuid4().hex
        status = _enum_value(task_status)
        identity_key = _asset_identity_key(source)
        existing_asset = connection.execute(
            "SELECT * FROM assets WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        if existing_asset is None:
            asset_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO assets (
                    id, identity_key, source_kind, platform, source_id, title, author,
                    published_at, archived_at, archive_path, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)""",
                (
                    asset_id,
                    identity_key,
                    source.kind.value,
                    source.platform,
                    source.source_id,
                    source.title,
                    source.author,
                    _datetime_to_db(source.published_at) if source.published_at else None,
                    _datetime_to_db(created),
                ),
            )
        else:
            asset_id = str(existing_asset["id"])
            connection.execute(
                """UPDATE assets SET
                    title = CASE WHEN title IS NULL OR title = '' THEN ? ELSE title END,
                    author = CASE WHEN author IS NULL OR author = '' THEN ? ELSE author END,
                    published_at = COALESCE(published_at, ?)
                    WHERE id = ?""",
                (
                    source.title,
                    source.author,
                    _datetime_to_db(source.published_at) if source.published_at else None,
                    asset_id,
                ),
            )

        source_json = json.dumps(source.to_dict(), ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            """INSERT INTO tasks (
                id, asset_id, source_json, status, legacy_path, legacy_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                asset_id,
                source_json,
                status,
                legacy_path,
                legacy_status,
                _datetime_to_db(created),
                _datetime_to_db(updated),
            ),
        )

        for step in sorted(steps, key=lambda item: item.ordinal):
            connection.execute(
                """INSERT INTO task_steps (
                    id, task_id, kind, ordinal, status, depends_on_json,
                    input_artifact_ids_json, output_artifact_ids_json, progress,
                    message, error_code, retry_count, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    step.id,
                    task_id,
                    step.kind.value,
                    step.ordinal,
                    step.status or step_status,
                    _json_list(step.depends_on),
                    _json_list(step.input_artifact_ids),
                    _json_list(step.output_artifact_ids),
                    step.progress,
                    step.message,
                    step.error_code,
                    step.retry_count,
                    _datetime_to_db(step.started_at) if step.started_at else None,
                    _datetime_to_db(step.finished_at) if step.finished_at else None,
                ),
            )

        asset_row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert asset_row is not None and task_row is not None
        return _asset_from_row(asset_row), self._task_from_row(connection, task_row)

    def _task_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> TaskRecord:
        steps = connection.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY ordinal, id",
            (row["id"],),
        ).fetchall()
        return TaskRecord(
            id=str(row["id"]),
            asset_id=str(row["asset_id"]),
            source=SourceRecord.from_dict(json.loads(row["source_json"])),
            status=str(row["status"]),
            created_at=_datetime_from_db(row["created_at"]),
            updated_at=_datetime_from_db(row["updated_at"]),
            steps=[_step_from_row(step) for step in steps],
        )


def _validate_steps(steps: list[StepRecord]) -> None:
    step_ids = [step.id for step in steps]
    ordinals = [step.ordinal for step in steps]
    if len(set(step_ids)) != len(step_ids):
        raise ValueError("step ids must be unique within a task")
    if len(set(ordinals)) != len(ordinals):
        raise ValueError("step ordinals must be unique within a task")
    known_ids = set(step_ids)
    for step in steps:
        missing = set(step.depends_on) - known_ids
        if missing:
            raise ValueError(f"step {step.id!r} depends on unknown steps")


def _asset_identity_key(source: SourceRecord) -> str:
    safe_value = sanitize_source_url(source.value) if source.kind is SourceKind.URL else source.value.strip()
    if source.platform and source.source_id:
        identity: dict[str, str] = {
            "kind": source.kind.value,
            "platform": source.platform.casefold(),
            "source_id": source.source_id,
        }
        if source.kind is SourceKind.URL:
            safe_query = urlsplit(safe_value).query
            if safe_query:
                identity["source_variant"] = safe_query
    else:
        identity = {"kind": source.kind.value, "value": safe_value}
    material = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _task_filter(
    status: str | None,
    platform: str | None,
    date_from: str | None,
    date_to: str | None,
    search: str | None,
    include_removed: bool,
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    clauses.append("t.archive_removed_at IS " + ("NOT NULL" if include_removed else "NULL"))
    if status is not None:
        clauses.append("t.status = ?")
        parameters.append(status)
    if platform == "local":
        clauses.append("a.source_kind = 'file'")
    elif platform is not None:
        clauses.append("lower(coalesce(a.platform, '')) = lower(?)")
        parameters.append(platform)
    if date_from is not None:
        clauses.append("date(coalesce(a.archived_at, t.created_at)) >= date(?)")
        parameters.append(date_from)
    if date_to is not None:
        clauses.append("date(coalesce(a.archived_at, t.created_at)) <= date(?)")
        parameters.append(date_to)
    if search:
        needle = search.casefold()
        clauses.append(
            "(instr(lower(coalesce(a.title, '')), ?) > 0 OR instr(lower(coalesce(a.source_id, '')), ?) > 0 "
            "OR instr(lower(t.id), ?) > 0)"
        )
        parameters.extend((needle, needle, needle))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(parameters)


def _asset_from_row(row: sqlite3.Row) -> AssetRecord:
    return AssetRecord(
        id=str(row["id"]),
        source_kind=SourceKind(row["source_kind"]),
        platform=row["platform"],
        source_id=row["source_id"],
        title=row["title"],
        author=row["author"],
        published_at=_optional_datetime(row["published_at"]),
        archived_at=_optional_datetime(row["archived_at"]),
        archive_path=row["archive_path"],
        sha256=row["sha256"],
        created_at=_datetime_from_db(row["created_at"]),
        description=row["description"],
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, json.JSONDecodeError, RecursionError):
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


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        id=str(row["id"]),
        kind=StepKind(row["kind"]),
        ordinal=int(row["ordinal"]),
        status=str(row["status"]),
        depends_on=json.loads(row["depends_on_json"]),
        input_artifact_ids=json.loads(row["input_artifact_ids_json"]),
        output_artifact_ids=json.loads(row["output_artifact_ids_json"]),
        progress=float(row["progress"]) if row["progress"] is not None else None,
        message=row["message"],
        error_code=row["error_code"],
        retry_count=int(row["retry_count"]),
        started_at=_optional_datetime(row["started_at"]),
        finished_at=_optional_datetime(row["finished_at"]),
    )


def _json_list(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _enum_value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _datetime_to_db(_utc_now())


def _datetime_to_db(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _datetime_from_db(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _optional_datetime(value: Any) -> datetime | None:
    return _datetime_from_db(value) if value else None
