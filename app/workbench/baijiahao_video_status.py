"""Read Baijiahao creator-center content status without submitting content."""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests


def fetch_status(cookies: dict[str, str], target: dict[str, object]) -> dict[str, object]:
    unknown = {"status": "unknown", "message": "无法唯一核实百家号作品，请稍后重试。"}
    try:
        started = int(datetime.fromisoformat(str(target["started_at"])).timestamp())
        title = str(target["effective_title"]).strip()
        if not title or not cookies:
            return unknown
        works: list[dict] = []
        count = None
        for page in range(1, 11):
            response = requests.get(
                "https://baijiahao.baidu.com/pcui/article/lists",
                params={"currentPage": page, "pageSize": 50, "type": "", "collection": "publish"},
                cookies=cookies, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://baijiahao.baidu.com/builder/rc/content"},
                timeout=15, allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            current_count = data["page"]["totalCount"]
            entries = data["list"]
            if payload.get("errno") != 0 or type(current_count) is not int or current_count < 0 or current_count > 500 or not isinstance(entries, list):
                return unknown
            if count is not None and current_count != count:
                return unknown
            count = current_count
            if len(entries) != min(50, max(0, count - len(works))) or not all(isinstance(item, dict) for item in entries):
                return unknown
            works.extend(entries)
            if len(works) == count:
                break
        else:
            return unknown
        candidates = []
        remote_id = target.get("remote_id")
        for item in works:
            if item.get("type") not in {"video", "ugc_video"}:
                continue
            work_id = item.get("article_id")
            item_title = item.get("title")
            created = item.get("first_storage_time")
            if not isinstance(work_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", work_id) or not isinstance(item_title, str) or type(created) is not int:
                return unknown
            if (work_id == remote_id if remote_id else item_title.strip() == title and started - 30 <= created <= started + 7200):
                candidates.append(item)
        if len(candidates) != 1:
            return unknown
        item = candidates[0]
        visible = item.get("visible_status")
        timer = item.get("timer_time", 0)
        if (item.get("status") == "publish" and item.get("is_published") == 1 and item.get("issue_status") == 1
                and isinstance(visible, dict) and visible.get("can_set_self_visible") == 1 and visible.get("can_set_public") == -1
                and type(timer) is int and timer <= time.time()):
            status, message = "published", "百家号内容管理显示作品已发布。"
        else:
            status, message = "present", "已找到百家号作品，公开状态待确认。"
        return {"status": status, "remote_id": item["article_id"], "message": message}
    except (KeyError, TypeError, ValueError, AttributeError, requests.RequestException):
        return unknown
