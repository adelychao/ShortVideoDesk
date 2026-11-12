"""Render validated article diagrams in a local, isolated Chromium page."""

from pathlib import Path
from typing import Any

from .database import PROJECT_ROOT


class InfographicRenderError(RuntimeError):
    pass


def infographic_bundle_path() -> Path:
    frontend = PROJECT_ROOT / "frontend"
    for path in (
        frontend / "node_modules" / "@antv" / "infographic" / "dist" / "infographic.min.js",
        frontend / "dist" / "assets" / "infographic.min.js",
    ):
        if path.is_file():
            return path
    raise InfographicRenderError("知识图解组件尚未安装，请在项目目录运行 npm --prefix frontend ci。")


def render_infographic_png(diagram: dict[str, Any], bundle: Path) -> bytes:
    from playwright.sync_api import Error, sync_playwright

    template = Path(__file__).with_suffix(".html")
    custom_renderer = Path(__file__).with_name("article_infographic_diagrams.js")
    try:
        with sync_playwright() as playwright:
            browser = None
            for channel in (None, "msedge", "chrome"):
                try:
                    browser = playwright.chromium.launch(
                        channel=channel, headless=True, timeout=8_000, chromium_sandbox=True,
                    )
                    break
                except Error:
                    continue
            if browser is None:
                raise InfographicRenderError("无法启动知识图解所需的浏览器，请检查 Chromium、Edge 或 Chrome 的安装。")
            try:
                context = browser.new_context(
                    viewport={"width": 1600, "height": 900}, service_workers="block",
                )
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(10_000)
                page.set_content(template.read_text(encoding="utf-8"))
                page.add_script_tag(path=bundle)
                page.add_script_tag(path=custom_renderer)
                page.evaluate("(data) => window.renderArticleInfographic(data)", diagram)
                png = page.locator("main").screenshot(type="png", animations="disabled")
            finally:
                browser.close()
    except (Error, OSError) as exc:
        raise InfographicRenderError("知识图解排版失败，请重试或调整文章内容。") from exc
    if not png or len(png) > 10_000_000:
        raise InfographicRenderError("知识图解 PNG 超出保存限制。")
    return png
