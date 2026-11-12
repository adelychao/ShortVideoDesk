"""Thin adapter around the existing anonymous platform download pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import parse_qs, urlsplit
import uuid

from multi_sources import PLATFORMS, DownloadIssue, parse_source, resolve_source
from .database import Database


ProgressCallback = Callable[[str, float | None, str], None]


@dataclass(frozen=True)
class MediaInspection:
    duration_seconds: float
    width: int | None
    height: int | None
    sha256: str
    full_decode_verified: bool
    has_audio: bool


@dataclass(frozen=True)
class DownloadResult:
    metadata: dict[str, object]
    output_dir: Path
    files: dict[str, Path]
    inspection: MediaInspection


@dataclass(frozen=True)
class PlatformStatus:
    id: str
    name: str
    validation_status: str
    checked_at: datetime | None
    summary: str | None


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _inspection_from_metadata(metadata: dict[str, object], video: Path) -> MediaInspection:
    """Use the existing downloader's full-decode verifier result without decoding twice."""
    if not video.is_file():
        raise DownloadIssue("下载器返回的视频文件不存在", "media_validation_failed")
    digest = metadata.get("sha256")
    duration = metadata.get("duration_seconds")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise DownloadIssue("视频缺少可验证的 SHA-256", "media_validation_failed")
    if metadata.get("full_decode_verified") is not True:
        raise DownloadIssue("视频未通过完整解码校验", "media_validation_failed")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError) as exc:
        raise DownloadIssue("下载结果缺少有效时长", "media_validation_failed") from exc
    if duration_seconds <= 0:
        raise DownloadIssue("下载结果时长无效", "media_validation_failed")
    return MediaInspection(
        duration_seconds=duration_seconds,
        width=metadata.get("width") if isinstance(metadata.get("width"), int) else None,
        height=metadata.get("height") if isinstance(metadata.get("height"), int) else None,
        sha256=digest,
        full_decode_verified=True,
        has_audio=bool(metadata.get("audio_codec")),
    )


def inspect_video(path: Path, expected_duration: float, *, require_audio: bool = False) -> MediaInspection:
    """Expose the shared ffprobe/FFmpeg verifier using the workbench result type."""
    # Lazy import avoids a module cycle when anonymous_poc mounts the workbench API.
    from anonymous_poc import inspect_video as inspect_existing_video

    metadata = inspect_existing_video(path, expected_duration, require_audio=require_audio)
    return _inspection_from_metadata(metadata, path)


def download_source(
    source_text: str,
    archive_root: Path,
    update: ProgressCallback,
    *,
    allow_first_frame: bool,
    task_id: str | None = None,
    access_mode: str = "anonymous",
    account_id: str | None = None,
    database: Database | None = None,
) -> DownloadResult:
    """Download one supported public work into an isolated temporary directory."""
    initial_source = parse_source(source_text)
    browser_cookies: list[dict[str, object]] = []
    account_manager = None
    if access_mode == "account":
        from .platform_accounts import PlatformAccountError, get_platform_account_manager

        account_manager = get_platform_account_manager()
        try:
            if account_id:
                browser_cookies = account_manager.require_authenticated_cookies(
                    initial_source.platform, account_id=account_id, database=database
                )
            else:
                browser_cookies = account_manager.require_authenticated_cookies(initial_source.platform)
        except PlatformAccountError as exc:
            raise DownloadIssue(str(exc), "platform_account_required") from exc
    elif access_mode != "anonymous":
        raise ValueError("平台访问方式无效")
    source = resolve_source(initial_source, cookies=browser_cookies or None)
    if source.platform != initial_source.platform:
        raise DownloadIssue("短链接跳转到了其他平台，已停止访问", "untrusted_redirect")

    task_key = task_id or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", task_key):
        raise ValueError("任务 ID 格式无效")

    root = Path(archive_root).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    staging_root = root / ".workbench-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_root = staging_root.resolve(strict=True)
    if not _inside(staging_root, root):
        raise ValueError("临时下载目录必须位于归档目录内")

    def legacy_update(**changes: object) -> None:
        percent = changes.get("percent")
        progress = float(percent) if isinstance(percent, (int, float)) else None
        message = changes.get("message") or changes.get("phase") or "下载处理中"
        update("download", progress, str(message))

    from anonymous_poc import inspect_video as existing_verifier
    from multi_download import download_multiplatform

    def execute_download(cookie_file: Path | None) -> dict[str, object]:
        # The old Douyin entry point used a module-level DOWNLOADS path. Bind it to
        # this task's staging root while preserving its existing legacy call shape.
        def download_douyin(value: str, callback, *, archive_id=None, browser_cookies=None):
            from anonymous_poc import download_video

            return download_video(
                value,
                callback,
                archive_id=archive_id,
                output_root=staging_root,
                cookie_file=cookie_file,
                browser_cookies=browser_cookies,
            )

        return download_multiplatform(
            source.url,
            legacy_update,
            output_root=staging_root,
            douyin_download=download_douyin,
            verifier=existing_verifier,
            archive_id=task_key,
            allow_first_frame=allow_first_frame,
            cookie_file=cookie_file,
            browser_cookies=browser_cookies or None,
        )

    if account_manager is not None:
        from .platform_accounts import PlatformAccountError

        try:
            with account_manager.temporary_cookie_file(source.platform, browser_cookies) as cookie_file:
                result = execute_download(cookie_file)
        except PlatformAccountError as exc:
            raise DownloadIssue(str(exc), "platform_account_required") from exc
    else:
        result = execute_download(None)

    metadata = dict(result.get("metadata") or {})
    resolved_id = source.media_id
    returned_id = str(metadata.get("id") or "")
    if resolved_id and returned_id != str(resolved_id):
        raise DownloadIssue("下载结果的作品 ID 与原链接不一致", "identity_mismatch")
    parser_id = metadata.get("media_id")
    if source.platform in {"douyin", "bilibili"}:
        if not resolved_id:
            raise DownloadIssue("链接未解析出可验证的作品 ID", "identity_mismatch")
        if not isinstance(parser_id, str) or not parser_id:
            raise DownloadIssue("下载结果缺少媒体解析器作品 ID", "identity_mismatch")
        checked_parser_id = parser_id
        if source.platform == "bilibili":
            part_match = re.search(r"_p([0-9]+)$", parser_id)
            query_part = parse_qs(urlsplit(source.url).query).get("p", ["1"])
            requested_part = int(query_part[0]) if query_part and query_part[0].isdigit() else 1
            if part_match and int(part_match.group(1)) != requested_part:
                raise DownloadIssue("下载结果的 B 站分 P 与原链接不一致", "identity_mismatch")
            if not part_match and requested_part != 1:
                raise DownloadIssue("无法验证下载结果对应 B 站请求的分 P", "identity_mismatch")
            checked_parser_id = re.sub(r"_p[0-9]+$", "", parser_id)
        if checked_parser_id != str(resolved_id):
            raise DownloadIssue("媒体解析器返回的作品 ID 与原链接不一致", "identity_mismatch")

    output_dir = Path(str(result.get("output_dir") or "")).resolve(strict=False)
    if not _inside(output_dir, staging_root):
        raise DownloadIssue("下载器返回了临时目录之外的路径", "media_validation_failed")
    raw_files = result.get("files")
    if not isinstance(raw_files, dict):
        raise DownloadIssue("下载器未返回产物清单", "media_validation_failed")
    files: dict[str, Path] = {}
    for kind, value in raw_files.items():
        path = Path(str(value)).resolve(strict=False)
        if not _inside(path, output_dir):
            raise DownloadIssue("下载器返回了作品目录之外的文件", "media_validation_failed")
        if path.is_file():
            files[str(kind)] = path
    video = files.get("video")
    if video is None:
        raise DownloadIssue("下载器未返回可用视频文件", "media_validation_failed")

    if source.platform == "douyin" and allow_first_frame and "cover" not in files:
        from multi_download import cover_artifact

        cover_path, cover_source, cover_warnings = cover_artifact(output_dir, video, allow_first_frame=True)
        if cover_path is not None:
            cover_path = cover_path.resolve(strict=False)
            if not _inside(cover_path, output_dir) or not cover_path.is_file():
                raise DownloadIssue("首帧预览未生成在作品临时目录内", "media_validation_failed")
            files["cover"] = cover_path
        metadata["cover_source"] = cover_source
        existing_warnings = metadata.get("warnings")
        metadata["warnings"] = (existing_warnings if isinstance(existing_warnings, list) else []) + cover_warnings

    inspection = _inspection_from_metadata(metadata, video)
    # Source URLs are transient input. Do not persist even their redacted form
    # in temporary metadata sidecars.
    for source_key in ("source_url", "original_share_url", "raw_url", "webpage_url"):
        metadata.pop(source_key, None)
    cover_source = metadata.get("cover_source")
    metadata["cover_source"] = cover_source if isinstance(cover_source, str) and cover_source in {
        "original", "video_frame", "missing"
    } else ("original" if "cover" in files else "missing")
    metadata["task_id"] = task_key
    metadata["platform_account_session_used"] = access_mode == "account"

    # Existing downloaders calculate this during their full media inspection.
    # This inexpensive identity check catches an unexpected file mutation after it.
    with video.open("rb") as stream:
        current_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if current_digest != inspection.sha256:
        raise DownloadIssue("视频在校验后发生变化，未登记为成功", "media_validation_failed")

    metadata_file = files.get("metadata")
    if metadata_file is not None:
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return DownloadResult(metadata, output_dir, files, inspection)


def get_platform_status(validation_path: Path | None = None) -> list[PlatformStatus]:
    """Read the existing platform validation summary without exposing raw evidence."""
    path = validation_path or Path(__file__).resolve().parents[2] / "data" / "platform-validation.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    statuses: list[PlatformStatus] = []
    for platform_id, name in PLATFORMS.items():
        entry = payload.get(platform_id)
        entry = entry if isinstance(entry, dict) else {}
        checked_at: datetime | None = None
        raw_time = entry.get("tested_at") or entry.get("checked_at")
        if isinstance(raw_time, str):
            try:
                checked_at = datetime.fromisoformat(raw_time)
            except ValueError:
                checked_at = None
        passed, tested = entry.get("passed"), entry.get("tested")
        last_status = entry.get("last_status")
        summary_parts: list[str] = []
        if isinstance(passed, int) and isinstance(tested, int):
            summary_parts.append(f"最近实测通过 {passed}/{tested} 次")
        if isinstance(last_status, str) and last_status:
            summary_parts.append(f"最近结果：{last_status[:32]}")
        error_codes = entry.get("error_codes")
        if isinstance(error_codes, list):
            safe_codes = [str(code)[:48] for code in error_codes[:5] if isinstance(code, str)]
            if safe_codes:
                summary_parts.append("分类：" + "、".join(safe_codes))
        statuses.append(PlatformStatus(
            id=platform_id,
            name=name,
            validation_status=str(entry.get("status") or "未验证")[:48],
            checked_at=checked_at,
            summary="；".join(summary_parts) or None,
        ))
    return statuses
