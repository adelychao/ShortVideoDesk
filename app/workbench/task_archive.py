"""Safe archive removal and permanent deletion for individual task records."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive import _assert_contained
from .database import Database


class TaskArchiveError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


_OPENABLE_EXTENSIONS = {
    ".aac", ".flac", ".jpeg", ".jpg", ".json", ".m4a", ".md", ".mkv", ".mov", ".mp3",
    ".mp4", ".oga", ".ogg", ".opus", ".png", ".srt", ".txt", ".vtt", ".wav", ".webm", ".webp",
}
_KIND_LABELS = {
    "media": "视频/音频", "video": "视频", "audio": "音频", "cover": "封面", "thumbnail": "封面",
    "transcript": "转写稿", "transcript_raw": "原始转写", "transcript_draft": "校对稿",
    "ocr_result": "OCR 结果", "ocr_frame": "OCR 关键帧", "analysis": "分析结果",
    "comment_analysis": "评论分析", "knowledge_card": "知识卡", "final_script": "口播稿",
    "script": "稿件", "note": "资料笔记", "video_notes": "视频笔记",
}


def hide_task(database: Database, task_id: str) -> None:
    _set_hidden(database, task_id, True)


def restore_task(database: Database, task_id: str) -> None:
    _set_hidden(database, task_id, False)


def _set_hidden(database: Database, task_id: str, hidden: bool) -> None:
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskArchiveError("任务不存在。", status_code=404)
        connection.execute(
            "UPDATE tasks SET archive_removed_at = ? WHERE id = ?",
            (_utc_now() if hidden else None, task_id),
        )


def deletion_preview(
    database: Database,
    task_id: str,
    archive_root: Path | None,
    *,
    runner_active: bool = False,
) -> dict[str, Any]:
    with database.transaction() as connection:
        return _build_preview(connection, task_id, archive_root, runner_active=runner_active)


def delete_task(
    database: Database,
    task_id: str,
    archive_root: Path | None,
    expected_token: str,
    *,
    runner_active: bool = False,
) -> dict[str, Any]:
    if not expected_token:
        raise TaskArchiveError("删除预览已失效，请重新预览后再确认。")
    staged: list[tuple[Path, Path]] = []
    trash_root: Path | None = None
    archive_directory: Path | None = None
    root = _safe_root(archive_root)
    try:
        with database.transaction(write=True) as connection:
            preview = _build_preview(connection, task_id, archive_root, runner_active=runner_active)
            if preview["token"] != expected_token:
                raise TaskArchiveError("任务或文件引用在预览后发生变化，请核对更新后的清单。")
            if preview["blocked_reason"]:
                raise TaskArchiveError(str(preview["blocked_reason"]))

            asset_row = connection.execute(
                "SELECT archive_path FROM assets WHERE id = ?", (preview["asset_id"],)
            ).fetchone()
            sibling_task = connection.execute(
                "SELECT 1 FROM tasks WHERE asset_id = ? AND id <> ? LIMIT 1",
                (preview["asset_id"], task_id),
            ).fetchone()
            if asset_row is not None and sibling_task is None:
                archive_directory = _task_archive_directory(asset_row["archive_path"], root)

            deletable = [item for item in preview["items"] if item["action"] == "delete" and item["file_path"]]
            if deletable:
                if root is None:
                    raise TaskArchiveError("未配置归档目录，不能安全删除产出文件。")
                job_id = uuid.uuid4().hex
                trash_root = root / ".workbench-delete" / job_id
                trash_root.parent.mkdir(exist_ok=True)
                _assert_contained(trash_root, root)
                trash_root.mkdir()
                for index, item in enumerate(deletable):
                    source = Path(str(item["file_path"]))
                    if _has_symlink_component(source, root):
                        raise TaskArchiveError("文件路径状态已变化，请刷新删除预览。")
                    safe_source = _assert_contained(source, root, strict=True)
                    if safe_source.is_symlink() or not safe_source.is_file():
                        raise TaskArchiveError("文件状态已变化，请刷新删除预览。")
                    current_stat = safe_source.stat()
                    if current_stat.st_size != item["size_bytes"] or current_stat.st_mtime_ns != item["mtime_ns"]:
                        raise TaskArchiveError("文件内容在预览后发生变化，请刷新清单后再确认。")
                    destination = trash_root / f"{index:06d}_{safe_source.name}"
                    os.replace(safe_source, destination)
                    staged.append((destination, safe_source))

            comment_ids = [str(item) for item in preview["deletable_comment_ids"]]
            if comment_ids:
                placeholders = ",".join("?" for _ in comment_ids)
                connection.execute(f"DELETE FROM comments WHERE id IN ({placeholders})", tuple(comment_ids))
            if preview["clear_asset_archive_path"]:
                connection.execute("UPDATE assets SET archive_path = NULL WHERE id = ?", (preview["asset_id"],))
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

        cleanup_errors: list[str] = []
        for staged_path, _original_path in staged:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(staged_path.name)
        if archive_directory is not None and root is not None:
            if _remove_empty_archive_directory(archive_directory, root):
                _remove_empty_date_directory(archive_directory.parent, root)
        if trash_root is not None:
            try:
                trash_root.rmdir()
                trash_root.parent.rmdir()
            except OSError:
                pass
        return {
            "deleted": True,
            "deleted_files": len(staged) - len(cleanup_errors),
            "cleanup_pending": cleanup_errors,
            "deleted_comments": len(preview["deletable_comment_ids"]),
        }
    except BaseException as exc:
        rollback_failed = False
        for staged_path, original_path in reversed(staged):
            try:
                original_path.parent.mkdir(parents=True, exist_ok=True)
                if staged_path.exists() and not original_path.exists():
                    os.replace(staged_path, original_path)
            except OSError:
                rollback_failed = True
        if trash_root is not None:
            try:
                trash_root.rmdir()
                trash_root.parent.rmdir()
            except OSError:
                pass
        if rollback_failed:
            raise TaskArchiveError("任务记录尚未删除，但有文件未能还原；请检查归档目录中的 .workbench-delete 文件夹。") from exc
        if isinstance(exc, TaskArchiveError):
            raise
        if isinstance(exc, (OSError, RuntimeError, ValueError)):
            raise TaskArchiveError("无法安全整理文件，任务记录尚未删除；请检查文件是否被占用后重试。") from exc
        raise


def open_task_output(
    database: Database,
    task_id: str,
    archive_root: Path | None,
    relative_path: str,
    *,
    reveal: bool,
) -> Path:
    root = _safe_root(archive_root)
    if root is None:
        raise TaskArchiveError("请先配置归档目录。")
    with database.transaction() as connection:
        row = connection.execute("SELECT asset_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskArchiveError("任务不存在。", status_code=404)
        artifact_rows = connection.execute("SELECT path FROM artifacts WHERE task_id = ?", (task_id,)).fetchall()
        asset = connection.execute("SELECT archive_path FROM assets WHERE id = ?", (row["asset_id"],)).fetchone()
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise TaskArchiveError("文件路径无效。", status_code=404)
    lexical_target = root / requested
    try:
        if _has_symlink_component(lexical_target, root):
            raise ValueError("symbolic link component")
        target = _assert_contained(lexical_target, root, strict=True)
        if target.is_symlink() or not target.is_file():
            raise ValueError("not a regular file")
    except (OSError, RuntimeError, ValueError):
        raise TaskArchiveError("文件不存在或不可访问。", status_code=404) from None
    allowed_directories: set[Path] = set()
    for artifact_row in artifact_rows:
        try:
            artifact_path = _assert_contained(Path(str(artifact_row["path"])), root)
        except (OSError, ValueError, RuntimeError):
            continue
        if artifact_path.is_symlink():
            continue
        allowed_directories.add(artifact_path.parent.resolve())
    if asset and asset["archive_path"]:
        try:
            asset_path = _assert_contained(Path(str(asset["archive_path"])), root)
            if not asset_path.is_symlink():
                allowed_directories.add((asset_path if asset_path.is_dir() else asset_path.parent).resolve())
        except (OSError, ValueError, RuntimeError):
            pass
    resolved_target = target.resolve(strict=True)
    if not any(_is_within(resolved_target, directory) for directory in allowed_directories):
        raise TaskArchiveError("文件不属于此任务的归档产出。", status_code=404)
    if not reveal and target.suffix.casefold() not in _OPENABLE_EXTENSIONS:
        raise TaskArchiveError("此文件类型不支持直接查看，请选择打开目录。", status_code=422)
    return target


def _build_preview(
    connection: sqlite3.Connection,
    task_id: str,
    archive_root: Path | None,
    *,
    runner_active: bool = False,
) -> dict[str, Any]:
    task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise TaskArchiveError("任务不存在。", status_code=404)
    asset = connection.execute("SELECT * FROM assets WHERE id = ?", (task["asset_id"],)).fetchone()
    if asset is None:
        raise TaskArchiveError("素材记录不存在。")
    root = _safe_root(archive_root)
    artifacts = connection.execute(
        "SELECT id, kind, path, sha256, version FROM artifacts WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    linked_articles = connection.execute(
        """SELECT id, title FROM articles
           WHERE source_task_id = ? OR source_artifact_id IN (SELECT id FROM artifacts WHERE task_id = ?)
           ORDER BY title COLLATE NOCASE, id""",
        (task_id, task_id),
    ).fetchall()
    task_status = connection.execute(
        "SELECT count(*) AS total FROM task_steps WHERE task_id = ? AND status IN ('queued', 'running', 'interrupted', 'awaiting_authorization')",
        (task_id,),
    ).fetchone()
    comment_rows = connection.execute(
        """SELECT c.id,
                  EXISTS(SELECT 1 FROM comment_collection_items AS other
                         WHERE other.comment_id = c.id AND other.task_id <> ?) AS shared
           FROM comments AS c
           WHERE c.task_id = ? OR c.id IN (SELECT comment_id FROM comment_collection_items WHERE task_id = ?)
           ORDER BY c.id""",
        (task_id, task_id, task_id),
    ).fetchall()
    deletable_comment_ids = [str(row["id"]) for row in comment_rows if not bool(row["shared"])]
    shared_comment_count = len(comment_rows) - len(deletable_comment_ids)
    batch_rows = connection.execute(
        "SELECT id, media_path, media_name, status FROM video_publish_batches WHERE asset_id = ? ORDER BY created_at, id",
        (str(asset["id"]),),
    ).fetchall()
    sibling_count_row = connection.execute(
        "SELECT count(*) AS total FROM tasks WHERE asset_id = ? AND id <> ?",
        (str(asset["id"]), task_id),
    ).fetchone()
    sibling_task_count = int(sibling_count_row["total"])

    registered_by_path: dict[str, dict[str, Any]] = {}
    item_by_key: dict[str, dict[str, Any]] = {}
    scan_directories: set[Path] = set()
    for row in artifacts:
        raw_path = str(row["path"])
        key = _path_key(raw_path)
        if key in registered_by_path:
            registered_by_path[key]["artifact_ids"].append(str(row["id"]))
            continue
        registered_by_path[key] = {
            "artifact_ids": [str(row["id"])],
            "kind": str(row["kind"]),
            "path": raw_path,
            "sha256": str(row["sha256"]),
        }
    if isinstance(asset["archive_path"], str) and asset["archive_path"]:
        media_path = Path(str(asset["archive_path"]))
        media_key = _path_key(str(media_path))
        if media_key not in registered_by_path and media_path.suffix.casefold() in _OPENABLE_EXTENSIONS:
            registered_by_path[media_key] = {
                "artifact_ids": [],
                "kind": "media",
                "path": str(media_path),
                "sha256": str(asset["sha256"] or ""),
            }

    for descriptor in registered_by_path.values():
        raw_path = str(descriptor["path"])
        path = Path(raw_path)
        item = _file_item(
            connection,
            task_id,
            descriptor,
            root,
            linked_articles,
            batch_rows,
            str(asset["archive_path"] or ""),
            sibling_task_count,
        )
        item_by_key[_path_key(raw_path)] = item
        if item["inside_archive"]:
            try:
                if root and _has_symlink_component(path, root):
                    raise ValueError("symbolic link component")
                safe_path = _assert_contained(path, root) if root else None
                if safe_path is not None:
                    scan_directories.add(safe_path.parent)
            except (OSError, ValueError, RuntimeError):
                pass

    archive_path_value = asset["archive_path"]
    if root is not None and archive_path_value:
        try:
            raw_media_path = Path(str(archive_path_value))
            if _has_symlink_component(raw_media_path, root):
                raise ValueError("symbolic link component")
            media_path = _assert_contained(raw_media_path, root)
            if not media_path.is_symlink():
                scan_directories.add(media_path if media_path.is_dir() else media_path.parent)
        except (OSError, ValueError, RuntimeError):
            pass

    unknown_items: list[dict[str, Any]] = []
    if root is not None:
        for directory in sorted(scan_directories, key=lambda path: str(path).casefold()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for current, dir_names, file_names in os.walk(directory, followlinks=False):
                dir_names[:] = [name for name in dir_names if name != ".workbench-delete"]
                current_path = Path(current)
                for name in list(dir_names):
                    candidate = current_path / name
                    if candidate.is_symlink():
                        unknown_items.append(_unknown_item(candidate, root, unsafe=True))
                        dir_names.remove(name)
                for name in file_names:
                    candidate = current_path / name
                    if _path_key(str(candidate)) in item_by_key:
                        continue
                    unknown_items.append(_unknown_item(candidate, root, unsafe=candidate.is_symlink()))

    unique_unknown: dict[str, dict[str, Any]] = {}
    for item in unknown_items:
        unique_unknown[_path_key(item["full_path"])] = item
    items = list(item_by_key.values())
    items.extend(unique_unknown.values())
    items.sort(key=lambda item: (item["full_path"].casefold(), item["name"].casefold()))

    task_row = {
        "name": "任务记录、处理步骤与模型调用记录",
        "kind": "task_record",
        "size_bytes": None,
        "relative_path": "工作台数据库",
        "full_path": "工作台数据库",
        "status": "将删除",
        "reason": "此任务及其专属记录",
        "action": "delete",
        "file_path": None,
        "artifact_id": None,
        "can_open": False,
        "can_reveal": False,
    }
    items.insert(0, task_row)
    if comment_rows:
        items.append({
            "name": f"本次采集的评论数据（{len(comment_rows)} 条）",
            "kind": "comments",
            "size_bytes": None,
            "relative_path": "工作台数据库 / 评论资料",
            "full_path": "工作台数据库 / 评论资料",
            "status": f"删除独占 {len(deletable_comment_ids)} 条；保留共享 {shared_comment_count} 条",
            "reason": "共享评论仍被其他任务使用时保留其内容",
            "action": "delete_data",
            "file_path": None,
            "artifact_id": None,
            "can_open": False,
            "can_reveal": False,
        })
    for row in linked_articles:
        items.append({
            "name": f"文章：{str(row['title'] or '未命名文章')}",
            "kind": "linked_article",
            "size_bytes": None,
            "relative_path": "文章管理",
            "full_path": f"文章管理 / {row['id']}",
            "status": "保留：文章正文与发布历史不受影响",
            "reason": "删除后会解除文章的来源任务关联",
            "action": "keep",
            "file_path": None,
            "artifact_id": None,
            "can_open": False,
            "can_reveal": False,
        })
    media_item = item_by_key.get(_path_key(str(asset["archive_path"]))) if asset["archive_path"] else None
    clear_asset_archive_path = bool(
        sibling_task_count == 0
        and media_item is not None
        and media_item["status"] in {"将删除", "文件不存在"}
    )
    items.append({
        "name": "素材来源与身份信息",
        "kind": "asset_record",
        "size_bytes": None,
        "relative_path": "素材档案数据库",
        "full_path": f"素材档案数据库 / {asset['id']}",
        "status": "保留：用于其他任务和后续素材去重",
        "reason": (
            "素材身份信息保留；媒体文件删除后会清除旧归档路径"
            if clear_asset_archive_path
            else "此操作删除单次任务；素材身份信息不会随之清除"
        ),
        "action": "keep",
        "file_path": None,
        "artifact_id": None,
        "can_open": False,
        "can_reveal": False,
    })
    for row in batch_rows:
        items.append({
            "name": f"视频发布任务：{str(row['media_name'] or '视频')}",
            "kind": "video_publish_batch",
            "size_bytes": None,
            "relative_path": "视频发布记录",
            "full_path": f"视频发布记录 / {row['id']}",
            "status": "保留：发布记录与发布媒体不受影响",
            "reason": f"发布状态：{row['status']}",
            "action": "keep",
            "file_path": None,
            "artifact_id": None,
            "can_open": False,
            "can_reveal": False,
        })

    task_state_active = str(task["status"]) in {"queued", "running", "interrupted", "awaiting_authorization"}
    has_pending_steps = int(task_status["total"]) > 0
    can_stop = bool(runner_active or task_state_active)
    blocked_reason = None
    if runner_active and not task_state_active and not has_pending_steps:
        blocked_reason = "停止请求已发出，后台步骤仍在收尾；完成后请刷新预览。"
    elif task_state_active or runner_active:
        blocked_reason = "任务仍有排队或执行中的步骤，暂不能删除。请先停止任务并刷新预览。"
    elif (artifacts or asset["archive_path"]) and root is None:
        blocked_reason = "归档目录当前不可用，无法核实这条任务的文件范围。请恢复归档目录后再删除。"
    file_count = sum(1 for item in items if item["action"] == "delete" and item["file_path"])
    deleted_bytes = sum(int(item["size_bytes"] or 0) for item in items if item["action"] == "delete")
    digest_payload = {
        "task": [str(task["id"]), str(task["status"]), str(task["updated_at"]), task["archive_removed_at"]],
        "files": [
            [item["full_path"], item["size_bytes"], item.get("mtime_ns"), item["status"], item["action"]]
            for item in items if item.get("file_path")
        ],
        "comments": [str(row["id"]) + ("s" if bool(row["shared"]) else "d") for row in comment_rows],
        "articles": [str(row["id"]) for row in linked_articles],
        "batches": [str(row["id"]) for row in batch_rows],
        "sibling_task_count": sibling_task_count,
        "clear_asset_archive_path": clear_asset_archive_path,
        "asset_archive_path": str(archive_path_value or ""),
        "runner_active": runner_active,
        "can_stop": can_stop,
        "blocked_reason": blocked_reason,
    }
    token = hashlib.sha256(json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "task_id": task_id,
        "asset_id": str(asset["id"]),
        "clear_asset_archive_path": clear_asset_archive_path,
        "title": str(asset["title"] or "未命名素材"),
        "status": str(task["status"]),
        "items": _numbered(items),
        "token": token,
        "blocked_reason": blocked_reason,
        "can_stop": can_stop,
        "delete_file_count": int(file_count),
        "delete_bytes": int(deleted_bytes),
        "keep_file_count": sum(item.get("file_path") is not None and item["action"] == "keep" for item in items),
        "deletable_comment_ids": deletable_comment_ids,
        "shared_comment_count": shared_comment_count,
    }


def _file_item(
    connection: sqlite3.Connection,
    task_id: str,
    descriptor: dict[str, Any],
    root: Path | None,
    linked_articles: list[sqlite3.Row],
    batches: list[sqlite3.Row],
    asset_media_path: str,
    sibling_task_count: int,
) -> dict[str, Any]:
    path = Path(str(descriptor["path"]))
    full_path = str(path)
    relative_path = full_path
    inside_archive = False
    status = "保留：路径无法安全确认"
    reason = "路径未通过归档目录安全校验"
    action = "keep"
    size_bytes: int | None = None
    mtime_ns: int | None = None
    resolved: Path | None = None
    if root is not None:
        try:
            if _has_symlink_component(path, root):
                raise ValueError("symbolic link component")
            resolved = _assert_contained(path, root)
            inside_archive = True
            relative_path = resolved.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError("symbolic link")
            if not resolved.exists():
                status, reason = "文件不存在", "仅删除数据库记录"
            elif not resolved.is_file():
                status, reason = "保留：不是普通文件", "避免删除目录或特殊文件"
            else:
                stat_result = resolved.stat()
                size_bytes = int(stat_result.st_size)
                mtime_ns = int(stat_result.st_mtime_ns)
                other_rows = connection.execute(
                    "SELECT task_id, path FROM artifacts WHERE task_id <> ?", (task_id,)
                ).fetchall()
                other_shared = any(
                    _same_path(str(row["path"]), str(descriptor["path"])) for row in other_rows
                )
                if descriptor["artifact_ids"]:
                    article_placeholders = ",".join("?" for _ in descriptor["artifact_ids"])
                    article_ref = connection.execute(
                        f"SELECT count(*) AS total FROM articles WHERE source_artifact_id IN ({article_placeholders})",
                        tuple(descriptor["artifact_ids"]),
                    ).fetchone()
                else:
                    article_ref = {"total": 0}
                batch_refs = [str(row["id"]) for row in batches if _same_path(str(row["media_path"]), str(descriptor["path"]))]
                if other_shared:
                    status, reason = "保留：被其他任务共享", "另有任务登记了同一路径"
                elif sibling_task_count and asset_media_path and _same_path(str(descriptor["path"]), asset_media_path):
                    status, reason = "保留：同素材的其他任务仍在使用", "视频文件属于共享素材记录"
                elif int(article_ref["total"]):
                    status, reason = "保留：文章仍引用此文件", "删除任务不会删除文章及其历史"
                elif batch_refs:
                    status, reason = "保留：发布记录仍引用此文件", "发布记录和媒体文件会保留"
                else:
                    status, reason, action = "将删除", "此文件只属于当前任务且位于归档目录内", "delete"
        except (OSError, RuntimeError, ValueError):
            if not _lexically_within(path, root):
                status, reason = "保留：归档目录以外的文件", "外部导出副本由用户管理，不会自动删除"
            else:
                status, reason = "保留：路径无法安全确认", "路径包含符号链接或无法通过安全校验"
    if resolved is None and not path.is_symlink():
        try:
            if path.is_file():
                stat_result = path.stat()
                size_bytes = int(stat_result.st_size)
                mtime_ns = int(stat_result.st_mtime_ns)
        except OSError:
            pass
    kind = str(descriptor["kind"])
    return {
        "name": path.name,
        "kind": kind,
        "kind_label": _KIND_LABELS.get(kind, kind.replace("_", " ")),
        "size_bytes": size_bytes,
        "relative_path": relative_path,
        "full_path": full_path,
        "status": status,
        "reason": reason,
        "action": action,
        "file_path": (str(resolved) if action == "delete" and resolved is not None else full_path) if size_bytes is not None else None,
        "artifact_id": descriptor["artifact_ids"][0] if descriptor["artifact_ids"] else None,
        "artifact_ids": descriptor["artifact_ids"],
        "can_open": inside_archive and resolved is not None and size_bytes is not None and path.suffix.casefold() in _OPENABLE_EXTENSIONS,
        "can_reveal": inside_archive and resolved is not None and size_bytes is not None,
        "inside_archive": inside_archive,
        "mtime_ns": mtime_ns,
    }


def _unknown_item(path: Path, root: Path, *, unsafe: bool) -> dict[str, Any]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = str(path)
    size: int | None = None
    mtime_ns: int | None = None
    try:
        if not unsafe:
            info = path.stat()
            if path.is_file():
                size, mtime_ns = int(info.st_size), int(info.st_mtime_ns)
    except OSError:
        pass
    return {
        "name": path.name,
        "kind": "unregistered",
        "kind_label": "未登记文件",
        "size_bytes": size,
        "relative_path": relative,
        "full_path": str(path),
        "status": "保留：无法确认归属" if not unsafe else "保留：路径包含符号链接",
        "reason": "非工作台登记产物，不会自动删除",
        "action": "keep",
        "file_path": str(path) if size is not None and not unsafe else None,
        "artifact_id": None,
        "artifact_ids": [],
        "can_open": False,
        "can_reveal": not unsafe and path.is_file(),
        "inside_archive": True,
        "mtime_ns": mtime_ns,
    }


def _numbered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"index": index + 1, **item} for index, item in enumerate(items)]


def _safe_root(value: Path | None) -> Path | None:
    if value is None:
        return None
    try:
        if value.is_symlink():
            return None
        resolved = value.resolve(strict=True)
        return resolved if resolved.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _task_archive_directory(archive_path_value: object, root: Path | None) -> Path | None:
    if root is None or not isinstance(archive_path_value, str) or not archive_path_value:
        return None
    path = Path(archive_path_value)
    try:
        if _has_symlink_component(path, root):
            return None
        safe_path = _assert_contained(path, root)
        directory = safe_path if safe_path.is_dir() else safe_path.parent
        relative = directory.relative_to(root)
        if len(relative.parts) < 2 or _has_symlink_component(directory, root):
            return None
        safe_directory = _assert_contained(directory, root, strict=True)
        if safe_directory.is_symlink() or not safe_directory.is_dir():
            return None
        return safe_directory
    except (OSError, RuntimeError, ValueError):
        return None


def _remove_empty_archive_directory(directory: Path, root: Path) -> bool:
    try:
        if _has_symlink_component(directory, root):
            return False
        safe_directory = _assert_contained(directory, root, strict=True)
        if safe_directory.is_symlink() or not safe_directory.is_dir():
            return False
        relative = safe_directory.relative_to(root)
        if len(relative.parts) < 2:
            return False
    except (OSError, RuntimeError, ValueError):
        return False

    for current, dir_names, _file_names in os.walk(safe_directory, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in dir_names:
            child = current_path / name
            if child.is_symlink():
                continue
            try:
                if not _has_symlink_component(child, root):
                    child.rmdir()
            except OSError:
                pass
    try:
        safe_directory.rmdir()
    except OSError:
        return False
    return True


def _remove_empty_date_directory(directory: Path, root: Path) -> None:
    try:
        if _has_symlink_component(directory, root):
            return
        safe_directory = _assert_contained(directory, root, strict=True)
        relative = safe_directory.relative_to(root)
        if (
            safe_directory.is_symlink()
            or not safe_directory.is_dir()
            or len(relative.parts) != 1
            or not _date_directory(safe_directory.name)
        ):
            return
        safe_directory.rmdir()
    except (OSError, RuntimeError, ValueError):
        pass


def _date_directory(name: str) -> bool:
    try:
        return datetime.strptime(name, "%Y-%m-%d").strftime("%Y-%m-%d") == name
    except ValueError:
        return False


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _same_path(left: str, right: str) -> bool:
    return _path_key(left) == _path_key(right)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _has_symlink_component(candidate: Path, root: Path) -> bool:
    lexical = candidate.absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _lexically_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.absolute().relative_to(root)
        return True
    except ValueError:
        return False


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
