"""Read article tag options through the signed-in CSDN editor session."""

from typing import Any
from urllib.parse import urlsplit


def csdn_session_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"csdn.net", "editor.csdn.net", "bizapi.csdn.net", "passport.csdn.net", "csdnimg.cn"}
    return [cookie for cookie in cookies if str(cookie.get("domain") or "").lstrip(".").lower() in allowed]


def create_csdn_temporary_context(context: Any) -> Any:
    browser = context.browser
    if browser is None:
        raise ValueError("无法创建临时 CSDN 会话。")
    isolated = browser.new_context(locale="zh-CN", accept_downloads=False)
    try:
        isolated.add_cookies(csdn_session_cookies(context.cookies()))
    except Exception:
        isolated.close()
        raise
    return isolated


def is_csdn_tag_read_only_request(method: str, url: str) -> bool:
    parsed = urlsplit(url)
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    host = parsed.hostname or ""
    if host != "csdn.net" and not host.endswith(".csdn.net"):
        return True
    return (host, parsed.path) in {
        ("bizapi.csdn.net", "/blog/phoenix/console/v1/tag/get-recommend-tags"),
        ("bizapi.csdn.net", "/blog/phoenix/console/v1/tag/search-recommend-tag"),
        ("bizapi.csdn.net", "/blog/phoenix/console/v1/article/get-quality-score"),
        ("passport.csdn.net", "/v1/api/check/userstatus"),
    }


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def normalize_csdn_catalog(data: dict[str, Any]) -> dict[str, Any]:
    raw_categories = data.get("categories")
    if not isinstance(raw_categories, dict) or not raw_categories:
        raise ValueError("CSDN 未返回标签分类，请稍后重试。")
    max_tags = data.get("max")
    if isinstance(max_tags, bool) or not isinstance(max_tags, int) or not 1 <= max_tags <= 50:
        raise ValueError("无法确认 CSDN 标签数量上限，请稍后重试。")
    categories = []
    for name, values in raw_categories.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError("CSDN 返回的标签分类格式有变化，请稍后重试。")
        tags = _strings(values)
        if name == "推荐":
            tags = list(dict.fromkeys(_strings(data.get("common")) + tags))
        categories.append({"name": name, "tags": tags})
    raw_results = data.get("search") or []
    if not isinstance(raw_results, list):
        raise ValueError("CSDN 返回的标签搜索结果格式有变化，请稍后重试。")
    if any(not isinstance(item, dict) or not isinstance(item.get("value"), str) for item in raw_results):
        raise ValueError("CSDN 返回的标签搜索结果格式有变化，请稍后重试。")
    results = _strings([item["value"] for item in raw_results])
    level = data.get("level")
    custom_allowed = level >= 3 if isinstance(level, int) and not isinstance(level, bool) else None
    declarations = data.get("declarations", [])
    if not isinstance(declarations, list) or any(not isinstance(item, str) for item in declarations):
        raise ValueError("CSDN 返回的创作声明选项格式有变化，请稍后重试。")
    return {"categories": categories, "max_tags": max_tags, "results": results, "custom_allowed": custom_allowed, "declarations": _strings(declarations)}


def read_csdn_tags(context: Any, document: dict[str, Any], keyword: str) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    isolated = create_csdn_temporary_context(context)
    try:
        page = isolated.new_page()
        # 2026-11-09 19:38:48 zhw：查询页使用临时编辑内容，平台文章接口只允许只读查询。
        page.route("**/*", lambda route: route.continue_() if is_csdn_tag_read_only_request(route.request.method, route.request.url) else route.abort())
        page.goto("https://editor.csdn.net/md/", wait_until="domcontentloaded", timeout=25000)
        group = page.locator(".modal__publish-article:visible .mark_selection[typename='tags']")
        page.locator(".article-bar__title-display").wait_for(timeout=20000)
        title_input = page.locator(".article-bar__title.article-bar__title--input")
        if not title_input.is_visible():
            page.locator(".article-bar__title-display").click(timeout=10000)
        title_input.fill("标签数据读取", timeout=10000)
        page.locator("pre.editor__inner.markdown-highlighting[contenteditable=true]").fill("这是用于读取标签分类的临时内容。", timeout=10000)
        for _attempt in range(3):
            if group.is_visible():
                break
            page.locator("button.btn.btn-publish").first.click(timeout=10000)
            try:
                group.wait_for(timeout=5000)
                break
            except PlaywrightTimeoutError:
                continue
        else:
            raise ValueError("无法打开 CSDN 标签选择器，请确认账号仍然登录。")
        data = group.evaluate("""async (el, args) => {
            const tag = el.__vue__;
            let owner = tag;
            while (owner && typeof owner.$options.methods?.getTagInfo !== 'function') owner = owner.$parent;
            if (!tag || !owner || typeof tag.querySearchAsync !== 'function') throw new Error('tag editor unavailable');
            let categories = null, common = null;
            const fake = {
                $store: {getters: {'file/current': {name: args.title}, 'content/current': {text: args.content}}},
                set ArticleTagMarkData(value) { categories = value },
                set ArticleTagCommonData(value) { common = value },
                set recommendImages(value) {}
            };
            owner.$options.methods.getTagInfo.call(fake);
            for (let i = 0; i < 60 && categories === null; i++) await new Promise(r => setTimeout(r, 200));
            if (categories === null) throw new Error('catalog unavailable');
            let search = [];
            if (args.keyword) search = await new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('search unavailable')), 15000);
                tag.querySearchAsync(args.keyword, values => { clearTimeout(timer); resolve(values) });
            });
            const dialog = el.closest('.modal__publish-article');
            const declarations = Array.from(dialog?.querySelectorAll('.creation-statement-row .el-select-dropdown__item') || [])
                .map(item => item.textContent.trim()).filter(Boolean);
            return {categories, common, max: tag.maxlen, level: tag.blog_level, search, declarations};
        }""", {"title": str(document.get("title") or ""), "content": str(document.get("body_markdown") or ""), "keyword": keyword})
        return normalize_csdn_catalog(data)
    finally:
        isolated.close()
