import pytest

from app.workbench.api import VideoPublishOptionsRequest
from app.workbench.database import Database
from app.workbench.video_publishing import (
    SauResult, SocialAutoUploadAdapter, VideoPublishError, VideoPublishManager,
    _normalize_video_publish_draft_form, validate_douyin_location_options,
)


def test_api_preserves_selected_location_identity():
    options = VideoPublishOptionsRequest(location="北京路", location_id="6601231759000471556", location_address="烟台市北京路", location_city_code="370600")
    assert options.model_dump(exclude_none=True)["location_id"] == "6601231759000471556"


@pytest.mark.parametrize("scope", ["370600", "domestic"])
def test_location_scope_survives_search_and_draft(scope):
    places = [{"id": "6601231759000471556", "name": "岱王山", "address": "烟台市岱王山", "city_code": "370600"}]
    cities = [{"code": "370600", "name": "烟台市"}] if scope == "370600" else []
    tabs = [{"code": "domestic", "name": "国内"}] if scope == "domestic" else []
    payload = {"items": places, "cities": cities, "tabs": tabs, "selected_city_code": scope}
    result = VideoPublishManager._normalize_douyin_poi_result(payload, None)
    assert result["tabs"] == tabs
    assert result["cities"] == cities
    assert len(result["items"]) == 1
    place = result["items"][0]
    expected_tab = "domestic" if scope == "domestic" else None
    assert place.get("search_tab_code") == expected_tab
    form, _ = _normalize_video_publish_draft_form({
        "douyin_metadata": {"a": {"creator_tags": [{**place, "category": "location"}]}},
    })
    restored = form["douyin_metadata"]["a"]["creator_tags"][0]
    assert restored["city_code"] == "370600"
    assert restored.get("search_tab_code") == expected_tab
    options = {
        "location": restored["name"],
        "location_id": restored["id"],
        "location_address": restored["address"],
        "location_city_code": restored["city_code"],
    }
    if expected_tab:
        options["location_search_tab_code"] = expected_tab
    validate_douyin_location_options(options)
    assert options.get("location_search_tab_code") == expected_tab


@pytest.mark.parametrize("options", [
    {"location": "北京路"},
    {"location": "北京路", "location_id": "6601231759000471556", "location_city_code": "370600"},
    {"location_id": "6601231759000471556"},
])
def test_incomplete_location_is_rejected(options):
    with pytest.raises(VideoPublishError):
        validate_douyin_location_options(options)


def test_complete_location_and_empty_location_are_valid():
    validate_douyin_location_options({})
    validate_douyin_location_options({"location": "北京路", "location_id": "6601231759000471556", "location_address": "烟台市北京路", "location_city_code": "370600"})


def test_selected_location_fields_are_forwarded_to_sau(tmp_path):
    adapter = SocialAutoUploadAdapter(None)
    calls = []
    adapter.run = lambda arguments, on_start, timeout: calls.append(arguments) or SauResult(0, "")
    options = {"location": "北京路", "location_id": "6601231759000471556", "location_address": "烟台市北京路", "location_city_code": "370600"}
    target = {"platform_id": "douyin", "account_alias": "user", "media_path": str(tmp_path / "v.mp4"), "title": "新作品", "description": "", "tags": [], "options": options}
    adapter.upload(target, lambda process: None)
    args = calls[0]
    assert args[args.index("--location-id") + 1] == options["location_id"]
    assert args[args.index("--location-address") + 1] == options["location_address"]
    assert args[args.index("--location-city-code") + 1] == options["location_city_code"]


@pytest.fixture
def connected_douyin_manager(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','douyin','user','connected','2026-11-02','2026-11-02')")
    return VideoPublishManager(db)


def test_poi_prewarm_does_not_wait_behind_an_active_account_task(connected_douyin_manager):
    manager = connected_douyin_manager
    with manager._account_lock("a"):
        with pytest.raises(VideoPublishError, match="正在执行其他操作"):
            manager.search_douyin_poi("a", "", "local", wait_for_warmup=False)


def test_hot_recommendations_load_while_location_holds_account_lock(connected_douyin_manager, monkeypatch):
    manager = connected_douyin_manager
    monkeypatch.setattr(
        manager._douyin_hot_recommendations, "read",
        lambda: [{"id": "2754647", "title": "测试热点", "view_count": 12000, "view_count_text": "1.2万在看"}],
    )
    with manager._account_lock("a"):
        result = manager.search_douyin_hot_topics("a")
    assert result["items"][0]["id"] == "2754647"
