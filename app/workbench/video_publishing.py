"""Local video distribution batches and the isolated social-auto-upload adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..errors import SiteIssueCode, classify_site_issue
from ..browser_safety import BrowserSafetyError, browser_automation_paused, is_windows_platform, require_safe_browser, require_controlled_video_browser, reserve_xiaohongshu_login, xiaohongshu_login_slot
from .archive import _assert_contained
from .bilibili_login import BilibiliLoginError, bilibili_identity, check_bilibili_login, run_bilibili_login
from .database import Database, PROJECT_ROOT
from .image_storage import (
    IMAGE_FILENAME, VIDEO_COVER_LEASE_SECONDS, image_directory, registered_image_path,
    remove_registered_image, store_image,
)
from .account_workbench_browser import AccountWorkbenchBrowserManager, PLATFORM_WORKBENCH_URLS
from .uploads import UploadError, UploadReceipt, _hash_upload, clean_upload_filename, delete_upload, get_upload, save_upload_from_path
from .steps.import_media import _validate_media_header


VIDEO_PLATFORMS: tuple[dict[str, object], ...] = (
    {"id": "douyin", "cli_id": "douyin", "name": "抖音", "schedule": True, "tid_required": False, "max_tags": 5},
    {"id": "xiaohongshu", "cli_id": "xiaohongshu", "name": "小红书", "schedule": True, "tid_required": False, "max_tags": 10},
    {"id": "kuaishou", "cli_id": "kuaishou", "name": "快手", "schedule": True, "tid_required": False, "max_tags": 4},
    {"id": "bilibili", "cli_id": "bilibili", "name": "哔哩哔哩", "schedule": True, "tid_required": True, "max_tags": 10},
    {"id": "wechat_channels", "cli_id": "tencent", "name": "微信视频号", "schedule": True, "tid_required": False, "max_tags": 30},
    {"id": "baijiahao", "cli_id": "baijiahao", "name": "百家号", "schedule": True, "tid_required": False, "max_tags": 30},
    {"id": "weibo", "cli_id": "weibo", "name": "微博", "schedule": False, "tid_required": False, "max_tags": 30},
    {"id": "zhihu", "cli_id": "zhihu", "name": "知乎", "schedule": False, "tid_required": False, "max_tags": 0, "article_only": True},
)
_PLATFORM_BY_ID = {str(item["id"]): item for item in VIDEO_PLATFORMS}
_SUPPORTED_LOGIN_PLATFORMS = frozenset({'douyin', 'xiaohongshu', 'bilibili', 'kuaishou', 'wechat_channels', 'baijiahao', 'zhihu'})
_WORKBENCH_SESSION_PREFIXES = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "kuaishou": "kuaishou",
    "bilibili": "bilibili",
    "wechat_channels": "tencent",
    "baijiahao": "baijiahao",
    "zhihu": "zhihu",
}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_ACCOUNT_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_VIDEO_PUBLISH_HEARTBEAT_SECONDS = 15
_XHS_MANUAL_SUBMIT_CODE = "xhs_manual_submit"
_VIDEO_PUBLISH_HEARTBEAT_STALE_SECONDS = 45
_VIDEO_PUBLISH_STALL_SECONDS = 5 * 60
_VIDEO_PUBLISH_RETRY_WARNING_COUNT = 3
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SECRET_PATTERN = re.compile(r"(?i)(token|signature|sign|auth|authorization|api[_-]?key|cookie|secret)=([^&\s]+)")
_PUBLISH_MEDIA_ROOT = PROJECT_ROOT / "data" / "workbench" / "video-publish"
_PUBLISH_COVER_UPLOAD_ROOT = PROJECT_ROOT / "data" / "workbench" / "video-publish-cover-uploads"
_VIDEO_PUBLISH_DRAFT_ROOT = PROJECT_ROOT / "data" / "workbench" / "video-publish-drafts"
_PUBLISH_COVER_ID = re.compile(r"^[0-9a-f]{32}$")
_VIDEO_PUBLISH_DRAFT_ID = re.compile(r"^[0-9a-f]{32}$")
_VIDEO_PUBLISH_DRAFT_SLOT = re.compile(r"^(?:common|douyin|xiaohongshu|kuaishou|bilibili|baijiahao|weibo|wechat_channels):(single|portrait|landscape|wide|square|tall|original)$")
_PUBLISH_BATCH_COVER_FILENAME = re.compile(
    r"^(?:platform-cover-[0-9a-f]{32}|douyin-cover-(?:portrait|landscape))\.(?:jpe?g|png|webp)$"
)
_BATCH_COVER_KEYS = (
    "cover_filename", "cover_4_3_filename", "cover_16_9_filename",
    "cover_portrait_filename", "cover_landscape_filename",
)
_VIDEO_PUBLISH_MEDIA_TERMINAL_STATUSES = frozenset({"published", "failed", "cancelled"})
_VIDEO_PUBLISH_CLEANUP_SETTING_KEYS = ("video_publish_auto_cleanup_enabled", "video_publish_retention_days")
_COVER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_DOUYIN_DECLARATIONS = {
    "内容由AI生成",
    "内容为个人观点或见解",
    "内容为转载信息",
    "内容含营销推广信息",
    "虚拟演绎，仅供娱乐",
    "虚构演绎，仅供娱乐",
    "无需添加自主声明",
}
_MAX_COVER_BYTES = 20 * 1024 * 1024
_BAIJIAHAO_DECLARATIONS = frozenset({
    "无需声明", "含AI生成内容", "内容为转载", "含虚构演绎内容", "内容含营销信息", "个人观点，仅供参考",
})
_MAX_TARGETS = 20
_UPLOAD_TIMEOUT_SECONDS = 4 * 60 * 60
_LOGGER = logging.getLogger(__name__)
_DUPLICATE_ACCOUNT_MESSAGE = '该账号已添加，无需重复登录。'


class VideoPublishError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def register_local_video_source(database: Database, path: Path) -> dict[str, object]:
    try:
        if not path.is_absolute() or _has_reparse_point(path) or not path.is_file():
            raise ValueError("invalid file")
        path = path.resolve(strict=True)
        filename, extension = clean_upload_filename(path.name)
        if extension not in _VIDEO_SUFFIXES:
            raise VideoPublishError("video_invalid", "请选择支持的视频文件。")
        if _validate_media_header(path, extension) != "video":
            raise ValueError("invalid video")
        digest, size = _hash_upload(path)
        if size <= 0:
            raise ValueError("empty video")
        mtime = path.stat().st_mtime_ns
    except VideoPublishError:
        raise
    except (OSError, ValueError, UploadError) as exc:
        raise VideoPublishError("local_video_invalid", "无法读取所选视频，请检查文件格式和访问权限。", 409) from exc
    source_id = uuid.uuid4().hex
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO video_publish_local_sources VALUES (?, ?, ?, ?, ?, ?)",
                           (source_id, str(path), filename, size, digest, mtime))
    return local_video_source_payload(get_local_video_source(database, source_id))


def get_local_video_source(database: Database, source_id: str, *, verify_hash: bool = False) -> UploadReceipt:
    if not _PUBLISH_COVER_ID.fullmatch(source_id or ""):
        raise VideoPublishError("local_video_invalid", "视频记录无效，请重新选择原创视频。", 409)
    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM video_publish_local_sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise VideoPublishError("local_video_missing", "视频记录不存在，请重新选择原创视频。", 409)
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
        raise VideoPublishError("local_video_changed", "原创视频文件已移动、删除或发生变化，请重新选择视频。", 409) from exc
    return UploadReceipt(source_id, str(row["filename"]), int(row["size_bytes"]), str(row["sha256"]), "video", False, path)


def local_video_source_payload(receipt: UploadReceipt) -> dict[str, object]:
    return {**receipt.to_payload(), "sourceType": "local", "sourcePath": str(receipt.path),
            "previewUrl": f"/api/v1/video-publish/local-sources/{receipt.upload_id}/media"}


def validate_douyin_repost_options(options: dict[str, Any]) -> None:
    source = options.get("repost_source")
    title = str(options.get("repost_title") or "").strip()
    work_id = str(options.get("repost_id") or "")
    if options.get("declaration") == "内容为转载信息":
        if source not in {"inside", "outside"}:
            raise VideoPublishError("douyin_repost_source_required", "抖音转载声明请先选择取材站内或取材站外。")
        if source == "inside" and (not title or len(title) > 500 or not re.fullmatch(r"\d{5,30}", work_id)):
            raise VideoPublishError("douyin_repost_title_required", "取材站内请为每个抖音账号选择有效的原作品。")
        if source == "outside" and (title or work_id):
            raise VideoPublishError("douyin_repost_title_invalid", "取材站外无需选择站内原作品。")
        if title:
            options["repost_title"] = title
    elif source or title or work_id:
        raise VideoPublishError("douyin_repost_invalid", "只有转载声明可以选择取材来源。")


def validate_douyin_location_options(options: dict[str, Any]) -> None:
    name = str(options.get("location") or "").strip()
    poi_id = str(options.get("location_id") or "")
    address = str(options.get("location_address") or "").strip()
    city_code = str(options.get("location_city_code") or "")
    city_name = str(options.get("location_city_name") or "").strip()
    if not any((name, poi_id, address, city_code, city_name)):
        return
    if not name or len(name) > 100 or not re.fullmatch(r"\d{5,30}", poi_id) or not address or len(address) > 300 or not re.fullmatch(r"\d{6}", city_code) or len(city_name) > 100:
        raise VideoPublishError("douyin_location_incomplete", "地点信息不完整，请重新搜索并选择具体地点。")
    options.update(location=name, location_id=poi_id, location_address=address, location_city_code=city_code)
    if city_name:
        options["location_city_name"] = city_name


def normalize_video_publish_schedule(value: str | None, platforms: list[str], *, now: datetime | None = None) -> str | None:
    if not value:
        return None
    try:
        planned = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VideoPublishError("schedule_invalid", "定时发布时间格式无效。") from exc
    if planned.tzinfo is not None:
        planned = planned.astimezone().replace(tzinfo=None)
    planned = planned.replace(second=0, microsecond=0)
    current = now or datetime.now()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    if planned <= current:
        raise VideoPublishError("schedule_in_past", "定时发布时间必须晚于当前时间，请重新选择。")
    if "baijiahao" in platforms and not current + timedelta(hours=1) <= planned <= current + timedelta(days=7):
        raise VideoPublishError("baijiahao_schedule_range", "百家号定时发布时间需在 1 小时后至 7 天内。")
    if 'bilibili' in platforms and planned < current + timedelta(minutes=5):
        raise VideoPublishError('bilibili_schedule_too_soon', '哔哩哔哩定时发布时间需至少距现在 5 分钟。')
    if 'bilibili' in platforms and planned > current + timedelta(days=15):
        raise VideoPublishError('bilibili_schedule_too_far', '哔哩哔哩定时发布时间不能超过 15 天。')
    if set(platforms) & {"douyin", "kuaishou", "xiaohongshu", "wechat_channels"} and planned <= current + timedelta(hours=2):
        raise VideoPublishError("schedule_too_soon", "当前所选平台要求定时发布时间距现在超过 2 小时，请重新选择。")
    if "douyin" in platforms and planned > current + timedelta(days=14):
        raise VideoPublishError("schedule_too_far", "抖音定时发布时间不能超过 14 天，请重新选择。")
    return planned.strftime("%Y-%m-%d %H:%M")


def normalize_video_publish_target_schedule(schedule_at: str | None, options: dict[str, Any], platform_id: str, *, now: datetime | None = None) -> str | None:
    if "schedule_at_override" in options:
        override = options["schedule_at_override"]
        if override is None:
            effective = None
        elif not schedule_at or not isinstance(override, str) or not override.strip():
            raise VideoPublishError("schedule_invalid", "平台单独定时时间无效，请重新选择。")
        else:
            effective = override
    else:
        effective = schedule_at
    if effective and not _PLATFORM_BY_ID[platform_id]["schedule"]:
        raise VideoPublishError("schedule_unsupported", f"{_PLATFORM_BY_ID[platform_id]['name']} 当前不支持定时发布。")
    return normalize_video_publish_schedule(effective, [platform_id], now=now)


@dataclass(frozen=True)
class SauCommand:
    argv: tuple[str, ...]
    cwd: Path
    project_path: str


@dataclass(frozen=True)
class SauResult:
    returncode: int | None
    output: str
    timed_out: bool = False
    launch_error: str | None = None
    platform_status: str | None = None
    remote_id: str | None = None
    remote_url: str | None = None
    safety_blocked: bool = False


@dataclass(frozen=True)
class VideoPublishCoverUpload:
    upload_id: str
    filename: str
    size_bytes: int
    sha256: str
    path: Path
    managed: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "upload_id": self.upload_id, "filename": self.filename, "size_bytes": self.size_bytes,
            "preview_url": f"/api/v1/video-publish/covers/{self.upload_id}",
        }


async def save_video_publish_cover(upload_file: Any, *, database: Database) -> VideoPublishCoverUpload:
    original_name = getattr(upload_file, "filename", None)
    if not isinstance(original_name, str) or not original_name.strip() or len(original_name) > 1024:
        raise VideoPublishError("cover_filename_invalid", "请选择有效的封面图片。")
    basename = original_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = Path(basename).suffix.casefold()
    if suffix not in _COVER_SUFFIXES:
        raise VideoPublishError("cover_type_invalid", "封面仅支持 JPG、PNG 或 WEBP 图片。")
    safe_name = re.sub(r"[^\w .()\-\u4e00-\u9fff]", "_", Path(basename).stem, flags=re.UNICODE).strip(" ._")[:120] or "视频封面"
    filename = f"{safe_name}{suffix}"
    reported_size = getattr(upload_file, "size", None)
    if isinstance(reported_size, int) and reported_size > _MAX_COVER_BYTES:
        raise VideoPublishError("cover_too_large", "封面图片不能超过 20 MB。")
    content = await upload_file.read(_MAX_COVER_BYTES + 1)
    if len(content) > _MAX_COVER_BYTES:
        raise VideoPublishError("cover_too_large", "封面图片不能超过 20 MB。")
    if not content or (isinstance(reported_size, int) and len(content) != reported_size):
        raise VideoPublishError("cover_upload_invalid", "封面图片为空或上传校验失败，请重新选择图片。")
    return _store_video_cover(database, filename, suffix, content)


def get_video_publish_cover_upload(upload_id: str) -> VideoPublishCoverUpload:
    if not isinstance(upload_id, str) or not _PUBLISH_COVER_ID.fullmatch(upload_id):
        raise VideoPublishError("cover_upload_invalid", "封面上传记录无效，请重新选择图片。")
    root = _prepare_publish_cover_upload_root()
    folder = root / upload_id
    try:
        _assert_contained(folder, root, strict=True)
        receipt_path = folder / "receipt.json"
        if folder.is_symlink() or receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 8192:
            raise ValueError("invalid cover receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        suffix = str(receipt.get("suffix", "")).casefold()
        path = folder / f"cover{suffix}"
        _assert_contained(path, root, strict=True)
        if (
            receipt.get("version") != 1
            or receipt.get("upload_id") != upload_id
            or suffix not in _COVER_SUFFIXES
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != int(receipt.get("size_bytes", -1))
            or _sha256_file(path) != receipt.get("sha256")
        ):
            raise ValueError("cover receipt does not match file")
        _validate_cover_image(path, suffix)
        return VideoPublishCoverUpload(upload_id, str(receipt["filename"]), path.stat().st_size, str(receipt["sha256"]), path.resolve(strict=True))
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise VideoPublishError("cover_upload_invalid", "封面上传记录无效或已过期，请重新选择图片。") from exc


def delete_video_publish_cover_upload(upload_id: str) -> bool:
    if not isinstance(upload_id, str) or not _PUBLISH_COVER_ID.fullmatch(upload_id):
        return False
    return _remove_publish_cover_upload(upload_id)


def _store_video_cover(database: Database, filename: str, suffix: str, content: bytes) -> VideoPublishCoverUpload:
    directory = image_directory(database, "video_publish_cover_root")
    with store_image(database, directory, original_name=filename, suffix=suffix, content=content) as (_, row):
        path = registered_image_path(row)
        _validate_cover_image(path, suffix)
        return VideoPublishCoverUpload(row["id"], filename, row["size_bytes"], row["sha256"], path, managed=True)


def keep_video_publish_covers(database: Database, ids: list[str]) -> int:
    if not ids:
        return 0
    with database.transaction(write=True) as connection:
        placeholders = ",".join("?" for _ in ids)
        return connection.execute(
            f"UPDATE workbench_images SET keep_until = ? WHERE kind = 'video_cover' AND id IN ({placeholders})",
            (time.time() + VIDEO_COVER_LEASE_SECONDS, *ids),
        ).rowcount


def release_video_publish_cover(database: Database, upload_id: str) -> bool:
    if not _PUBLISH_COVER_ID.fullmatch(upload_id):
        return False
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT 1 FROM workbench_images WHERE kind = 'video_cover' AND id = ?", (upload_id,),
        ).fetchone()
    # 编辑页可能共用封面。由续期和引用清理统一判断何时删除。
    return row is not None or delete_video_publish_cover_upload(upload_id)


def _read_cover_upload(database: Database, upload_id: str) -> VideoPublishCoverUpload:
    if not _PUBLISH_COVER_ID.fullmatch(upload_id):
        raise VideoPublishError("cover_upload_invalid", "封面上传记录无效，请重新选择图片。")
    with database.transaction(write=True) as connection:
        row = connection.execute(
            "SELECT * FROM workbench_images WHERE kind = 'video_cover' AND id = ?", (upload_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE workbench_images SET keep_until = ? WHERE id = ?",
                (time.time() + VIDEO_COVER_LEASE_SECONDS, upload_id),
            )
    if row is None:
        return get_video_publish_cover_upload(upload_id)
    try:
        path = registered_image_path(row)
        if _sha256_file(path) != row["sha256"]:
            raise ValueError("cover has changed")
        return VideoPublishCoverUpload(upload_id, row["original_name"], row["size_bytes"], row["sha256"], path, managed=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise VideoPublishError("cover_upload_invalid", "封面文件不可用或已变化，请检查保存目录或重新选择。", 409) from exc


def get_video_publish_cover_upload_path(database: Database, upload_id: str) -> Path:
    return _read_cover_upload(database, upload_id).path


def _managed_batch_cover(connection: sqlite3.Connection, filename: str) -> Path:
    match = IMAGE_FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError("invalid managed cover filename")
    row = connection.execute(
        "SELECT * FROM workbench_images WHERE kind = 'video_cover' AND id = ? AND suffix = ?",
        (match[1], match[2]),
    ).fetchone()
    if row is None:
        raise ValueError("managed cover is unavailable")
    return registered_image_path(row)


def _validate_cover_image(path: Path, suffix: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(12)
    valid = (
        (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )
    if not valid:
        raise VideoPublishError("cover_image_invalid", "图片内容与文件类型不一致，请选择有效的 JPG、PNG 或 WEBP 图片。")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_publish_cover_upload_root() -> Path:
    project = PROJECT_ROOT.resolve(strict=True)
    try:
        relative = _PUBLISH_COVER_UPLOAD_ROOT.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("cover_storage_invalid", "封面暂存目录无效。", 500) from exc
    current = project
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise VideoPublishError("cover_storage_invalid", "封面暂存目录不能包含符号链接。", 500)
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise VideoPublishError("cover_storage_invalid", "封面暂存目录不可用。", 500)
    resolved = _PUBLISH_COVER_UPLOAD_ROOT.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("cover_storage_invalid", "封面暂存目录超出工作台数据目录。", 500) from exc
    return resolved


def _remove_publish_cover_upload(upload_id: str) -> bool:
    try:
        root = _prepare_publish_cover_upload_root()
        folder = root / upload_id
        if folder.is_symlink() or not folder.is_dir():
            return False
        _assert_contained(folder, root, strict=True)
        entries = list(folder.iterdir())
        if any(item.is_symlink() or not item.is_file() for item in entries):
            return False
        for item in entries:
            item.unlink()
        folder.rmdir()
        return True
    except (OSError, RuntimeError, ValueError, VideoPublishError):
        return False


def _read_xhs_identity(account_file: Path) -> str | None:
    try:
        if account_file.is_symlink() or account_file.stat().st_size > 5 * 1024 * 1024:
            return None
        data = json.loads(account_file.read_text(encoding='utf-8'))
        origins = [item for item in data.get('origins', []) if isinstance(item, dict)
                   and item.get('origin') == 'https://creator.xiaohongshu.com']
        if len(origins) != 1:
            return None
        entries = [item for item in origins[0].get('localStorage', []) if isinstance(item, dict)
                   and item.get('name') == 'USER_INFO_FOR_BIZ']
        if len(entries) != 1:
            return None
        identity = json.loads(entries[0]['value']).get('userId')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
            return None
        return hashlib.sha256(('xiaohongshu\0' + identity).encode('utf-8')).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
        return None


def _read_bilibili_identity(account_file: Path) -> str | None:
    try:
        if account_file.is_symlink() or account_file.stat().st_size > 5 * 1024 * 1024:
            return None
        return bilibili_identity(json.loads(account_file.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return None


def _read_wechat_channels_identity(account_file: Path) -> str | None:
    try:
        if account_file.is_symlink() or account_file.stat().st_size > 5 * 1024 * 1024:
            return None
        payload = json.loads(account_file.read_text(encoding='utf-8'))
        origins = [item for item in payload.get('origins', []) if isinstance(item, dict)
                   and item.get('origin') == 'https://channels.weixin.qq.com']
        if len(origins) != 1:
            return None
        entries = [item for item in origins[0].get('localStorage', []) if isinstance(item, dict)
                   and item.get('name') == 'finder_username']
        if len(entries) != 1:
            return None
        username = entries[0].get('value')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}', username):
            return None
        return hashlib.sha256(('wechat_channels\0' + username).encode('utf-8')).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _safe_kuaishou_avatar(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        roots = ('yximgs.com', 'kwimgs.com', 'gifshow.com', 'kuaishou.com', 'kwai.net', 'wskwai.com', 'ksapisrv.com')
        if (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and any(host == root or host.endswith('.' + root) for root in roots)):
            return value
    except ValueError:
        pass
    return ''


def _safe_wechat_channels_avatar(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        roots = ('wx.qlogo.cn', 'qpic.cn')
        if (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and any(host == root or host.endswith('.' + root) for root in roots)):
            return value
    except ValueError:
        pass
    return ''


def _bound_video_identity(connection: sqlite3.Connection, account_id: str) -> str | None:
    row = connection.execute('SELECT value_json FROM settings WHERE key = ?', (f'video_publish_identity:{account_id}',)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row['value_json'])
        if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
            return value
    except (TypeError, ValueError):
        pass
    raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)


def _read_xhs_saved_profile(account_file: Path) -> dict[str, str]:
    try:
        if account_file.is_symlink() or account_file.stat().st_size > 5 * 1024 * 1024:
            return {}
        data = json.loads(account_file.read_text(encoding='utf-8'))
        origins = [item for item in data.get('origins', []) if isinstance(item, dict)
                   and item.get('origin') == 'https://creator.xiaohongshu.com']
        if len(origins) != 1:
            return {}
        entries = [item for item in origins[0].get('localStorage', []) if isinstance(item, dict)
                   and item.get('name') == 'USER_INFO_FOR_BIZ']
        if len(entries) != 1:
            return {}
        info = json.loads(entries[0]['value'])
        name, avatar, user_id = info.get('userName'), info.get('userAvatar'), info.get('userId')
        if not isinstance(user_id, str) or not user_id.strip():
            return {}
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            return {}
        if not isinstance(avatar, str) or len(avatar) > 2048:
            return {}
        parsed = urlsplit(avatar)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not (host == 'xhscdn.com' or host.endswith('.xhscdn.com'))):
            return {}
        return {'name': name.strip(), 'avatar_url': avatar}
    except (OSError, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
        return {}


class SocialAutoUploadAdapter:
    """Invokes the SAU CLI without a shell and keeps its environment isolated."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._upload_progress_callbacks = threading.local()

    def _set_upload_progress_callbacks(
        self,
        on_output: Callable[[str], None] | None,
        on_heartbeat: Callable[[], None] | None,
    ) -> None:
        self._upload_progress_callbacks.on_output = on_output
        self._upload_progress_callbacks.on_heartbeat = on_heartbeat

    def configured_project_path(self) -> str | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT sau_project_path FROM video_publish_settings WHERE id = 1"
            ).fetchone()
        value = row["sau_project_path"] if row is not None else None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def set_project_path(self, project_path: str | None) -> str | None:
        clean_path: str | None = None
        if project_path:
            raw = Path(project_path).expanduser()
            if raw.is_symlink():
                raise VideoPublishError("sau_path_invalid", "SAU 项目目录不能是符号链接。")
            try:
                resolved = raw.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise VideoPublishError("sau_path_invalid", "SAU 项目目录不存在或无法访问。") from exc
            if not resolved.is_dir() or not (resolved / "pyproject.toml").is_file():
                raise VideoPublishError("sau_path_invalid", "请选择包含 pyproject.toml 的 social-auto-upload 项目目录。")
            clean_path = str(resolved)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO video_publish_settings (id, sau_project_path, updated_at)
                   VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET
                   sau_project_path = excluded.sau_project_path, updated_at = excluded.updated_at""",
                (clean_path, _utc_now()),
            )
        return clean_path

    def resolve_command(self) -> tuple[SauCommand | None, str]:
        raw_path = self.configured_project_path()
        if not raw_path:
            return None, "请配置 social-auto-upload 项目目录。"
        try:
            project = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None, "已保存的 SAU 项目目录不可访问，请重新配置。"
        if not project.is_dir() or not (project / "pyproject.toml").is_file():
            return None, "所配置目录中未找到 pyproject.toml，请选择 social-auto-upload 项目根目录。"
        local_cli = project / ".venv" / "Scripts" / "sau.exe"
        if os.name != "nt":
            local_cli = project / ".venv" / "bin" / "sau"
        if local_cli.is_file():
            return SauCommand((str(local_cli),), project, str(project)), ""
        uv = shutil.which("uv")
        if uv:
            return SauCommand((uv, "run", "--project", str(project), "sau"), project, str(project)), ""
        return None, "未找到 uv，也未找到 SAU 虚拟环境中的 sau 可执行文件。"

    def status(self) -> dict[str, object]:
        project_path = self.configured_project_path()
        command, message = self.resolve_command()
        return {
            "configured": command is not None,
            "project_path": project_path,
            "message": message or "SAU 项目目录已配置；首次登录或发布时会实际调用 CLI。",
            "command_source": "uv project" if command and "--project" in command.argv else "project virtualenv" if command else None,
        }

    def login(self, platform_id: str, account_alias: str, on_start: Callable[[subprocess.Popen[str]], None], timeout: int = 1800) -> SauResult:
        platform = _PLATFORM_BY_ID[platform_id]
        arguments = [str(platform["cli_id"]), "login", "--account", account_alias]
        if platform_id not in {"bilibili", "zhihu"}:
            arguments.append("--headed")
        return self.run(
            arguments,
            on_start,
            timeout,
            interactive_terminal=platform_id == "bilibili",
        )

    def check(self, platform_id: str, account_alias: str, on_start: Callable[[subprocess.Popen[str]], None], timeout: int = 120) -> SauResult:
        platform = _PLATFORM_BY_ID[platform_id]
        arguments = [str(platform["cli_id"]), "check", "--account", account_alias]
        if platform_id == "zhihu":
            arguments.append("--with-profile")
        return self.run(arguments, on_start, timeout)

    def read_account_profile(self, platform_id: str, account_alias: str, on_start: Callable[[subprocess.Popen[str]], None]) -> dict[str, str]:
        if platform_id not in {"xiaohongshu", "douyin", "baijiahao"} or not _ACCOUNT_ALIAS.fullmatch(account_alias):
            return {}
        if platform_id != 'xiaohongshu' or not browser_automation_paused():
            require_safe_browser("video account profile")
        command, _ = self.resolve_command()
        if command is None:
            return {}
        account_file = command.cwd / "cookies" / f"{platform_id}_{account_alias}.json"
        if not account_file.is_file() or account_file.is_symlink():
            return {}
        if platform_id == 'xiaohongshu' and is_windows_platform():
            return _read_xhs_saved_profile(account_file)
        python = command.cwd / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.is_file():
            argv = [str(python)]
        else:
            uv = shutil.which("uv")
            if not uv:
                return {}
            argv = [uv, "run", "--project", str(command.cwd), "python"]
        argv.extend([str(Path(__file__).with_name("video_account_profile_probe.py")), platform_id, str(account_file)])
        try:
            process = subprocess.Popen(argv, cwd=str(command.cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            on_start(process)
            try:
                output, _ = process.communicate(timeout=55)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return {}
            result = json.loads(output) if process.returncode == 0 else {}
            if not isinstance(result, dict):
                return {}
            name = result.get("name")
            avatar = result.get("avatar_url")
            if not isinstance(name, str) or not name.strip() or len(name) > 80 or not isinstance(avatar, str) or len(avatar) > 2048:
                return {}
            if platform_id == 'baijiahao':
                if not _safe_baijiahao_avatar(avatar):
                    return {}
            else:
                parsed = urlsplit(avatar)
                host = (parsed.hostname or "").lower()
                domains = {"xiaohongshu": ("xhscdn.com",), "douyin": ("douyinpic.com", "byteimg.com", "douyincdn.com")}[platform_id]
                if parsed.scheme != "https" or parsed.username or parsed.password or not any(host == domain or host.endswith("." + domain) for domain in domains):
                    return {}
            return {"name": name.strip(), "avatar_url": avatar}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def douyin_status(self, target: dict[str, Any], on_start: Callable[[subprocess.Popen[str]], None]) -> dict[str, Any]:
        try:
            require_safe_browser("douyin status")
        except BrowserSafetyError as exc:
            return {"status": "unknown", "message": str(exc)}
        command, message = self.resolve_command()
        if command is None:
            return {"status": "unknown", "message": message}
        python = command.cwd / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.is_file():
            argv = [str(python), "-m", "sau_commands.douyin_status"]
        else:
            uv = shutil.which("uv")
            if not uv:
                return {"status": "unknown", "message": "未找到 SAU 的 Python 运行环境。"}
            argv = [uv, "run", "--project", str(command.cwd), "python", "-m", "sau_commands.douyin_status"]
        try:
            started = int(datetime.fromisoformat(str(target["schedule_at"] or target["started_at"])).timestamp())
        except (ValueError, TypeError):
            return {"status": "unknown", "message": "任务缺少可用于匹配作品的时间。"}
        argv.extend(["--account", str(target["account_alias"]), "--title", str(target["effective_title"]), "--started", str(started)])
        if target.get("remote_id"):
            argv.extend(["--remote-id", str(target["remote_id"])])
        try:
            process = subprocess.Popen(argv, cwd=str(command.cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            on_start(process)
            try:
                output, _ = process.communicate(timeout=75)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return {"status": "unknown", "message": "查询抖音作品状态超时。"}
            marker = re.search(r"(?m)^DOUYIN_STATUS_JSON:(\{[^\r\n]*\})\s*$", output[-16000:])
            result = json.loads(marker.group(1)) if marker else None
            if process.returncode != 0 or not isinstance(result, dict):
                return {"status": "unknown", "message": "抖音状态查询进程未返回有效结果。"}
            if result.get("status") not in {"unknown", "private", "present", "reviewing", "published", "rejected", "restricted"}:
                return {"status": "unknown", "message": "抖音返回了暂不支持识别的状态。"}
            return result
        except (OSError, ValueError, TypeError):
            return {"status": "unknown", "message": "无法启动或读取抖音状态查询。"}

    def upload(
        self,
        target: dict[str, Any],
        on_start: Callable[[subprocess.Popen[str]], None],
    ) -> SauResult:
        platform_id = str(target["platform_id"])
        platform = _PLATFORM_BY_ID[platform_id]
        options = target["options"]
        title = str(options.get("title") or target["title"])
        description = str(options.get("description") or target["description"])
        tag_limit = int(platform["max_tags"])
        tags = options.get("tags") if isinstance(options.get("tags"), list) else target["tags"]
        tags = tags[:tag_limit]
        argv = [
            str(platform["cli_id"]), "upload-video",
            "--account", str(target["account_alias"]),
            "--file", str(target["media_path"]),
            "--title", title,
            "--desc", description,
        ]
        if tags:
            argv.extend(["--tags", ",".join(str(tag) for tag in tags)])
        if target.get("schedule_at"):
            argv.extend(["--schedule", str(target["schedule_at"])])
        if platform_id == "bilibili":
            category = options.get("bilibili_category")
            if category:
                from .bilibili_options import fetch_bilibili_categories, fetch_bilibili_statements, predict_bilibili_legacy_tid
                command, message = self.resolve_command()
                if command is None:
                    return SauResult(None, '', launch_error=message)
                alias = str(target['account_alias'])
                if not _ACCOUNT_ALIAS.fullmatch(alias):
                    return SauResult(None, '', launch_error='B站账号信息无效。')
                try:
                    account_file = command.cwd / 'cookies' / f'bilibili_{alias}.json'
                    if not any(item['id'] == category for item in fetch_bilibili_categories(account_file)):
                        return SauResult(None, '', launch_error='该B站账号的投稿分区已变化，请重新选择。')
                    statement = options.get('bilibili_statement')
                    if statement is not None and not any(item['id'] == statement for item in fetch_bilibili_statements(account_file)):
                        return SauResult(None, '', launch_error='该B站账号的创作声明已变化，请重新选择。')
                    tid = predict_bilibili_legacy_tid(account_file, title)
                except ValueError as exc:
                    return SauResult(None, '', launch_error=str(exc))
                argv.extend(['--tid', str(tid), '--human-type2', str(category)])
                statement = options.get('bilibili_statement')
                if statement is None and options.get('bilibili_ai'):
                    statement = 1
                if statement is not None:
                    argv.extend(['--creation-statement', str(statement)])
                if statement == -2 and options.get('bilibili_source'):
                    argv.extend(['--source', str(options['bilibili_source'])])
                if options.get('bilibili_no_reprint'):
                    argv.append('--no-reprint')
                if options.get('bilibili_private'):
                    argv.append('--private')
                if options.get('bilibili_dynamic'):
                    argv.extend(['--dynamic', str(options['bilibili_dynamic'])])
            else:
                argv.extend(["--tid", str(options.get("tid", ""))])
        media_directory = Path(str(target["media_path"])).parent

        def append_cover(filename: object, flag: str) -> str | None:
            if not filename:
                return None
            try:
                if IMAGE_FILENAME.fullmatch(str(filename)):
                    cover_path = target["managed_cover_paths"][str(filename)]
                else:
                    cover_path = (media_directory / str(filename)).resolve(strict=True)
                    _assert_contained(cover_path, media_directory, strict=True)
                if cover_path.is_symlink() or not cover_path.is_file():
                    raise ValueError("cover file is unavailable")
            except (KeyError, OSError, RuntimeError, ValueError):
                return f"{platform['name']}封面文件不存在，请重新创建发布任务。"
            argv.extend([flag, str(cover_path)])
            return None

        if platform_id in {"douyin", "wechat_channels"}:
            cover_flags = (
                ("cover_portrait_filename", "--thumbnail-portrait"),
                ("cover_landscape_filename", "--thumbnail-landscape"),
            )
            for key, flag in cover_flags:
                error = append_cover(options.get(key), flag)
                if error:
                    return SauResult(None, "", launch_error=error)
        elif platform_id == "baijiahao":
            cover_3_4 = options.get("cover_portrait_filename")
            cover_16_9 = options.get("cover_16_9_filename")
            has_ratio_covers = bool(cover_3_4 or cover_16_9)
            if cover_3_4 and not cover_16_9 and options.get("cover_filename") != cover_3_4:
                cover_16_9 = options.get("cover_filename")
            if has_ratio_covers and not (cover_3_4 and cover_16_9):
                return SauResult(None, "", launch_error="百家号 3:4 与 16:9 封面不完整，请重新编辑任务并设置两张封面。")
            if cover_3_4 and cover_16_9:
                for filename, flag in (
                    (cover_3_4, "--thumbnail-3-4"),
                    (cover_16_9, "--thumbnail-16-9"),
                ):
                    error = append_cover(filename, flag)
                    if error:
                        return SauResult(None, "", launch_error=error)
            else:
                error = append_cover(options.get("cover_filename"), "--thumbnail")
                if error:
                    return SauResult(None, "", launch_error=error)
        else:
            error = append_cover(options.get("cover_filename"), "--thumbnail")
            if error:
                return SauResult(None, "", launch_error=error)

        if platform_id in {"kuaishou", "wechat_channels"} and options.get("collection_name"):
            argv.extend(["--collection", str(options["collection_name"])])
        if platform_id == "baijiahao":
            declaration = options.get("baijiahao_declaration")
            if not isinstance(declaration, str) or declaration not in _BAIJIAHAO_DECLARATIONS:
                return SauResult(None, "", launch_error="请重新编辑百家号任务，选择符合实际的创作声明。")
            argv.extend(["--declaration", declaration])
        if platform_id == "wechat_channels" and options.get("short_title"):
            argv.extend(["--short-title", str(options["short_title"])])
        if platform_id == "wechat_channels" and options.get("save_as_draft"):
            argv.append("--draft")
        if platform_id == "xiaohongshu":
            if target.get("xhs_manual_submit") and os.name != "nt":
                return SauResult(None, "", launch_error="此设备无法打开受控的小红书发布窗口。")
            if os.name == "nt":
                argv.append("--headed")
                if target.get("xhs_manual_submit"):
                    argv.append("--xhs-manual-submit")
            if options.get("xhs_location_id"):
                from .xhs_poi import XhsPoiError, search_poi
                command, message = self.resolve_command()
                if command is None:
                    return SauResult(None, '', launch_error=message)
                alias = str(target['account_alias'])
                if not _ACCOUNT_ALIAS.fullmatch(alias):
                    return SauResult(None, '', launch_error='小红书账号信息无效。')
                account_file = command.cwd / 'cookies' / f'xiaohongshu_{alias}.json'
                if account_file.is_symlink() or account_file.parent.is_symlink():
                    return SauResult(None, '', launch_error='小红书账号信息不可用。')
                try:
                    pois = search_poi(account_file, str(options['xhs_location']))
                except XhsPoiError as exc:
                    return SauResult(None, '', launch_error=str(exc))
                if not any(item['id'] == options['xhs_location_id'] and item['name'] == options['xhs_location'] and item['address'] == options.get('xhs_location_address') for item in pois):
                    return SauResult(None, '', launch_error='小红书地点已变化或无法确认，请重新搜索并选择。')
            if options.get("xhs_original"):
                argv.append("--xhs-original")
            for key, flag in (("xhs_declaration", "--xhs-declaration"), ("xhs_repost_source", "--xhs-repost-source"), ("xhs_visibility", "--xhs-visibility"), ("xhs_location", "--xhs-location"), ("xhs_location_id", "--xhs-location-id"), ("xhs_location_address", "--xhs-location-address")):
                if options.get(key):
                    argv.extend([flag, str(options[key])])
        if platform_id == "douyin":
            if options.get("declaration"):
                argv.extend(["--declaration", str(options["declaration"])])
            if options.get("repost_source"):
                argv.extend(["--repost-source", str(options["repost_source"])])
            if options.get("repost_title"):
                argv.extend(["--repost-title", str(options["repost_title"])])
            if options.get("repost_id"):
                argv.extend(["--repost-id", str(options["repost_id"])])
            if options.get("location"):
                argv.extend(["--location", str(options["location"])])
            if options.get("location_id"):
                argv.extend(["--location-id", str(options["location_id"])])
            if options.get("location_address"):
                argv.extend(["--location-address", str(options["location_address"])])
            if options.get("location_city_code"):
                argv.extend(["--location-city-code", str(options["location_city_code"])])
            if options.get("visibility"):
                argv.extend(["--visibility", str(options["visibility"])])
            if "allow_download" in options:
                argv.append("--allow-download" if options["allow_download"] else "--disallow-download")
        on_output = getattr(self._upload_progress_callbacks, "on_output", None)
        on_heartbeat = getattr(self._upload_progress_callbacks, "on_heartbeat", None)
        if on_output is None and on_heartbeat is None:
            result = self.run(argv, on_start, _UPLOAD_TIMEOUT_SECONDS)
        else:
            result = self.run(
                argv, on_start, _UPLOAD_TIMEOUT_SECONDS,
                on_output=on_output, on_heartbeat=on_heartbeat,
            )
        if (platform_id == "baijiahao" and target.get("schedule_at") and result.returncode not in (None, 0)
                and "--schedule" in result.output and "unrecognized arguments" in result.output.casefold()):
            return SauResult(
                result.returncode,
                result.output,
                timed_out=result.timed_out,
                launch_error="当前配置的 SAU 项目不支持百家号定时发布，请更新 social-auto-upload 后重试。",
            )
        return result

    def upload_xiaohongshu_note(
        self,
        account_alias: str,
        image_paths: list[Path],
        title: str,
        note: str,
        on_start: Callable[[subprocess.Popen[str]], None],
    ) -> SauResult:
        if not _ACCOUNT_ALIAS.fullmatch(account_alias):
            return SauResult(None, "", launch_error="小红书账号信息无效。")
        if not 1 <= len(image_paths) <= 18:
            return SauResult(None, "", launch_error="小红书图文需要 1 到 18 张图片。")
        if not title.strip() or len(title.strip()) > 20:
            return SauResult(None, "", launch_error="小红书笔记标题须为 1 到 20 个字符。")
        if len(note) > 1000:
            return SauResult(None, "", launch_error="小红书笔记正文不能超过 1000 个字符。")
        argv = [
            "xiaohongshu", "upload-note", "--account", account_alias,
            "--title", title.strip(), "--note", note,
            "--images", *(str(path) for path in image_paths),
        ]
        return self.run(argv, on_start, 30 * 60)

    def upload_zhihu_article(
        self,
        account_alias: str,
        title: str,
        content_file: Path,
        on_start: Callable[[subprocess.Popen[str]], None],
    ) -> SauResult:
        if not _ACCOUNT_ALIAS.fullmatch(account_alias):
            return SauResult(None, '', launch_error='知乎账号信息无效。')
        if not title.strip() or len(title.strip()) > 100:
            return SauResult(None, '', launch_error='知乎文章标题须为 1 到 100 个字符。')
        if content_file.is_symlink() or not content_file.is_file():
            return SauResult(None, '', launch_error='知乎文章正文文件不可用。')
        result = self.run([
            'zhihu', 'upload-article', '--account', account_alias,
            '--title', title.strip(), '--content-file', str(content_file), '--headed',
        ], on_start, 30 * 60)
        if result.launch_error:
            return result
        payload = _parse_zhihu_article_result(result.output)
        if payload is not None:
            state = payload.get('status')
            if state == 'published' and payload.get('success') is True:
                url = payload.get('url')
                match = re.fullmatch(r'https://zhuanlan\.zhihu\.com/p/(\d+)/?', url) if isinstance(url, str) else None
                if match:
                    return SauResult(result.returncode, result.output, timed_out=result.timed_out,
                                     platform_status='published', remote_id=match.group(1), remote_url=f'https://zhuanlan.zhihu.com/p/{match.group(1)}')
            if state == 'failed_before_submit':
                return SauResult(result.returncode, result.output, timed_out=result.timed_out, platform_status='failed')
            if state == 'submission_unknown':
                return SauResult(result.returncode, result.output, timed_out=result.timed_out, platform_status='unknown')
        if '知乎文章阶段=failed_before_submit' in result.output:
            return SauResult(result.returncode, result.output, timed_out=result.timed_out, platform_status='failed')
        return SauResult(result.returncode, result.output, timed_out=result.timed_out, platform_status='unknown')

    def run(
        self,
        arguments: list[str],
        on_start: Callable[[subprocess.Popen[str]], None],
        timeout: int,
        *,
        interactive_terminal: bool = False,
        on_output: Callable[[str], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> SauResult:
        xhs_login = is_windows_platform() and len(arguments) >= 2 and arguments[:2] == ['xiaohongshu', 'login']
        if xhs_login:
            try:
                with xiaohongshu_login_slot():
                    return self._run_command(
                        arguments, on_start, timeout, interactive_terminal=interactive_terminal, xhs_login=True,
                        on_output=on_output, on_heartbeat=on_heartbeat,
                    )
            except BrowserSafetyError as exc:
                return SauResult(None, '', launch_error=str(exc))
        return self._run_command(
            arguments, on_start, timeout, interactive_terminal=interactive_terminal, xhs_login=False,
            on_output=on_output, on_heartbeat=on_heartbeat,
        )

    def _run_command(
        self,
        arguments: list[str],
        on_start: Callable[[subprocess.Popen[str]], None],
        timeout: int,
        *,
        interactive_terminal: bool,
        xhs_login: bool,
        on_output: Callable[[str], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> SauResult:
        controlled_upload = os.name == 'nt' and len(arguments) >= 2 and (
            (arguments[0] in {'xiaohongshu', 'kuaishou', 'douyin', 'bilibili', 'tencent', 'baijiahao'} and arguments[1] == 'upload-video')
            or (arguments[0] == 'xiaohongshu' and arguments[1] == 'upload-note')
        )
        if not xhs_login and not controlled_upload:
            try:
                require_safe_browser("video publishing")
            except BrowserSafetyError as exc:
                return SauResult(None, "", launch_error=str(exc))
        command, message = self.resolve_command()
        if command is None:
            return SauResult(None, "", launch_error=message)
        command_argv = [*command.argv, *arguments]
        if arguments[:2] == ["xiaohongshu", "upload-video"]:
            extension = command.cwd / "uploader" / "xiaohongshu_uploader" / "workbench_video.py"
            requested_extension = "--headed" in arguments or any(arg.startswith("--xhs-") for arg in arguments)
            if not extension.is_file() and requested_extension:
                return SauResult(None, "", launch_error="当前 SAU 项目缺少小红书工作台发布扩展，请更新该项目。")
            if extension.is_file():
                python = command.cwd / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                if python.is_file():
                    command_argv = [str(python), "-m", "uploader.xiaohongshu_uploader.workbench_video", *arguments[2:]]
                elif len(command.argv) > 1 and command.argv[1] == "run":
                    command_argv = [*command.argv[:-1], "python", "-m", "uploader.xiaohongshu_uploader.workbench_video", *arguments[2:]]
                else:
                    return SauResult(None, "", launch_error="无法找到 SAU 项目的 Python 运行环境。")
        if controlled_upload:
            try:
                require_controlled_video_browser(command.cwd)
            except BrowserSafetyError as exc:
                return SauResult(None, '', launch_error=str(exc), safety_blocked=True)
            python = command.cwd / '.venv' / 'Scripts' / 'python.exe'
            if not python.is_file():
                return SauResult(None, '', launch_error='受控发布需要 SAU 项目内的 Python 运行环境。')
            runner = Path(__file__).with_name('safe_video_runner.py')
            command_argv = [str(python), str(runner), *arguments]
        environment = None
        if xhs_login:
            try:
                reserve_xiaohongshu_login(self.database, command.cwd)
            except BrowserSafetyError as exc:
                return SauResult(None, "", launch_error=str(exc))
            environment = os.environ.copy()
            environment.pop('SAU_XHS_CREATOR_BASE_URL', None)
            environment['SAU_XHS_SAFE_LOGIN'] = '1'
            environment['SAU_XHS_SAFETY_MODULE'] = str(Path(__file__).resolve().parents[1] / 'browser_safety.py')
        try:
            process = subprocess.Popen(
                command_argv,
                cwd=str(command.cwd),
                env=environment,
                stdin=None if interactive_terminal else subprocess.DEVNULL,
                stdout=None if interactive_terminal else subprocess.PIPE,
                stderr=None if interactive_terminal else subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                    if interactive_terminal
                    else getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            )
        except OSError as exc:
            return SauResult(None, "", launch_error=_safe_message(exc))
        on_start(process)
        try:
            if interactive_terminal:
                return SauResult(process.wait(timeout=timeout), "")
            if on_output is None and on_heartbeat is None:
                output, _ = process.communicate(timeout=timeout)
            else:
                output, timed_out = self._communicate_with_progress(
                    process, timeout, on_output=on_output, on_heartbeat=on_heartbeat,
                )
                if timed_out:
                    result = _sau_result(process.returncode, output[-16_000:])
                    return SauResult(
                        result.returncode, result.output, timed_out=True,
                        platform_status=result.platform_status, remote_id=result.remote_id, remote_url=result.remote_url,
                    )
            return _sau_result(process.returncode, output[-16_000:])
        except subprocess.TimeoutExpired:
            process.kill()
            if interactive_terminal:
                process.wait()
                return SauResult(process.returncode, "", timed_out=True)
            output, _ = process.communicate()
            result = _sau_result(process.returncode, output[-16_000:])
            return SauResult(
                result.returncode, result.output, timed_out=True,
                platform_status=result.platform_status, remote_id=result.remote_id, remote_url=result.remote_url,
            )

    @staticmethod
    def _communicate_with_progress(
        process: subprocess.Popen[str],
        timeout: int,
        *,
        on_output: Callable[[str], None] | None,
        on_heartbeat: Callable[[], None] | None,
    ) -> tuple[str, bool]:
        output_chunks: list[str] = []
        reader_error: list[BaseException] = []

        def drain_output() -> None:
            stream = process.stdout
            if stream is None:
                return
            try:
                for line in iter(stream.readline, ""):
                    output_chunks.append(line)
                    if on_output is not None:
                        try:
                            on_output(line)
                        except Exception:
                            _LOGGER.exception("video publish output callback failed")
            except (OSError, ValueError) as exc:
                reader_error.append(exc)

        reader = threading.Thread(target=drain_output, name="video-publish-output", daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(_VIDEO_PUBLISH_HEARTBEAT_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                if on_heartbeat is not None and process.poll() is None:
                    try:
                        on_heartbeat()
                    except Exception:
                        _LOGGER.exception("video publish heartbeat callback failed")
        if timed_out and process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        if reader.is_alive():
            _LOGGER.warning("video publish output reader did not stop after process exit")
        if reader_error:
            _LOGGER.warning("video publish output reader stopped: %s", reader_error[0])
        return "".join(output_chunks), timed_out


def video_publish_platforms(database: Database, *, scope: str = 'video') -> list[dict[str, object]]:
    if scope not in {'video', 'article'}:
        raise ValueError('发布账号用途无效。')
    adapter = SocialAutoUploadAdapter(database)
    status = adapter.status()
    platforms = [item for item in VIDEO_PLATFORMS if scope == 'article' or not item.get('article_only')]
    if scope == 'article':
        platforms = [item for item in platforms if item.get('article_only') or item['id'] in {'xiaohongshu', 'baijiahao'}]
    return [{**item,
             "schedule": bool(item["schedule"] and not (scope == 'article' and item['id'] == 'baijiahao')),
             "sau_ready": bool(status["configured"]),
             "login_available": item['id'] in _SUPPORTED_LOGIN_PLATFORMS
             or (scope == 'article' and item['id'] == 'baijiahao')} for item in platforms]


def list_video_publish_accounts(database: Database, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM video_publish_accounts"
    if not include_disabled:
        query += " WHERE enabled = 1"
    query += " ORDER BY platform_id, created_at, id"
    with database.transaction() as connection:
        rows = connection.execute(query).fetchall()
        bound: dict[str, str] = {}
        for row in rows:
            try:
                identity = _bound_video_identity(connection, str(row['id']))
                if identity:
                    bound[str(row['id'])] = identity
            except VideoPublishError:
                _LOGGER.warning('could not read account identity for %s', row['id'])
    items = [_account_payload(row) for row in rows]
    try:
        root = SocialAutoUploadAdapter(database).configured_project_path()
        cookies = Path(root).resolve(strict=True) / 'cookies' if root else None
        if cookies is not None and (not cookies.is_dir() or cookies.is_symlink()):
            cookies = None
        seen: set[str] = set()
        for item in items:
            if item['platform_id'] != 'xiaohongshu' or not item['enabled']:
                continue
            alias = str(item['account_alias'])
            if not _ACCOUNT_ALIAS.fullmatch(alias):
                continue
            identity = bound.get(str(item['id']))
            if not identity and cookies is not None:
                identity = _read_xhs_identity(cookies / f'xiaohongshu_{alias}.json')
            if identity and identity in seen:
                item['duplicate_notice'] = '该账号已添加，请保留一条记录。'
            if identity:
                seen.add(identity)
    except (OSError, ValueError, RuntimeError):
        _LOGGER.warning('could not inspect saved account identities')
    return items


def list_video_publish_duplicate_notices(database: Database) -> list[dict[str, str]]:
    with database.transaction() as connection:
        rows = connection.execute("""SELECT id, platform_id FROM video_publish_accounts
            WHERE enabled = 0 AND status = 'error' AND message = ?
              AND julianday(checked_at) BETWEEN julianday('now', '-5 minutes') AND julianday('now', '+1 minute')
            ORDER BY checked_at, id""", (_DUPLICATE_ACCOUNT_MESSAGE,)).fetchall()
    return [{'account_id': str(row['id']), 'platform_id': str(row['platform_id']),
             'message': _DUPLICATE_ACCOUNT_MESSAGE} for row in rows]


def _xiaohongshu_pending_alias(alias: str, account_id: str, updated_at: str) -> str:
    token = hashlib.sha256(f'{account_id}\0{updated_at}'.encode('utf-8')).hexdigest()[:16]
    return f'{alias}_pending_{token}'


def latest_video_publish_login_qr(database: Database, account_id: str) -> bytes | None:
    with database.transaction() as connection:
        account = connection.execute(
            """SELECT platform_id, account_alias, status, enabled, updated_at
               FROM video_publish_accounts WHERE id = ?""",
            (account_id,),
        ).fetchone()
    if account is None or not bool(account["enabled"]) or account["status"] != "login_in_progress":
        return None
    platform = _PLATFORM_BY_ID.get(str(account["platform_id"]))
    project_path = SocialAutoUploadAdapter(database).configured_project_path()
    if platform is None or not project_path:
        return None
    try:
        project = Path(project_path).resolve(strict=True)
        cookies_dir = project / "cookies"
        if cookies_dir.is_symlink():
            return None
        cookies_root = cookies_dir.resolve(strict=True)
        cookies_root.relative_to(project)
        login_started_at = datetime.fromisoformat(str(account["updated_at"])).timestamp() - 5
        cli_id = str(platform["cli_id"])
        alias = str(account["account_alias"])
        if not _ACCOUNT_ALIAS.fullmatch(alias):
            return None
        if account['platform_id'] in {'xiaohongshu', 'bilibili', 'kuaishou', 'wechat_channels'}:
            alias = _xiaohongshu_pending_alias(alias, account_id, str(account['updated_at']))
        candidates = sorted(
            cookies_root.glob(f"{cli_id}_{alias}_*qrcode*.png"),
            key=lambda path: path.stat().st_mtime_ns if path.is_file() and not path.is_symlink() else -1,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(cookies_root)
            metadata = resolved.stat()
            if metadata.st_size < 8 or metadata.st_size > 2 * 1024 * 1024 or metadata.st_mtime < login_started_at:
                continue
            image = resolved.read_bytes()
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                return image
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def add_video_publish_account(database: Database, platform_id: str, alias: str | None = None, display_name: str = "") -> dict[str, Any]:
    if platform_id not in _PLATFORM_BY_ID:
        raise VideoPublishError("platform_unsupported", "该平台暂不支持视频发布。")
    account_id = uuid.uuid4().hex
    clean_alias = alias.strip() if alias is not None else f"account_{account_id}"
    clean_name = display_name.strip()[:80]
    if not _ACCOUNT_ALIAS.fullmatch(clean_alias):
        raise VideoPublishError("account_alias_invalid", "账号别名仅支持 1–64 位英文字母、数字、点、下划线和短横线，且必须以字母或数字开头。")
    now = _utc_now()
    try:
        with database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO video_publish_accounts
                   (id, platform_id, account_alias, display_name, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'not_connected', ?, ?)""",
                (account_id, platform_id, clean_alias, clean_name, now, now),
            )
            row = connection.execute("SELECT * FROM video_publish_accounts WHERE id = ?", (account_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise VideoPublishError("account_alias_exists", "该平台已存在相同的 SAU 账号别名。", 409) from exc
    return _account_payload(row)


def _account_display_name(account: sqlite3.Row) -> str:
    alias = str(account["account_alias"])
    return str(account["display_name"] or account["profile_name"] or (f"账号 {str(account['id'])[-6:]}" if re.fullmatch(r"account_[a-f0-9]{32}", alias) else alias))


def request_video_publish_account_action(database: Database, account_id: str, action: str) -> dict[str, Any]:
    if action not in {"login", "check"}:
        raise VideoPublishError("account_action_invalid", "账号操作无效。")
    status = "login_in_progress" if action == "login" else "checking"
    message = "正在打开登录窗口，请在平台页面完成登录。" if action == "login" else "正在检查平台登录状态。"
    with database.transaction(write=True) as connection:
        account = connection.execute('SELECT status, platform_id FROM video_publish_accounts WHERE id = ? AND enabled = 1', (account_id,)).fetchone()
        if account is None:
            raise KeyError('视频发布账号不存在。')
        if action == 'login' and account['platform_id'] == 'bilibili':
            message = '正在准备登录二维码，请使用哔哩哔哩 App 扫码。'
        if action == 'login' and account['platform_id'] == 'wechat_channels':
            message = '正在连接微信视频号登录页并获取二维码；扫码确认后，账号信息会自动同步。'
        if account['status'] in {'login_in_progress', 'checking'}:
            raise VideoPublishError('account_busy', '账号正在处理，请稍候。', 409)
        if action == 'check' and account['status'] not in {'connected', 'error'}:
            raise VideoPublishError('account_login_required', '请先登录该账号。', 409)
        active = connection.execute(
            """SELECT 1 FROM video_publish_targets
               WHERE account_id = ? AND status IN ('queued', 'running') LIMIT 1""",
            (account_id,),
        ).fetchone()
        if active is not None:
            raise VideoPublishError("account_busy", "该账号有发布任务正在排队或执行，请完成后再登录或检查。", 409)
        cursor = connection.execute(
            """UPDATE video_publish_accounts SET status = ?, message = ?, updated_at = ?
               WHERE id = ? AND enabled = 1""",
            (status, message, datetime.now(timezone.utc).isoformat(timespec='microseconds'), account_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("视频发布账号不存在。")
        row = connection.execute("SELECT * FROM video_publish_accounts WHERE id = ?", (account_id,)).fetchone()
    return _account_payload(row)


def update_video_publish_account_status(
    database: Database,
    account_id: str,
    status: str,
    message: str,
) -> dict[str, Any]:
    allowed = {"not_connected", "login_in_progress", "checking", "connected", "expired", "error"}
    if status not in allowed:
        raise ValueError("账号状态无效。")
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            """UPDATE video_publish_accounts SET status = ?, message = ?, checked_at = ?, updated_at = ?
               WHERE id = ?""",
            (status, _safe_message(message), _utc_now(), _utc_now(), account_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("视频发布账号不存在。")
        row = connection.execute("SELECT * FROM video_publish_accounts WHERE id = ?", (account_id,)).fetchone()
    return _account_payload(row)


def list_video_media(database: Database, archive_root: Path | None, *, limit: int = 100) -> list[dict[str, object]]:
    if archive_root is None:
        return []
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT a.id AS asset_id, a.title AS title, a.created_at AS asset_created_at,
                      (SELECT media.id FROM artifacts AS media
                       JOIN tasks AS media_task ON media_task.id = media.task_id
                       WHERE media_task.asset_id = a.id AND media.kind IN ('media', 'video', 'legacy_video')
                       ORDER BY media.created_at DESC, media.version DESC, media.id DESC LIMIT 1) AS artifact_id,
                      (SELECT media.path FROM artifacts AS media
                       JOIN tasks AS media_task ON media_task.id = media.task_id
                       WHERE media_task.asset_id = a.id AND media.kind IN ('media', 'video', 'legacy_video')
                       ORDER BY media.created_at DESC, media.version DESC, media.id DESC LIMIT 1) AS media_path,
                      (SELECT media.sha256 FROM artifacts AS media
                       JOIN tasks AS media_task ON media_task.id = media.task_id
                       WHERE media_task.asset_id = a.id AND media.kind IN ('media', 'video', 'legacy_video')
                       ORDER BY media.created_at DESC, media.version DESC, media.id DESC LIMIT 1) AS media_sha256
               FROM assets AS a
               WHERE EXISTS (SELECT 1 FROM artifacts AS media
                             JOIN tasks AS media_task ON media_task.id = media.task_id
                             WHERE media_task.asset_id = a.id AND media.kind IN ('media', 'video', 'legacy_video'))
               ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        try:
            path = Path(str(row["media_path"]))
            if path.suffix.casefold() not in _VIDEO_SUFFIXES or path.is_symlink():
                continue
            safe_path = _assert_contained(path, archive_root, strict=True)
            if not safe_path.is_file():
                continue
            items.append({
                "asset_id": str(row["asset_id"]),
                "artifact_id": str(row["artifact_id"]),
                "title": str(row["title"] or safe_path.name),
                "name": safe_path.name,
                "size_bytes": safe_path.stat().st_size,
                "sha256": str(row["media_sha256"]),
                "created_at": str(row["asset_created_at"]),
            })
        except (OSError, RuntimeError, ValueError):
            continue
    return items


def validate_xhs_options(options: dict[str, Any]) -> None:
    declaration = options.get("xhs_declaration")
    if declaration is not None and declaration not in {"fiction", "ai", "advertising", "self_shot", "repost"}:
        raise VideoPublishError("xhs_declaration_invalid", "小红书内容声明无效。")
    if "xhs_original" in options and not isinstance(options["xhs_original"], bool):
        raise VideoPublishError("xhs_original_invalid", "小红书原创声明无效。")
    if options.get("xhs_visibility") not in {None, "public", "private", "friends"}:
        raise VideoPublishError("xhs_visibility_invalid", "小红书可见范围无效。")
    for key in ("xhs_repost_source", "xhs_location"):
        if key in options:
            value = options[key]
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
                raise VideoPublishError("xhs_option_invalid", "小红书地点或转载来源无效。")
            options[key] = value.strip()
    if declaration == "repost" and not options.get("xhs_repost_source"):
        raise VideoPublishError("xhs_repost_source_required", "小红书来源转载需要填写媒体名称。")
    if declaration != "repost" and options.get("xhs_repost_source"):
        raise VideoPublishError("xhs_repost_source_invalid", "只有来源转载可以填写媒体名称。")
    if any(options.get(key) for key in ("xhs_location_id", "xhs_location_address")):
        if not all(isinstance(options.get(key), str) and 0 < len(options[key].strip()) <= limit for key, limit in (("xhs_location", 100), ("xhs_location_id", 128), ("xhs_location_address", 300))):
            raise VideoPublishError("xhs_location_invalid", "小红书地点信息不完整，请重新搜索并选择。")


def _normalize_video_publish_target_options(
    *,
    platform_id: str,
    schedule_at: str | None,
    options: dict[str, Any],
    common_title: str,
    common_description: str,
    common_tags: list[str],
    allow_partial_douyin_cover_replacement: bool = False,
    allow_partial_baijiahao_cover_replacement: bool = False,
) -> dict[str, Any]:
    if platform_id not in _PLATFORM_BY_ID or _PLATFORM_BY_ID[platform_id].get('article_only'):
        raise VideoPublishError("platform_unsupported", "所选账号的平台暂不支持视频发布。")
    options = dict(options)
    effective_schedule = normalize_video_publish_target_schedule(schedule_at, options, platform_id)
    if "schedule_at_override" in options:
        options["schedule_at_override"] = effective_schedule
    if platform_id == "bilibili":
        statement = options.get('bilibili_statement')
        if statement is not None and (type(statement) is not int or statement not in {-2, -1, 1, 2, 3, 4}):
            raise VideoPublishError('bilibili_statement_invalid', 'B站创作声明无效，请重新选择。')
        source = str(options.get('bilibili_source') or '').strip()
        if statement == -2 and not source:
            raise VideoPublishError('bilibili_source_required', '转载视频需要填写转载来源。')
        if statement == -2 and options.get('bilibili_no_reprint'):
            raise VideoPublishError('bilibili_repost_authorization_invalid', '转载视频不能选择自制禁止转载声明。')
        if source and statement != -2:
            raise VideoPublishError('bilibili_source_invalid', '只有转载视频可以填写转载来源。')
        if source:
            options['bilibili_source'] = source
        category = options.get('bilibili_category')
        if category is not None:
            if type(category) is not int or not 1 <= category <= 10000:
                raise VideoPublishError('bilibili_category_invalid', '哔哩哔哩分区无效，请重新选择。')
        else:
            if any(options.get(key) for key in ('bilibili_ai', 'bilibili_private', 'bilibili_dynamic')):
                raise VideoPublishError('bilibili_category_required', '使用B站新投稿设置时，请先选择投稿分区。')
            try:
                tid = int(options.get("tid"))
            except (TypeError, ValueError) as exc:
                raise VideoPublishError("bilibili_tid_required", "发布到哔哩哔哩时请选择分区。") from exc
            if not 1 <= tid <= 10000:
                raise VideoPublishError("bilibili_tid_invalid", "哔哩哔哩分区 ID 无效。")
            options["tid"] = tid
    if platform_id not in {"douyin", "wechat_channels", "baijiahao"} and any(
        options.get(key) for key in ("cover_portrait_upload_id", "cover_landscape_upload_id")
    ):
        raise VideoPublishError("cover_platform_invalid", "当前平台不支持比例封面，请使用通用封面或平台封面。")
    if platform_id != "bilibili" and options.get("cover_4_3_upload_id"):
        raise VideoPublishError("cover_platform_invalid", "4:3 比例封面仅支持哔哩哔哩。")
    if platform_id not in {"bilibili", "baijiahao"} and options.get("cover_16_9_upload_id"):
        raise VideoPublishError("cover_platform_invalid", "16:9 封面仅支持哔哩哔哩和百家号。")
    if platform_id in {"douyin", "wechat_channels"} and options.get("cover_upload_id"):
        raise VideoPublishError("cover_ratio_required", "抖音和视频号需要按 3:4 或 4:3 封面位上传封面。")
    if (
        platform_id == "douyin"
        and bool(options.get("cover_portrait_upload_id")) != bool(options.get("cover_landscape_upload_id"))
        and not allow_partial_douyin_cover_replacement
    ):
        raise VideoPublishError("douyin_cover_pair_required", "抖音自定义封面需要同时设置 3:4 和 4:3 两种比例。")
    if (
        platform_id == "baijiahao"
        and bool(options.get("cover_portrait_upload_id")) != bool(options.get("cover_16_9_upload_id"))
        and not allow_partial_baijiahao_cover_replacement
    ):
        raise VideoPublishError("baijiahao_cover_pair_required", "百家号封面需要同时设置 3:4 和 16:9 两张图片。")
    if platform_id != "wechat_channels" and options.get("save_as_draft"):
        raise VideoPublishError("draft_platform_invalid", "保存草稿目前仅支持微信视频号。")
    allowed_options = {"title", "description", "tags", "tid", "schedule_at_override"}
    if platform_id in {"douyin", "wechat_channels"}:
        allowed_options.update({"cover_portrait_upload_id", "cover_landscape_upload_id"})
    elif platform_id == "bilibili":
        allowed_options.update({"cover_4_3_upload_id", "cover_16_9_upload_id", "bilibili_category", "bilibili_category_name", "bilibili_ai", "bilibili_statement", "bilibili_statement_name", "bilibili_source", "bilibili_no_reprint", "bilibili_private", "bilibili_dynamic"})
    else:
        allowed_options.add("cover_upload_id")
    if platform_id == "baijiahao":
        allowed_options.update({"cover_portrait_upload_id", "cover_16_9_upload_id"})
    if platform_id in {"kuaishou", "wechat_channels"}:
        allowed_options.add("collection_name")
    if platform_id in {"kuaishou", "xiaohongshu"}:
        allowed_options.add("cover_ratio")
    if platform_id == "wechat_channels":
        allowed_options.update({"short_title", "save_as_draft"})
    if platform_id == "douyin":
        allowed_options.update({"declaration", "repost_source", "repost_title", "repost_id", "location", "location_id", "location_address", "location_city_code", "location_city_name", "visibility", "allow_download"})
    if platform_id == "xiaohongshu":
        allowed_options.update({"xhs_original", "xhs_declaration", "xhs_repost_source", "xhs_visibility", "xhs_location", "xhs_location_id", "xhs_location_address"})
    if platform_id == "baijiahao":
        declaration = options.get("baijiahao_declaration")
        if not isinstance(declaration, str) or declaration not in _BAIJIAHAO_DECLARATIONS:
            raise VideoPublishError("baijiahao_declaration_required", "发布到百家号时请选择符合实际的创作声明。")
        allowed_options.add("baijiahao_declaration")
    options = {key: value for key, value in options.items() if key in allowed_options}
    if platform_id == "xiaohongshu":
        validate_xhs_options(options)
    if "title" in options:
        options["title"] = str(options["title"]).strip()[:200]
    if "description" in options:
        options["description"] = str(options["description"])[:5000]
    if "tags" in options and isinstance(options["tags"], list):
        options["tags"] = [str(value).strip().lstrip("#")[:50] for value in options["tags"] if str(value).strip()][:30]
    for key in ("cover_upload_id", "cover_portrait_upload_id", "cover_landscape_upload_id", "cover_4_3_upload_id", "cover_16_9_upload_id"):
        value = options.get(key)
        if value and (not isinstance(value, str) or not _PUBLISH_COVER_ID.fullmatch(value)):
            raise VideoPublishError("cover_upload_invalid", "封面上传记录无效，请重新选择图片。")
    if "collection_name" in options:
        collection_name = str(options["collection_name"]).strip()
        if collection_name:
            options["collection_name"] = collection_name[:100]
        else:
            options.pop("collection_name")
    if "short_title" in options:
        short_title = str(options["short_title"]).strip()
        if short_title:
            options["short_title"] = short_title[:15]
        else:
            options.pop("short_title")
    if platform_id == "douyin":
        if "declaration" in options:
            declaration = str(options["declaration"]).strip()
            if declaration not in _DOUYIN_DECLARATIONS:
                raise VideoPublishError("douyin_declaration_invalid", "抖音自主声明选项无效，请重新选择。")
            options["declaration"] = declaration
        validate_douyin_repost_options(options)
        validate_douyin_location_options(options)
        if "visibility" in options and options["visibility"] not in {"public", "friends", "private"}:
            raise VideoPublishError("douyin_visibility_invalid", "抖音可见范围选项无效。")
        if "allow_download" in options and not isinstance(options["allow_download"], bool):
            raise VideoPublishError("douyin_download_permission_invalid", "抖音下载权限选项无效。")
    effective_title = str(options.get("title") or common_title)
    effective_description = str(options.get("description") or common_description)
    effective_tags = options.get("tags") if isinstance(options.get("tags"), list) else common_tags
    if not effective_title.strip() or len(effective_title) > 200:
        raise VideoPublishError("title_invalid", "视频标题不能为空，且不能超过 200 个字符。")
    if len(effective_description) > 5000:
        raise VideoPublishError("description_invalid", "视频描述不能超过 5000 个字符。")
    max_tags = int(_PLATFORM_BY_ID[platform_id]["max_tags"])
    options["tags"] = effective_tags[:max_tags]
    if platform_id == "weibo" and len(effective_title) > 30:
        raise VideoPublishError("weibo_title_too_long", "微博视频标题不能超过 30 个字符。")
    if platform_id == "bilibili" and not effective_description.strip():
        raise VideoPublishError("bilibili_description_required", "发布到哔哩哔哩时必须填写视频简介。")
    return options


def create_video_publish_batch(
    database: Database,
    *,
    archive_root: Path | None,
    idempotency_key: str,
    asset_id: str | None,
    upload_id: str | None,
    local_source_id: str | None = None,
    cover_portrait_upload_id: str | None = None,
    cover_landscape_upload_id: str | None = None,
    title: str,
    description: str,
    tags: list[str],
    schedule_at: str | None,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    video_upload_id = upload_id
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise VideoPublishError("idempotency_key_invalid", "任务提交标识无效，请刷新页面后重试。")
    if sum(bool(item) for item in (asset_id, upload_id, local_source_id)) != 1:
        raise VideoPublishError("media_source_invalid", "请选择素材库视频或上传一个本地视频。")
    clean_title = title.strip()
    if not clean_title or len(clean_title) > 200:
        raise VideoPublishError("title_invalid", "视频标题不能为空，且不能超过 200 个字符。")
    if len(description) > 1000:
        raise VideoPublishError("description_invalid", "视频简介不能超过 1000 个字符。")
    clean_tags = [item.strip().lstrip("#")[:50] for item in tags if isinstance(item, str) and item.strip()]
    clean_tags = list(dict.fromkeys(clean_tags))[:30]
    if not 1 <= len(targets) <= _MAX_TARGETS:
        raise VideoPublishError("targets_invalid", f"请选择 1–{_MAX_TARGETS} 个发布账号。")
    if len({str(item.get("account_id", "")) for item in targets}) != len(targets):
        raise VideoPublishError("targets_duplicate", "同一发布账号不能重复选择。")
    existing_batch_id: str | None = None
    with database.transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM video_publish_batches WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            existing_batch_id = str(existing["id"])
        else:
            account_ids = [str(item.get("account_id", "")) for item in targets]
            placeholders = ",".join("?" for _ in account_ids)
            accounts = connection.execute(
                f"SELECT * FROM video_publish_accounts WHERE enabled = 1 AND id IN ({placeholders})",
                account_ids,
            ).fetchall()
    if existing_batch_id is not None:
        return get_video_publish_batch(database, existing_batch_id) or {}
    account_by_id = {str(row["id"]): row for row in accounts}
    if len(account_by_id) != len(targets):
        raise VideoPublishError("account_missing", "部分选中账号已删除或不可用，请刷新账号列表。", 409)
    schedule_at = normalize_video_publish_schedule(schedule_at, [])
    normalized_targets: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for target in targets:
        account = account_by_id[str(target["account_id"])]
        platform_id = str(account["platform_id"])
        if account["status"] != "connected":
            raise VideoPublishError("account_not_connected", f"账号「{_account_display_name(account)}」尚未确认登录，请先登录并检查账号。", 409)
        options = target.get("options") if isinstance(target.get("options"), dict) else {}
        options = _normalize_video_publish_target_options(
            platform_id=platform_id,
            schedule_at=schedule_at,
            options=options,
            common_title=clean_title,
            common_description=description,
            common_tags=clean_tags,
        )
        normalized_targets.append((account, options))

    if local_source_id:
        receipt = get_local_video_source(database, local_source_id, verify_hash=True)
        source_path, media_name, expected_sha = receipt.path, receipt.filename, receipt.sha256
        resolved_asset_id = None
    elif upload_id:
        try:
            receipt = get_upload(upload_id, verify_hash=True)
        except UploadError as exc:
            raise VideoPublishError("upload_invalid", str(exc)) from exc
        if receipt.detected_media_type != "video":
            raise VideoPublishError("video_required", "视频发布任务只能选择视频文件。")
        source_path, media_name, expected_sha = receipt.path, receipt.filename, receipt.sha256
        resolved_asset_id = None
    else:
        if archive_root is None:
            raise VideoPublishError("archive_unconfigured", "请先在知识库与设置中配置归档目录，再从素材库选择视频。", 409)
        source_path, media_name, expected_sha = _resolve_asset_video(database, archive_root, str(asset_id))
        resolved_asset_id = asset_id

    if source_path.suffix.casefold() not in _VIDEO_SUFFIXES:
        raise VideoPublishError("video_required", "所选文件不是支持的视频格式。")
    has_douyin_target = any(str(account["platform_id"]) == "douyin" for account, _ in normalized_targets)
    if not has_douyin_target and (cover_portrait_upload_id or cover_landscape_upload_id):
        raise VideoPublishError("cover_platform_invalid", "抖音比例封面需要选择抖音账号后再上传。")

    target_cover_upload_ids: dict[str, dict[str, str]] = {}
    all_cover_upload_ids = {
        upload_id for upload_id in (
            cover_portrait_upload_id, cover_landscape_upload_id,
        ) if upload_id
    }
    for account, options in normalized_targets:
        platform_id = str(account["platform_id"])
        general_id = str(options.get("cover_upload_id") or "")
        if platform_id in {"douyin", "wechat_channels"}:
            portrait_id = str(options.get("cover_portrait_upload_id") or general_id or "")
            landscape_id = str(options.get("cover_landscape_upload_id") or general_id or "")
            if platform_id == "douyin":
                portrait_id = str(options.get("cover_portrait_upload_id") or cover_portrait_upload_id or general_id or "")
                landscape_id = str(options.get("cover_landscape_upload_id") or cover_landscape_upload_id or general_id or "")
            covers = {key: value for key, value in {"portrait": portrait_id, "landscape": landscape_id}.items() if value}
            if platform_id == "douyin" and covers and set(covers) != {"portrait", "landscape"}:
                raise VideoPublishError("douyin_cover_pair_required", "抖音自定义封面需要同时设置 3:4 和 4:3 两种比例。")
        elif platform_id == "bilibili":
            covers = {
                key: str(options.get(option_key) or "")
                for key, option_key in {"4_3": "cover_4_3_upload_id", "16_9": "cover_16_9_upload_id"}.items()
                if options.get(option_key)
            }
            if set(covers) != {"4_3", "16_9"}:
                raise VideoPublishError("bilibili_covers_required", "哔哩哔哩需要同时设置 4:3 和 16:9 两张封面。")
        elif platform_id == "baijiahao" and (
            options.get("cover_portrait_upload_id") or options.get("cover_16_9_upload_id")
        ):
            covers = {
                key: str(options[option_key])
                for key, option_key in {
                    "portrait": "cover_portrait_upload_id",
                    "16_9": "cover_16_9_upload_id",
                }.items()
                if options.get(option_key)
            }
        else:
            covers = {"single": general_id} if general_id else {}
        target_cover_upload_ids[str(account["id"])] = covers
        all_cover_upload_ids.update(covers.values())
        if platform_id == "baijiahao" and not (covers.get("single") or set(covers) == {"portrait", "16_9"}):
            raise VideoPublishError("cover_required", "发布到百家号时必须提供 3:4 和 16:9 两张封面。")
        if platform_id == "weibo" and not covers.get("single"):
            raise VideoPublishError("cover_required", f"发布到{_PLATFORM_BY_ID[platform_id]['name']}时必须上传封面。")

    cover_uploads = {upload_id: _read_cover_upload(database, upload_id) for upload_id in all_cover_upload_ids}
    if local_source_id:
        cover_uploads = {
            cover_id: cover if cover.managed else _store_video_cover(
                database, cover.filename, Path(cover.filename).suffix.casefold(), cover.path.read_bytes())
            for cover_id, cover in cover_uploads.items()
        }
    for account, _ in normalized_targets:
        platform_id = str(account["platform_id"])
        if platform_id == "weibo":
            cover = cover_uploads[target_cover_upload_ids[str(account["id"])]["single"]]
            if cover.size_bytes > 5 * 1024 * 1024:
                raise VideoPublishError("cover_too_large", "微博视频封面不能超过 5 MB，请压缩图片或单独上传微博封面。")
    batch_id = uuid.uuid4().hex
    media_path = source_path if local_source_id else _copy_video_to_managed_storage(source_path, batch_id, media_name, expected_sha)
    cover_filenames_by_upload_id: dict[str, str] = {}
    try:
        for upload_id, cover in cover_uploads.items():
            cover_filenames_by_upload_id[upload_id] = _attach_batch_cover(cover, batch_id, upload_id)
        if has_douyin_target and not any(
            target_cover_upload_ids[str(account["id"])] for account, _ in normalized_targets
            if str(account["platform_id"]) == "douyin"
        ):
            generated_douyin_covers = _generate_douyin_first_frame_covers(database, media_path)
        else:
            generated_douyin_covers = {}
        for account, options in normalized_targets:
            platform_id = str(account["platform_id"])
            covers = target_cover_upload_ids[str(account["id"])]
            for key in ("cover_upload_id", "cover_portrait_upload_id", "cover_landscape_upload_id", "cover_4_3_upload_id", "cover_16_9_upload_id"):
                options.pop(key, None)
            if platform_id in {"douyin", "wechat_channels"}:
                portrait_filename = cover_filenames_by_upload_id.get(covers.get("portrait", ""))
                landscape_filename = cover_filenames_by_upload_id.get(covers.get("landscape", ""))
                if portrait_filename:
                    options["cover_portrait_filename"] = portrait_filename
                elif platform_id == "douyin" and generated_douyin_covers.get("portrait"):
                    options["cover_portrait_filename"] = generated_douyin_covers["portrait"]
                if landscape_filename:
                    options["cover_landscape_filename"] = landscape_filename
                elif platform_id == "douyin" and generated_douyin_covers.get("landscape"):
                    options["cover_landscape_filename"] = generated_douyin_covers["landscape"]
            elif platform_id == "bilibili":
                cover_4_3_filename = cover_filenames_by_upload_id[covers["4_3"]]
                cover_16_9_filename = cover_filenames_by_upload_id[covers["16_9"]]
                # zhw 2026-11-03 14:48:17：SAU 仍只接收一个封面参数，双封面都归档，4:3 沿用现有发布参数。
                options["cover_filename"] = cover_4_3_filename
                options["cover_4_3_filename"] = cover_4_3_filename
                options["cover_16_9_filename"] = cover_16_9_filename
            elif platform_id == "baijiahao" and set(covers) == {"portrait", "16_9"}:
                cover_3_4_filename = cover_filenames_by_upload_id[covers["portrait"]]
                cover_16_9_filename = cover_filenames_by_upload_id[covers["16_9"]]
                options["cover_portrait_filename"] = cover_3_4_filename
                options["cover_16_9_filename"] = cover_16_9_filename
                options["cover_filename"] = cover_16_9_filename
            elif covers.get("single"):
                options["cover_filename"] = cover_filenames_by_upload_id[covers["single"]]
    except BaseException:
        _remove_owned_video_directory(batch_id)
        raise
    now = _utc_now()
    try:
        with database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO video_publish_batches
                   (id, idempotency_key, asset_id, media_path, media_name, media_size, media_sha256,
                    title, description, tags_json, schedule_at, status, created_at, updated_at, local_source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (batch_id, idempotency_key, resolved_asset_id, str(media_path), media_name,
                 media_path.stat().st_size, expected_sha, clean_title, description.strip(),
                 _json(clean_tags), schedule_at, now, now, local_source_id),
            )
            for account, options in normalized_targets:
                connection.execute(
                    """INSERT INTO video_publish_targets
                       (id, batch_id, account_id, platform_id, account_alias, account_name,
                        options_json, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (uuid.uuid4().hex, batch_id, str(account["id"]), str(account["platform_id"]),
                     str(account["account_alias"]), _account_display_name(account),
                     _json(options), now, now),
                )
    except sqlite3.IntegrityError as exc:
        _remove_owned_video_directory(batch_id)
        with database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM video_publish_batches WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if existing is not None:
            return get_video_publish_batch(database, str(existing["id"])) or {}
        raise VideoPublishError("batch_conflict", "发布任务创建冲突，请刷新后重试。", 409) from exc
    except BaseException:
        _remove_owned_video_directory(batch_id)
        raise
    if video_upload_id:
        delete_upload(video_upload_id)
    for uploaded_cover_id, cover in cover_uploads.items():
        if not getattr(cover, "managed", False):
            delete_video_publish_cover_upload(uploaded_cover_id)
    return get_video_publish_batch(database, batch_id) or {}


def _prepare_video_publish_draft_root() -> Path:
    project = PROJECT_ROOT.resolve(strict=True)
    try:
        relative = _VIDEO_PUBLISH_DRAFT_ROOT.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("draft_storage_invalid", "本地草稿保存目录无效。", 500) from exc
    current = project
    for part in relative.parts:
        current = current / part
        if _has_reparse_point(current):
            raise VideoPublishError("draft_storage_invalid", "本地草稿目录不能包含符号链接或目录联接。", 500)
        current.mkdir(exist_ok=True)
        if _has_reparse_point(current) or not current.is_dir():
            raise VideoPublishError("draft_storage_invalid", "本地草稿保存目录不可用。", 500)
    resolved = _VIDEO_PUBLISH_DRAFT_ROOT.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("draft_storage_invalid", "本地草稿目录超出工作台数据目录。", 500) from exc
    return resolved


def _copy_verified_draft_file(source: Path, target: Path, *, expected_sha: str, expected_size: int, maximum: int | None) -> None:
    if _has_reparse_point(source) or not source.is_file():
        raise VideoPublishError("draft_source_invalid", "草稿素材暂存文件不可用，请重新上传。", 409)
    target_root = _prepare_video_publish_draft_root()
    try:
        _assert_contained(target, target_root)
        if target.exists() or target.is_symlink():
            raise FileExistsError("draft destination already exists")
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as input_file, target.open("xb") as output_file:
            before = os.fstat(input_file.fileno())
            while chunk := input_file.read(1024 * 1024):
                total += len(chunk)
                if maximum is not None and total > maximum:
                    raise ValueError("draft file exceeds size limit")
                digest.update(chunk)
                output_file.write(chunk)
            after = os.fstat(input_file.fileno())
            output_file.flush()
            os.fsync(output_file.fileno())
        if (
            total != expected_size
            or before.st_size != total
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or digest.hexdigest() != expected_sha
        ):
            raise ValueError("draft file changed while copying")
    except (OSError, RuntimeError, ValueError) as exc:
        target.unlink(missing_ok=True)
        raise VideoPublishError("draft_copy_failed", "保存本地草稿素材失败，请重新上传后重试。", 409) from exc


def _normalize_video_publish_draft_form(form: dict[str, object]) -> tuple[dict[str, object], str]:
    allowed = {
        "title", "description", "tags", "schedule_mode", "schedule_at", "platform_schedules",
        "selected_account_ids", "active_platform_id", "overrides", "poi_selections",
        "poi_queries", "poi_city_codes", "xhs_poi_selections", "xhs_poi_queries",
        "repost_selections", "tag_draft", "cover_preferences",
    }
    if not isinstance(form, dict) or set(form) - allowed:
        raise VideoPublishError("draft_form_invalid", "本地草稿表单数据无效，请刷新页面后重试。")
    try:
        serialized = json.dumps(form, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        normalized = json.loads(serialized)
    except (TypeError, ValueError, RecursionError) as exc:
        raise VideoPublishError("draft_form_invalid", "本地草稿表单数据无法保存。") from exc
    if len(serialized.encode("utf-8")) > 512 * 1024:
        raise VideoPublishError("draft_form_too_large", "本地草稿设置过多，无法保存。")
    if not isinstance(normalized, dict):
        raise VideoPublishError("draft_form_invalid", "本地草稿表单数据无效。")
    title = normalized.get("title", "")
    description = normalized.get("description", "")
    tags = normalized.get("tags", [])
    schedule_mode = normalized.get("schedule_mode", "immediate")
    selected_ids = normalized.get("selected_account_ids", [])
    if (
        not isinstance(title, str) or len(title) > 200
        or not isinstance(description, str) or len(description) > 1000
        or not isinstance(tags, list) or len(tags) > 30
        or any(not isinstance(item, str) or len(item) > 50 for item in tags)
        or not isinstance(schedule_mode, str) or schedule_mode not in {"immediate", "scheduled"}
        or not isinstance(selected_ids, list) or len(selected_ids) > _MAX_TARGETS
        or any(not isinstance(item, str) or len(item) > 64 for item in selected_ids)
    ):
        raise VideoPublishError("draft_form_invalid", "本地草稿中的标题、简介、标签或账号设置无效。")
    return normalized, serialized


def save_video_publish_draft(
    database: Database,
    *,
    draft_id: str | None,
    upload_id: str | None,
    cover_upload_ids: dict[str, str],
    form: dict[str, object],
    local_source_id: str | None = None,
) -> dict[str, object]:
    if local_source_id:
        if upload_id:
            raise VideoPublishError("media_source_invalid", "请选择一个视频来源。")
        return _save_local_video_publish_draft(database, draft_id, local_source_id, cover_upload_ids, form)
    normalized_form, form_json = _normalize_video_publish_draft_form(form)
    media = get_upload(upload_id, verify_hash=True)
    if media.detected_media_type != "video" or Path(media.filename).suffix.casefold() not in _VIDEO_SUFFIXES:
        raise VideoPublishError("draft_video_required", "本地草稿需要一个有效的视频文件。")
    if len(cover_upload_ids) > 80 or any(
        not _VIDEO_PUBLISH_DRAFT_SLOT.fullmatch(slot)
        or not _PUBLISH_COVER_ID.fullmatch(upload_id_value)
        for slot, upload_id_value in cover_upload_ids.items()
    ):
        raise VideoPublishError("draft_covers_invalid", "本地草稿封面信息无效，请重新选择封面。")

    resolved_draft_id = draft_id or uuid.uuid4().hex
    if not _VIDEO_PUBLISH_DRAFT_ID.fullmatch(resolved_draft_id):
        raise VideoPublishError("draft_id_invalid", "本地草稿标识无效，请刷新页面后重试。")
    with database.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM video_publish_drafts WHERE id = ?", (resolved_draft_id,)
        ).fetchone()
    if draft_id and existing is None:
        raise VideoPublishError("draft_not_found", "本地草稿不存在或已删除，请刷新列表。", 404)

    covers_by_upload: dict[str, VideoPublishCoverUpload] = {}
    for cover_id in set(cover_upload_ids.values()):
        covers_by_upload[cover_id] = _read_cover_upload(database, cover_id)

    root = _prepare_video_publish_draft_root()
    temporary_id = uuid.uuid4().hex
    staging = root / f"{resolved_draft_id}.tmp-{temporary_id}"
    final_folder = root / resolved_draft_id
    backup = root / f"{resolved_draft_id}.backup-{temporary_id}"
    staging.mkdir()
    _assert_contained(staging, root, strict=True)
    moved_existing = False
    installed_new = False
    try:
        media_extension = Path(media.filename).suffix.casefold()
        _copy_verified_draft_file(
            media.path,
            staging / f"media{media_extension}",
            expected_sha=media.sha256,
            expected_size=media.size_bytes,
            maximum=None,
        )
        cover_manifest: dict[str, dict[str, object]] = {}
        for slot, cover_id in sorted(cover_upload_ids.items()):
            cover = covers_by_upload[cover_id]
            suffix = Path(cover.filename).suffix.casefold()
            if suffix not in _COVER_SUFFIXES:
                raise VideoPublishError("draft_cover_type_invalid", "本地草稿封面格式无效。")
            filename = f"cover-{cover_id}{suffix}"
            target = staging / filename
            if not cover.managed and not target.exists():
                _copy_verified_draft_file(
                    cover.path,
                    target,
                    expected_sha=cover.sha256,
                    expected_size=cover.size_bytes,
                    maximum=_MAX_COVER_BYTES,
                )
                _validate_cover_image(target, suffix)
            cover_manifest[slot] = {
                "stored_name": filename,
                "filename": cover.filename,
                "sha256": cover.sha256,
                "size_bytes": cover.size_bytes,
            }
            if cover.managed:
                cover_manifest[slot]["image_id"] = cover.upload_id

        now = datetime.now(timezone.utc).isoformat()
        media_name, media_extension = clean_upload_filename(media.filename)
        if final_folder.exists() or final_folder.is_symlink():
            if existing is None:
                raise VideoPublishError("draft_storage_conflict", "本地草稿文件目录冲突，请重新保存。", 409)
            _assert_contained(final_folder, root, strict=True)
            os.replace(final_folder, backup)
            moved_existing = True
        elif existing is not None:
            raise VideoPublishError("draft_storage_missing", "本地草稿文件已不存在，请删除该草稿后重新创建。", 409)
        os.replace(staging, final_folder)
        installed_new = True
        try:
            with database.transaction(write=True) as connection:
                if existing is None:
                    connection.execute(
                        """INSERT INTO video_publish_drafts
                           (id, media_name, media_extension, media_size, media_sha256, confirmed_over_limit,
                            form_json, covers_json, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (resolved_draft_id, media_name, media_extension, media.size_bytes, media.sha256,
                         int(media.confirmed_over_limit), form_json, _json(cover_manifest), now, now),
                    )
                else:
                    cursor = connection.execute(
                        """UPDATE video_publish_drafts
                           SET media_name = ?, media_extension = ?, media_size = ?, media_sha256 = ?,
                               confirmed_over_limit = ?, form_json = ?, covers_json = ?, updated_at = ?, local_source_id = NULL
                           WHERE id = ? AND updated_at = ?""",
                        (media_name, media_extension, media.size_bytes, media.sha256,
                         int(media.confirmed_over_limit), form_json, _json(cover_manifest), now,
                         resolved_draft_id, str(existing["updated_at"])),
                    )
                    if cursor.rowcount != 1:
                        raise VideoPublishError("draft_changed", "草稿已在其他页面更新，请刷新后重试。", 409)
        except BaseException:
            if installed_new:
                _remove_video_publish_draft_directory(final_folder)
                installed_new = False
            if moved_existing and backup.exists():
                os.replace(backup, final_folder)
                moved_existing = False
            raise
        if moved_existing:
            _remove_video_publish_draft_directory(backup)
            moved_existing = False
    except VideoPublishError:
        if staging.exists():
            _remove_video_publish_draft_directory(staging)
        if installed_new and final_folder.exists():
            _remove_video_publish_draft_directory(final_folder)
        if moved_existing and backup.exists() and not final_folder.exists():
            os.replace(backup, final_folder)
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if staging.exists():
            _remove_video_publish_draft_directory(staging)
        if installed_new and final_folder.exists():
            _remove_video_publish_draft_directory(final_folder)
        if moved_existing and backup.exists() and not final_folder.exists():
            os.replace(backup, final_folder)
        raise VideoPublishError("draft_save_failed", "保存本地草稿失败，请检查磁盘空间后重试。", 500) from exc
    saved = get_video_publish_draft(database, resolved_draft_id)
    if saved is None:
        raise VideoPublishError("draft_save_failed", "本地草稿已保存但无法读取，请刷新后重试。", 500)
    return saved


def _save_local_video_publish_draft(database: Database, draft_id: str | None, source_id: str,
                                    cover_upload_ids: dict[str, str], form: dict[str, object]) -> dict[str, object]:
    _, form_json = _normalize_video_publish_draft_form(form)
    media = get_local_video_source(database, source_id, verify_hash=True)
    if len(cover_upload_ids) > 80 or any(not _VIDEO_PUBLISH_DRAFT_SLOT.fullmatch(slot)
            or not _PUBLISH_COVER_ID.fullmatch(cover_id) for slot, cover_id in cover_upload_ids.items()):
        raise VideoPublishError("draft_covers_invalid", "本地草稿封面信息无效，请重新选择封面。")
    resolved_id = draft_id or uuid.uuid4().hex
    if not _VIDEO_PUBLISH_DRAFT_ID.fullmatch(resolved_id):
        raise VideoPublishError("draft_id_invalid", "本地草稿标识无效。")
    manifest: dict[str, dict[str, object]] = {}
    converted: dict[str, VideoPublishCoverUpload] = {}
    for slot, cover_id in sorted(cover_upload_ids.items()):
        cover = converted.get(cover_id) or _read_cover_upload(database, cover_id)
        if not cover.managed:
            cover = _store_video_cover(database, cover.filename, Path(cover.filename).suffix.casefold(), cover.path.read_bytes())
        converted[cover_id] = cover
        manifest[slot] = {"stored_name": f"cover-{cover.upload_id}{Path(cover.filename).suffix.casefold()}",
                          "filename": cover.filename, "sha256": cover.sha256, "size_bytes": cover.size_bytes,
                          "image_id": cover.upload_id}
    now = datetime.now(timezone.utc).isoformat()
    name, extension = clean_upload_filename(media.filename)
    with database.transaction(write=True) as connection:
        existing = connection.execute("SELECT * FROM video_publish_drafts WHERE id = ?", (resolved_id,)).fetchone()
        if draft_id and existing is None:
            raise VideoPublishError("draft_not_found", "本地草稿不存在或已删除。", 404)
        if existing is None:
            connection.execute("""INSERT INTO video_publish_drafts
                (id, media_name, media_extension, media_size, media_sha256, confirmed_over_limit,
                 form_json, covers_json, created_at, updated_at, local_source_id)
                 VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (resolved_id, name, extension, media.size_bytes, media.sha256, form_json, _json(manifest), now, now, source_id))
        else:
            connection.execute("""UPDATE video_publish_drafts SET media_name=?, media_extension=?, media_size=?,
                media_sha256=?, confirmed_over_limit=0, form_json=?, covers_json=?, updated_at=?, local_source_id=? WHERE id=?""",
                (name, extension, media.size_bytes, media.sha256, form_json, _json(manifest), now, source_id, resolved_id))
    if existing is not None and not existing["local_source_id"]:
        _remove_video_publish_draft_directory(_prepare_video_publish_draft_root() / resolved_id)
    return get_video_publish_draft(database, resolved_id) or {}


def list_video_publish_drafts(database: Database) -> list[dict[str, object]]:
    with database.transaction() as connection:
        rows = connection.execute("SELECT * FROM video_publish_drafts ORDER BY updated_at DESC, id DESC").fetchall()
        account_rows = connection.execute(
            "SELECT id, platform_id, account_alias, display_name, profile_name FROM video_publish_accounts"
        ).fetchall()
    accounts = {str(row["id"]): row for row in account_rows}
    return [_video_publish_draft_payload(row, accounts=accounts, include_form=False) for row in rows]


def get_video_publish_draft(database: Database, draft_id: str) -> dict[str, object] | None:
    if not _VIDEO_PUBLISH_DRAFT_ID.fullmatch(draft_id):
        raise VideoPublishError("draft_id_invalid", "本地草稿标识无效。")
    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM video_publish_drafts WHERE id = ?", (draft_id,)).fetchone()
        account_rows = connection.execute(
            "SELECT id, platform_id, account_alias, display_name, profile_name FROM video_publish_accounts"
        ).fetchall()
    if row is None:
        return None
    accounts = {str(account["id"]): account for account in account_rows}
    return _video_publish_draft_payload(row, accounts=accounts, include_form=True)


def _video_publish_draft_payload(
    row: sqlite3.Row,
    *,
    accounts: dict[str, sqlite3.Row],
    include_form: bool,
) -> dict[str, object]:
    form = _decode_object(row["form_json"])
    selected_ids = form.get("selected_account_ids", [])
    if not isinstance(selected_ids, list):
        selected_ids = []
    account_names: list[str] = []
    platform_ids: list[str] = []
    for account_id in selected_ids:
        account = accounts.get(str(account_id))
        if account is None:
            account_names.append("账号已不可用")
            continue
        name = _account_display_name(account)
        account_names.append(name)
        platform_id = str(account["platform_id"])
        if platform_id not in platform_ids:
            platform_ids.append(platform_id)
    title = form.get("title")
    payload: dict[str, object] = {
        "id": str(row["id"]),
        "title": str(title).strip() if isinstance(title, str) and title.strip() else str(row["media_name"]),
        "media_name": str(row["media_name"]),
        "media_size": int(row["media_size"]),
        "platforms": platform_ids,
        "account_names": account_names,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "linked_media": bool(row["local_source_id"]),
    }
    if include_form:
        payload.update({
            "media_extension": str(row["media_extension"]),
            "media_sha256": str(row["media_sha256"]),
            "confirmed_over_limit": bool(row["confirmed_over_limit"]),
            "form": form,
            "covers": _decode_object(row["covers_json"]),
            "local_source_id": row["local_source_id"],
        })
    return payload


def get_video_publish_draft_media_path(database: Database, draft_id: str) -> Path | None:
    draft = get_video_publish_draft(database, draft_id)
    if draft is None:
        return None
    if draft.get("local_source_id"):
        return get_local_video_source(database, str(draft["local_source_id"])).path
    root = _prepare_video_publish_draft_root()
    folder = root / draft_id
    try:
        _assert_contained(folder, root, strict=True)
        if _has_reparse_point(folder) or not folder.is_dir():
            raise ValueError("invalid draft directory")
        path = folder / f"media{draft['media_extension']}"
        _assert_contained(path, folder, strict=True)
        if _has_reparse_point(path) or not path.is_file() or path.stat().st_size != draft["media_size"]:
            raise ValueError("invalid draft media file")
        return path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise VideoPublishError("draft_media_missing", "本地草稿视频文件不可用，请删除该草稿后重新上传。", 409) from exc


def get_video_publish_draft_cover_path(database: Database, draft_id: str, slot: str) -> Path | None:
    draft = get_video_publish_draft(database, draft_id)
    if draft is None:
        return None
    if not _VIDEO_PUBLISH_DRAFT_SLOT.fullmatch(slot):
        raise VideoPublishError("draft_cover_slot_invalid", "本地草稿封面位置无效。")
    covers = draft.get("covers")
    cover = covers.get(slot) if isinstance(covers, dict) else None
    if not isinstance(cover, dict):
        return None
    image_id = cover.get("image_id")
    if isinstance(image_id, str):
        image = _read_cover_upload(database, image_id)
        if not image.managed or image.sha256 != cover.get("sha256") or image.size_bytes != cover.get("size_bytes"):
            raise VideoPublishError("draft_cover_invalid", "本地草稿封面记录无效。", 409)
        return image.path
    filename = cover.get("stored_name")
    sha256 = cover.get("sha256")
    size_bytes = cover.get("size_bytes")
    if (
        not isinstance(filename, str)
        or not re.fullmatch(r"cover-[0-9a-f]{32}\.(?:jpe?g|png|webp)", filename)
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
    ):
        raise VideoPublishError("draft_cover_invalid", "本地草稿封面记录无效。", 409)
    root = _prepare_video_publish_draft_root()
    folder = root / draft_id
    try:
        _assert_contained(folder, root, strict=True)
        path = folder / filename
        _assert_contained(path, folder, strict=True)
        if _has_reparse_point(folder) or _has_reparse_point(path) or not path.is_file() or path.stat().st_size != size_bytes:
            raise ValueError("invalid draft cover file")
        if _sha256_file(path) != sha256:
            raise ValueError("draft cover hash mismatch")
        _validate_cover_image(path, path.suffix.casefold())
        return path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise VideoPublishError("draft_cover_missing", "本地草稿封面文件不可用，请重新上传封面。", 409) from exc


async def restore_video_publish_draft(database: Database, draft_id: str) -> dict[str, object] | None:
    draft = get_video_publish_draft(database, draft_id)
    if draft is None:
        return None
    local_source_id = draft.get("local_source_id")
    media_upload: UploadReceipt | None
    if local_source_id:
        try:
            media_upload = get_local_video_source(database, str(local_source_id), verify_hash=True)
        except VideoPublishError:
            media_upload = None
    else:
        media_path = get_video_publish_draft_media_path(database, draft_id)
        if media_path is None:
            return None
        media_upload = await save_upload_from_path(media_path, filename=str(draft["media_name"]),
                                                   confirm_over_limit=bool(draft["confirmed_over_limit"]))
    if media_upload and (media_upload.size_bytes != int(draft["media_size"]) or media_upload.sha256 != str(draft["media_sha256"])):
        if not local_source_id and media_upload:
            delete_upload(media_upload.upload_id)
        raise VideoPublishError("draft_media_corrupt", "本地草稿视频完整性校验失败，请重新上传视频后保存。", 409)
    restored_covers: dict[str, VideoPublishCoverUpload] = {}
    by_stored_name: dict[str, VideoPublishCoverUpload] = {}
    try:
        covers = draft.get("covers")
        if isinstance(covers, dict):
            for slot, raw_cover in covers.items():
                if not isinstance(slot, str) or not isinstance(raw_cover, dict):
                    continue
                stored_name = str(raw_cover.get("stored_name") or "")
                image_id = raw_cover.get("image_id")
                if isinstance(image_id, str):
                    upload = by_stored_name.get(stored_name)
                    if upload is None:
                        upload = _read_cover_upload(database, image_id)
                        if not upload.managed or upload.sha256 != raw_cover.get("sha256") or upload.size_bytes != raw_cover.get("size_bytes"):
                            raise VideoPublishError("draft_cover_invalid", "本地草稿封面记录无效。", 409)
                        by_stored_name[stored_name] = upload
                    restored_covers[slot] = upload
                    continue
                path = get_video_publish_draft_cover_path(database, draft_id, slot)
                if path is None:
                    raise VideoPublishError("draft_cover_missing", "本地草稿封面记录不完整，请重新上传封面后保存。", 409)
                upload = by_stored_name.get(stored_name)
                if upload is None:
                    upload = _store_video_cover(
                        database, str(raw_cover.get("filename") or stored_name), path.suffix.casefold(), path.read_bytes(),
                    )
                    by_stored_name[stored_name] = upload
                restored_covers[slot] = upload
    except BaseException:
        if not local_source_id:
            delete_upload(media_upload.upload_id)
        raise
    return {
        "draft": draft,
        "media_upload": (local_video_source_payload(media_upload) if local_source_id else media_upload.to_payload()) if media_upload else None,
        "cover_uploads": {slot: cover.to_payload() for slot, cover in restored_covers.items()},
    }


def _remove_video_publish_draft_directory(folder: Path) -> int | None:
    try:
        root = _prepare_video_publish_draft_root()
        if not folder.exists() and not folder.is_symlink():
            return 0
        _assert_contained(folder, root, strict=True)
        if _has_reparse_point(folder) or not folder.is_dir():
            return None
        items = list(folder.iterdir())
        for item in items:
            if _has_reparse_point(item) or not item.is_file() or not (
                re.fullmatch(r"media\.(?:mp4|mov|mkv|webm)", item.name)
                or re.fullmatch(r"cover-[0-9a-f]{32}\.(?:jpe?g|png|webp)", item.name)
            ):
                return None
        bytes_freed = sum(item.stat().st_size for item in items)
        for item in items:
            item.unlink()
        folder.rmdir()
        return bytes_freed
    except (OSError, RuntimeError, ValueError, VideoPublishError):
        return None


def delete_video_publish_draft(database: Database, draft_id: str) -> dict[str, int | bool] | None:
    if not _VIDEO_PUBLISH_DRAFT_ID.fullmatch(draft_id):
        raise VideoPublishError("draft_id_invalid", "本地草稿标识无效。")
    draft = get_video_publish_draft(database, draft_id)
    if draft is None:
        return None
    folder = _prepare_video_publish_draft_root() / draft_id
    bytes_freed = _remove_video_publish_draft_directory(folder)
    if bytes_freed is None:
        raise VideoPublishError("draft_delete_failed", "草稿文件清理失败，请关闭正在使用该文件的程序后重试。", 409)
    with database.transaction(write=True) as connection:
        cursor = connection.execute("DELETE FROM video_publish_drafts WHERE id = ?", (draft_id,))
        if cursor.rowcount != 1:
            raise VideoPublishError("draft_changed", "本地草稿已被其他页面删除，请刷新列表。", 409)
    return {"deleted": True, "bytes_freed": bytes_freed}


def list_video_publish_batches(
    database: Database,
    *,
    query: str = "",
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    query_clauses: list[str] = []
    query_parameters: list[object] = []
    clean_query = query.strip()[:200]
    if clean_query:
        query_clauses.append("(b.title LIKE ? OR b.media_name LIKE ? OR EXISTS (SELECT 1 FROM video_publish_targets t WHERE t.batch_id = b.id AND t.account_name LIKE ?))")
        query_parameters.extend([f"%{clean_query}%"] * 3)
    clauses = list(query_clauses)
    parameters = list(query_parameters)
    if status != "all":
        status_groups = {
            "in_progress": ("queued", "running"),
            "awaiting_confirmation": ("pending_review", "unknown"),
            "needs_attention": ("partial", "failed"),
        }
        if status in status_groups:
            values = status_groups[status]
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f"b.status IN ({placeholders})")
            parameters.extend(values)
        elif status == "published":
            clauses.append(
                """b.status = 'published' AND EXISTS (
                    SELECT 1 FROM video_publish_targets t
                    WHERE t.batch_id = b.id AND t.status = 'published'
                      AND COALESCE(json_extract(t.options_json, '$.save_as_draft'), 0) != 1
                )"""
            )
        elif status == "draft_saved":
            clauses.append(
                """b.status = 'published' AND EXISTS (
                    SELECT 1 FROM video_publish_targets t WHERE t.batch_id = b.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM video_publish_targets t
                    WHERE t.batch_id = b.id AND (
                        t.status != 'published'
                        OR COALESCE(json_extract(t.options_json, '$.save_as_draft'), 0) != 1
                    )
                )"""
            )
        elif status in {"queued", "running", "pending_review", "partial", "failed", "unknown", "cancelled"}:
            clauses.append("b.status = ?")
            parameters.append(status)
        else:
            raise VideoPublishError("status_invalid", "发布状态筛选无效。")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query_where = " WHERE " + " AND ".join(query_clauses) if query_clauses else ""
    draft_saved_clause = """b.status = 'published'
       AND EXISTS (SELECT 1 FROM video_publish_targets t WHERE t.batch_id = b.id)
       AND NOT EXISTS (
           SELECT 1 FROM video_publish_targets t
           WHERE t.batch_id = b.id AND (
               t.status != 'published'
               OR COALESCE(json_extract(t.options_json, '$.save_as_draft'), 0) != 1
           )
       )"""
    draft_saved_where = query_where + (" AND " if query_where else " WHERE ") + draft_saved_clause
    with database.transaction() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM video_publish_batches b" + where, parameters).fetchone()[0])
        grouped_rows = connection.execute(
            "SELECT b.status, COUNT(*) AS total FROM video_publish_batches b" + query_where + " GROUP BY b.status",
            query_parameters,
        ).fetchall()
        draft_saved_count = int(connection.execute(
            "SELECT COUNT(*) FROM video_publish_batches b" + draft_saved_where,
            query_parameters,
        ).fetchone()[0])
        rows = connection.execute(
            "SELECT b.* FROM video_publish_batches b" + where + " ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?",
            [*parameters, max(1, min(limit, 100)), max(0, offset)],
        ).fetchall()
    totals_by_status = {str(row["status"]): int(row["total"]) for row in grouped_rows}
    status_counts = {
        "all": sum(totals_by_status.values()),
        "in_progress": totals_by_status.get("queued", 0) + totals_by_status.get("running", 0),
        "awaiting_confirmation": totals_by_status.get("pending_review", 0) + totals_by_status.get("unknown", 0),
        "published": max(0, totals_by_status.get("published", 0) - draft_saved_count),
        "draft_saved": draft_saved_count,
        "needs_attention": totals_by_status.get("partial", 0) + totals_by_status.get("failed", 0),
    }
    return {
        "items": [get_video_publish_batch(database, str(row["id"])) for row in rows],
        "total": total,
        "limit": max(1, min(limit, 100)),
        "offset": max(0, offset),
        "status_counts": status_counts,
    }


def get_video_publish_batch(
    database: Database, batch_id: str, *, include_attempts: bool = False,
) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM video_publish_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        targets = connection.execute(
            "SELECT * FROM video_publish_targets WHERE batch_id = ? ORDER BY created_at, id", (batch_id,)
        ).fetchall()
        payload = _batch_payload(row)
        payload["targets"] = [_target_payload(target) for target in targets]
        if include_attempts and targets:
            attempts = connection.execute(
                """SELECT attempt.target_id, attempt.attempt_number, attempt.status,
                          attempt.exit_code, attempt.message, attempt.started_at, attempt.finished_at
                   FROM video_publish_attempts AS attempt
                   JOIN video_publish_targets AS target ON target.id = attempt.target_id
                   WHERE target.batch_id = ?
                   ORDER BY attempt.started_at, attempt.attempt_number""",
                (batch_id,),
            ).fetchall()
            attempts_by_target: dict[str, list[dict[str, Any]]] = {}
            for attempt in attempts:
                attempts_by_target.setdefault(str(attempt["target_id"]), []).append({
                    "attempt_number": int(attempt["attempt_number"]),
                    "status": str(attempt["status"]),
                    "exit_code": attempt["exit_code"],
                    "message": _safe_message(str(attempt["message"] or "")),
                    "started_at": str(attempt["started_at"]),
                    "finished_at": attempt["finished_at"],
                })
            for target in payload["targets"]:
                target["attempts"] = attempts_by_target.get(target["id"], [])
        payload["summary"] = _target_summary(targets)
        if payload["status"] == "published" and payload["summary"]["draft_saved"] == payload["summary"]["total"]:
            payload["status"] = "draft_saved"
        return payload


def get_video_publish_batch_cover_path(database: Database, batch_id: str, filename: str) -> Path:
    managed = IMAGE_FILENAME.fullmatch(filename) is not None
    if not _PUBLISH_COVER_ID.fullmatch(batch_id) or not (managed or _PUBLISH_BATCH_COVER_FILENAME.fullmatch(filename)):
        raise KeyError("视频发布封面不存在。")
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT options_json FROM video_publish_targets AS target
               JOIN video_publish_batches AS batch ON batch.id = target.batch_id
               WHERE target.batch_id = ? AND batch.media_cleaned = 0""",
            (batch_id,),
        ).fetchall()
    referenced_filenames: set[str] = set()
    for row in rows:
        options = _decode_object(row["options_json"])
        for key in _BATCH_COVER_KEYS:
            value = options.get(key)
            if isinstance(value, str):
                referenced_filenames.add(value)
    if filename not in referenced_filenames:
        raise KeyError("视频发布封面不存在。")

    try:
        if managed:
            with database.transaction() as connection:
                return _managed_batch_cover(connection, filename)
        root = _prepare_publish_media_root()
        folder = root / batch_id
        cover_path = folder / filename
        if _has_reparse_point(folder) or _has_reparse_point(cover_path):
            raise ValueError("publish cover contains a reparse point")
        resolved = _assert_contained(cover_path, folder, strict=True)
        if not resolved.is_file() or resolved.stat().st_size > _MAX_COVER_BYTES:
            raise ValueError("publish cover is unavailable")
        return resolved
    except VideoPublishError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise KeyError("视频发布封面不存在。") from exc


def get_video_publish_batch_media_path(database: Database, batch_id: str) -> Path | None:
    if not _PUBLISH_COVER_ID.fullmatch(batch_id):
        raise KeyError("视频发布批次不存在。")
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT media_path, media_cleaned, local_source_id, media_size FROM video_publish_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
    if row is None or row["media_cleaned"]:
        return None

    if row["local_source_id"]:
        receipt = get_local_video_source(database, str(row["local_source_id"]))
        if Path(str(row["media_path"])) != receipt.path:
            raise KeyError("视频发布素材不存在。")
        return receipt.path

    try:
        root = _prepare_publish_media_root()
        folder = root / batch_id
        path = Path(str(row["media_path"]))
        if _has_reparse_point(folder) or not folder.is_dir() or _has_reparse_point(path):
            raise ValueError("invalid publish media path")
        resolved = _assert_contained(path, folder, strict=True)
        if (
            resolved.suffix.casefold() not in _VIDEO_SUFFIXES
            or not resolved.is_file()
            or resolved.stat().st_size != row["media_size"]
        ):
            raise ValueError("invalid publish media file")
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise KeyError("视频发布素材不存在。") from exc


def edit_video_publish_target(database: Database, target_id: str, options: dict[str, Any], *, confirmed: bool) -> str:
    if not confirmed:
        raise VideoPublishError("confirmation_required", "请确认只修改当前账号的发布设置。", 400)
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT target.*, batch.title AS common_title, batch.description AS common_description,
                      batch.tags_json AS common_tags_json, batch.schedule_at, batch.media_path, batch.local_source_id, batch.media_cleaned,
                      account.status AS account_status, account.enabled AS account_enabled
               FROM video_publish_targets AS target
               JOIN video_publish_batches AS batch ON batch.id = target.batch_id
               JOIN video_publish_accounts AS account ON account.id = target.account_id
               WHERE target.id = ?""",
            (target_id,),
        ).fetchone()
    if row is None:
        raise KeyError("发布目标不存在。")
    if row["status"] not in {"failed", "cancelled"}:
        raise VideoPublishError("target_not_editable", "只有失败或已取消的账号目标可以编辑。请先停止任务或核实平台结果。", 409)
    if row["account_status"] != "connected" or not bool(row["account_enabled"]):
        raise VideoPublishError("account_not_connected", "该账号当前未连接，请检查账号登录状态后再编辑。", 409)
    media_path = Path(str(row["media_path"]))
    if row["media_cleaned"] or media_path.is_symlink() or media_path.parent.is_symlink() or not media_path.is_file():
        raise VideoPublishError("media_cleaned", "任务素材已清理或原视频不可用，请重新选择视频创建任务。", 409)
    if row["local_source_id"]:
        get_local_video_source(database, str(row["local_source_id"]), verify_hash=True)
    else:
        try:
            _assert_contained(media_path, _prepare_publish_media_root(), strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VideoPublishError("media_unavailable", "原任务视频文件不可用，无法安全编辑。", 409) from exc

    platform_id = str(row["platform_id"])
    common_title = str(row["common_title"])
    common_description = str(row["common_description"])
    common_tags = _decode_list(row["common_tags_json"])
    previous_options = _decode_object(row["options_json"])
    normalized_options = _normalize_video_publish_target_options(
        platform_id=platform_id,
        schedule_at=row["schedule_at"],
        options=options,
        common_title=common_title,
        common_description=common_description,
        common_tags=common_tags,
        allow_partial_douyin_cover_replacement=True,
        allow_partial_baijiahao_cover_replacement=(
            platform_id == "baijiahao"
            and any(previous_options.get(key) for key in (
                "cover_filename", "cover_portrait_filename", "cover_16_9_filename",
            ))
        ),
    )
    cover_filename_keys = (
        "cover_filename", "cover_4_3_filename", "cover_16_9_filename",
        "cover_portrait_filename", "cover_landscape_filename",
    )
    updated_options = {
        key: previous_options[key]
        for key in cover_filename_keys
        if isinstance(previous_options.get(key), str) and previous_options[key]
    }
    updated_options.update(normalized_options)

    cover_upload_fields = (
        ("cover_upload_id", "cover_filename"),
        ("cover_portrait_upload_id", "cover_portrait_filename"),
        ("cover_landscape_upload_id", "cover_landscape_filename"),
        ("cover_4_3_upload_id", "cover_4_3_filename"),
        ("cover_16_9_upload_id", "cover_16_9_filename"),
    )
    cover_upload_ids: set[str] = set()
    copied_cover_filenames: list[str] = []
    for upload_key, filename_key in cover_upload_fields:
        upload_id = normalized_options.get(upload_key)
        if not upload_id:
            continue
        cover_upload_ids.add(str(upload_id))
        try:
            cover = _read_cover_upload(database, str(upload_id))
            filename = _attach_batch_cover(cover, str(row["batch_id"]), str(upload_id))
        except BaseException:
            for copied_filename in copied_cover_filenames:
                _remove_video_publish_batch_cover(str(row["batch_id"]), copied_filename)
            raise
        updated_options[filename_key] = filename
        updated_options.pop(upload_key, None)
        if _PUBLISH_BATCH_COVER_FILENAME.fullmatch(filename):
            copied_cover_filenames.append(filename)
    if platform_id == "douyin" and (
        bool(updated_options.get("cover_portrait_filename")) != bool(updated_options.get("cover_landscape_filename"))
    ):
        for filename in copied_cover_filenames:
            _remove_video_publish_batch_cover(str(row["batch_id"]), filename)
        raise VideoPublishError("douyin_cover_pair_required", "抖音自定义封面需要同时设置 3:4 和 4:3 两种比例。")

    now = _utc_now()
    try:
        with database.transaction(write=True) as connection:
            current = connection.execute(
                """SELECT target.status, account.status AS account_status, account.enabled AS account_enabled
                   FROM video_publish_targets AS target
                   JOIN video_publish_accounts AS account ON account.id = target.account_id
                   WHERE target.id = ?""",
                (target_id,),
            ).fetchone()
            if current is None:
                raise KeyError("发布目标不存在。")
            if current["status"] not in {"failed", "cancelled"}:
                raise VideoPublishError("target_not_editable", "账号状态已变化，请刷新任务后再编辑。", 409)
            if current["account_status"] != "connected" or not bool(current["account_enabled"]):
                raise VideoPublishError("account_not_connected", "该账号当前未连接，请检查账号登录状态后再编辑。", 409)
            cursor = connection.execute(
                """UPDATE video_publish_targets
                   SET options_json = ?, status = 'queued', progress = NULL, message = '',
                       remote_id = NULL, remote_url = NULL, platform_status = NULL, platform_message = '',
                       platform_checked_at = NULL, platform_observed_at = NULL,
                       heartbeat_at = NULL, last_output_at = NULL, upload_retry_count = 0, diagnostic_code = NULL,
                       started_at = NULL, finished_at = NULL, updated_at = ?
                   WHERE id = ? AND status IN ('failed', 'cancelled')""",
                (_json(updated_options), now, target_id),
            )
            if cursor.rowcount != 1:
                raise VideoPublishError("target_not_editable", "账号状态已变化，请刷新任务后再编辑。", 409)
            _refresh_batch_status(connection, str(row["batch_id"]))
    except BaseException:
        for filename in copied_cover_filenames:
            _remove_video_publish_batch_cover(str(row["batch_id"]), filename)
        raise
    for upload_id in cover_upload_ids:
        delete_video_publish_cover_upload(upload_id)
    return target_id


def request_video_publish_retry(
    database: Database, target_id: str, *, confirmed: bool,
    change_schedule: bool = False, schedule_at: str | None = None, xhs_manual_submit: bool = False,
) -> str:
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT * FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise KeyError("发布目标不存在。")
        if xhs_manual_submit and (row["platform_id"] != "xiaohongshu" or os.name != "nt"):
            raise VideoPublishError("manual_submit_unsupported", "只有 Windows 上的小红书视频任务支持打开受控发布窗口。", 409)
        status = str(row["status"])
        if status == "failed":
            pass
        elif status == "unknown" and confirmed:
            pass
        elif status == "unknown":
            raise VideoPublishError("confirmation_required", "结果待核实。请先检查平台作品列表，确认未发布后再勾选确认重试。", 409)
        else:
            raise VideoPublishError("target_not_retryable", "只有明确失败的目标，或经人工确认未发布的待核实目标可以重试。", 409)
        options = _decode_object(row["options_json"])
        if row["platform_id"] == "baijiahao" and options.get("baijiahao_declaration") not in tuple(_BAIJIAHAO_DECLARATIONS):
            raise VideoPublishError("baijiahao_declaration_required", "请点击重新编辑，为百家号选择创作声明后重试。", 409)
        if row["platform_id"] == "douyin":
            try:
                validate_douyin_repost_options(options)
            except VideoPublishError as exc:
                raise VideoPublishError("repost_retry_incomplete", "该历史任务缺少完整的转载来源信息，请新建发布任务并重新选择来源。", 409) from exc
            try:
                validate_douyin_location_options(options)
            except VideoPublishError as exc:
                raise VideoPublishError("location_retry_incomplete", "该历史任务没有保存完整地点，请新建发布任务并重新选择地点。", 409) from exc
        batch = connection.execute("SELECT schedule_at, media_path, local_source_id, media_cleaned FROM video_publish_batches WHERE id = ?", (row["batch_id"],)).fetchone()
        if batch is None or batch["media_cleaned"] or not Path(str(batch["media_path"])).is_file():
            raise VideoPublishError("media_cleaned", "任务素材已清理或原视频不可用，请重新选择视频创建任务。", 409)
        if batch["local_source_id"]:
            get_local_video_source(database, str(batch["local_source_id"]), verify_hash=True)
        if change_schedule:
            if schedule_at and not bool(_PLATFORM_BY_ID[str(row["platform_id"])]["schedule"]):
                raise VideoPublishError("schedule_unsupported", "该平台暂不支持定时发布。")
            options["schedule_at_override"] = normalize_video_publish_schedule(schedule_at, [str(row["platform_id"])])
        else:
            effective = options.get("schedule_at_override", batch["schedule_at"] if batch else None)
            normalize_video_publish_schedule(effective, [str(row["platform_id"])])
        connection.execute(
            """UPDATE video_publish_targets SET status = 'queued', progress = NULL, message = '',
               remote_id = NULL, remote_url = NULL, platform_status = NULL, platform_message = '',
               platform_checked_at = NULL, platform_observed_at = NULL,
               heartbeat_at = NULL, last_output_at = NULL, upload_retry_count = 0, diagnostic_code = ?,
               options_json = ?, started_at = NULL, finished_at = NULL, updated_at = ? WHERE id = ?""",
            (_XHS_MANUAL_SUBMIT_CODE if xhs_manual_submit else None, _json(options), _utc_now(), target_id),
        )
        _refresh_batch_status(connection, str(row["batch_id"]))
    return target_id


def confirm_video_publish_target(database: Database, target_id: str, state: str, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed or state not in {"published", "draft_saved", "not_published"}:
        raise VideoPublishError("confirmation_invalid", "请确认平台后台核对结果。")
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT * FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise KeyError("发布目标不存在。")
        if row["status"] not in {"pending_review", "unknown"}:
            raise VideoPublishError("target_not_confirmable", "该发布目标当前不需要人工核实。", 409)
        options = _decode_object(row["options_json"])
        is_draft_target = row["platform_id"] == "wechat_channels" and bool(options.get("save_as_draft"))
        if (state == "draft_saved" and not is_draft_target) or (state == "published" and is_draft_target):
            raise VideoPublishError("confirmation_invalid", "请按该视频号任务的草稿保存方式确认结果。", 409)
        target_status = "published" if state in {"published", "draft_saved"} else "failed"
        if state == "draft_saved":
            message = "用户已在视频号后台确认草稿保存成功。"
        elif state == "published":
            message = "用户已在平台确认作品发布成功。"
        else:
            security_message = _xhs_submit_security_message(row["platform_id"], row["message"])
            message = (
                f"用户已核实作品未发布。{security_message}"
                if security_message else "用户已核实平台未接收作品，可安全重试。"
            )
        now = _utc_now()
        connection.execute(
            "UPDATE video_publish_targets SET status = ?, message = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (target_status, message, now, now, target_id),
        )
        connection.execute(
            """UPDATE video_publish_attempts SET status = ?, message = ?, finished_at = ?
               WHERE target_id = ? AND attempt_number = (SELECT MAX(attempt_number) FROM video_publish_attempts WHERE target_id = ?)""",
            (target_status, message, now, target_id, target_id),
        )
        _refresh_batch_status(connection, str(row["batch_id"]))
    return get_video_publish_target(database, target_id) or {}


def get_video_publish_target(database: Database, target_id: str) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
    return _target_payload(row) if row is not None else None


def clean_video_publish_batch_media(database: Database, batch_id: str) -> dict[str, int | bool]:
    """Delete a completed batch's managed video and cover copies, retaining history."""
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise KeyError("视频发布批次不存在。")
    with database.transaction(write=True) as connection:
        batch = connection.execute("SELECT id FROM video_publish_batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise KeyError("视频发布批次不存在。")
        targets = connection.execute(
            "SELECT status FROM video_publish_targets WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        if not targets or any(str(target["status"]) not in _VIDEO_PUBLISH_MEDIA_TERMINAL_STATUSES for target in targets):
            raise VideoPublishError("media_cleanup_not_allowed", "任务仍在排队、发布或等待确认，暂不能清理本机视频。", 409)
        bytes_freed = _remove_owned_video_directory(batch_id)
        if bytes_freed is None:
            raise VideoPublishError("media_cleanup_failed", "无法安全清理该任务的本机视频文件。", 500)
        connection.execute("UPDATE video_publish_batches SET media_cleaned = 1 WHERE id = ?", (batch_id,))
    return {"deleted": True, "bytes_freed": bytes_freed}


def delete_video_publish_batch(database: Database, batch_id: str) -> dict[str, int | bool]:
    """Delete a settled batch, its target history, and its managed media copies."""
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise KeyError("视频发布批次不存在。")
    with database.transaction(write=True) as connection:
        batch = connection.execute("SELECT id FROM video_publish_batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise KeyError("视频发布批次不存在。")
        targets = connection.execute(
            "SELECT status FROM video_publish_targets WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        if not targets or any(str(target["status"]) not in _VIDEO_PUBLISH_MEDIA_TERMINAL_STATUSES for target in targets):
            raise VideoPublishError(
                "batch_delete_not_allowed",
                "任务仍在排队、发布或等待确认，或有平台结果未知；请先停止任务并核实所有平台结果后再删除。",
                409,
            )
        bytes_freed = _remove_owned_video_directory(batch_id)
        if bytes_freed is None:
            raise VideoPublishError(
                "batch_delete_media_failed",
                "无法完整清理该任务的本机视频和封面副本；发布记录未删除，部分副本可能已清理，请刷新记录后检查。",
                500,
            )
        connection.execute("DELETE FROM video_publish_batches WHERE id = ?", (batch_id,))
    return {"deleted": True, "bytes_freed": bytes_freed}


def cleanup_expired_video_publish_media(database: Database, *, now: datetime | None = None) -> dict[str, int]:
    """Remove overdue managed media for batches whose every target has a definite result."""
    enabled, retention_days = _video_publish_cleanup_policy(database)
    now_utc = _as_utc_datetime(now or datetime.now(timezone.utc))
    if not enabled:
        return {"cleaned_batches": 0, "bytes_freed": 0}
    cutoff = now_utc - timedelta(days=retention_days)
    with database.transaction() as connection:
        candidates = connection.execute(
            """SELECT batch.id FROM video_publish_batches AS batch
               WHERE batch.media_cleaned = 0 AND EXISTS (
                   SELECT 1 FROM video_publish_targets AS target
                   WHERE target.batch_id = batch.id
               )
               AND NOT EXISTS (
                   SELECT 1 FROM video_publish_targets AS target
                   WHERE target.batch_id = batch.id
                     AND target.status NOT IN ('published', 'failed', 'cancelled')
               )
               ORDER BY batch.created_at, batch.id"""
        ).fetchall()
    cleaned_batches = 0
    bytes_freed = 0
    for candidate in candidates:
        batch_id = str(candidate["id"])
        with database.transaction(write=True) as connection:
            batch = connection.execute(
                "SELECT id FROM video_publish_batches WHERE id = ? AND media_cleaned = 0", (batch_id,),
            ).fetchone()
            if batch is None:
                continue
            targets = connection.execute(
                "SELECT status, finished_at, updated_at FROM video_publish_targets WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            if not targets or any(str(target["status"]) not in _VIDEO_PUBLISH_MEDIA_TERMINAL_STATUSES for target in targets):
                continue
            finished_times = [
                timestamp
                for target in targets
                if (timestamp := _parse_video_publish_timestamp(target["finished_at"] or target["updated_at"])) is not None
            ]
            if len(finished_times) != len(targets) or max(finished_times) > cutoff:
                continue
            removed_bytes = _remove_owned_video_directory(batch_id)
            if removed_bytes is None:
                _LOGGER.warning("could not safely clean expired video publish media for batch %s", batch_id)
                continue
            connection.execute("UPDATE video_publish_batches SET media_cleaned = 1 WHERE id = ?", (batch_id,))
            cleaned_batches += 1
            bytes_freed += removed_bytes
    bytes_freed += _cleanup_unused_video_covers(database, now_utc.timestamp())
    return {"cleaned_batches": cleaned_batches, "bytes_freed": bytes_freed}


def _cleanup_unused_video_covers(database: Database, cutoff: float) -> int:
    freed = 0
    with database.transaction(write=True) as connection:
        expired = connection.execute(
            "SELECT * FROM workbench_images WHERE kind = 'video_cover' AND keep_until <= ?", (cutoff,),
        ).fetchall()
        if not expired:
            return 0
        referenced: set[str] = set()
        for row in connection.execute("SELECT covers_json FROM video_publish_drafts"):
            for cover in _decode_object(row["covers_json"]).values():
                if isinstance(cover, dict) and isinstance(cover.get("image_id"), str):
                    referenced.add(cover["image_id"])
        for row in connection.execute(
            """SELECT options_json FROM video_publish_targets AS target
               JOIN video_publish_batches AS batch ON batch.id = target.batch_id
               WHERE batch.media_cleaned = 0""",
        ):
            options = _decode_object(row["options_json"])
            for key in _BATCH_COVER_KEYS:
                match = IMAGE_FILENAME.fullmatch(str(options.get(key) or ""))
                if match:
                    referenced.add(match[1])
        for row in expired:
            if row["id"] in referenced:
                continue
            try:
                freed += remove_registered_image(row)
                connection.execute("DELETE FROM workbench_images WHERE id = ?", (row["id"],))
            except (OSError, ValueError, RuntimeError):
                _LOGGER.warning("暂时无法清理视频封面 %s", row["id"])
    return freed


def _video_publish_cleanup_policy(database: Database) -> tuple[bool, int]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT key, value_json FROM settings WHERE key IN (?, ?)", _VIDEO_PUBLISH_CLEANUP_SETTING_KEYS,
        ).fetchall()
    values = {str(row["key"]): row["value_json"] for row in rows}
    if not values:
        return True, 15
    try:
        enabled = json.loads(values.get("video_publish_auto_cleanup_enabled", "true"))
        days = json.loads(values.get("video_publish_retention_days", "15"))
    except (TypeError, json.JSONDecodeError, RecursionError):
        return False, 15
    if not isinstance(enabled, bool) or isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
        return False, 15
    return enabled, days


def _parse_video_publish_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc_datetime(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def cancel_queued_video_target(database: Database, target_id: str) -> bool:
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT batch_id, status FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise KeyError("发布目标不存在。")
        if row["status"] == "queued":
            now = _utc_now()
            connection.execute(
                "UPDATE video_publish_targets SET status = 'cancelled', message = '用户已取消该平台任务。', updated_at = ?, finished_at = ? WHERE id = ?",
                (now, now, target_id),
            )
            _refresh_batch_status(connection, str(row["batch_id"]))
            return True
        if row["status"] == "running":
            return False
        raise VideoPublishError("target_not_cancellable", "只有排队中或正在上传的目标可以取消。", 409)


def recover_video_publish_targets(database: Database) -> list[str]:
    now = _utc_now()
    interrupted_message = "应用关闭或重启时任务中断，平台是否接收结果未知；请先检查平台后台，不要直接重发。"
    with database.transaction(write=True) as connection:
        rows = connection.execute("SELECT id, batch_id FROM video_publish_targets WHERE status = 'running'").fetchall()
        for row in rows:
            connection.execute(
                "UPDATE video_publish_targets SET status = 'unknown', message = ?, updated_at = ? WHERE id = ?",
                (interrupted_message, now, str(row["id"])),
            )
            connection.execute(
                """UPDATE video_publish_attempts SET status = 'unknown', message = ?, finished_at = ?
                   WHERE target_id = ? AND status = 'running'""",
                (interrupted_message, now, str(row["id"])),
            )
            _refresh_batch_status(connection, str(row["batch_id"]))
        queued = connection.execute(
            "SELECT id FROM video_publish_targets WHERE status = 'queued' ORDER BY created_at, id"
        ).fetchall()
    return [str(row["id"]) for row in queued]


def recover_video_publish_accounts(database: Database) -> None:
    now = _utc_now()
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE video_publish_accounts SET status = 'not_connected', message = '', updated_at = ?
               WHERE platform_id = 'xiaohongshu' AND status = 'error' AND message = ?""",
            (now, '为保护 Windows 账户，请在上次启动 15 分钟后再试。'),
        )
        connection.execute(
            """UPDATE video_publish_accounts
               SET status = 'not_connected',
                   message = '应用重启前的登录检查已中断，请重新检查账号状态。',
                   updated_at = ?
               WHERE status IN ('login_in_progress', 'checking')""",
            (now,),
        )


class VideoPublishManager:
    def __init__(self, database: Database, *, worker_count: int = 2) -> None:
        self.database = database
        self.adapter = SocialAutoUploadAdapter(database)
        self.worker_count = max(1, min(worker_count, 3))
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requests: set[str] = set()
        self._account_login_cancel_events: dict[str, threading.Event] = {}
        self._account_locks: dict[str, threading.Lock] = {}
        self._last_output_write: dict[str, float] = {}
        self._sync_pending: set[str] = set()
        self._sync_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._deferred_thread: threading.Thread | None = None
        self._deferred_publish: set[str] = set()
        self._started = False
        self.account_workbench_browser = AccountWorkbenchBrowserManager()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.database.migrate()
            self._backfill_xiaohongshu_identities()
            self._restore_saved_xhs_profiles()
            self._remove_legacy_duplicate_placeholders()
            self._recover_kuaishou_failed_checks()
            queued = recover_video_publish_targets(self.database)
            recover_video_publish_accounts(self.database)
            self._stop.clear()
            self._workers = [
                threading.Thread(target=self._worker, name=f"video-publish-{index + 1}", daemon=True)
                for index in range(self.worker_count)
            ]
            self._cleanup_thread = threading.Thread(
                target=self._media_cleanup_worker,
                name="video-publish-media-cleanup",
                daemon=True,
            )
            self._deferred_thread = threading.Thread(target=self._resume_deferred_worker, name='video-publish-resume', daemon=True)
            self._started = True
            for thread in self._workers:
                thread.start()
            self._cleanup_thread.start()
            self._deferred_thread.start()
            for target_id in queued:
                self._queue.put(("publish", target_id))

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._stop.set()
                processes = list(self._processes.values())
                workers = list(self._workers)
                scheduler = self._sync_thread
                cleanup_thread = self._cleanup_thread
                deferred_thread = self._deferred_thread
                self._deferred_thread = None
                self._deferred_publish.clear()
                self._sync_thread = None
                self._cleanup_thread = None
                self._workers = []
                self._started = False
            else:
                processes = []
                workers = []
                scheduler = None
                cleanup_thread = None
                deferred_thread = None
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        for _ in workers:
            self._queue.put(None)
        for thread in workers:
            thread.join(timeout=2)
        if deferred_thread:
            deferred_thread.join(timeout=2)
        if scheduler:
            scheduler.join(timeout=2)
        if cleanup_thread:
            cleanup_thread.join(timeout=2)
        self.account_workbench_browser.close_all()

    def open_account_workbench(self, account_id: str) -> dict[str, str]:
        account = _get_account(self.database, account_id)
        if account is None:
            raise KeyError(account_id)
        platform_id = str(account["platform_id"])
        if platform_id not in PLATFORM_WORKBENCH_URLS:
            raise VideoPublishError("workbench_unsupported", "该平台没有可复用的网页登录工作台。", 409)
        if account["status"] != "connected":
            raise VideoPublishError("account_not_connected", "账号未连接或登录状态已失效，请先检查账号状态。", 409)
        storage_state = self._load_account_workbench_state(account)
        try:
            self.account_workbench_browser.open(account_id, platform_id, storage_state)
        except BrowserSafetyError as exc:
            raise VideoPublishError("browser_safety", str(exc), 409) from None
        except Exception:
            raise VideoPublishError("workbench_unavailable", "平台创作工作台暂时无法打开，请稍后重试。", 409) from None
        return {"message": f"已打开{_PLATFORM_BY_ID[platform_id]['name']}创作工作台。"}

    def _load_account_workbench_state(self, account: dict[str, Any]) -> dict[str, Any]:
        platform_id = str(account["platform_id"])
        raw_project = self.adapter.configured_project_path()
        if not raw_project:
            raise VideoPublishError("sau_path_invalid", "请先配置 social-auto-upload 项目目录。", 409)
        project_path = Path(raw_project).expanduser()
        if _has_reparse_point(project_path):
            raise VideoPublishError("sau_path_invalid", "SAU 项目目录不可用，请检查账号管理配置。", 409)
        try:
            project = project_path.resolve(strict=True)
            project_root = project / "cookies"
            if (
                not project.is_dir()
                or not (project / "pyproject.toml").is_file()
                or _has_reparse_point(project / "pyproject.toml")
                or _has_reparse_point(project_root)
            ):
                raise OSError("invalid SAU session directory")
            cookies_root = project_root.resolve(strict=True)
            cookies_root.relative_to(project)
            if not cookies_root.is_dir():
                raise OSError("invalid cookies directory")
            alias = account["account_alias"]
            if not _ACCOUNT_ALIAS.fullmatch(alias):
                raise OSError("invalid account alias")
            prefix = _WORKBENCH_SESSION_PREFIXES[platform_id]
            session_file = cookies_root / f"{prefix}_{alias}.json"
            if _has_reparse_point(session_file):
                raise OSError("invalid session file")
            resolved_session = session_file.resolve(strict=True)
            resolved_session.relative_to(cookies_root)
            if not resolved_session.is_file() or resolved_session.stat().st_size > 5 * 1024 * 1024:
                raise OSError("invalid session file")
            with resolved_session.open("r", encoding="utf-8") as handle:
                saved_state = json.load(handle)
            if not isinstance(saved_state, dict):
                raise OSError("invalid browser state")
            if platform_id == "bilibili":
                identity = bilibili_identity(saved_state)
                cookie_info = saved_state.get("cookie_info")
                raw_cookies = cookie_info.get("cookies") if isinstance(cookie_info, dict) else None
                if not identity or not isinstance(raw_cookies, list):
                    raise OSError("invalid bilibili session")
                with self.database.transaction() as connection:
                    bound_identity = _bound_video_identity(connection, str(account["id"]))
                if bound_identity and identity != bound_identity:
                    raise OSError("bilibili identity changed")
                cookies: list[dict[str, Any]] = []
                for item in raw_cookies:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                        continue
                    try:
                        expires = float(item.get("expires", -1))
                        if expires > 100_000_000_000:
                            expires /= 1000
                    except (TypeError, ValueError):
                        expires = -1
                    cookies.append({
                        "name": item["name"],
                        "value": item["value"],
                        "domain": ".bilibili.com",
                        "path": "/",
                        "expires": expires,
                        "secure": True,
                        "httpOnly": item.get("http_only") in (True, 1),
                    })
                if not any(item["name"].casefold() == "sessdata" and item["value"] for item in cookies):
                    raise OSError("missing bilibili auth cookie")
                return {"cookies": cookies, "origins": []}
            cookies = saved_state.get("cookies")
            origins = saved_state.get("origins")
            if not isinstance(cookies, list) or not isinstance(origins, list):
                raise OSError("invalid browser state")
            return {"cookies": cookies, "origins": origins}
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            raise VideoPublishError(
                "account_session_unavailable",
                "未找到可用的网页登录状态，请重新检查该平台账号。",
                409,
            ) from None

    def _media_cleanup_worker(self) -> None:
        while not self._stop.is_set():
            try:
                result = cleanup_expired_video_publish_media(self.database)
                if result["cleaned_batches"]:
                    _LOGGER.info(
                        "automatically cleaned %s video publish batches and freed %s bytes",
                        result["cleaned_batches"], result["bytes_freed"],
                    )
            except Exception:
                _LOGGER.exception("video publish media cleanup failed")
            if self._stop.wait(60 * 60):
                return

    def enqueue_publish(self, target_id: str) -> None:
        self._queue.put(("publish", target_id))

    def publish_xiaohongshu_note(
        self,
        account_id: str,
        publication_id: str,
        image_paths: list[Path],
        title: str,
        note: str,
    ) -> SauResult:
        with self._account_lock(account_id):
            account = _get_account(self.database, account_id)
            if account is None or account.get("platform_id") != "xiaohongshu" or account.get("status") != "connected":
                return SauResult(None, "", launch_error="小红书账号未连接或已失效，请先检查登录状态。")
            project = self.adapter.configured_project_path()
            if not project:
                return SauResult(None, "", launch_error="请先配置 social-auto-upload 项目目录。")
            try:
                require_controlled_video_browser(Path(project))
            except BrowserSafetyError as exc:
                return SauResult(None, "", launch_error=str(exc), safety_blocked=True)
            key = f"article:{publication_id}"
            try:
                return self.adapter.upload_xiaohongshu_note(
                    str(account["account_alias"]), image_paths, title, note,
                    lambda process: self._register_process(key, process),
                )
            finally:
                with self._lock:
                    self._processes.pop(key, None)

    def publish_zhihu_article(
        self,
        account_id: str,
        publication_id: str,
        title: str,
        content_file: Path,
    ) -> SauResult:
        with self._account_lock(account_id):
            account = _get_account(self.database, account_id)
            if account is None or account.get('platform_id') != 'zhihu' or account.get('status') != 'connected':
                return SauResult(None, '', launch_error='知乎账号未连接或已失效，请先检查登录状态。')
            project = self.adapter.configured_project_path()
            if not project:
                return SauResult(None, '', launch_error='请先配置 social-auto-upload 项目目录。')
            try:
                project_root = Path(project).resolve(strict=True)
                cookies = project_root / 'cookies'
                if cookies.is_symlink() or not cookies.is_dir():
                    raise ValueError('cookies directory unavailable')
                cookies.resolve(strict=True).relative_to(project_root)
                alias = str(account['account_alias'])
                if not _ACCOUNT_ALIAS.fullmatch(alias):
                    raise ValueError('invalid alias')
                account_file = cookies / f'zhihu_{alias}.json'
                if account_file.is_symlink() or not account_file.is_file():
                    return SauResult(None, '', launch_error='知乎账号本机登录状态不可用，请重新登录。')
            except (OSError, RuntimeError, ValueError):
                return SauResult(None, '', launch_error='知乎账号本机登录状态不可用，请重新登录。')
            key = f'article:{publication_id}'
            try:
                return self.adapter.upload_zhihu_article(
                    alias, title, content_file,
                    lambda process: self._register_process(key, process),
                )
            finally:
                with self._lock:
                    self._processes.pop(key, None)

    def _resume_deferred_worker(self) -> None:
        while not self._stop.wait(10):
            self._resume_deferred_once()

    def _resume_deferred_once(self) -> None:
        with self._lock:
            pending = tuple(self._deferred_publish)
            self._deferred_publish.clear()
        for target_id in pending:
            self._queue.put(('publish', target_id))

    def _backfill_xiaohongshu_identities(self) -> None:
        raw = self.adapter.configured_project_path()
        if not raw:
            return
        try:
            project = Path(raw).resolve(strict=True)
            cookies = project / 'cookies'
            if cookies.is_symlink() or not cookies.is_dir():
                return
            with self.database.transaction(write=True) as connection:
                rows = connection.execute("SELECT id, account_alias FROM video_publish_accounts WHERE platform_id='xiaohongshu' AND enabled=1").fetchall()
                for row in rows:
                    alias = str(row['account_alias'])
                    if not _ACCOUNT_ALIAS.fullmatch(alias):
                        continue
                    identity = _read_xhs_identity(cookies / f'xiaohongshu_{alias}.json')
                    if identity:
                        connection.execute('INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)',
                                           (f"video_publish_identity:{row['id']}", json.dumps(identity), _utc_now()))
        except (OSError, ValueError, RuntimeError):
            _LOGGER.warning('could not inspect saved xiaohongshu account identities')

    def _restore_saved_xhs_profiles(self) -> None:
        if not browser_automation_paused():
            return
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT id, account_alias FROM video_publish_accounts
                WHERE platform_id='xiaohongshu' AND enabled=1 AND status='connected'
                AND (COALESCE(profile_name,'')='' OR COALESCE(avatar_url,'')='')""").fetchall()
        for row in rows:
            self._save_cached_xhs_profile(str(row['id']), str(row['account_alias']))

    def _save_cached_xhs_profile(self, account_id: str, account_alias: str) -> None:
        try:
            profile = self.adapter.read_account_profile('xiaohongshu', account_alias, lambda process: None)
            if profile:
                with self.database.transaction(write=True) as connection:
                    connection.execute("UPDATE video_publish_accounts SET profile_name=?, avatar_url=? WHERE id=? AND enabled=1",
                                       (profile['name'], profile['avatar_url'], account_id))
        except (OSError, ValueError, RuntimeError):
            _LOGGER.warning('could not read saved xiaohongshu account profile for %s', account_id)

    def enqueue_account_action(self, account_id: str, action: str) -> None:
        if action not in {"login", "check"}:
            raise ValueError("账号操作无效。")
        if action == "login":
            with self._lock:
                self._account_login_cancel_events[account_id] = threading.Event()
        self._queue.put((action, account_id))

    def cancel_account_login(self, account_id: str) -> None:
        account = _get_account(self.database, account_id)
        if account is None:
            raise KeyError(account_id)
        if account["status"] != "login_in_progress":
            raise VideoPublishError("account_login_not_running", "该账号当前没有正在进行的登录。", 409)
        with self._lock:
            cancel_event = self._account_login_cancel_events.get(account_id)
            if cancel_event is None:
                raise VideoPublishError("account_login_not_running", "登录已结束，请刷新账号状态。", 409)
            cancel_event.set()
            process = self._processes.get(f"account:{account_id}")
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def logout_account(self, account_id: str) -> None:
        lock = self._account_lock(account_id)
        if not lock.acquire(blocking=False):
            raise VideoPublishError("account_busy", "该账号正在使用中，请稍后再退出登录。", 409)
        try:
            raw_project = self.adapter.configured_project_path()
            try:
                if not raw_project:
                    raise ValueError("SAU not configured")
                project = Path(raw_project).resolve(strict=True)
                if not project.is_dir() or not (project / "pyproject.toml").is_file():
                    raise ValueError("SAU not available")
                cookies = project / "cookies"
                if cookies.is_symlink() or (cookies.exists() and not cookies.is_dir()):
                    raise ValueError("invalid cookies directory")
                if cookies.exists():
                    cookies.resolve(strict=True).relative_to(project)
            except (OSError, RuntimeError, ValueError):
                raise VideoPublishError("sau_path_invalid", "无法访问已配置的 SAU 目录，无法清除本机登录状态，请先检查配置。", 409) from None

            with self.database.transaction(write=True) as connection:
                account = connection.execute("SELECT * FROM video_publish_accounts WHERE id = ? AND enabled = 1", (account_id,)).fetchone()
                if account is None:
                    raise KeyError("视频发布账号不存在。")
                if account["status"] in {"login_in_progress", "checking"}:
                    raise VideoPublishError("account_busy", "账号正在登录或检查，请完成后再退出登录。", 409)
                active = connection.execute("SELECT 1 FROM video_publish_targets WHERE account_id = ? AND status IN ('queued', 'running') LIMIT 1", (account_id,)).fetchone()
                if active:
                    raise VideoPublishError("account_busy", "该账号有发布任务正在排队或执行，请完成或取消任务后再退出登录。", 409)
                platform = _PLATFORM_BY_ID.get(str(account["platform_id"]))
                alias = str(account["account_alias"])
                if platform is None or not _ACCOUNT_ALIAS.fullmatch(alias):
                    raise VideoPublishError("account_invalid", "账号信息无效，无法清除本机登录状态。", 409)
                credentials = [cookies / f"{platform['cli_id']}_{alias}.json"]
                if account["platform_id"] == "zhihu":
                    credentials.append(cookies / f"zhihu_{alias}.identity.json")
                try:
                    for credential in credentials:
                        if credential.is_symlink() or (credential.exists() and not credential.is_file()):
                            raise ValueError("invalid credential")
                        credential.unlink(missing_ok=True)
                except (OSError, ValueError):
                    raise VideoPublishError("logout_failed", "无法删除该账号的本机登录状态，请检查 SAU 目录权限后重试。", 409) from None
                connection.execute("UPDATE video_publish_targets SET account_name = ? WHERE account_id = ? AND (account_name IS NULL OR account_name = '')", (_account_display_name(account), account_id))
                connection.execute("UPDATE video_publish_accounts SET enabled = 0, status = 'not_connected', message = '', profile_name = '', avatar_url = '', updated_at = ? WHERE id = ?", (_utc_now(), account_id))
        finally:
            lock.release()

    def get_repost_history(self, account_id: str) -> list[dict[str, str]]:
        account = _get_account(self.database, account_id)
        if account is None:
            raise KeyError(account_id)
        if account["platform_id"] != "douyin" or account["status"] != "connected":
            raise VideoPublishError("repost_account_unavailable", "请先连接要选择站内作品的抖音账号。", 409)
        key = f"repost-history:{account_id}"
        try:
            with self._account_lock(account_id):
                result = self.adapter.run(["douyin", "repost-history", "--account", str(account["account_alias"])],
                                          lambda process: self._register_process(key, process), 90)
        finally:
            with self._lock:
                self._processes.pop(key, None)
        if result.launch_error:
            raise VideoPublishError("repost_history_unavailable", result.launch_error, 409)
        if result.timed_out or result.returncode != 0:
            raise VideoPublishError("repost_history_unavailable", "无法读取该账号的抖音观看历史，请确认登录状态后重试。", 409)
        marker = re.search(r"(?m)^DOUYIN_REPOST_HISTORY_JSON:(\{[^\r\n]*\})\s*$", result.output)
        try:
            payload = json.loads(marker.group(1)) if marker else None
        except (TypeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise VideoPublishError("repost_history_unavailable", "抖音观看历史返回的数据无法识别，请重试。", 502)
        return [{"id": item["id"], "title": item["title"]} for item in payload["items"][:20]
                if isinstance(item, dict) and isinstance(item.get("id"), str) and re.fullmatch(r"\d{5,30}", item["id"])
                and isinstance(item.get("title"), str) and 0 < len(item["title"]) <= 500]

    def search_xhs_poi(self, account_id: str, keyword: str) -> dict[str, object]:
        from .xhs_poi import XhsPoiError, search_poi
        account = _get_account(self.database, account_id)
        if account is None:
            raise KeyError(account_id)
        if account['platform_id'] != 'xiaohongshu' or account['status'] != 'connected':
            raise VideoPublishError('poi_account_unavailable', '请先连接要搜索地点的小红书账号。', 409)
        lock = self._account_lock(account_id)
        if not lock.acquire(blocking=False):
            raise VideoPublishError('poi_account_busy', '该小红书账号正在执行其他操作，请稍后再搜索地点。', 409)
        try:
            account_file, _ = self._xiaohongshu_cookie_paths(str(account['account_alias']), 'unused_pending')
            if account_file.is_symlink() or not account_file.is_file():
                raise VideoPublishError('poi_account_unavailable', '该小红书账号登录信息不可用，请重新登录。', 409)
            with self.database.transaction() as connection:
                bound = _bound_video_identity(connection, account_id)
            if bound and _read_xhs_identity(account_file) != bound:
                raise VideoPublishError('poi_account_unavailable', '账号登录身份与已绑定账号不一致，请重新登录。', 409)
            return {'items': search_poi(account_file, keyword)}
        except XhsPoiError as exc:
            raise VideoPublishError('poi_search_unavailable', str(exc), 409) from None
        finally:
            lock.release()

    def search_douyin_poi(self, account_id: str, keyword: str, city_code: str | None = None) -> dict[str, object]:
        account = _get_account(self.database, account_id)
        if account is None:
            raise KeyError(account_id)
        if account["platform_id"] != "douyin" or account["status"] != "connected":
            raise VideoPublishError("poi_account_unavailable", "请先连接要搜索地点的抖音账号。", 409)
        keyword = keyword.strip()
        if not keyword or len(keyword) > 100 or (city_code and not re.fullmatch(r"\d{6}", city_code)):
            raise VideoPublishError("poi_search_invalid", "请输入有效的地点关键词和城市。")
        args = ["douyin", "poi-search", "--account", str(account["account_alias"]), "--keyword", keyword]
        if city_code:
            args.extend(["--city-code", city_code])
        key = f"poi-search:{account_id}"
        account_lock = self._account_lock(account_id)
        if not account_lock.acquire(blocking=False):
            raise VideoPublishError("poi_account_busy", "该抖音账号正在执行其他操作，请稍后再搜索地点。", 409)
        try:
            result = self.adapter.run(args, lambda process: self._register_process(key, process), 90)
        finally:
            account_lock.release()
            with self._lock:
                self._processes.pop(key, None)
        if result.launch_error:
            raise VideoPublishError("poi_search_unavailable", result.launch_error, 409)
        if result.timed_out or result.returncode != 0:
            raise VideoPublishError("poi_search_unavailable", "无法读取抖音地点，请检查该账号登录状态或稍后重试。", 409)
        marker = re.search(r"(?m)^DOUYIN_POI_JSON:(\{[^\r\n]*\})\s*$", result.output)
        try:
            payload = json.loads(marker.group(1)) if marker else None
        except (TypeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not isinstance(payload.get("cities"), list):
            raise VideoPublishError("poi_search_unavailable", "抖音地点搜索结果无法识别，请重试。", 502)
        selected = str(payload.get("selected_city_code") or "")
        if not re.fullmatch(r"\d{6}", selected) or (city_code and selected != city_code):
            raise VideoPublishError("poi_search_invalid_city", "无法确认抖音返回的搜索城市，请重试。", 502)
        cities = [{"code": item["code"], "name": item["name"]} for item in payload["cities"][:50]
                  if isinstance(item, dict) and isinstance(item.get("code"), str) and re.fullmatch(r"\d{6}", item["code"])
                  and isinstance(item.get("name"), str) and 0 < len(item["name"]) <= 100]
        items = []
        for item in payload["items"][:30]:
            if not isinstance(item, dict):
                continue
            candidate = {"location": item.get("name"), "location_id": item.get("id"), "location_address": item.get("address"),
                         "location_city_code": selected, "location_city_name": next((c["name"] for c in cities if c["code"] == selected), "")}
            try:
                validate_douyin_location_options(candidate)
            except VideoPublishError:
                continue
            items.append({"id": candidate["location_id"], "name": candidate["location"], "address": candidate["location_address"],
                          "city_code": selected, "city_name": candidate.get("location_city_name", "")})
        return {"items": items, "cities": cities, "selected_city_code": selected}

    def enqueue_status_sync(self, target_id: str) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT platform_id, status, remote_id, platform_observed_at FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise KeyError(target_id)
        if row["platform_id"] != "douyin" or row["status"] in {"queued", "running", "cancelled"}:
            raise VideoPublishError("sync_unavailable", "该任务当前不能同步抖音状态。", 409)
        if row["status"] in {"unknown", "failed"} and not (row["remote_id"] and row["platform_observed_at"]):
            raise VideoPublishError("sync_identity_unknown", "该任务没有可确认的抖音作品标识，请先在平台核实，避免匹配到其他作品。", 409)
        try:
            require_safe_browser("manual douyin status")
        except BrowserSafetyError as exc:
            raise VideoPublishError("browser_safety_blocked", str(exc), 409) from exc
        with self._lock:
            if target_id in self._sync_pending:
                return False
            self._sync_pending.add(target_id)
        self._queue.put(("sync", target_id))
        return True

    def cancel_target(self, target_id: str) -> None:
        if cancel_queued_video_target(self.database, target_id):
            return
        process_key = f"publish:{target_id}"
        with self._lock:
            self._cancel_requests.add(target_id)
            process = self._processes.get(process_key)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                return
            action, item_id = item
            try:
                if action == "publish":
                    self._run_publish(item_id)
                elif action == "sync":
                    self._run_status_sync(item_id)
                else:
                    self._run_account_action(item_id, action)
            except Exception as exc:
                _LOGGER.exception("video publish worker failed for %s", item_id)
                if action == "publish":
                    _finish_video_publish_target(
                        self.database, item_id, "unknown",
                        f"发布执行器发生异常，平台结果待核实：{_safe_message(exc)}", None,
                    )
                elif action == "sync":
                    _LOGGER.warning("douyin status sync failed for %s", item_id)
                else:
                    try:
                        update_video_publish_account_status(self.database, item_id, "error", _safe_message(exc))
                    except KeyError:
                        _LOGGER.warning("video publish account disappeared while handling worker error: %s", item_id)
            finally:
                if action == "login":
                    with self._lock:
                        cancel_event = self._account_login_cancel_events.pop(item_id, None)
                    if cancel_event is not None and cancel_event.is_set():
                        try:
                            update_video_publish_account_status(self.database, item_id, "not_connected", "登录已取消。")
                        except KeyError:
                            _LOGGER.warning("video publish account disappeared after login cancellation: %s", item_id)
                if action == "sync":
                    with self._lock:
                        self._sync_pending.discard(item_id)
                self._queue.task_done()

    def _run_status_sync(self, target_id: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute("""SELECT t.*, b.title, b.schedule_at FROM video_publish_targets t
                JOIN video_publish_batches b ON b.id = t.batch_id WHERE t.id = ?""", (target_id,)).fetchone()
        if row is None or row["status"] in {"queued", "running", "cancelled"}:
            return
        target = dict(row)
        if target["status"] in {"unknown", "failed"} and not (target["remote_id"] and target["platform_observed_at"]):
            return
        previous_remote_id = target["remote_id"]
        if not target["platform_observed_at"]:
            target["remote_id"] = None
        options = _decode_object(target["options_json"])
        target["schedule_at"] = options.get("schedule_at_override", target["schedule_at"])
        target["effective_title"] = str(options.get("title") or target["title"])
        key = f"sync:{target_id}"
        with self._account_lock(str(target["account_id"])):
            if self._stop.is_set():
                return
            result = self.adapter.douyin_status(target, lambda process: self._register_process(key, process))
        with self._lock:
            self._processes.pop(key, None)
        state = str(result.get("status") or "unknown")
        remote_id = result.get("remote_id")
        if not isinstance(remote_id, str) or not re.fullmatch(r"\d{5,30}", remote_id):
            remote_id = None
        if target["remote_id"] and remote_id and target["remote_id"] != remote_id:
            state, remote_id = "unknown", None
        now = _utc_now()
        message = _safe_message(result.get("message") or "平台状态未确认。")
        with self.database.transaction(write=True) as connection:
            current = connection.execute("SELECT status, attempt_count, remote_id FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
            if current is None or current["attempt_count"] != target["attempt_count"] or current["status"] != target["status"] or current["remote_id"] != previous_remote_id:
                return
            if state != "unknown" and remote_id:
                connection.execute("""UPDATE video_publish_targets SET platform_status = ?, platform_message = ?, platform_checked_at = ?, platform_observed_at = ?, remote_id = ?, remote_url = ?, updated_at = ? WHERE id = ?""",
                    (state, message, now, now, remote_id, f"https://www.douyin.com/video/{remote_id}", now, target_id))
            else:
                connection.execute("UPDATE video_publish_targets SET platform_message = ?, platform_checked_at = ?, updated_at = ? WHERE id = ?", (message, now, now, target_id))

    def _run_publish(self, target_id: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT status,platform_id FROM video_publish_targets WHERE id=?", (target_id,)).fetchone()
        if row is None or row['status'] != 'queued':
            return
        if os.name == 'nt' and row['platform_id'] not in {'xiaohongshu', 'kuaishou', 'douyin', 'bilibili', 'wechat_channels', 'baijiahao'}:
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE video_publish_targets SET message=?, updated_at=? WHERE id=? AND status='queued'",
                                   ('该平台的受控发布暂未开放，任务已暂停。', _utc_now(), target_id))
            return
        try:
            project = self.adapter.configured_project_path()
            if not project:
                raise BrowserSafetyError('尚未配置发布组件，发布已暂停。')
            with xiaohongshu_login_slot():
                require_controlled_video_browser(Path(project))
                self._run_publish_guarded(target_id)
        except BrowserSafetyError as exc:
            with self.database.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE video_publish_targets SET message = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                    (str(exc), _utc_now(), target_id),
                )
            with self._lock:
                self._deferred_publish.add(target_id)

    def _run_publish_guarded(self, target_id: str) -> None:
        target = _claim_video_publish_target(self.database, target_id)
        if target is None:
            return
        key = f"publish:{target_id}"
        with self._account_lock(str(target["account_id"])):
            with self._lock:
                cancelled_before_start = target_id in self._cancel_requests
                self._cancel_requests.discard(target_id)
            if cancelled_before_start:
                _finish_video_publish_target(self.database, target_id, "cancelled", "已在上传启动前取消该平台任务。", None)
                return
            if self._stop.is_set():
                _finish_video_publish_target(self.database, target_id, "unknown", "应用正在关闭，发布任务尚未开始；请先检查平台后台。", None)
                return
            current_account = _get_account(self.database, str(target["account_id"]))
            if current_account is None or current_account["status"] != "connected":
                _finish_video_publish_target(self.database, target_id, "failed", "账号在任务启动前变为不可用，请重新检查账号后再试。", None)
                return
            if target["platform_id"] == "douyin":
                try:
                    validate_douyin_location_options(target["options"])
                except VideoPublishError:
                    _finish_video_publish_target(self.database, target_id, "failed", "该任务的地点信息不完整，请新建任务并重新搜索选择地点。", None)
                    return
            if target["platform_id"] == "xiaohongshu" and target["options"].get("xhs_declaration") == "self_shot":
                _finish_video_publish_target(self.database, target_id, "failed", _XHS_STAGE_MESSAGES["设置自主拍摄声明"], None)
                return
            try:
                normalize_video_publish_schedule(target.get("schedule_at"), [str(target["platform_id"])])
            except VideoPublishError as exc:
                _finish_video_publish_target(self.database, target_id, "failed", f"{exc.message} 请重新设置发布时间后重试。", None)
                return
            media_path = Path(str(target["media_path"]))
            if target.get("local_source_id"):
                try:
                    get_local_video_source(self.database, str(target["local_source_id"]), verify_hash=True)
                except VideoPublishError as exc:
                    _finish_video_publish_target(self.database, target_id, "failed", exc.message, None)
                    return
                with self._lock:
                    cancelled_during_check = target_id in self._cancel_requests
                    self._cancel_requests.discard(target_id)
                if cancelled_during_check:
                    _finish_video_publish_target(self.database, target_id, "cancelled", "已在上传启动前取消该平台任务。", None)
                    return
                if self._stop.is_set():
                    _finish_video_publish_target(self.database, target_id, "unknown", "应用正在关闭，发布任务尚未开始；请先检查平台后台。", None)
                    return
            if media_path.is_symlink() or not media_path.is_file():
                _finish_video_publish_target(self.database, target_id, "failed", "发布视频文件已不存在，请重新选择素材创建任务。", None)
                return
            self.adapter._set_upload_progress_callbacks(
                lambda line: self._record_publish_output(target_id, line),
                lambda: self._record_publish_heartbeat(target_id),
            )
            try:
                result = self.adapter.upload(target, lambda process: self._register_process(key, process))
            finally:
                self.adapter._set_upload_progress_callbacks(None, None)
        with self._lock:
            cancelled = target_id in self._cancel_requests
            self._cancel_requests.discard(target_id)
            self._processes.pop(key, None)
            self._last_output_write.pop(target_id, None)
        if result.safety_blocked and not cancelled:
            with self.database.transaction(write=True) as connection:
                row = connection.execute("SELECT batch_id,attempt_count,status FROM video_publish_targets WHERE id=?", (target_id,)).fetchone()
                if row is not None and row['status'] == 'running':
                    connection.execute("DELETE FROM video_publish_attempts WHERE target_id=? AND attempt_number=? AND status='running'", (target_id, row['attempt_count']))
                    connection.execute("UPDATE video_publish_targets SET status='queued',attempt_count=attempt_count-1,progress=NULL,message=?,started_at=NULL,updated_at=? WHERE id=?", (result.launch_error or '安全检查未通过，等待恢复。', _utc_now(), target_id))
                    _refresh_batch_status(connection, str(row['batch_id']))
            with self._lock:
                self._deferred_publish.add(target_id)
        elif result.safety_blocked and cancelled:
            _finish_video_publish_target(self.database, target_id, 'cancelled', '已在浏览器启动前取消任务。', None)
        elif result.launch_error:
            _finish_video_publish_target(self.database, target_id, "failed", result.launch_error, None)
        elif cancelled:
            _finish_video_publish_target(
                self.database, target_id, "unknown",
                "已请求停止上传，平台是否接收作品未知；请先检查平台后台。", result.returncode,
            )
        elif result.timed_out:
            _finish_video_publish_target(
                self.database, target_id, "unknown",
                "上传超时，平台是否接收作品未知；请先检查平台后台。", result.returncode,
            )
        elif result.returncode == 0:
            if target["platform_id"] == "wechat_channels" and target["options"].get("save_as_draft"):
                message = "草稿保存请求已提交，请到视频号草稿箱确认。"
            else:
                message = "发布任务已提交，请到平台确认作品状态。"
            _finish_video_publish_target(self.database, target_id, "pending_review", message, result.returncode)
        else:
            if target["platform_id"] == "wechat_channels" and _wechat_channels_cookie_expired(result.output):
                update_video_publish_account_status(
                    self.database,
                    str(target["account_id"]),
                    "expired",
                    "视频号登录 Cookie 已失效，请重新扫码登录。",
                )
                _finish_video_publish_target(
                    self.database,
                    target_id,
                    "failed",
                    "视频号登录 Cookie 已失效，本次发布在上传前停止；请重新扫码登录后重试。",
                    result.returncode,
                )
                return
            status, message = _video_publish_failure(str(target["platform_id"]), result.output)
            _finish_video_publish_target(self.database, target_id, status, message, result.returncode)

    def _run_account_action(self, account_id: str, action: str) -> None:
        with self._account_lock(account_id):
            if action == "login" and self._account_login_cancellation_requested(account_id):
                return
            self._run_account_action_locked(account_id, action)

    def _account_login_cancellation_requested(self, account_id: str) -> bool:
        with self._lock:
            cancel_event = self._account_login_cancel_events.get(account_id)
            return cancel_event.is_set() if cancel_event is not None else False

    def _xiaohongshu_cookie_paths(self, account_alias: str, pending_alias: str) -> tuple[Path, Path]:
        try:
            if not _ACCOUNT_ALIAS.fullmatch(account_alias) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,110}', pending_alias):
                raise ValueError('invalid account alias')
            raw = self.adapter.configured_project_path()
            if not raw:
                raise ValueError('missing project')
            project = Path(raw).resolve(strict=True)
            if not (project / 'pyproject.toml').is_file():
                raise ValueError('invalid project')
            cookies = project / 'cookies'
            if cookies.is_symlink():
                raise ValueError('linked cookies directory')
            cookies.mkdir(exist_ok=True)
            if not cookies.is_dir() or cookies.resolve(strict=True).parent != project:
                raise ValueError('invalid cookies directory')
            return cookies / f'xiaohongshu_{account_alias}.json', cookies / f'xiaohongshu_{pending_alias}.json'
        except (OSError, RuntimeError, ValueError):
            raise VideoPublishError('account_identity_unavailable', '无法核验账号信息，请检查登录环境后重试。', 409) from None

    def _reject_duplicate_placeholder(self, account: dict[str, Any], target: Path) -> None:
        now = _utc_now()
        with self.database.transaction(write=True) as connection:
            account_id = str(account['id'])
            has_history = connection.execute('SELECT 1 FROM video_publish_targets WHERE account_id = ? LIMIT 1', (account_id,)).fetchone()
            bound = connection.execute('SELECT 1 FROM settings WHERE key = ?', (f'video_publish_identity:{account_id}',)).fetchone()
            removable = not has_history and not bound and not target.exists() and not target.is_symlink()
            connection.execute("""UPDATE video_publish_accounts SET enabled = ?, status = 'error', message = ?,
                checked_at = ?, updated_at = ? WHERE id = ? AND enabled = 1""",
                (0 if removable else 1, _DUPLICATE_ACCOUNT_MESSAGE, now, now, account_id))

    def _remove_legacy_duplicate_placeholders(self) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT * FROM video_publish_accounts WHERE enabled = 1
                AND platform_id IN ('xiaohongshu', 'douyin') AND status = 'error' AND message = ?""",
                ('该账号已添加，请使用已有账号。',)).fetchall()
        for row in rows:
            try:
                target, _ = self._xiaohongshu_cookie_paths(str(row['account_alias']), 'unused_pending')
                if row['platform_id'] == 'douyin':
                    target = target.with_name(f"douyin_{row['account_alias']}.json")
                self._reject_duplicate_placeholder(dict(row), target)
            except VideoPublishError:
                _LOGGER.warning('could not safely clean up duplicate account %s', row['id'])

    def _recover_kuaishou_failed_checks(self) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT id,account_alias FROM video_publish_accounts
                WHERE platform_id='kuaishou' AND enabled=1 AND status='error' AND message=?""",
                ('暂时无法检查登录状态，请稍后重试。',)).fetchall()
        for row in rows:
            try:
                target, _ = self._xiaohongshu_cookie_paths(str(row['account_alias']), 'unused_pending')
                target = target.with_name(f"kuaishou_{row['account_alias']}.json")
                if target.is_symlink() or not target.is_file() or target.stat().st_size > 5 * 1024 * 1024:
                    continue
                with self.database.transaction(write=True) as connection:
                    if _bound_video_identity(connection, str(row['id'])):
                        connection.execute("""UPDATE video_publish_accounts SET status='connected', message=?, updated_at=?
                            WHERE id=? AND enabled=1 AND status='error' AND message=?""",
                            ('本次检查未完成，显示最近一次登录状态。', _utc_now(), row['id'], '暂时无法检查登录状态，请稍后重试。'))
            except (VideoPublishError, OSError, ValueError):
                _LOGGER.warning('could not restore previous kuaishou account state for %s', row['id'])

    def _accept_xiaohongshu_login(self, account: dict[str, Any], target: Path, pending: Path) -> None:
        self._accept_platform_login(account, target, pending, 'xiaohongshu', _read_xhs_identity)

    def _accept_platform_login(self, account: dict[str, Any], target: Path, pending: Path,
                               platform: str, identity_reader: Callable[[Path], str | None],
                               *, file_prefix: str | None = None) -> None:
        if self._account_login_cancellation_requested(str(account["id"])):
            raise VideoPublishError("account_login_cancelled", "登录已取消。", 409)
        identity = identity_reader(pending)
        if not identity:
            raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录后再试。', 409)
        with self.database.transaction(write=True) as connection:
            current = connection.execute('SELECT enabled FROM video_publish_accounts WHERE id = ?', (account['id'],)).fetchone()
            if current is None or not current['enabled']:
                raise VideoPublishError('account_unavailable', '账号已移除，请重新添加。', 409)
            if target.is_symlink():
                raise VideoPublishError('account_identity_unavailable', '无法核验账号信息，请检查登录环境后重试。', 409)
            bound_identity = _bound_video_identity(connection, str(account['id']))
            if bound_identity and bound_identity != identity:
                raise VideoPublishError('account_identity_changed', '该位置已绑定其他账号，请先移除或新建账号。', 409)
            if target.exists():
                old_identity = identity_reader(target)
                if not old_identity or (bound_identity and bound_identity != old_identity):
                    raise VideoPublishError('account_identity_unavailable', '无法确认原账号身份，请先检查原账号。', 409)
                if old_identity != identity:
                    raise VideoPublishError('account_identity_changed', '该位置已绑定其他账号，请先移除或新建账号。', 409)
            others = connection.execute("""SELECT id, account_alias, status FROM video_publish_accounts
                WHERE platform_id = ? AND enabled = 1 AND id != ?""", (platform, account['id'])).fetchall()
            for other in others:
                alias = str(other['account_alias'])
                if not _ACCOUNT_ALIAS.fullmatch(alias):
                    raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)
                existing = target.parent / f'{file_prefix or platform}_{alias}.json'
                bound_other = _bound_video_identity(connection, str(other['id']))
                if not existing.exists() and not existing.is_symlink():
                    if bound_other == identity:
                        raise VideoPublishError('duplicate_platform_account', '该账号已添加，请使用已有账号。', 409)
                    if not bound_other and other['status'] in {'connected', 'checking', 'expired'}:
                        raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)
                    continue
                other_identity = identity_reader(existing)
                if not other_identity or (bound_other and bound_other != other_identity):
                    raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)
                if other_identity == identity:
                    raise VideoPublishError('duplicate_platform_account', '该账号已添加，请使用已有账号。', 409)
            with self._lock:
                cancel_event = self._account_login_cancel_events.get(str(account["id"]))
                if cancel_event is not None and cancel_event.is_set():
                    raise VideoPublishError("account_login_cancelled", "登录已取消。", 409)
                os.replace(pending, target)
                now = _utc_now()
                connection.execute('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at',
                                   (f"video_publish_identity:{account['id']}", json.dumps(identity), now))
                connection.execute("""UPDATE video_publish_accounts SET status='connected', message=?, checked_at=?, updated_at=?
                                      WHERE id=? AND enabled=1""", ('登录成功。', now, now, account['id']))
                self._account_login_cancel_events.pop(str(account["id"]), None)

    def _run_xiaohongshu_login(self, account: dict[str, Any], on_start: Callable[[subprocess.Popen[str]], None]) -> None:
        account_id = str(account['id'])
        alias = str(account['account_alias'])
        with self.database.transaction() as connection:
            current = connection.execute('SELECT updated_at FROM video_publish_accounts WHERE id = ?', (account_id,)).fetchone()
        if current is None:
            return
        pending_alias = _xiaohongshu_pending_alias(alias, account_id, str(current['updated_at']))
        pending: Path | None = None
        pending_owned = False
        try:
            target, pending = self._xiaohongshu_cookie_paths(alias, pending_alias)
            if pending.exists() or pending.is_symlink():
                raise VideoPublishError('account_identity_unavailable', '无法准备登录，请重试。', 409)
            pending_owned = True
            result = self.adapter.login('xiaohongshu', pending_alias, on_start)
            if result.launch_error:
                update_video_publish_account_status(self.database, account_id, 'error', result.launch_error)
                return
            if result.returncode != 0 and "BrowserType.launch" in result.output and "Executable doesn't exist" in result.output:
                update_video_publish_account_status(self.database, account_id, 'error', '登录窗口无法打开，请检查浏览器是否已安装。')
                return
            if result.timed_out or result.returncode != 0:
                update_video_publish_account_status(self.database, account_id, 'not_connected', '登录窗口已关闭或超时，请重试。')
                return
            if is_windows_platform() and 'XHS_SAFE_LOGIN_VERIFIED' not in result.output:
                raise VideoPublishError('account_identity_unavailable', '无法确认登录结果，请重试。', 409)
            self._accept_xiaohongshu_login(account, target, pending)
            self._save_cached_xhs_profile(account_id, alias)
        except VideoPublishError as exc:
            if exc.code == 'duplicate_platform_account':
                self._reject_duplicate_placeholder(account, target)
            else:
                update_video_publish_account_status(self.database, account_id, 'error', exc.message)
        except (OSError, ValueError):
            _LOGGER.warning('could not finish xiaohongshu account login for %s', account_id)
            update_video_publish_account_status(self.database, account_id, 'error', '登录暂未完成，请重试。')
        finally:
            with self._lock:
                self._processes.pop(f'account:{account_id}', None)
            if pending is not None and pending_owned:
                try:
                    if pending.exists() and not pending.is_symlink() and pending.is_file():
                        pending.unlink()
                except OSError:
                    _LOGGER.warning('could not remove temporary account session for %s', account_id)

    def _run_bilibili_login(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        with self.database.transaction() as connection:
            current = connection.execute('SELECT updated_at FROM video_publish_accounts WHERE id = ?', (account_id,)).fetchone()
        if current is None:
            return
        pending_alias = _xiaohongshu_pending_alias(alias, account_id, str(current['updated_at']))
        pending = qr = None
        pending_owned = qr_owned = False
        try:
            target, pending = self._xiaohongshu_cookie_paths(alias, pending_alias)
            target = target.with_name(f'bilibili_{alias}.json')
            pending = pending.with_name(f'bilibili_{pending_alias}.json')
            qr = pending.with_name(f'bilibili_{pending_alias}_login_qrcode.png')
            if pending.exists() or pending.is_symlink() or qr.exists() or qr.is_symlink():
                raise VideoPublishError('account_identity_unavailable', '无法准备登录，请重试。', 409)

            def show_qr(image: bytes) -> None:
                nonlocal qr_owned
                with qr.open('xb') as stream:
                    qr_owned = True
                    stream.write(image)

            with self._lock:
                cancel_event = self._account_login_cancel_events.get(account_id)
            credentials, profile = run_bilibili_login(show_qr, self._stop, cancel_event)
            if not bilibili_identity(credentials):
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录后再试。', 409)
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            pending_owned = True
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(credentials, stream, ensure_ascii=False)
            self._accept_platform_login(account, target, pending, 'bilibili', _read_bilibili_identity)
            if profile:
                with self.database.transaction(write=True) as connection:
                    connection.execute('UPDATE video_publish_accounts SET profile_name = ?, avatar_url = ? WHERE id = ? AND enabled = 1',
                                       (profile.get('name', ''), profile.get('avatar_url', ''), account_id))
        except VideoPublishError as exc:
            if exc.code == 'duplicate_platform_account':
                self._reject_duplicate_placeholder(account, target)
            else:
                update_video_publish_account_status(self.database, account_id, 'error', exc.message)
        except BilibiliLoginError as exc:
            update_video_publish_account_status(self.database, account_id, 'error', str(exc))
        except (OSError, ValueError, TypeError):
            _LOGGER.warning('could not finish bilibili account login for %s', account_id)
            update_video_publish_account_status(self.database, account_id, 'error', '登录暂未完成，请重试。')
        finally:
            for path, owned in ((pending, pending_owned), (qr, qr_owned)):
                if path is not None and owned:
                    try:
                        if path.exists() and not path.is_symlink() and path.is_file():
                            path.unlink()
                    except OSError:
                        _LOGGER.warning('could not remove temporary login file for %s', account_id)

    def _launch_kuaishou_login(self, account_id: str, pending: Path, qr: Path, *, check: bool = False) -> dict[str, str]:
        raw_project = self.adapter.configured_project_path()
        if not raw_project:
            raise VideoPublishError('sau_path_invalid', '请先配置登录环境。', 409)
        project = Path(raw_project).resolve(strict=True)
        python = project / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            raise VideoPublishError('sau_path_invalid', '登录组件暂不可用，请检查运行环境。', 409)
        helper = Path(__file__).with_name('kuaishou_login_browser.py').resolve(strict=True)
        if hashlib.sha256(helper.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != '685f31f608cf62409ae089a726407421db5cf8e2aaba968a5dea56f6c15e295c':
            raise VideoPublishError('account_login_unavailable', '登录组件暂不可用，请检查运行环境。', 409)
        try:
            with xiaohongshu_login_slot():
                reserve_xiaohongshu_login(self.database, project)
                environment = os.environ.copy()
                environment.pop('PYTHONPATH', None)
                environment.pop('PYTHONHOME', None)
                args = [str(python), str(helper), str(project), str(pending), str(qr)]
                if check:
                    args.append('check')
                process = subprocess.Popen(args,
                                           cwd=str(project), env=environment, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           encoding='utf-8', errors='replace', shell=False,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._register_process(f'account:{account_id}', process)
                try:
                    output, _ = process.communicate(timeout=360)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise VideoPublishError('account_login_timeout', '登录超时，请重新登录。', 409) from None
                if process.returncode != 0:
                    raise VideoPublishError('account_login_failed', '登录未完成，请重新尝试。', 409)
                profile = json.loads(output)
                if not isinstance(profile, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(profile.get('identity', ''))):
                    raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
                return profile
        except BrowserSafetyError as exc:
            raise VideoPublishError('account_login_unavailable', str(exc), 409) from None
        except (OSError, ValueError, TypeError):
            raise VideoPublishError('account_login_failed', '登录未完成，请重新尝试。', 409) from None
        finally:
            with self._lock:
                self._processes.pop(f'account:{account_id}', None)

    def _launch_wechat_channels_login(self, account_id: str, session_file: Path, qr: Path,
                                      *, check: bool = False) -> dict[str, str]:
        raw_project = self.adapter.configured_project_path()
        if not raw_project:
            raise VideoPublishError('sau_path_invalid', '请先配置登录环境。', 409)
        project = Path(raw_project).resolve(strict=True)
        python = project / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            raise VideoPublishError('sau_path_invalid', '登录组件暂不可用，请检查运行环境。', 409)
        helper = Path(__file__).with_name('wechat_channels_login_browser.py').resolve(strict=True)
        if hashlib.sha256(helper.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != 'd1431cb109929abcfa7baf45a5472324555743f0943208f40ec1fae099a05e8d':
            raise VideoPublishError('account_login_unavailable', '登录组件暂不可用，请检查运行环境。', 409)
        try:
            with xiaohongshu_login_slot():
                reserve_xiaohongshu_login(self.database, project)
                environment = os.environ.copy()
                environment.pop('PYTHONPATH', None)
                environment.pop('PYTHONHOME', None)
                args = [str(python), str(helper), str(project), str(session_file), str(qr)]
                if check:
                    args.append('check')
                process = subprocess.Popen(args,
                                           cwd=str(project), env=environment, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           encoding='utf-8', errors='replace', shell=False,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._register_process(f'account:{account_id}', process)
                try:
                    output, _ = process.communicate(timeout=120 if check else 720)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise VideoPublishError('account_login_timeout', '登录超时，请重新尝试。', 409) from None
                if process.returncode != 0:
                    raise VideoPublishError('account_login_failed', '登录未完成，请重新尝试。', 409)
                result = json.loads(output)
                if not isinstance(result, dict) or result.get('status') not in {'connected', 'expired', 'unavailable', 'identity_mismatch'}:
                    raise VideoPublishError('account_login_failed', '登录未完成，请重新尝试。', 409)
                if result['status'] == 'connected' and not re.fullmatch(r'[0-9a-f]{64}', str(result.get('identity', ''))):
                    raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
                return result
        except BrowserSafetyError:
            raise VideoPublishError('account_login_unavailable', '暂时无法打开登录窗口，请稍后再试。', 409) from None
        except (OSError, ValueError, TypeError):
            raise VideoPublishError('account_login_failed', '登录未完成，请重新尝试。', 409) from None
        finally:
            with self._lock:
                self._processes.pop(f'account:{account_id}', None)

    def _wechat_channels_cookie_paths(self, alias: str, pending_alias: str) -> tuple[Path, Path]:
        target, pending = self._xiaohongshu_cookie_paths(alias, pending_alias)
        return target.with_name(f'tencent_{alias}.json'), pending.with_name(f'tencent_{pending_alias}.json')

    def _run_wechat_channels_login(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        with self.database.transaction() as connection:
            current = connection.execute('SELECT updated_at FROM video_publish_accounts WHERE id=?', (account_id,)).fetchone()
        if current is None:
            return
        pending_alias = _xiaohongshu_pending_alias(alias, account_id, str(current['updated_at']))
        pending = qr = target = None
        pending_owned = qr_owned = False
        try:
            target, pending = self._wechat_channels_cookie_paths(alias, pending_alias)
            qr = pending.with_name(f'tencent_{pending_alias}_login_qrcode.png')
            if any(path.exists() or path.is_symlink() for path in (pending, qr, qr.with_suffix('.png.tmp'))):
                raise VideoPublishError('account_identity_unavailable', '无法准备登录，请重试。', 409)
            pending_owned = qr_owned = True
            profile = self._launch_wechat_channels_login(account_id, pending, qr)
            identity = str(profile.get('identity', ''))
            if (profile.get('status') != 'connected' or not re.fullmatch(r'[0-9a-f]{64}', identity)
                    or pending.is_symlink() or not pending.is_file() or pending.stat().st_size > 5 * 1024 * 1024
                    or _read_wechat_channels_identity(pending) != identity):
                raise VideoPublishError('account_identity_unavailable', '无法确认视频号账号身份，请重新登录。', 409)
            self._accept_platform_login(account, target, pending, 'wechat_channels',
                                        _read_wechat_channels_identity, file_prefix='tencent')
            name = profile.get('name', '')
            if not isinstance(name, str) or len(name) > 80:
                name = ''
            avatar = _safe_wechat_channels_avatar(profile.get('avatar_url'))
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE video_publish_accounts SET profile_name=CASE WHEN ?<>'' THEN ? ELSE profile_name END, avatar_url=CASE WHEN ?<>'' THEN ? ELSE avatar_url END WHERE id=? AND enabled=1",
                                   (name.strip(), name.strip(), avatar, avatar, account_id))
        except VideoPublishError as exc:
            if exc.code == 'duplicate_platform_account' and target is not None:
                self._reject_duplicate_placeholder(account, target)
            else:
                update_video_publish_account_status(self.database, account_id, 'error', exc.message)
        except (OSError, ValueError, TypeError):
            _LOGGER.warning('could not finish wechat channels account login for %s', account_id)
            update_video_publish_account_status(self.database, account_id, 'error', '登录暂未完成，请重试。')
        finally:
            for path, owned in ((pending, pending_owned), (qr, qr_owned),
                                (qr.with_suffix('.png.tmp') if qr else None, qr_owned)):
                if path is not None and owned:
                    try:
                        if path.exists() and not path.is_symlink() and path.is_file():
                            path.unlink()
                    except OSError:
                        _LOGGER.warning('could not remove temporary login file for %s', account_id)

    def _run_kuaishou_login(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        with self.database.transaction() as connection:
            current = connection.execute('SELECT updated_at FROM video_publish_accounts WHERE id=?', (account_id,)).fetchone()
        if current is None:
            return
        pending_alias = _xiaohongshu_pending_alias(alias, account_id, str(current['updated_at']))
        pending = qr = target = None
        pending_owned = qr_owned = False
        try:
            target, pending = self._xiaohongshu_cookie_paths(alias, pending_alias)
            target = target.with_name(f'kuaishou_{alias}.json')
            pending = pending.with_name(f'kuaishou_{pending_alias}.json')
            qr = pending.with_name(f'kuaishou_{pending_alias}_login_qrcode.png')
            if any(path.exists() or path.is_symlink() for path in (pending, qr, qr.with_suffix('.tmp'))):
                raise VideoPublishError('account_identity_unavailable', '无法准备登录，请重试。', 409)
            pending_owned = qr_owned = True
            profile = self._launch_kuaishou_login(account_id, pending, qr)
            identity = str(profile.get('identity', ''))
            if not re.fullmatch(r'[0-9a-f]{64}', identity) or pending.is_symlink() or not pending.is_file() or pending.stat().st_size > 5 * 1024 * 1024:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            state = json.loads(pending.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
                raise VideoPublishError('account_identity_unavailable', '无法确认登录状态，请重新登录。', 409)
            with self.database.transaction(write=True) as connection:
                current = connection.execute('SELECT enabled FROM video_publish_accounts WHERE id=?', (account_id,)).fetchone()
                if current is None or not current['enabled']:
                    raise VideoPublishError('account_unavailable', '账号已移除，请重新添加。', 409)
                bound = _bound_video_identity(connection, account_id)
                if target.is_symlink() or (target.exists() and not bound):
                    raise VideoPublishError('account_identity_unavailable', '无法确认原账号身份，请先检查原账号。', 409)
                if bound and bound != identity:
                    raise VideoPublishError('account_identity_changed', '该位置已绑定其他账号，请先移除或新建账号。', 409)
                others = connection.execute("SELECT id,account_alias,status FROM video_publish_accounts WHERE platform_id='kuaishou' AND enabled=1 AND id!=?", (account_id,)).fetchall()
                for other in others:
                    other_identity = _bound_video_identity(connection, str(other['id']))
                    if other_identity == identity:
                        raise VideoPublishError('duplicate_platform_account', '该账号已添加，无需重复登录。', 409)
                    other_alias = str(other['account_alias'])
                    if not _ACCOUNT_ALIAS.fullmatch(other_alias):
                        raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)
                    other_path = target.parent / f'kuaishou_{other_alias}.json'
                    if not other_identity and (other_path.exists() or other_path.is_symlink() or other['status'] in {'connected', 'checking', 'expired'}):
                        raise VideoPublishError('account_identity_unavailable', '无法核验已有账号，请先检查账号列表。', 409)
                if self._account_login_cancellation_requested(account_id):
                    raise VideoPublishError('account_login_cancelled', '登录已取消。', 409)
                os.replace(pending, target)
                now = _utc_now()
                connection.execute('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at',
                                   (f'video_publish_identity:{account_id}', json.dumps(identity), now))
                name = profile.get('name', '')
                if not isinstance(name, str) or len(name) > 80:
                    name = ''
                connection.execute("UPDATE video_publish_accounts SET status='connected',message=?,profile_name=?,avatar_url=?,checked_at=?,updated_at=? WHERE id=? AND enabled=1",
                                   ('登录成功。', name.strip(), _safe_kuaishou_avatar(profile.get('avatar_url')), now, now, account_id))
        except VideoPublishError as exc:
            if exc.code == 'duplicate_platform_account' and target is not None:
                self._reject_duplicate_placeholder(account, target)
            else:
                update_video_publish_account_status(self.database, account_id, 'error', exc.message)
        except (OSError, ValueError, TypeError):
            _LOGGER.warning('could not finish kuaishou account login for %s', account_id)
            update_video_publish_account_status(self.database, account_id, 'error', '登录暂未完成，请重试。')
        finally:
            for path, owned in ((pending, pending_owned), (qr, qr_owned), (qr.with_suffix('.tmp') if qr else None, qr_owned)):
                if path is not None and owned:
                    try:
                        if path.exists() and not path.is_symlink() and path.is_file():
                            path.unlink()
                    except OSError:
                        _LOGGER.warning('could not remove temporary login file for %s', account_id)

    def _check_wechat_channels_account(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        try:
            target, _ = self._wechat_channels_cookie_paths(alias, 'unused_pending')
            if not target.exists() and not target.is_symlink():
                update_video_publish_account_status(self.database, account_id, 'expired', '登录状态已失效，请重新登录。')
                return
            if target.is_symlink() or not target.is_file() or target.stat().st_size > 5 * 1024 * 1024:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            with self.database.transaction() as connection:
                bound = _bound_video_identity(connection, account_id)
            if not bound or _read_wechat_channels_identity(target) != bound:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            profile = self._launch_wechat_channels_login(account_id, target, target.with_suffix('.check.png'), check=True)
            if profile.get('status') == 'expired':
                update_video_publish_account_status(self.database, account_id, 'expired', '登录状态已失效，请重新登录。')
                return
            if profile.get('status') == 'identity_mismatch':
                raise VideoPublishError('account_identity_changed', '当前登录账号与已绑定账号不一致，请重新登录。', 409)
            if profile.get('status') == 'unavailable':
                update_video_publish_account_status(
                    self.database, account_id, 'error', '本次检查未完成，无法确认登录状态，请稍后重试。',
                )
                return
            if profile.get('status') != 'connected' or profile.get('identity') != bound:
                raise VideoPublishError('account_identity_unavailable', '无法确认当前登录账号，请重新登录。', 409)
            name = profile.get('name', '')
            if not isinstance(name, str) or len(name) > 80:
                name = ''
            avatar = _safe_wechat_channels_avatar(profile.get('avatar_url'))
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE video_publish_accounts SET profile_name=CASE WHEN ?<>'' THEN ? ELSE profile_name END, avatar_url=CASE WHEN ?<>'' THEN ? ELSE avatar_url END WHERE id=? AND enabled=1",
                                   (name.strip(), name.strip(), avatar, avatar, account_id))
            update_video_publish_account_status(self.database, account_id, 'connected', '登录状态有效。')
        except VideoPublishError as exc:
            if exc.code in {'account_login_failed', 'account_login_timeout', 'account_login_unavailable'}:
                update_video_publish_account_status(
                    self.database, account_id, 'error', '本次检查未完成，无法确认登录状态，请稍后重试。',
                )
            else:
                update_video_publish_account_status(self.database, account_id, 'error', str(exc))
        except (OSError, ValueError, TypeError, AttributeError):
            update_video_publish_account_status(
                self.database, account_id, 'error', '本次检查未完成，无法确认登录状态，请稍后重试。',
            )

    def _check_kuaishou_account(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        try:
            target, _ = self._xiaohongshu_cookie_paths(alias, 'unused_pending')
            target = target.with_name(f'kuaishou_{alias}.json')
            if not target.exists() and not target.is_symlink():
                update_video_publish_account_status(self.database, account_id, 'expired', '登录状态已失效，请重新登录。')
                return
            if target.is_symlink() or not target.is_file() or target.stat().st_size > 5 * 1024 * 1024:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            with self.database.transaction() as connection:
                bound = _bound_video_identity(connection, account_id)
            if not bound:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            profile = self._launch_kuaishou_login(account_id, target, target.with_suffix('.check.png'), check=True)
            if profile.get('identity') != bound:
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            name, avatar = profile.get('name', ''), _safe_kuaishou_avatar(profile.get('avatar_url'))
            if not isinstance(name, str) or len(name) > 80:
                name = ''
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE video_publish_accounts SET profile_name=CASE WHEN ?<>'' THEN ? ELSE profile_name END, avatar_url=CASE WHEN ?<>'' THEN ? ELSE avatar_url END WHERE id=? AND enabled=1",
                                   (name, name, avatar, avatar, account_id))
            update_video_publish_account_status(self.database, account_id, 'connected', '登录状态有效。')
        except VideoPublishError as exc:
            if exc.code in {'account_login_failed', 'account_login_timeout', 'account_login_unavailable'}:
                update_video_publish_account_status(
                    self.database, account_id, 'error', '本次检查未完成，无法确认登录状态，请稍后重试。',
                )
            else:
                update_video_publish_account_status(self.database, account_id, 'error', str(exc))
        except (OSError, ValueError, TypeError, AttributeError):
            update_video_publish_account_status(
                self.database, account_id, 'error', '本次检查未完成，无法确认登录状态，请稍后重试。',
            )

    def _check_bilibili_account(self, account: dict[str, Any]) -> None:
        account_id, alias = str(account['id']), str(account['account_alias'])
        try:
            target, _ = self._xiaohongshu_cookie_paths(alias, 'unused_pending')
            target = target.with_name(f'bilibili_{alias}.json')
            if not target.exists() and not target.is_symlink():
                update_video_publish_account_status(self.database, account_id, 'expired', '登录状态已失效，请重新登录。')
                return
            identity = _read_bilibili_identity(target)
            with self.database.transaction() as connection:
                bound = _bound_video_identity(connection, account_id)
            if not identity or (bound and identity != bound):
                raise VideoPublishError('account_identity_unavailable', '无法确认账号身份，请重新登录。', 409)
            credentials = json.loads(target.read_text(encoding='utf-8'))
            valid, profile = check_bilibili_login(credentials)
            if not valid:
                update_video_publish_account_status(self.database, account_id, 'expired', '登录状态已失效，请重新登录。')
                return
            if profile:
                with self.database.transaction(write=True) as connection:
                    connection.execute('UPDATE video_publish_accounts SET profile_name=?, avatar_url=? WHERE id=? AND enabled=1',
                                       (profile.get('name', ''), profile.get('avatar_url', ''), account_id))
            update_video_publish_account_status(self.database, account_id, 'connected', '登录状态有效。')
        except (VideoPublishError, BilibiliLoginError) as exc:
            update_video_publish_account_status(self.database, account_id, 'error', str(exc))
        except (OSError, ValueError, TypeError):
            update_video_publish_account_status(self.database, account_id, 'error', '暂时无法检查登录状态，请稍后重试。')

    def _run_account_action_locked(self, account_id: str, action: str) -> None:
        if self._stop.is_set():
            return
        account = _get_account(self.database, account_id)
        if account is None:
            return
        key = f"account:{account_id}"
        on_start = lambda process: self._register_process(key, process)
        if action == "login":
            if account['platform_id'] == 'xiaohongshu':
                self._run_xiaohongshu_login(account, on_start)
                return
            if account['platform_id'] == 'bilibili':
                self._run_bilibili_login(account)
                return
            if account['platform_id'] == 'kuaishou':
                self._run_kuaishou_login(account)
                return
            if account['platform_id'] == 'wechat_channels':
                self._run_wechat_channels_login(account)
                return
            if account['platform_id'] in {'douyin', 'zhihu', 'baijiahao'}:
                platform_id = str(account['platform_id'])
                result = self.adapter.login(platform_id, str(account['account_alias']), on_start)
                with self._lock:
                    self._processes.pop(key, None)
                if self._account_login_cancellation_requested(account_id):
                    return
                if result.launch_error:
                    update_video_publish_account_status(self.database, account_id, 'error', result.launch_error)
                elif result.timed_out or result.returncode != 0:
                    update_video_publish_account_status(self.database, account_id, 'not_connected', '登录窗口已关闭或超时，请重试。')
                else:
                    if platform_id == 'zhihu':
                        self._save_zhihu_account_profile(account_id, _parse_zhihu_login_profile(result.output))
                    elif platform_id == 'baijiahao':
                        self._refresh_baijiahao_account_profile(account_id, str(account['account_alias']), key)
                    name = str(_PLATFORM_BY_ID[platform_id]['name'])
                    update_video_publish_account_status(self.database, account_id, 'connected', f'{name}登录状态已保存。')
                return
            update_video_publish_account_status(self.database, account_id, 'error', '暂无法核验该平台账号身份，请稍后重试。')
            return
        if account['platform_id'] == 'bilibili':
            self._check_bilibili_account(account)
            return
        if account['platform_id'] == 'kuaishou':
            self._check_kuaishou_account(account)
            return
        if account['platform_id'] == 'wechat_channels':
            self._check_wechat_channels_account(account)
            return
        result = self.adapter.check(
            str(account["platform_id"]), str(account["account_alias"]),
            lambda process: self._register_process(key, process),
        )
        with self._lock:
            self._processes.pop(key, None)
        if result.launch_error:
            _LOGGER.warning("video account check tool could not start for %s: %s", account_id, _safe_message(result.launch_error))
            update_video_publish_account_status(
                self.database, account_id, "error",
                "账号检查工具暂时无法启动，请检查发布工具设置后重试。",
            )
        elif result.timed_out:
            update_video_publish_account_status(
                self.database, account_id, "error",
                "账号登录检查超时，请稍后点击“重新检查”。这不代表账号已过期。",
            )
        elif _account_check_expired(result):
            detail = _last_diagnostic_line(result.output)
            if detail.strip().casefold() == 'invalid':
                detail = ''
            update_video_publish_account_status(
                self.database, account_id, "expired",
                "登录状态无效或已过期，请重新登录。" + (f" {detail}" if detail else ""),
            )
        elif result.returncode == 0:
            if account["platform_id"] == "zhihu":
                self._save_zhihu_account_profile(account_id, _parse_zhihu_profile_output(result.output))
            elif account["platform_id"] == "baijiahao":
                self._refresh_baijiahao_account_profile(account_id, str(account["account_alias"]), key)
            else:
                try:
                    profile = self.adapter.read_account_profile(
                        str(account["platform_id"]), str(account["account_alias"]),
                        lambda process: self._register_process(key, process),
                    )
                except Exception:
                    _LOGGER.warning("could not read video account profile for %s", account_id)
                    profile = {}
                with self._lock:
                    self._processes.pop(key, None)
                if profile:
                    with self.database.transaction(write=True) as connection:
                        connection.execute("UPDATE video_publish_accounts SET profile_name = ?, avatar_url = ? WHERE id = ? AND enabled = 1",
                                           (profile["name"], profile["avatar_url"], account_id))
            update_video_publish_account_status(self.database, account_id, "connected", "登录状态有效。")
        else:
            detail = _last_diagnostic_line(result.output)
            if detail:
                _LOGGER.warning("video account check failed for %s: %s", account_id, detail)
            update_video_publish_account_status(
                self.database, account_id, "error",
                "暂时无法确认账号登录状态，请稍后点击“重新检查”。这不代表账号已过期。",
            )

    def _save_zhihu_account_profile(self, account_id: str, profile: dict[str, Any]) -> None:
        name = profile.get("name") if isinstance(profile, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            name = ""
        else:
            name = name.strip()
        avatar = _safe_zhihu_avatar(profile.get("avatar_url")) if isinstance(profile, dict) else ""
        if not name and not avatar:
            return
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE video_publish_accounts
                   SET profile_name = CASE WHEN ? <> '' THEN ? ELSE profile_name END,
                       avatar_url = CASE WHEN ? <> '' THEN ? ELSE avatar_url END
                   WHERE id = ? AND enabled = 1""",
                (name, name, avatar, avatar, account_id),
            )

    def _refresh_baijiahao_account_profile(self, account_id: str, account_alias: str, process_key: str) -> None:
        try:
            profile = self.adapter.read_account_profile(
                'baijiahao', account_alias,
                lambda process: self._register_process(process_key, process),
            )
        except Exception:
            _LOGGER.warning('could not read Baijiahao account profile for %s', account_id)
            profile = {}
        finally:
            with self._lock:
                self._processes.pop(process_key, None)
        name = profile.get('name') if isinstance(profile, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            name = ''
        else:
            name = name.strip()
        avatar = _safe_baijiahao_avatar(profile.get('avatar_url')) if isinstance(profile, dict) else ''
        if not name and not avatar:
            return
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE video_publish_accounts
                   SET profile_name = CASE WHEN ? <> '' THEN ? ELSE profile_name END,
                       avatar_url = CASE WHEN ? <> '' THEN ? ELSE avatar_url END
                   WHERE id = ? AND enabled = 1""",
                (name, name, avatar, avatar, account_id),
            )

    def _register_process(self, key: str, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[key] = process
            target_id = key.removeprefix("publish:") if key.startswith("publish:") else ""
            cancel_now = target_id in self._cancel_requests
            account_id = key.removeprefix("account:") if key.startswith("account:") else ""
            account_cancel = self._account_login_cancel_events.get(account_id) if account_id else None
            cancel_account_now = account_cancel.is_set() if account_cancel is not None else False
        if (cancel_now or cancel_account_now) and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _record_publish_heartbeat(self, target_id: str) -> None:
        try:
            with self.database.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE video_publish_targets SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                    (_utc_now(), target_id),
                )
        except Exception:
            _LOGGER.exception("video publish heartbeat could not be stored for %s", target_id)

    def _record_publish_output(self, target_id: str, line: str) -> None:
        clean_line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        failed_upload = "检测到上传失败" in clean_line
        upload_completed = "视频已经传完啦" in clean_line
        xhs_verification = re.fullmatch(r"XHS_VIDEO_VERIFY:(461|471)", clean_line.strip())
        xhs_manual_ready = clean_line.strip() == "XHS_VIDEO_MANUAL_READY"
        now_monotonic = time.monotonic()
        with self._lock:
            should_store = failed_upload or upload_completed or xhs_verification is not None or xhs_manual_ready or (
                now_monotonic - self._last_output_write.get(target_id, 0.0) >= _VIDEO_PUBLISH_HEARTBEAT_SECONDS
            )
            if should_store:
                self._last_output_write[target_id] = now_monotonic
        if not should_store:
            return

        now = _utc_now()
        try:
            with self.database.transaction(write=True) as connection:
                row = connection.execute(
                    "SELECT platform_id, upload_retry_count FROM video_publish_targets WHERE id = ? AND status = 'running'",
                    (target_id,),
                ).fetchone()
                if row is None:
                    return
                is_douyin = str(row["platform_id"]) == "douyin"
                if row["platform_id"] == "xiaohongshu" and (xhs_manual_ready or xhs_verification):
                    if xhs_manual_ready:
                        message = "小红书视频已上传，发布信息已填好。请在原窗口点击发布，并按页面提示完成验证。工作台最多等待 10 分钟。"
                    else:
                        message = (
                            f"小红书触发安全检查（HTTP {xhs_verification[1]}）。"
                            "请在已打开的小红书窗口按页面提示验证并提交。工作台会等待最多 2 分钟，并核对平台回执。"
                        )
                    connection.execute(
                        """UPDATE video_publish_targets SET last_output_at = ?, message = ?, updated_at = ?
                           WHERE id = ? AND status = 'running'""",
                        (now, message, now, target_id),
                    )
                elif failed_upload and is_douyin:
                    retry_count = int(row["upload_retry_count"] or 0) + 1
                    diagnostic_code = "repeated_upload_failures" if retry_count >= _VIDEO_PUBLISH_RETRY_WARNING_COUNT else None
                    message = f"抖音上传页面报告失败，上传器正在重试（累计 {retry_count} 次）。"
                    connection.execute(
                        """UPDATE video_publish_targets
                           SET last_output_at = ?, upload_retry_count = ?, diagnostic_code = ?,
                               message = ?, updated_at = ? WHERE id = ? AND status = 'running'""",
                        (now, retry_count, diagnostic_code, message, now, target_id),
                    )
                elif upload_completed and is_douyin:
                    connection.execute(
                        """UPDATE video_publish_targets
                           SET last_output_at = ?, diagnostic_code = NULL,
                               message = '抖音视频上传完成，正在继续发布设置。', updated_at = ?
                           WHERE id = ? AND status = 'running'""",
                        (now, now, target_id),
                    )
                else:
                    connection.execute(
                        "UPDATE video_publish_targets SET last_output_at = ? WHERE id = ? AND status = 'running'",
                        (now, target_id),
                    )
        except Exception:
            _LOGGER.exception("video publish output signal could not be stored for %s", target_id)

    def _account_lock(self, account_id: str) -> threading.Lock:
        with self._lock:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._account_locks[account_id] = lock
            return lock


def _claim_video_publish_target(database: Database, target_id: str) -> dict[str, Any] | None:
    now = _utc_now()
    with database.transaction(write=True) as connection:
        row = connection.execute(
            """SELECT t.*, b.media_path, b.media_name, b.title, b.description, b.tags_json, b.local_source_id,
                      b.schedule_at, b.id AS batch_id, a.status AS account_status, a.enabled AS account_enabled
               FROM video_publish_targets t JOIN video_publish_batches b ON b.id = t.batch_id
               JOIN video_publish_accounts a ON a.id = t.account_id
               WHERE t.id = ? AND t.status = 'queued'""",
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        if row["account_status"] != "connected" or not bool(row["account_enabled"]):
            now = _utc_now()
            message = "该账号登录状态已失效或已隐藏，请重新检查账号后再发布。"
            connection.execute(
                "UPDATE video_publish_targets SET status = 'failed', message = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (message, now, now, target_id),
            )
            _refresh_batch_status(connection, str(row["batch_id"]))
            return None
        attempt_number = int(row["attempt_count"]) + 1
        xhs_manual_submit = row["platform_id"] == "xiaohongshu" and row["diagnostic_code"] == _XHS_MANUAL_SUBMIT_CODE
        connection.execute(
            """UPDATE video_publish_targets SET status = 'running', progress = 0,
               attempt_count = ?, message = '正在上传视频…', remote_id = NULL, remote_url = NULL,
               heartbeat_at = NULL, last_output_at = NULL, upload_retry_count = 0, diagnostic_code = ?,
               started_at = ?, finished_at = NULL, updated_at = ?
               WHERE id = ? AND status = 'queued'""",
            (attempt_number, _XHS_MANUAL_SUBMIT_CODE if xhs_manual_submit else None, now, now, target_id),
        )
        connection.execute(
            """INSERT INTO video_publish_attempts (id, target_id, attempt_number, status, started_at)
               VALUES (?, ?, ?, 'running', ?)""",
            (uuid.uuid4().hex, target_id, attempt_number, now),
        )
        _refresh_batch_status(connection, str(row["batch_id"]))
        options = _decode_object(row["options_json"])
        managed_cover_paths: dict[str, Path] = {}
        for key in _BATCH_COVER_KEYS:
            filename = str(options.get(key) or "")
            if IMAGE_FILENAME.fullmatch(filename):
                try:
                    managed_cover_paths[filename] = _managed_batch_cover(connection, filename)
                except (OSError, ValueError, RuntimeError):
                    _LOGGER.warning("任务 %s 的封面 %s 暂不可用", target_id, filename)
        return {
            "id": target_id,
            "account_id": str(row["account_id"]),
            "platform_id": str(row["platform_id"]),
            "xhs_manual_submit": xhs_manual_submit,
            "account_alias": str(row["account_alias"]),
            "media_path": str(row["media_path"]),
            "local_source_id": row["local_source_id"],
            "title": str(row["title"]),
            "description": str(row["description"]),
            "tags": _decode_list(row["tags_json"]),
            "schedule_at": options.get("schedule_at_override", row["schedule_at"]),
            "options": options,
            "managed_cover_paths": managed_cover_paths,
        }


def _finish_video_publish_target(
    database: Database,
    target_id: str,
    status: str,
    message: str,
    exit_code: int | None,
    *,
    remote_id: str | None = None,
    remote_url: str | None = None,
) -> None:
    allowed = {"pending_review", "published", "failed", "unknown", "cancelled"}
    if status not in allowed:
        raise ValueError("视频发布状态无效。")
    now = _utc_now()
    safe_message = _safe_message(message)
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT batch_id FROM video_publish_targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            return
        connection.execute(
            """UPDATE video_publish_targets SET status = ?, progress = ?, message = ?, remote_id = ?, remote_url = ?, finished_at = ?, updated_at = ?
               WHERE id = ?""",
            (status, 100 if status in {"pending_review", "published"} else None, safe_message, remote_id, remote_url, now, now, target_id),
        )
        connection.execute(
            """UPDATE video_publish_attempts SET status = ?, exit_code = ?, message = ?, finished_at = ?
               WHERE target_id = ? AND attempt_number = (
                   SELECT MAX(attempt_number) FROM video_publish_attempts WHERE target_id = ?
               )""",
            (status, exit_code, safe_message, now, target_id, target_id),
        )
        _refresh_batch_status(connection, str(row["batch_id"]))


def _refresh_batch_status(connection: sqlite3.Connection, batch_id: str) -> None:
    rows = connection.execute("SELECT status FROM video_publish_targets WHERE batch_id = ?", (batch_id,)).fetchall()
    statuses = [str(row["status"]) for row in rows]
    if not statuses:
        return
    if "running" in statuses:
        status = "running"
    elif "queued" in statuses:
        status = "queued"
    elif all(value == "published" for value in statuses):
        status = "published"
    elif all(value == "cancelled" for value in statuses):
        status = "cancelled"
    elif "unknown" in statuses and all(value in {"unknown", "published"} for value in statuses):
        status = "unknown" if all(value == "unknown" for value in statuses) else "partial"
    elif "pending_review" in statuses and all(value in {"pending_review", "published"} for value in statuses):
        status = "pending_review" if all(value == "pending_review" for value in statuses) else "partial"
    elif "published" in statuses or "pending_review" in statuses or "unknown" in statuses:
        status = "partial"
    elif all(value == "cancelled" for value in statuses):
        status = "cancelled"
    else:
        status = "failed"
    connection.execute(
        "UPDATE video_publish_batches SET status = ?, updated_at = ? WHERE id = ?",
        (status, _utc_now(), batch_id),
    )


def _resolve_asset_video(database: Database, archive_root: Path, asset_id: str) -> tuple[Path, str, str]:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT media.path, media.sha256 FROM tasks AS t
               JOIN artifacts AS media ON media.task_id = t.id
               WHERE t.asset_id = ? AND media.kind IN ('media', 'video', 'legacy_video')
               ORDER BY media.created_at DESC, media.version DESC, media.id DESC LIMIT 1""",
            (asset_id,),
        ).fetchone()
    if row is None:
        raise VideoPublishError("asset_media_missing", "该素材没有可用的视频文件。", 404)
    try:
        path = Path(str(row["path"]))
        if path.is_symlink() or path.suffix.casefold() not in _VIDEO_SUFFIXES:
            raise ValueError("unsupported media")
        safe_path = _assert_contained(path, archive_root, strict=True)
        if not safe_path.is_file():
            raise ValueError("not a file")
        filename, _ = clean_upload_filename(safe_path.name)
        return safe_path, filename, str(row["sha256"])
    except (OSError, RuntimeError, ValueError) as exc:
        raise VideoPublishError("asset_media_invalid", "素材文件不存在、格式不支持或不在归档目录内。", 409) from exc


def _copy_video_to_managed_storage(source: Path, batch_id: str, filename: str, expected_sha: str) -> Path:
    safe_name, extension = clean_upload_filename(filename)
    if extension not in _VIDEO_SUFFIXES:
        raise VideoPublishError("video_required", "所选文件不是支持的视频格式。")
    root = _prepare_publish_media_root()
    folder = root / batch_id
    if folder.exists() or folder.is_symlink():
        raise VideoPublishError("media_storage_conflict", "发布素材目录冲突，请重新提交。", 409)
    folder.mkdir()
    target = folder / safe_name
    temporary = folder / f".{safe_name}.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total = 0
    try:
        if source.is_symlink():
            raise ValueError("source is symlink")
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            before = os.fstat(input_file.fileno())
            while chunk := input_file.read(1024 * 1024):
                total += len(chunk)
                digest.update(chunk)
                output_file.write(chunk)
            after = os.fstat(input_file.fileno())
            output_file.flush()
            os.fsync(output_file.fileno())
        if (
            total <= 0
            or before.st_size != total or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or digest.hexdigest() != expected_sha
        ):
            raise ValueError("video changed while copying")
        os.replace(temporary, target)
        return target.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _remove_owned_video_directory(batch_id)
        raise VideoPublishError("media_copy_failed", "保存发布视频时校验失败，请重新选择素材。", 409) from exc


def _attach_batch_cover(cover: VideoPublishCoverUpload, batch_id: str, upload_id: str) -> str:
    if getattr(cover, "managed", False):
        return cover.path.name
    return _copy_video_publish_cover_to_batch(cover, batch_id, upload_id)


def _copy_video_publish_cover_to_batch(cover: VideoPublishCoverUpload, batch_id: str, upload_id: str) -> str:
    if not _PUBLISH_COVER_ID.fullmatch(upload_id):
        raise VideoPublishError("cover_upload_invalid", "封面上传记录无效，请重新选择图片。")
    source_root = _prepare_publish_cover_upload_root()
    try:
        source = _assert_contained(cover.path, source_root, strict=True)
        if source.is_symlink() or not source.is_file() or _sha256_file(source) != cover.sha256:
            raise ValueError("cover file changed before it was copied")
        _validate_cover_image(source, source.suffix.casefold())
        root = _prepare_publish_media_root()
        folder = root / batch_id
        _assert_contained(folder, root, strict=True)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("publish media directory is unavailable")
        filename = f"platform-cover-{upload_id}{source.suffix.casefold()}"
        destination = folder / filename
        temporary = folder / f".{filename}.{uuid.uuid4().hex}.tmp"
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            digest = hashlib.sha256()
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if digest.hexdigest() != cover.sha256:
            raise ValueError("cover file changed while copying")
        os.replace(temporary, destination)
        return filename
    except (OSError, RuntimeError, ValueError) as exc:
        raise VideoPublishError("cover_copy_failed", "保存视频封面失败，请重新上传图片。", 409) from exc


def _remove_video_publish_batch_cover(batch_id: str, filename: str) -> None:
    try:
        root = _prepare_publish_media_root()
        folder = _assert_contained(root / batch_id, root, strict=True)
        if folder.is_symlink() or not folder.is_dir():
            return
        target = _assert_contained(folder / filename, folder, strict=False)
        if target.is_symlink():
            return
        target.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError):
        return


def _generate_douyin_first_frame_covers(database: Database, media_path: Path) -> dict[str, str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise VideoPublishError(
            "cover_ffmpeg_missing",
            "未上传自定义封面，自动生成首帧封面需要 FFmpeg。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH，或先上传自定义封面。",
            409,
        )
    results: dict[str, str] = {}
    for orientation, width, height in (("portrait", 1080, 1440), ("landscape", 1440, 1080)):
        filename = f"douyin-cover-{orientation}.jpg"
        try:
            result = subprocess.run(
                [
                    executable, "-nostdin", "-hide_banner", "-v", "error",
                    "-protocol_whitelist", "file,pipe", "-i", str(media_path),
                    "-map", "0:v:0", "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                    "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoPublishError("cover_frame_failed", "无法从视频提取抖音首帧封面，请上传自定义封面后重试。", 409) from exc
        if result.returncode != 0 or not result.stdout or len(result.stdout) > _MAX_COVER_BYTES:
            raise VideoPublishError("cover_frame_failed", "无法生成抖音首帧封面，请上传自定义封面后重试。", 409)
        try:
            results[orientation] = _store_video_cover(database, filename, ".jpg", result.stdout).path.name
        except (OSError, ValueError, RuntimeError) as exc:
            raise VideoPublishError("cover_storage_invalid", "无法写入视频封面目录，请检查目录设置。", 500) from exc
    return results


def _prepare_publish_media_root() -> Path:
    project = PROJECT_ROOT.resolve(strict=True)
    try:
        relative = _PUBLISH_MEDIA_ROOT.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("media_storage_invalid", "发布素材保存目录无效。", 500) from exc
    current = project
    for part in relative.parts:
        current = current / part
        if _has_reparse_point(current):
            raise VideoPublishError("media_storage_invalid", "发布素材目录不能包含符号链接或目录联接。", 500)
        current.mkdir(exist_ok=True)
        if _has_reparse_point(current) or not current.is_dir():
            raise VideoPublishError("media_storage_invalid", "发布素材目录不可用。", 500)
    resolved = _PUBLISH_MEDIA_ROOT.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise VideoPublishError("media_storage_invalid", "发布素材目录超出工作台数据目录。", 500) from exc
    return resolved


def _remove_owned_video_directory(batch_id: str) -> int | None:
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        return None
    try:
        root = _prepare_publish_media_root()
        folder = root / batch_id
        try:
            folder_stat = folder.lstat()
        except FileNotFoundError:
            return 0
        if _has_reparse_point(folder):
            return None
        if not stat.S_ISDIR(folder_stat.st_mode):
            return None
        resolved_folder = folder.resolve(strict=True)
        if os.path.normcase(str(resolved_folder)) != os.path.normcase(str(folder)):
            return None
        _assert_contained(folder, root, strict=True)
        items = list(folder.iterdir())
        for item in items:
            if _has_reparse_point(item) or not item.is_file():
                return None
        bytes_freed = sum(item.stat().st_size for item in items)
        for item in items:
            item.unlink()
        folder.rmdir()
        return bytes_freed
    except (OSError, RuntimeError, ValueError, VideoPublishError):
        return None


def _has_reparse_point(path: Path) -> bool:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(result, "st_file_attributes", 0)
    return stat.S_ISLNK(result.st_mode) or bool(file_attributes & reparse_flag)


def _get_account(database: Database, account_id: str) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM video_publish_accounts WHERE id = ? AND enabled = 1", (account_id,)
        ).fetchone()
    return _account_payload(row) if row is not None else None


def _account_payload(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["status"])
    message = _safe_message(str(row["message"] or ""))
    if status == "error" and re.search(
        r"(?:Traceback \(most recent call last\)|\b(?:NameError|ImportError|ModuleNotFoundError|AttributeError|SyntaxError|UnboundLocalError):)",
        message,
        re.IGNORECASE,
    ):
        message = "账号检查程序遇到内部错误，暂时无法核验登录状态。请重启工作台后点击“重新检查”；此提示不代表账号已过期。"
    return {
        "id": str(row["id"]),
        "platform_id": str(row["platform_id"]),
        "account_alias": str(row["account_alias"]),
        "display_name": str(row["display_name"] or ""),
        "profile_name": str(row["profile_name"] or ""),
        "avatar_url": str(row["avatar_url"] or ""),
        "status": status,
        "message": message,
        "checked_at": row["checked_at"],
        "enabled": bool(row["enabled"]),
    }


def _batch_payload(row: sqlite3.Row) -> dict[str, Any]:
    media_path = Path(str(row["media_path"]))
    return {
        "id": str(row["id"]),
        "asset_id": row["asset_id"],
        "media_name": str(row["media_name"]),
        "media_size": int(row["media_size"]),
        "media_available": not bool(row["media_cleaned"]) and media_path.is_file() and not media_path.is_symlink(),
        "media_source_type": "local" if row["local_source_id"] else "managed",
        "media_sha256": str(row["media_sha256"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "tags": _decode_list(row["tags_json"]),
        "schedule_at": row["schedule_at"],
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _target_payload(row: sqlite3.Row) -> dict[str, Any]:
    options = _decode_object(row["options_json"])
    status = str(row["status"])
    if status == "published" and row["platform_id"] == "wechat_channels" and options.get("save_as_draft"):
        status = "draft_saved"
    return {
        "id": str(row["id"]),
        "batch_id": str(row["batch_id"]),
        "account_id": str(row["account_id"]),
        "platform_id": str(row["platform_id"]),
        "account_alias": str(row["account_alias"]),
        "account_name": str(row["account_name"] or ""),
        "options": options,
        "status": status,
        "progress": row["progress"],
        "message": _safe_message(str(row["message"] or "")),
        "remote_id": row["remote_id"],
        "remote_url": row["remote_url"],
        "platform_status": row["platform_status"],
        "platform_message": _safe_message(str(row["platform_message"] or "")),
        "platform_checked_at": row["platform_checked_at"],
        "platform_observed_at": row["platform_observed_at"],
        "heartbeat_at": row["heartbeat_at"],
        "last_output_at": row["last_output_at"],
        "upload_retry_count": int(row["upload_retry_count"] or 0),
        "diagnosis": _video_publish_target_diagnosis(row),
        "attempt_count": int(row["attempt_count"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _video_publish_target_diagnosis(row: sqlite3.Row) -> dict[str, str] | None:
    if str(row["status"]) != "running":
        return None
    now = datetime.now(timezone.utc)
    started_at = _parse_video_publish_timestamp(row["started_at"])
    heartbeat_at = _parse_video_publish_timestamp(row["heartbeat_at"])
    last_output_at = _parse_video_publish_timestamp(row["last_output_at"])
    retry_count = int(row["upload_retry_count"] or 0)

    if started_at and now - started_at >= timedelta(seconds=_VIDEO_PUBLISH_HEARTBEAT_STALE_SECONDS):
        if heartbeat_at is None or now - heartbeat_at >= timedelta(seconds=_VIDEO_PUBLISH_HEARTBEAT_STALE_SECONDS):
            return {
                "code": "heartbeat_missing",
                "severity": "danger",
                "message": "45 秒内没有收到上传进程心跳；任务可能失去响应。请先核查平台后台，避免直接重发。",
            }
    if row["diagnostic_code"] == "repeated_upload_failures" and retry_count >= _VIDEO_PUBLISH_RETRY_WARNING_COUNT:
        return {
            "code": "repeated_upload_failures",
            "severity": "warning",
            "message": f"平台已检测到 {retry_count} 次上传失败并触发重试。请检查平台上传页面与网络状态；工作台不会自动停止任务或重复创建投稿。",
        }

    activity_at = last_output_at or started_at
    if activity_at and now - activity_at >= timedelta(seconds=_VIDEO_PUBLISH_STALL_SECONDS):
        return {
            "code": "no_recent_output",
            "severity": "warning",
            "message": "上传进程仍在运行，但 5 分钟没有新的上传器输出，可能正在等待平台或疑似卡住。请先核查平台后台；此提示不会自动停止或重发。",
        }
    return None


def _target_summary(rows: list[sqlite3.Row]) -> dict[str, int]:
    result = {key: 0 for key in ("total", "queued", "running", "pending_review", "published", "draft_saved", "failed", "unknown", "cancelled")}
    result["total"] = len(rows)
    for row in rows:
        status = str(row["status"])
        options = _decode_object(row["options_json"])
        if status == "published" and row["platform_id"] == "wechat_channels" and options.get("save_as_draft"):
            status = "draft_saved"
        if status in result:
            result[status] += 1
    return result


def _sau_result(returncode: int | None, output: str) -> SauResult:
    marker = re.search(r"(?m)^SAU_RESULT_JSON:(\{[^\r\n]*\})\s*$", output)
    if not marker:
        return SauResult(returncode, output)
    try:
        payload = json.loads(marker.group(1))
    except (json.JSONDecodeError, TypeError):
        return SauResult(returncode, output)
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"published", "pending_review", "failed"}:
        status = None
    remote_id = payload.get("remote_id") if isinstance(payload, dict) else None
    if not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", remote_id):
        remote_id = None
    remote_url = payload.get("remote_url") if isinstance(payload, dict) else None
    remote_match = re.fullmatch(r"https://www\.douyin\.com/video/([A-Za-z0-9_-]{1,128})/?(?:\?[^\s#]*)?", remote_url) if isinstance(remote_url, str) else None
    if remote_match:
        remote_url = f"https://www.douyin.com/video/{remote_match.group(1)}"
    else:
        remote_url = None
    return SauResult(returncode, output, platform_status=status, remote_id=remote_id, remote_url=remote_url)


def _parse_zhihu_article_result(output: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for start in range(len(output) - 1, -1, -1):
        if output[start] != '{':
            continue
        try:
            value, end = decoder.raw_decode(output[start:])
        except (json.JSONDecodeError, TypeError):
            continue
        if not output[start + end:].strip() and isinstance(value, dict) and value.get('type') == 'article':
            return value
    return None


def _parse_zhihu_profile_value(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    name = value.get("name")
    avatar = value.get("avatar_url")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return {}
    if not isinstance(avatar, str) or len(avatar) > 2048:
        avatar = ""
    return {"name": name.strip(), "avatar_url": avatar}


def _parse_zhihu_login_profile(output: str) -> dict[str, str]:
    """从 SAU 登录命令尾部 JSON 中取资料，不解析登录提示文案。"""
    decoder = json.JSONDecoder()
    for start in range(len(output) - 1, -1, -1):
        if output[start] != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[start:])
        except (json.JSONDecodeError, TypeError):
            continue
        if output[start + end:].strip():
            continue
        if isinstance(value, dict) and value.get("success") is True:
            return _parse_zhihu_profile_value(value.get("profile"))
    return {}


def _parse_zhihu_profile_output(output: str) -> dict[str, str]:
    """只读取 SAU 知乎检查输出中的独立资料标记行。"""
    for line in reversed(output.splitlines()):
        if not line.startswith("ZHIHU_PROFILE_JSON:"):
            continue
        try:
            value = json.loads(line.removeprefix("ZHIHU_PROFILE_JSON:"))
        except (json.JSONDecodeError, TypeError, RecursionError):
            return {}
        return _parse_zhihu_profile_value(value)
    return {}


def _safe_zhihu_avatar(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.lower() != "https" or parsed.username or parsed.password
            or port not in (None, 443)
            or not (host == "zhimg.com" or host.endswith(".zhimg.com"))):
        return ""
    return value


def _safe_baijiahao_avatar(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ''
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError:
        return ''
    if (parsed.scheme.lower() != 'https' or parsed.username or parsed.password
            or port not in (None, 443) or host != 'pic.rmb.bdstatic.com'):
        return ''
    return value


_XHS_STAGE_MESSAGES = {
    "设置自主拍摄声明": "小红书的自主拍摄声明需要拍摄地点和拍摄日期，当前工作台暂不支持填写。作品未提交；请点击重新编辑，选择符合实际的其他声明或不填写声明。",
    "设置内容声明": "小红书内容类型声明设置失败，作品未提交。请点击重新编辑，检查或调整内容类型声明后再试。",
    "设置原创声明": "小红书原创声明设置失败，作品未提交。请点击重新编辑，检查原创声明设置后再试。",
    "设置定时发布时间": "小红书定时发布时间设置失败，作品未提交。请点击重新编辑，检查发布时间或改为立即发布后再试。",
    "核对定时发布时间": "无法确认小红书实际定时时间，作品未提交。请点击重新编辑，检查发布时间后再试。",
    "填写标题、正文和话题": "小红书标题、正文或话题设置失败，作品未提交。请点击重新编辑，检查标题、正文和标签后再试。",
    "设置封面": "小红书封面设置失败，作品未提交。请点击重新编辑，检查封面后再试。",
    "检查封面弹窗": "小红书封面弹窗未关闭，无法继续发布，作品未提交。请点击重新编辑，检查封面后再试。",
    "设置可见范围": "小红书可见范围设置失败，作品未提交。请点击重新编辑，检查可见范围后再试。",
    "设置地点": "小红书地点设置失败，作品未提交。请点击重新编辑，重新选择地点后再试。",
}


def _xhs_presubmit_diagnostic(platform: str, output: str) -> str | None:
    if platform != 'xiaohongshu':
        return None
    for line in reversed(output.splitlines()):
        if line.startswith('XHS_PRE_SUBMIT_ERROR:'):
            data = _decode_object(line.removeprefix('XHS_PRE_SUBMIT_ERROR:'))
            stage = data.get('stage')
            if stage == '填写标题、正文和话题' and isinstance(data.get('topic'), str) and data['topic'].strip():
                topic = _safe_message(data['topic'])[:50]
                return f'小红书话题“{topic}”未能确认添加，作品未提交。请点击重新编辑，检查该标签后再试。'
            return _XHS_STAGE_MESSAGES.get(stage) if isinstance(stage, str) else None
    return None


def _xhs_submit_security_message(platform: str, message: str) -> str | None:
    if platform != "xiaohongshu":
        return None
    status = re.search(r"小红书提交接口返回 HTTP (461|471)(?=，|$)", message)
    if status is None:
        return None
    return (
        f"小红书提交接口返回 HTTP {status[1]}，本次提交被平台拦截。"
        "请打开小红书创作服务平台，按页面提示处理验证，再手动提交。"
    )


def _video_publish_failure(platform: str, output: str) -> tuple[str, str]:
    for line in reversed(output.splitlines()):
        if not line.startswith("SAU_VIDEO_ERROR:"):
            continue
        payload = _decode_object(line.removeprefix("SAU_VIDEO_ERROR:"))
        stage, detail, attempted = (payload.get(key) for key in ("stage", "detail", "submission_attempted"))
        if payload.get("platform") != platform or type(attempted) is not bool:
            continue
        if not isinstance(stage, str) or not stage.strip() or not isinstance(detail, str) or not detail.strip():
            continue
        message = f"{_safe_message(stage)[:60]}：{_safe_message(detail)[:350]}"
        if attempted:
            if stage == "核对小红书提交回执" and (security_message := _xhs_submit_security_message(platform, detail)):
                return "unknown", f"{security_message}提交前请先核对作品和定时列表。"
            return "unknown", f"{message}。请先在平台后台核对作品，再决定是否重试。"
        return "failed", f"{message}。作品未提交，可重新编辑后重试。"

    diagnostic = _douyin_presubmit_diagnostic(output) if platform == "douyin" else _xhs_presubmit_diagnostic(platform, output)
    if diagnostic:
        return "failed", diagnostic
    message = "发布未能正常完成，平台是否接收作品未知；请先检查平台后台。"
    detail = _last_diagnostic_line(output)
    return "unknown", f"{message} {detail}" if detail else message


def _last_diagnostic_line(value: str) -> str:
    lines = [
        _safe_message(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line))
        for line in value.splitlines()
    ]
    lines = [line for line in lines if line and not line.lstrip().startswith(("╰╴at ", "├╴at "))]
    diagnostics = [
        line for line in reversed(lines)
        if re.search(r"\b(error|failed|exception|panic|timeout)\b|失败|错误|拒绝|超时|失效|未确认|未能|已停止提交", line, re.IGNORECASE)
    ]
    for line in diagnostics:
        if not re.search(r"(?:^|\|\s*)WARNING\s*:", line):
            return line
    if diagnostics:
        return diagnostics[0]
    for line in reversed(lines):
        if line and not re.search(r"(?:^|\s)at\s+[^\s]+\.rs:\d+:\d+\s*$", line):
            return line
    return ""


def _account_check_expired(result: SauResult) -> bool:
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.output)
    lowered = output.casefold()
    if any(line.strip().casefold() == "invalid" for line in output.splitlines()):
        return True
    if any(marker in lowered for marker in (
        "cookie is missing or expired", "cookie文件已失效", "cookie 已失效",
        "登录态失效", "登录状态已失效", "登录状态无效", "账号未登录",
    )):
        return True
    return classify_site_issue(output) == SiteIssueCode.LOGIN_REQUIRED


def _wechat_channels_cookie_expired(value: str) -> bool:
    lowered = value.lower()
    return "cookie文件已失效" in lowered or "cookie 已失效" in lowered or "cookie is expired" in lowered


def _douyin_presubmit_diagnostic(value: str) -> str | None:
    for line in reversed(value.splitlines()):
        clean_line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        for marker, ending in (
            ("抖音连续 ", "已停止重传"),
            ("抖音重新选择视频重试失败，已停止上传：", ""),
            ("DouyinUploadAbortError:", ""),
        ):
            start = clean_line.find(marker)
            if start < 0:
                continue
            detail = clean_line[start:].strip()
            if ending and ending not in detail:
                continue
            detail = _safe_message(detail)
            if detail:
                return f"{detail}；上传在发布提交前中止，可修复问题后安全重试。"
    return None


def _safe_message(value: object) -> str:
    text = str(value).replace("\x00", " ").strip()
    text = _URL_PATTERN.sub("<链接已隐藏>", text)
    text = _SECRET_PATTERN.sub(r"\1=<已隐藏>", text)
    return text[:500]


def _decode_object(value: object) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except (TypeError, json.JSONDecodeError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def _decode_list(value: object) -> list[str]:
    try:
        result = json.loads(str(value))
    except (TypeError, json.JSONDecodeError, RecursionError):
        return []
    return [str(item) for item in result if isinstance(item, str)] if isinstance(result, list) else []


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
