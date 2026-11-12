import threading

import pytest

from app.workbench.bilibili_login import BilibiliLoginError, bilibili_identity, check_bilibili_login, run_bilibili_login


def credential(mid=42):
    return {'sso': [], 'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 's'},
                                                    {'name': 'bili_jct', 'value': 'c'},
                                                    {'name': 'DedeUserID', 'value': str(mid)}]},
            'token_info': {'mid': mid, 'expires_in': 3600, 'access_token': 'a', 'refresh_token': 'r'}}


class Response:
    def __init__(self, value): self.value = value
    def raise_for_status(self): pass
    def json(self): return self.value


class CookieJar:
    def set(self, *args, **kwargs): pass


class Session:
    def __init__(self, replies):
        self.headers = {}
        self.cookies = CookieJar()
        self.replies = iter(replies)
        self.posts = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def post(self, url, data, timeout):
        self.posts.append((url, data))
        return Response(next(self.replies))
    def get(self, url, timeout):
        return Response({'code': 0, 'data': {'isLogin': True, 'mid': 42, 'uname': '昵称', 'face': 'https://i0.hdslb.com/avatar.jpg'}})


class ImmediateStop:
    def wait(self, seconds): return False


def test_qr_login_polls_until_confirmed_and_produces_biliup_credentials(monkeypatch):
    session = Session([{'code': 0, 'data': {'auth_code': 'abc', 'url': 'https://passport.bilibili.com/x/example'}},
                       {'code': 86039}, {'code': 0, 'data': credential()}])
    monkeypatch.setattr('app.workbench.bilibili_login.requests.Session', lambda: session)
    images = []
    result, profile = run_bilibili_login(images.append, ImmediateStop())
    assert images[0].startswith(b'\x89PNG\r\n\x1a\n')
    assert result['platform'] == 'BiliTV'
    assert bilibili_identity(result)
    assert profile == {'name': '昵称', 'avatar_url': 'https://i0.hdslb.com/avatar.jpg'}
    assert len(session.posts) == 3
    assert all(len(data['sign']) == 32 for _, data in session.posts)


def test_expired_qr_is_reported(monkeypatch):
    session = Session([{'code': 0, 'data': {'auth_code': 'abc', 'url': 'https://passport.bilibili.com/x/example'}},
                       {'code': 86038}])
    monkeypatch.setattr('app.workbench.bilibili_login.requests.Session', lambda: session)
    with pytest.raises(BilibiliLoginError, match='过期'):
        run_bilibili_login(lambda image: None, ImmediateStop())


def test_login_rejects_untrusted_qr_url(monkeypatch):
    session = Session([{'code': 0, 'data': {'auth_code': 'abc', 'url': 'https://example.com/login'}}])
    monkeypatch.setattr('app.workbench.bilibili_login.requests.Session', lambda: session)
    with pytest.raises(BilibiliLoginError):
        run_bilibili_login(lambda image: pytest.fail('untrusted QR must not be shown'), threading.Event())


def test_identity_rejects_inconsistent_or_incomplete_credentials():
    data = credential()
    assert bilibili_identity(data)
    data['token_info']['mid'] = 43
    assert bilibili_identity(data) is None
    data = credential()
    data['token_info']['refresh_token'] = ''
    assert bilibili_identity(data) is None


def test_status_check_uses_the_saved_account_and_returns_profile(monkeypatch):
    session = Session([])
    monkeypatch.setattr('app.workbench.bilibili_login.requests.Session', lambda: session)
    valid, profile = check_bilibili_login(credential())
    assert valid is True
    assert profile['name'] == '昵称'


def test_status_check_reports_expired_but_rejects_mismatched_identity(monkeypatch):
    session = Session([])
    session.get = lambda url, timeout: Response({'code': -101, 'data': {'isLogin': False}})
    monkeypatch.setattr('app.workbench.bilibili_login.requests.Session', lambda: session)
    assert check_bilibili_login(credential()) == (False, {})
    session.get = lambda url, timeout: Response({'code': 0, 'data': {'isLogin': True, 'mid': 999}})
    with pytest.raises(BilibiliLoginError, match='身份'):
        check_bilibili_login(credential())
