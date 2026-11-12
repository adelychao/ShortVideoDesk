"""Original-path registry for collection media selected from the local machine.

素材采集的本地媒体不进入上传暂存：选择文件后只校验并登记原文件路径，
任务导入步骤直接基于原文件工作，归档副本仍由 import_media 写入。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .database import Database
from .steps.import_media import (
    MAX_SOFT_LIMIT_BYTES,
    _ALLOWED_EXTENSIONS,
    _validate_media_header,
)
from .uploads import UploadError, UploadReceipt, _hash_upload, clean_upload_filename
from .video_publishing import _has_reparse_point


_SOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})


class CollectionMediaError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 422,
        *,
        size_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.size_bytes = size_bytes


def _format_size(size: int) -> str:
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def register_collection_media_source(
    database: Database,
    path: Path,
    *,
    confirm_over_limit: bool = False,
) -> dict[str, object]:
    """Validate a local media file in place and record its original path."""
    if not isinstance(confirm_over_limit, bool):
        raise CollectionMediaError("invalid", "大文件确认状态无效。")
    try:
        if not path.is_absolute() or _has_reparse_point(path) or not path.is_file():
            raise ValueError("invalid file")
        path = path.resolve(strict=True)
        filename, extension = clean_upload_filename(path.name)
        if extension not in _ALLOWED_EXTENSIONS:
            raise CollectionMediaError(
                "unsupported", "请选择 MP4、MOV、MKV、WEBM、MP3、M4A 或 WAV 文件。"
            )
        _validate_media_header(path, extension)
        digest, size = _hash_upload(path)
        if size <= 0:
            raise ValueError("empty media")
        if size > MAX_SOFT_LIMIT_BYTES and not confirm_over_limit:
            raise CollectionMediaError(
                "over_limit",
                f"文件大小为 {_format_size(size)}，超过 2 GB 软上限，请确认后重新选择。",
                409,
                size_bytes=size,
            )
        mtime = path.stat().st_mtime_ns
    except CollectionMediaError:
        raise
    except UploadError as exc:
        # 文件名或扩展名不合规时保留具体提示（如“请选择 MP4、MOV…”）。
        raise CollectionMediaError("unsupported", str(exc), 422) from exc
    except (OSError, ValueError) as exc:
        raise CollectionMediaError(
            "invalid", "无法读取所选文件，请检查文件格式和访问权限。", 409
        ) from exc
    source_id = uuid.uuid4().hex
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO collection_media_sources VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, str(path), filename, size, digest, mtime, int(confirm_over_limit)),
        )
    return collection_media_source_payload(
        get_collection_media_source(database, source_id)
    )


def get_collection_media_source(
    database: Database,
    source_id: str,
    *,
    verify_hash: bool = False,
) -> UploadReceipt:
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise CollectionMediaError("invalid", "媒体记录无效，请重新选择文件。", 409)
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM collection_media_sources WHERE id = ?", (source_id,)
        ).fetchone()
    if row is None:
        raise CollectionMediaError("missing", "媒体记录不存在，请重新选择文件。", 409)
    path = Path(str(row["path"]))
    try:
        if _has_reparse_point(path) or not path.is_file():
            raise ValueError("missing")
        info = path.stat()
        if info.st_size != row["size_bytes"] or info.st_mtime_ns != row["mtime_ns"]:
            raise ValueError("changed")
        if verify_hash and _hash_upload(path) != (row["sha256"], row["size_bytes"]):
            raise ValueError("changed")
    except (OSError, ValueError, UploadError) as exc:
        raise CollectionMediaError(
            "changed", "所选文件已移动、删除或发生变化，请重新选择文件。", 409
        ) from exc
    extension = Path(str(row["filename"])).suffix.casefold()
    media_type = "video" if extension in _VIDEO_EXTENSIONS else "audio"
    return UploadReceipt(
        source_id,
        str(row["filename"]),
        int(row["size_bytes"]),
        str(row["sha256"]),
        media_type,
        bool(row["confirmed_over_limit"]),
        path,
    )


def collection_media_source_payload(receipt: UploadReceipt) -> dict[str, object]:
    return {**receipt.to_payload(), "sourceType": "local", "sourcePath": str(receipt.path)}


def delete_collection_media_source(database: Database, source_id: str) -> bool:
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        return False
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            "DELETE FROM collection_media_sources WHERE id = ?", (source_id,)
        )
        return cursor.rowcount > 0
