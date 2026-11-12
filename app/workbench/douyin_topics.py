"""Read public topic suggestions shown by the Douyin creator publishing page."""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_TOPIC_SUGGESTIONS_URL = "https://creator.douyin.com/aweme/v1/search/challengesug/"
_MAX_RESPONSE_BYTES = 1_000_000


class DouyinTopicSearchError(RuntimeError):
    """A bounded, user-safe topic lookup failure."""


def _format_view_count(value: int) -> str:
    if value >= 100_000_000:
        amount = (Decimal(value) / Decimal(100_000_000)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{amount.normalize()}亿次浏览"
    if value >= 10_000:
        amount = (Decimal(value) / Decimal(10_000)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{amount.normalize()}万次浏览"
    return f"{value}次浏览"


def search_douyin_topics(keyword: str) -> list[dict[str, str]]:
    keyword = keyword.strip().lstrip("#").strip()
    if not keyword or len(keyword) > 50:
        raise ValueError("请输入 1 到 50 个字符的话题关键词。")

    query = urlencode({"source": "challenge_create", "aid": "2906", "keyword": keyword})
    request = Request(
        f"{_TOPIC_SUGGESTIONS_URL}?{query}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://creator.douyin.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise DouyinTopicSearchError("抖音返回的话题数据过大，请稍后重试。")
        payload = json.loads(raw)
    except DouyinTopicSearchError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DouyinTopicSearchError("抖音话题暂时无法查询，请稍后重试。") from None

    if (
        not isinstance(payload, dict)
        or type(payload.get("status_code")) is not int
        or payload.get("status_code") != 0
        or not isinstance(payload.get("sug_list"), list)
    ):
        raise DouyinTopicSearchError("抖音暂未返回可识别的话题数据，请稍后重试。")

    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in payload["sug_list"][:30]:
        if not isinstance(item, dict):
            continue
        name = item.get("cha_name")
        cid = item.get("cid")
        count = item.get("view_count")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 100:
            continue
        if not isinstance(cid, (str, int)) or isinstance(cid, bool):
            continue
        topic_id = str(cid)
        if not topic_id or len(topic_id) > 128 or topic_id in seen_ids:
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or len(str(count)) > 24:
            continue
        view_count = count
        seen_ids.add(topic_id)
        items.append({
            "id": topic_id,
            "name": name.strip(),
            "view_count": str(view_count),
            "view_count_text": _format_view_count(view_count),
        })
    return items
