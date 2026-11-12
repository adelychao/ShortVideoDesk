"""Single-window, user-initiated Kuaishou creator login."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

LOGIN_URL = ('https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api'
             '&callback=https%3A%2F%2Fcp.kuaishou.com%2Frest%2Finfra%2Fsts%3FfollowUrl%3D'
             'https%253A%252F%252Fcp.kuaishou.com%252Farticle%252Fpublish%252Fvideo%26setRootDomain%3Dtrue')


def _safety(project: Path):
    path = Path(__file__).resolve().parents[1] / 'browser_safety.py'
    spec = importlib.util.spec_from_file_location('workbench_browser_safety', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('无法验证登录环境。')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reserve_xiaohongshu_login(None, project)
    return module


def _account(payload):
    if not isinstance(payload, dict) or payload.get('result') != 1:
        return None
    data = payload.get('data', payload)
    if not isinstance(data, dict):
        return None
    uid = data.get('userId')
    if isinstance(uid, bool) or not isinstance(uid, (str, int)):
        return None
    uid = str(uid)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uid) or uid == '0':
        return None
    name = data.get('userName', '')
    if not isinstance(name, str) or len(name) > 80:
        name = ''
    return {'identity': hashlib.sha256(('kuaishou\0' + uid).encode('utf-8')).hexdigest(),
            'name': name.strip(), 'avatar_url': _avatar(data.get('avatarUrl', ''))}


def _avatar(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        roots = ('yximgs.com', 'kwimgs.com', 'gifshow.com', 'kuaishou.com', 'kwai.net', 'wskwai.com', 'ksapisrv.com')
        if (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and any(host == root or host.endswith('.' + root) for root in roots)):
            return value
    except ValueError:
        pass
    return ''


async def login(project: Path, pending: Path, qr: Path, *, check: bool = False) -> dict:
    safety = _safety(project)
    async with async_playwright() as playwright:
        safety.reserve_xiaohongshu_login(None, project)
        executable = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright/chromium-1208/chrome-win64/chrome.exe'
        browser = await playwright.chromium.launch(headless=False, executable_path=str(executable))
        try:
            context = await browser.new_context(storage_state=str(pending)) if check else await browser.new_context()
            page = await context.new_page()
            await page.goto('https://cp.kuaishou.com/article/publish/video' if check else LOGIN_URL,
                            wait_until='domcontentloaded', timeout=45000)
            image = page.locator('main#login-form div.qr-login img[alt="qrcode"]').first
            if not check:
                try:
                    if not await image.is_visible():
                        await page.locator('main#login-form div.platform-switch').first.click(timeout=10000)
                    await image.wait_for(state='visible', timeout=30000)
                except Exception:
                    pass
            for _ in range(25 if check else 150):
                if sys.platform == 'win32' and not safety._session_unlocked():
                    raise RuntimeError('电脑已锁屏，请解锁后重新登录。')
                parsed = urlsplit(page.url)
                if parsed.scheme == 'https' and parsed.hostname == 'cp.kuaishou.com':
                    payload = await page.evaluate('''async () => {
                        if (!window.$http || typeof window.$http.post !== 'function') return null;
                        try {
                            const response = await window.$http.post('/rest/cp/creator/pc/home/infoV2', {},
                                {universalErrorHandler: false, universalLoading: false});
                            const user = response.data || response;
                            return {result: response.result, data: {userId: user.userId, userName: user.userName}};
                        } catch (_) { return null; }
                    }''', isolated_context=False)
                    profile = _account(payload)
                    if profile:
                        try:
                            avatar = page.locator('.user-info-avatar img').first
                            await avatar.wait_for(state='attached', timeout=6000)
                            profile['avatar_url'] = _avatar(await avatar.get_attribute('src'))
                        except Exception:
                            pass
                        if not check:
                            await context.storage_state(path=str(pending))
                        return profile
                elif not check and await image.is_visible():
                    data = await image.screenshot(type='png')
                    temp = qr.with_suffix('.tmp')
                    temp.write_bytes(data)
                    os.replace(temp, qr)
                await page.wait_for_timeout(2000)
            raise RuntimeError('等待登录超时，请重新登录。')
        finally:
            await browser.close()


if __name__ == '__main__':
    try:
        result = asyncio.run(login(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
                                   check=len(sys.argv) > 4 and sys.argv[4] == 'check'))
        print(json.dumps(result, ensure_ascii=True))
    except Exception:
        print('登录未完成，请重新尝试。', file=sys.stderr)
        sys.exit(1)
