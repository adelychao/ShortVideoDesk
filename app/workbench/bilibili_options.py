"""Read Bilibili creator-center categories without exposing account credentials."""
from __future__ import annotations

import json
from pathlib import Path

import requests


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
