from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from app.workbench.database import Database
from app.workbench.video_publishing import VideoPublishError, VideoPublishManager, get_video_publish_target, request_video_publish_retry


@pytest.fixture
def setup_target(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','douyin','user','connected','2026-11-02T00:00:00+00:00','2026-11-02T00:00:00+00:00')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique-key','video.mp4','video.mp4',1,'hash','标题','pending_review','2026-11-02T00:00:00+00:00','2026-11-02T00:00:00+00:00')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,attempt_count,started_at,created_at,updated_at) VALUES('t','b','a','douyin','user','pending_review',1,'2026-11-02T00:00:00+00:00','2026-11-02T00:00:00+00:00','2026-11-02T00:00:00+00:00')")
    return database, VideoPublishManager(database)


def test_platform_observation_is_separate_from_local_submission(setup_target):
    database, manager = setup_target
    manager.adapter.douyin_status = lambda target, on_start: {"status": "private", "remote_id": "123456789", "message": "私密作品"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "pending_review"
    assert target["platform_status"] == "private"
    assert target["remote_id"] == "123456789"
    assert target["platform_observed_at"] is not None


def test_scheduled_observation_is_not_published(setup_target):
    database, manager = setup_target
    manager.adapter.douyin_status = lambda target, on_start: {"status": "scheduled", "remote_id": "123456789", "message": "定时发布中"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "pending_review"
    assert target["platform_status"] == "scheduled"
    assert target["platform_observed_at"] is not None


def test_failed_refresh_preserves_last_observation(setup_target):
    database, manager = setup_target
    manager.adapter.douyin_status = lambda target, on_start: {"status": "private", "remote_id": "123456789", "message": "私密作品"}
    manager._run_status_sync("t")
    previous = get_video_publish_target(database, "t")
    manager.adapter.douyin_status = lambda target, on_start: {"status": "unknown", "remote_id": None, "message": "网络不可用"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["platform_status"] == "private"
    assert target["platform_observed_at"] == previous["platform_observed_at"]
    assert target["platform_message"] == "网络不可用"


def test_unknown_submission_without_remote_id_is_not_matched_by_title(setup_target):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET status='unknown' WHERE id='t'")
    with pytest.raises(VideoPublishError, match="作品标识"):
        manager.enqueue_status_sync("t")


def test_legacy_unverified_remote_id_is_not_trusted(setup_target):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET remote_id='999999' WHERE id='t'")
    def verify(target, on_start):
        assert target["remote_id"] is None
        return {"status": "private", "remote_id": "123456789", "message": "已核实"}
    manager.adapter.douyin_status = verify
    manager._run_status_sync("t")
    assert get_video_publish_target(database, "t")["remote_id"] == "123456789"


def test_late_sync_cannot_overwrite_a_new_attempt(setup_target):
    database, manager = setup_target
    def changed_attempt(target, on_start):
        with database.transaction(write=True) as c:
            c.execute("UPDATE video_publish_targets SET attempt_count=2 WHERE id='t'")
        return {"status": "published", "remote_id": "123456789", "message": "已发布"}
    manager.adapter.douyin_status = changed_attempt
    manager._run_status_sync("t")
    assert get_video_publish_target(database, "t")["platform_status"] is None


def test_confirmed_published_updates_target_attempt_and_batch(setup_target):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_attempts(id,target_id,attempt_number,status,started_at) VALUES('attempt','t',1,'pending_review','2026-11-02T00:00:00+00:00')")
    manager.adapter.douyin_status = lambda target, on_start: {"status": "published", "remote_id": "123456789", "message": "抖音作品管理页显示已发布。"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "published"
    assert target["remote_id"] == "123456789"
    with database.transaction() as c:
        assert c.execute("SELECT status FROM video_publish_attempts WHERE id='attempt'").fetchone()[0] == "published"
        assert c.execute("SELECT status FROM video_publish_batches WHERE id='b'").fetchone()[0] == "published"


def test_published_without_verified_id_remains_unconfirmed(setup_target):
    database, manager = setup_target
    manager.adapter.douyin_status = lambda target, on_start: {"status": "published", "remote_id": None, "message": "已发布"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "pending_review"
    assert "无法确认" in target["platform_message"]


def test_auto_sync_schedules_only_due_safe_targets(setup_target, monkeypatch):
    database, manager = setup_target
    monkeypatch.setattr("app.workbench.video_publishing.require_safe_browser", lambda reason: None)
    monkeypatch.setattr("app.workbench.video_publishing.browser_automation_paused", lambda: False)
    now = datetime.now(timezone.utc)
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_batches SET schedule_at=? WHERE id='b'", ((now + timedelta(hours=1)).isoformat(),))
    manager._schedule_status_sync_once(now=now)
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._sync_pending.clear()
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_batches SET schedule_at=NULL WHERE id='b'")
        c.execute("UPDATE video_publish_targets SET platform_checked_at=? WHERE id='t'", (now.isoformat(),))
    manager._schedule_status_sync_once(now=now)
    assert manager._queue.empty()
    manager._schedule_status_sync_once(now=now + timedelta(minutes=6))
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._schedule_status_sync_once(now=now + timedelta(minutes=6))
    assert manager._queue.empty()


def test_auto_sync_does_not_guess_identity_for_unknown_submission(setup_target, monkeypatch):
    database, manager = setup_target
    monkeypatch.setattr("app.workbench.video_publishing.require_safe_browser", lambda reason: None)
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET status='unknown' WHERE id='t'")
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.empty()


def test_future_non_douyin_target_still_waits_for_scheduled_time(setup_target, monkeypatch):
    database, manager = setup_target
    monkeypatch.setattr("app.workbench.video_publishing.browser_automation_paused", lambda: False)
    now = datetime.now(timezone.utc)
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET platform_id='bilibili' WHERE id='t'")
        c.execute("UPDATE video_publish_batches SET schedule_at=? WHERE id='b'", ((now + timedelta(hours=1)).isoformat(),))
    manager._schedule_status_sync_once(now=now)
    assert manager._queue.empty()


def test_restart_recovers_pending_status_checks(setup_target, monkeypatch):
    database, manager = setup_target
    monkeypatch.setattr("app.workbench.video_publishing.require_safe_browser", lambda reason: None)
    monkeypatch.setattr("app.workbench.video_publishing.browser_automation_paused", lambda: False)
    manager.adapter.douyin_status = lambda target, on_start: {"status": "published", "remote_id": "123456789", "message": "已发布"}
    completed = Event()
    original = manager._run_status_sync
    def run_sync(target_id):
        try:
            original(target_id)
        finally:
            completed.set()
    monkeypatch.setattr(manager, "_run_status_sync", run_sync)
    manager.start()
    try:
        assert completed.wait(5)
        assert get_video_publish_target(database, "t")["status"] == "published"
    finally:
        manager.stop()


def test_mismatched_verified_work_never_confirms_publication(setup_target):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET remote_id='111111', platform_observed_at='2026-11-02T00:00:00+00:00' WHERE id='t'")
    manager.adapter.douyin_status = lambda target, on_start: {"status": "published", "remote_id": "222222", "message": "已发布"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "pending_review"
    assert target["remote_id"] == "111111"
    assert "不一致" in target["platform_message"]


def test_rejected_work_is_reported_without_automatically_enabling_retry(setup_target):
    database, manager = setup_target
    manager.adapter.douyin_status = lambda target, on_start: {"status": "rejected", "remote_id": "123456789", "message": "审核未通过"}
    manager._run_status_sync("t")
    target = get_video_publish_target(database, "t")
    assert target["status"] == "pending_review"
    assert target["platform_status"] == "rejected"
    assert target["message"] == "审核未通过"


def test_bilibili_auto_sync_updates_public_archive(setup_target, monkeypatch):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET platform_id='bilibili' WHERE id='a'")
        c.execute("UPDATE video_publish_targets SET platform_id='bilibili' WHERE id='t'")
    monkeypatch.setattr(manager, "_bilibili_status", lambda target: {"status": "published", "remote_id": "BV1ab411c7XY", "message": "开放浏览"}, raising=False)
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._run_status_sync("t")
    result = get_video_publish_target(database, "t")
    assert result["status"] == "published"
    assert result["remote_id"] == "BV1ab411c7XY"
    assert result["remote_url"] == "https://www.bilibili.com/video/BV1ab411c7XY"


def test_kuaishou_auto_sync_uses_submitted_description(setup_target, monkeypatch):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET platform_id='kuaishou' WHERE id='a'")
        c.execute("UPDATE video_publish_batches SET description='实际简介' WHERE id='b'")
        c.execute("UPDATE video_publish_targets SET platform_id='kuaishou' WHERE id='t'")
    monkeypatch.setattr("app.workbench.video_publishing.require_safe_browser", lambda reason: None)
    monkeypatch.setattr("app.workbench.video_publishing.browser_automation_paused", lambda: False)
    def lookup(target, on_start):
        assert target["effective_caption"] == "实际简介"
        return {"status": "published", "remote_id": "3x8abcdef12345", "message": "已发布"}
    monkeypatch.setattr(manager.adapter, "kuaishou_status", lookup, raising=False)
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._run_status_sync("t")
    result = get_video_publish_target(database, "t")
    assert result["status"] == "published"
    assert result["remote_url"] == "https://www.kuaishou.com/short-video/3x8abcdef12345"


def test_automatic_sync_skips_browser_work_when_safety_is_paused(setup_target, monkeypatch):
    _, manager = setup_target
    monkeypatch.setattr("app.workbench.video_publishing.browser_automation_paused", lambda: True)
    monkeypatch.setattr("app.workbench.video_publishing.require_safe_browser", lambda reason: pytest.fail("不应重复启动浏览器安全检查"))
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.empty()


def test_wechat_channels_public_post_is_automatically_confirmed(setup_target, monkeypatch):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET platform_id='wechat_channels' WHERE id='a'")
        c.execute("UPDATE video_publish_targets SET platform_id='wechat_channels' WHERE id='t'")
    monkeypatch.setattr(manager, "_wechat_channels_status", lambda target: {"status": "published", "remote_id": "video/id-123456", "message": "已发表"}, raising=False)
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._run_status_sync("t")
    result = get_video_publish_target(database, "t")
    assert result["status"] == "published"
    assert result["remote_id"] == "video/id-123456"
    assert result["remote_url"] is None


def test_wechat_channels_draft_is_not_marked_published(setup_target):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET platform_id='wechat_channels', options_json=? WHERE id='t'", ('{"save_as_draft":true}',))
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.empty()


def test_baijiahao_public_video_is_automatically_confirmed(setup_target, monkeypatch):
    database, manager = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET platform_id='baijiahao' WHERE id='a'")
        c.execute("UPDATE video_publish_targets SET platform_id='baijiahao' WHERE id='t'")
    monkeypatch.setattr(manager, "_baijiahao_status", lambda target: {"status": "published", "remote_id": "123456789", "message": "已发布"}, raising=False)
    manager._schedule_status_sync_once(now=datetime.now(timezone.utc))
    assert manager._queue.get_nowait() == ("sync", "t")
    manager._run_status_sync("t")
    result = get_video_publish_target(database, "t")
    assert result["status"] == "published"
    assert result["remote_id"] == "123456789"
    assert result["remote_url"] is None


def test_manual_status_check_is_rejected_when_browser_is_unsafe(setup_target, monkeypatch):
    from app import browser_safety
    _, manager = setup_target
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    monkeypatch.setattr(browser_safety, "_session_unlocked", lambda: False)
    with pytest.raises(VideoPublishError, match="稍后重试"):
        manager.enqueue_status_sync("t")
    assert manager._queue.empty()


def test_legacy_repost_without_source_cannot_be_retried_silently(setup_target):
    database, _ = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET status='failed', options_json=? WHERE id='t'", ('{"declaration":"内容为转载信息"}',))
    with pytest.raises(VideoPublishError, match="新建发布任务"):
        request_video_publish_retry(database, "t", confirmed=True)
    assert get_video_publish_target(database, "t")["status"] == "failed"


def test_legacy_location_without_poi_id_cannot_be_retried_silently(setup_target):
    database, _ = setup_target
    with database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_targets SET status='failed', options_json=? WHERE id='t'", ('{"location":"广州塔"}',))
    with pytest.raises(VideoPublishError, match="重新选择地点"):
        request_video_publish_retry(database, "t", confirmed=True)
    assert get_video_publish_target(database, "t")["status"] == "failed"
