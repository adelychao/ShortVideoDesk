import pytest

from app.workbench.api import VideoPublishOptionsRequest
from app.workbench.video_publishing import SauResult, SocialAutoUploadAdapter, VideoPublishError, validate_douyin_repost_options


def test_repost_fields_survive_api_parsing():
    data = VideoPublishOptionsRequest(declaration="内容为转载信息", repost_source="inside", repost_title="原作品", repost_id="123456789")
    assert data.model_dump(exclude_none=True)["repost_title"] == "原作品"


@pytest.mark.parametrize("options", [
    {"declaration": "内容为转载信息"},
    {"declaration": "内容为转载信息", "repost_source": "inside"},
    {"declaration": "内容为转载信息", "repost_source": "inside", "repost_title": "原作品"},
    {"declaration": "内容为转载信息", "repost_source": "outside", "repost_title": "原作品"},
    {"declaration": "内容由AI生成", "repost_source": "outside"},
])
def test_incomplete_repost_options_are_rejected(options):
    with pytest.raises(VideoPublishError):
        validate_douyin_repost_options(options)


def test_repost_sources_require_explicit_user_choice():
    validate_douyin_repost_options({"declaration": "内容为转载信息", "repost_source": "outside"})
    validate_douyin_repost_options({"declaration": "内容为转载信息", "repost_source": "inside", "repost_title": "原作品", "repost_id": "123456789"})


def test_repost_options_are_forwarded_to_sau(tmp_path):
    adapter = SocialAutoUploadAdapter(None)
    calls = []
    adapter.run = lambda arguments, on_start, timeout: calls.append(arguments) or SauResult(0, "")
    target = {"platform_id": "douyin", "account_alias": "user", "media_path": str(tmp_path / "video.mp4"),
              "title": "新作品", "description": "", "tags": [], "options": {"declaration": "内容为转载信息",
              "repost_source": "inside", "repost_title": "原作品", "repost_id": "123456789"}}
    adapter.upload(target, lambda process: None)
    args = calls[0]
    assert args[args.index("--repost-source") + 1] == "inside"
    assert args[args.index("--repost-title") + 1] == "原作品"
    assert args[args.index("--repost-id") + 1] == "123456789"
