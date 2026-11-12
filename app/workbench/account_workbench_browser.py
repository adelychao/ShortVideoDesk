"""Open fixed social creator workbenches in account-scoped browser contexts."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..browser_safety import require_safe_browser

PLATFORM_WORKBENCH_URLS: dict[str, str] = {
    "douyin": "https://creator.douyin.com/",
    "xiaohongshu": "https://creator.xiaohongshu.com/",
    "kuaishou": "https://cp.kuaishou.com/",
    "bilibili": "https://member.bilibili.com/platform/home",
    "wechat_channels": "https://channels.weixin.qq.com/platform",
    "baijiahao": "https://baijiahao.baidu.com/builder/rc/home",
    "zhihu": "https://www.zhihu.com/creator",
}
DOUYIN_WORKBENCH_URL = PLATFORM_WORKBENCH_URLS["douyin"]
_BROWSER_OPEN_TIMEOUT_SECONDS = 45
_BROWSER_CLOSE_TIMEOUT_SECONDS = 5
_BROWSER_ERROR_MESSAGE = "平台创作工作台暂时无法打开，请稍后重试。"


@dataclass
class _Command:
    name: str
    done: threading.Event = field(default_factory=threading.Event)
    error: bool = False


@dataclass
class _BrowserSession:
    account_id: str
    platform_id: str
    storage_state: dict[str, Any]
    commands: queue.Queue[_Command] = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    exited: threading.Event = field(default_factory=threading.Event)
    startup_error: bool = False
    thread: threading.Thread | None = None


class AccountWorkbenchBrowserManager:
    """Maintain one visible, resizable Playwright context per publishing account."""

    def __init__(self, playwright_factory: Callable[[], Any] | None = None) -> None:
        self._playwright_factory = playwright_factory
        self._lock = threading.Lock()
        self._sessions: dict[str, _BrowserSession] = {}

    def open(self, account_id: str, platform_id: str, storage_state: dict[str, Any]) -> None:
        require_safe_browser("open platform workbench")
        if platform_id not in PLATFORM_WORKBENCH_URLS:
            raise RuntimeError(_BROWSER_ERROR_MESSAGE)
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and session.platform_id != platform_id:
                raise RuntimeError(_BROWSER_ERROR_MESSAGE)
            if session is None or session.exited.is_set():
                session = _BrowserSession(account_id, platform_id, storage_state)
                session.thread = threading.Thread(
                    target=self._run_session,
                    args=(session,),
                    name=f"account-workbench-{platform_id}-{account_id}",
                    daemon=True,
                )
                self._sessions[account_id] = session
                session.thread.start()

        if not session.ready.wait(_BROWSER_OPEN_TIMEOUT_SECONDS):
            session.commands.put(_Command("close"))
            if session.thread is not None:
                session.thread.join(timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS)
            self._forget(session)
            raise RuntimeError(_BROWSER_ERROR_MESSAGE)
        if session.startup_error:
            self._forget(session)
            raise RuntimeError(_BROWSER_ERROR_MESSAGE)

        command = _Command("open_page")
        session.commands.put(command)
        if not command.done.wait(_BROWSER_OPEN_TIMEOUT_SECONDS) or command.error:
            raise RuntimeError(_BROWSER_ERROR_MESSAGE)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        commands: list[tuple[_BrowserSession, _Command]] = []
        for session in sessions:
            if session.exited.is_set():
                continue
            command = _Command("close")
            session.commands.put(command)
            commands.append((session, command))
        for session, command in commands:
            command.done.wait(_BROWSER_CLOSE_TIMEOUT_SECONDS)
            if session.thread is not None:
                session.thread.join(timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS)
        with self._lock:
            self._sessions.clear()

    def _forget(self, session: _BrowserSession) -> None:
        with self._lock:
            if self._sessions.get(session.account_id) is session:
                self._sessions.pop(session.account_id, None)

    def _run_session(self, session: _BrowserSession) -> None:
        playwright = None
        browser = None
        context = None
        try:
            if self._playwright_factory is None:
                from playwright.sync_api import sync_playwright

                playwright = sync_playwright().start()
            else:
                playwright = self._playwright_factory().start()
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(storage_state=session.storage_state, no_viewport=True)
            session.ready.set()
            had_page = False

            while True:
                try:
                    command = session.commands.get(timeout=0.5)
                except queue.Empty:
                    if not browser.is_connected() or (had_page and not context.pages):
                        return
                    continue
                if command.name == "close":
                    command.done.set()
                    return
                try:
                    page = context.new_page()
                    page.goto(
                        PLATFORM_WORKBENCH_URLS[session.platform_id],
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    had_page = True
                except Exception:
                    command.error = True
                finally:
                    command.done.set()
        except Exception:
            session.startup_error = True
            session.ready.set()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            session.exited.set()
            self._forget(session)
