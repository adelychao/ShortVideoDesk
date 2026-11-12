"""Private staging for user-selected local media uploads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import PROJECT_ROOT
from .steps.import_media import (
    CHUNK_SIZE,
    MAX_SOFT_LIMIT_BYTES,
    _ALLOWED_EXTENSIONS,
    _validate_media_header,
)


_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SAFE_FILENAME_PATTERN = re.compile(r"[^\w .()\-\u4e00-\u9fff]", re.UNICODE)
UPLOAD_ROOT = PROJECT_ROOT / "data" / "workbench" / "uploads"


@dataclass(frozen=True)
class UploadReceipt:
    upload_id: str
    filename: str
    size_bytes: int
    sha256: str
    detected_media_type: str
    confirmed_over_limit: bool
    path: Path

    def to_payload(self) -> dict[str, object]:
        return {
            "uploadId": self.upload_id,
            "filename": self.filename,
            "sizeBytes": self.size_bytes,
            "sha256": self.sha256,
            "detectedMediaType": self.detected_media_type,
        }


class UploadError(ValueError):
    """Raised for malformed or unsupported staged uploads."""


def clean_upload_filename(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise UploadError("请选择一个有效的媒体文件。")
    basename = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    extension = Path(basename).suffix.casefold()
    if extension not in _ALLOWED_EXTENSIONS:
        raise UploadError("文件扩展名不受支持。请选择 MP4、MOV、MKV、WEBM、MP3、M4A 或 WAV。")
    stem = Path(basename).stem
    stem = _SAFE_FILENAME_PATTERN.sub("_", stem).strip(" ._")[:160].rstrip(" .") or "本地媒体"
    return f"{stem}{extension}", extension


async def save_upload(upload_file: Any, *, confirm_over_limit: bool) -> UploadReceipt:
    """Stream a multipart upload to disk while calculating its digest."""
    filename, extension = clean_upload_filename(getattr(upload_file, "filename", None))
    if not isinstance(confirm_over_limit, bool):
        raise UploadError("大文件确认状态无效。")
    reported_size = getattr(upload_file, "size", None)
    if (
        isinstance(reported_size, int)
        and not isinstance(reported_size, bool)
        and reported_size > MAX_SOFT_LIMIT_BYTES
        and not confirm_over_limit
    ):
        raise UploadError("文件超过 2 GB 软上限，请确认后重新上传。")
    root = _prepare_upload_root()
    upload_id = secrets.token_hex(16)
    directory = root / upload_id
    try:
        directory.mkdir()
        _assert_upload_contained(directory, root, strict=True)
        target = directory / f"media{extension}"
        digest = hashlib.sha256()
        size_bytes = 0
        with target.open("xb") as output:
            while True:
                chunk = await upload_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_SOFT_LIMIT_BYTES and not confirm_over_limit:
                    raise UploadError("文件超过 2 GB 软上限，请确认后重新上传。")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        _assert_upload_contained(target, root, strict=True)
        if target.is_symlink() or not target.is_file() or size_bytes <= 0:
            raise UploadError("媒体文件为空或无法读取。")
        if isinstance(reported_size, int) and size_bytes != reported_size:
            raise UploadError("上传文件大小校验失败，请重新选择文件。")

        detected_media_type = await _validate_upload_media(target, extension)
        receipt_data = {
            "version": 1,
            "upload_id": upload_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
            "detected_media_type": detected_media_type,
            "confirmed_over_limit": confirm_over_limit,
            "extension": extension,
        }
        receipt_path = directory / "receipt.json"
        receipt_tmp = directory / f".receipt-{secrets.token_hex(8)}.tmp"
        _assert_upload_contained(receipt_tmp, root)
        with receipt_tmp.open("xb") as receipt_file:
            receipt_file.write(json.dumps(receipt_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
        os.replace(receipt_tmp, receipt_path)
        _assert_upload_contained(receipt_path, root, strict=True)
        return UploadReceipt(
            upload_id=upload_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
            detected_media_type=detected_media_type,
            confirmed_over_limit=confirm_over_limit,
            path=target.resolve(strict=True),
        )
    except BaseException:
        _remove_owned_upload(upload_id)
        raise


async def save_upload_from_path(
    source: Path,
    *,
    filename: str,
    confirm_over_limit: bool,
) -> UploadReceipt:
    """Copy a previously validated managed file into ordinary upload staging."""
    import asyncio

    if source.is_symlink() or not source.is_file():
        raise UploadError("本地草稿视频文件不可用，请重新上传。")
    source = source.resolve(strict=True)
    expected_sha, expected_size = _hash_upload(source)
    with source.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise UploadError("本地草稿视频文件不可用，请重新上传。")

        class _PathReader:
            def __init__(self) -> None:
                self.filename = filename
                self.size = expected_size

            async def read(self, size: int = -1) -> bytes:
                return await asyncio.to_thread(stream.read, size)

        receipt = await save_upload(_PathReader(), confirm_over_limit=confirm_over_limit)
        after = os.fstat(stream.fileno())
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or receipt.size_bytes != expected_size
        or receipt.sha256 != expected_sha
    ):
        delete_upload(receipt.upload_id)
        raise UploadError("本地草稿视频文件在恢复期间发生变化，请重新打开草稿。")
    return receipt


def get_upload(upload_id: str, *, verify_hash: bool = True) -> UploadReceipt:
    if not isinstance(upload_id, str) or not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise UploadError("上传凭据无效或已过期，请重新选择文件。")
    root = _prepare_upload_root()
    directory = root / upload_id
    _assert_upload_contained(directory, root, strict=True)
    if directory.is_symlink() or not directory.is_dir():
        raise UploadError("上传凭据无效或已过期，请重新选择文件。")
    receipt_path = directory / "receipt.json"
    _assert_upload_contained(receipt_path, root, strict=True)
    if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 32 * 1024:
        raise UploadError("上传凭据无效或已过期，请重新选择文件。")
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise UploadError("上传凭据无效或已过期，请重新选择文件。") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("upload_id") != upload_id:
        raise UploadError("上传凭据无效或已过期，请重新选择文件。")
    filename, extension = clean_upload_filename(data.get("filename"))
    size_bytes = data.get("size_bytes")
    digest = data.get("sha256")
    media_type = data.get("detected_media_type")
    confirmed = data.get("confirmed_over_limit")
    if (
        filename != data.get("filename")
        or data.get("extension") != extension
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or media_type not in {"video", "audio"}
        or not isinstance(confirmed, bool)
        or media_type != ("video" if extension in {".mp4", ".mov", ".mkv", ".webm"} else "audio")
        or (size_bytes > MAX_SOFT_LIMIT_BYTES and not confirmed)
    ):
        raise UploadError("上传凭据无效或已过期，请重新选择文件。")
    media_path = directory / f"media{extension}"
    _assert_upload_contained(media_path, root, strict=True)
    if media_path.is_symlink() or not media_path.is_file() or media_path.stat().st_size != size_bytes:
        raise UploadError("上传文件已变化，请重新选择文件。")
    if verify_hash:
        actual_digest, actual_size = _hash_upload(media_path)
        if actual_size != size_bytes or actual_digest != digest:
            raise UploadError("上传文件完整性校验失败，请重新选择文件。")
    return UploadReceipt(
        upload_id=upload_id,
        filename=filename,
        size_bytes=size_bytes,
        sha256=digest,
        detected_media_type=media_type,
        confirmed_over_limit=confirmed,
        path=media_path.resolve(strict=True),
    )


def delete_upload(upload_id: str) -> bool:
    """Remove only the known files owned by an upload receipt."""
    if not isinstance(upload_id, str) or not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
        return False
    try:
        root = _prepare_upload_root()
        directory = root / upload_id
        if directory.is_symlink() or not directory.is_dir():
            return False
        _assert_upload_contained(directory, root, strict=True)
        for child in directory.iterdir():
            if child.is_symlink() or not child.is_file() or child.name not in {"receipt.json"} and not re.fullmatch(r"media\.(mp4|mov|mkv|webm|mp3|m4a|wav)", child.name):
                return False
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        return True
    except (OSError, UploadError):
        return False


def _prepare_upload_root() -> Path:
    root = UPLOAD_ROOT
    project = PROJECT_ROOT.resolve(strict=True)
    try:
        root.relative_to(project)
    except ValueError as exc:
        raise UploadError("本地上传暂存目录配置无效。") from exc
    current = project
    for part in root.relative_to(project).parts:
        current = current / part
        if current.is_symlink():
            raise UploadError("本地上传暂存目录不能包含符号链接。")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise UploadError("本地上传暂存目录不可用。")
    resolved = root.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise UploadError("本地上传暂存目录配置无效。") from exc
    return resolved


def _assert_upload_contained(path: Path, root: Path, *, strict: bool = False) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=strict)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UploadError("上传路径超出本机暂存目录。") from exc
    if resolved == resolved_root:
        raise UploadError("上传路径无效。")
    return resolved


async def _validate_upload_media(path: Path, extension: str) -> str:
    import asyncio

    try:
        return await asyncio.to_thread(_validate_media_header, path, extension)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        raise UploadError("文件内容与所选媒体类型不匹配或文件无法解码。") from exc


def _hash_upload(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise UploadError("上传文件不可读取。")
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(source.fileno())
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise UploadError("上传文件在校验期间发生变化。")
    return digest.hexdigest(), total


def _remove_owned_upload(upload_id: str) -> None:
    try:
        root = _prepare_upload_root()
        directory = root / upload_id
        if directory.is_symlink() or not directory.is_dir():
            return
        _assert_upload_contained(directory, root, strict=True)
        for child in directory.iterdir():
            if child.is_symlink() or not child.is_file():
                return
            child.unlink()
        directory.rmdir()
    except (OSError, UploadError):
        return
