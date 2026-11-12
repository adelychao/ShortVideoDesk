"""Checks for the anonymous-only prototype's input and media identity boundary."""
import pytest

from anonymous_poc import canonical_source, detail_for_request, validate_source


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/video/123",
    "https://www.douyin.com.attacker.example/video/123",
    "https://attacker.douyin.com/video/123",
    "https://user:pass@www.douyin.com/video/123",
    "https://www.douyin.com:444/video/123",
    "http://www.douyin.com/video/123",
    "https://www.douyin.com/user/someone",
])
def test_only_official_https_single_video_links_are_accepted(url):
    with pytest.raises(ValueError):
        validate_source(url)


def test_share_text_is_accepted_and_tracking_query_is_removed():
    assert canonical_source("分享 https://www.douyin.com/video/123?track=example") == (
        "https://www.douyin.com/video/123", "123"
    )


def test_short_link_redirect_cannot_leave_official_site(monkeypatch):
    class Response:
        status_code = 302
        headers = {"Location": "https://127.0.0.1/private"}
        def close(self):
            pass

    monkeypatch.setattr("anonymous_poc.requests.get", lambda *a, **k: Response())
    with pytest.raises(ValueError):
        canonical_source("https://v.douyin.com/example/")


@pytest.mark.parametrize("url,payload", [
    ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=456",
     {"status_code": 0, "aweme_detail": {"aweme_id": "456"}}),
    ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123",
     {"status_code": 0, "aweme_detail": {"aweme_id": "456"}}),
    ("https://attacker.example/aweme/v1/web/aweme/detail/?aweme_id=123",
     {"status_code": 0, "aweme_detail": {"aweme_id": "123"}}),
    ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123",
     {"status_code": 1, "aweme_detail": {"aweme_id": "123"}}),
])
def test_recommendations_and_unbound_responses_are_never_downloaded(url, payload):
    assert detail_for_request(url, payload, "123") is None


def test_exact_requested_video_response_is_accepted():
    detail = {"aweme_id": "123", "desc": "example"}
    assert detail_for_request(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123",
        {"status_code": 0, "aweme_detail": detail}, "123"
    ) == detail


def test_local_api_rejects_invalid_sources_and_cross_origin_submission():
    from fastapi.testclient import TestClient
    from anonymous_poc import app
    with TestClient(app) as client:
        assert client.post("/api/jobs", json={"url": "https://127.0.0.1/video/123"}).status_code == 400
        assert client.post("/api/jobs", json={"url": "https://www.douyin.com/video/123"},
                           headers={"Origin": "https://attacker.example"}).status_code == 403
        assert client.get("/api/jobs/unknown/files/video").status_code == 404
        assert client.get("/api/health").json()["personal_chrome_cookies_used"] is False
