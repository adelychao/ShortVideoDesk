from app.workbench import article_platforms
from app.workbench.config import AppConfig
from app.workbench.database import Database


def test_capabilities_distinguish_article_publication_from_manual_handoff():
    wechat = article_platforms.article_platform_capabilities("wechat")
    csdn = article_platforms.article_platform_capabilities("csdn")
    assert wechat["article"]["publish"] == "official_api"
    assert wechat["article"]["status_query"] is True
    assert csdn["article"]["publish"] == "browser"
    assert csdn["article"]["status_query"] is False
    assert article_platforms.article_platform_capabilities("toutiao")["article"]["publish"] == "browser"


def test_xiaohongshu_note_requires_manual_publication():
    note = article_platforms.article_platform_capabilities("xiaohongshu")["image_note"]
    assert note["publish"] == "manual"
    assert note["status_query"] is False


def test_unknown_platform_is_not_advertised_as_publishing():
    assert article_platforms.article_platform_capabilities("unknown") == {}


def test_platform_list_exposes_actual_capabilities(tmp_path, monkeypatch):
    database = Database(tmp_path / "capabilities.sqlite3")
    database.migrate()
    monkeypatch.setattr(article_platforms, "_weibo_status", lambda **_kwargs: {})
    statuses = article_platforms.platform_statuses(AppConfig(database), include_live_status=False)
    by_id = {status["id"]: status for status in statuses}
    assert by_id["wechat"]["capabilities"]["article"]["publish"] == "official_api"
    assert by_id["xiaohongshu"]["capabilities"]["image_note"]["publish"] == "manual"
    assert by_id["toutiao"]["capabilities"]["article"]["publish"] == "browser"
