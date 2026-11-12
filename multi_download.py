"""Anonymous platform adapters and real local media artifact validation."""
from datetime import datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE

from app.xiaohongshu import parse_note
from multi_sources import (
    DownloadIssue, Source, classify_failure, ensure_identity, parse_source,
    redact_url, resolve_source,
)

CHINA = timezone(timedelta(hours=8))


def archive_directory(output_root, platform, media_id, *, archive_id=None):
    key = re.sub(r"[^A-Za-z0-9_-]", "_", str(media_id))[:100]
    if not re.fullmatch(r"[a-z]+", platform):
        raise ValueError("无效的平台目录")
    if archive_id is not None and not re.fullmatch(r"[a-f0-9]{32}", archive_id):
        raise ValueError("无效的任务存档标识")
    suffix = f"_{archive_id}" if archive_id else ""
    return Path(output_root) / f"{datetime.now(CHINA):%Y-%m-%d}_{platform}_{key}{suffix}"


class QuietLogger:
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def asset_formats(assets):
    formats = []
    for index, asset in enumerate(assets, 1):
        for backup, url in enumerate(asset.candidates, 1):
            formats.append({"url": url, "format_id": f"stream-{index}-{backup}",
                            "ext": "mp4", "width": asset.width, "height": asset.height,
                            "filesize": asset.size, "vcodec": asset.video_codec,
                            "acodec": asset.audio_codec})
    return formats


def info_for_xhs(note):
    if not note.videos:
        raise DownloadIssue("此笔记未提供可下载视频；图片笔记不算视频下载成功", "unsupported_media")
    thumbnails = [{"url": u} for asset in note.images for u in asset.candidates]
    return {"id": note.note_id, "title": note.title, "uploader": note.author,
            "uploader_id": note.author_id, "upload_date": (note.upload_date or "").replace("-", "") or None,
            "formats": asset_formats(note.videos), "thumbnails": thumbnails,
            "duration": next((a.duration for a in note.videos if a.duration), None),
            "http_headers": {"Referer": "https://www.xiaohongshu.com/"},
            "extractor": "XiaoHongShu", "extractor_key": "XiaoHongShu"}


def probe_public_page(source):
    from app.browser_safety import require_safe_browser
    require_safe_browser("public page probe")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, timeout=15_000)
        try:
            context = browser.new_context(locale="zh-CN", accept_downloads=False)
            page = context.new_page()
            response = page.goto(source.url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1200)
            evidence = {"http_status": response.status if response else None,
                        "final_url": redact_url(page.url), "page_title": page.title(),
                        "page_excerpt": page.locator("body").inner_text()[:450]}
            return evidence, page.content()
        finally:
            browser.close()


def page_issue(evidence):
    text = evidence.get("page_title", "") + " " + evidence.get("page_excerpt", "")
    if any(w in text for w in ("安全限制", "IP存在风险", "300012")):
        return DownloadIssue("平台明确限制了当前 IP 的匿名访问（安全限制／300012）", "security_blocked", evidence)
    if any(w in text for w in ("登录后查看", "扫码登录", "请先登录", "完成验证")):
        return DownloadIssue("平台页面要求登录或验证，当前只使用匿名会话", "authentication_required", evidence)
    if text.strip().startswith('{"result":'):
        return DownloadIssue("平台返回错误数据，未提供可观看的作品页面", "access_rejected", evidence)
    return None


def extract_xhs(source, ydl, update):
    try:
        note, _ = parse_note(source.url, use_browser_cookies=False, allow_cookie_fallback=False)
        info = info_for_xhs(note)
        ensure_identity(source, info)
        return info
    except Exception as original:
        update(message="正在检查小红书匿名页面是否可正常访问")
        evidence, html = probe_public_page(source)
        update(access_evidence=evidence)
        if issue := page_issue(evidence):
            raise issue from original
        # 作者：zhw；日期：2026-10-24 00:30:57。复用提取器，仅以匿名浏览器实际取得的公开 HTML 替代同一作品的首个网页请求。
        class BrowserDocumentExtractor(XiaoHongShuIE):
            def _download_webpage(self, url, *args, **kwargs):
                if url == source.url:
                    return html
                return super()._download_webpage(url, *args, **kwargs)
        try:
            info = BrowserDocumentExtractor(ydl)._real_extract(source.url)
            ensure_identity(source, info)
            if not info.get("formats"):
                raise DownloadIssue("小红书匿名页面未提供视频流；可能需要有效分享参数或平台验证", "media_unavailable", evidence)
            return info
        except Exception as exc:
            code, message = classify_failure(exc)
            raise DownloadIssue(message, code, evidence) from exc


def published_at(info):
    try:
        if info.get("timestamp") is not None:
            return datetime.fromtimestamp(float(info["timestamp"]), CHINA).isoformat()
        if info.get("upload_date"):
            return datetime.strptime(info["upload_date"], "%Y%m%d").date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return None


def select_post_video(source, info):
    ensure_identity(source, info)
    if info.get("_type") == "multi_video":
        raise DownloadIssue("该作品使用连续视频片段，当前尚未接入完整拼接；未只下载首段冒充完整视频", "unsupported_segmented")
    if info.get("_type") == "playlist":
        entries = list(itertools.islice(info.get("entries") or [], 30))
        videos = [entry for entry in entries if isinstance(entry, dict) and entry.get("formats")]
        if not videos:
            raise DownloadIssue("帖子中没有可下载的视频", "unsupported_media")
        return videos[0], f"该帖子返回多段媒体，本轮仅下载第一段视频（检测到 {len(videos)} 段）"
    if not info.get("formats") and not info.get("url"):
        raise DownloadIssue("作品没有返回视频流，未使用图片代替视频", "unsupported_media")
    return info, None


def cover_artifact(folder: Path, video: Path, *, allow_first_frame=True):
    covers = [p for p in folder.glob("cover.*") if p.suffix.lower() in {".jpg", ".jpeg", ".webp", ".png", ".avif"}]
    for cover in covers:
        check = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(cover)],
                               capture_output=True, text=True, timeout=15)
        if check.returncode == 0 and json.loads(check.stdout).get("streams"):
            return cover, "original", []
    if not allow_first_frame:
        return None, "missing", ["未取得可用原封面（未启用首帧回退）"]
    cover = folder / "preview.jpg"
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-q:v", "3", "-y", str(cover)],
                       check=True, capture_output=True, timeout=30)
        return cover, "video_frame", ["未取得可用的原封面，预览图由视频首帧生成"]
    except (subprocess.SubprocessError, OSError):
        return None, "missing", ["视频已保存，但原封面和首帧预览均未取得"]


def download_multiplatform(value, update, *, output_root, douyin_download, verifier, archive_id=None,
                           allow_first_frame=True, cookie_file=None, browser_cookies=None):
    initial = parse_source(value)
    update(platform=initial.platform, phase="解析", message=f"正在解析{initial.name}公开视频")
    if initial.platform == "douyin":
        if browser_cookies:
            result = douyin_download(value, update, archive_id=archive_id, browser_cookies=browser_cookies)
        else:
            result = douyin_download(value, update, archive_id=archive_id)
        result["metadata"].update(
            platform_id="douyin",
            cover_source="original" if "cover" in result.get("files", {}) else "missing",
            parser="app-session-chrome+yt-dlp" if browser_cookies else "anonymous-chrome+yt-dlp",
            platform_account_session_used=bool(browser_cookies),
        )
        Path(result["files"]["metadata"]).write_text(json.dumps(result["metadata"], ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    source = resolve_source(initial)
    update(resolved_url=redact_url(source.url), phase="解析")
    key = re.sub(r"[^A-Za-z0-9_-]", "_", source.media_id or "video")[:100]
    if source.platform == "bilibili":
        key += "_p" + parse_qs(urlsplit(source.url).query).get("p", ["1"])[0]
    folder = archive_directory(output_root, source.platform, key, archive_id=archive_id)

    def progress(event):
        if event.get("status") == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            update(phase="下载", message=f"正在下载{source.name}媒体", percent=(
                min(100, round(event.get("downloaded_bytes", 0) / total * 100, 1)) if total else None))
        elif event.get("status") == "finished":
            update(phase="合并", message="媒体已传输，正在处理本地文件")

    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": QuietLogger(),
        "outtmpl": {"default": str(folder / "video.%(ext)s"), "thumbnail": str(folder / "cover.%(ext)s")},
        "format": "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b",
        "merge_output_format": "mp4", "writethumbnail": True,
        "socket_timeout": 12, "retries": 1, "fragment_retries": 1,
        "extractor_retries": 0, "noplaylist": True, "overwrites": True,
        "concurrent_fragment_downloads": 4, "progress_hooks": [progress],
    }
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    with YoutubeDL(options) as ydl:
        if source.platform == "xiaohongshu":
            info = extract_xhs(source, ydl, update)
        else:
            info = ydl.extract_info(source.url, download=False)
        if not isinstance(info, dict):
            raise DownloadIssue("解析器未返回视频信息", "extraction_failed")
        info, selection_note = select_post_video(source, info)
        update(parsed=True, page_accessible=True, title=info.get("title"), phase="下载",
               message="作品身份已核对，正在保存视频与封面")
        folder.mkdir(parents=True, exist_ok=True)
        info = ydl.process_ie_result(info, download=True)
    candidates = [p for p in folder.glob("video.*") if p.suffix in {".mp4", ".mkv", ".webm", ".mov", ".flv"}]
    if not candidates:
        raise DownloadIssue("传输结束后未找到视频文件", "download_failed")
    video = sorted(candidates, key=lambda p: (p.suffix != ".mp4", -p.stat().st_mtime))[0]
    update(phase="校验", message="正在核对本地文件的音画流、时长与哈希", percent=100)
    verified = verifier(video, float(info.get("duration") or 0), require_audio=info.get("acodec") not in {None, "none"})
    cover, cover_source, warnings = cover_artifact(folder, video, allow_first_frame=allow_first_frame)
    if selection_note:
        warnings.append(selection_note)
    if not verified.get("audio_codec"):
        warnings.append("该下载文件没有音频流，已按无声视频记录")
    metadata = {
        "id": source.media_id, "media_id": info.get("id"), "title": info.get("title"),
        "author": info.get("uploader") or info.get("creator") or info.get("channel"),
        "author_id": info.get("uploader_id"), "platform": source.name,
        "platform_id": source.platform, "published_at": published_at(info),
        "downloaded_at": datetime.now(CHINA).isoformat(),
        "personal_chrome_cookies_used": False, "cover_source": cover_source,
        "platform_account_session_used": bool(browser_cookies),
        "parser": "anonymous-browser+platform-adapter" if source.platform == "xiaohongshu" else "yt-dlp",
        "quality_scope": "匿名访问可用版本；优先兼容编码，未验证全站最高画质",
        "format_id": info.get("format_id"), "warnings": warnings, **verified,
    }
    metadata_path = folder / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    note = folder / "metadata.md"
    note.write_text(f"# {metadata.get('title') or source.media_id}\n\n"
                    f"- 平台：{source.name}\n- 作者：{metadata.get('author') or '平台未提供'}\n"
                    f"- 发布时间：{metadata.get('published_at') or '平台未提供'}\n"
                    f"- 分辨率：{verified['width']}×{verified['height']}\n"
                    f"- 时长：{verified['duration_seconds']:.3f} 秒\n"
                    f"- 文件：[{video.name}]({video.name})\n"
                    f"- SHA-256：{verified['sha256']}\n\n"
                    + "\n".join(f"> {warning}" for warning in warnings) + "\n", encoding="utf-8")
    files = {"video": str(video), "metadata": str(metadata_path), "note": str(note)}
    if cover:
        files["cover"] = str(cover)
    return {"metadata": metadata, "output_dir": str(folder), "files": files, "warnings": warnings}
