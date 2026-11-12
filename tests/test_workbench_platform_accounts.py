from __future__ import annotations

from app.workbench.database import Database
from app.workbench import downloads
from app.workbench import platform_accounts
from app.workbench.comments.bilibili import BilibiliCommentAdapter
from app.workbench.platform_accounts import (
    PlatformAccountError,
    PlatformAccountManager,
    get_task_access_mode,
    save_task_access_mode,
)
from multi_sources import DownloadIssue
import pytest
import threading
import sys
import types


def test_task_account_mode_is_persisted_per_task(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id, identity_key, source_kind, created_at) VALUES ('a','a','url','now')")
        connection.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t','a','{}','queued','now','now')")

    save_task_access_mode(database, "t", "account")

    assert get_task_access_mode(database, "t") == "account"
    assert get_task_access_mode(database, "other") == "anonymous"


def test_account_status_only_reports_session_state_and_filters_platform_cookies(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "sessionid", "value": "secret-douyin-session", "domain": ".douyin.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True},
        {"name": "SESSDATA", "value": "secret-bilibili-session", "domain": ".bilibili.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True},
        {"name": "unrelated", "value": "third-party", "domain": ".example.com", "path": "/", "expires": -1, "secure": True, "httpOnly": False},
    ])

    status = manager.account_status("douyin")
    cookies = manager.cookies("douyin")

    assert status["status"] == "connected"
    assert status["authenticated"] is True
    assert "secret" not in str(status)
    assert [cookie["name"] for cookie in cookies] == ["sessionid"]


def test_csdn_account_status_requires_both_authentication_cookies(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "UserToken", "value": "private-token", "domain": ".csdn.net", "expires": -1},
        {"name": "UserName", "value": "creator", "domain": ".blog.csdn.net", "expires": -1},
        {"name": "unrelated", "value": "third-party", "domain": ".example.com", "expires": -1},
    ])

    status = manager.account_status("csdn")
    cookies = manager.cookies("csdn")

    assert status["status"] == "connected"
    assert status["authenticated"] is True
    assert "private-token" not in str(status)
    assert [cookie["name"] for cookie in cookies] == ["UserToken", "UserName"]


def test_csdn_profile_decodes_nickname_and_keeps_only_public_identity_fields():
    profile = PlatformAccountManager._csdn_profile_from_cookies([
        {"name": "UserToken", "value": "private-token"},
        {"name": "UserName", "value": "test_writer_42"},
        {"name": "UserNick", "value": "%E6%B5%8B%E8%AF%95%E4%BD%9C%E8%80%85"},
    ], "https://i-avatar.csdnimg.cn/avatar_test_writer_42.jpg!1")

    assert profile == {
        "profile_name": "测试作者",
        "profile_username": "test_writer_42",
        "profile_avatar_url": "https://i-avatar.csdnimg.cn/avatar_test_writer_42.jpg!1",
    }


def test_csdn_account_status_attaches_profile_details_without_exposing_session_tokens(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "UserToken", "value": "private-token", "domain": ".csdn.net", "expires": -1},
        {"name": "UserName", "value": "test_writer_42", "domain": ".csdn.net", "expires": -1},
        {"name": "UserNick", "value": "%E6%B5%8B%E8%AF%95%E4%BD%9C%E8%80%85", "domain": ".csdn.net", "expires": -1},
    ])
    monkeypatch.setattr(manager, "_read_csdn_avatar_url", lambda: "https://i-avatar.csdnimg.cn/avatar.jpg")

    status = manager.account_status("csdn")

    assert status["profile_name"] == "测试作者"
    assert status["profile_username"] == "test_writer_42"
    assert status["profile_avatar_url"] == "https://i-avatar.csdnimg.cn/avatar.jpg"
    assert "private-token" not in str(status)


@pytest.mark.parametrize("avatar_url", [
    "http://i-avatar.csdnimg.cn/avatar.jpg",
    "https://evil.example/avatar.jpg",
    "https://i-avatar.csdnimg.cn.evil.example/avatar.jpg",
])
def test_csdn_profile_rejects_untrusted_avatar_urls(avatar_url):
    profile = PlatformAccountManager._csdn_profile_from_cookies([
        {"name": "UserName", "value": "test_writer_42"},
        {"name": "UserNick", "value": "creator"},
    ], avatar_url)

    assert profile["profile_avatar_url"] is None


def test_csdn_avatar_reader_uses_editor_profile_image_without_returning_other_data():
    class Locator:
        def __init__(self):
            self.selector = None

        def wait_for(self, state, timeout):
            assert state == "attached"
            assert timeout > 0

        def get_attribute(self, name, timeout):
            assert name == "src"
            assert timeout > 0
            return "https://i-avatar.csdnimg.cn/avatar.jpg?tracking=private"

    class Page:
        def __init__(self):
            self.locator_instance = Locator()

        def locator(self, selector):
            self.locator_instance.selector = selector
            return self.locator_instance

    page = Page()

    avatar_url = PlatformAccountManager._csdn_avatar_from_page(page)

    assert page.locator_instance.selector == "img.img-user-avatar"
    assert avatar_url == "https://i-avatar.csdnimg.cn/avatar.jpg"


def test_csdn_app_managed_window_opens_the_editor_in_its_isolated_profile(tmp_path):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    definition = next(item for item in platform_accounts.PLATFORM_ACCOUNTS if item["id"] == "csdn")

    assert definition["login_url"] == "https://editor.csdn.net/md/"
    assert manager._profile_path("csdn") == (tmp_path / "profiles" / "csdn").resolve()


def test_csdn_username_cookie_without_token_is_not_authenticated(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "UserName", "value": "creator", "domain": ".csdn.net", "expires": -1},
    ])

    status = manager.account_status("csdn")

    assert status["status"] == "not_connected"
    assert status["authenticated"] is False


def test_csdn_open_editor_status_keeps_manual_publish_in_the_same_window(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_window_is_open", lambda _platform: True)
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _platform: [])

    status = manager.account_status("csdn")

    assert status["status"] == "login_window_open"
    assert "同一窗口手动发布" in status["message"]


def test_toutiao_account_session_accepts_only_toutiao_auth_cookies(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    definition = next(item for item in platform_accounts.PLATFORM_ACCOUNTS if item["id"] == "toutiao")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "sessionid", "value": "toutiao-session", "domain": ".toutiao.com", "expires": -1},
        {"name": "sid_tt", "value": "wrong-domain", "domain": ".evil.example", "expires": -1},
        {"name": "ttwid", "value": "anonymous-id", "domain": ".toutiao.com", "expires": -1},
    ])
    monkeypatch.setattr(manager, "_read_toutiao_profile", lambda: {
        "profile_name": "测试头条号", "profile_avatar_url": "https://p3.toutiaoimg.com/avatar.png",
    })

    status = manager.account_status("toutiao")
    cookies = manager.cookies("toutiao")

    assert definition["login_url"] == "https://mp.toutiao.com/profile_v4/graphic/publish"
    assert manager._profile_path("toutiao") == (tmp_path / "profiles" / "toutiao").resolve()
    assert status["status"] == "connected"
    assert status["authenticated"] is True
    assert status["profile_name"] == "测试头条号"
    assert status["profile_avatar_url"] == "https://p3.toutiaoimg.com/avatar.png"
    assert "toutiao-session" not in str(status)
    assert [cookie["name"] for cookie in cookies] == ["sessionid", "ttwid"]


def test_toutiao_profile_extracts_only_public_name_and_safe_avatar():
    profile = PlatformAccountManager._toutiao_profile_from_payload({
        "data": {"user": {
            "id": "private-account-id",
            "screen_name": "测试头条号",
            "https_avatar_url": "https://p3.toutiaoimg.com/avatar.png?private=1",
        }},
    })

    assert profile == {
        "profile_name": "测试头条号",
        "profile_avatar_url": "https://p3.toutiaoimg.com/avatar.png",
    }
    assert "private-account-id" not in str(profile)


def test_toutiao_profile_accepts_toutiaostatic_avatar_cdn():
    avatar_url = "https://sf3-cdn-tos.toutiaostatic.com/img/user-avatar/avatar~300x300.image"

    profile = PlatformAccountManager._toutiao_profile_from_payload({
        "data": {"user": {
            "screen_name": "测试头条号",
            "https_avatar_url": avatar_url,
        }},
    })

    assert profile["profile_avatar_url"] == avatar_url


@pytest.mark.parametrize("avatar_url", [
    "http://p3.toutiaoimg.com/avatar.png",
    "https://toutiaoimg.com.evil.example/avatar.png",
    "https://evil.example/avatar.png",
])
def test_toutiao_profile_rejects_untrusted_avatar_urls(avatar_url):
    profile = PlatformAccountManager._toutiao_profile_from_payload({
        "data": {"user": {"screen_name": "测试头条号", "https_avatar_url": avatar_url}},
    })

    assert profile["profile_name"] == "测试头条号"
    assert profile["profile_avatar_url"] is None


def test_toutiao_anonymous_cookie_does_not_count_as_authenticated(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "ttwid", "value": "anonymous-id", "domain": ".toutiao.com", "expires": -1},
    ])

    status = manager.account_status("toutiao")

    assert status["status"] == "not_connected"
    assert status["authenticated"] is False


def test_toutiao_open_editor_status_keeps_manual_publish_in_the_same_window(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "_window_is_open", lambda _platform: True)
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _platform: [])

    status = manager.account_status("toutiao")

    assert status["status"] == "login_window_open"
    assert "同一窗口手动发布" in status["message"]


@pytest.mark.parametrize("platform", ["csdn", "toutiao"])
def test_open_workbench_reuses_live_platform_profile(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(platform_accounts, "require_safe_browser", lambda _operation: None)
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    window = platform_accounts._LoginWindow(platform, tmp_path / platform)
    window.ready.set()
    keepalive = threading.Event()
    window.thread = threading.Thread(target=keepalive.wait, daemon=True)
    window.thread.start()
    manager._windows[platform] = window
    observed_commands = []

    def respond_to_open_page():
        command = window.commands.get(timeout=2)
        observed_commands.append(command.kind)
        command.completed.set()

    responder = threading.Thread(target=respond_to_open_page, daemon=True)
    responder.start()
    monkeypatch.setattr(manager, "start_login", lambda *_args: pytest.fail("existing profile should be reused"))
    monkeypatch.setattr(manager, "account_status", lambda selected: {
        "id": selected, "status": "connected", "authenticated": True, "message": "检测到登录会话。",
    })

    result = manager.open_workbench(platform)

    responder.join(timeout=2)
    keepalive.set()
    window.thread.join(timeout=2)
    assert result == {
        "id": platform,
        "status": "connected",
        "authenticated": True,
        "message": f"已打开{next(item['name'] for item in platform_accounts.PLATFORM_ACCOUNTS if item['id'] == platform)}工作台；平台可能仍要求重新登录。",
    }
    assert observed_commands == ["open_page"]


def test_open_workbench_starts_existing_login_flow_when_profile_is_not_open(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    operations = []
    monkeypatch.setattr(
        platform_accounts,
        "require_safe_browser",
        lambda operation: operations.append(operation),
    )
    monkeypatch.setattr(manager, "start_login", lambda platform: {"id": platform, "status": "login_window_open"})

    result = manager.open_workbench("csdn")

    assert operations == ["open platform workbench"]
    assert result == {"id": "csdn", "status": "login_window_open"}


def test_open_workbench_rejects_non_article_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_accounts, "require_safe_browser", lambda _operation: None)
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")

    with pytest.raises(PlatformAccountError, match="没有可打开"):
        manager.open_workbench("douyin")


@pytest.mark.parametrize("platform", ["csdn", "toutiao"])
def test_login_window_open_page_command_uses_fixed_workbench_and_resizable_context(tmp_path, monkeypatch, platform):
    class FakeTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self):
            self.urls = []

        def goto(self, url, **_kwargs):
            self.urls.append(url)

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.closed = False

        def new_page(self):
            page = FakePage()
            self.pages.append(page)
            return page

        def close(self):
            self.closed = True

    context = FakeContext()

    class FakeChromium:
        options = None

        def launch_persistent_context(self, **options):
            self.options = options
            return context

    chromium = FakeChromium()
    fake_playwright = types.SimpleNamespace(chromium=chromium)

    class FakePlaywrightContext:
        def __enter__(self):
            return fake_playwright

        def __exit__(self, *_args):
            return False

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.TimeoutError = FakeTimeoutError
    fake_sync_api.sync_playwright = FakePlaywrightContext
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    window = platform_accounts._LoginWindow(platform, tmp_path / platform)
    worker = threading.Thread(target=manager._login_window_worker, args=(window, next(
        item["login_url"] for item in platform_accounts.PLATFORM_ACCOUNTS if item["id"] == platform
    )), daemon=True)
    window.thread = worker
    worker.start()

    assert window.ready.wait(timeout=2)
    command = platform_accounts._LoginCommand("open_page")
    window.commands.put(command)
    assert command.completed.wait(timeout=2)
    close_command = platform_accounts._LoginCommand("close")
    window.commands.put(close_command)
    worker.join(timeout=2)

    assert command.error is None
    assert chromium.options["no_viewport"] is True
    assert [page.urls for page in context.pages] == [
        [next(item["login_url"] for item in platform_accounts.PLATFORM_ACCOUNTS if item["id"] == platform)],
        [next(item["login_url"] for item in platform_accounts.PLATFORM_ACCOUNTS if item["id"] == platform)],
    ]
    assert not worker.is_alive()


def test_video_collection_account_list_does_not_include_article_only_csdn(monkeypatch, tmp_path):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    checked = []

    def fake_status(platform):
        checked.append(platform)
        return {"id": platform, "status": "not_connected"}

    monkeypatch.setattr(manager, "account_status", fake_status)

    statuses = manager.all_statuses()

    assert [item["id"] for item in statuses] == ["douyin", "bilibili"]
    assert checked == ["douyin", "bilibili"]


def test_expired_platform_cookies_are_not_returned_or_exported(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    now = __import__("time").time()
    monkeypatch.setattr(manager, "_load_profile_cookies", lambda _: [
        {"name": "sessionid", "value": "expired", "domain": ".douyin.com", "path": "/", "expires": now - 100, "secure": True, "httpOnly": True},
        {"name": "sid_tt", "value": "session", "domain": ".douyin.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True},
    ])

    cookies = manager.cookies("douyin")
    with manager.temporary_cookie_file("douyin", cookies) as cookie_file:
        contents = cookie_file.read_text(encoding="utf-8")

    assert [cookie["value"] for cookie in cookies] == ["session"]
    assert "expired" not in contents
    assert "session" in contents


def test_temporary_cookie_file_is_scoped_and_removed_after_use(tmp_path, monkeypatch):
    manager = PlatformAccountManager(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(manager, "cookies", lambda _: [
        {"name": "sessionid", "value": "secret-value", "domain": ".douyin.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True},
        {"name": "other", "value": "other-value", "domain": ".example.com", "path": "/", "expires": -1, "secure": True, "httpOnly": False},
    ])

    with manager.temporary_cookie_file("douyin") as cookie_file:
        contents = cookie_file.read_text(encoding="utf-8")
        assert "secret-value" in contents
        assert "other-value" not in contents
        assert "HttpOnly_.douyin.com" in contents

    assert not cookie_file.exists()


def test_account_download_fails_closed_when_platform_session_is_missing(tmp_path, monkeypatch):
    class MissingAccount:
        def require_authenticated_cookies(self, _platform):
            raise PlatformAccountError("请先登录。")

    monkeypatch.setattr(platform_accounts, "get_platform_account_manager", lambda: MissingAccount())

    with pytest.raises(DownloadIssue) as captured:
        downloads.download_source(
            "https://www.douyin.com/video/123456789",
            tmp_path,
            lambda *_: None,
            allow_first_frame=False,
            access_mode="account",
        )

    assert captured.value.code == "platform_account_required"
    assert str(captured.value) == "请先登录。"


def test_bilibili_comment_adapter_receives_only_supplied_platform_cookies():
    adapter = BilibiliCommentAdapter(
        cookies=[
            {"name": "SESSDATA", "value": "private-session", "domain": ".bilibili.com", "path": "/", "expires": -1, "secure": True},
        ],
        authenticated=True,
    )
    try:
        assert adapter.session.cookies.get("SESSDATA", domain=".bilibili.com") == "private-session"
        assert adapter._authenticated is True
    finally:
        adapter.close()
