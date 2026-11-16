"""Read Bilibili creator-center categories without exposing account credentials."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests


def fetch_bilibili_publish_status(cookies: dict[str, str], target: dict[str, object]) -> dict[str, object]:
    unknown = {"status": "unknown", "message": "无法唯一核实 B 站稿件，请稍后重试。"}
    try:
        started = int(datetime.fromisoformat(str(target["started_at"])).timestamp())
        title = str(target["effective_title"]).strip()
        if not title or not cookies.get("SESSDATA"):
            return unknown
        archives: list[dict] = []
        count = None
        for page in range(1, 11):
            response = requests.get(
                "https://member.bilibili.com/x/web/archives",
                params={"status": "is_pubing,pubed,not_pubed", "pn": page, "ps": 50, "coop": 1},
                cookies=cookies,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://member.bilibili.com/"},
                timeout=10, allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            current_count = data["page"]["count"]
            items = data["arc_audits"]
            if payload.get("code") != 0 or type(current_count) is not int or current_count < 0 or current_count > 500 or not isinstance(items, list):
                return unknown
            if count is not None and current_count != count:
                return unknown
            count = current_count
            if len(items) != min(50, max(0, count - len(archives))):
                return unknown
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("Archive"), dict):
                    return unknown
                archive = item["Archive"]
                if not isinstance(archive.get("title"), str) or type(archive.get("ctime")) is not int or not re.fullmatch(r"BV[A-Za-z0-9]{10}", str(archive.get("bvid") or "")):
                    return unknown
                archives.append(archive)
            if len(archives) == count:
                break
        else:
            return unknown
        remote_id = target.get("remote_id")
        candidates = [item for item in archives if (item["bvid"] == remote_id if remote_id else
                      item["title"].strip() == title and started - 30 <= item["ctime"] <= started + 7200)]
        if len(candidates) != 1:
            return unknown
        item = candidates[0]
        description = item.get("state_desc")
        if item.get("is_only_self") == 1 or item.get("no_public") == 1:
            status, message = "private", "B 站稿件当前为非公开状态。"
        elif item.get("state") == 0 and description == "开放浏览" and item.get("is_only_self") == 0 and item.get("no_public") == 0:
            status, message = "published", "B 站稿件管理显示开放浏览。"
        elif isinstance(description, str) and ("审核中" in description or "待审核" in description):
            status, message = "reviewing", "B 站稿件审核中。"
        elif isinstance(description, str) and ("未通过" in description or "退回" in description):
            status, message = "rejected", "B 站稿件审核未通过，请到创作中心查看原因。"
        else:
            status, message = "present", "已找到 B 站稿件，具体发布状态待确认。"
        return {"status": status, "remote_id": item["bvid"], "message": message}
    except (OSError, KeyError, TypeError, ValueError, AttributeError, requests.RequestException):
        return unknown


def _cookies(account_file: Path) -> dict[str, str]:
    if account_file.is_symlink() or not account_file.is_file() or account_file.stat().st_size > 5 * 1024 * 1024:
        raise ValueError('B站登录状态不可用，请重新登录。')
    data = json.loads(account_file.read_text(encoding='utf-8'))
    cookie_list = data['cookie_info']['cookies']
    cookies = {item['name']: item['value'] for item in cookie_list if isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('value'), str)}
    if not cookies.get('SESSDATA'):
        raise ValueError('B站登录状态不可用，请重新登录。')
    return cookies


def fetch_bilibili_categories(account_file: Path) -> list[dict[str, object]]:
    try:
        cookies = _cookies(account_file)
        response = requests.get('https://member.bilibili.com/x/vupre/web/archive/human/type2/list', cookies=cookies, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://member.bilibili.com/'}, timeout=10, allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 0:
            raise ValueError('获取B站分区失败，请检查登录状态后重试。')
        items = result['data']['type_list']
        if not isinstance(items, list):
            raise ValueError('B站返回了无法识别的分区列表。')
        categories = [{'id': item['id'], 'name': item['name']} for item in items if isinstance(item, dict) and type(item.get('id')) is int and item['id'] > 0 and isinstance(item.get('name'), str) and item['name'].strip()]
        if not categories:
            raise ValueError('B站分区列表为空，请稍后重试。')
        return categories
    except (OSError, KeyError, TypeError, AttributeError, requests.RequestException, json.JSONDecodeError) as exc:
        raise ValueError('获取B站分区失败，请检查网络和登录状态后重试。') from exc


def fetch_bilibili_statements(account_file: Path) -> list[dict[str, object]]:
    try:
        response = requests.get('https://member.bilibili.com/x/vupre/web/archive/pre', cookies=_cookies(account_file), headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://member.bilibili.com/'}, timeout=10, allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 0:
            raise ValueError('获取B站创作声明失败，请检查登录状态后重试。')
        items = result['data']['neutral_mark']['mark_list']
        if not isinstance(items, list):
            raise ValueError('B站返回了无法识别的创作声明列表。')
        statements = [{'id': item['id'], 'name': item['content']} for item in items if isinstance(item, dict) and type(item.get('id')) is int and isinstance(item.get('content'), str) and item['content'].strip()]
        if not statements:
            raise ValueError('B站创作声明列表为空，请稍后重试。')
        return statements
    except (OSError, KeyError, TypeError, AttributeError, requests.RequestException, json.JSONDecodeError) as exc:
        raise ValueError('获取B站创作声明失败，请检查网络和登录状态后重试。') from exc


def predict_bilibili_legacy_tid(account_file: Path, title: str) -> int:
    try:
        cookies = _cookies(account_file)
        csrf = cookies.get('bili_jct')
        if not csrf:
            raise ValueError('B站登录状态不可用，请重新登录。')
        response = requests.post('https://member.bilibili.com/x/vupre/web/archive/types/predict', params={'csrf': csrf}, data={'filename': '', 'title': title}, cookies=cookies, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://member.bilibili.com/'}, timeout=10, allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        values = payload.get('data')
        if payload.get('code') != 0 or not isinstance(values, list) or not values or type(values[0].get('id')) is not int or values[0]['id'] <= 0:
            raise ValueError('无法获取B站投稿所需的分区信息，请重试。')
        return values[0]['id']
    except (OSError, KeyError, TypeError, AttributeError, requests.RequestException, json.JSONDecodeError) as exc:
        raise ValueError('获取B站投稿分区失败，请检查网络和登录状态。') from exc
