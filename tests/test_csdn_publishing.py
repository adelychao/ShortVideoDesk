import pytest

from app.workbench.csdn_publishing import _set_declaration, _set_tags, csdn_published_row_url, csdn_submitted_article_id, ensure_csdn_auto_supported, fill_csdn_title, is_csdn_default_declaration, prepare_csdn_options, validate_csdn_tag_count, verify_csdn_article_url
from app.workbench.article_platforms import prepare_csdn_publication
from app.workbench.csdn_tags import csdn_session_cookies, is_csdn_tag_read_only_request, normalize_csdn_catalog


def test_reject_unverified_csdn_settings_before_opening_browser():
    with pytest.raises(ValueError, match="分类专栏"):
        prepare_csdn_options({"tags": ["Python"], "csdn": {"category": "自定义"}})
    with pytest.raises(ValueError, match="多平台发布"):
        prepare_csdn_options({"tags": ["Python"], "csdn": {"cross_publish": True}})


def test_csdn_local_body_images_require_manual_handoff():
    for document in ({"body_markdown": "![图](data/article-assets/a.png)"}, {"body_markdown": '<img src="/api/v1/articles/a/images/x.png">'}, {"body_markdown": "正文", "blocks": [{"image_path": "data/article-assets/a.png"}]}):
        with pytest.raises(ValueError, match="正文配图"):
            ensure_csdn_auto_supported(document, {"tags": ["Python"], "csdn": {}})


def test_csdn_receipt_must_be_a_detail_page_for_the_logged_in_account():
    assert verify_csdn_article_url("https://blog.csdn.net/example/article/details/12345", "example") == "12345"
    assert verify_csdn_article_url("https://blog.csdn.net/other/article/details/12345", "example") is None
    assert verify_csdn_article_url("https://evil.example/example/article/details/12345", "example") is None
    assert verify_csdn_article_url("https://blog.csdn.net/example/article/details/12345?x=1", "example") is None


def test_csdn_supports_seven_tags_for_current_platform_limit():
    tags = [f"标签{i}" for i in range(7)]
    assert prepare_csdn_publication({"title": "标题", "body_markdown": "正文"}, tags)["tags"] == tags


def test_csdn_catalog_preserves_platform_categories_search_results_and_limit():
    result = normalize_csdn_catalog({"categories": {"推荐": ["机器学习"], "Python": ["python", "django"]}, "common": ["python"], "max": 7, "level": 3, "search": [{"value": "python"}, {"value": "ipython"}]})
    assert result["categories"] == [{"name": "推荐", "tags": ["python", "机器学习"]}, {"name": "Python", "tags": ["python", "django"]}]
    assert result["max_tags"] == 7
    assert result["custom_allowed"] is True
    assert result["results"] == ["python", "ipython"]


def test_csdn_catalog_exposes_only_declarations_returned_by_platform():
    values = ["无声明", "部分内容由AI辅助生成", "内容来源网络，进行整合/再创作", "个人观点，仅供参考"]
    data = {"categories": {"推荐": []}, "max": 7, "declarations": values}
    assert normalize_csdn_catalog(data)["declarations"] == values
    assert normalize_csdn_catalog({"categories": {"推荐": []}, "max": 7})["declarations"] == []
    with pytest.raises(ValueError, match="创作声明"):
        normalize_csdn_catalog({**data, "declarations": ["无声明", {"bad": "value"}]})


def test_csdn_creation_declaration_does_not_force_manual_handoff():
    selected = "部分内容由AI辅助生成"
    assert prepare_csdn_options({"csdn": {"declaration": selected}})["declaration"] == selected


def test_csdn_declaration_selection_is_read_back_and_unknown_option_is_rejected():
    from app.browser_safety import require_safe_browser
    from playwright.sync_api import sync_playwright

    require_safe_browser("public page probe")
    html = '''<div class="modal__publish-article"><div class="form-entry creation-statement-row">
      <label class="form-entry__label">创作声明</label><div class="el-select">
      <input readonly placeholder="无声明"><ul hidden>
      <li class="el-select-dropdown__item selected">无声明</li>
      <li class="el-select-dropdown__item">部分内容由AI辅助生成</li>
      <li class="el-select-dropdown__item">内容来源网络，进行整合/再创作</li>
      <li class="el-select-dropdown__item">个人观点，仅供参考</li></ul></div></div></div>
      <script>const input=document.querySelector('input'),list=document.querySelector('ul');
      input.onclick=()=>list.hidden=false;
      list.onclick=e=>{const li=e.target.closest('li');if(!li)return;list.querySelector('.selected').classList.remove('selected');
      li.classList.add('selected');input.value=li.textContent;list.hidden=true}</script>'''
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            dialog = page.locator(".modal__publish-article")
            _set_declaration(dialog, "")
            for value in ("部分内容由AI辅助生成", "内容来源网络，进行整合/再创作", "个人观点，仅供参考"):
                _set_declaration(dialog, value)
                assert page.locator("input").input_value() == value
            with pytest.raises(ValueError, match="创作声明"):
                _set_declaration(dialog, "平台未提供的声明")
            assert page.locator("input").input_value() == "个人观点，仅供参考"
        finally:
            browser.close()


def test_csdn_catalog_does_not_invent_missing_platform_data():
    with pytest.raises(ValueError, match="标签"):
        normalize_csdn_catalog({"categories": {}, "max": 7})
    with pytest.raises(ValueError, match="数量"):
        normalize_csdn_catalog({"categories": {"推荐": []}, "max": None})
    with pytest.raises(ValueError, match="格式"):
        normalize_csdn_catalog({"categories": {"Python": [{"name": "python"}]}, "max": 7})


def test_csdn_tag_count_is_checked_against_actual_platform_limit():
    validate_csdn_tag_count(["1", "2", "3", "4", "5", "6", "7"], 7)
    with pytest.raises(ValueError, match="7"):
        validate_csdn_tag_count([str(i) for i in range(8)], 7)
    with pytest.raises(ValueError, match="上限"):
        validate_csdn_tag_count(["1"], None)


def test_tag_lookup_blocks_editor_mutations_but_allows_platform_tag_queries():
    assert is_csdn_tag_read_only_request("POST", "https://bizapi.csdn.net/blog/phoenix/console/v1/tag/get-recommend-tags")
    assert is_csdn_tag_read_only_request("POST", "https://bizapi.csdn.net/blog/phoenix/console/v1/tag/search-recommend-tag")
    assert not is_csdn_tag_read_only_request("POST", "https://blog-console-api.csdn.net/v3/editor/saveArticle")
    assert not is_csdn_tag_read_only_request("POST", "https://bizapi.csdn.net/new/unknown-write")
    assert not is_csdn_tag_read_only_request("POST", "https://mp.csdn.net/new/unknown-write")
    assert is_csdn_tag_read_only_request("POST", "https://passport.csdn.net/v1/api/check/userstatus")


def test_temporary_csdn_session_copies_only_csdn_cookies():
    cookies = [{"name": "user", "domain": ".csdn.net"}, {"name": "session", "domain": "editor.csdn.net"}, {"name": "other", "domain": ".example.com"}, {"name": "impostor", "domain": "csdn.net.example.com"}]
    assert [item["name"] for item in csdn_session_cookies(cookies)] == ["user", "session"]


def test_csdn_default_declaration_accepts_platform_default_but_not_other_values():
    assert is_csdn_default_declaration("")
    assert is_csdn_default_declaration("无声明")
    assert not is_csdn_default_declaration("部分内容由AI辅助生成")


def test_csdn_submission_receipt_requires_matching_publish_request():
    response = {"code": 200, "data": {"id": 170365251}}
    assert csdn_submitted_article_id({"pubStatus": "publish", "title": "文章标题"}, response, "文章标题") == "170365251"
    assert csdn_submitted_article_id({"pubStatus": "draft", "title": "文章标题"}, response, "文章标题") is None
    assert csdn_submitted_article_id({"pubStatus": "publish", "title": "其他文章"}, response, "文章标题") is None
    assert csdn_submitted_article_id({"pubStatus": "publish", "title": "文章标题"}, {"code": 500, "data": {"id": 1}}, "文章标题") is None


def test_csdn_published_row_must_match_id_account_title_and_visibility():
    row = {"title": "文章标题", "url": "https://blog.csdn.net/example/article/details/170365251", "details": "原创\n仅我可见"}
    assert csdn_published_row_url(row, "example", "文章标题", "170365251", "private") == row["url"]
    assert csdn_published_row_url(row, "example", "文章标题", "999", "private") is None
    assert csdn_published_row_url(row, "other", "文章标题", "170365251", "private") is None
    assert csdn_published_row_url(row, "example", "其他文章", "170365251", "private") is None
    assert csdn_published_row_url(row, "example", "文章标题", "170365251", "public") is None
    assert csdn_published_row_url({**row, "details": "审核中\n仅我可见"}, "example", "文章标题", "170365251", "private") is None


def test_csdn_title_retries_if_editor_rerenders_before_input_is_ready():
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    class Input:
        ready = False
        value = ""
        def is_visible(self): return self.ready
        def wait_for(self, **_kwargs):
            if not self.ready: raise PlaywrightTimeoutError("rerender")
        def fill(self, value, **_kwargs): self.value = value
        def input_value(self): return self.value

    class Display:
        clicks = 0
        def click(self, **_kwargs):
            self.clicks += 1
            title.ready = self.clicks >= 2

    title, display = Input(), Display()

    class Page:
        def wait_for_function(self, *_args, **_kwargs): return None
        def locator(self, selector): return title if selector.endswith("--input") else display

    fill_csdn_title(Page(), "测试文章标题")
    assert title.value == "测试文章标题"
    assert display.clicks == 2


def test_csdn_waits_for_platform_tag_search_before_pressing_enter():
    class Locator:
        def __init__(self, group, name): self.group, self.name = group, name
        @property
        def first(self): return self
        def count(self): return 0 if self.name == "close" else 1
        def click(self, **_kwargs): pass
        def wait_for(self, **_kwargs): pass
        def fill(self, value): self.group.query, self.group.ready = value, False
        def press(self, key):
            self.group.presses += 1
            if key == "Enter" and self.group.ready and self.group.presses > 1: self.group.selected.append(self.group.query)
        def all_text_contents(self): return self.group.selected

    class Group:
        query = ""
        ready = False
        presses = 0
        selected = []
        def count(self): return 1
        def locator(self, selector):
            if "button[title" in selector: name = "close"
            elif "el-tag" in selector: name = "selected"
            else: name = "input" if "input" in selector else "open"
            return Locator(self, name)
        def evaluate(self, script, *args):
            if "maxlen" in script: return 7
            if "ArticleTagMarkData" in script: return True
            if "querySearchAsync" in script:
                self.ready = True
                return [self.query]
            if ".selected" in script: return self.selected
            raise AssertionError("unexpected evaluation")

    group = Group()

    class Dialog:
        def locator(self, _selector): return group

    class Page:
        def wait_for_timeout(self, _ms): pass

    _set_tags(Page(), Dialog(), ["人工智能", "AI"])
    assert group.selected == ["人工智能", "AI"]
    assert group.presses == 3
