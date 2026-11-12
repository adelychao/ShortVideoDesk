"""Public anonymous Bilibili reply adapter using the documented web APIs."""

from __future__ import annotations

import json
import re

from yt_dlp.dependencies import requests

from ..models import CommentPage, SourceRecord
from .base import CommentAdapterError, normalize_record


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36"


class BilibiliCommentAdapter:
    def __init__(
        self,
        *,
        cookies: list[dict[str, object]] | None = None,
        authenticated: bool = False,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT, "Referer": "https://www.bilibili.com/"})
        self._authenticated = authenticated
        for cookie in cookies or []:
            name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
            if not all(isinstance(item, str) and item for item in (name, value, domain)):
                continue
            path = cookie.get("path") if isinstance(cookie.get("path"), str) else "/"
            self.session.cookies.set(
                name,
                value,
                domain=domain,
                path=path,
                secure=cookie.get("secure") is True,
                expires=cookie.get("expires") if isinstance(cookie.get("expires"), int) and cookie.get("expires") > 0 else None,
            )
        self._aid: int | None = None
        self._bvid: str | None = None
        self._reply_warning: str | None = None

    def close(self) -> None:
        self.session.close()

    def _json(self, url: str, params: dict[str, object]) -> dict[str, object]:
        try:
            response = self.session.get(url, params=params, timeout=20)
        except requests.RequestException as exc:
            raise CommentAdapterError("comments_network_error", "B 站评论请求未完成，请稍后重试。", retryable=True) from exc
        if response.status_code in {401, 403, 412, 429}:
            message = "B 站限制了当前登录会话的评论访问；已保留此前采集内容。" if self._authenticated else "B 站限制了匿名评论访问；已保留此前采集内容。"
            raise CommentAdapterError("comments_access_limited", message, retryable=True)
        if response.status_code != 200 or len(response.content) > 5 * 1024 * 1024:
            raise CommentAdapterError("comments_response_changed", "B 站评论接口返回了无法处理的响应。", retryable=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CommentAdapterError("comments_response_changed", "B 站评论响应格式无法识别。", retryable=True) from exc
        if not isinstance(payload, dict):
            raise CommentAdapterError("comments_response_changed", "B 站评论响应格式无法识别。", retryable=True)
        if payload.get("code") != 0:
            raise CommentAdapterError("comments_access_limited", "B 站未公开提供本条评论页，可能存在访问限制。", retryable=True)
        return payload

    def _resolve_aid(self, source: SourceRecord) -> int:
        source_id = source.source_id or ""
        if self._aid is not None and self._bvid == source_id:
            return self._aid
        params: dict[str, object]
        if re.fullmatch(r"BV[0-9A-Za-z]{10}", source_id):
            params = {"bvid": source_id}
        elif re.fullmatch(r"[0-9]{1,20}", source_id):
            params = {"aid": int(source_id)}
        else:
            raise CommentAdapterError("comments_source_id_missing", "评论采集需要完整的 B 站 BV/AV 作品链接。")
        payload = self._json("https://api.bilibili.com/x/web-interface/view", params)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CommentAdapterError("comments_source_unavailable", "B 站没有返回可验证的作品信息。", retryable=True)
        aid = data.get("aid")
        bvid = data.get("bvid")
        if not isinstance(aid, int) or (source_id.startswith("BV") and bvid != source_id):
            raise CommentAdapterError("comments_source_mismatch", "B 站作品身份校验未通过。")
        self._aid = aid
        self._bvid = source_id
        return aid

    @staticmethod
    def _records(entries: object, *, root_id: str | None = None) -> list:
        result = []
        if not isinstance(entries, list):
            return result
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            member = entry.get("member")
            if not isinstance(content, dict):
                content = {}
            if not isinstance(member, dict):
                member = {}
            parent = entry.get("parent") or root_id
            record = normalize_record(
                comment_id=entry.get("rpid"),
                text=content.get("message"),
                author=member.get("uname"),
                like_count=entry.get("like"),
                timestamp=entry.get("ctime"),
                parent_id=parent,
                root_id=entry.get("root") or root_id,
                reply_count=entry.get("rcount"),
            )
            if record is not None:
                result.append(record)
        return result

    def _replies(self, aid: int, root: dict[str, object]) -> list:
        count = root.get("rcount")
        root_id = root.get("rpid")
        if not isinstance(count, int) or count <= 0 or not isinstance(root_id, int):
            return []
        children = []
        page_no = 1
        while True:
            try:
                payload = self._json(
                    "https://api.bilibili.com/x/v2/reply/reply",
                    {"type": 1, "oid": aid, "root": root_id, "pn": page_no, "ps": 20},
                )
            except CommentAdapterError:
                self._reply_warning = "部分楼中楼受到平台限制；已保留可访问的主评论和此前采集内容"
                break
            data = payload.get("data")
            if not isinstance(data, dict):
                break
            children.extend(self._records(data.get("replies"), root_id=str(root_id)))
            cursor = data.get("cursor")
            if isinstance(cursor, dict) and cursor.get("is_end") is True:
                break
            if not data.get("replies"):
                break
            page_no += 1
        return children

    def fetch_page(self, source: SourceRecord, cursor: str | None):
        aid = self._resolve_aid(source)
        page_no = 1
        if cursor:
            try:
                state = json.loads(cursor)
                page_no = int(state["page"])
                if state.get("aid") != aid or not 1 <= page_no <= 1_000_000:
                    raise ValueError
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise CommentAdapterError("comments_cursor_invalid", "B 站评论分页状态无效。") from exc
        payload = self._json(
            "https://api.bilibili.com/x/v2/reply",
            {"type": 1, "oid": aid, "pn": page_no, "ps": 20, "sort": 0},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CommentAdapterError("comments_response_changed", "B 站评论分页结构发生变化。", retryable=True)
        roots = data.get("replies")
        if not isinstance(roots, list):
            roots = []
        comments = self._records(roots)
        cursor_data = data.get("cursor")
        is_end = cursor_data.get("is_end") if isinstance(cursor_data, dict) else None
        if is_end is True or not roots:
            return CommentPage(
                comments, None, "平台已返回末页", False, reply_warning=self._reply_warning
            )
        if is_end is False:
            next_cursor = json.dumps({"aid": aid, "page": page_no + 1}, separators=(",", ":"))
            return CommentPage(comments, next_cursor, None, True, reply_warning=self._reply_warning)
        page_data = data.get("page")
        page_count = page_data.get("count") if isinstance(page_data, dict) else None
        if isinstance(page_count, int) and page_no * 20 >= page_count:
            return CommentPage(
                comments, None, "平台已返回末页", False, reply_warning=self._reply_warning
            )
        if roots:
            return CommentPage(
                comments,
                json.dumps({"aid": aid, "page": page_no + 1}, separators=(",", ":")),
                None,
                True,
                reply_warning=self._reply_warning,
            )
        return CommentPage(
            comments, None, "平台没有返回更多评论", False, reply_warning=self._reply_warning
        )
