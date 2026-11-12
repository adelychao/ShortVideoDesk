"""Incremental anonymous comment collection for a verified platform work."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import html
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from ..archive import _assert_contained
from ..database import Database
from ..comments.base import (
    CommentAdapter,
    CommentAdapterError,
    adapter_for,
    collection_cursor,
    finish_collection,
    save_comment_page,
)
from ..models import CommentPage, SourceKind, SourceRecord, StepStatus
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled
from ..platform_accounts import (
    PlatformAccountError,
    get_platform_account_manager,
    get_task_access_account,
    get_task_access_mode,
)


def collect_comments(context: StepContext) -> StepResult:
    source = context.task.source
    if source.kind is not SourceKind.URL:
        raise RunnerError("comments_unsupported", "评论采集只接受抖音或 B 站公开作品链接。", status_code=422)

    database = context.repository.database
    with database.transaction() as connection:
        asset = connection.execute(
            "SELECT platform, source_id FROM assets WHERE id = ?", (context.task.asset_id,)
        ).fetchone()
    asset_platform = str(asset["platform"] or "").casefold() if asset is not None else ""
    source_id = str(asset["source_id"] or "").strip() if asset is not None else ""
    if source.platform not in {"douyin", "bilibili"} or asset_platform != source.platform:
        raise RunnerError("comments_unsupported", "评论采集只接受抖音或 B 站公开作品链接。", status_code=422)
    if not source_id:
        raise RunnerError("comments_source_id_missing", "下载步骤未返回可验证的作品 ID，无法采集评论。", status_code=422)
    if source.source_id and source.source_id != source_id:
        raise RunnerError("comments_source_mismatch", "下载作品 ID 与任务来源不一致，已停止评论采集。", status_code=422)
    # Short links do not carry an ID at task creation. The preceding download
    # step records the verified ID on the asset; use that ID for comments.
    source = replace(source, source_id=source_id)

    access_mode = get_task_access_mode(database, context.task.id)
    cookies = None
    if access_mode == "account":
        try:
            account_id, _account_name = get_task_access_account(database, context.task.id)
            if account_id:
                cookies = get_platform_account_manager().require_authenticated_cookies(
                    source.platform, account_id=account_id, database=database
                )
            else:
                cookies = get_platform_account_manager().require_authenticated_cookies(source.platform)
        except PlatformAccountError as exc:
            raise RunnerError("platform_account_required", str(exc), status_code=409) from exc
    adapter = _create_comment_adapter(source, access_mode, cookies)
    _set_adapter_progress(adapter, context)
    cursor = collection_cursor(database, context.task.id)
    previous_count = _collection_count(database, context.task.id)
    pages = 0
    collected = 0
    stop_reason = ""
    reply_warnings: list[str] = []
    partial = False
    visited: set[str | None] = set()
    # 作者：zhw；日期：2026-10-30 07:02:52。单次重试在本步骤线程内立即完成，不等待同任务的并行步骤。
    automatic_retry_used = False
    try:
        while True:
            if context.cancel_event.is_set():
                raise TaskCancelled
            if cursor in visited:
                partial = True
                stop_reason = "分页游标重复，已停止以避免重复请求"
                break
            visited.add(cursor)
            context.progress(
                context.step.id,
                None,
                f"正在读取第 {pages + 1} 页公开评论；已采集 {collected} 条。",
            )
            fetch_error = None
            try:
                page = adapter.fetch_page(source, cursor)
            except CommentAdapterError as exc:
                if context.cancel_event.is_set():
                    raise TaskCancelled
                if exc.retryable and not automatic_retry_used:
                    automatic_retry_used = True
                    _record_automatic_retry(database, context.task.id, context.step.id)
                    context.progress(context.step.id, None, "评论获取暂时失败，系统正在自动重试（1/1）。")
                    adapter.close()
                    adapter = _create_comment_adapter(source, access_mode, cookies)
                    _set_adapter_progress(adapter, context)
                    try:
                        page = adapter.fetch_page(source, cursor)
                    except CommentAdapterError as retry_error:
                        fetch_error = retry_error
                else:
                    fetch_error = exc
            if fetch_error is not None:
                previous_count = _collection_count(database, context.task.id)
                if previous_count <= 0:
                    raise RunnerError(fetch_error.code, str(fetch_error), status_code=409) from fetch_error
                partial = True
                stop_reason = str(fetch_error)
                break

            missing = {
                "author": sum(comment.author is None for comment in page.comments),
                "created_at": sum(comment.created_at is None for comment in page.comments),
                "like_count": sum(comment.like_count is None for comment in page.comments),
                "parent_id": sum(comment.parent_id is None for comment in page.comments),
            }
            pages, collected = save_comment_page(
                database,
                task_id=context.task.id,
                asset_id=context.task.asset_id,
                page=page,
                missing_like_count=missing["like_count"],
                missing_fields=missing,
            )
            if collected > previous_count:
                _mark_analysis_stale(database, context.task.id)
                previous_count = collected
            context.progress(
                context.step.id,
                None,
                f"已增量保存第 {pages} 页，本次去重后记录 {collected} 条评论。",
            )
            page_reply_warning = page.reply_warning
            page_stop_reason = page.stop_reason
            if page_stop_reason and "楼中楼" in page_stop_reason:
                page_reply_warning = page_reply_warning or page_stop_reason
                page_stop_reason = None
            if page_reply_warning and page_reply_warning not in reply_warnings:
                reply_warnings.append(page_reply_warning)
            if page_stop_reason and page_stop_reason not in {"平台已返回末页", "平台没有返回更多评论"}:
                partial = True
                stop_reason = page_stop_reason
            if page.next_cursor is None:
                stop_reason = stop_reason or page_stop_reason or "平台未提供后续分页"
                partial = partial or page.has_more is not False or collected == 0 or "限制" in stop_reason or "重复" in stop_reason
                break
            cursor = page.next_cursor
    except TaskCancelled:
        finish_collection(
            database,
            context.task.id,
            status="partial",
            stop_reason="用户取消；已保存分页可在重试时继续",
        )
        raise
    finally:
        adapter.close()

    if context.cancel_event.is_set():
        raise TaskCancelled
    if not stop_reason:
        stop_reason = "用户取消后保留已完成分页"
        finish_collection(database, context.task.id, status="partial", stop_reason=stop_reason)
        partial = True
    collection_reason = _collection_reason(stop_reason, reply_warnings)
    finish_collection(
        database,
        context.task.id,
        status="partial" if partial else "completed",
        stop_reason=collection_reason,
    )
    note = _write_comment_note(context, collection_reason)
    return StepResult(
        status=StepStatus.PARTIAL.value if partial else StepStatus.COMPLETED.value,
        artifact_ids=[note],
        metadata={
            "result": {
                "collected_count": collected,
                "stop_reason": collection_reason,
                "scope": "公开主评论，不包含楼中楼回复",
            }
        },
    )


def _collection_reason(stop_reason: str, reply_warnings: list[str]) -> str:
    parts = [stop_reason, *reply_warnings]
    return "；".join(dict.fromkeys(part for part in parts if part))[:256]


def _collection_count(database, task_id: str) -> int:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT collected_count FROM comment_collection_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return int(row["collected_count"]) if row else 0


def _create_comment_adapter(
    source: SourceRecord, access_mode: str, cookies: list[dict[str, object]] | None
) -> CommentAdapter:
    if access_mode == "account":
        return adapter_for(source.platform, cookies=cookies, authenticated=True)
    return adapter_for(source.platform)


def _set_adapter_progress(adapter: CommentAdapter, context: StepContext) -> None:
    set_progress_callback = getattr(adapter, "set_progress_callback", None)
    if callable(set_progress_callback):
        set_progress_callback(lambda message: context.progress(context.step.id, None, message))


def _record_automatic_retry(database: Database, task_id: str, step_id: str) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE task_steps SET retry_count = retry_count + 1,
                   message = '评论获取暂时失败，系统正在自动重试（1/1）。'
               WHERE task_id = ? AND id = ? AND status = 'running'""",
            (task_id, step_id),
        )


def _mark_analysis_stale(database, task_id: str) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE task_steps SET status = 'interrupted', error_code = 'comments_analysis_stale',
                   message = '评论已更新，现有分析基于旧数据；请重新授权并运行此步骤。'
               WHERE task_id = ? AND kind = 'analyze_comments' AND status = 'completed'""",
            (task_id,),
        )


def _write_comment_note(context: StepContext, stop_reason: str) -> str:
    database = context.repository.database
    with database.transaction() as connection:
        asset = connection.execute("SELECT * FROM assets WHERE id = ?", (context.task.asset_id,)).fetchone()
        state = connection.execute(
            "SELECT * FROM comment_collection_state WHERE task_id = ?", (context.task.id,)
        ).fetchone()
    if asset is None or state is None:
        raise ValueError("comment collection records are incomplete")
    archive_path_value = asset["archive_path"]
    if not isinstance(archive_path_value, str) or not archive_path_value:
        raise RunnerError("archive_missing", "下载归档目录不可用，无法生成评论资料笔记。", status_code=409)
    archive_root = context.archive_root.resolve(strict=True)
    archive_entry = _assert_contained(Path(archive_path_value), archive_root, strict=True)
    # Asset.archive_path records the primary media file; older rows may store its folder.
    archive_folder = archive_entry.parent if archive_entry.is_file() else archive_entry
    archive_folder = _assert_contained(archive_folder, archive_root, strict=True)
    if archive_folder.is_symlink() or not archive_folder.is_dir():
        raise RunnerError("archive_missing", "下载归档目录不可用，无法生成评论资料笔记。", status_code=409)

    title = _markdown_text(asset["title"] or context.task.source.title or "未命名素材")
    collected = int(state["collected_count"])
    sort_order = str(state["sort_order"])
    sort_sql = {
        "likes_desc": "CASE WHEN c.like_count IS NULL THEN 1 ELSE 0 END, c.like_count DESC, c.created_at DESC, c.platform_comment_id ASC",
        "time_desc": "CASE WHEN c.created_at IS NULL THEN 1 ELSE 0 END, c.created_at DESC, c.platform_comment_id ASC",
        "time_asc": "CASE WHEN c.created_at IS NULL THEN 1 ELSE 0 END, c.created_at ASC, c.platform_comment_id ASC",
    }.get(sort_order, "CASE WHEN c.like_count IS NULL THEN 1 ELSE 0 END, c.like_count DESC, c.created_at DESC, c.platform_comment_id ASC")
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT c.platform_comment_id, c.parent_id, c.author, c.text, c.like_count,
                      c.created_at, c.collected_at, c.raw_fields_json
               FROM comments AS c JOIN comment_collection_items AS ci ON ci.comment_id = c.id
               WHERE c.asset_id = ? AND ci.task_id = ?
               ORDER BY """ + sort_sql,
            (context.task.asset_id, context.task.id),
        ).fetchall()
    lines = [
        f"# {title} · 评论资料",
        "",
        "## 评论",
        "",
        "| 序号 | 点赞数 | 评论内容 |",
        "| ---: | ---: | --- |",
    ]
    for index, row in enumerate(rows, start=1):
        if context.cancel_event.is_set():
            raise TaskCancelled
        likes = row["like_count"] if row["like_count"] is not None else "未提供"
        content = " ".join(part for part in _markdown_text(row["text"]).splitlines() if part)
        lines.append(f"| {index} | {likes} | {content} |")
    lines.append("")
    content = "\n".join(lines).encode("utf-8")
    target = _choose_note_target(archive_folder, context.task.id)
    digest = hashlib.sha256(content).hexdigest()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        version_row = None
        with database.transaction(write=True) as connection:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'note'",
                (context.task.id, context.step.id),
            ).fetchone()
            version = int(version_row["version"])
            artifact_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO artifacts (
                       id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                   ) VALUES (?, ?, ?, 'note', ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    context.task.id,
                    context.step.id,
                    str(target),
                    digest,
                    version,
                    json.dumps({"scope": "comments", "collected_count": collected, "stop_reason": stop_reason}, ensure_ascii=False, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return artifact_id
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                target.unlink()
        except OSError:
            pass
        raise


def _choose_note_target(folder: Path, task_id: str) -> Path:
    preferred = folder / "评论资料.md"
    _assert_contained(preferred, folder)
    if not preferred.exists() and not preferred.is_symlink():
        return preferred
    task_suffix = task_id[:8]
    conflict = folder / f"评论资料-{task_suffix}.md"
    _assert_contained(conflict, folder)
    if not conflict.exists() and not conflict.is_symlink():
        return conflict
    candidate = folder / f"评论资料-{task_suffix}-{uuid.uuid4().hex[:12]}.md"
    _assert_contained(candidate, folder)
    return candidate


def _markdown_text(value: object) -> str:
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = html.escape(value, quote=False)
    value = re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", value)
    return "\n".join(line.strip() for line in value.split("\n"))[:20_000]
