from types import SimpleNamespace

import pytest

import anonymous_poc
from multi_sources import DownloadIssue


class FakePage:
    def __init__(self, clock, deliver_on_reload):
        self.clock = clock
        self.deliver_on_reload = deliver_on_reload
        self.reloads = 0
        self.handler = None

    def on(self, _event, callback):
        self.handler = callback

    def goto(self, *_args, **_kwargs):
        pass

    def reload(self, **_kwargs):
        self.reloads += 1
        if self.deliver_on_reload:
            payload = {'status_code': 0, 'aweme_detail': {'aweme_id': '123'}}
            response = SimpleNamespace(status=200, url='https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123', json=lambda: payload)
            self.handler(response)

    def wait_for_timeout(self, milliseconds):
        self.clock[0] += milliseconds / 1000


def setup_page(monkeypatch, deliver_on_reload):
    import playwright.sync_api
    clock = [0.0]
    page = FakePage(clock, deliver_on_reload)
    context = SimpleNamespace(new_page=lambda: page, add_cookies=lambda _: None)
    browser = SimpleNamespace(new_context=lambda **_: context, close=lambda: None)
    engine = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser))
    class Manager:
        def __enter__(self): return engine
        def __exit__(self, *_): return None
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: Manager())
    monkeypatch.setattr(anonymous_poc.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(anonymous_poc, 'verified_aweme_metadata', lambda *_: True)
    return page


def test_detail_is_retried_once_when_first_page_does_not_supply_response(monkeypatch):
    page = setup_page(monkeypatch, True)
    events = []
    result = anonymous_poc.capture_public_detail('https://www.douyin.com/video/123', '123', lambda **event: events.append(event))
    assert result['aweme_id'] == '123'
    assert page.reloads == 1
    assert any('重试' in event.get('message', '') for event in events)


def test_missing_detail_after_retry_has_actionable_error(monkeypatch):
    page = setup_page(monkeypatch, False)
    with pytest.raises(DownloadIssue) as error:
        anonymous_poc.capture_public_detail('https://www.douyin.com/video/123', '123', lambda **_: None)
    assert error.value.code == 'media_unavailable'
    assert page.reloads == 1
