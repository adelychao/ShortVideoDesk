"""App-managed persistent platform login sessions for supported workbench tasks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from pathlib import Path
import json
import queue
import re
import shutil
import tempfile
import threading
import time
from typing import Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

from ..browser_safety import BrowserSafetyError, require_safe_browser
from .database import Database, PROJECT_ROOT


PLATFORM_ACCOUNTS = (
    {"id": "douyin", "name": "抖音", "login_url": "https://www.douyin.com/"},
    {"id": "bilibili", "name": "B 站", "login_url": "https://www.bilibili.com/"},
    {"id": "csdn", "name": "CSDN", "login_url": "https://editor.csdn.net/md/"},
    {"id": "toutiao", "name": "今日头条", "login_url": "https://mp.toutiao.com/profile_v4/graphic/publish"},
)
_PLATFORM_BY_ID = {item["id"]: item for item in PLATFORM_ACCOUNTS}
_ARTICLE_WORKBENCH_PLATFORMS = frozenset({"csdn", "toutiao"})
_AUTH_COOKIE_NAMES = {
    "douyin": {"sessionid", "sessionid_ss", "sid_guard", "sid_tt"},
    "bilibili": {"sessdata", "dedeuserid", "bili_jct"},
    "csdn": {"usertoken", "username", "userinfo"},
    "toutiao": {"sessionid", "sessionid_ss", "sid_tt"},
}
_COOKIE_DOMAINS = {
    "douyin": "douyin.com", "bilibili": "bilibili.com", "csdn": "csdn.net",
    "toutiao": "toutiao.com",
}
_COOKIE_TIMEOUT_SECONDS = 20
_LOGGER = logging.getLogger(__name__)


class PlatformAccountError(RuntimeError):
    pass


class PlatformAccountSelectionRequired(PlatformAccountError):
    pass


@dataclass
class _LoginCommand:
    kind: str
    payload: object | None = None
    completed: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: str | None = None


@dataclass
class _LoginWindow:
    platform: str
    profile_path: Path
    commands: queue.Queue[_LoginCommand] = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    startup_error: str | None = None
    thread: threading.Thread | None = None


class PlatformAccountManager:
    """Manage isolated Playwright profiles and expose only platform-scoped cookies."""

    def __init__(self, profile_root: Path | None = None) -> None:
        self.profile_root = Path(profile_root or PROJECT_ROOT / "data" / "accounts" / "profiles")
        self._lock = threading.RLock()
        self._windows: dict[str, _LoginWindow] = {}

    def account_status(self, platform: str) -> dict[str, object]:
        definition = self._platform(platform)
        window_open = self._window_is_open(platform)
        try:
            cookies = self.cookies(platform)
        except PlatformAccountError as exc:
            return {
                "id": platform,
                "name": definition["name"],
                "status": "error",
                "authenticated": False,
                "login_window_open": window_open,
                "message": str(exc),
            }
        authenticated = self._has_auth_cookie(platform, cookies)
        profile = (
            self._csdn_profile_from_cookies(cookies, self._read_csdn_avatar_url())
            if platform == "csdn" and authenticated else {}
        )
        if platform == "toutiao" and authenticated:
            profile = self._load_toutiao_profile_cache()
        if authenticated:
            message = "检测到平台登录会话；平台仍可能要求重新登录。"
            status = "connected"
        elif window_open:
            if platform == "csdn":
                message = "CSDN 编辑器已在工作台窗口打开；完成登录后可在同一窗口手动发布，再刷新状态。"
            elif platform == "toutiao":
                message = "头条号创作页已在工作台窗口打开；完成登录后可在同一窗口手动发布，再刷新状态。"
            else:
                message = "登录窗口已打开；完成登录后关闭窗口并刷新状态。"
            status = "login_window_open"
        else:
            message = "尚未检测到平台登录会话。"
            status = "not_connected"
        return {
            "id": platform,
            "name": definition["name"],
            "status": status,
            "authenticated": authenticated,
            "login_window_open": window_open,
            "message": message,
            **profile,
        }

    def all_statuses(self) -> list[dict[str, object]]:
        return [
            self.account_status(item["id"])
            for item in PLATFORM_ACCOUNTS
            if item["id"] not in {"csdn", "toutiao"}
        ]

    def start_login(self, platform: str) -> dict[str, object]:
        try:
            require_safe_browser("platform login")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        definition = self._platform(platform)
        with self._lock:
            existing = self._windows.get(platform)
            if existing and existing.thread and existing.thread.is_alive():
                return self.account_status(platform)
            profile_path = self._profile_path(platform)
            profile_path.mkdir(parents=True, exist_ok=True)
            window = _LoginWindow(platform=platform, profile_path=profile_path)
            thread = threading.Thread(
                target=self._login_window_worker,
                args=(window, str(definition["login_url"])),
                name=f"platform-login-{platform}",
                daemon=True,
            )
            window.thread = thread
            self._windows[platform] = window
            thread.start()

        if not window.ready.wait(timeout=35):
            self._forget_window(window)
            raise PlatformAccountError("登录窗口启动超时；请检查 Playwright 浏览器运行环境。")
        if window.startup_error:
            self._forget_window(window)
            raise PlatformAccountError(window.startup_error)
        return self.account_status(platform)

    def open_workbench(self, platform: str) -> dict[str, object]:
        try:
            require_safe_browser("open platform workbench")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        if platform not in _ARTICLE_WORKBENCH_PLATFORMS:
            raise PlatformAccountError("该平台没有可打开的网页登录工作台。")
        definition = self._platform(platform)
        with self._lock:
            window = self._windows.get(platform)
            if window is not None and window.thread is not None and window.thread.is_alive():
                command = _LoginCommand("open_page")
                window.commands.put(command)
            else:
                window = None
                command = None
        if window is None:
            status = self.start_login(platform)
            return self._workbench_open_status(definition, status)
        if not window.ready.wait(timeout=35):
            raise PlatformAccountError("工作台窗口启动超时；请检查 Playwright 浏览器运行环境。")
        if window.startup_error:
            raise PlatformAccountError(window.startup_error)
        if command is None or not command.completed.wait(timeout=30):
            raise PlatformAccountError("工作台页面打开超时；请检查浏览器窗口后重试。")
        if command.error:
            raise PlatformAccountError(command.error)
        return self._workbench_open_status(definition, self.account_status(platform))

    def publish_csdn_article(self, document, options, cover_path, before_submit):
        from .csdn_publishing import prepare_csdn_options
        prepare_csdn_options(options)
        try:
            require_safe_browser("open platform workbench")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        self.require_authenticated_cookies("csdn")
        self.start_login("csdn")
        window = self._get_open_window("csdn")
        if window is None:
            raise PlatformAccountError("CSDN 工作台未能打开，请检查登录状态。")
        command = _LoginCommand("publish_csdn", (document, options, cover_path, before_submit))
        window.commands.put(command)
        while not command.completed.wait(timeout=1):
            if window.thread is None or not window.thread.is_alive():
                raise PlatformAccountError("CSDN 工作台已关闭，请核对平台文章状态。")
        if command.error:
            raise PlatformAccountError(command.error)
        return command.result

    def publish_toutiao_article(self, document, settings, cover_paths, body_image_paths, before_submit):
        try:
            require_safe_browser("open platform workbench")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        self.require_authenticated_cookies("toutiao")
        self.start_login("toutiao")
        window = self._get_open_window("toutiao")
        if window is None:
            raise PlatformAccountError("今日头条工作台未能打开，请检查登录状态。")
        command = _LoginCommand("publish_toutiao", (document, settings, cover_paths, body_image_paths, before_submit))
        window.commands.put(command)
        while not command.completed.wait(timeout=1):
            if window.thread is None or not window.thread.is_alive():
                raise PlatformAccountError("今日头条工作台已关闭，请核对平台文章状态。")
        if command.error:
            raise PlatformAccountError(command.error)
        return command.result

    def get_toutiao_publish_options(self):
        try:
            require_safe_browser("open platform workbench")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        self.require_authenticated_cookies("toutiao")
        window = self._get_open_window("toutiao")
        if window is not None:
            command = _LoginCommand("toutiao_options")
            window.commands.put(command)
            while not command.completed.wait(timeout=1):
                if window.thread is None or not window.thread.is_alive():
                    raise PlatformAccountError("今日头条工作台已关闭，请重新检查账号状态。")
            if command.error:
                raise PlatformAccountError(command.error)
            return command.result

        profile_path = self._profile_path("toutiao")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=True,
                    locale="zh-CN",
                    accept_downloads=False,
                    timeout=25_000,
                )
                try:
                    from .toutiao_publishing import read_toutiao_publication_options
                    return read_toutiao_publication_options(context)
                finally:
                    context.close()
        except PlatformAccountError:
            raise
        except Exception as exc:
            _LOGGER.warning("今日头条发表选项读取失败：%s", type(exc).__name__)
            raise PlatformAccountError("无法读取当前头条号发表选项，请检查账号登录状态后重试。") from exc

    def ensure_toutiao_profile(self) -> dict[str, object]:
        """Read and persist Toutiao display metadata once for the local account profile."""
        cookies = self.cookies("toutiao")
        if not self._has_auth_cookie("toutiao", cookies):
            return self.account_status("toutiao")
        cached = self._load_toutiao_profile_cache()
        if cached.get("profile_name") or cached.get("profile_avatar_url"):
            return self.account_status("toutiao")
        profile = self._read_toutiao_profile()
        if profile.get("profile_name") or profile.get("profile_avatar_url"):
            self._save_toutiao_profile_cache(profile)
        return self.account_status("toutiao")

    def get_csdn_tags(self, document, keyword):
        try:
            require_safe_browser("open platform workbench")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        self.require_authenticated_cookies("csdn")
        self.start_login("csdn")
        window = self._get_open_window("csdn")
        if window is None:
            raise PlatformAccountError("CSDN 工作台未能打开，请检查登录状态。")
        command = _LoginCommand("csdn_tags", (document, keyword))
        window.commands.put(command)
        while not command.completed.wait(timeout=1):
            if window.thread is None or not window.thread.is_alive():
                raise PlatformAccountError("CSDN 工作台已关闭，请重新登录。")
        if command.error:
            raise PlatformAccountError(command.error)
        return command.result

    @staticmethod
    def _workbench_open_status(
        definition: dict[str, str],
        status: dict[str, object],
    ) -> dict[str, object]:
        if status.get("authenticated"):
            status["message"] = f"已打开{definition['name']}工作台；平台可能仍要求重新登录。"
        return status

    def cookies(self, platform: str) -> list[dict[str, object]]:
        self._platform(platform)
        window = self._get_open_window(platform)
        if window is not None:
            command = _LoginCommand("cookies")
            window.commands.put(command)
            if not command.completed.wait(timeout=_COOKIE_TIMEOUT_SECONDS):
                raise PlatformAccountError("应用登录窗口暂时无响应，请关闭窗口后重试。")
            if command.error:
                raise PlatformAccountError(command.error)
            raw = command.result if isinstance(command.result, list) else []
        else:
            raw = self._load_profile_cookies(platform)
        return [
            cookie for cookie in raw
            if self._cookie_matches_platform(platform, cookie) and not self._cookie_is_expired(cookie)
        ]

    def require_authenticated_cookies(
        self,
        platform: str,
        account_id: str | None = None,
        database: Database | None = None,
    ) -> list[dict[str, object]]:
        if account_id:
            _account, cookies = resolve_video_publish_account(database or Database(), platform, account_id)
            return cookies
        cookies = self.cookies(platform)
        if not self._has_auth_cookie(platform, cookies):
            raise PlatformAccountError("未检测到该平台的登录会话；请先在账号管理的“视频发布”中登录并刷新状态。")
        return cookies

    @contextmanager
    def temporary_cookie_file(
        self,
        platform: str,
        cookies: list[dict[str, object]] | None = None,
    ) -> Iterator[Path]:
        selected_cookies = cookies if cookies is not None else self.require_authenticated_cookies(platform)
        cookies = [
            cookie for cookie in selected_cookies
            if self._cookie_matches_platform(platform, cookie) and not self._cookie_is_expired(cookie)
        ]
        self.profile_root.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".request-cookie-",
            suffix=".txt",
            dir=self.profile_root,
            delete=False,
        )
        cookie_path = Path(handle.name)
        try:
            with handle:
                handle.write("# Netscape HTTP Cookie File\n")
                for cookie in cookies:
                    row = self._netscape_cookie_row(cookie)
                    if row is not None:
                        handle.write(row + "\n")
            yield cookie_path
        finally:
            cookie_path.unlink(missing_ok=True)

    def clear_account(self, platform: str) -> dict[str, object]:
        self._platform(platform)
        window = self._get_open_window(platform)
        if window is not None:
            command = _LoginCommand("close")
            window.commands.put(command)
            if not command.completed.wait(timeout=10):
                raise PlatformAccountError("登录窗口未能正常关闭；请先手动关闭窗口，再清除状态。")
            if window.thread is not None:
                window.thread.join(timeout=5)
                if window.thread.is_alive():
                    raise PlatformAccountError("登录窗口仍在运行；请先手动关闭窗口，再清除状态。")
        profile_path = self._profile_path(platform)
        if profile_path.is_symlink():
            raise PlatformAccountError("账号配置目录异常，未执行清理。")
        if profile_path.exists():
            try:
                shutil.rmtree(profile_path)
            except OSError as exc:
                raise PlatformAccountError("无法清除平台账号状态；请确认应用对项目数据目录有访问权限。") from exc
        return self.account_status(platform)

    def shutdown(self) -> None:
        with self._lock:
            windows = list(self._windows.values())
        for window in windows:
            if window.thread is None or not window.thread.is_alive():
                continue
            command = _LoginCommand("close")
            window.commands.put(command)
            command.completed.wait(timeout=5)
            window.thread.join(timeout=5)

    def _login_window_worker(self, window: _LoginWindow, login_url: str) -> None:
        context = None
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(window.profile_path),
                    headless=False,
                    no_viewport=True,
                    locale="zh-CN",
                    accept_downloads=False,
                    timeout=25_000,
                )
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                try:
                    page.goto(login_url, wait_until="domcontentloaded", timeout=25_000)
                except PlaywrightTimeoutError:
                    pass
                window.ready.set()
                while True:
                    try:
                        command = window.commands.get(timeout=0.25)
                    except queue.Empty:
                        if not context.pages:
                            break
                        continue
                    if command.kind == "cookies":
                        try:
                            command.result = context.cookies()
                        except Exception:
                            command.error = "无法读取当前平台登录状态。"
                        finally:
                            command.completed.set()
                    elif command.kind == "csdn_profile":
                        try:
                            command.result = next((
                                avatar for active_page in context.pages
                                if (avatar := self._csdn_avatar_from_page(active_page))
                            ), None)
                        except Exception:
                            command.error = "无法读取 CSDN 头像资料。"
                        finally:
                            command.completed.set()
                    elif command.kind == "toutiao_profile":
                        try:
                            command.result = next((
                                profile for active_page in context.pages
                                if (profile := self._toutiao_profile_from_page(active_page)).get("profile_name")
                                or profile.get("profile_avatar_url")
                            ), {})
                        except Exception:
                            command.error = "无法读取今日头条账号资料。"
                        finally:
                            command.completed.set()
                    elif command.kind == "open_page":
                        try:
                            active_page = context.new_page()
                            try:
                                active_page.goto(login_url, wait_until="domcontentloaded", timeout=25_000)
                            except PlaywrightTimeoutError:
                                pass
                        except Exception:
                            command.error = "无法打开平台工作台页面；请检查浏览器窗口后重试。"
                        finally:
                            command.completed.set()
                    elif command.kind == "publish_csdn":
                        try:
                            from .csdn_publishing import publish_csdn_article
                            document, options, cover_path, before_submit = command.payload
                            command.result = publish_csdn_article(context, document, options, cover_path, before_submit)
                        except Exception as exc:
                            command.error = str(exc) or "CSDN 发布未完成。"
                        finally:
                            command.completed.set()
                    elif command.kind == "publish_toutiao":
                        try:
                            from .toutiao_publishing import publish_toutiao_article
                            document, settings, cover_paths, body_image_paths, before_submit = command.payload
                            command.result = publish_toutiao_article(
                                context, document, settings, cover_paths, body_image_paths, before_submit,
                            )
                        except Exception as exc:
                            command.error = str(exc) or "今日头条发表未完成。"
                        finally:
                            command.completed.set()
                    elif command.kind == "toutiao_options":
                        try:
                            from .toutiao_publishing import read_toutiao_publication_options
                            command.result = read_toutiao_publication_options(context)
                        except Exception as exc:
                            _LOGGER.warning("今日头条发表选项读取失败：%s", type(exc).__name__)
                            command.error = "无法读取当前头条号发表选项，请检查账号登录状态后重试。"
                        finally:
                            command.completed.set()
                    elif command.kind == "csdn_tags":
                        try:
                            from .csdn_tags import read_csdn_tags
                            document, keyword = command.payload
                            command.result = read_csdn_tags(context, document, keyword)
                        except Exception as exc:
                            _LOGGER.warning("CSDN 标签读取失败：%s", type(exc).__name__)
                            command.error = "无法读取 CSDN 标签，请检查账号登录状态和平台页面后重试。"
                        finally:
                            command.completed.set()
                    elif command.kind == "close":
                        try:
                            context.close()
                        finally:
                            command.completed.set()
                        break
        except Exception:
            window.startup_error = "无法启动平台登录窗口；请确认 Playwright 浏览器已安装且未被安全软件拦截。"
            window.ready.set()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            self._forget_window(window)

    def _load_profile_cookies(self, platform: str) -> list[dict[str, object]]:
        profile_path = self._profile_path(platform)
        if not profile_path.is_dir() or not any(profile_path.iterdir()):
            return []
        try:
            require_safe_browser("platform account session")
        except BrowserSafetyError as exc:
            raise PlatformAccountError(str(exc)) from exc
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=True,
                    locale="zh-CN",
                    accept_downloads=False,
                    timeout=25_000,
                )
                try:
                    return context.cookies()
                finally:
                    context.close()
        except Exception as exc:
            raise PlatformAccountError("读取应用专用登录档案失败；请关闭登录窗口后重试。") from exc

    def _read_csdn_avatar_url(self) -> str | None:
        window = self._get_open_window("csdn")
        if window is not None:
            command = _LoginCommand("csdn_profile")
            window.commands.put(command)
            if command.completed.wait(timeout=_COOKIE_TIMEOUT_SECONDS) and not command.error:
                return self._safe_csdn_avatar_url(command.result)
            return None

        profile_path = self._profile_path("csdn")
        if not profile_path.is_dir() or not any(profile_path.iterdir()):
            return None
        try:
            require_safe_browser("platform account session")
        except BrowserSafetyError:
            return None

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=True,
                    locale="zh-CN",
                    accept_downloads=False,
                    timeout=25_000,
                )
                try:
                    pages = context.pages
                    page = pages[0] if pages else context.new_page()
                    if not page.url.startswith("https://editor.csdn.net/"):
                        page.goto("https://editor.csdn.net/md/", wait_until="domcontentloaded", timeout=15_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=4_000)
                    except Exception as exc:
                        _LOGGER.debug("CSDN editor did not reach network idle: %s", type(exc).__name__)
                    return self._csdn_avatar_from_page(page)
                finally:
                    context.close()
        except Exception as exc:
            _LOGGER.debug("Could not read CSDN profile image: %s", type(exc).__name__)
            return None

    @classmethod
    def _csdn_avatar_from_page(cls, page: object) -> str | None:
        try:
            avatar = page.locator("img.img-user-avatar")
            avatar.wait_for(state="attached", timeout=5_000)
            raw_url = avatar.get_attribute("src", timeout=3_000)
        except Exception as exc:
            _LOGGER.debug("Could not read CSDN avatar element: %s", type(exc).__name__)
            return None
        return cls._safe_csdn_avatar_url(raw_url)

    @staticmethod
    def _safe_csdn_avatar_url(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = urlsplit(value)
            hostname = (parsed.hostname or "").casefold()
            if (
                parsed.scheme.casefold() != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or not (hostname == "csdnimg.cn" or hostname.endswith(".csdnimg.cn"))
            ):
                return None
        except ValueError:
            return None
        if not parsed.path.startswith("/"):
            return None
        return urlunsplit(("https", hostname, parsed.path, "", ""))

    @classmethod
    def _csdn_profile_from_cookies(
        cls,
        cookies: list[dict[str, object]],
        avatar_url: object = None,
    ) -> dict[str, str | None]:
        profile_cookies = {
            str(cookie.get("name", "")).casefold(): unquote(str(cookie.get("value", ""))).strip()
            for cookie in cookies
            if str(cookie.get("name", "")).casefold() in {"username", "usernick"}
        }
        username = profile_cookies.get("username") or None
        nickname = profile_cookies.get("usernick") or None
        return {
            "profile_name": nickname,
            "profile_username": username,
            "profile_avatar_url": cls._safe_csdn_avatar_url(avatar_url),
        }

    def _read_toutiao_profile(self) -> dict[str, str | None]:
        window = self._get_open_window("toutiao")
        if window is not None:
            command = _LoginCommand("toutiao_profile")
            window.commands.put(command)
            if command.completed.wait(timeout=_COOKIE_TIMEOUT_SECONDS) and not command.error:
                result = command.result if isinstance(command.result, dict) else {}
                return self._toutiao_profile_from_payload({"data": {"user": {
                    "screen_name": result.get("profile_name"),
                    "https_avatar_url": result.get("profile_avatar_url"),
                }}})
            return {"profile_name": None, "profile_avatar_url": None}

        profile_path = self._profile_path("toutiao")
        if not profile_path.is_dir() or not any(profile_path.iterdir()):
            return {"profile_name": None, "profile_avatar_url": None}
        try:
            require_safe_browser("platform account session")
        except BrowserSafetyError:
            return {"profile_name": None, "profile_avatar_url": None}

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=True,
                    locale="zh-CN",
                    accept_downloads=False,
                    timeout=25_000,
                )
                try:
                    pages = context.pages
                    page = pages[0] if pages else context.new_page()
                    if self._toutiao_page_host(page) != "mp.toutiao.com":
                        page.goto("https://mp.toutiao.com/profile_v4/index", wait_until="domcontentloaded", timeout=15_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=4_000)
                    except Exception as exc:
                        _LOGGER.debug("Toutiao creator page did not reach network idle: %s", type(exc).__name__)
                    return self._toutiao_profile_from_page(page)
                finally:
                    context.close()
        except Exception as exc:
            _LOGGER.debug("Could not read Toutiao profile: %s", type(exc).__name__)
            return {"profile_name": None, "profile_avatar_url": None}

    def _load_toutiao_profile_cache(self) -> dict[str, str | None]:
        cache_path = self._profile_path("toutiao") / "workbench-profile.json"
        if cache_path.is_symlink() or not cache_path.is_file():
            return {"profile_name": None, "profile_avatar_url": None}
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"profile_name": None, "profile_avatar_url": None}
        if not isinstance(payload, dict):
            return {"profile_name": None, "profile_avatar_url": None}
        return self._toutiao_profile_from_payload({"data": {"user": {
            "screen_name": payload.get("profile_name"),
            "https_avatar_url": payload.get("profile_avatar_url"),
        }}})

    def _save_toutiao_profile_cache(self, profile: dict[str, str | None]) -> None:
        safe_profile = self._toutiao_profile_from_payload({"data": {"user": {
            "screen_name": profile.get("profile_name"),
            "https_avatar_url": profile.get("profile_avatar_url"),
        }}})
        if not safe_profile.get("profile_name") and not safe_profile.get("profile_avatar_url"):
            return
        profile_path = self._profile_path("toutiao")
        profile_path.mkdir(parents=True, exist_ok=True)
        cache_path = profile_path / "workbench-profile.json"
        if cache_path.is_symlink():
            raise PlatformAccountError("今日头条本地资料缓存位置异常，未保存账号资料。")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=".profile-", suffix=".tmp",
                dir=profile_path, delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(safe_profile, handle, ensure_ascii=False)
                handle.write("\n")
            temporary_path.replace(cache_path)
        except OSError as exc:
            raise PlatformAccountError("无法保存今日头条账号昵称和头像到本机。") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _toutiao_page_host(page: object) -> str:
        try:
            return str(urlsplit(str(page.url)).hostname or "").casefold()
        except (AttributeError, TypeError, ValueError):
            return ""

    @classmethod
    def _toutiao_profile_from_page(cls, page: object) -> dict[str, str | None]:
        if cls._toutiao_page_host(page) != "mp.toutiao.com":
            return {"profile_name": None, "profile_avatar_url": None}
        try:
            payload = page.evaluate("""async () => {
              const response = await fetch('/mp/agw/media/get_media_info', {
                credentials: 'include',
                headers: { 'Accept': 'application/json, text/plain, */*' },
              });
              if (!response.ok) return null;
              return await response.json();
            }""")
        except Exception as exc:
            _LOGGER.debug("Could not read Toutiao profile response: %s", type(exc).__name__)
            return {"profile_name": None, "profile_avatar_url": None}
        return cls._toutiao_profile_from_payload(payload)

    @classmethod
    def _toutiao_profile_from_payload(cls, payload: object) -> dict[str, str | None]:
        data = payload.get("data") if isinstance(payload, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return {"profile_name": None, "profile_avatar_url": None}
        raw_name = user.get("screen_name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if len(name) > 80:
            name = ""
        return {
            "profile_name": name or None,
            "profile_avatar_url": cls._safe_toutiao_avatar_url(user.get("https_avatar_url")),
        }

    @staticmethod
    def _safe_toutiao_avatar_url(value: object) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 2048:
            return None
        try:
            parsed = urlsplit(value)
            hostname = (parsed.hostname or "").casefold().rstrip(".")
            if (
                parsed.scheme.casefold() != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or not any(hostname == root or hostname.endswith("." + root)
                           for root in ("toutiaoimg.com", "toutiaostatic.com", "pstatp.com"))
                or not parsed.path.startswith("/")
            ):
                return None
        except ValueError:
            return None
        return urlunsplit(("https", hostname, parsed.path, "", ""))

    def _profile_path(self, platform: str) -> Path:
        self._platform(platform)
        path = (self.profile_root / platform).resolve(strict=False)
        root = self.profile_root.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PlatformAccountError("平台账号配置目录无效。") from exc
        return path

    @staticmethod
    def _platform(platform: str) -> dict[str, str]:
        if not isinstance(platform, str) or platform not in _PLATFORM_BY_ID:
            raise PlatformAccountError("当前尚未开放该平台的登录会话。")
        return _PLATFORM_BY_ID[platform]

    def _get_open_window(self, platform: str) -> _LoginWindow | None:
        with self._lock:
            window = self._windows.get(platform)
            if window is not None and window.thread is not None and window.thread.is_alive():
                return window
            return None

    def _window_is_open(self, platform: str) -> bool:
        return self._get_open_window(platform) is not None

    def _forget_window(self, window: _LoginWindow) -> None:
        with self._lock:
            if self._windows.get(window.platform) is window:
                self._windows.pop(window.platform, None)

    @staticmethod
    def _cookie_matches_platform(platform: str, cookie: dict[str, object]) -> bool:
        domain = cookie.get("domain")
        if not isinstance(domain, str):
            return False
        domain = domain.lstrip(".").casefold().rstrip(".")
        root = _COOKIE_DOMAINS[platform]
        return domain == root or domain.endswith("." + root)

    @classmethod
    def _has_auth_cookie(cls, platform: str, cookies: list[dict[str, object]]) -> bool:
        names = _AUTH_COOKIE_NAMES[platform]
        present = {
            str(cookie.get("name", "")).casefold()
            for cookie in cookies
            if isinstance(cookie.get("name"), str)
            and cookie.get("value")
            and not cls._cookie_is_expired(cookie)
        }
        if platform == "csdn":
            return "usertoken" in present and bool(present & {"username", "userinfo"})
        return bool(present & names) and any(
            isinstance(cookie.get("name"), str)
            and str(cookie["name"]).casefold() in names
            and not cls._cookie_is_expired(cookie)
            for cookie in cookies
        )

    @staticmethod
    def _cookie_is_expired(cookie: dict[str, object]) -> bool:
        expires = cookie.get("expires")
        return isinstance(expires, (int, float)) and expires > 0 and expires <= time.time()

    @staticmethod
    def _netscape_cookie_row(cookie: dict[str, object]) -> str | None:
        values = [cookie.get("domain"), cookie.get("path"), cookie.get("name"), cookie.get("value")]
        if any(not isinstance(value, str) or any(ch in value for ch in "\t\r\n") for value in values):
            return None
        domain, path, name, value = (str(item) for item in values)
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") is True else "FALSE"
        expires = cookie.get("expires")
        expiry = str(max(0, int(expires))) if isinstance(expires, (int, float)) else "0"
        if cookie.get("httpOnly") is True:
            domain = "#HttpOnly_" + domain
        return "\t".join((domain, include_subdomains, path or "/", secure, expiry, name, value))


def resolve_video_publish_account(
    database: Database,
    platform: str,
    account_id: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Return a connected publishing account and its platform-scoped SAU cookies."""
    if platform not in _PLATFORM_BY_ID:
        raise PlatformAccountError("该平台暂不支持使用视频发布账号采集。")
    from .video_publishing import SocialAutoUploadAdapter, list_video_publish_accounts

    candidates = [
        item for item in list_video_publish_accounts(database)
        if item.get("platform_id") == platform and item.get("enabled") is True
    ]
    if account_id:
        account = next((item for item in candidates if item.get("id") == account_id), None)
        if account is None:
            raise PlatformAccountError("所选账号不存在或不属于当前平台，请重新选择。")
    elif len(candidates) == 1:
        account = candidates[0]
    elif len(candidates) > 1:
        raise PlatformAccountSelectionRequired("该平台有多个账号，请选择本次任务使用的账号。")
    else:
        raise PlatformAccountError("请先在账号管理的“视频发布”中添加并登录该平台账号。")

    if account.get("status") != "connected":
        raise PlatformAccountError("所选账号尚未连接，请先在账号管理中完成登录并检查状态。")
    alias = account.get("account_alias")
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias):
        raise PlatformAccountError("所选账号凭据不可用，请在账号管理中重新登录。")

    adapter = SocialAutoUploadAdapter(database)
    command, message = adapter.resolve_command()
    if command is None:
        raise PlatformAccountError(message or "视频账号凭据目录不可用，请检查 SAU 配置。")
    project = command.cwd.resolve(strict=True)
    cookies_dir = project / "cookies"
    if cookies_dir.is_symlink() or not cookies_dir.is_dir():
        raise PlatformAccountError("所选账号凭据不可用，请在账号管理中重新登录。")
    try:
        cookies_root = cookies_dir.resolve(strict=True)
        cookies_root.relative_to(project)
        credential = cookies_root / f"{platform}_{alias}.json"
        if credential.is_symlink() or not credential.is_file():
            raise ValueError("missing or linked credential")
        safe_credential = credential.resolve(strict=True)
        safe_credential.relative_to(cookies_root)
        if safe_credential.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("credential too large")
        state = json.loads(safe_credential.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid storage state")
        if platform == "bilibili":
            from .bilibili_login import bilibili_identity

            cookie_info = state.get("cookie_info")
            bili_cookies = cookie_info.get("cookies") if isinstance(cookie_info, dict) else None
            if not bilibili_identity(state) or not isinstance(bili_cookies, list):
                raise ValueError("invalid bilibili credentials")
            raw_cookies = []
            for item in bili_cookies:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                    continue
                expires = item.get("expires", -1)
                try:
                    expires = float(expires)
                    if expires > 100_000_000_000:
                        expires /= 1000
                except (TypeError, ValueError):
                    expires = -1
                raw_cookies.append({
                    "name": item["name"], "value": item["value"], "domain": ".bilibili.com",
                    "path": "/", "expires": expires, "secure": True,
                    "httpOnly": item.get("http_only") in (True, 1),
                })
        else:
            raw_cookies = state.get("cookies")
            if not isinstance(raw_cookies, list):
                raise ValueError("invalid storage state")
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        raise PlatformAccountError("所选账号凭据不可用，请在账号管理中重新登录。") from None

    cookies = [
        {key: cookie[key] for key in ("name", "value", "domain", "path", "expires", "secure", "httpOnly") if key in cookie}
        for cookie in raw_cookies
        if isinstance(cookie, dict)
        and all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain"))
        and PlatformAccountManager._cookie_matches_platform(platform, cookie)
        and not PlatformAccountManager._cookie_is_expired(cookie)
    ]
    if not PlatformAccountManager._has_auth_cookie(platform, cookies):
        raise PlatformAccountError("所选账号登录状态已失效，请在账号管理中重新登录。")
    return account, cookies


def save_task_access_mode(
    database: Database,
    task_id: str,
    mode: str,
    account_id: str | None = None,
    account_name: str | None = None,
) -> None:
    if mode not in {"anonymous", "account"}:
        raise ValueError("平台访问方式无效。")
    if mode == "anonymous":
        account_id = None
        account_name = None
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO platform_access_options (task_id, mode, account_id, account_name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET mode = excluded.mode, account_id = excluded.account_id, account_name = excluded.account_name",
            (task_id, mode, account_id, account_name or ""),
        )


def get_task_access_mode(database: Database, task_id: str) -> str:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT mode FROM platform_access_options WHERE task_id = ?", (task_id,)
        ).fetchone()
    return str(row["mode"]) if row and row["mode"] in {"anonymous", "account"} else "anonymous"


def get_task_access_account(database: Database, task_id: str) -> tuple[str | None, str | None]:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT account_id, account_name FROM platform_access_options WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None, None
    account_id = str(row["account_id"]) if row["account_id"] else None
    account_name = str(row["account_name"]) if row["account_name"] else None
    return account_id, account_name


_DEFAULT_MANAGER = PlatformAccountManager()


def get_platform_account_manager() -> PlatformAccountManager:
    return _DEFAULT_MANAGER


__all__ = [
    "PLATFORM_ACCOUNTS",
    "PlatformAccountError",
    "PlatformAccountManager",
    "PlatformAccountSelectionRequired",
    "get_platform_account_manager",
    "get_task_access_account",
    "get_task_access_mode",
    "resolve_video_publish_account",
    "save_task_access_mode",
]
