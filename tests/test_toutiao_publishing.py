from __future__ import annotations

import pytest

from app.workbench import toutiao_publishing
from app.workbench.toutiao_publishing import (
    _response_success,
    _result_id,
    _toutiao_cover_has_identity,
    _valid_toutiao_receipt,
    _valid_toutiao_publish_form,
    ensure_toutiao_auto_supported,
    prepare_toutiao_options,
    validate_toutiao_cover_selection,
    validate_toutiao_title,
)


def _cover(index: int) -> str:
    return f"data/article-assets/{'a' * 32}/{index:032x}.png"


def test_toutiao_title_matches_editor_limits():
    assert validate_toutiao_title("表达方法学了一堆") == "表达方法学了一堆"
    with pytest.raises(ValueError, match="2 到 30"):
        validate_toutiao_title("甲")
    with pytest.raises(ValueError, match="2 到 30"):
        validate_toutiao_title("甲" * 31)


@pytest.mark.parametrize(
    ("mode", "references", "valid"),
    [
        ("single", [_cover(1)], True),
        ("triple", [_cover(1), _cover(2), _cover(3)], True),
        ("none", [], True),
        ("single", [], False),
        ("triple", [_cover(1), _cover(2)], False),
        ("triple", [_cover(1), _cover(1), _cover(3)], False),
        ("none", [_cover(1)], False),
    ],
)
def test_toutiao_cover_selection_requires_distinct_refs_matching_mode(mode, references, valid):
    settings = {"cover_mode": mode, "cover_image_paths": references}
    if valid:
        assert validate_toutiao_cover_selection(settings) == references
    else:
        with pytest.raises(ValueError, match="封面"):
            validate_toutiao_cover_selection(settings)


def test_toutiao_never_enables_monetization_or_declarations_by_default():
    settings = prepare_toutiao_options(
        {"title": "表达方法学了一堆", "toutiao": {"cover_mode": "single", "cover_image_paths": [_cover(1)]}},
        {"title": "文章标题"},
        available_declarations=["取材网络", "引用AI"],
    )
    assert settings["advertising"] is False
    assert settings["original_first"] is False
    assert settings["allow_tips"] is False
    assert settings["sync_micro"] is False
    assert settings["declarations"] == []


def test_toutiao_rejects_unavailable_declaration_and_unverified_options():
    document = {"title": "文章标题"}
    with pytest.raises(ValueError, match="声明"):
        prepare_toutiao_options(
            {"title": "文章标题", "toutiao": {"cover_mode": "none", "cover_image_paths": [], "declarations": ["引用AI"]}},
            document,
            available_declarations=["取材网络"],
        )
    with pytest.raises(ValueError, match="合集"):
        prepare_toutiao_options(
            {"title": "文章标题", "toutiao": {"cover_mode": "none", "cover_image_paths": [], "collection": "未读取的合集"}},
            document,
            available_declarations=[],
            available_collections=[],
        )


def test_toutiao_rejects_local_media_and_unknown_declaration_data():
    with pytest.raises(ValueError, match="封面"):
        validate_toutiao_cover_selection({"cover_mode": "single", "cover_image_paths": ["/api/v1/articles/a/covers/x"]})
    with pytest.raises(ValueError, match="作品声明"):
        prepare_toutiao_options(
            {"title": "文章标题", "toutiao": {"cover_mode": "single", "cover_image_paths": [_cover(1)], "declarations": ["invented"]}},
            {"title": "文章标题"},
            available_declarations=["取材网络"],
        )


def test_toutiao_submission_receipt_requires_matching_title_and_success_code():
    assert _result_id({"data": {"title": "目标文章", "group_id": "123456"}}, "目标文章") == "123456"
    assert _result_id({"data": {"title": "另一篇文章", "group_id": "123456"}}, "目标文章") is None
    assert _response_success({"err_no": 0}, 200)
    assert not _response_success({"err_no": 1001}, 200)
    assert not _response_success({"err_no": 0}, 500)
    assert not _response_success({}, 200)
    assert _valid_toutiao_receipt({"err_no": 0, "data": {"title": "目标文章", "group_id": "123456"}}, 200, "目标文章")
    assert not _valid_toutiao_receipt({"err_no": 0, "data": {"title": "另一篇文章", "group_id": "123456"}}, 200, "目标文章")
    assert not _valid_toutiao_receipt({"err_no": 0, "data": {"group_id": "123456"}}, 200, "目标文章")


def test_toutiao_final_request_gate_checks_content_settings_and_cover_mode():
    from types import SimpleNamespace

    settings = {"cover_mode": "none", "cover_image_paths": [], "advertising": False,
                "original_first": False, "allow_tips": False, "sync_micro": False}
    fields = {
        "save": "1", "title": "文章标题", "content": "<p>正文</p>",
        "article_ad_type": "2", "claim_exclusive": "0", "praise": "0",
        "disable_praise": "1", "govern_forward": "0",
        "draft_form_data": '{"coverType":1}', "pgc_feed_covers": "[]",
    }
    assert _valid_toutiao_publish_form(SimpleNamespace(post_data_json=fields), "文章标题", "正文", settings, save_value="1")
    assert not _valid_toutiao_publish_form(SimpleNamespace(post_data_json={**fields, "save": "0"}), "文章标题", "正文", settings, save_value="1")
    assert not _valid_toutiao_publish_form(SimpleNamespace(post_data_json={**fields, "article_ad_type": "3"}), "文章标题", "正文", settings, save_value="1")
    assert not _valid_toutiao_publish_form(SimpleNamespace(post_data_json={**fields, "content": "<p>其他正文</p>"}), "文章标题", "正文", settings, save_value="1")


def test_toutiao_preview_request_must_be_the_platform_preview_and_cover_has_identity():
    from types import SimpleNamespace

    settings = {"cover_mode": "single", "cover_image_paths": [_cover(1)], "advertising": False,
                "original_first": False, "allow_tips": False, "sync_micro": False}
    fields = {
        "save": "0", "is_app_preview": "1", "title": "文章标题", "content": "<p>正文</p>",
        "article_ad_type": "2", "claim_exclusive": "0", "praise": "0",
        "disable_praise": "1", "govern_forward": "0",
        "draft_form_data": '{"coverType":2}',
        "pgc_feed_covers": '[{"url":"https://p3.toutiaoimg.com/cover.png"}]',
    }
    assert _valid_toutiao_publish_form(SimpleNamespace(post_data_json=fields), "文章标题", "正文", settings, save_value="0")
    assert not _valid_toutiao_publish_form(SimpleNamespace(post_data_json={**fields, "is_app_preview": "0"}), "文章标题", "正文", settings, save_value="0")
    body_image_url = "https://p3.toutiaoimg.com/body.png"
    second_body_image_url = "https://p3.toutiaoimg.com/body-2.png"
    body_image_fields = {
        **fields,
        "content": f'<p>正文</p><p><img src="{body_image_url}" /></p><p><img src="{second_body_image_url}" /></p>',
    }
    assert _valid_toutiao_publish_form(
        SimpleNamespace(post_data_json=body_image_fields), "文章标题", "正文", settings,
        save_value="0", expected_body_images=[body_image_url, second_body_image_url],
    )
    assert not _valid_toutiao_publish_form(
        SimpleNamespace(post_data_json=body_image_fields), "文章标题", "正文", settings,
        save_value="0", expected_body_images=[second_body_image_url, body_image_url],
    )
    assert _toutiao_cover_has_identity({"image_id": "tos-cn-i-001"})
    assert not _toutiao_cover_has_identity({"url": "https://example.com/cover.png"})
    assert not _toutiao_cover_has_identity({"width": 720, "height": 400})


def test_toutiao_allows_managed_article_images_for_automatic_upload():
    article_id = "a" * 32
    block_id = "paragraph-1"
    filename = "b" * 32 + ".png"
    document = {
        "_article_id": article_id,
        "body_markdown": f"正文段落\n\n![正文配图](/api/v1/articles/{article_id}/images/{filename}?slot={block_id})",
        "blocks": [{
            "id": block_id, "type": "paragraph", "text": "正文段落",
            "image_path": f"data/article-assets/{article_id}/{filename}",
        }],
    }

    ensure_toutiao_auto_supported(document, {"collection": ""})


@pytest.mark.parametrize("suffix", ["png", "jpeg"])
def test_toutiao_body_image_is_uploaded_and_embedded_in_order(tmp_path, suffix):
    article_id = "a" * 32
    block_id = "paragraph-1"
    filename = "b" * 32 + "." + suffix
    path = tmp_path / filename
    path.write_bytes(b"png")
    document = {
        "_article_id": article_id,
        "body_markdown": f"正文段落\n\n![正文配图](/api/v1/articles/{article_id}/images/{filename}?slot={block_id})\n\n后续段落",
        "blocks": [{
            "id": block_id, "type": "paragraph", "text": "正文段落",
            "image_path": f"data/article-assets/{article_id}/{filename}",
        }],
    }
    uploaded = []

    content_html, expected_text, image_urls = toutiao_publishing._prepare_toutiao_body_html(
        document, [path], lambda image: uploaded.append(image) or "https://p3.toutiaoimg.com/body.png",
    )

    assert uploaded == [path]
    assert content_html.index("正文段落") < content_html.index("https://p3.toutiaoimg.com/body.png") < content_html.index("后续段落")
    assert "/api/v1/articles/" not in content_html
    assert expected_text == "正文段落 后续段落"
    assert image_urls == ["https://p3.toutiaoimg.com/body.png"]


def test_toutiao_body_image_upload_response_requires_success_and_platform_url():
    assert toutiao_publishing._toutiao_uploaded_image_url({
        "state": "SUCCESS", "url": "https://p3.toutiaoimg.com/body.png",
    }) == "https://p3.toutiaoimg.com/body.png"
    with pytest.raises(ValueError, match="没有接受"):
        toutiao_publishing._toutiao_uploaded_image_url({"state": "ERROR", "url": "https://p3.toutiaoimg.com/body.png"})
    with pytest.raises(ValueError, match="没有接受"):
        toutiao_publishing._toutiao_uploaded_image_url({"state": "SUCCESS", "url": "https://example.com/body.png"})


def test_toutiao_rejects_unmanaged_body_images_and_collections():
    with pytest.raises(ValueError, match="正文配图"):
        ensure_toutiao_auto_supported({"body_markdown": "![插图](https://example.com/image.png)"}, {"collection": ""})
    with pytest.raises(ValueError, match="合集"):
        ensure_toutiao_auto_supported({"body_markdown": "正文"}, {"collection": "已有合集"})


def test_toutiao_verified_user_selected_declaration_stays_eligible_for_automatic_publish():
    document = {"title": "文章标题", "body_markdown": "正文"}
    options = {
        "title": "文章标题",
        "toutiao": {
            "cover_mode": "none",
            "cover_image_paths": [],
            "declarations": ["取材网络"],
        },
    }
    settings = prepare_toutiao_options(
        options,
        document,
        available_declarations=["取材网络"],
        available_collections=[],
    )

    ensure_toutiao_auto_supported(document, settings)

    assert settings["declarations"] == ["取材网络"]
