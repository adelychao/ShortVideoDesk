import asyncio
import json
from pathlib import Path

import pytest

import app.workbench.wechat_channels_login_browser as login_helper
from app.workbench.wechat_channels_login_browser import _profile_from_auth_data


def test_official_auth_data_binds_stable_channels_identity_and_profile():
    payload = {
        "errCode": 0,
        "data": {
            "finderUser": {
                "finderUsername": "finder_test_123",
                "nickname": "频道昵称",
                "headImgUrl": "https://wx.qlogo.cn/mmhead/a/132",
            }
        },
    }

    assert _profile_from_auth_data(payload) == {
        "identity": "a1a3a701ad4f89fa4911b8b661cfc58b30fa0e94fd2f5e8c1415edf6eb22c7aa",
        "name": "频道昵称",
        "avatar_url": "https://wx.qlogo.cn/mmhead/a/132",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"errCode": 1, "data": {"finderUser": {"finderUsername": "finder_test_123"}}},
        {"errCode": 0, "data": {"finderUser": {"finderUsername": ""}}},
        {"errCode": 0, "data": {"finderUser": {"finderUsername": "x" * 129}}},
        {"errCode": False, "data": {"finderUser": {"finderUsername": "finder_test_123"}}},
    ],
)
def test_invalid_auth_data_cannot_create_channels_identity(payload):
    assert _profile_from_auth_data(payload) is None


@pytest.mark.parametrize(
    "avatar_url",
    [
        "http://wx.qlogo.cn/avatar.png",
        "https://wx.qlogo.cn.evil.test/avatar.png",
        "https://user@wx.qlogo.cn/avatar.png",
        "https://qpic.cn:444/avatar.png",
    ],
)
def test_untrusted_avatar_does_not_affect_verified_channels_identity(avatar_url):
    payload = {
        "errCode": 0,
        "data": {
            "finderUser": {
                "finderUsername": "finder_test_123",
                "nickname": "频道昵称",
                "headImgUrl": avatar_url,
            }
        },
    }

    profile = _profile_from_auth_data(payload)

    assert profile is not None
    assert profile["identity"] == "a1a3a701ad4f89fa4911b8b661cfc58b30fa0e94fd2f5e8c1415edf6eb22c7aa"
    assert profile["avatar_url"] == ""


class _FakeResponse:
    url = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data"
    status = 200

    async def json(self):
        return {
            "errCode": 0,
            "data": {
                "finderUser": {
                    "finderUsername": "finder_test_123",
                    "nickname": "频道昵称",
                    "headImgUrl": "https://wx.qlogo.cn/mmhead/a/132",
                }
            },
        }


class _FakeLocator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def screenshot(self, **_kwargs):
        return b"\x89PNG\r\n\x1a\n" + b"x" * 200

    async def is_visible(self):
        return True

    async def count(self):
        return 1

    async def get_attribute(self, _name):
        return None

    async def evaluate(self, _script):
        return True


class _FakeFrame:
    url = "https://open.weixin.qq.com/connect/qrconnect"

    def locator(self, _selector):
        return _FakeLocator()


class _FakePage:
    url = "https://channels.weixin.qq.com/login.html"
    storage_username = "finder_test_123"
    emit_auth_response = True
    redirect_to_login = False

    def __init__(self):
        self.listeners = {}
        self.frames = [_FakeFrame()]

    def on(self, event, callback):
        self.listeners[event] = callback

    async def goto(self, url, **_kwargs):
        self.url = "https://channels.weixin.qq.com/login.html" if self.redirect_to_login else url
        callback = self.listeners.get("response")
        if callback and self.emit_auth_response:
            callback(_FakeResponse())
            await asyncio.sleep(0)

    async def evaluate(self, _script, **_kwargs):
        return self.storage_username

    def locator(self, _selector):
        return _FakeLocator()

    async def wait_for_timeout(self, _milliseconds):
        await asyncio.sleep(0)


class _FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    async def storage_state(self, *, path):
        Path(path).write_text(
            json.dumps({
                "cookies": [],
                "origins": [{
                    "origin": "https://channels.weixin.qq.com",
                    "localStorage": [{"name": "finder_username", "value": "finder_test_123"}],
                }],
            }),
            encoding="utf-8",
        )

    async def close(self):
        return None


class _FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.context_options = []
        self.closed = False

    async def new_context(self, **kwargs):
        context = _FakeContext()
        self.contexts.append(context)
        self.context_options.append(kwargs)
        return context

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self):
        self.browser = _FakeBrowser()
        self.launches = []

    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        return self.browser


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def test_login_uses_one_browser_and_stages_verified_channels_session(tmp_path, monkeypatch):
    playwright = _FakePlaywright()

    class _Safety:
        def reserve_xiaohongshu_login(self, _database, _project):
            return None

        def _session_unlocked(self):
            return True

    monkeypatch.setattr(login_helper, "_safety", lambda _project: _Safety(), raising=False)
    monkeypatch.setattr(login_helper, "async_playwright", lambda: playwright, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    project = tmp_path / "sau"
    project.mkdir()
    cookies = project / "cookies"
    cookies.mkdir()
    candidate = cookies / "tencent_pending.json"
    qr = cookies / "tencent_pending_login_qrcode.png"

    result = login_helper.login(project, candidate, qr)

    assert result == {
        "status": "connected",
        "identity": "a1a3a701ad4f89fa4911b8b661cfc58b30fa0e94fd2f5e8c1415edf6eb22c7aa",
        "name": "频道昵称",
        "avatar_url": "https://wx.qlogo.cn/mmhead/a/132",
    }
    assert len(playwright.chromium.launches) == 1
    assert playwright.chromium.browser.closed
    assert candidate.is_file()
    assert qr.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_check_loads_saved_session_and_never_rewrites_it(tmp_path, monkeypatch):
    playwright = _FakePlaywright()

    class _Safety:
        def reserve_xiaohongshu_login(self, _database, _project):
            return None

        def _session_unlocked(self):
            return True

    monkeypatch.setattr(login_helper, "_safety", lambda _project: _Safety())
    monkeypatch.setattr(login_helper, "async_playwright", lambda: playwright)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    project = tmp_path / "sau"
    cookies = project / "cookies"
    cookies.mkdir(parents=True)
    session = cookies / "tencent_creator.json"
    session.write_text(json.dumps({
        "cookies": [],
        "origins": [{
            "origin": "https://channels.weixin.qq.com",
            "localStorage": [{"name": "finder_username", "value": "finder_test_123"}],
        }],
    }), encoding="utf-8")
    original = session.read_bytes()

    result = login_helper.login(project, session, cookies / "unused.png", check=True)

    assert result["identity"] == "a1a3a701ad4f89fa4911b8b661cfc58b30fa0e94fd2f5e8c1415edf6eb22c7aa"
    assert playwright.chromium.browser.context_options == [{"storage_state": str(session)}]
    assert session.read_bytes() == original
    assert not (cookies / "unused.png").exists()


def test_login_rejects_auth_identity_that_differs_from_official_storage(tmp_path, monkeypatch):
    playwright = _FakePlaywright()

    class _Safety:
        def reserve_xiaohongshu_login(self, _database, _project):
            return None

        def _session_unlocked(self):
            return True

    monkeypatch.setattr(login_helper, "_safety", lambda _project: _Safety())
    monkeypatch.setattr(login_helper, "async_playwright", lambda: playwright)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(_FakePage, "storage_username", "different_creator")
    project = tmp_path / "sau"
    cookies = project / "cookies"
    cookies.mkdir(parents=True)
    candidate = cookies / "tencent_pending.json"

    with pytest.raises(login_helper.WechatChannelsLoginError, match="无法核实视频号账号身份"):
        login_helper.login(project, candidate, cookies / "qr.png")

    assert not candidate.exists()


@pytest.mark.parametrize(
    ("redirect", "status"),
    [(True, "expired"), (False, "unavailable")],
)
def test_check_distinguishes_expired_session_from_unavailable(tmp_path, monkeypatch, redirect, status):
    playwright = _FakePlaywright()

    class _Safety:
        def reserve_xiaohongshu_login(self, _database, _project):
            return None

        def _session_unlocked(self):
            return True

    monkeypatch.setattr(login_helper, "_safety", lambda _project: _Safety())
    monkeypatch.setattr(login_helper, "async_playwright", lambda: playwright)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(_FakePage, "emit_auth_response", False)
    monkeypatch.setattr(_FakePage, "redirect_to_login", redirect)
    monkeypatch.setattr(login_helper, "_CHECK_POLL_ATTEMPTS", 1)
    project = tmp_path / "sau"
    cookies = project / "cookies"
    cookies.mkdir(parents=True)
    session = cookies / "tencent_creator.json"
    session.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    result = login_helper.login(project, session, cookies / "unused.png", check=True)

    assert result["status"] == status
    assert not (cookies / "unused.png").exists()


def test_expired_qr_is_refreshed_in_the_existing_login_page():
    class _RefreshLocator:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def click(self, **_kwargs):
            self.page.clicked = True

    class _RefreshPage:
        clicked = False

        def locator(self, _selector):
            return _RefreshLocator(self)

        async def wait_for_timeout(self, _milliseconds):
            return None

    page = _RefreshPage()

    assert asyncio.run(login_helper._refresh_expired_qr(page)) is True
    assert page.clicked is True


def test_qr_load_failure_text_triggers_refresh_inside_the_official_page():
    class _RetryLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        @property
        def first(self):
            return self

        async def count(self):
            return int("加载失败，点击重试" in self.selector)

        async def is_visible(self):
            return await self.count() == 1

        async def click(self, **_kwargs):
            self.page.clicked = True

    class _RetryPage:
        frames = []
        clicked = False

        def locator(self, selector):
            return _RetryLocator(self, selector)

        async def wait_for_timeout(self, _milliseconds):
            return None

    page = _RetryPage()

    assert asyncio.run(login_helper._refresh_expired_qr(page)) is True
    assert page.clicked is True


def test_qr_capture_supports_the_legacy_sau_login_iframe(tmp_path):
    class _Image:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def get_attribute(self, _name):
            return None

        async def evaluate(self, _script):
            return True

        async def screenshot(self, **_kwargs):
            return b"\x89PNG\r\n\x1a\n" + b"x" * 200

    class _Frame:
        url = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/login-for-iframe"

        def locator(self, selector):
            assert selector == "div#app img.qrcode"
            return _Image()

    class _Page:
        frames = [_Frame()]
        url = "https://channels.weixin.qq.com/login.html"

        def locator(self, _selector):
            return _Image()

        async def wait_for_timeout(self, _milliseconds):
            return None

    qr = tmp_path / "wechat-login.png"

    assert asyncio.run(login_helper._capture_qr(_Page(), qr)) is True
    assert qr.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_qr_capture_saves_the_official_png_source_instead_of_a_placeholder(tmp_path):
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"actual-wechat-qr" * 20

    class _Image:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "src"
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    class _Frame:
        url = "https://open.weixin.qq.com/connect/qrconnect"

        def locator(self, _selector):
            return _Image()

    class _Page:
        frames = [_Frame()]
        url = "https://channels.weixin.qq.com/login.html"

        def locator(self, _selector):
            return _Image()

        async def wait_for_timeout(self, _milliseconds):
            return None

    qr = tmp_path / "wechat-login.png"

    assert asyncio.run(login_helper._capture_qr(_Page(), qr)) is True
    assert qr.read_bytes() == png


def test_qr_capture_fetches_relative_source_only_from_official_wechat_host(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"downloaded-wechat-qr" * 20
    requested = []

    class _Response:
        ok = True
        headers = {"content-type": "image/png"}

        async def body(self):
            return png

    class _Request:
        async def get(self, url, **_kwargs):
            requested.append(url)
            return _Response()

    class _Context:
        request = _Request()

    class _Image:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "src"
            return "/connect/qrcode/temporary"

    class _Frame:
        url = "https://open.weixin.qq.com/connect/qrconnect"

        def locator(self, _selector):
            return _Image()

    class _Page:
        frames = [_Frame()]
        context = _Context()
        url = "https://channels.weixin.qq.com/login.html"

        def locator(self, _selector):
            return _Image()

        async def wait_for_timeout(self, _milliseconds):
            return None

    qr = tmp_path / "wechat-login.png"

    assert asyncio.run(login_helper._capture_qr(_Page(), qr)) is True
    assert requested == ["https://open.weixin.qq.com/connect/qrcode/temporary"]
    assert qr.read_bytes() == png


def test_qr_capture_uses_the_sau_iframe_locator_after_an_official_redirect(tmp_path):
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"redirected-wechat-qr" * 20

    class _Locator:
        def __init__(self, *, count=0, src=None):
            self.matches = count
            self.src = src

        @property
        def first(self):
            return self

        async def count(self):
            return self.matches

        async def is_visible(self):
            return self.matches > 0

        async def get_attribute(self, name):
            assert name == "src"
            return self.src

    class _QrFrameLocator:
        def locator(self, selector):
            assert selector == "div#app img.qrcode"
            return _Locator(count=1, src="data:image/png;base64," + base64.b64encode(png).decode("ascii"))

    class _Page:
        frames = []
        url = "https://channels.weixin.qq.com/login.html"

        def locator(self, selector):
            if selector == 'iframe[src*="login-for-iframe"]':
                return _Locator(count=1, src="https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/login-for-iframe")
            return _Locator()

        def frame_locator(self, selector):
            assert selector == 'iframe[src*="login-for-iframe"]'
            return _QrFrameLocator()

        async def wait_for_timeout(self, _milliseconds):
            return None

    qr = tmp_path / "wechat-login.png"

    assert asyncio.run(login_helper._capture_qr(_Page(), qr)) is True
    assert qr.read_bytes() == png


def test_qr_capture_rejects_external_source_and_broken_image_placeholder(tmp_path):
    class _Image:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def get_attribute(self, name):
            assert name == "src"
            return "https://untrusted.invalid/qr.png"

        async def evaluate(self, _script):
            return False

        async def screenshot(self, **_kwargs):
            pytest.fail("must not save the QR error placeholder as a usable code")

    class _Frame:
        url = "https://open.weixin.qq.com/connect/qrconnect"

        def locator(self, _selector):
            return _Image()

    class _Page:
        frames = [_Frame()]
        url = "https://channels.weixin.qq.com/login.html"

        def locator(self, _selector):
            return _Image()

        async def wait_for_timeout(self, _milliseconds):
            return None

    qr = tmp_path / "wechat-login.png"

    assert asyncio.run(login_helper._capture_qr(_Page(), qr)) is False
    assert not qr.exists()
