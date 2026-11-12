"""CSDN browser publishing with conservative verification of the result."""

from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit

from .article_platforms import prepare_csdn_publication
from .csdn_tags import create_csdn_temporary_context


def prepare_csdn_options(options: dict) -> dict:
    settings = options.get("csdn") or {}
    unsupported = (("category", "分类专栏"),
                   ("activity", "活动"), ("topic", "话题"),
                   ("backup_gitcode", "GitCode 备份"), ("cross_publish", "多平台发布"))
    for key, label in unsupported:
        if settings.get(key):
            raise ValueError(f"CSDN 的{label}暂无法自动核验，请清空该设置或在平台手动发布。")
    if settings.get("article_type", "original") != "original" or settings.get("source_url"):
        raise ValueError("CSDN 转载和翻译的来源设置暂无法自动核验，请在平台手动发布。")
    return settings


def ensure_csdn_auto_supported(document: dict, options: dict) -> None:
    prepare_csdn_options(options)
    body = str(document.get("body_markdown") or "")
    local_reference = r"(?:data[:/]|/api/|file:|https?://(?:127\.0\.0\.1|localhost)(?::\d+)?/)"
    local_image = re.search(r"!\[[^\]]*\]\(\s*<?" + local_reference, body, re.I) or re.search(r"<img\b[^>]*\bsrc\s*=\s*['\"]?" + local_reference, body, re.I)
    blocks = document.get("blocks") or []
    if local_image or any(isinstance(block, dict) and block.get("image_path") for block in blocks):
        raise ValueError("CSDN 正文配图暂需在平台手动上传并发表。")


def verify_csdn_article_url(url: str, username: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "blog.csdn.net" or parsed.query or parsed.fragment or not username:
        return None
    match = re.fullmatch(r"/([^/]+)/article/details/(\d+)/?", parsed.path)
    if not match or unquote(match[1]).casefold() != username.casefold():
        return None
    return match[2]


def csdn_submitted_article_id(request: object, response: object, title: str) -> str | None:
    if not isinstance(request, dict) or request.get("pubStatus") != "publish" or request.get("title") != title:
        return None
    if not isinstance(response, dict) or response.get("code") != 200 or not isinstance(response.get("data"), dict):
        return None
    value = response["data"].get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        return None
    return str(value)


def csdn_published_row_url(row: object, username: str, title: str, article_id: str, visibility: str) -> str | None:
    if not isinstance(row, dict) or row.get("title") != title:
        return None
    url, details = row.get("url"), row.get("details")
    if not isinstance(url, str) or not isinstance(details, str) or verify_csdn_article_url(url, username) != article_id:
        return None
    if any(state in details for state in ("审核中", "未通过", "草稿")):
        return None
    if visibility == "public":
        if any(label in details for label in ("仅我可见", "粉丝可见", "VIP可见", "VIP可读")):
            return None
    elif visibility == "private":
        if "仅我可见" not in details:
            return None
    elif visibility == "followers":
        if "粉丝可见" not in details:
            return None
    elif visibility == "vip":
        if not any(label in details for label in ("VIP可见", "VIP可读")):
            return None
    else:
        return None
    return url


def verify_csdn_published_listing(context, username: str, title: str, article_id: str, visibility: str) -> str | None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = context.new_page()
    try:
        page.goto("https://mp.csdn.net/mp_blog/manage/article", wait_until="domcontentloaded", timeout=25000)
        published_tab = page.get_by_role("tab", name=re.compile(r"^已发布"))
        with page.expect_response(
            lambda response: urlsplit(response.url).hostname == "bizapi.csdn.net"
            and urlsplit(response.url).path == "/blog/phoenix/console/v1/article/list"
            and response.status == 200,
            timeout=15000,
        ):
            published_tab.click(timeout=15000)
        page.wait_for_function("el => el.classList.contains('active')", arg=published_tab.element_handle(), timeout=10000)
        rows = page.locator(".article-list-item-mp").filter(has=page.locator(f"a[href*='articleId={article_id}']"))
        try:
            rows.first.wait_for(timeout=10000)
        except PlaywrightTimeoutError:
            return None
        if rows.count() != 1:
            return None
        row = rows.first
        title_link = row.locator(".list-item-title a")
        details = row.locator(".flex-cont")
        browse = row.get_by_role("link", name="浏览", exact=True)
        if title_link.count() != 1 or details.count() != 1 or browse.count() != 1:
            return None
        data = {"title": title_link.inner_text().strip(), "details": details.inner_text(), "url": browse.get_attribute("href") or ""}
        return csdn_published_row_url(data, username, title, article_id, visibility)
    finally:
        page.close()


def validate_csdn_tag_count(tags: list[str], max_tags: int | None) -> None:
    if isinstance(max_tags, bool) or not isinstance(max_tags, int) or not 1 <= max_tags <= 50:
        raise ValueError("无法确认 CSDN 标签数量上限。")
    if not 1 <= len(tags) <= max_tags:
        raise ValueError(f"CSDN 当前允许 1 至 {max_tags} 个标签。")


def is_csdn_default_declaration(value: str) -> bool:
    return value.strip() in {"", "无声明"}


def fill_csdn_title(page, value: str):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.wait_for_function("() => !!document.querySelector('.article-bar__title-display')?.parentElement?.parentElement?.__vue__", timeout=20000)
    title = page.locator(".article-bar__title.article-bar__title--input")
    for _attempt in range(3):
        try:
            if not title.is_visible():
                page.locator(".article-bar__title-display").click(timeout=5000)
            title.wait_for(state="visible", timeout=5000)
            title.fill(value, timeout=5000)
            if title.input_value() == value:
                return title
        except PlaywrightTimeoutError:
            continue
    raise ValueError("CSDN 标题编辑器未能就绪，请稍后重试。")


def _field(dialog, name: str):
    label = dialog.locator("label.form-entry__label").filter(has_text=name)
    if label.count() != 1:
        raise ValueError(f"无法定位 CSDN 的{name}设置，已停止提交。")
    return label.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' form-entry ')][1]")


def _assert_declaration(dialog, expected: str) -> None:
    group = _field(dialog, "创作声明")
    selected = [value.strip() for value in group.locator(".el-select-dropdown__item.selected").all_text_contents()]
    field = group.locator("input")
    if field.count() != 1 or selected != [expected]:
        raise ValueError("无法核验 CSDN 创作声明，已停止提交。")
    value = field.input_value().strip()
    if value != expected and not (expected == "无声明" and value == ""):
        raise ValueError("CSDN 创作声明与所选内容不一致，已停止提交。")


def _set_declaration(dialog, declaration: str) -> None:
    expected = declaration.strip() or "无声明"
    group = _field(dialog, "创作声明")
    options = group.locator(".el-select-dropdown__item")
    if expected not in [value.strip() for value in options.all_text_contents()]:
        raise ValueError("CSDN 当前未提供所选创作声明，请刷新平台选项后重试。")
    selected = [value.strip() for value in group.locator(".el-select-dropdown__item.selected").all_text_contents()]
    if selected != [expected]:
        group.locator("input").click(timeout=5000)
        match = options.filter(has_text=re.compile(r"^\s*" + re.escape(expected) + r"\s*$"))
        if match.count() != 1:
            raise ValueError("无法唯一匹配 CSDN 创作声明，已停止提交。")
        match.click(timeout=5000)
    _assert_declaration(dialog, expected)


def _select_radio(dialog, field: str, value: str) -> None:
    group = _field(dialog, field)
    label = group.locator("label").filter(has_text=value)
    if label.count() != 1:
        raise ValueError(f"无法定位 CSDN 的{field}选项“{value}”。")
    radio_id = label.get_attribute("for")
    if not radio_id or not re.fullmatch(r"[a-zA-Z0-9_-]+", radio_id):
        raise ValueError(f"无法核验 CSDN 的{field}选项。")
    label.click(timeout=5000)
    radio = group.locator(f"input[type=radio][id='{radio_id}']")
    if radio.count() != 1 or not radio.is_checked():
        raise ValueError(f"CSDN 未确认{field}选项“{value}”。")


def _set_tags(page, dialog, tags: list[str]) -> None:
    group = dialog.locator(".mark_selection[typename='tags']")
    if group.count() != 1:
        raise ValueError("无法定位 CSDN 标签选择器。")
    validate_csdn_tag_count(tags, group.evaluate("el => el.__vue__?.maxlen ?? null"))
    group.evaluate("""async el => {
        const vm = el.__vue__;
        for (let i = 0; i < 60 && (!vm?.ArticleTagMarkData || !Object.keys(vm.ArticleTagMarkData).length); i++)
            await new Promise(r => setTimeout(r, 200));
        if (!vm?.ArticleTagMarkData || !Object.keys(vm.ArticleTagMarkData).length) throw new Error('tag catalog unavailable');
        await new Promise(r => setTimeout(r, 700));
    }""")
    existing = group.evaluate("el => Array.from(el.__vue__?.selected || [])")
    if existing:
        for _tag in existing:
            group.locator(".mark_selection_title_el_tag .el-tag__close").first.click(timeout=5000)
        if group.evaluate("el => Array.from(el.__vue__?.selected || [])"):
            raise ValueError("无法清除 CSDN 自动推荐标签，已停止提交。")
    group.locator("button.tag__btn-tag").click(timeout=5000)
    for tag in tags:
        for _attempt in range(2):
            field = group.locator("input[placeholder*='搜索']")
            field.wait_for(timeout=5000)
            field.fill(tag)
            group.evaluate("""(el, value) => new Promise((resolve, reject) => {
                const vm = el.__vue__;
                if (!vm || typeof vm.querySearchAsync !== 'function') return reject(new Error('tag search unavailable'));
                const timer = setTimeout(() => reject(new Error('tag search timed out')), 15000);
                vm.querySearchAsync(value, results => { clearTimeout(timer); resolve(results) });
            })""", tag)
            field.press("Enter")
            page.wait_for_timeout(350)
            if tag in group.evaluate("el => Array.from(el.__vue__?.selected || [])"):
                break
        else:
            raise ValueError(f"CSDN 未接受标签“{tag}”，请检查平台标签后重试。")
    selected = [s.strip().removesuffix("×").strip() for s in group.locator(".mark_selection_title_el_tag .el-tag").all_text_contents()]
    if sorted(selected) != sorted(tags):
        raise ValueError("CSDN 未接受全部指定标签，请在平台检查标签后重试。")
    close = group.locator(".mark_selection_box button[title='关闭']")
    if close.count() and close.first.is_visible():
        close.first.click()


def _set_cover(page, dialog, path: Path) -> None:
    group = _field(dialog, "添加封面")
    file_input = group.locator("input[type=file]")
    if file_input.count() != 1:
        raise ValueError("无法定位 CSDN 封面上传控件，已停止提交。")
    file_input.set_input_files(str(path))
    page.wait_for_timeout(1000)
    crop = dialog.locator(".vue-image-crop-upload:visible")
    if crop.count():
        crop.get_by_text("确认上传", exact=True).click(timeout=5000)
    preview = group.locator(".preview-box.hasImg img.preview")
    preview.wait_for(state="visible", timeout=20000)
    src = preview.get_attribute("src") or ""
    if urlsplit(src).scheme != "https":
        raise ValueError("CSDN 未确认封面上传完成，已停止提交。")


def publish_csdn_article(context, document: dict, options: dict, cover_path: Path | None, before_submit) -> dict:
    isolated = create_csdn_temporary_context(context)
    try:
        return _publish_csdn_in_context(isolated, document, options, cover_path, before_submit)
    finally:
        isolated.close()


def _publish_csdn_in_context(context, document: dict, options: dict, cover_path: Path | None, before_submit) -> dict:
    ensure_csdn_auto_supported(document, options)
    settings = prepare_csdn_options(options)
    data = prepare_csdn_publication(document, options.get("tags") or [])
    cookies = context.cookies("https://csdn.net")
    username = next((unquote(str(c.get("value") or "")) for c in cookies if str(c.get("name", "")).lower() == "username"), "")
    if not username:
        raise ValueError("无法核验 CSDN 登录账号，请重新登录后重试。")
    page = context.new_page()
    page.goto("https://editor.csdn.net/md/", wait_until="domcontentloaded", timeout=25000)
    title = fill_csdn_title(page, data["title"])
    editor = page.locator("pre.editor__inner.markdown-highlighting[contenteditable=true]")
    editor.click(timeout=10000)
    page.keyboard.press("Control+A")
    editor.evaluate("""(el, value) => {
        const escape = s => s.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        document.execCommand('insertHTML', false, value.split('\\n').map(escape).join('<br>'));
    }""", data["body_markdown"])
    if title.input_value() != data["title"] or (editor.text_content() or "").strip() != data["body_markdown"].strip():
        raise ValueError("CSDN 编辑器内容未能完整写入，已停止提交。")
    page.locator("button.btn.btn-publish").first.click(timeout=10000)
    dialog = page.locator(".modal__publish-article:visible")
    dialog.wait_for(timeout=10000)
    _set_tags(page, dialog, data["tags"])
    summary = settings.get("summary", "")
    summary_field = _field(dialog, "文章摘要").locator("textarea")
    summary_field.fill(summary)
    if summary_field.input_value() != summary:
        raise ValueError("CSDN 摘要未能写入，已停止提交。")
    _select_radio(dialog, "文章类型", "原创")
    visibility = {"public": "全部可见", "private": "仅我可见", "followers": "粉丝可见", "vip": "VIP可见"}
    template = {"default": "默认模板", "simple": "简洁模板", "ink": "墨衍模板"}
    _select_radio(dialog, "可见范围", visibility[settings.get("visibility", "public")])
    _select_radio(dialog, "文章模板", template[settings.get("template", "simple")])
    _select_radio(dialog, "多平台发布", "否")
    backup = _field(dialog, "文章备份").locator("input[type=checkbox]")
    if backup.count() != 1 or backup.is_checked():
        raise ValueError("无法确认 CSDN GitCode 备份未启用，已停止提交。")
    _set_declaration(dialog, str(settings.get("declaration") or ""))
    if cover_path is not None:
        _set_cover(page, dialog, cover_path)
    final_tags = dialog.locator(".mark_selection[typename='tags']").evaluate("el => Array.from(el.__vue__?.selected || [])")
    if sorted(final_tags) != sorted(data["tags"]):
        raise ValueError("CSDN 标签在提交前发生变化，已停止提交。")
    _assert_declaration(dialog, str(settings.get("declaration") or "").strip() or "无声明")
    if title.input_value() != data["title"] or (editor.text_content() or "").strip() != data["body_markdown"].strip():
        raise ValueError("CSDN 标题或正文在提交前发生变化，已停止提交。")
    button = dialog.get_by_role("button", name="发布文章", exact=True)
    if button.count() != 1 or not button.is_enabled():
        raise ValueError("CSDN 未允许提交文章，请检查平台提示。")
    existing_urls = {candidate.url for candidate in context.pages}
    responses = []

    def collect_save_response(response) -> None:
        parsed = urlsplit(response.url)
        if parsed.hostname == "bizapi.csdn.net" and parsed.path == "/blog-console-api/v3/mdeditor/saveArticle" and response.request.method == "POST":
            responses.append(response)

    page.on("response", collect_save_response)
    before_submit()
    button.click(timeout=10000)
    deadline = time.monotonic() + 30
    checked_responses = 0
    submitted_id = None
    next_listing_check = 0.0
    while time.monotonic() < deadline:
        while checked_responses < len(responses):
            response = responses[checked_responses]
            checked_responses += 1
            try:
                remote_id = csdn_submitted_article_id(response.request.post_data_json, response.json(), data["title"]) if response.status == 200 else None
            except Exception:
                remote_id = None
            if remote_id:
                submitted_id = remote_id
        if submitted_id and time.monotonic() >= next_listing_check:
            try:
                remote_url = verify_csdn_published_listing(context, username, data["title"], submitted_id, settings.get("visibility", "public"))
            except Exception:
                remote_url = None
            if remote_url:
                return {"status": "published", "remote_id": submitted_id, "remote_url": remote_url, "message": "CSDN 已确认文章发布状态、标题和可见范围。"}
            next_listing_check = time.monotonic() + 5
        for candidate in context.pages:
            if candidate.url in existing_urls:
                continue
            remote_id = verify_csdn_article_url(candidate.url, username)
            if not remote_id:
                continue
            heading = candidate.locator("h1.title-article, h1#articleContentId")
            if heading.count() and heading.first.inner_text().strip() == data["title"]:
                try:
                    remote_url = verify_csdn_published_listing(context, username, data["title"], remote_id, settings.get("visibility", "public"))
                except Exception:
                    remote_url = None
                if remote_url:
                    return {"status": "published", "remote_id": remote_id, "remote_url": remote_url, "message": "CSDN 已确认文章发布状态、标题和可见范围。"}
        page.wait_for_timeout(500)
    return {"status": "unknown", "remote_id": submitted_id, "message": "CSDN 已提交，但未核验到对应文章，请先在平台核对，避免重复发布。"}
