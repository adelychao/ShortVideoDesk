"""Single-window WeChat Channels login and session verification."""
from __future__ import annotations

import asyncio
import ast
import base64
import binascii
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit


_CHANNEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_AVATAR_HOSTS = ("wx.qlogo.cn", "qpic.cn")
_LOGIN_URL = "https://channels.weixin.qq.com"
_HOME_URL = "https://channels.weixin.qq.com/platform"
_OFFICIAL_HOST = "channels.weixin.qq.com"
_QR_HOSTS = frozenset({"channels.weixin.qq.com", "open.weixin.qq.com"})
_LOGIN_POLL_ATTEMPTS = 300
_CHECK_POLL_ATTEMPTS = 30
_POLL_INTERVAL_MS = 2000
_DIAGNOSTIC_EVENT_LIMIT = 80
_RISK_MARKERS = (
    "操作频繁", "访问过于频繁", "存在风险", "风险验证", "访问受限", "环境异常",
    "too many requests", "risk control", "security check",
)


class WechatChannelsLoginError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.status = status


def async_playwright():
    from patchright.async_api import async_playwright as start

    return start()


def _safety(project: Path):
    path = Path(__file__).resolve().parents[1] / "browser_safety.py"
    spec = importlib.util.spec_from_file_location("workbench_browser_safety", path)
    if spec is None or spec.loader is None:
        raise WechatChannelsLoginError("登录组件暂不可用，请检查运行环境后重试。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reserve_xiaohongshu_login(None, project)
    return module


def _storage_identity(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    origins = [entry for entry in payload.get("origins", []) if isinstance(entry, dict)
               and entry.get("origin") == "https://channels.weixin.qq.com"]
    if len(origins) != 1:
        return None
    entries = [entry for entry in origins[0].get("localStorage", []) if isinstance(entry, dict)
               and entry.get("name") == "finder_username"]
    if len(entries) != 1:
        return None
    username = entries[0].get("value")
    if not isinstance(username, str) or not _CHANNEL_ID.fullmatch(username):
        return None
    return hashlib.sha256(f"wechat_channels\0{username}".encode("utf-8")).hexdigest()


def _official_auth_response(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname == _OFFICIAL_HOST and parsed.path.endswith("/auth/auth_data")
    except ValueError:
        return False


def _login_page(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname == _OFFICIAL_HOST and parsed.path.endswith("/login.html")
    except ValueError:
        return False


def _valid_png(data: object) -> bool:
    return isinstance(data, bytes) and data.startswith(b"\x89PNG\r\n\x1a\n") and 128 <= len(data) <= 2 * 1024 * 1024


def _png_from_data_url(value: object) -> bytes | None:
    if not isinstance(value, str) or len(value) > 3 * 1024 * 1024 or not value.startswith("data:image/"):
        return None
    try:
        header, encoded = value.split(",", 1)
        if ";base64" not in header.lower():
            return None
        data = base64.b64decode("".join(encoded.split()), validate=True)
        return data if _valid_png(data) else None
    except (ValueError, binascii.Error):
        return None


def _official_qr_url(frame_url: str, image_src: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(frame_url, image_src))
        if (parsed.scheme == "https" and parsed.hostname in _QR_HOSTS
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443)):
            return parsed.geturl()
    except ValueError:
        pass
    return None


def _configured_chrome_executable(project: Path) -> Path | None:
    """Use the browser configured by SAU so platform login follows its system proxy."""
    config = project / "conf.py"
    try:
        if config.is_symlink() or not config.is_file() or config.stat().st_size > 1024 * 1024:
            return None
        tree = ast.parse(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        return None

    for statement in tree.body:
        value_node = None
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "LOCAL_CHROME_PATH"
            for target in statement.targets
        ):
            value_node = statement.value
        elif (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
              and statement.target.id == "LOCAL_CHROME_PATH"):
            value_node = statement.value
        if value_node is None:
            continue
        try:
            configured = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            continue
        if not isinstance(configured, str) or not configured.strip():
            continue
        executable = Path(configured).expanduser()
        try:
            if executable.is_symlink():
                return None
            resolved = executable.resolve(strict=True)
            return resolved if resolved.is_file() else None
        except OSError:
            return None
    return None


async def _save_qr_png(qr_file: Path, data: bytes) -> bool:
    if not _valid_png(data):
        return False
    temporary = qr_file.with_suffix(qr_file.suffix + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, qr_file)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _official_endpoint_class(url: str, resource_type: str = "other") -> str | None:
    """Return a coarse route class; never expose a query string or tokenized path."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _QR_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            return None
    except ValueError:
        return None

    path = parsed.path.lower()
    if parsed.hostname == "open.weixin.qq.com":
        if path.startswith("/connect/qrconnect"):
            return "qr_page" if resource_type != "image" else "qr_image"
        if resource_type == "image" and ("qr" in path or "qrcode" in path):
            return "qr_image"
        if resource_type == "image":
            return "official_image"
        return "open_weixin_other"
    if path.endswith("/auth/auth_data"):
        return "channels_auth"
    if resource_type == "image" and ("qr" in path or "qrcode" in path):
        return "qr_image"
    if resource_type == "image":
        return "official_image"
    if path.endswith("/login.html") or path in {"", "/"}:
        return "channels_login"
    return "channels_other"


def _resource_type(value: object) -> str:
    allowed = {"document", "stylesheet", "image", "media", "font", "script", "xhr", "fetch",
               "eventsource", "websocket", "manifest", "texttrack", "other"}
    return value if isinstance(value, str) and value in allowed else "other"


def _content_type_class(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    mime = value.split(";", 1)[0].strip().lower()
    if mime == "image/png":
        return "image_png"
    if mime in {"image/jpeg", "image/jpg"}:
        return "image_jpeg"
    if mime.startswith("image/"):
        return "image_other"
    if mime == "text/html":
        return "text_html"
    if mime in {"application/json", "text/json"} or mime.endswith("+json"):
        return "json"
    if mime.startswith("text/"):
        return "text_other"
    return "other"


def _request_failure_code(value: object) -> str:
    match = re.search(r"(?:net::)?(ERR_[A-Z0-9_]{1,64})", str(value or ""))
    return match.group(1) if match else "unknown"


def _record_network_event(qr_file: Path, diagnostic: dict[str, object], event: dict[str, object]) -> None:
    events = diagnostic.setdefault("network_events", [])
    if not isinstance(events, list):
        events = []
        diagnostic["network_events"] = events
    sanitized: dict[str, object] = {}
    endpoint = event.get("endpoint")
    if endpoint in {
        "qr_page", "qr_image", "official_image", "open_weixin_other", "channels_auth",
        "channels_login", "channels_other",
    }:
        sanitized["endpoint"] = endpoint
    status = event.get("status")
    if type(status) is int and 100 <= status <= 599:
        sanitized["status"] = status
    resource_type = event.get("resource_type")
    if isinstance(resource_type, str):
        sanitized["resource_type"] = _resource_type(resource_type)
    error_code = event.get("error_code")
    if isinstance(error_code, str) and re.fullmatch(r"ERR_[A-Z0-9_]{1,64}", error_code):
        sanitized["error_code"] = error_code
    if not sanitized:
        return
    events.append(sanitized)
    del events[:-_DIAGNOSTIC_EVENT_LIMIT]
    _write_qr_capture_diagnostic(qr_file, diagnostic)


def _qr_png_dimensions(data: bytes) -> tuple[int, int] | None:
    if not _valid_png(data) or len(data) < 24:
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if 256 <= width <= 1200 and width == height:
        return width, height
    return None


async def _capture_qr_response(response, qr_file: Path, state: dict[str, object], generation: int) -> bool:
    """Save the official browser response itself; do not issue a second QR request."""
    try:
        if state.get("qr_generation") != generation:
            return False
        if int(getattr(response, "status", 0)) != 200:
            return False
        headers = getattr(response, "headers", {})
        content_type = headers.get("content-type", "") if isinstance(headers, dict) else ""
        if _content_type_class(content_type) != "image_png":
            return False
        content_length = headers.get("content-length", "") if isinstance(headers, dict) else ""
        if content_length and content_length.isdigit() and int(content_length) > 2 * 1024 * 1024:
            return False
        data = await response.body()
        if state.get("qr_generation") != generation or _qr_png_dimensions(data) is None:
            return False
        return await _save_qr_png(qr_file, data)
    except Exception:
        return False


async def _response_matches_qr_image(page, response_url: str) -> bool:
    if _official_qr_url(response_url, response_url) is None:
        return False
    for frame in getattr(page, "frames", []):
        try:
            parsed = urlsplit(frame.url)
            if parsed.scheme != "https" or parsed.hostname not in _QR_HOSTS or parsed.port not in (None, 443):
                continue
            image = frame.locator("img.qrcode").first
            if not await image.count():
                continue
            source = await image.get_attribute("src") or ""
            if source and _official_qr_url(frame.url, source) == response_url:
                return True
        except Exception:
            continue
    return False


async def _capture_matched_qr_response(
    page, response, qr_file: Path, state: dict[str, object], generation: int,
) -> bool:
    if state.get("qr_generation") != generation:
        return False
    if not await _response_matches_qr_image(page, getattr(response, "url", "")):
        return False
    return await _capture_qr_response(response, qr_file, state, generation)


async def _qr_image_fingerprints(page) -> set[str]:
    """Keep a one-attempt in-memory fingerprint so a stale DOM image is not reused."""
    fingerprints: set[str] = set()
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        try:
            parsed = urlsplit(scope.url)
            if parsed.scheme != "https" or parsed.hostname not in _QR_HOSTS or parsed.port not in (None, 443):
                continue
            image = scope.locator("img.qrcode").first
            if not await image.count():
                continue
            source = await image.get_attribute("src") or ""
            data = None
            try:
                data_url = await image.evaluate("""img => {
                  if (!img.complete || !img.naturalWidth || !img.naturalHeight
                      || img.naturalWidth > 1200 || img.naturalHeight > 1200) return '';
                  const canvas = document.createElement('canvas');
                  canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
                  const context = canvas.getContext('2d');
                  if (!context) return '';
                  context.drawImage(img, 0, 0);
                  return canvas.toDataURL('image/png');
                }""")
                data = _png_from_data_url(data_url)
                if data is None and await image.is_visible():
                    data = await image.screenshot(type="png", timeout=3000)
            except Exception:
                data = None
            if _valid_png(data):
                fingerprints.add("png:" + hashlib.sha256(data).hexdigest())
            elif source:
                fingerprints.add("src:" + hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest())
        except Exception:
            continue
    return fingerprints


def _clear_qr_before_retry(qr_file: Path) -> None:
    if qr_file.is_symlink():
        raise WechatChannelsLoginError("登录二维码文件无效，请重新尝试。")
    try:
        qr_file.unlink(missing_ok=True)
    except OSError:
        raise WechatChannelsLoginError("无法刷新登录二维码，请重新尝试。") from None
    if qr_file.exists():
        raise WechatChannelsLoginError("无法刷新登录二维码，请重新尝试。")


def _write_qr_capture_diagnostic(qr_file: Path, diagnostic: dict[str, object]) -> None:
    path = qr_file.parent / "tencent_login_network_debug.json"
    temporary = qr_file.parent / "tencent_login_network_debug.json.tmp"
    try:
        if path.is_symlink() or temporary.is_symlink():
            return
        events = diagnostic.get("network_events", [])
        safe_events = []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            safe_event: dict[str, object] = {}
            endpoint = event.get("endpoint")
            if endpoint in {
                "qr_page", "qr_image", "official_image", "open_weixin_other", "channels_auth",
                "channels_login", "channels_other",
            }:
                safe_event["endpoint"] = endpoint
            status = event.get("status")
            if type(status) is int and 100 <= status <= 599:
                safe_event["status"] = status
            resource_type = event.get("resource_type")
            if isinstance(resource_type, str):
                safe_event["resource_type"] = _resource_type(resource_type)
            error_code = event.get("error_code")
            if isinstance(error_code, str) and re.fullmatch(r"ERR_[A-Z0-9_]{1,64}", error_code):
                safe_event["error_code"] = error_code
            if safe_event:
                safe_events.append(safe_event)
        payload = {"network_events": safe_events[-_DIAGNOSTIC_EVENT_LIMIT:]}
        temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def _capture_qr(page, qr_file: Path, *, reject_fingerprints: set[str] | None = None) -> bool:
    candidates: list[tuple[object, str, bool]] = []
    iframe_selector = 'iframe[src*="login-for-iframe"]'
    try:
        iframe = page.locator(iframe_selector).first
        if await iframe.count():
            iframe_url = _official_qr_url(page.url, await iframe.get_attribute("src") or "")
            if iframe_url:
                qr_image = page.frame_locator(iframe_selector).locator("div#app img.qrcode").first
                candidates.append((qr_image, iframe_url, True))
    except Exception:
        pass
    for frame in page.frames:
        try:
            parsed = urlsplit(frame.url)
            official_frame = parsed.scheme == "https" and parsed.hostname in _QR_HOSTS and parsed.port in (None, 443)
            if official_frame and "login-for-iframe" in frame.url:
                candidates.append((frame.locator("div#app img.qrcode").first, frame.url, True))
            elif official_frame and parsed.hostname == "open.weixin.qq.com" and "/connect/qrconnect" in parsed.path:
                candidates.append((frame.locator("img.qrcode").first, frame.url, True))
        except (AttributeError, ValueError):
            continue
    candidates.extend((page.locator(selector).first, page.url, False) for selector in (
        "div.login-qrcode-wrap img.qrcode",
        "div.qrcode-wrap img.qrcode",
        "img.qrcode",
    ))
    for image, frame_url, wait_for_source in candidates:
        try:
            if wait_for_source:
                try:
                    # SAU's current Channels flow exposes the QR image before the
                    # wrapper finishes showing it. Read its source as soon as it
                    # is attached so a loading overlay cannot hide a usable QR.
                    await image.wait_for(state="attached", timeout=5000)
                except Exception:
                    continue
            if not await image.count():
                continue
            source = ""
            for attempt in range(5 if wait_for_source else 1):
                source = await image.get_attribute("src") or ""
                if source:
                    break
                if attempt < 4:
                    await page.wait_for_timeout(200)
            data = _png_from_data_url(source)
            if data is None and source and not source.startswith("data:"):
                image_url = _official_qr_url(frame_url, source)
                # The browser response listener saves the QR as it arrives. Refetching
                # this URL can invalidate a one-time QR or race the official iframe.
                if not image_url:
                    continue
            if data is None:
                source_is_trusted = not source or source.startswith("data:image/") or _official_qr_url(frame_url, source) is not None
                if not source_is_trusted:
                    continue
                image_state = await image.evaluate("img => ({complete: img.complete, width: img.naturalWidth, height: img.naturalHeight})")
                loaded = (bool(image_state.get("complete")) and int(image_state.get("width", 0)) > 0
                          and int(image_state.get("height", 0)) > 0) if isinstance(image_state, dict) else bool(image_state)
                if not loaded:
                    continue
                try:
                    data_url = await image.evaluate("""img => {
                      if (!img.complete || !img.naturalWidth || !img.naturalHeight
                          || img.naturalWidth > 1200 || img.naturalHeight > 1200) return '';
                      const canvas = document.createElement('canvas');
                      canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
                      const context = canvas.getContext('2d');
                      if (!context) return '';
                      context.drawImage(img, 0, 0);
                      return canvas.toDataURL('image/png');
                    }""")
                    data = _png_from_data_url(data_url)
                except Exception:
                    data = None
                if data is None and await image.is_visible():
                    data = await image.screenshot(type="png", timeout=10000)
            if not _valid_png(data):
                continue
            if reject_fingerprints:
                if "png:" + hashlib.sha256(data).hexdigest() in reject_fingerprints:
                    continue
                if source and "src:" + hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest() in reject_fingerprints:
                    continue
            if await _save_qr_png(qr_file, data):
                return True
        except Exception:
            continue
    return False


async def _refresh_expired_qr(page, *, on_click_attempt=None) -> bool:
    """Refresh an expired SAU Channels QR code without opening another page."""
    selectors = (
        "div.login-qrcode-wrap div.mask.show div.refresh-wrap",
        "div.login-qrcode-wrap div.mask.show .refresh-wrap",
        'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'div.mask.show p.refresh-tip:has-text("网络不可用，点击刷新")',
        '.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")',
        '.mask.show p.refresh-tip:has-text("网络不可用，点击刷新")',
        'p:has-text("加载失败，点击重试")',
        "text=加载失败，点击重试",
    )
    scopes = []
    try:
        if urlsplit(page.url).hostname == _OFFICIAL_HOST:
            scopes.append(page)
    except (AttributeError, ValueError):
        pass
    for frame in getattr(page, "frames", []):
        try:
            parsed = urlsplit(frame.url)
            if (parsed.scheme == "https" and parsed.hostname in _QR_HOSTS
                    and parsed.port in (None, 443) and frame is not getattr(page, "main_frame", None)):
                scopes.append(frame)
        except (AttributeError, ValueError):
            continue
    for scope in scopes:
        for selector in selectors:
            try:
                button = scope.locator(selector).first
                if not await button.count() or not await button.is_visible():
                    continue
            except Exception:
                continue
            if on_click_attempt is not None:
                result = on_click_attempt()
                if asyncio.iscoroutine(result):
                    await result
            try:
                await button.click(timeout=2000)
                await page.wait_for_timeout(1000)
                return True
            except Exception:
                # The click might already have reached the official page. Do not
                # try another control or spend the single retry budget again.
                return False
    return False


async def _visible_risk_marker(page) -> bool:
    """Detect only a boolean marker on official pages; never retain page text."""
    frames = [page]
    frames.extend(getattr(page, "frames", []))
    for frame in frames:
        try:
            parsed = urlsplit(frame.url)
            if parsed.scheme != "https" or parsed.hostname not in _QR_HOSTS or parsed.port not in (None, 443):
                continue
            text = await frame.locator("body").inner_text(timeout=500)
            lowered = text[:20000].lower()
            if any(marker in lowered for marker in _RISK_MARKERS):
                return True
        except Exception:
            continue
    return False


def _validate_paths(project: Path, session_file: Path, qr_file: Path, *, check: bool) -> tuple[Path, Path, Path]:
    root = project.resolve(strict=True)
    cookies = root / "cookies"
    if cookies.is_symlink() or not cookies.is_dir():
        raise WechatChannelsLoginError("无法准备视频号登录，请检查 SAU 账号目录。")
    cookies = cookies.resolve(strict=True)
    session_file = Path(session_file).absolute()
    qr_file = Path(qr_file).absolute()
    if session_file.parent.resolve(strict=True) != cookies or qr_file.parent.resolve(strict=True) != cookies:
        raise WechatChannelsLoginError("登录文件路径无效，请重新尝试。")
    if session_file.is_symlink() or qr_file.is_symlink():
        raise WechatChannelsLoginError("登录文件路径无效，请重新尝试。")
    if check:
        if not session_file.is_file() or session_file.stat().st_size > 5 * 1024 * 1024:
            raise WechatChannelsLoginError("未找到有效的视频号登录状态，请重新登录。")
    elif session_file.exists():
        raise WechatChannelsLoginError("无法准备视频号登录，请重新尝试。")
    return root, session_file, qr_file


async def _run_login(project: Path, session_file: Path, qr_file: Path, *, check: bool) -> dict[str, str]:
    project, session_file, qr_file = _validate_paths(project, session_file, qr_file, check=check)
    safety = _safety(project)
    executable = _configured_chrome_executable(project)
    if executable is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise WechatChannelsLoginError("登录组件暂不可用，请检查运行环境后重试。")
        executable = Path(local_app_data) / "ms-playwright/chromium-1208/chrome-win64/chrome.exe"
    if not executable.is_file():
        raise WechatChannelsLoginError("登录组件暂不可用，请检查运行环境后重试。")
    state: dict[str, object] = {
        "profile": None, "qr_blocked_status": None, "risk_marker": False, "qr_generation": 0,
    }
    response_tasks: set[asyncio.Task] = set()
    request_generations: dict[int, int] = {}
    diagnostic: dict[str, object] = {"network_events": []}
    async with async_playwright() as playwright:
        safety.reserve_xiaohongshu_login(None, project)
        browser = await playwright.chromium.launch(headless=False, executable_path=str(executable))
        try:
            context = await browser.new_context(storage_state=str(session_file)) if check else await browser.new_context()
            page = await context.new_page()

            async def inspect_auth_response(response) -> None:
                if not _official_auth_response(getattr(response, "url", "")):
                    return
                if getattr(response, "status", 0) >= 400:
                    return
                try:
                    payload = await response.json()
                except Exception:
                    return
                profile = _profile_from_auth_data(payload)
                if profile is None:
                    return
                finder = payload["data"]["finderUser"]
                state["profile_candidate"] = profile
                state["candidate_username"] = finder.get("finderUsername")

            def track(coroutine) -> None:
                task = asyncio.create_task(coroutine)
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            def on_request(request) -> None:
                if check:
                    return
                try:
                    resource_type = _resource_type(getattr(request, "resource_type", "other"))
                    endpoint = _official_endpoint_class(getattr(request, "url", ""), resource_type)
                    if resource_type == "image" and endpoint in {"qr_image", "official_image"}:
                        request_generations[id(request)] = int(state["qr_generation"])
                except Exception:
                    return

            def on_response(response) -> None:
                url = getattr(response, "url", "")
                if _official_auth_response(url):
                    track(inspect_auth_response(response))
                if check:
                    return
                try:
                    request = response.request
                    generation = request_generations.pop(id(request), int(state["qr_generation"]))
                    resource_type = _resource_type(getattr(request, "resource_type", "other"))
                    endpoint = _official_endpoint_class(url, resource_type)
                    if endpoint is None:
                        return
                    status = int(getattr(response, "status", 0))
                    event = {
                        "endpoint": endpoint,
                        "status": status,
                        "resource_type": resource_type,
                    }
                    _record_network_event(qr_file, diagnostic, event)
                    if status in {403, 429}:
                        state["qr_blocked_status"] = status
                    if resource_type == "image" and endpoint == "qr_image":
                        track(_capture_qr_response(response, qr_file, state, generation))
                    elif resource_type == "image" and endpoint == "official_image":
                        track(_capture_matched_qr_response(page, response, qr_file, state, generation))
                except Exception:
                    return

            def on_request_failed(request) -> None:
                if check:
                    return
                try:
                    request_generations.pop(id(request), None)
                    resource_type = _resource_type(getattr(request, "resource_type", "other"))
                    endpoint = _official_endpoint_class(getattr(request, "url", ""), resource_type)
                    if endpoint is None:
                        return
                    failure = getattr(request, "failure", None)
                    _record_network_event(qr_file, diagnostic, {
                        "endpoint": endpoint,
                        "resource_type": resource_type,
                        "error_code": _request_failure_code(failure),
                    })
                except Exception:
                    return

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)
            await page.goto(_HOME_URL if check else _LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            attempts = _CHECK_POLL_ATTEMPTS if check else _LOGIN_POLL_ATTEMPTS
            last_qr_capture = 0
            qr_retry_used = False
            if not check:
                await _capture_qr(page, qr_file)
            for attempt in range(attempts):
                if sys.platform == "win32" and not safety._session_unlocked():
                    raise WechatChannelsLoginError("请先解锁电脑，再试一次。")
                profile = state.get("profile")
                if profile is None and isinstance(state.get("profile_candidate"), dict):
                    try:
                        username = await page.evaluate(
                            "() => localStorage.getItem('finder_username')", isolated_context=False
                        )
                    except Exception:
                        username = None
                    if username == state.get("candidate_username"):
                        profile = state["profile_candidate"]
                        state["profile"] = profile
                    elif username:
                        state["identity_mismatch"] = True
                if state.get("identity_mismatch"):
                    raise WechatChannelsLoginError("无法核实视频号账号身份，请重新登录。", status="identity_mismatch")
                if isinstance(profile, dict):
                    if not check:
                        await context.storage_state(path=str(session_file))
                        if session_file.is_symlink() or not session_file.is_file() or session_file.stat().st_size > 5 * 1024 * 1024:
                            raise WechatChannelsLoginError("无法保存视频号登录状态，请重新尝试。")
                    try:
                        stored_identity = _storage_identity(json.loads(session_file.read_text(encoding="utf-8")))
                    except (OSError, ValueError, TypeError):
                        stored_identity = None
                    if stored_identity != profile["identity"]:
                        raise WechatChannelsLoginError("无法核实视频号账号身份，请重新登录。", status="identity_mismatch")
                    return {"status": "connected", **profile}
                if check and _login_page(page.url):
                    return {"status": "expired", "identity": "", "name": "", "avatar_url": ""}
                if not check and attempt and attempt % 2 == 0:
                    if await _visible_risk_marker(page):
                        state["risk_marker"] = True
                    if not qr_retry_used:
                        blocked_status = state.get("qr_blocked_status")
                        if blocked_status not in {403, 429} and not state.get("risk_marker"):
                            async def consume_qr_retry() -> None:
                                nonlocal qr_retry_used
                                state["qr_previous_images"] = await _qr_image_fingerprints(page)
                                _clear_qr_before_retry(qr_file)
                                state["qr_generation"] = int(state["qr_generation"]) + 1
                                qr_retry_used = True

                            if await _refresh_expired_qr(page, on_click_attempt=consume_qr_retry):
                                previous_images = state.get("qr_previous_images")
                                await _capture_qr(
                                    page,
                                    qr_file,
                                    reject_fingerprints=previous_images if isinstance(previous_images, set) else None,
                                )
                                last_qr_capture = attempt
                if not check and attempt - last_qr_capture >= 5:
                    previous_images = state.get("qr_previous_images")
                    await _capture_qr(
                        page,
                        qr_file,
                        reject_fingerprints=previous_images if isinstance(previous_images, set) else None,
                    )
                    last_qr_capture = attempt
                await page.wait_for_timeout(_POLL_INTERVAL_MS)
            if check:
                return {"status": "unavailable", "identity": "", "name": "", "avatar_url": ""}
            raise WechatChannelsLoginError("等待视频号扫码登录超时，请重新发起登录。")
        finally:
            for task in tuple(response_tasks):
                if not task.done():
                    task.cancel()
            await browser.close()


def login(project: Path, session_file: Path, qr_file: Path, *, check: bool = False) -> dict[str, str]:
    return asyncio.run(_run_login(Path(project), Path(session_file), Path(qr_file), check=check))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("session_file", type=Path)
    parser.add_argument("qr_file", type=Path)
    parser.add_argument("action", nargs="?", choices=("check",))
    arguments = parser.parse_args(argv)
    try:
        result = login(arguments.project, arguments.session_file, arguments.qr_file, check=arguments.action == "check")
    except WechatChannelsLoginError as exc:
        if exc.status == "identity_mismatch":
            print(json.dumps({"status": "identity_mismatch"}, ensure_ascii=True))
            return 0
        print(json.dumps({"status": "failed"}, ensure_ascii=True))
        return 1
    except Exception:
        print(json.dumps({"status": "failed"}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


def _safe_avatar(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme == "https" and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443)
                and any(host == root or host.endswith("." + root) for root in _AVATAR_HOSTS)):
            return value
    except ValueError:
        pass
    return ""


def _profile_from_auth_data(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict) or type(payload.get("errCode")) is not int or payload["errCode"] != 0:
        return None
    data = payload.get("data")
    finder = data.get("finderUser") if isinstance(data, dict) else None
    if not isinstance(finder, dict):
        return None
    username = finder.get("finderUsername")
    if not isinstance(username, str) or not _CHANNEL_ID.fullmatch(username):
        return None
    name = finder.get("nickname", "")
    if not isinstance(name, str) or len(name) > 80:
        name = ""
    identity = hashlib.sha256(f"wechat_channels\0{username}".encode("utf-8")).hexdigest()
    return {
        "identity": identity,
        "name": name.strip(),
        "avatar_url": _safe_avatar(finder.get("headImgUrl", "")),
    }


if __name__ == "__main__":
    raise SystemExit(main())
