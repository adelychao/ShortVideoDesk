"""Shared comment adapter protocol, normalization, and page persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Protocol
import uuid

from ..database import Database
from ..models import CommentPage, CommentRecord, SourceRecord


class CommentAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CommentAdapter(Protocol):
    def fetch_page(self, source: SourceRecord, cursor: str | None) -> CommentPage: ...

    def close(self) -> None: ...


def adapter_for(
    platform: str,
    *,
    cookies: list[dict[str, object]] | None = None,
    authenticated: bool = False,
) -> CommentAdapter:
    if platform == "douyin":
        from .douyin import DouyinCommentAdapter

        return DouyinCommentAdapter(cookies=cookies, authenticated=authenticated)
    if platform == "bilibili":
        from .bilibili import BilibiliCommentAdapter

        return BilibiliCommentAdapter(cookies=cookies, authenticated=authenticated)
    raise CommentAdapterError("comments_unsupported", "当前只支持抖音和 B 站公开评论。")


def normalize_record(
    *,
    comment_id: object,
    text: object,
    author: object = None,
    like_count: object = None,
    timestamp: object = None,
    parent_id: object = None,
    root_id: object = None,
    reply_count: object = None,
) -> CommentRecord | None:
    platform_id = str(comment_id or "").strip()
    content = text if isinstance(text, str) else ""
    content = content.strip()
    if not platform_id or not content:
        return None
    if len(platform_id) > 256:
        return None
    if len(content) > 20_000:
        content = content[:20_000]
    safe_author = author.strip()[:256] if isinstance(author, str) and author.strip() else None
    parsed_like: int | None = None
    if isinstance(like_count, int) and not isinstance(like_count, bool) and like_count >= 0:
        parsed_like = like_count
    elif isinstance(like_count, str) and re.fullmatch(r"[0-9]{1,18}", like_count.strip()):
        parsed_like = int(like_count)
    created: datetime | None = None
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            created = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            created = None
    elif isinstance(timestamp, str):
        try:
            created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            created = None
    safe_parent = str(parent_id).strip() if parent_id not in (None, "", 0, "0") else None
    fields: dict[str, object] = {}
    if root_id not in (None, "", 0, "0"):
        fields["root_id"] = str(root_id).strip()[:256]
    if isinstance(reply_count, int) and not isinstance(reply_count, bool) and reply_count >= 0:
        fields["reply_count"] = reply_count
    return CommentRecord(
        id=uuid.uuid4().hex,
        platform_comment_id=platform_id,
        parent_id=safe_parent,
        author=safe_author,
        text=content,
        like_count=parsed_like,
        created_at=created,
        collected_at=datetime.now(timezone.utc),
        raw_fields=fields,
    )


def save_comment_page(
    database: Database,
    *,
    task_id: str,
    asset_id: str,
    page: CommentPage,
    missing_like_count: int,
    missing_fields: dict[str, int],
) -> tuple[int, int]:
    """Persist one page and its cursor atomically so a retry can resume."""
    inserted = 0
    with database.transaction(write=True) as connection:
        for comment in page.comments:
            connection.execute(
                """INSERT OR IGNORE INTO comments (
                       id, asset_id, task_id, platform_comment_id, parent_id, author,
                       text, like_count, created_at, collected_at, raw_fields_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    comment.id,
                    asset_id,
                    task_id,
                    comment.platform_comment_id,
                    comment.parent_id,
                    comment.author,
                    comment.text,
                    comment.like_count,
                    comment.created_at.isoformat() if comment.created_at else None,
                    comment.collected_at.isoformat(),
                    json.dumps(comment.raw_fields, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            existing = connection.execute(
                "SELECT id FROM comments WHERE asset_id = ? AND platform_comment_id = ?",
                (asset_id, comment.platform_comment_id),
            ).fetchone()
            if existing is None:
                continue
            link = connection.execute(
                """INSERT OR IGNORE INTO comment_collection_items (task_id, comment_id, collected_at)
                   VALUES (?, ?, ?)""",
                (task_id, str(existing["id"]), comment.collected_at.isoformat()),
            )
            inserted += max(0, link.rowcount)

        now = datetime.now(timezone.utc).isoformat()
        current = connection.execute(
            "SELECT missing_fields_json FROM comment_collection_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        try:
            accumulated = json.loads(current["missing_fields_json"]) if current else {}
        except (TypeError, json.JSONDecodeError):
            accumulated = {}
        if not isinstance(accumulated, dict):
            accumulated = {}
        for name, count in missing_fields.items():
            if isinstance(name, str) and isinstance(count, int) and count > 0:
                accumulated[name] = int(accumulated.get(name, 0)) + count
        state_reason = "；".join(
            reason for reason in (page.stop_reason, page.reply_warning) if reason
        )[:256] or None
        connection.execute(
            """INSERT INTO comment_collection_state (
                   task_id, asset_id, next_cursor, page_count, collected_count,
                   missing_like_count, missing_fields_json, stop_reason, status, updated_at
               ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, 'running', ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   next_cursor = excluded.next_cursor,
                   page_count = comment_collection_state.page_count + 1,
                   collected_count = comment_collection_state.collected_count + excluded.collected_count,
                   missing_like_count = comment_collection_state.missing_like_count + excluded.missing_like_count,
                   missing_fields_json = excluded.missing_fields_json,
                   stop_reason = excluded.stop_reason,
                   status = 'running', updated_at = excluded.updated_at""",
            (
                task_id,
                asset_id,
                page.next_cursor,
                inserted,
                missing_like_count,
                json.dumps(accumulated, ensure_ascii=False, separators=(",", ":")),
                state_reason,
                now,
            ),
        )
        state = connection.execute(
            "SELECT page_count, collected_count FROM comment_collection_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if state is None:
        raise sqlite3.IntegrityError("comment collection state was not persisted")
    return int(state["page_count"]), int(state["collected_count"])


def collection_cursor(database: Database, task_id: str) -> str | None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT next_cursor FROM comment_collection_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return str(row["next_cursor"]) if row is not None and row["next_cursor"] else None


def finish_collection(database: Database, task_id: str, *, status: str, stop_reason: str) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE comment_collection_state SET status = ?, stop_reason = ?, updated_at = ?
               WHERE task_id = ?""",
            (status, stop_reason[:256], datetime.now(timezone.utc).isoformat(), task_id),
        )
