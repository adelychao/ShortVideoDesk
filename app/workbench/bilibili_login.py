"""Browser-free QR login producing credentials compatible with biliup."""

from __future__ import annotations

import hashlib
import io
import time
from threading import Event
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import requests
import segno

_APP_KEY = '4409e2ce8ffd12b8'
_APP_SIGNING_KEY = '59b43e04ad6965f34319062b478f83dd'
_API = 'https://passport.bilibili.com/x/passport-tv-login/qrcode/'
_HEADERS = {'User-Agent': 'Mozilla/5.0 BiliDroid/7.80.0', 'Referer': 'https://www.bilibili.com/'}


class BilibiliLoginError(Exception):
    pass


def _form(**kwargs: str) -> dict[str, str]:
    values = {'appkey': _APP_KEY, 'local_id': '0', 'ts': str(int(time.time())), **kwargs}
    encoded = urlencode(sorted(values.items()))
    values['sign'] = hashlib.md5((encoded + _APP_SIGNING_KEY).encode(), usedforsecurity=False).hexdigest()
    return values


def bilibili_identity(data: Any) -> str | None:
    try:
        token = data['token_info']
        mid = token['mid']
        if isinstance(mid, bool) or not isinstance(mid, int) or not 0 < mid < 2**64:
            return None
        if not all(isinstance(token.get(key), str) and token[key] for key in ('access_token', 'refresh_token')):
            return None
        if isinstance(token.get('expires_in'), bool) or not isinstance(token.get('expires_in'), int) or token['expires_in'] <= 0:
            return None
        cookies = data['cookie_info']['cookies']
        if not isinstance(cookies, list) or not isinstance(data['sso'], list):
            return None
        found = {item.get('name'): item.get('value') for item in cookies if isinstance(item, dict)}
        if not all(isinstance(found.get(key), str) and found[key] for key in ('SESSDATA', 'bili_jct')):
            return None
        if 'DedeUserID' in found and str(found['DedeUserID']) != str(mid):
            return None
        return hashlib.sha256(f'bilibili\0{mid}'.encode()).hexdigest()
    except (KeyError, TypeError, AttributeError):
        return None


def _profile_info(user: dict[str, Any]) -> dict[str, str]:
    name, avatar = user.get('uname'), user.get('face')
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        return {}
    if not isinstance(avatar, str) or len(avatar) > 2048:
        avatar = ''
    parsed = urlsplit(avatar)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.hdslb.com'):
        avatar = ''
    return {'name': name.strip(), 'avatar_url': avatar}


def _check_session(session: requests.Session, data: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    if not bilibili_identity(data):
        raise BilibiliLoginError('无法确认账号身份，请重新登录。')
    for cookie in data['cookie_info']['cookies']:
        if isinstance(cookie, dict) and isinstance(cookie.get('name'), str) and isinstance(cookie.get('value'), str):
            session.cookies.set(cookie['name'], cookie['value'], domain='.bilibili.com', path='/')
    response = session.get('https://api.bilibili.com/x/web-interface/nav', timeout=10)
    response.raise_for_status()
    result = response.json()
    user = result.get('data')
    if result.get('code') == -101:
        return False, {}
    if result.get('code') != 0 or not isinstance(user, dict):
        raise BilibiliLoginError('暂时无法检查登录状态，请稍后重试。')
    if user.get('isLogin') is False:
        return False, {}
    if user.get('isLogin') is not True or user.get('mid') != data['token_info']['mid']:
        raise BilibiliLoginError('无法确认账号身份，请重新登录。')
    return True, _profile_info(user)


def check_bilibili_login(data: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    try:
        with requests.Session() as session:
            session.headers.update(_HEADERS)
            return _check_session(session, data)
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        raise BilibiliLoginError('暂时无法检查登录状态，请稍后重试。') from None


def run_bilibili_login(on_qr: Callable[[bytes], None], stop: Event, cancel: Event | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        with requests.Session() as session:
            session.headers.update(_HEADERS)
            response = session.post(_API + 'auth_code', data=_form(), timeout=10)
            response.raise_for_status()
            payload = response.json()
            data = payload.get('data')
            if payload.get('code') != 0 or not isinstance(data, dict):
                raise BilibiliLoginError('二维码获取失败，请稍后重试。')
            url, auth_code = data.get('url'), data.get('auth_code')
            if not isinstance(url, str) or not isinstance(auth_code, str) or not auth_code or len(auth_code) > 128:
                raise BilibiliLoginError('二维码获取失败，请稍后重试。')
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname != 'passport.bilibili.com':
                raise BilibiliLoginError('二维码获取失败，请稍后重试。')
            if stop.is_set() or (cancel is not None and cancel.is_set()):
                raise BilibiliLoginError('登录已取消。')
            image = io.BytesIO()
            segno.make(url, error='L').save(image, kind='png', scale=6, border=4)
            on_qr(image.getvalue())
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if stop.is_set() or (cancel is not None and cancel.is_set()):
                    break
                if cancel is not None:
                    if cancel.wait(3):
                        break
                    if stop.is_set():
                        break
                elif stop.wait(3):
                    break
                response = session.post(_API + 'poll', data=_form(auth_code=auth_code), timeout=10)
                response.raise_for_status()
                result = response.json()
                if stop.is_set() or (cancel is not None and cancel.is_set()):
                    break
                if result.get('code') == 86039:
                    continue
                if result.get('code') == 86038:
                    raise BilibiliLoginError('二维码已过期，请重新登录。')
                credentials = result.get('data')
                if result.get('code') != 0 or not bilibili_identity(credentials):
                    raise BilibiliLoginError('无法确认登录结果，请重新登录。')
                credentials['platform'] = 'BiliTV'
                try:
                    valid, profile = _check_session(session, credentials)
                    if not valid:
                        raise BilibiliLoginError('无法确认登录结果，请重新登录。')
                except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
                    profile = {}
                if stop.is_set() or (cancel is not None and cancel.is_set()):
                    raise BilibiliLoginError('登录已取消。')
                return credentials, profile
            raise BilibiliLoginError('登录已结束或超时，请重新登录。')
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        raise BilibiliLoginError('暂时无法连接哔哩哔哩，请检查网络后重试。') from None
