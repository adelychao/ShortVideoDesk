"""文章工作台已实现的内容形态与发布能力。"""

from copy import deepcopy
from typing import Any


_CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    "wechat": {"article": {"publish": "official_api", "draft": True, "status_query": True}},
    "csdn": {"article": {"publish": "browser", "draft": False, "status_query": False}},
    "zhihu": {"article": {"publish": "official_api_or_browser", "draft": False, "status_query": False}},
    "xiaohongshu": {"image_note": {"publish": "manual", "draft": False, "status_query": False}},
    "toutiao": {"article": {"publish": "browser", "draft": False, "status_query": False}},
    "baijiahao": {"article": {"publish": "manual", "draft": False, "status_query": False}},
    "weibo": {"article": {"publish": "official_cli", "draft": False, "status_query": False}},
}


def article_platform_capabilities(platform_id: str) -> dict[str, dict[str, Any]]:
    return deepcopy(_CAPABILITIES.get(platform_id, {}))
