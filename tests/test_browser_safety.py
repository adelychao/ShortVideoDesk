import pytest

from app import browser_safety
from app.workbench.video_publishing import SocialAutoUploadAdapter, VideoPublishManager
from app.workbench.database import Database


def test_supported_browser_operations_ignore_windows_state(monkeypatch):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_session_unlocked", lambda: pytest.fail("unexpected session query"))
    monkeypatch.setattr(browser_safety, "_account_safe", lambda: pytest.fail("unexpected account query"))
    browser_safety.require_safe_browser("video publishing")
    with pytest.raises(browser_safety.BrowserSafetyError):
        browser_safety.require_safe_browser("unknown operation")


def test_non_windows_does_not_inherit_windows_lockout_restriction(monkeypatch):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: False)
    browser_safety.require_safe_browser("test")


def test_sau_check_can_start_without_windows_account_gate(monkeypatch, tmp_path):
    from app.workbench.video_publishing import SauCommand
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_account_safe", lambda: pytest.fail("unexpected account query"))
    adapter = SocialAutoUploadAdapter(Database(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(adapter, "resolve_command", lambda: (SauCommand(("sau",), tmp_path, str(tmp_path)), ""))
    class Process:
        returncode = 0
        def communicate(self, timeout): return ("valid", None)
    monkeypatch.setattr("app.workbench.video_publishing.subprocess.Popen", lambda *args, **kwargs: Process())
    assert adapter.check("douyin", "account", lambda process: None).returncode == 0


def test_video_manager_starts_status_scheduler(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    manager = VideoPublishManager(database)
    manager.start()
    try:
        assert manager._sync_thread is not None and manager._sync_thread.is_alive()
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


def test_login_component_still_rejects_unverified_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_verified_xhs_browser", lambda project: False)
    with pytest.raises(browser_safety.BrowserSafetyError, match="登录组件"):
        browser_safety.reserve_xiaohongshu_login(None, tmp_path)


def test_compatibility_hooks_do_not_check_windows_account():
    assert browser_safety._session_unlocked() is True
    assert browser_safety._account_safe() is True


def test_health_does_not_report_windows_account_pause(monkeypatch):
    import anonymous_poc
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_windows_browser_state_safe", lambda: pytest.fail("unexpected state query"))
    assert anonymous_poc.health()["browser_automation_paused"] is False
