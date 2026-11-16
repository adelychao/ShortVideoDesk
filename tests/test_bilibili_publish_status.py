from app.workbench import bilibili_options


class Response:
    def __init__(self, entries, count=None):
        self.entries = entries
        self.count = len(entries) if count is None else count

    def raise_for_status(self):
        pass

    def json(self):
        return {"code": 0, "data": {"arc_audits": [{"Archive": item} for item in self.entries], "page": {"count": self.count}}}


def archive(bvid="BV1ab411c7XY", title="标题", ctime=1000, **overrides):
    result = {"bvid": bvid, "title": title, "ctime": ctime, "state": 0, "state_desc": "开放浏览", "is_only_self": 0, "no_public": 0}
    result.update(overrides)
    return result


def target(**overrides):
    result = {"effective_title": "标题", "started_at": "1970-01-01T00:16:40+00:00", "remote_id": None}
    result.update(overrides)
    return result


def test_unique_public_archive_is_confirmed(monkeypatch):
    monkeypatch.setattr(bilibili_options.requests, "get", lambda *args, **kwargs: Response([archive()]))
    result = bilibili_options.fetch_bilibili_publish_status({"SESSDATA": "session"}, target())
    assert result["status"] == "published"
    assert result["remote_id"] == "BV1ab411c7XY"


def test_same_title_and_time_on_multiple_pages_is_ambiguous(monkeypatch):
    def request(*args, **kwargs):
        page = kwargs["params"]["pn"]
        entries = [archive(bvid=f"BV{n:010d}", title=f"旧作品{n}", ctime=500) for n in range(49)]
        if page == 1:
            return Response([archive()] + entries, count=51)
        return Response([archive(bvid="BV2ab411c7XY")], count=51)
    monkeypatch.setattr(bilibili_options.requests, "get", request)
    assert bilibili_options.fetch_bilibili_publish_status({"SESSDATA": "session"}, target())["status"] == "unknown"


def test_incomplete_or_invalid_list_is_not_success(monkeypatch):
    monkeypatch.setattr(bilibili_options.requests, "get", lambda *args, **kwargs: Response([archive()], count=501))
    assert bilibili_options.fetch_bilibili_publish_status({"SESSDATA": "session"}, target())["status"] == "unknown"


def test_private_and_auditing_are_not_marked_public(monkeypatch):
    for item, expected in [(archive(is_only_self=1), "private"), (archive(state=-6, state_desc="审核中"), "reviewing")]:
        monkeypatch.setattr(bilibili_options.requests, "get", lambda *args, **kwargs: Response([item]))
        assert bilibili_options.fetch_bilibili_publish_status({"SESSDATA": "session"}, target())["status"] == expected
