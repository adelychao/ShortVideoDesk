import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def helper(monkeypatch):
    patchright = types.ModuleType('patchright')
    api = types.ModuleType('patchright.async_api')
    api.async_playwright = lambda: None
    monkeypatch.setitem(sys.modules, 'patchright', patchright)
    monkeypatch.setitem(sys.modules, 'patchright.async_api', api)
    path = Path(__file__).resolve().parents[1] / 'app/workbench/kuaishou_login_browser.py'
    spec = importlib.util.spec_from_file_location('test_kuaishou_login_browser_module', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_creator_response_requires_positive_identity(helper):
    first = helper._account({'result': 1, 'data': {'userId': '123', 'userName': '同名'}})
    assert first['identity'] and first['name'] == '同名'
    assert helper._account({'result': 1, 'data': {'userId': '456', 'userName': '同名'}})['identity'] != first['identity']
    assert helper._account({'result': 1, 'data': {'userId': '123', 'userName': '新昵称'}})['identity'] == first['identity']
    assert helper._account({'result': 500002, 'data': {'userId': '123'}}) is None
    assert helper._account({'result': 1, 'data': {'userId': 0}}) is None
    assert helper._account({'result': 1, 'userId': '123', 'userName': '同名'}) == first
    with_avatar = helper._account({'result': 1, 'data': {'userId': '123', 'userName': '同名', 'avatarUrl': 'https://p1.a.yximgs.com/user.jpg'}})
    assert with_avatar['avatar_url'] == 'https://p1.a.yximgs.com/user.jpg'
    assert helper._account({'result': 1, 'data': {'userId': '123', 'avatarUrl': 'https://example.com/a.jpg'}})['avatar_url'] == ''


def test_login_uses_one_verified_browser_and_saves_same_context(helper, monkeypatch, tmp_path):
    launches, saved = [], []
    class Safety:
        def reserve_xiaohongshu_login(self, database, project): pass
        def _session_unlocked(self): return True
    class Locator:
        @property
        def first(self): return self
        async def is_visible(self): return True
        async def wait_for(self, **kwargs): pass
    class Response:
        ok = True
        async def json(self): return {'result': 1, 'data': {'userId': '123', 'userName': '昵称'}}
    class Page:
        url = 'https://cp.kuaishou.com/article/publish/video'
        async def goto(self, url, **kwargs): assert url == helper.LOGIN_URL
        def locator(self, selector): return Locator()
        async def evaluate(self, script, **kwargs):
            assert 'window.$http.post' in script
            assert kwargs.get('isolated_context') is False
            return await Response().json()
    class Context:
        async def new_page(self): return Page()
        async def storage_state(self, path):
            saved.append(path)
            Path(path).write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
    class Browser:
        async def new_context(self): return Context()
        async def close(self): pass
    class Chromium:
        async def launch(self, **kwargs):
            launches.append(kwargs)
            return Browser()
    class Playwright:
        chromium = Chromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(helper, '_safety', lambda project: Safety())
    monkeypatch.setattr(helper, 'async_playwright', Playwright)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    pending = tmp_path / 'pending.json'
    result = asyncio.run(helper.login(tmp_path, pending, tmp_path / 'qr.png'))
    assert result['name'] == '昵称'
    assert saved == [str(pending)]
    assert len(launches) == 1 and launches[0]['headless'] is False


def test_check_reuses_existing_session_without_overwriting_it(helper, monkeypatch, tmp_path):
    class Safety:
        def reserve_xiaohongshu_login(self, database, project): pass
        def _session_unlocked(self): return True
    class Page:
        url = 'https://cp.kuaishou.com/article/publish/video'
        async def goto(self, url, **kwargs): assert url == self.url
        async def evaluate(self, script, **kwargs):
            assert kwargs.get('isolated_context') is False
            return {'result': 1, 'data': {'userId': 123, 'userName': '快手昵称'}}
        def locator(self, selector): return self
        @property
        def first(self): return self
        async def wait_for(self, **kwargs): pass
        async def get_attribute(self, name): return 'https://p1.a.yximgs.com/a.jpg'
    class Context:
        async def new_page(self): return Page()
        async def storage_state(self, **kwargs): pytest.fail('check must not overwrite credentials')
    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs['storage_state'] == str(tmp_path / 'saved.json')
            return Context()
        async def close(self): pass
    class Chromium:
        async def launch(self, **kwargs): return Browser()
    class Playwright:
        chromium = Chromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(helper, '_safety', lambda project: Safety())
    monkeypatch.setattr(helper, 'async_playwright', Playwright)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    profile = asyncio.run(helper.login(tmp_path, tmp_path / 'saved.json', tmp_path / 'unused.png', check=True))
    assert profile['name'] == '快手昵称'
    assert profile['avatar_url'] == 'https://p1.a.yximgs.com/a.jpg'
