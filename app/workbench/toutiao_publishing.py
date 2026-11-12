"""Conservative browser publishing for Toutiao long-form articles."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .article_platforms import _replace_managed_article_image_reference, markdown_to_html


_COVER_REFERENCE = re.compile(r"data/article-assets/([0-9a-f]{32})/([0-9a-f]{32}\.(?:jpe?g|png|webp))\Z")
_BODY_IMAGE_REFERENCE = re.compile(
    r'''!\[(?:\\.|[^\]])*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))'''
    r'''(?:\s+(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|\((?:\\.|[^)])*\)))?\s*\)'''
)
_BODY_IMAGE_UPLOAD_URL = (
    "https://mp.toutiao.com/mp/agw/article_material/photo/upload_picture"
    "?type=ueditor&pgc_watermark=1&action=uploadimage&encode=utf-8"
)
_DECLARATIONS = (
    "取材网络", "引用站内", "个人观点，仅供参考", "引用AI", "虚构演绎，故事经历",
    "投资观点，仅供参考", "健康医疗分享，仅供参考",
)


def validate_toutiao_title(value: str) -> str:
    title = value.strip()
    if not 2 <= len(title) <= 30:
        raise ValueError("今日头条文章标题须为 2 到 30 个字符。")
    return title


def validate_toutiao_cover_selection(settings: dict) -> list[str]:
    mode = settings.get("cover_mode")
    references = settings.get("cover_image_paths")
    if mode not in {"single", "triple", "none"} or not isinstance(references, list):
        raise ValueError("今日头条封面设置无效，请重新选择。")
    required = {"single": 1, "triple": 3, "none": 0}[mode]
    if len(references) != required or any(not isinstance(item, str) or not _COVER_REFERENCE.fullmatch(item) for item in references):
        raise ValueError("今日头条封面数量或图片来源无效，请重新选择封面。")
    if len(set(references)) != len(references):
        raise ValueError("今日头条三图封面需要选择 3 张不同的图片。")
    article_ids = {_COVER_REFERENCE.fullmatch(item).group(1) for item in references}
    if len(article_ids) > 1:
        raise ValueError("今日头条封面必须来自当前文章的图片库。")
    return list(references)


def prepare_toutiao_options(
    options: dict,
    document: dict,
    *,
    available_declarations: list[str] | tuple[str, ...] = _DECLARATIONS,
    available_collections: list[str] | tuple[str, ...] | None = None,
) -> dict:
    settings = options.get("toutiao")
    if not isinstance(settings, dict):
        raise ValueError("请先填写今日头条专属发表设置。")
    title = validate_toutiao_title(str(options.get("title") or document.get("title") or ""))
    references = validate_toutiao_cover_selection(settings)
    booleans = ("advertising", "original_first", "allow_tips", "sync_micro")
    for key in booleans:
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError("今日头条发表选项无效，请刷新设置后重试。")
    declarations = settings.get("declarations", [])
    if not isinstance(declarations, list) or any(not isinstance(item, str) or not item.strip() for item in declarations):
        raise ValueError("今日头条作品声明无效，请重新选择。")
    declarations = [item.strip() for item in declarations]
    if len(set(declarations)) != len(declarations) or any(item not in available_declarations for item in declarations):
        raise ValueError("所选作品声明不属于头条号当前提供的选项。")
    collection = str(settings.get("collection") or "").strip()
    if collection and (available_collections is None or collection not in available_collections):
        raise ValueError("所选合集未在头条号当前账号的合集列表中确认。")
    location = str(settings.get("location") or "").strip()
    if location:
        raise ValueError("今日头条发布位置尚未接入账号地点搜索，请清空后发布或在平台手动选择。")
    return {
        "title": title,
        "cover_mode": settings["cover_mode"],
        "cover_image_paths": references,
        "advertising": settings.get("advertising", False),
        "original_first": settings.get("original_first", False),
        "allow_tips": settings.get("allow_tips", False),
        "sync_micro": settings.get("sync_micro", False),
        "declarations": declarations,
        "location": "",
        "collection": collection,
    }


def ensure_toutiao_auto_supported(document: dict, settings: dict) -> None:
    body = str(document.get("body_markdown") or "")
    _validate_toutiao_body_images(document, body)
    if re.search(r"^\s*\|.*\|\s*$", body, re.M):
        raise ValueError("今日头条的表格正文暂不支持自动排版，请在平台手动检查后发表。")
    if settings.get("collection"):
        raise ValueError("今日头条合集选择尚未确认可写入文章，请清空合集后发布。")


def _validate_toutiao_body_images(document: dict, body: str) -> list[dict]:
    article_id = str(document.get("_article_id") or "")
    raw_blocks = document.get("blocks")
    blocks = [
        block for block in raw_blocks
        if isinstance(block, dict) and isinstance(block.get("image_path"), str) and block["image_path"]
    ] if isinstance(raw_blocks, list) else []
    if re.search(r"<img\b", body, re.I):
        raise ValueError("今日头条只支持自动上传文章图片库中的正文配图；请先将正文中的 HTML 图片转存到文章图片库。")
    if not blocks:
        if _BODY_IMAGE_REFERENCE.search(body):
            raise ValueError("今日头条正文包含未关联到文章图片库的图片；请重新保存正文配图后发布。")
        return []
    if not re.fullmatch(r"[0-9a-f]{32}", article_id):
        raise ValueError("今日头条正文配图没有关联到当前文章，请重新保存后发布。")

    indexed: dict[str, tuple[dict, str]] = {}
    for block in blocks:
        block_id = str(block.get("id") or "")
        reference = _COVER_REFERENCE.fullmatch(str(block.get("image_path") or ""))
        if not re.fullmatch(r"[\w.-]{1,128}", block_id) or not reference or reference.group(1) != article_id:
            raise ValueError("今日头条正文配图不属于当前文章图片库，请重新选择后发布。")
        indexed[block_id] = (block, reference.group(2))

    for match in _BODY_IMAGE_REFERENCE.finditer(body):
        source = match.group("angle") or match.group("plain") or ""
        parsed = urlsplit(source)
        query = parse_qs(parsed.query, keep_blank_values=True)
        block_id = query.get("slot", [""])[0]
        image = indexed.get(block_id)
        if (
            parsed.scheme or parsed.netloc or parsed.fragment
            or parsed.path != f"/api/v1/articles/{article_id}/images/{image[1] if image else ''}"
            or query != {"slot": [block_id]}
            or image is None
        ):
            raise ValueError("今日头条正文包含外部或未关联到文章图片库的图片；请先转存为文章正文配图。")
    return blocks


def _toutiao_uploaded_image_url(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("state") != "SUCCESS":
        raise ValueError("今日头条没有接受正文配图，请检查图片格式与登录状态。")
    image_url = str(payload.get("url") or "").strip()
    try:
        parsed = urlsplit(image_url)
    except ValueError:
        parsed = None
    host = (parsed.hostname or "").casefold() if parsed else ""
    if (
        parsed is None or parsed.scheme != "https" or not host
        or not any(host == domain or host.endswith("." + domain) for domain in ("toutiaoimg.com", "pstatp.com", "byteimg.com"))
    ):
        raise ValueError("今日头条没有接受正文配图，请检查图片格式与登录状态。")
    return image_url


def _upload_toutiao_body_image(page, path: Path) -> str:
    if not path.is_file() or path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("今日头条正文配图文件不可用或格式不受支持，请重新上传图片。")
    mime_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    }[path.suffix.casefold()]
    try:
        response = page.request.post(
            _BODY_IMAGE_UPLOAD_URL,
            multipart={"upfile": {"name": path.name, "mimeType": mime_type, "buffer": path.read_bytes()}},
            headers={
                "Origin": "https://mp.toutiao.com",
                "Referer": "https://mp.toutiao.com/profile_v4/graphic/publish",
            },
            timeout=30000,
        )
        if not response.ok:
            raise ValueError("今日头条正文配图上传失败，请检查图片格式与登录状态。")
        return _toutiao_uploaded_image_url(response.json())
    except ValueError:
        raise
    except Exception:
        raise ValueError("今日头条正文配图上传请求失败，请检查登录状态和网络后重试。") from None


def _prepare_toutiao_body_html(document: dict, image_paths: list[Path | None], upload_image) -> tuple[str, str, list[str]]:
    body = str(document.get("body_markdown") or "").strip()
    blocks = _validate_toutiao_body_images(document, body)
    if len(blocks) != len(image_paths):
        raise ValueError("今日头条正文配图与本地文件数量不一致，已停止提交。")
    output = body
    replacements: list[tuple[str, str]] = []
    search_from = 0
    uploaded: dict[Path, str] = {}
    for block, path in zip(blocks, image_paths):
        if path is None or not path.is_file():
            raise ValueError("今日头条正文配图文件不可用，请重新上传图片。")
        marker = "TOUTIAO_ARTICLE_IMAGE_" + uuid.uuid4().hex
        output, found = _replace_managed_article_image_reference(
            output, str(document.get("_article_id") or ""), block, marker,
        )
        if not found:
            block_text = str(block.get("text") or "").strip()
            position = output.find(block_text, search_from) if block_text else -1
            if position >= 0:
                end = position + len(block_text)
                output = output[:end] + "\n\n" + marker + "\n\n" + output[end:]
                search_from = end + len(marker) + 4
            else:
                output = output.rstrip() + "\n\n" + marker
                search_from = len(output)
        image_url = uploaded.get(path)
        if image_url is None:
            image_url = _toutiao_uploaded_image_url({"state": "SUCCESS", "url": upload_image(path)})
            uploaded[path] = image_url
        replacements.append((marker, image_url))

    content_html = markdown_to_html(output)
    image_urls: list[str] = []
    for marker, image_url in replacements:
        image_html = f'<img src="{html.escape(image_url, quote=True)}" alt="正文配图" />'
        content_html = content_html.replace(marker, image_html)
        image_urls.append(image_url)
    expected_text = _expected_plain_text(output)
    return content_html, expected_text, image_urls


def create_toutiao_temporary_context(context):
    cookies = context.cookies()
    temporary = context.browser.new_context(locale="zh-CN", accept_downloads=False)
    allowed = ("toutiao.com", "snssdk.com", "bytedance.com", "pstatp.com", "toutiaoimg.com", "toutiaostatic.com")
    selected = [
        cookie for cookie in cookies
        if any(
            str(cookie.get("domain", "")).lstrip(".") == root
            or str(cookie.get("domain", "")).lstrip(".").endswith("." + root)
            for root in allowed
        )
    ]
    if not any(str(cookie.get("name")) in {"sessionid", "sessionid_ss", "sid_tt"} for cookie in selected):
        temporary.close()
        raise ValueError("未确认今日头条登录状态，请先连接头条号账号。")
    temporary.add_cookies(selected)
    return temporary


def read_toutiao_publication_options(context) -> dict:
    """Read current editor options without accepting a dialog or submitting a change."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    temporary = create_toutiao_temporary_context(context)
    try:
        page = temporary.new_page()
        page.route("**/*", lambda route: route.continue_() if route.request.method in {"GET", "HEAD", "OPTIONS"} else route.abort())
        try:
            page.goto("https://mp.toutiao.com/profile_v4/graphic/publish", wait_until="domcontentloaded", timeout=25000)
        except PlaywrightTimeoutError:
            if not page.url.startswith("https://mp.toutiao.com/"):
                raise ValueError("今日头条创作页未能打开，请检查账号登录状态。") from None
        page.locator(".source-info-wrap").wait_for(state="visible", timeout=20000)
        close = page.locator(".ai-assistant-panel .close-btn")
        if close.count() and close.first.is_visible():
            close.first.click(timeout=3000)
        for _ in range(2):
            notice = page.get_by_text("我知道了", exact=True)
            if notice.count() and notice.first.is_visible():
                notice.first.click(timeout=2500)
        declarations = [
            _normalize_text(label.inner_text())
            for label in page.locator(".source-info-wrap label.byte-checkbox").all()
            if _normalize_text(label.inner_text())
        ]
        collection_button = page.get_by_role("button", name="添加至合集", exact=True)
        if collection_button.count() != 1:
            raise ValueError("无法读取当前头条号的合集选项。")
        collection_button.click(timeout=5000)
        page.wait_for_timeout(250)
        empty = page.get_by_text("暂无合集可选择", exact=False)
        if empty.count() and empty.first.is_visible():
            collections: list[str] = []
        else:
            matches = page.locator(".collection-list .collection-item, .collection-item, [class*=collection] [role=option]").all()
            collections = []
            for item in matches:
                name = _normalize_text(item.inner_text())
                if name and name not in {"确定", "取消", "添加合集"} and name not in collections:
                    collections.append(name)
            if not collections:
                raise ValueError("无法核验当前头条号的合集选项。")
        cancel = page.get_by_role("button", name="取消", exact=True)
        if cancel.count() == 1 and cancel.first.is_visible():
            cancel.click(timeout=3000)
        return {"declarations": declarations, "collections": collections}
    finally:
        temporary.close()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _expected_plain_text(markdown: str) -> str:
    value = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", markdown)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"TOUTIAO_ARTICLE_IMAGE_[0-9a-f]{32}", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*>\s?", "", value, flags=re.M)
    value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", value, flags=re.M)
    value = re.sub(r"(?<!\\)[*_~`]{1,3}", "", value)
    value = value.replace("\\*", "*").replace("\\_", "_")
    return _normalize_text(value)


def _editor_text(page) -> str:
    editor = page.locator("div.ProseMirror[contenteditable='true']")
    if editor.count() != 1:
        raise ValueError("无法定位今日头条正文编辑器，已停止提交。")
    return _normalize_text(editor.inner_text())


def _set_checkbox(page, label_text: str, desired: bool) -> None:
    labels = page.locator("label.byte-checkbox").filter(has_text=re.compile(r"^\s*" + re.escape(label_text)))
    exact = [label for label in labels.all() if _normalize_text(label.inner_text()).startswith(label_text)]
    if len(exact) != 1:
        raise ValueError(f"无法定位头条号“{label_text}”选项，已停止提交。")
    label = exact[0]
    checkbox = label.locator("input[type=checkbox]")
    if checkbox.count() != 1:
        raise ValueError(f"无法读取头条号“{label_text}”状态，已停止提交。")
    if checkbox.is_checked() != desired:
        label.click(timeout=5000)
    if checkbox.is_checked() != desired:
        raise ValueError(f"头条号未确认“{label_text}”状态，已停止提交。")


def _set_radio(page, label_text: str) -> None:
    labels = page.locator("label.byte-radio").filter(has_text=re.compile(r"^\s*" + re.escape(label_text)))
    exact = [label for label in labels.all() if _normalize_text(label.inner_text()) == label_text]
    if len(exact) != 1:
        raise ValueError(f"无法定位头条号“{label_text}”选项，已停止提交。")
    exact[0].click(timeout=5000)
    checked = exact[0].locator(".byte-radio-inner.checked")
    if checked.count() != 1:
        raise ValueError(f"头条号未确认“{label_text}”状态，已停止提交。")


def _set_publish_fields(
    page, document: dict, settings: dict, *, content_html: str | None = None,
    expected_text: str | None = None,
) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    title = page.locator('textarea[placeholder="请输入文章标题（2～30个字）"]')
    title.wait_for(state="visible", timeout=15000)
    title.fill(settings["title"])
    if title.input_value() != settings["title"]:
        raise ValueError("头条号未确认文章标题，已停止提交。")

    markdown = str(document.get("body_markdown") or "").strip()
    expected_text = expected_text if expected_text is not None else _expected_plain_text(markdown)
    editor = page.locator("div.ProseMirror[contenteditable='true']")
    editor.wait_for(state="visible", timeout=15000)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://mp.toutiao.com")
    page.evaluate("""async value => {
        await navigator.clipboard.write([new ClipboardItem({
            'text/html': new Blob([value.html], {type: 'text/html'}),
            'text/plain': new Blob([value.text], {type: 'text/plain'})
        })]);
    }""", {"html": content_html if content_html is not None else markdown_to_html(markdown), "text": expected_text})
    editor.click()
    editor.press("Control+V")
    try:
        page.wait_for_function(
            "({selector,expected}) => { const e=document.querySelector(selector); return !!e && e.innerText.replace(/\\s+/g,' ').trim()===expected; }",
            arg={"selector": "div.ProseMirror[contenteditable='true']", "expected": expected_text},
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        if _editor_text(page) != expected_text:
            raise ValueError("头条号正文与文章内容不一致，已停止提交。")

    cover_label = {"single": "单图", "triple": "三图", "none": "无封面"}[settings["cover_mode"]]
    _set_radio(page, cover_label)
    _set_radio(page, "投放广告赚收益" if settings["advertising"] else "不投放广告")

def _set_publish_flags(page, settings: dict) -> None:
    original = page.locator("label.byte-checkbox").filter(has_text=re.compile(r"^\s*头条首发"))
    if original.count() != 1:
        raise ValueError("无法定位头条号首发选项，已停止提交。")
    _set_checkbox(page, "头条首发", settings["original_first"])
    _set_checkbox(page, "允许赞赏", settings["allow_tips"])
    sync = page.locator(".form-tuwen_wtt_trans label.byte-checkbox")
    if sync.count() != 1:
        raise ValueError("无法定位头条号同步微头条选项，已停止提交。")
    if sync.locator("input").is_checked() != settings["sync_micro"]:
        sync.click(timeout=5000)
    if sync.locator("input").is_checked() != settings["sync_micro"]:
        raise ValueError("头条号未确认同步微头条状态，已停止提交。")

    declaration_root = page.locator(".source-info-wrap")
    if declaration_root.count() != 1:
        raise ValueError("无法定位头条号作品声明选项，已停止提交。")
    actual = {}
    for label in declaration_root.locator("label.byte-checkbox").all():
        checkbox = label.locator("input[type=checkbox]")
        name = _normalize_text(label.inner_text())
        if checkbox.count() != 1 or not name:
            raise ValueError("无法核验头条号作品声明，已停止提交。")
        actual[name] = (label, checkbox)
    if any(name not in actual for name in settings["declarations"]):
        raise ValueError("所选作品声明不属于头条号当前提供的选项。")
    for name, (label, checkbox) in actual.items():
        desired = name in settings["declarations"]
        if checkbox.is_checked() != desired:
            label.click(timeout=5000)
        if checkbox.is_checked() != desired:
            raise ValueError(f"头条号未确认作品声明“{name}”，已停止提交。")


def _upload_cover(page, path: Path, index: int) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.locator(".article-cover-add").nth(index).click(timeout=10000)
    modal = page.locator(".cover-upload-modal:visible, .upload-image-modal:visible, [class*=upload-modal]:visible")
    if modal.count() == 0:
        modal = page.locator("body")
    local = page.get_by_text("本地上传", exact=True)
    if local.count() != 1:
        raise ValueError("无法定位头条号封面上传按钮，已停止提交。")
    local.click(timeout=5000)
    inputs = page.locator("input[type=file]")
    if inputs.count() < 1:
        raise ValueError("无法定位头条号封面文件选择器，已停止提交。")
    inputs.first.set_input_files(str(path))
    try:
        page.get_by_text(re.compile(r"已上传\s*1\s*张图片")).wait_for(timeout=20000)
    except PlaywrightTimeoutError:
        body = page.locator("body").inner_text()
        if "上传失败" in body:
            raise ValueError("头条号封面上传失败；请检查账号权限、图片格式和网络。")
        raise ValueError("未收到头条号封面上传成功确认，已停止提交。")
    confirm = page.get_by_role("button", name="确定", exact=True)
    if confirm.count() != 1:
        raise ValueError("无法确认头条号已选择封面，已停止提交。")
    confirm.click(timeout=5000)
    # The editor renders selected covers as a background in some builds and as
    # an image in others. The authoritative check is the platform's generated
    # publish form, which is inspected before the final submit is authorized.
    page.locator(".article-cover-preview").wait_for(state="visible", timeout=10000)
    return f"uploaded:{index}"


def _prepare_covers(page, settings: dict, resolved_paths: list[Path | None]) -> list[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    count = len(settings["cover_image_paths"])
    if count == 0:
        return []
    add_buttons = page.locator(".article-cover-add")
    try:
        page.wait_for_function(
            "count => document.querySelectorAll('.article-cover-add').length >= count",
            arg=count,
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pass
    if add_buttons.count() < count:
        raise ValueError("头条号封面位与所选模式不一致，已停止提交。")
    uploaded: list[str] = []
    for index, image_path in enumerate(resolved_paths):
        if image_path is None:
            raise ValueError("今日头条封面图片不可用，请重新选择。")
        uploaded.append(_upload_cover(page, image_path, index))
    return uploaded


def _page_has_title(page, title: str) -> bool:
    try:
        return title in page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "pre"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _ImageSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "img":
            return
        source = next((value for name, value in attrs if name.casefold() == "src"), None)
        if source:
            self.sources.append(source)


def _html_plain_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return _normalize_text(" ".join(parser.parts))


def _publish_form_fields(request) -> dict[str, str]:
    try:
        payload = request.post_data_json
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return {str(key): str(value if value is not None else "") for key, value in payload.items()}
    raw = request.post_data or ""
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def _valid_toutiao_publish_form(
    request, expected_title: str, expected_text: str, settings: dict,
    *, save_value: str, expected_body_images: list[str] | None = None,
) -> bool:
    fields = _publish_form_fields(request)
    if fields.get("save") != save_value or fields.get("title") != expected_title:
        return False
    if save_value == "0" and fields.get("is_app_preview") != "1":
        return False
    content = fields.get("content", "")
    if _html_plain_text(content) != expected_text:
        return False
    if expected_body_images is not None:
        image_parser = _ImageSourceParser()
        image_parser.feed(content)
        image_parser.close()
        if image_parser.sources != expected_body_images:
            return False
    expected_flags = {
        "article_ad_type": "3" if settings["advertising"] else "2",
        "claim_exclusive": "1" if settings["original_first"] else "0",
        "praise": "1" if settings["allow_tips"] else "0",
        "disable_praise": "0" if settings["allow_tips"] else "1",
        "govern_forward": "1" if settings["sync_micro"] else "0",
    }
    if any(fields.get(key) != value for key, value in expected_flags.items()):
        return False
    try:
        draft = json.loads(fields.get("draft_form_data", "{}"))
        covers = json.loads(fields.get("pgc_feed_covers", "[]"))
    except json.JSONDecodeError:
        return False
    expected_cover_type = {"single": 2, "triple": 3, "none": 1}[settings["cover_mode"]]
    if not isinstance(draft, dict) or draft.get("coverType") != expected_cover_type:
        return False
    if not isinstance(covers, list) or len(covers) != len(settings["cover_image_paths"]):
        return False
    if any(not isinstance(cover, dict) or not _toutiao_cover_has_identity(cover) for cover in covers):
        return False
    if len({json.dumps(cover, ensure_ascii=False, sort_keys=True) for cover in covers}) != len(covers):
        return False
    return True


def _toutiao_cover_has_identity(cover: dict) -> bool:
    identity_keys = {"url", "uri", "image_url", "image_uri", "web_uri", "origin_uri", "id", "image_id", "imageId"}
    for key, value in cover.items():
        if str(key) not in identity_keys or not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        identity = str(value).strip()
        if not identity:
            continue
        if identity.startswith(("http://", "https://")):
            parsed = urlsplit(identity)
            if parsed.scheme != "https" or not any(
                (parsed.hostname or "").endswith(domain)
                for domain in ("toutiaoimg.com", "pstatp.com", "byteimg.com")
            ):
                continue
        return True
    return False


def _result_id(value: object, title: str | None = None) -> str | None:
    if not isinstance(value, dict):
        return None
    matched_title = False
    ids: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and child == title:
                    matched_title = True
                if key in {"group_id", "groupId", "article_id", "articleId", "item_id", "itemId"} and isinstance(child, (str, int)) and not isinstance(child, bool):
                    if str(child).isdigit():
                        ids.append(str(child))
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    if title is not None and not matched_title:
        return None
    return ids[0] if ids else None


def publish_toutiao_article(
    context, document: dict, settings: dict, resolved_paths: list[Path | None],
    resolved_body_image_paths: list[Path | None], before_submit,
) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    temporary = create_toutiao_temporary_context(context)
    try:
        page = temporary.new_page()
        responses = []
        expected_title = settings["title"]
        expected_text = _expected_plain_text(str(document.get("body_markdown") or "").strip())
        expected_body_image_urls: list[str] = []
        submit_gate = {"authorized": False, "sent": False, "preview_valid": False}

        def publish_gate(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "mp.toutiao.com" or parsed.path != "/mp/agw/article/publish" or route.request.method != "POST":
                return route.continue_()
            if submit_gate["sent"]:
                return route.abort()
            if not submit_gate["authorized"]:
                if _valid_toutiao_publish_form(
                    route.request, expected_title, expected_text, settings,
                    save_value="0", expected_body_images=expected_body_image_urls,
                ):
                    submit_gate["preview_valid"] = True
                return route.abort()
            if not submit_gate["preview_valid"] or not _valid_toutiao_publish_form(
                route.request, expected_title, expected_text, settings,
                save_value="1", expected_body_images=expected_body_image_urls,
            ):
                return route.abort()
            submit_gate["sent"] = True
            return route.continue_()

        page.route("**/*", publish_gate)

        def capture(response):
            parsed = urlsplit(response.url)
            if (
                not submit_gate["sent"]
                or parsed.hostname != "mp.toutiao.com"
                or parsed.path != "/mp/agw/article/publish"
                or response.request.method != "POST"
            ):
                return
            try:
                data = response.json()
                receipt_valid = _valid_toutiao_receipt(data, response.status, expected_title)
                article_id = _result_id(data, expected_title) if receipt_valid else None
                responses.append((article_id, response.status, receipt_valid))
            except Exception:
                return

        page.on("response", capture)
        try:
            page.goto("https://mp.toutiao.com/profile_v4/graphic/publish", wait_until="domcontentloaded", timeout=25000)
        except PlaywrightTimeoutError:
            if not page.url.startswith("https://mp.toutiao.com/"):
                raise ValueError("今日头条创作页未能打开，请检查账号登录状态。") from None
        page.locator('textarea[placeholder="请输入文章标题（2～30个字）"]').wait_for(state="visible", timeout=20000)
        close = page.locator(".ai-assistant-panel .close-btn")
        if close.count() and close.first.is_visible():
            close.first.click(timeout=3000)
        for _ in range(2):
            notice = page.get_by_text("我知道了", exact=True)
            if notice.count() and notice.first.is_visible():
                notice.first.click(timeout=2500)

        content_html, expected_text, expected_body_image_urls = _prepare_toutiao_body_html(
            document, resolved_body_image_paths, lambda path: _upload_toutiao_body_image(page, path),
        )
        _set_publish_fields(page, document, settings, content_html=content_html, expected_text=expected_text)
        _prepare_covers(page, settings, resolved_paths)
        # Cover selection can cause the platform editor to refresh portions of
        # its form. Set these flags after the final cover upload, then verify.
        _set_publish_flags(page, settings)

        if page.locator('textarea[placeholder="请输入文章标题（2～30个字）"]').input_value() != expected_title:
            raise ValueError("头条号文章标题校验未通过，已停止提交。")
        if _editor_text(page) != expected_text:
            raise ValueError("头条号正文复核失败，已停止提交。")
        for label, desired in (
            ("头条首发", settings["original_first"]),
            ("允许赞赏", settings["allow_tips"]),
        ):
            current = page.locator("label.byte-checkbox").filter(has_text=re.compile(r"^\s*" + re.escape(label))).first.locator("input")
            checked = current.is_checked() if current.count() == 1 else None
            if checked != desired:
                raise ValueError(f"头条号“{label}”设置复核失败（期望 {desired}，实际 {checked}），已停止提交。")
        sync_checkbox = page.locator(".form-tuwen_wtt_trans input[type=checkbox]")
        if sync_checkbox.count() != 1 or sync_checkbox.is_checked() != settings["sync_micro"]:
            raise ValueError("头条号同步微头条设置复核失败，已停止提交。")
        chosen_declarations = [
            _normalize_text(label.inner_text())
            for label in page.locator(".source-info-wrap label.byte-checkbox").all()
            if label.locator("input").is_checked()
        ]
        if sorted(chosen_declarations) != sorted(settings["declarations"]):
            raise ValueError("头条号作品声明复核失败，已停止提交。")

        preview = page.get_by_role("button", name="预览并发布", exact=True)
        if preview.count() != 1 or not preview.is_enabled():
            raise ValueError("头条号尚未开放发表按钮，请检查文章和账号状态。")
        preview.click(timeout=10000)
        page.wait_for_timeout(800)
        if not submit_gate["preview_valid"]:
            raise ValueError("头条号未确认预览中的标题、正文、封面和发表设置；尚未提交文章。")
        final_button = page.get_by_role("button", name="确认发布", exact=True)
        if final_button.count() != 1:
            raise ValueError("无法确认头条号最终发表确认按钮；未提交文章。")
        before_submit()
        submit_gate["authorized"] = True
        final_button.click(timeout=10000)
        try:
            page.get_by_text(re.compile(r"发布成功|提交成功|已发布" )).first.wait_for(timeout=20000)
        except PlaywrightTimeoutError:
            pass
        for _attempt in range(20):
            if responses:
                break
            page.wait_for_timeout(250)
        if len(responses) == 1 and responses[0][2]:
            article_id = responses[0][0]
            return {
                "status": "published",
                "remote_id": article_id,
                "remote_url": f"https://www.toutiao.com/article/{article_id}/" if article_id else None,
                "message": f"头条号最终发表接口已返回成功回执；已自动上传正文配图 {len(expected_body_image_urls)} 张。如平台仍在审核，公开展示时间由平台决定。",
            }
        return {
            "status": "unknown",
            "message": "头条号最终提交结果暂未取得可核实回执；请先到头条号内容管理核对，避免重复发表。",
        }
    finally:
        temporary.close()


def _contains_exact_string(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, dict):
        return any(_contains_exact_string(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_string(item, expected) for item in value)
    return False


def _response_success(value: object, status: int) -> bool:
    if not 200 <= status < 300 or not isinstance(value, dict):
        return False
    saw_success_code = False
    for key in ("err_no", "errNo", "error_code", "errorCode", "code", "status_code"):
        if key in value and value[key] not in (None, ""):
            saw_success_code = True
            if value[key] not in (0, "0", 200, "200"):
                return False
    for key in ("success", "published", "publish_success"):
        if key in value:
            if value[key] is True:
                return True
            if value[key] is False:
                return False
    return saw_success_code


def _valid_toutiao_receipt(value: object, status: int, expected_title: str) -> bool:
    return (
        _response_success(value, status)
        and _contains_exact_string(value, expected_title)
        and _result_id(value, expected_title) is not None
    )
