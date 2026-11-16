from app.workbench import baijiahao_video_status


def work(**values):
    return {"article_id": "123456789", "type": "ugc_video", "title": "标题", "first_storage_time": 1000,
            "status": "publish", "is_published": 1, "issue_status": 1,
            "visible_status": {"can_set_self_visible": 1, "can_set_public": -1}, **values}


class Response:
    def __init__(self, entries, count=None):
        self.entries = entries
        self.count = len(entries) if count is None else count

    def raise_for_status(self):
        pass

    def json(self):
        return {"errno": 0, "data": {"list": self.entries, "page": {"totalCount": self.count}}}


def target(**values):
    return {"effective_title": "标题", "started_at": "1970-01-01T00:16:40+00:00", "remote_id": None, **values}


def test_public_ugc_video_is_confirmed(monkeypatch):
    monkeypatch.setattr(baijiahao_video_status.requests, "get", lambda *a, **k: Response([work()]))
    result = baijiahao_video_status.fetch_status({"session": "value"}, target())
    assert result["status"] == "published"
    assert result["remote_id"] == "123456789"


def test_duplicate_or_incomplete_results_are_not_confirmed(monkeypatch):
    monkeypatch.setattr(baijiahao_video_status.requests, "get", lambda *a, **k: Response([work(), work(article_id="987654321")]))
    assert baijiahao_video_status.fetch_status({"session": "value"}, target())["status"] == "unknown"
    monkeypatch.setattr(baijiahao_video_status.requests, "get", lambda *a, **k: Response([work()], count=501))
    assert baijiahao_video_status.fetch_status({"session": "value"}, target())["status"] == "unknown"


def test_private_reviewing_and_article_are_not_confirmed(monkeypatch):
    for value in [work(visible_status={"can_set_public": 1, "can_set_self_visible": -1}), work(status="audit"), work(type="news")]:
        monkeypatch.setattr(baijiahao_video_status.requests, "get", lambda *a, **k: Response([value]))
        assert baijiahao_video_status.fetch_status({"session": "value"}, target())["status"] != "published"
