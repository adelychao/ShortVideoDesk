"""Read the signed-in account's visible creator profile with SAU's browser runtime."""

import asyncio
import json
import sys
from urllib.parse import urlsplit

from patchright.async_api import async_playwright


async def read_profile(platform: str, account_file: str) -> dict[str, str]:
    if platform not in {'xiaohongshu', 'douyin', 'baijiahao'}:
        return {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel='chromium')
        try:
            context = await browser.new_context(storage_state=account_file)
            page = await context.new_page()
            if platform == 'xiaohongshu':
                await page.goto('https://creator.xiaohongshu.com/', wait_until='domcontentloaded', timeout=30000)
                if urlsplit(page.url).hostname != 'creator.xiaohongshu.com':
                    return {}
                info = page.locator('.d-topbar-default .user-info').first
                await info.wait_for(state='visible', timeout=15000)
                name = (await info.locator('.name-box').inner_text()).strip()
                avatar = await info.locator('img.user_avatar').get_attribute('src') or ''
            else:
                if platform == 'douyin':
                    await page.goto('https://creator.douyin.com/creator-micro/home', wait_until='domcontentloaded', timeout=30000)
                    if urlsplit(page.url).hostname != 'creator.douyin.com':
                        return {}
                    info = page.locator('main.creator-home-redesign div[class*="container-"]').filter(has=page.locator('[class*="unique_id-"]')).first
                    await info.wait_for(state='visible', timeout=15000)
                    name = (await info.locator('div[class^="name-"]').inner_text()).strip()
                    avatar = await info.locator('div[class^="avatar-"] img').get_attribute('src') or ''
                else:
                    async with page.expect_response(
                        lambda response: (
                            urlsplit(response.url).hostname == 'baijiahao.baidu.com'
                            and urlsplit(response.url).path == '/user-ui/cms/settingInfo'
                        ),
                        timeout=20000,
                    ) as response_info:
                        await page.goto('https://baijiahao.baidu.com/builder/rc/home', wait_until='domcontentloaded', timeout=30000)
                    if urlsplit(page.url).hostname != 'baijiahao.baidu.com':
                        return {}
                    response = await response_info.value
                    if response.status != 200:
                        return {}
                    return _parse_baijiahao_profile_payload(await response.json())
            return {'name': name, 'avatar_url': avatar}
        finally:
            await browser.close()


def _parse_baijiahao_profile_payload(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get('errno') != 0:
        return {}
    data = payload.get('data')
    if not isinstance(data, dict):
        return {}
    name, avatar = data.get('name'), data.get('avatar')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return {}
    if not isinstance(avatar, str) or not avatar.strip() or len(avatar) > 2048:
        return {}
    return {'name': name.strip(), 'avatar_url': avatar.strip()}


if __name__ == '__main__':
    try:
        result = asyncio.run(read_profile(sys.argv[1], sys.argv[2]))
    except Exception:
        result = {}
    print(json.dumps(result, ensure_ascii=True))
