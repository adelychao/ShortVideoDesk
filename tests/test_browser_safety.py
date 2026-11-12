import pytest

from app import browser_safety
from app.workbench.video_publishing import SocialAutoUploadAdapter, VideoPublishManager
from app.workbench.database import Database


def test_windows_automation_fails_closed_before_browser_start(monkeypatch):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    with pytest.raises(browser_safety.BrowserSafetyError, match="暂停"):
        browser_safety.require_safe_browser("test")


def test_non_windows_does_not_inherit_windows_lockout_restriction(monkeypatch):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: False)
    browser_safety.require_safe_browser("test")


def test_sau_is_blocked_before_external_process(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    adapter = SocialAutoUploadAdapter(Database(tmp_path / "test.sqlite3"))
    result = adapter.run(["douyin", "check"], lambda process: pytest.fail("started"), 1)
    assert result.launch_error and "暂停" in result.launch_error


def test_video_manager_does_not_start_status_scheduler(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    manager = VideoPublishManager(database)
    manager.start()
    try:
        assert manager._sync_thread is None
    finally:
        manager.stop()


def test_blocked_publish_stays_queued_without_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    database = Database(tmp_path / "test.sqlite3")
    database.migrate()
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','douyin','user','connected','2026-11-02','2026-11-02')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique','video.mp4','video.mp4',1,'hash','title','queued','2026-11-02','2026-11-02')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','douyin','user','queued','2026-11-02','2026-11-02')")
    manager = VideoPublishManager(database)
    manager.adapter.upload = lambda *args: pytest.fail("publishing must not start")
    manager._run_publish("t")
    with database.transaction() as c:
        row = c.execute("SELECT status, message, attempt_count FROM video_publish_targets WHERE id='t'").fetchone()
        assert row["status"] == "queued"
        assert "暂停" in row["message"]
        assert row["attempt_count"] == 0
        assert c.execute("SELECT COUNT(*) FROM video_publish_attempts").fetchone()[0] == 0


def test_platform_login_is_blocked_before_worker_or_browser(monkeypatch, tmp_path):
    from app.workbench.platform_accounts import PlatformAccountError, PlatformAccountManager
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    manager = PlatformAccountManager(tmp_path)
    with pytest.raises(PlatformAccountError, match="暂停"):
        manager.start_login("douyin")
    assert not manager._windows


def test_open_chrome_is_blocked_before_process_start(monkeypatch):
    from app.browser import open_chrome
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr("app.browser.subprocess.Popen", lambda *args, **kwargs: pytest.fail("browser started"))
    with pytest.raises(browser_safety.BrowserSafetyError, match="暂停"):
        open_chrome("https://example.com")


def test_health_reports_browser_safety_pause(monkeypatch):
    import anonymous_poc
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_windows_browser_state_safe", lambda: False)
    assert anonymous_poc.health()["browser_automation_paused"] is True
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: False)
    assert anonymous_poc.health()["browser_automation_paused"] is False
