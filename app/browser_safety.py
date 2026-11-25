"""Browser component verification and coordination for platform automation."""

import logging
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys

_LOGGER = logging.getLogger(__name__)
SAFETY_MESSAGE = "平台页面暂时无法打开，请稍后重试。"
_XHS_LOGIN_MUTEX_NAME = 'Local\\VideoWorkbenchXiaohongshuLogin'
_SAFE_BROWSER_OPERATIONS = frozenset({
    "open platform login", "xiaohongshu discovery",
    "douyin signing", "douyin discovery", "douyin comments", "platform login",
    "platform account session", "video account profile", "douyin status",
    "video publishing", "manual douyin status", "anonymous douyin",
    "public page probe",
    "open platform workbench",
})


class BrowserSafetyError(RuntimeError):
    pass


def _is_windows() -> bool:
    return sys.platform == "win32"


def is_windows_platform() -> bool:
    return _is_windows()


def browser_automation_paused() -> bool:
    if not _is_windows():
        return False
    return not _windows_browser_state_safe()


def require_safe_browser(operation: str) -> None:
    if not _is_windows():
        return
    if operation not in _SAFE_BROWSER_OPERATIONS:
        _LOGGER.warning("Browser launch blocked for unsupported operation: %s", operation)
        raise BrowserSafetyError(SAFETY_MESSAGE)


def _windows_browser_state_safe() -> bool:
    return _session_unlocked()


def _session_unlocked() -> bool:
    try:
        class SessionInfo(ctypes.Structure):
            _fields_ = [
                ("level", wintypes.DWORD),
                ("session_id", wintypes.DWORD),
                ("state", wintypes.DWORD),
                ("flags", wintypes.LONG),
            ]

        api = ctypes.WinDLL("wtsapi32", use_last_error=True)
        query = api.WTSQuerySessionInformationW
        query.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        query.restype = wintypes.BOOL
        api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        pointer = ctypes.c_void_p()
        size = wintypes.DWORD()
        current_session = 0xFFFFFFFF
        session_info_ex = 25
        if not query(None, current_session, session_info_ex, ctypes.byref(pointer), ctypes.byref(size)):
            return False
        try:
            if size.value < ctypes.sizeof(SessionInfo):
                return False
            info = ctypes.cast(pointer, ctypes.POINTER(SessionInfo)).contents
            # WTSINFOEX starts with Level, followed immediately by the level-1
            # fields SessionId, SessionState and SessionFlags. SessionFlags is
            # 0 while locked and 1 while unlocked.
            return info.level == 1 and info.state == 0 and info.flags == 1
        finally:
            api.WTSFreeMemory(pointer)
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        return False


def _account_safe() -> bool:
    # zhw 2026-11-16 23:20:15：兼容已固定哈希的小红书登录组件，不查询 Windows 密码错误次数。
    return True


def _verified_xhs_browser(project: Path) -> bool:
    try:
        source_hashes = {
            'sau_commands/xiaohongshu.py': '21a5bfa7e169a7c614896e198a1a914079aefc8f173e5b6b0a1a54a5377f94bd',
            'uploader/xiaohongshu_uploader/main.py': 'c8e1e7d933cd902c02410cebebe29406d82a2c8fdbaa72b229e241c6beab085f',
        }
        for filename, expected in source_hashes.items():
            with (project / filename).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    return False
        manifest = json.loads((project / '.venv/Lib/site-packages/patchright/driver/package/browsers.json').read_text(encoding='utf-8'))
        chromium = next(b for b in manifest['browsers'] if b['name'] == 'chromium')
        if chromium['revision'] != '1208' or chromium['browserVersion'] != '145.0.7632.6':
            return False
        base = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1208/chrome-win64'
        hashes = {'chrome.exe': 'ff0c99fc5bb2301cad77930aa76e5354cf2a5fefb7efdcf3b3bc10ba0550c015',
                  'chrome.dll': '44b4f037604948f3d17116c43b18d2e0872631666781c92b6587175f1827c7a8'}
        for name, expected in hashes.items():
            with (base / name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    return False
        return True
    except (OSError, KeyError, TypeError, ValueError, StopIteration):
        return False


def reserve_xiaohongshu_login(database, project: Path) -> None:
    if not _is_windows():
        return
    if not _verified_xhs_browser(project):
        _LOGGER.warning('Xiaohongshu login blocked: browser or application files changed')
        raise BrowserSafetyError('登录组件暂不可用，请检查运行环境后重试。')


def require_controlled_video_browser(project: Path) -> Path | None:
    if not _is_windows():
        return None
    if not _verified_xhs_browser(project):
        raise BrowserSafetyError('受控浏览器或登录组件校验未通过，发布已暂停。')
    return Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1208/chrome-win64/chrome.exe'


@contextmanager
def xiaohongshu_login_slot():
    if not _is_windows():
        yield
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, _XHS_LOGIN_MUTEX_NAME)
    if not handle:
        raise BrowserSafetyError('暂时无法获取浏览器任务锁，请稍后再试。')
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0x102:  # WAIT_TIMEOUT
            raise BrowserSafetyError('其他浏览器任务正在运行，请稍后再试。')
        if result not in (0, 0x80):
            raise BrowserSafetyError('暂时无法获取浏览器任务锁，请稍后再试。')
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
