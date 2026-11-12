"""Run supported SAU publishing commands with a check at every browser launch."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def install_browser_guard(browser_type, safety, project: Path) -> None:
    original_launch = browser_type.launch

    async def guarded_launch(self, *args, **kwargs):
        if self.name != 'chromium':
            raise RuntimeError('发布只允许使用受控 Chromium。')
        executable = safety.require_controlled_video_browser(project)
        if executable is None:
            raise RuntimeError('无法确认受控浏览器。')
        kwargs.pop('channel', None)
        kwargs['executable_path'] = str(executable)
        browser = await original_launch(self, *args, **kwargs)
        try:
            safety.require_controlled_video_browser(project)
        except Exception:
            await browser.close()
            raise
        return browser

    async def forbidden(self, *args, **kwargs):
        raise RuntimeError('当前发布不允许连接或启动未经检查的浏览器会话。')

    browser_type.launch = guarded_launch
    browser_type.launch_persistent_context = forbidden
    browser_type.connect = forbidden
    browser_type.connect_over_cdp = forbidden


def main() -> int:
    project = Path.cwd().resolve(strict=True)
    safety_file = Path(__file__).resolve().parents[1] / 'browser_safety.py'
    spec = importlib.util.spec_from_file_location('workbench_publish_browser_safety', safety_file)
    if spec is None or spec.loader is None:
        raise RuntimeError('无法加载浏览器安全检查。')
    safety = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(safety)
    safety.require_controlled_video_browser(project)
    sys.path.insert(0, str(project))
    from patchright.async_api._generated import BrowserType
    install_browser_guard(BrowserType, safety, project)
    args = sys.argv[1:]
    supported_video = len(args) >= 2 and args[0] in {'douyin', 'kuaishou', 'xiaohongshu', 'bilibili', 'tencent', 'baijiahao'} and args[1] == 'upload-video'
    supported_xhs_note = len(args) >= 2 and args[:2] == ['xiaohongshu', 'upload-note']
    if not supported_video and not supported_xhs_note:
        raise RuntimeError('不支持的受控发布命令。')
    if args[:2] == ['xiaohongshu', 'upload-video'] and (project / 'uploader/xiaohongshu_uploader/workbench_video.py').is_file():
        from uploader.xiaohongshu_uploader.workbench_video import main as xhs_main
        sys.argv = [sys.argv[0], *args[2:]]
        return xhs_main()
    from sau_cli import main as sau_main
    return sau_main(args)


if __name__ == '__main__':
    sys.exit(main())
