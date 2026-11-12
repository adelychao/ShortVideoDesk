from datetime import datetime, timezone

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


def test_manual_status_check_is_rejected_when_browser_is_unsafe(setup_target, monkeypatch):
    from app import browser_safety
    _, manager = setup_target
    monkeypatch.setattr(browser_safety, "_is_windows", lambda: True)
    with pytest.raises(VideoPublishError, match="暂停"):
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
