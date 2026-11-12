"""Browser regression for account navigation and video account operations."""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(['node', str(root / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
accounts = []
requests = []

def handle(route):
    req = route.request
    path = req.url.split('/api/v1/', 1)[-1].split('?', 1)[0]
    requests.append((req.method, path))
    sau = {'configured': True, 'project_path': 'D:/sau', 'message': '已配置', 'command_source': 'uv'}
    if path == 'video-publish/status': body = {'sau': sau}
    elif path == 'video-publish/platforms':
        body = {'platforms': [{'id': pid, 'name': name, 'cli_id': pid, 'schedule': True, 'tid_required': False, 'max_tags': 30, 'sau_ready': True, 'login_available': True} for pid, name in [('douyin', '抖音'), ('xiaohongshu', '小红书'), ('kuaishou', '快手'), ('bilibili', 'B 站'), ('wechat_channels', '微信视频号'), ('baijiahao', '百家号'), ('zhihu', '知乎'), ('unknown', '测试平台')]]}
    elif path == 'video-publish/accounts' and req.method == 'POST':
        data = req.post_data_json
        assert set(data) == {'platform_id'}
        body = {'id': 'a1', **data, 'account_alias': 'account_' + 'a' * 32, 'display_name': '', 'profile_name': '', 'avatar_url': '', 'status': 'not_connected', 'message': '', 'checked_at': None, 'enabled': True}
        accounts.append(body)
    elif path == 'video-publish/accounts': body = {'items': accounts}
    elif path == 'video-publish/accounts/a1/login':
        accounts[0] = {**accounts[0], 'status': 'login_in_progress'}
        body = accounts[0]
    elif path == 'video-publish/accounts/a1/check':
        accounts[0] = {**accounts[0], 'status': 'connected', 'profile_name': '平台昵称', 'avatar_url': 'https://p26.douyinpic.com/avatar.jpg', 'checked_at': '2026-11-02T08:00:00Z'}
        body = accounts[0]
    elif path.startswith('video-publish/accounts/') and path.endswith('/workbench'):
        assert req.method == 'POST'
        assert req.post_data_json == {}
        account_id = path.split('/')[2]
        account = next(item for item in accounts if item['id'] == account_id)
        platform_name = next(name for pid, name in [('douyin', '抖音'), ('xiaohongshu', '小红书'), ('kuaishou', '快手'), ('bilibili', 'B 站'), ('wechat_channels', '微信视频号'), ('baijiahao', '百家号'), ('zhihu', '知乎')] if pid == account['platform_id'])
        body = {'message': f'已打开{platform_name}创作工作台。'}
    elif path == 'video-publish/accounts/a1' and req.method == 'DELETE':
        accounts[:] = [item for item in accounts if item['id'] != 'a1']
        body = {}
    elif path == 'article-platforms':
        platforms = [('wechat', '微信公众号'), ('csdn', 'CSDN'), ('zhihu', '知乎'), ('xiaohongshu', '小红书'), ('toutiao', '今日头条'), ('baijiahao', '百家号'), ('weibo', '微博')]
        body = {'platforms': [{'id': pid, 'name': name, 'mode': 'manual', 'configured': False, 'connected': pid in ('csdn', 'toutiao'), 'message': '登录状态有效。' if pid in ('csdn', 'toutiao') else '', 'authorization_url': None, 'manual_url': None, 'secret_available': False, 'public_config': {}, 'accounts': [{'id': pid + '-profile', 'name': name + '账号', 'configured': True, 'auth_type': 'browser', 'status': 'connected'}] if pid in ('csdn', 'toutiao') else []} for pid, name in platforms]}
    elif path in ('article-platforms/csdn/editor', 'article-platforms/toutiao/editor'):
        platform_id = path.split('/')[1]
        body = {'id': platform_id, 'status': 'connected', 'authenticated': True, 'login_window_open': True, 'message': '已打开工作台。'}
    elif path == 'platform-accounts':
        body = {'platforms': [{'id': 'douyin', 'name': '抖音', 'status': 'not_connected', 'authenticated': False, 'login_window_open': False, 'message': '尚未登录'}]}
    elif path == 'platform-accounts/douyin/login':
        body = {'id': 'douyin', 'name': '抖音', 'status': 'login_window_open', 'authenticated': False, 'login_window_open': True, 'message': '请完成登录'}
    elif path == 'video-publish/media': body = {'items': []}
    else: body = {}
    route.fulfill(status=200, content_type='application/json', body=json.dumps(body, ensure_ascii=False))

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('**/api/v1/**', handle)
        for attempt in range(60):
            try:
                page.goto(f'http://127.0.0.1:{port}/settings?tab=accounts')
                break
            except Exception:
                if attempt == 59: raise
                time.sleep(.2)
        expect(page).to_have_url(f'http://127.0.0.1:{port}/accounts?tab=article')
        expect(page.get_by_role('link', name='账号管理', exact=True)).to_be_visible()
        expect(page.get_by_role('tab', name='文章发布')).to_have_attribute('aria-selected', 'true')
        article_logos = page.locator('.publishing-platform-tab img')
        expect(article_logos).to_have_count(7)
        for logo in article_logos.all():
            expect(logo).to_have_js_property('complete', True)
            assert logo.evaluate('(element) => element.naturalWidth') > 0
        wechat_logo_src = article_logos.nth(0).get_attribute('src')
        if os.environ.get('ARTICLE_TEST_SCREENSHOT'):
            page.screenshot(path=os.environ['ARTICLE_TEST_SCREENSHOT'], full_page=True)
        article_logos.nth(1).evaluate("element => { element.src = '/missing-platform-logo.svg' }")
        expect(page.locator('.publishing-platform-tab').nth(1).locator('.platform-logo__fallback')).to_have_text('C')
        page.get_by_role('tab', name='视频发布', exact=True).click()
        expect(page.get_by_text('登录状态可用于视频发布、素材下载和评论采集。')).to_be_visible()
        video_logos = page.locator('.account-platforms img')
        expect(video_logos).to_have_count(7)
        douyin_logo_src = video_logos.nth(0).get_attribute('src')
        assert video_logos.nth(4).get_attribute('src') != wechat_logo_src
        expect(page.locator('.account-platforms .platform-logo__fallback')).to_have_text('测')
        page.get_by_role('button', name='添加账号', exact=True).click()
        expect(page.get_by_text('账号 a1', exact=True)).to_be_visible()
        expect(page.get_by_text('等待网页登录')).to_be_visible()
        expect(page.get_by_role('button', name='打开工作台', exact=True)).to_have_count(0)
        assert ('POST', 'video-publish/accounts/a1/login') in requests
        accounts[0] = {**accounts[0], 'status': 'connected'}
        page.get_by_role('button', name='刷新状态').click()
        page.get_by_role('button', name='检查', exact=True).click()
        expect(page.get_by_text('上次检查：', exact=False)).to_be_visible()
        expect(page.get_by_text('平台昵称', exact=True)).to_be_visible()
        expect(page.locator('.account-avatar')).to_have_attribute('src', 'https://p26.douyinpic.com/avatar.jpg')
        assert ('POST', 'video-publish/accounts/a1/check') in requests
        workbench_button = page.get_by_role('button', name='打开工作台', exact=True)
        expect(workbench_button).to_be_visible()
        workbench_button.click()
        expect(page.get_by_text('已打开抖音创作工作台。', exact=True)).to_be_visible()
        assert ('POST', 'video-publish/accounts/a1/workbench') in requests
        supported_accounts = [
            ('xiaohongshu', '小红书'), ('kuaishou', '快手'), ('bilibili', 'B 站'),
            ('wechat_channels', '微信视频号'), ('baijiahao', '百家号'), ('zhihu', '知乎'),
        ]
        for index, (platform_id, platform_name) in enumerate(supported_accounts, start=2):
            accounts.append({'id': f'a{index}', 'platform_id': platform_id, 'account_alias': f'creator_{index}', 'display_name': platform_name + '账号', 'profile_name': '', 'avatar_url': '', 'status': 'connected', 'message': '', 'checked_at': None, 'enabled': True})
        accounts.append({'id': 'unknown-account', 'platform_id': 'unknown', 'account_alias': 'creator_unknown', 'display_name': '测试账号', 'profile_name': '', 'avatar_url': '', 'status': 'connected', 'message': '', 'checked_at': None, 'enabled': True})
        page.get_by_role('button', name='刷新状态').click()
        for index, (platform_id, platform_name) in enumerate(supported_accounts, start=2):
            page.locator('.account-platforms button').filter(has_text=platform_name).click()
            platform_workbench = page.get_by_role('button', name='打开工作台', exact=True)
            expect(platform_workbench).to_be_visible()
            platform_workbench.click()
            expect(page.get_by_text(f'已打开{platform_name}创作工作台。', exact=True)).to_be_visible()
            assert ('POST', f'video-publish/accounts/a{index}/workbench') in requests
        page.locator('.account-platforms button').filter(has_text='测试平台').click()
        expect(page.get_by_role('button', name='打开工作台', exact=True)).to_have_count(0)
        page.get_by_role('tab', name='文章发布', exact=True).click()
        for platform_name, account_id in [('知乎', 'a7'), ('小红书', 'a2'), ('百家号', 'a6')]:
            page.get_by_role('button', name=platform_name, exact=False).click()
            embedded_workbench = page.get_by_role('button', name='打开工作台', exact=True)
            expect(embedded_workbench).to_be_visible()
            embedded_workbench.click()
            assert ('POST', f'video-publish/accounts/{account_id}/workbench') in requests
        page.get_by_role('button', name='CSDN', exact=False).click()
        expect(page.get_by_role('button', name='打开工作台', exact=True)).to_be_visible()
        page.get_by_role('button', name='打开工作台', exact=True).click()
        assert ('POST', 'article-platforms/csdn/editor') in requests
        page.get_by_role('button', name='今日头条', exact=False).click()
        expect(page.get_by_role('button', name='打开工作台', exact=True)).to_be_visible()
        page.get_by_role('button', name='打开工作台', exact=True).click()
        assert ('POST', 'article-platforms/toutiao/editor') in requests
        page.get_by_role('tab', name='视频发布', exact=True).click()
        page.locator('.account-platforms button').filter(has_text='抖音').click()
        if os.environ.get('ACCOUNT_TEST_SCREENSHOT'):
            page.screenshot(path=os.environ['ACCOUNT_TEST_SCREENSHOT'], full_page=True)
        page.on('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='退出登录').click()
        expect(page.get_by_text('平台昵称', exact=True)).to_have_count(0)
        browser.close()
        print('Account workbench shortcuts passed for supported connected platforms')
finally:
    server.terminate()
    server.wait(timeout=10)
