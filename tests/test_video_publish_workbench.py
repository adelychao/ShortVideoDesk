import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workbench import api
from app.workbench.database import Database
from app.workbench.account_workbench_browser import (
    DOUYIN_WORKBENCH_URL,
    PLATFORM_WORKBENCH_URLS,
    AccountWorkbenchBrowserManager,
)
from app.workbench.video_publishing import (
    VideoPublishError,
    VideoPublishManager,
    add_video_publish_account,
    update_video_publish_account_status,
)


def test_douyin_workbench_endpoint_opens_connected_account(tmp_path, monkeypatch):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    account = add_video_publish_account(database, "douyin", "creator")
    update_video_publish_account_status(database, account["id"], "connected", "登录状态有效。")
    manager = VideoPublishManager(database)
    opened = []
    monkeypatch.setattr(
        manager,
        "open_account_workbench",
        lambda account_id: opened.append(account_id) or {"message": "已打开抖音创作者中心。"},
        raising=False,
    )
    monkeypatch.setattr(api, "_video_publish_manager", lambda _request: manager)
    app = FastAPI()
    app.include_router(api._router)

    with TestClient(app, base_url="http://testserver") as client:
        response = client.post(f"/api/v1/video-publish/accounts/{account['id']}/workbench")

    assert response.status_code == 200
    assert response.json() == {"message": "已打开抖音创作者中心。"}
    assert opened == [account["id"]]


def _connected_account(tmp_path, platform_id="douyin"):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    account = add_video_publish_account(database, platform_id, "creator")
    update_video_publish_account_status(database, account["id"], "connected", "登录状态有效。")
    manager = VideoPublishManager(database)
    project = tmp_path / "sau"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "sau"\n', encoding="utf-8")
    cookies = project / "cookies"
    cookies.mkdir()
    prefix = {"wechat_channels": "tencent"}.get(platform_id, platform_id)
    session_file = cookies / f"{prefix}_creator.json"
    state = {"cookies": [], "origins": []}
    if platform_id == "bilibili":
        state = {
            "token_info": {"mid": 123, "access_token": "token", "refresh_token": "refresh", "expires_in": 3600},
            "cookie_info": {"cookies": [
                {"name": "SESSDATA", "value": "session", "expires": -1},
                {"name": "bili_jct", "value": "csrf", "expires": -1},
            ]},
            "sso": [],
        }
    session_file.write_text(json.dumps(state), encoding="utf-8")
    manager.adapter.set_project_path(str(project))
    return database, account, manager, session_file


def test_open_account_workbench_requires_connected_douyin_account(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    account = add_video_publish_account(database, "douyin", "creator")
    manager = VideoPublishManager(database)
    opened = []
    manager.account_workbench_browser.open = lambda *args: opened.append(args)

    with pytest.raises(VideoPublishError) as error:
        manager.open_account_workbench(account["id"])

    assert error.value.status_code == 409
    assert opened == []


def test_open_account_workbench_uses_verified_saved_session(tmp_path):
    _database, account, manager, session_file = _connected_account(tmp_path)
    opened = []
    manager.account_workbench_browser.open = lambda account_id, platform, state: opened.append((account_id, platform, state))

    result = manager.open_account_workbench(account["id"])

    assert result == {"message": "已打开抖音创作工作台。"}
    assert opened == [(account["id"], "douyin", {"cookies": [], "origins": []})]


@pytest.mark.parametrize(
    "platform_id",
    ["xiaohongshu", "kuaishou", "bilibili", "wechat_channels", "baijiahao", "zhihu"],
)
def test_open_account_workbench_accepts_other_connected_sa_u_platforms(tmp_path, platform_id):
    _database, account, manager, _session_file = _connected_account(tmp_path, platform_id)
    opened = []
    manager.account_workbench_browser.open = lambda *args: opened.append(args)

    result = manager.open_account_workbench(account["id"])

    assert result["message"]
    assert opened
    assert opened[0][1] == platform_id


@pytest.mark.parametrize(
    "state_text",
    ["not-json", json.dumps({"cookies": [], "origins": None}), json.dumps({"origins": []})],
)
def test_open_account_workbench_rejects_invalid_saved_session(tmp_path, state_text):
    _database, account, manager, session_file = _connected_account(tmp_path)
    session_file.write_text(state_text, encoding="utf-8")
    opened = []
    manager.account_workbench_browser.open = lambda *args: opened.append(args)

    with pytest.raises(VideoPublishError) as error:
        manager.open_account_workbench(account["id"])

    assert error.value.status_code == 409
    assert opened == []


def test_open_account_workbench_rejects_path_traversal_alias(tmp_path):
    database, account, manager, _session_file = _connected_account(tmp_path)
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_accounts SET account_alias = '../escape' WHERE id = ?", (account["id"],))
    opened = []
    manager.account_workbench_browser.open = lambda *args: opened.append(args)

    with pytest.raises(VideoPublishError) as error:
        manager.open_account_workbench(account["id"])

    assert error.value.status_code == 409
    assert opened == []


def test_open_account_workbench_rejects_session_symlink(tmp_path):
    _database, account, manager, session_file = _connected_account(tmp_path)
    outside_state = tmp_path / "outside.json"
    outside_state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    session_file.unlink()
    try:
        session_file.symlink_to(outside_state)
    except (OSError, NotImplementedError):
        pytest.skip("the current Windows account cannot create a file symlink")
    opened = []
    manager.account_workbench_browser.open = lambda *args: opened.append(args)

    with pytest.raises(VideoPublishError) as error:
        manager.open_account_workbench(account["id"])

    assert error.value.status_code == 409
    assert opened == []


class _FakePage:
    def __init__(self, context):
        self.context = context
        self.url = ""

    def goto(self, url, **_kwargs):
        self.url = url


class _FakeContext:
    def __init__(self, storage_state, no_viewport=False):
        self.storage_state = storage_state
        self.no_viewport = no_viewport
        self.pages = []
        self.closed = False

    def new_page(self):
        page = _FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.connected = True

    def new_context(self, **kwargs):
        context = _FakeContext(**kwargs)
        self.contexts.append(context)
        return context

    def is_connected(self):
        return self.connected

    def close(self):
        self.connected = False


class _FakeChromium:
    def __init__(self):
        self.browsers = []

    def launch(self, **_kwargs):
        browser = _FakeBrowser()
        self.browsers.append(browser)
        return browser


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


def test_account_workbench_browser_reuses_context_and_opens_fixed_url(tmp_path, monkeypatch):
    import app.workbench.account_workbench_browser as browser_module

    monkeypatch.setattr(browser_module, "require_safe_browser", lambda _operation: None)
    fake_playwright = _FakePlaywright()
    browser_manager = AccountWorkbenchBrowserManager(lambda: fake_playwright)
    storage_state = {"cookies": [], "origins": []}

    browser_manager.open("account-1", "douyin", storage_state)
    browser_manager.open("account-1", "douyin", storage_state)

    browser = fake_playwright.chromium.browsers[0]
    assert len(fake_playwright.chromium.browsers) == 1
    assert len(browser.contexts) == 1
    context = browser.contexts[0]
    assert context.storage_state == storage_state
    assert context.no_viewport is True
    assert [page.url for page in context.pages] == [DOUYIN_WORKBENCH_URL, DOUYIN_WORKBENCH_URL]

    browser_manager.close_all()

    assert context.closed
    assert fake_playwright.stopped


@pytest.mark.parametrize("platform_id,expected_url", list(PLATFORM_WORKBENCH_URLS.items()))
def test_account_workbench_browser_uses_fixed_platform_url(tmp_path, monkeypatch, platform_id, expected_url):
    import app.workbench.account_workbench_browser as browser_module

    monkeypatch.setattr(browser_module, "require_safe_browser", lambda _operation: None)
    fake_playwright = _FakePlaywright()
    browser_manager = AccountWorkbenchBrowserManager(lambda: fake_playwright)

    browser_manager.open("account-1", platform_id, {"cookies": [], "origins": []})

    assert fake_playwright.chromium.browsers[0].contexts[0].pages[0].url == expected_url
    browser_manager.close_all()


@pytest.mark.parametrize(("platform_id", "home_url"), [
    ("xiaohongshu", "https://creator.xiaohongshu.com/"),
    ("kuaishou", "https://cp.kuaishou.com/"),
    ("bilibili", "https://member.bilibili.com/platform/home"),
])
def test_account_workbench_targets_creator_home_for_selected_platforms(platform_id, home_url):
    assert PLATFORM_WORKBENCH_URLS[platform_id] == home_url


def test_account_workbench_browser_obeys_safety_gate_before_launch(monkeypatch):
    import app.workbench.account_workbench_browser as browser_module
    from app.browser_safety import BrowserSafetyError

    launched = []
    monkeypatch.setattr(
        browser_module,
        "require_safe_browser",
        lambda operation: (_ for _ in ()).throw(BrowserSafetyError("browser paused")),
    )
    browser_manager = AccountWorkbenchBrowserManager(lambda: launched.append(True))

    with pytest.raises(BrowserSafetyError):
        browser_manager.open("account-1", "douyin", {})

    assert launched == []


def test_video_publish_manager_stops_workbench_browser_even_if_workers_never_started(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    manager = VideoPublishManager(database)
    closed = []
    manager.account_workbench_browser.close_all = lambda: closed.append(True)

    manager.stop()

    assert closed == [True]
