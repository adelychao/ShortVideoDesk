"""Read video-channel works using the existing creator-center session."""
from __future__ import annotations

import re
from datetime import datetime

import requests


def fetch_status(cookies: dict[str, str], target: dict[str, object]) -> dict[str, object]:
    unknown = {"status": "unknown", "message": "无法唯一核实视频号作品，请稍后重试。"}
    try:
        started = int(datetime.fromisoformat(str(target.get("schedule_at") or target["started_at"])).timestamp())
        title = " ".join(str(target["effective_title"]).split())
        description = " ".join(str(target.get("effective_description") or "").split())
        if not title or not cookies:
            return unknown
        works: list[dict] = []
        count = None
        for page in range(1, 11):
            response = requests.post(
                "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_list",
                json={"pageSize": 50, "currentPage": page}, cookies=cookies,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://channels.weixin.qq.com/platform/post/list"},
                timeout=10, allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            current_count = data["totalCount"]
            entries = data["list"]
            if payload.get("errCode") != 0 or type(current_count) is not int or current_count < 0 or current_count > 500 or not isinstance(entries, list):
                return unknown
            if count is not None and current_count != count:
                return unknown
            count = current_count
            if len(entries) != min(50, max(0, count - len(works))):
                return unknown
            for item in entries:
                if not isinstance(item, dict) or not isinstance(item.get("desc"), dict):
                    return unknown
                item_description = item["desc"].get("description")
                if not isinstance(item_description, str) or type(item.get("createTime")) is not int or not re.fullmatch(r"[A-Za-z0-9_/-]{5,128}", str(item.get("objectId") or "")):
                    return unknown
                works.append(item)
            if len(works) == count:
                break
        else:
            return unknown
        remote_id = target.get("remote_id")
        def matches(item: dict) -> bool:
            text = " ".join(item["desc"]["description"].split())
            return (item["objectId"] == remote_id if remote_id else
                    (text == title or text.startswith(title + " ")) and (not description or description in text)
                    and started - 30 <= item["createTime"] <= started + 7200)
        candidates = [item for item in works if matches(item)]
        if len(candidates) != 1:
            return unknown
        item = candidates[0]
        if item["desc"].get("mediaType") != 4:
            return unknown
        disabled = item.get("disableInfo")
        if item.get("status") == 1 and item.get("visibleType") == 1 and isinstance(disabled, dict) and disabled.get("isDisabled") is False:
            status, message = "published", "视频号作品列表显示作品已发表。"
        else:
            status, message = "present", "已找到视频号作品，公开状态待确认。"
        return {"status": status, "remote_id": item["objectId"], "message": message}
    except (KeyError, TypeError, ValueError, AttributeError, requests.RequestException):
        return unknown
