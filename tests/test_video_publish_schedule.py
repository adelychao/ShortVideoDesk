from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.workbench.database import Database
from app.workbench.video_publishing import (
    VideoPublishError,
    VideoPublishManager,
    _claim_video_publish_target,
    normalize_video_publish_schedule,
    normalize_video_publish_target_schedule,
    request_video_publish_retry,
)


@pytest.mark.parametrize("value", ["2026-11-02T16:59", "2026-11-02T18:30", "2026-11-17T16:30"])
def test_douyin_schedule_rejects_times_outside_platform_window(value):
    with pytest.raises(VideoPublishError):
        normalize_video_publish_schedule(value, ["douyin"], now=datetime(2026, 11, 2, 16, 30))


def test_douyin_schedule_accepts_supported_time():
    assert normalize_video_publish_schedule("2026-11-02T19:00", ["douyin"], now=datetime(2026, 11, 2, 16, 30)) == "2026-11-02 19:00"


@pytest.mark.parametrize('value,allowed', [
    ('2026-11-02T17:29', False), ('2026-11-02T17:30', True),
    ('2026-11-09T16:30', True), ('2026-11-09T16:31', False),
])
def test_baijiahao_schedule_checks_platform_window(value, allowed):
    now = datetime(2026, 11, 2, 16, 30)
    if allowed:
        assert normalize_video_publish_schedule(value, ["baijiahao"], now=now) == value.replace("T", " ")
    else:
        with pytest.raises(VideoPublishError, match='1 小时后至 7 天'):
            normalize_video_publish_schedule(value, ["baijiahao"], now=now)


def test_platform_schedule_inherits_or_overrides_independently():
    now = datetime(2026, 11, 2, 16, 30)
    assert normalize_video_publish_target_schedule("2026-11-03T19:00", {}, "douyin", now=now) == "2026-11-03 19:00"
    assert normalize_video_publish_target_schedule("2026-11-03T19:00", {"schedule_at_override": "2026-11-04T20:00"}, "douyin", now=now) == "2026-11-04 20:00"
    assert normalize_video_publish_target_schedule("2026-11-03T19:00", {"schedule_at_override": None}, "douyin", now=now) is None
    assert normalize_video_publish_target_schedule(None, {}, "weibo", now=now) is None


def test_platform_schedule_validates_effective_time_and_unsupported_platform():
    now = datetime(2026, 11, 2, 16, 30)
    with pytest.raises(VideoPublishError, match="微博"):
        normalize_video_publish_target_schedule("2026-11-03T19:00", {}, "weibo", now=now)
    with pytest.raises(VideoPublishError):
        normalize_video_publish_target_schedule("2026-11-03T19:00", {"schedule_at_override": "2026-11-02T17:00"}, "douyin", now=now)
    with pytest.raises(VideoPublishError):
        normalize_video_publish_target_schedule("2026-11-03T19:00", {"schedule_at_override": ""}, "douyin", now=now)


def test_create_request_accepts_platform_schedule_override():
    from app.workbench.api import VideoPublishOptionsRequest
    value = VideoPublishOptionsRequest(schedule_at_override="2026-11-04T20:00")
    assert value.model_dump(exclude_none=True)["schedule_at_override"] == "2026-11-04T20:00"


def test_batch_preserves_each_platform_schedule_through_claim(tmp_path, monkeypatch):
    from app.workbench import video_publishing as publish

    db = Database(tmp_path / "test.sqlite")
    db.migrate()
    with db.transaction(write=True) as c:
        for account_id, platform in (("a", "douyin"), ("b", "kuaishou")):
            c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES(?,?,?,'connected','2026-11-02','2026-11-02')", (account_id, platform, account_id))
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    monkeypatch.setattr(publish, "get_upload", lambda *a, **kw: SimpleNamespace(path=media, filename="video.mp4", sha256="hash", detected_media_type="video"))
    monkeypatch.setattr(publish, "_copy_video_to_managed_storage", lambda *a: media)
    monkeypatch.setattr(publish, "_generate_douyin_first_frame_covers", lambda *a: {})
    monkeypatch.setattr(publish, "delete_upload", lambda *a: None)
    common = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    individual = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    batch = publish.create_video_publish_batch(db, archive_root=None, idempotency_key="1234567890abcdef", asset_id=None, upload_id="u", title="标题", description="", tags=[], schedule_at=common, targets=[{"account_id": "a", "options": {"schedule_at_override": individual}}, {"account_id": "b", "options": {}}])
    targets = {target["platform_id"]: target for target in batch["targets"]}
    assert _claim_video_publish_target(db, targets["douyin"]["id"])["schedule_at"] == individual.replace("T", " ")
    assert _claim_video_publish_target(db, targets["kuaishou"]["id"])["schedule_at"] == common.replace("T", " ")


def test_retry_can_change_one_target_to_immediate_without_changing_batch(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.migrate()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','douyin','user','connected','2026-11-02','2026-11-02')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,schedule_at,created_at,updated_at) VALUES('b','unique',?,'video.mp4',1,'hash','标题','failed','2020-01-01 17:00','2026-11-02','2026-11-02')", (str(media),))
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','douyin','user','failed','2026-11-02','2026-11-02')")
    with pytest.raises(VideoPublishError):
        request_video_publish_retry(db, "t", confirmed=True)
    request_video_publish_retry(db, "t", confirmed=True, change_schedule=True, schedule_at=None)
    claimed = _claim_video_publish_target(db, "t")
    assert claimed["schedule_at"] is None
    with db.transaction() as c:
        assert c.execute("SELECT schedule_at FROM video_publish_batches WHERE id='b'").fetchone()[0] == "2020-01-01 17:00"

    now = datetime.now(timezone.utc).isoformat()
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    with db.transaction(write=True) as c:
        c.execute("UPDATE video_publish_batches SET schedule_at=? WHERE id='b'", (future,))
        c.execute("UPDATE video_publish_targets SET status='pending_review', created_at=? WHERE id='t'", (now,))
    manager = VideoPublishManager(db)
    manager.start()
    try:
        assert manager._sync_thread is None
        assert manager._queue.empty()
    finally:
        manager.stop()
