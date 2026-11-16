from types import SimpleNamespace
from unittest.mock import Mock

from app import browser_safety
from app.workbench.comments import douyin
from app.workbench.models import SourceKind, SourceRecord


def test_douyin_comments_can_load_when_windows_account_check_fails(monkeypatch):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: False)
    payload = {'status_code': 0, 'comments': [], 'cursor': 0, 'has_more': 0}
    response = SimpleNamespace(
        status=200,
        url='https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=123',
        json=lambda: payload,
    )

    class Page:
        def on(self, event, callback):
            assert event == 'response'
            self.callback = callback

        def goto(self, url, **kwargs):
            assert url == 'https://www.douyin.com/video/123'
            self.callback(response)

    page = Page()
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, 'sync_playwright', lambda: Mock(start=Mock(return_value=playwright)))
    source = SourceRecord(kind=SourceKind.URL, value='https://www.douyin.com/video/123', platform='douyin', source_id='123')
    adapter = douyin.DouyinCommentAdapter()
    try:
        result = adapter.fetch_page(source, None)
        assert result.comments == []
        assert result.has_more is False
    finally:
        adapter.close()
