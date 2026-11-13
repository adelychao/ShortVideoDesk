"""Local anonymous multi-platform downloader; never opens a personal profile.

Reuses this repository's link parsing/metadata validation and yt-dlp's media
parser. The original multi-platform application remains available via run.py.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from yt_dlp import YoutubeDL
from yt_dlp.dependencies import requests
from yt_dlp.extractor.tiktok import DouyinIE

from app.douyin import verified_aweme_metadata
from app.browser_safety import browser_automation_paused, require_safe_browser
from app.build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
from app.runtime import RUNTIME_STOP_EVENT, STOP_TOKEN_HEADER, current_runtime_identity
from multi_sources import DownloadIssue, PLATFORMS, classify_failure, parse_source, resolve_source

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
STATE = ROOT / "data" / "workbench-legacy-jobs"
READ_ONLY_LEGACY_STATE = (
    ROOT / "data" / "anonymous-jobs",
    ROOT / "data" / "state",
)
CHINA = timezone(timedelta(hours=8))


def validate_source(value: str) -> str:
    return parse_source(value).url


def canonical_source(value: str) -> tuple[str, str]:
    source = parse_source(value)
    if source.platform != "douyin":
        raise ValueError("抖音解析器不接受其他平台的作品")
    source = resolve_source(source)
    return f"https://www.douyin.com/video/{source.media_id}", source.media_id


def detail_for_request(url: str, payload: object, media_id: str) -> dict | None:
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname != "www.douyin.com"
        or p.port not in (None, 443) or p.username or p.password
        or p.path != "/aweme/v1/web/aweme/detail/"
        or parse_qs(p.query).get("aweme_id") != [media_id]):
        return None
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        return None
    detail = payload.get("aweme_detail")
    if isinstance(detail, dict) and str(detail.get("aweme_id")) == media_id:
        return detail
    return None


def capture_public_detail(url: str, media_id: str, update, *, browser_cookies=None) -> dict:
    from playwright.sync_api import sync_playwright

    found: list[dict] = []
    update(message="正在加载抖音作品页", phase="解析")
    require_safe_browser("anonymous douyin")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, timeout=20_000)
        try:
            # This isolated context receives only platform-filtered cookies from the app profile.
            context = browser.new_context(locale="zh-CN", accept_downloads=False)
            if browser_cookies:
                context.add_cookies(browser_cookies)
            page = context.new_page()

            def on_response(response):
                if found or response.status != 200:
                    return
                if "/aweme/v1/web/aweme/detail/" not in response.url:
                    return
                try:
                    detail = detail_for_request(response.url, response.json(), media_id)
                    if detail and verified_aweme_metadata(detail, media_id):
                        found.append(detail)
                except Exception:
                    return

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            for attempt in range(2):
                if attempt:
                    update(message="作品详情暂未返回，正在刷新页面重试一次", phase="解析")
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                deadline = time.monotonic() + 35
                while not found and time.monotonic() < deadline:
                    page.wait_for_timeout(250)
                if found:
                    break
            if not found:
                raise DownloadIssue("刷新重试后仍未获取到该作品的视频详情。请检查平台账号登录状态，或稍后重新尝试。", "media_unavailable")
            return found[0]
        finally:
            browser.close()


def inspect_video(path: Path, expected_duration: float, *, require_audio: bool = True) -> dict:
    if not path.is_file():
        raise DownloadIssue("归档视频文件不存在，未通过媒体校验", "media_validation_failed")
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height:format=duration,size",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration", 0))
    if not video or (require_audio and not audio) or duration <= 0 or path.stat().st_size <= 0:
        raise DownloadIssue("文件未通过音视频检查，已保留文件供检查", "media_validation_failed")
    if expected_duration > 0 and abs(duration - expected_duration) > max(2, expected_duration * .01):
        raise DownloadIssue("文件时长与原作品不符，未标记为下载成功", "media_validation_failed")
    try:
        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?", "-progress", "pipe:1", "-nostats", "-f", "null", "-",
        ], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
           timeout=min(300, max(30, duration * .5 + 10)))
    except FileNotFoundError as exc:
        raise DownloadIssue("未找到 FFmpeg，无法执行全片解码校验", "local_dependency") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadIssue("全片解码校验超时，文件已保留，未标记为成功", "validation_timeout") from exc
    except subprocess.CalledProcessError as exc:
        raise DownloadIssue("全片解码失败，文件可能截断或损坏，未标记为成功", "media_validation_failed") from exc
    times = re.findall(r"(?m)^out_time_us=([0-9]+)$", decoded.stdout)
    decoded_duration = int(times[-1]) / 1_000_000 if times else 0
    if decoded_duration <= 0 or abs(decoded_duration - duration) > max(2, duration * .01):
        raise DownloadIssue("实际解码时长与容器声明不一致，未标记为成功", "media_validation_failed")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"duration_seconds": duration, "width": video["width"],
            "height": video["height"], "video_codec": video["codec_name"],
            "audio_codec": audio["codec_name"] if audio else None, "size_bytes": path.stat().st_size,
            "sha256": digest, "full_decode_verified": True,
            "decoded_duration_seconds": decoded_duration}


class QuietLogger:
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def _douyin_title_and_description(detail: dict) -> tuple[str, str | None]:
    description_value = detail.get("desc")
    description = description_value.strip() if isinstance(description_value, str) else ""
    item_title = next((
        value.strip()
        for key in ("item_title", "itemTitle", "preview_title", "previewTitle")
        if isinstance((value := detail.get(key)), str) and value.strip()
    ), "")
    if item_title:
        return item_title, description if description and description != item_title else None
    return description, None


def download_video(value: str, update, *, archive_id=None, output_root: Path | None = None,
                   cookie_file: Path | None = None, browser_cookies=None) -> dict:
    url, media_id = canonical_source(value)
    detail = capture_public_detail(url, media_id, update, browser_cookies=browser_cookies)
    title, description = _douyin_title_and_description(detail)
    update(parsed=True, page_accessible=True, title=title or detail.get("desc"), phase="下载",
           message="已核对抖音作品，正在保存视频与封面")
    from multi_download import archive_directory
    folder = archive_directory(output_root or DOWNLOADS, "douyin", media_id, archive_id=archive_id)
    folder.mkdir(parents=True, exist_ok=True)

    def progress(p):
        if p.get("status") == "downloading":
            total = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
            update(phase="下载", message="正在下载视频", percent=(
                round(p.get("downloaded_bytes", 0) / total * 100, 1) if total else None))

    options = {"quiet": True, "noprogress": True, "logger": QuietLogger(),
               "outtmpl": str(folder / "video.%(ext)s"), "writethumbnail": True,
               # Prefer H.264 for broad local playback. Do not claim global maximum quality.
               "format": "best[vcodec=h264]/best", "socket_timeout": 20,
               "retries": 2, "progress_hooks": [progress], "overwrites": True}
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    with YoutubeDL(options) as ydl:
        info = DouyinIE(ydl)._parse_aweme_video_app(detail)
        if str(info.get("id")) != media_id:
            raise ValueError("解析器返回了另一条作品，下载已停止")
        existing_headers = info.get("http_headers")
        media_headers = dict(existing_headers) if isinstance(existing_headers, dict) else {}
        media_headers.setdefault("Referer", url)
        info.update(
            webpage_url=url,
            extractor="Douyin",
            extractor_key="Douyin",
            http_headers=media_headers,
        )
        info = ydl.process_ie_result(info, download=True)
        path = Path(ydl.prepare_filename(info))
    update(phase="校验", message="正在检查本地音视频流与时长", percent=100)
    verified = inspect_video(path, float(info.get("duration") or 0))
    covers = [p for p in folder.glob("video.*") if p.suffix in {".jpg", ".jpeg", ".webp", ".png"}]
    cover = covers[0] if covers else None
    author = detail.get("author", {})
    timestamp = detail.get("create_time")
    metadata = {"id": media_id, "title": title or info.get("title"),
                "media_id": info.get("id"),
                "author": author.get("nickname") or info.get("uploader"),
                "author_handle": info.get("uploader"), "platform": "抖音",
                "published_at": datetime.fromtimestamp(timestamp, CHINA).isoformat() if timestamp else None,
                "cover_source": "original" if cover else "missing",
                "personal_chrome_cookies_used": False,
                "platform_account_session_used": bool(browser_cookies),
                "quality_scope": "匿名网页可用版本，优先 H.264；未验证全站最高画质",
                **verified}
    if description:
        metadata["description"] = description
    (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {"video": str(path), "metadata": str(folder / "metadata.json")}
    if cover:
        files["cover"] = str(cover)
    return {"metadata": metadata, "output_dir": str(folder), "files": files}


app = FastAPI(title="匿名视频下载验证原型")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
lock = threading.RLock()
jobs: dict[str, dict] = {}


def save_job(job_id: str, **changes):
    with lock:
        jobs[job_id].update(changes)
        jobs[job_id] = _sanitize_legacy_job(jobs[job_id])
        STATE.mkdir(parents=True, exist_ok=True)
        temporary = STATE / f"{job_id}.tmp"
        temporary.write_text(json.dumps(jobs[job_id], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(STATE / f"{job_id}.json")


def _sanitize_legacy_job(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in {"source_url", "original_share_url", "raw_url", "webpage_url", "traceback"}:
                continue
            if any(secret in normalized for secret in ("authorization", "api_key", "cookie", "secret")):
                continue
            if normalized == "source" and isinstance(item, str):
                try:
                    from app.workbench.models import sanitize_source_url
                    cleaned[str(key)] = sanitize_source_url(item)
                except (ImportError, TypeError, ValueError):
                    cleaned[str(key)] = "<链接已隐藏>"
                continue
            cleaned[str(key)] = _sanitize_legacy_job(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_legacy_job(item) for item in value[:500]]
    if isinstance(value, str):
        value = re.sub(r"https?://\S+", "<链接已隐藏>", value, flags=re.IGNORECASE)
        value = re.sub(
            r"(?i)(xsec_token|share_token|token|signature|sign|auth|authorization|api[_-]?key)=([^&\s]+)",
            r"\1=<已隐藏>",
            value,
        )
        return value[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


for state_directory in (*READ_ONLY_LEGACY_STATE, STATE):
    for state_file in state_directory.glob("*.json"):
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeError, RecursionError):
            continue
        if not isinstance(saved, dict):
            # The SQLite migration records malformed non-object JSON as invalid.
            # The compatibility view skips it without changing the legacy file.
            continue
        job_id = saved.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if saved.get("status") in {"排队", "进行中"}:
            saved.update(status="已中断", message="上次运行已结束，请显式重试任务")
        try:
            jobs[job_id] = _sanitize_legacy_job(saved)
        except RecursionError:
            continue


def _legacy_job_event(job_id: str, event: dict[str, object]):
    status = event.get("status")
    message = str(event.get("message") or "处理中")
    changes = {"message": message}
    if isinstance(event.get("progress"), (int, float)):
        changes["percent"] = round(float(event["progress"]), 1)
    phase = event.get("phase")
    if isinstance(phase, str):
        changes["phase"] = phase
    if status == "queued":
        changes["status"] = "排队"
    elif status == "running":
        changes["status"] = "进行中"
    elif status == "completed":
        result = event.get("result")
        result = result if isinstance(result, dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        cover = metadata.get("cover_source")
        changes.update(
            status="已完成" if cover != "missing" else "部分完成",
            message=("视频已保存，本地媒体校验通过" + ("；封面未获取" if cover == "missing" else "")),
            result=result,
            finished_at=datetime.now(CHINA).isoformat(),
        )
    elif status in {"failed", "blocked"}:
        changes.update(
            status="失败",
            error_code=str(event.get("error_code") or "download_failed"),
            finished_at=datetime.now(CHINA).isoformat(),
        )
    elif status == "cancelled":
        changes.update(status="已取消", error_code="cancelled", finished_at=datetime.now(CHINA).isoformat())
    save_job(job_id, **changes)


class NewJob(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


@app.get("/api/health")
def health(include_browser_status: bool = True):
    source_build_id = calculate_build_id()
    instance_id, _, server_port = current_runtime_identity()
    payload = {
        "status": "ok",
        "app_id": APP_ID,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "source_build_id": source_build_id,
        "restart_required": source_build_id != BUILD_ID,
        "instance_id": instance_id,
        "server_pid": os.getpid(),
        "server_port": server_port,
        "mode": "anonymous-only",
        "personal_chrome_cookies_used": False,
    }
    if include_browser_status:
        payload["browser_automation_paused"] = browser_automation_paused()
    return payload


@app.post("/api/runtime/stop")
def stop_runtime(request: Request):
    client_host = request.client.host if request.client is not None else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Runtime stop is local-only")

    instance_id, expected_token, _ = current_runtime_identity()
    supplied_token = request.headers.get(STOP_TOKEN_HEADER, "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="This runtime was not started with a stop token")
    if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid runtime stop token")

    def request_stop() -> None:
        time.sleep(0.1)
        RUNTIME_STOP_EVENT.set()

    threading.Thread(target=request_stop, name="runtime-stop", daemon=True).start()
    return {"status": "stopping", "instance_id": instance_id}


@app.get("/api/platforms")
def platform_catalog():
    validation = ROOT / "data" / "platform-validation.json"
    try:
        latest = json.loads(validation.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        latest = {}
    if not isinstance(latest, dict):
        latest = {}
    latest = {
        key: value if isinstance(value, dict) else {}
        for key, value in latest.items()
        if isinstance(key, str)
    }
    return _sanitize_legacy_job([
        {"id": key, "name": name, "validation_status": latest.get(key, {}).get("status", "未验证"),
         "validation": latest.get(key, {})} for key, name in PLATFORMS.items()
    ])


@app.post("/api/jobs", status_code=202)
def create_job(body: NewJob, request: Request):
    if request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "仅接受本地页面提交")
    try:
        source = parse_source(body.url)
        from app.workbench.models import sanitize_source_url
        safe_source = sanitize_source_url(source.url)
    except ValueError as exc:
        raise HTTPException(400, "链接无效或不属于支持的平台。") from exc
    from app.workbench.runner import RunnerError, get_default_runner
    runner = get_default_runner()
    with lock:
        if sum(j["status"] in {"排队", "进行中"} for j in jobs.values()) >= 5:
            raise HTTPException(429, "最多排队 5 个任务，请等当前下载完成")
    try:
        runner.require_archive_root()
        _, task = runner.create_download_task(body.url, allow_first_frame=True)
    except RunnerError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "链接无效或不属于支持的平台。") from exc
    job_id = task.id
    with lock:
        jobs[job_id] = {
            "id": job_id,
            "source": safe_source,
            "platform": source.platform,
            "status": "排队",
            "message": "等待处理",
            "created_at": datetime.now(CHINA).isoformat(),
        }
        save_job(job_id)
    try:
        runner.enqueue(
            job_id,
            transient_source_url=body.url,
            allow_first_frame=True,
            callback=lambda event: _legacy_job_event(job_id, event),
        )
    except RunnerError as exc:
        save_job(job_id, status="失败", message=str(exc), error_code=exc.code)
        raise HTTPException(exc.status_code, str(exc)) from exc
    return {"id": job_id}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        return sorted((_sanitize_legacy_job(job) for job in jobs.values()),
                      key=lambda job: job.get("created_at", ""), reverse=True)


@app.get("/api/jobs/{job_id}/files/{kind}")
def get_file(job_id: str, kind: str):
    from app.workbench.archive import _assert_contained
    from app.workbench.runner import _hash_file, get_default_runner
    runner = get_default_runner()
    root = runner.config.archive_root
    if not isinstance(job_id, str) or not job_id or len(job_id) > 512:
        raise HTTPException(404, "文件不存在")
    legacy_requested = kind.startswith("legacy_")
    base_kind = kind.removeprefix("legacy_")
    if base_kind not in {"video", "cover", "metadata", "note"}:
        raise HTTPException(404, "文件不存在")
    legacy_kind = f"legacy_{base_kind}"
    preferred_kinds = (
        [legacy_kind, base_kind] if legacy_requested else [base_kind, legacy_kind]
    )
    with runner.database.transaction() as connection:
        row = connection.execute(
            """SELECT artifacts.path, artifacts.sha256, tasks.legacy_path
               FROM artifacts JOIN tasks ON tasks.id = artifacts.task_id
               WHERE artifacts.task_id = ? AND artifacts.kind IN (?, ?)
               ORDER BY CASE artifacts.kind WHEN ? THEN 0 ELSE 1 END,
                        artifacts.created_at DESC, artifacts.version DESC LIMIT 1""",
            (job_id, preferred_kinds[0], preferred_kinds[1], preferred_kinds[0]),
        ).fetchone()
    resolved_job_id = job_id
    if row is None:
        resolved_job_id = _find_legacy_task_for_public_id(runner, job_id) or ""
        if resolved_job_id:
            with runner.database.transaction() as connection:
                row = connection.execute(
                    """SELECT artifacts.path, artifacts.sha256, tasks.legacy_path
                       FROM artifacts JOIN tasks ON tasks.id = artifacts.task_id
                       WHERE artifacts.task_id = ? AND artifacts.kind = ?
                       ORDER BY artifacts.created_at DESC, artifacts.version DESC LIMIT 1""",
                    (resolved_job_id, legacy_kind),
                ).fetchone()
    if row is None:
        raise HTTPException(404, "文件不存在")
    try:
        registered_path = Path(str(row["path"]))
        if registered_path.is_symlink():
            raise ValueError("registered file is a symbolic link")
        path = None
        if root is not None:
            try:
                path = _assert_contained(registered_path, root, strict=True)
            except (OSError, ValueError):
                path = None
        if path is None:
            legacy_path_value = row["legacy_path"]
            if not isinstance(legacy_path_value, str) or not legacy_path_value:
                raise ValueError("artifact is outside the configured archive")
            legacy_source = Path(legacy_path_value)
            if legacy_source.is_symlink():
                raise ValueError("legacy source record is not a regular file")
            legacy_source = legacy_source.resolve(strict=True)
            if not legacy_source.is_file():
                raise ValueError("legacy source record is unavailable")
            approved_legacy_roots = (
                ROOT / "data" / "anonymous-jobs",
                ROOT / "data" / "state",
            )
            approved = False
            for allowed_root in approved_legacy_roots:
                if allowed_root.is_symlink():
                    continue
                try:
                    _assert_contained(legacy_source, allowed_root, strict=True)
                    approved = True
                    break
                except (OSError, ValueError):
                    continue
            if not approved:
                raise ValueError("artifact task is not an approved legacy record")
            downloads_root = ROOT / "downloads"
            if downloads_root.is_symlink():
                raise ValueError("legacy downloads directory is not safe")
            path = _assert_contained(registered_path, downloads_root, strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("registered artifact is unavailable")
        digest, _ = _hash_file(path)
        if digest.casefold() != str(row["sha256"]).casefold():
            raise ValueError("registered artifact hash changed")
    except (OSError, ValueError):
        raise HTTPException(404, "文件不存在") from None
    return FileResponse(path, filename=path.name)


def _find_legacy_task_for_public_id(runner, public_id: str) -> str | None:
    """Map an unchanged legacy JSON ID through its registered, allowlisted path."""
    from app.workbench.archive import _assert_contained

    allowed_roots = (
        ROOT / "data" / "anonymous-jobs",
        ROOT / "data" / "state",
    )
    with runner.database.transaction() as connection:
        rows = connection.execute(
            "SELECT id, legacy_path FROM tasks WHERE legacy_path IS NOT NULL"
        ).fetchall()
    matches: set[str] = set()
    for row in rows:
        legacy_path_value = row["legacy_path"]
        if not isinstance(legacy_path_value, str) or not legacy_path_value:
            continue
        source_path = Path(legacy_path_value)
        if source_path.is_symlink():
            continue
        approved_path = None
        for allowed_root in allowed_roots:
            if allowed_root.is_symlink():
                continue
            try:
                approved_path = _assert_contained(source_path, allowed_root, strict=True)
                break
            except (OSError, ValueError):
                continue
        if approved_path is None or approved_path.is_symlink() or not approved_path.is_file():
            continue
        try:
            if approved_path.stat().st_size > 8 * 1024 * 1024:
                continue
            legacy_record = json.loads(approved_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
        if isinstance(legacy_record, dict) and legacy_record.get("id") == public_id:
            matches.add(str(row["id"]))
            if len(matches) > 1:
                return None
    return next(iter(matches)) if matches else None


@app.get("/", response_class=HTMLResponse)
def index():
    from app.workbench.static import frontend_index_path

    frontend_index = frontend_index_path()
    if frontend_index is not None:
        return frontend_index.read_text(encoding="utf-8")
    return (ROOT / "multiplatform.html").read_text(encoding="utf-8")


from app.workbench.api import mount_workbench_api
from app.workbench.static import mount_frontend

mount_workbench_api(app)
mount_frontend(app)


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not args.no_browser:
        import webbrowser
        threading.Timer(1, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port)
