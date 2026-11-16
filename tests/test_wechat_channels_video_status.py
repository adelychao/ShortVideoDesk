from app.workbench import wechat_channels_status


def item(**values):
    return {"objectId": "video/id-123456", "createTime": 1000, "status": 1, "visibleType": 1,
            "disableInfo": {"isDisabled": False}, "desc": {"description": "标题\n#AI\n视频简介", "mediaType": 4}, **values}


class Response:
    def __init__(self, entries, count=None):
        self.entries = entries
        self.count = len(entries) if count is None else count

    def raise_for_status(self):
        pass

    def json(self):
        return {"errCode": 0, "data": {"list": self.entries, "totalCount": self.count}}


def target(**values):
    return {"effective_title": "标题", "started_at": "1970-01-01T00:16:40+00:00", "schedule_at": None, "remote_id": None, **values}


def test_normal_public_post_is_confirmed(monkeypatch):
    monkeypatch.setattr(wechat_channels_status.requests, "post", lambda *a, **k: Response([item()]))
    result = wechat_channels_status.fetch_status({"session": "value"}, target())
    assert result["status"] == "published"
    assert result["remote_id"] == "video/id-123456"


def test_duplicate_or_incomplete_results_stay_unknown(monkeypatch):
    monkeypatch.setattr(wechat_channels_status.requests, "post", lambda *a, **k: Response([item(), item(objectId="video/id-654321")]))
    assert wechat_channels_status.fetch_status({"session": "value"}, target())["status"] == "unknown"
    monkeypatch.setattr(wechat_channels_status.requests, "post", lambda *a, **k: Response([item()], count=501))
    assert wechat_channels_status.fetch_status({"session": "value"}, target())["status"] == "unknown"


def test_private_disabled_or_other_status_is_not_published(monkeypatch):
    for work in [item(visibleType=2), item(disableInfo={"isDisabled": True}), item(status=2)]:
        monkeypatch.setattr(wechat_channels_status.requests, "post", lambda *a, **k: Response([work]))
        assert wechat_channels_status.fetch_status({"session": "value"}, target())["status"] != "published"


def test_description_mismatch_is_not_confirmed(monkeypatch):
    monkeypatch.setattr(wechat_channels_status.requests, "post", lambda *a, **k: Response([item()]))
    assert wechat_channels_status.fetch_status({"session": "value"}, target(effective_description="不同简介"))["status"] == "unknown"
