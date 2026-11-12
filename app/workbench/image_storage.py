"""登记工作台管理的图片，保留每个文件实际写入的目录。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import AppConfig, validate_directory
from .database import Database, PROJECT_ROOT


IMAGE_ID = re.compile(r"[0-9a-f]{32}")
IMAGE_FILENAME = re.compile(r"([0-9a-f]{32})(\.(?:jpe?g|png|webp))")
VIDEO_COVER_LEASE_SECONDS = 24 * 60 * 60


def image_directory(database: Database, setting: str) -> Path:
    configured = AppConfig(database).get_settings()[setting]
    if not isinstance(configured, str):
        raise ValueError("图片保存目录配置无效。")
    return validate_directory(configured, create_missing=True)


def _checked_path(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("图片路径不能使用符号链接或目录联接。")
    if path.resolve(strict=True) != path:
        raise ValueError("图片路径已变化。")
    return path


def registered_image_path(row: sqlite3.Row) -> Path:
    root = _checked_path(Path(row["directory"]))
    filename = f'{row["id"]}{row["suffix"]}'
    if not IMAGE_FILENAME.fullmatch(filename):
        raise ValueError("图片文件名无效。")
    if row["article_id"] is not None:
        if not IMAGE_ID.fullmatch(row["article_id"]):
            raise ValueError("文章标识无效。")
        root = _checked_path(root / row["article_id"])
    path = _checked_path(root / filename)
    if not path.is_file() or path.stat().st_size != row["size_bytes"]:
        raise ValueError("图片文件不可用或已变化。")
    return path


@contextmanager
def store_image(
    database: Database,
    directory: Path,
    *,
    original_name: str,
    suffix: str,
    content: bytes,
    article_id: str | None = None,
) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row]]:
    resource_id = uuid.uuid4().hex
    if not content or not IMAGE_FILENAME.fullmatch(resource_id + suffix):
        raise ValueError("图片为空或格式不受支持。")
    if article_id is not None and not IMAGE_ID.fullmatch(article_id):
        raise ValueError("文章标识无效。")
    root = _checked_path(directory)
    folder = root if article_id is None else root / article_id
    folder.mkdir(exist_ok=True)
    _checked_path(folder)
    digest = hashlib.sha256(content).hexdigest()
    created: Path | None = None
    try:
        with database.transaction(write=True) as connection:
            matches = connection.execute(
                """SELECT * FROM workbench_images
                   WHERE article_id IS ? AND sha256 = ? AND directory = ? AND suffix = ?""",
                (article_id, digest, str(root), suffix),
            ).fetchall()
            for row in matches:
                try:
                    if hashlib.sha256(registered_image_path(row).read_bytes()).hexdigest() != digest:
                        continue
                except (OSError, ValueError, RuntimeError):
                    continue
                if article_id is None:
                    connection.execute(
                        "UPDATE workbench_images SET keep_until = ? WHERE id = ?",
                        (time.time() + VIDEO_COVER_LEASE_SECONDS, row["id"]),
                    )
                yield connection, row
                return
            path = folder / (resource_id + suffix)
            with path.open("xb") as output:
                created = path
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            connection.execute(
                """INSERT INTO workbench_images
                   (id, kind, article_id, directory, original_name, suffix, size_bytes, sha256, keep_until)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (resource_id, "video_cover" if article_id is None else "article_image", article_id,
                 str(root), original_name, suffix, len(content), digest, time.time() + VIDEO_COVER_LEASE_SECONDS),
            )
            row = connection.execute("SELECT * FROM workbench_images WHERE id = ?", (resource_id,)).fetchone()
            yield connection, row
    except BaseException:
        if created is not None:
            try:
                _checked_path(created).unlink()
            except (OSError, ValueError, RuntimeError):
                pass
        raise


def resolve_article_image(
    database: Database | None, article_id: str, reference: str, *, project_root: Path = PROJECT_ROOT,
) -> Path | None:
    prefix = f"data/article-assets/{article_id}/"
    if not IMAGE_ID.fullmatch(article_id) or not reference.startswith(prefix):
        return None
    filename = reference.removeprefix(prefix)
    match = IMAGE_FILENAME.fullmatch(filename)
    if match is None:
        return None
    try:
        if database is not None:
            with database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM workbench_images WHERE id = ? AND article_id = ? AND kind = 'article_image'",
                    (match[1], article_id),
                ).fetchone()
            if row is not None:
                return registered_image_path(row) if row["suffix"] == match[2] else None
        folder = _checked_path(project_root / "data" / "article-assets" / article_id)
        path = _checked_path(folder / filename)
        return path if path.is_file() and path.stat().st_size <= 10_000_000 else None
    except (OSError, ValueError, RuntimeError):
        return None


def remove_registered_image(row: sqlite3.Row) -> int:
    # 删除按登记位置执行，图片根目录离线时保留登记，供后续重试。
    root = _checked_path(Path(row["directory"]))
    filename = f'{row["id"]}{row["suffix"]}'
    if not IMAGE_FILENAME.fullmatch(filename):
        raise ValueError("图片文件名无效。")
    if row["article_id"] is not None:
        if not IMAGE_ID.fullmatch(row["article_id"]):
            raise ValueError("文章标识无效。")
        root = root / row["article_id"]
    try:
        path = _checked_path(root / filename)
    except FileNotFoundError:
        return 0
    if not path.is_file():
        raise ValueError("图片文件不可用。")
    size = path.stat().st_size
    path.unlink()
    if row["article_id"] is not None:
        try:
            root.rmdir()
        except OSError:
            pass
    return size
