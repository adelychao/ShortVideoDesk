"""读取抖音创作者中心的公共推荐热点。"""
from __future__ import annotations

import json
import threading
import time
from http.client import HTTPException
from urllib.request import Request, urlopen


_URL = "https://creator.douyin.com/aweme/v1/hotspot/recommend/?aid=2906"
_MAX_BYTES = 1_000_000
_FRESH_SECONDS = 60
_STALE_SECONDS = 180


class DouyinHotTopicsError(RuntimeError):
    pass


def _fetch_recommendations() -> list[dict]:
    request = Request(_URL, headers={
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://creator.douyin.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        ),
    })
    with urlopen(request, timeout=5) as response:
        raw = response.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("热点响应过大")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        raise ValueError("热点响应无效")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    source = data.get("all_sentences", data.get("rec_sentences"))
    if not isinstance(source, list):
        raise ValueError("缺少热点列表")
    items = []
    for row in source[:50]:
        if not isinstance(row, dict):
            continue
        try:
            count = max(0, int(row.get("hot_value", 0)))
            count_text = f"{count / 10000:.1f}万在看" if count else ""
        except (TypeError, ValueError, OverflowError):
            count, count_text = 0, ""
        cover = row.get("word_cover")
        urls = cover.get("url_list") if isinstance(cover, dict) else None
        items.append({
            "id": str(row.get("sentence_id") or ""),
            "title": row.get("word"),
            "view_count": count,
            "view_count_text": count_text,
            "thumbnail_url": urls[0] if isinstance(urls, list) and urls else None,
        })
    return items


class DouyinHotRecommendations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] | None = None
        self._updated_at = 0.0

    def read(self) -> list[dict]:
        with self._lock:
            age = time.monotonic() - self._updated_at
            if self._items is not None and age < _FRESH_SECONDS:
                return self._items
            try:
                self._items = _fetch_recommendations()
            except (OSError, ValueError, HTTPException, RecursionError) as exc:
                if self._items is not None and age < _STALE_SECONDS:
                    return self._items
                raise DouyinHotTopicsError("抖音热点列表暂不可用，请稍后重试。") from exc
            self._updated_at = time.monotonic()
            return self._items
