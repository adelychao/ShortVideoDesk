from types import SimpleNamespace

import multi_download
import pytest
from app import browser_safety
from multi_sources import parse_source


def test_public_page_probe_does_not_depend_on_windows_account_check(monkeypatch):
    import playwright.sync_api

    page = SimpleNamespace(
        goto=lambda *_args, **_kwargs: SimpleNamespace(status=200),
        wait_for_timeout=lambda *_: None,
        url='https://www.xiaohongshu.com/explore/abcdef1234567890abcdef12',
        title=lambda: '作品',
        locator=lambda _: SimpleNamespace(inner_text=lambda: '作品内容'),
        content=lambda: '<html>作品</html>',
    )
    browser = SimpleNamespace(new_context=lambda **_: SimpleNamespace(new_page=lambda: page), close=lambda: None)
    class Manager:
        def __enter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser))
        def __exit__(self, *_): return None

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: Manager())
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: False)
    evidence, html = multi_download.probe_public_page(parse_source(page.url))
    assert evidence['http_status'] == 200
    assert html == '<html>作品</html>'


def test_legacy_collection_account_can_read_session_despite_windows_account_check(tmp_path, monkeypatch):
    import playwright.sync_api
    from app.workbench.platform_accounts import PlatformAccountManager

    manager = PlatformAccountManager(profile_root=tmp_path)
    profile = tmp_path / 'douyin'
    profile.mkdir()
    (profile / 'marker').write_text('profile', encoding='utf-8')
    cookie = {'name': 'sessionid', 'value': 'session', 'domain': '.douyin.com', 'expires': -1}
    context = SimpleNamespace(cookies=lambda: [cookie], close=lambda: None)
    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **_: context))
        def __exit__(self, *_): return None

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: Manager())
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: False)
    assert manager.require_authenticated_cookies('douyin', collection_access=True) == [cookie]

    from app.workbench.platform_accounts import PlatformAccountError
    with pytest.raises(PlatformAccountError):
        manager.require_authenticated_cookies('douyin')
