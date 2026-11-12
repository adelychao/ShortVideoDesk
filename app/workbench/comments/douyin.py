"""Public anonymous Douyin comments from a fresh, non-persistent browser context."""

from __future__ import annotations

import re
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ..models import CommentPage, SourceRecord
from ...browser_safety import BrowserSafetyError, require_safe_browser
from .base import CommentAdapterError, normalize_record


_VIDEO_ID = re.compile(r"^[0-9]{1,30}$")
# 抖音公开页会将评论接口调度到 www-hj 子域；继续核对 aweme_id 防止串作品。zhw; 2026-10-24 20:07:37
_COMMENT_HOSTS = {"www.douyin.com", "douyin.com", "www-hj.douyin.com"}
_REQUEST_TIMEOUT_MS = 12_000
_REPLY_MAX_ATTEMPTS = 2
_REPLY_RETRY_DELAY_MS = 750
_VERIFY_TEXT = ("请完成下列验证", "请完成验证", "请拖动滑块", "verify you are human", "complete the verification", "complete the captcha")
_PAGE_SCRIPT = """async ({endpoint, params, base_url, timeout_ms = 12000}) => {
  const url = new URL(endpoint, base_url || location.origin);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  try {
    const response = await fetch(url, {
      credentials: 'include',
      headers: {'Accept': 'application/json'},
      signal: controller.signal,
    });
    const text = await response.text();
    if (text.length > 5242880) throw new Error('response_too_large');
    return {status: response.status, text};
  } finally {
    clearTimeout(timer);
  }
}"""
_START_PAGE_REQUEST = """(args) => {
  window.__workbenchCommentRequest = {state: 'pending'};
  const run = """ + _PAGE_SCRIPT + """;
  run(args).then(
    response => window.__workbenchCommentRequest = {state: 'done', response},
    error => window.__workbenchCommentRequest = {
      state: 'error',
      name: error && error.message === 'response_too_large'
        ? 'ResponseTooLargeError'
        : error && typeof error.name === 'string' ? error.name : 'NetworkError',
    }
  );
}"""


class DouyinCommentAdapter:
    def __init__(
        self,
        *,
        cookies: list[dict[str, object]] | None = None,
        authenticated: bool = False,
    ) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initial: dict[str, object] | None = None
        self._reply_warning: str | None = None
        self._api_origin: str | None = None
        self._progress_callback: Callable[[str], None] | None = None
        self._account_cookies = cookies or []
        self._authenticated = authenticated

    def set_progress_callback(self, callback: Callable[[str], None]) -> None:
        self._progress_callback = callback

    def _report_progress(self, message: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(message)

    def close(self) -> None:
        for resource in (self._context, self._browser, self._playwright):
            if resource is None:
                continue
            try:
                if resource is self._playwright:
                    resource.stop()
                else:
                    resource.close()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
        self._api_origin = None

    def _ensure_browser(self, source: SourceRecord) -> None:
        if self._page is not None:
            return
        media_id = source.source_id or ""
        if not _VIDEO_ID.fullmatch(media_id):
            raise CommentAdapterError("comments_source_id_missing", "评论采集需要完整的抖音作品链接。")
        try:
            require_safe_browser("douyin comments")
        except BrowserSafetyError as exc:
            raise CommentAdapterError("browser_safety_blocked", str(exc), retryable=False) from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(channel="chrome", headless=True, timeout=20_000)
            self._context = self._browser.new_context(locale="zh-CN", accept_downloads=False)
            if self._account_cookies:
                self._context.add_cookies(self._account_cookies)
            self._page = self._context.new_page()
        except PlaywrightError as exc:
            self.close()
            raise CommentAdapterError("comments_browser_start_failed", "无法启动匿名浏览器；请检查 Chrome 和本机浏览器环境。", retryable=True) from exc

        try:
            def on_response(response) -> None:
                if self._initial is not None or response.status != 200:
                    return
                parsed = urlsplit(response.url)
                if parsed.hostname not in _COMMENT_HOSTS or parsed.path != "/aweme/v1/web/comment/list/":
                    return
                if parse_qs(parsed.query).get("aweme_id") != [media_id]:
                    return
                self._api_origin = f"{parsed.scheme}://{parsed.netloc}"
                try:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("status_code") == 0:
                        self._initial = payload
                except Exception:
                    return

            self._page.on("response", on_response)
            navigation_timed_out = False
            try:
                self._page.goto(f"https://www.douyin.com/video/{media_id}", wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeoutError:
                navigation_timed_out = True
                if not self._is_expected_page(media_id):
                    raise CommentAdapterError("comments_navigation_timeout", "抖音作品页面加载超时，未能确认当前页面；请稍后重试。", retryable=True) from None
            deadline = time.monotonic() + 18
            while self._initial is None and time.monotonic() < deadline:
                self._page.wait_for_timeout(250)
            if navigation_timed_out and (self._initial is None or not self._is_expected_page(media_id)):
                raise CommentAdapterError("comments_navigation_timeout", "抖音作品页面加载超时，未取得可确认的评论；请稍后重试。", retryable=True)
            if self._initial is None:
                message = "抖音登录会话未取得可访问的评论；请检查账号状态或稍后重试。" if self._authenticated else "抖音匿名页面没有返回可访问的评论。"
                raise CommentAdapterError("comments_access_limited", message, retryable=True)
        except CommentAdapterError:
            self.close()
            raise
        except PlaywrightError as exc:
            self.close()
            raise CommentAdapterError("comments_navigation_failed", "匿名浏览器打开抖音作品页面时连接中断；请检查网络后重试。", retryable=True) from exc

    def _is_expected_page(self, media_id: str) -> bool:
        try:
            if self._page is None or self._page.is_closed():
                return False
            parsed = urlsplit(self._page.url)
            if (parsed.scheme != "https" or parsed.hostname not in {"www.douyin.com", "douyin.com"}
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.path.rstrip("/") != f"/video/{media_id}"):
                return False
            modal_ids = parse_qs(parsed.query).get("modal_id")
            if modal_ids is not None and modal_ids != [media_id]:
                return False
            visible_text = self._page.locator("body").inner_text(timeout=1500).casefold()
            return not any(marker in visible_text for marker in _VERIFY_TEXT)
        except (PlaywrightError, ValueError):
            return False

    @staticmethod
    def _records(payload: dict[str, object], *, parent_id: str | None = None) -> list:
        entries = payload.get("comments")
        result = []
        if not isinstance(entries, list):
            return result
        for item in entries:
            if not isinstance(item, dict):
                continue
            user = item.get("user")
            if not isinstance(user, dict):
                user = {}
            reply_id = item.get("reply_id")
            record = normalize_record(
                comment_id=item.get("cid"),
                text=item.get("text"),
                author=user.get("nickname"),
                like_count=item.get("digg_count"),
                timestamp=item.get("create_time"),
                parent_id=parent_id or (reply_id if reply_id not in (None, "", 0, "0") else None),
                reply_count=item.get("reply_comment_total"),
            )
            if record is not None:
                result.append(record)
        return result

    def _request(self, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        try:
            # 作品页的请求机制会处理评论分页；空白页收到的响应可能为空。zhw; 2026-10-28 09:15:52
            page = self._page
            page.evaluate(
                _START_PAGE_REQUEST,
                {
                    "endpoint": endpoint,
                    "params": params,
                    "base_url": self._api_origin,
                    "timeout_ms": _REQUEST_TIMEOUT_MS,
                },
            )
            deadline = time.monotonic() + (_REQUEST_TIMEOUT_MS / 1000) + 1
            state = None
            while time.monotonic() < deadline:
                state = page.evaluate("() => window.__workbenchCommentRequest")
                if isinstance(state, dict) and state.get("state") != "pending":
                    break
                page.wait_for_timeout(100)
            response = state.get("response") if isinstance(state, dict) and state.get("state") == "done" else None
        except PlaywrightTimeoutError as exc:
            raise CommentAdapterError("comments_request_timeout", "抖音评论请求等待超时。", retryable=True) from exc
        except PlaywrightError as exc:
            raise CommentAdapterError("comments_network_error", "抖音评论页请求中断。", retryable=True) from exc
        if isinstance(state, dict) and state.get("state") == "error":
            if state.get("name") == "AbortError":
                code, message = "comments_request_timeout", "抖音评论请求超时。"
            elif state.get("name") == "ResponseTooLargeError":
                code, message = "comments_invalid_response", "抖音评论响应超过本地安全大小限制。"
            else:
                code, message = "comments_network_error", "抖音评论请求网络中断。"
            raise CommentAdapterError(code, message, retryable=True)
        if isinstance(state, dict) and state.get("state") == "pending":
            raise CommentAdapterError("comments_request_timeout", "抖音评论请求等待超时。", retryable=True)
        if not isinstance(response, dict):
            raise CommentAdapterError("comments_no_response", "抖音评论请求未收到有效响应；可能超时或网络中断。", retryable=True)
        status = response.get("status")
        if status in (401, 403, 429):
            message = "抖音拒绝了当前登录会话的评论请求。" if self._authenticated else "抖音拒绝了匿名评论请求。"
            raise CommentAdapterError("comments_access_limited", message, retryable=True)
        if isinstance(status, int) and (status in (408, 425) or 500 <= status < 600):
            raise CommentAdapterError("comments_network_error", "抖音评论服务暂时不可用。", retryable=True)
        if status != 200 or not isinstance(response.get("text"), str):
            raise CommentAdapterError("comments_invalid_response", "抖音评论请求未返回有效响应。", retryable=True)
        import json
        try:
            payload = json.loads(response["text"])
        except (TypeError, ValueError) as exc:
            raise CommentAdapterError("comments_invalid_response", "抖音没有返回可读取的评论分页。", retryable=True) from exc
        if not isinstance(payload, dict) or payload.get("status_code") != 0:
            raise CommentAdapterError("comments_invalid_response", "抖音评论分页不可访问；已保留此前采集内容。", retryable=True)
        return payload

    def _reply_records(self, media_id: str, root: dict[str, object]) -> list:
        root_id = root.get("cid")
        reply_count = root.get("reply_comment_total")
        if not isinstance(root_id, (str, int)) or not isinstance(reply_count, int) or reply_count <= 0:
            return []
        cursor: object = 0
        collected = []
        visited_cursors = {str(cursor)}
        reply_pages = 0
        endpoint = "/aweme/v1/web/comment/list/reply/"
        while True:
            params = {
                "device_platform": "webapp", "aid": 6383, "aweme_id": media_id,
                "item_id": media_id, "comment_id": root_id, "cursor": cursor,
                "count": 20, "item_type": 0,
            }
            last_error = None
            payload = None
            for attempt in range(_REPLY_MAX_ATTEMPTS):
                self._report_progress(f"正在采集评论楼中楼回复，第 {reply_pages + 1} 页")
                try:
                    payload = self._request(endpoint, params)
                    last_error = None
                    break
                except CommentAdapterError as exc:
                    last_error = exc
                    transient = exc.code in {
                        "comments_no_response", "comments_network_error", "comments_request_timeout",
                    }
                    if not transient or attempt + 1 >= _REPLY_MAX_ATTEMPTS:
                        break
                    self._report_progress("楼中楼回复请求暂时无响应，正在重试一次")
                    self._page.wait_for_timeout(_REPLY_RETRY_DELAY_MS)
            if last_error is not None:
                exc = last_error
                if exc.code == "comments_access_limited":
                    reason = "平台拒绝了当前登录会话的回复请求" if self._authenticated else "平台拒绝了匿名回复请求"
                elif exc.code == "comments_request_timeout":
                    reason = "回复请求超时（已自动重试一次）"
                elif exc.code in {"comments_no_response", "comments_network_error"}:
                    reason = "回复请求连续无响应或网络中断（已自动重试一次）"
                else:
                    reason = "回复请求未返回可读取的内容"
                if self._reply_warning is None:
                    self._reply_warning = f"部分楼中楼未获取：{reason}；已保留可访问的主评论和此前采集内容"
                elif reason not in self._reply_warning:
                    self._reply_warning += f"；{reason}"
                break
            if not isinstance(payload, dict):
                break
            reply_pages += 1
            collected.extend(self._records(payload, parent_id=str(root_id)))
            if payload.get("has_more") != 1:
                break
            next_cursor = payload.get("cursor")
            if next_cursor in (None, cursor, ""):
                break
            next_cursor = str(next_cursor)
            if next_cursor in visited_cursors:
                self._reply_warning = "抖音楼中楼分页游标重复，已停止继续请求；已保留此前采集内容"
                break
            visited_cursors.add(next_cursor)
            cursor = next_cursor
            self._page.wait_for_timeout(250)
        return collected

    def fetch_page(self, source: SourceRecord, cursor: str | None):
        self._ensure_browser(source)
        media_id = source.source_id or ""
        if cursor is None and self._initial is not None:
            payload = self._initial
            self._initial = None
        else:
            cursor_value = cursor or "0"
            if not _VIDEO_ID.fullmatch(cursor_value):
                raise CommentAdapterError("comments_cursor_invalid", "抖音评论分页状态无效。")
            payload = self._request("/aweme/v1/web/comment/list/", {
                "device_platform": "webapp", "aid": 6383, "aweme_id": media_id,
                "cursor": cursor_value, "count": 20, "item_type": 0,
            })
        comments = self._records(payload)
        has_more = payload.get("has_more")
        next_cursor = payload.get("cursor")
        if has_more == 1 and isinstance(next_cursor, (str, int)) and str(next_cursor) != str(cursor or "0"):
            return CommentPage(
                comments, str(next_cursor), None, True, reply_warning=self._reply_warning
            )
        terminal_reason = (
            "平台已返回末页" if has_more == 0 else "平台未提供可继续的匿名评论分页"
        )
        return CommentPage(
            comments,
            None,
            terminal_reason,
            False,
            reply_warning=self._reply_warning,
        )
