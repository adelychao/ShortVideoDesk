"""Consumer contracts for the expanded local downloader."""
import subprocess

import pytest
from fastapi.testclient import TestClient

import anonymous_poc as local


@pytest.mark.parametrize("url", [
    "https://www.xiaohongshu.com/explore/674051740000000007027a15?xsec_token=public_share_token&xsec_source=pc_share",
    "https://xhslink.com/o/Example123",
    "https://www.bilibili.com/video/BV1BPpm6sEwQ?p=2",
    "https://b23.tv/Example123",
    "https://m.weibo.cn/status/4189191225395228",
    "https://www.tiktok.com/@patroxofficial/video/6742501081818877190",
    "https://www.instagram.com/reel/Chunk8-jurw/",
    "https://x.com/captainamerica/status/719944021058060289",
    "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/",
])
def test_public_single_video_links_reach_the_platform_router(url):
    assert local.validate_source(url) == url


def test_platform_catalog_distinguishes_routing_from_real_download_validation():
    with TestClient(local.app) as client:
        response = client.get("/api/platforms")
        assert response.status_code == 200
        data = response.json()
        assert {p["id"] for p in data} == {
            "douyin", "xiaohongshu", "bilibili", "weibo",
            "tiktok", "instagram", "twitter", "reddit",
        }
        assert all("validation_status" in p for p in data)


def test_silent_video_is_a_valid_download_when_the_source_has_no_audio(tmp_path):
    video = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=black:s=64x64:d=0.2", "-c:v", "libx264", "-y", str(video),
    ], check=True, capture_output=True)
    result = local.inspect_video(video, .2, require_audio=False)
    assert result["width"] == 64
    assert result["audio_codec"] is None
    assert result["duration_seconds"] > 0
    with pytest.raises(ValueError):
        local.inspect_video(video, .2, require_audio=True)


@pytest.mark.parametrize("url,info", [
    ("https://www.bilibili.com/video/BV1BPpm6sEwQ?p=2", {"id": "BV1BPpm6sEwQ_p1"}),
    ("https://www.bilibili.com/video/BV1BPpm6sEwQ?p=2", {"id": "BV1BPpm6sEwQ_p1", "display_id": "BV1BPpm6sEwQ"}),
    ("https://www.xiaohongshu.com/explore/674051740000000007027a15", {"id": "6411cf99000000001300b6d9"}),
    ("https://x.com/example/status/123", {"id": "456", "display_id": "789"}),
])
def test_unrelated_video_and_wrong_bilibili_part_are_rejected(url, info):
    from multi_sources import ensure_identity, parse_source
    with pytest.raises(ValueError):
        ensure_identity(parse_source(url), info)


def test_post_identity_can_differ_from_the_embedded_video_identity():
    from multi_sources import ensure_identity, parse_source
    source = parse_source("https://x.com/captainamerica/status/719944021058060289")
    ensure_identity(source, {"id": "717462543795523584", "display_id": "719944021058060289"})


def test_image_only_xiaohongshu_post_is_not_reported_as_a_video():
    from app.xiaohongshu import XiaohongshuNote
    from multi_download import info_for_xhs
    with pytest.raises(ValueError):
        info_for_xhs(XiaohongshuNote("674051740000000007027a15", "图片笔记", "作者", "2026-10-24"))


def test_xiaohongshu_adapter_preserves_verified_streams_and_author_metadata():
    from app.xiaohongshu import RemoteAsset, XiaohongshuNote
    from multi_download import info_for_xhs
    note = XiaohongshuNote(
        "674051740000000007027a15", "视频笔记", "作者", "2026-10-24",
        videos=[RemoteAsset(["https://sns-video-bd.xhscdn.com/example.mp4"], 1,
                            width=1080, height=1920, duration=12.5)],
    )
    info = info_for_xhs(note)
    assert info["id"] == "674051740000000007027a15"
    assert info["uploader"] == "作者"
    assert info["duration"] == 12.5
    assert info["formats"][0]["url"] == "https://sns-video-bd.xhscdn.com/example.mp4"


def test_reloaded_download_history_is_sorted_by_creation_time(monkeypatch):
    monkeypatch.setattr(local, "jobs", {
        "old": {"id": "old", "created_at": "2026-10-23T20:00:00+08:00"},
        "new": {"id": "new", "created_at": "2026-10-24T02:00:00+08:00"},
        "middle": {"id": "middle", "created_at": "2026-10-24T01:00:00+08:00"},
    })
    with TestClient(local.app) as client:
        assert [job["id"] for job in client.get("/api/jobs").json()] == ["new", "middle", "old"]


def test_failed_job_keeps_the_last_observed_access_evidence(monkeypatch, tmp_path):
    import multi_download
    from multi_sources import DownloadIssue

    def unavailable_video(value, update, **kwargs):
        update(access_evidence={"http_status": 200, "page_excerpt": "安全限制"})
        raise DownloadIssue("没有视频流", "media_unavailable")

    monkeypatch.setattr(multi_download, "download_multiplatform", unavailable_video)
    monkeypatch.setattr(local, "STATE", tmp_path)
    monkeypatch.setattr(local, "jobs", {"example": {"id": "example", "status": "排队"}})
    local.run_job("example", "https://www.xiaohongshu.com/explore/674051740000000007027a15")
    saved = __import__("json").loads((tmp_path / "example.json").read_text(encoding="utf-8"))
    assert saved["status"] == "失败"
    assert saved["error_code"] == "media_unavailable"
    assert saved["access_evidence"] == {"http_status": 200, "page_excerpt": "安全限制"}


def test_platform_status_keeps_both_successful_and_failed_runs():
    from scripts.multiplatform_live_validation import build_catalog
    reports = [
        {"started_at": "2026-10-24T00:30:00+08:00", "cases": [
            {"job_id": "good", "platform": "douyin", "download_verified": True, "status": "已完成"},
        ]},
        {"started_at": "2026-10-24T00:40:00+08:00", "cases": [
            {"job_id": "bad", "platform": "douyin", "download_verified": False,
             "status": "失败", "error_code": "access_rejected"},
        ]},
    ]
    result = build_catalog(reports)
    assert result["douyin"]["status"] == "有波动"
    assert result["douyin"]["passed"] == 1
    assert result["douyin"]["tested"] == 2
    assert result["douyin"]["last_status"] == "失败"
    assert result["bilibili"]["status"] == "未验证"


def test_each_download_job_has_its_own_archive(tmp_path):
    from multi_download import archive_directory
    first = archive_directory(tmp_path, "bilibili", "BV1BPpm6sEwQ", archive_id="a" * 32)
    second = archive_directory(tmp_path, "bilibili", "BV1BPpm6sEwQ", archive_id="b" * 32)
    first.mkdir(parents=True)
    (first / "metadata.json").write_text("first download", encoding="utf-8")
    second.mkdir(parents=True)
    (second / "metadata.json").write_text("second download", encoding="utf-8")
    assert (first / "metadata.json").read_text(encoding="utf-8") == "first download"
    assert first != second
    assert tmp_path.resolve() in first.resolve().parents


def test_av_link_is_bound_using_the_extractors_legacy_work_and_part_id():
    from multi_sources import ensure_identity, parse_source
    source = parse_source("https://www.bilibili.com/video/av8903802?p=2")
    ensure_identity(source, {"id": "BV1xx411c7mD_p2", "_old_archive_ids": ["bilibili 8903802_part2"]})
    with pytest.raises(ValueError):
        ensure_identity(source, {"id": "BV1xx411c7mD_p1", "_old_archive_ids": ["bilibili 8903802_part1"]})


def test_sequential_video_fragments_are_never_mistaken_for_independent_posts():
    from multi_sources import parse_source
    from multi_download import select_post_video
    source = parse_source("https://www.bilibili.com/video/BV1BPpm6sEwQ")
    fragmented = {"id": "BV1BPpm6sEwQ", "_type": "multi_video", "duration": 20, "entries": [
        {"id": "fragment1", "duration": 10, "formats": [{"url": "https://example.com/first.flv"}]},
        {"id": "fragment2", "duration": 10, "formats": [{"url": "https://example.com/second.flv"}]},
    ]}
    with pytest.raises(ValueError):
        select_post_video(source, fragmented)


def test_douyin_short_link_cannot_redirect_to_another_supported_platform(monkeypatch):
    class Response:
        status_code = 302
        headers = {"Location": "https://m.weibo.cn/status/4189191225395228"}
        def close(self): pass
    monkeypatch.setattr("anonymous_poc.requests.get", lambda *a, **k: Response())
    with pytest.raises(ValueError):
        local.canonical_source("https://v.douyin.com/Example123/")


def test_bilibili_part_numbers_with_leading_zero_are_compared_as_numbers():
    from multi_sources import ensure_identity, parse_source
    source = parse_source("https://www.bilibili.com/video/BV1BPpm6sEwQ?p=02")
    ensure_identity(source, {"id": "BV1BPpm6sEwQ_p2"})


def test_readable_mp4_headers_do_not_make_a_truncated_transfer_successful(tmp_path):
    full = tmp_path / "full.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart", "-y", str(full),
    ], check=True, capture_output=True)
    damaged = tmp_path / "truncated.mp4"
    data = full.read_bytes()
    damaged.write_bytes(data[:len(data)//2])
    with pytest.raises(ValueError):
        local.inspect_video(damaged, 2)


def test_missing_archive_file_is_reported_as_failed_media_validation(tmp_path):
    from multi_sources import DownloadIssue
    with pytest.raises(DownloadIssue) as raised:
        local.inspect_video(tmp_path / "不存在的视频.mp4", 10)
    assert raised.value.code == "media_validation_failed"
