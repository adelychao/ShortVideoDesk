import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import browser_safety
from app.workbench import safe_video_runner
from app.workbench.database import Database
from app.workbench.video_publishing import SauResult, VideoPublishManager


@pytest.mark.parametrize('attempted,status', [(False, 'failed'), (True, 'unknown')])
def test_structured_publish_error_overrides_cover_warning(attempted, status):
    import json
    from app.workbench.video_publishing import _video_publish_failure

    payload = {'platform': 'baijiahao', 'stage': '定时发布', 'submission_attempted': attempted,
               'detail': '排期未确认 https://example.test/upload?token=secret'}
    output = 'WARNING: 封面设置失败\nSAU_VIDEO_ERROR:' + json.dumps(payload) + '\n'
    actual, message = _video_publish_failure('baijiahao', output)
    assert actual == status
    assert '定时发布' in message and '排期未确认' in message
    assert '封面' not in message and 'secret' not in message
    assert _video_publish_failure('douyin', output)[0] == 'unknown'


def test_terminal_publish_error_wins_over_earlier_warning_for_legacy_sau():
    from app.workbench.video_publishing import _video_publish_failure

    status, message = _video_publish_failure(
        'xiaohongshu', 'WARNING: 封面设置失败\n小红书提交结果未确认，请先到平台后台核实。\n'
    )
    assert status == 'unknown'
    assert '提交结果未确认' in message and '封面' not in message


@pytest.mark.parametrize('http_status', [461, 471])
def test_xhs_security_response_keeps_result_unknown_and_directs_to_creator(http_status):
    import json
    from app.workbench.video_publishing import _video_publish_failure

    payload = {'platform': 'xiaohongshu', 'stage': '核对小红书提交回执', 'submission_attempted': True,
               'detail': f'小红书提交接口返回 HTTP {http_status}，无法确认接收结果'}
    status, message = _video_publish_failure('xiaohongshu', 'SAU_VIDEO_ERROR:' + json.dumps(payload))
    assert status == 'unknown'
    assert f'HTTP {http_status}' in message and '验证' in message and '手动提交' in message


def test_xhs_manual_confirmation_keeps_security_response_in_history(tmp_path):
    from app.workbench.video_publishing import confirm_video_publish_target

    db = Database(tmp_path / 'db.sqlite')
    db.migrate()
    prior_message = '核对小红书提交回执：小红书提交接口返回 HTTP 461，无法确认接收结果。请先在平台后台核对作品，再决定是否重试。'
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','xiaohongshu','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique','unused','video.mp4',5,'x','title','unknown','t','t')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,message,attempt_count,created_at,updated_at) VALUES('t','b','a','xiaohongshu','user','unknown',?,1,'t','t')", (prior_message,))
        c.execute("INSERT INTO video_publish_attempts(target_id,attempt_number,status,message,started_at) VALUES('t',1,'unknown',?,'t')", (prior_message,))
    result = confirm_video_publish_target(db, 't', 'not_published', confirmed=True)
    assert result['status'] == 'failed'
    assert '用户已核实' in result['message'] and 'HTTP 461' in result['message']
    assert '手动提交' in result['message']
    with db.transaction() as c:
        attempt = c.execute("SELECT message FROM video_publish_attempts WHERE target_id='t'").fetchone()
    assert 'HTTP 461' in attempt['message']


def test_xhs_presubmit_marker_is_actionable_but_submit_failure_stays_unknown():
    from app.workbench.video_publishing import _xhs_presubmit_diagnostic
    output = 'XHS_PRE_SUBMIT_ERROR:{"stage":"设置定时发布时间"}\n'
    diagnostic = _xhs_presubmit_diagnostic('xiaohongshu', output)
    assert '定时' in diagnostic and '未提交' in diagnostic and '重新编辑' in diagnostic
    assert _xhs_presubmit_diagnostic('douyin', output) is None
    assert _xhs_presubmit_diagnostic('xiaohongshu', 'XHS_PRE_SUBMIT_ERROR:{"stage":"提交发布"}') is None
    topic = _xhs_presubmit_diagnostic('xiaohongshu', 'XHS_PRE_SUBMIT_ERROR:{"stage":"填写标题、正文和话题","topic":"短视频"}')
    assert '短视频' in topic and '未提交' in topic


def test_self_shot_is_reported_before_upload(tmp_path, monkeypatch):
    from app.workbench import video_publishing as publish
    db = Database(tmp_path / 'db.sqlite')
    db.migrate()
    media = tmp_path / 'video.mp4'
    media.write_bytes(b'video')
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','xiaohongshu','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique',?,'video.mp4',5,'x','title','queued','t','t')", (str(media),))
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,options_json,status,created_at,updated_at) VALUES('t','b','a','xiaohongshu','user',?,'queued','t','t')", ('{"xhs_declaration":"self_shot"}',))
    manager = VideoPublishManager(db)
    monkeypatch.setattr(manager.adapter, 'upload', lambda *args: pytest.fail('不能在缺少拍摄信息时上传'))
    manager._run_publish_guarded('t')
    with db.transaction() as c:
        row = c.execute("SELECT status,message FROM video_publish_targets WHERE id='t'").fetchone()
    assert row['status'] == 'failed'
    assert '拍摄地点' in row['message'] and '拍摄日期' in row['message'] and '未提交' in row['message']


def test_video_guard_rejects_locked_session_before_other_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: False)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: pytest.fail('account checked after locked session'))
    with pytest.raises(browser_safety.BrowserSafetyError, match='解锁'):
        browser_safety.require_controlled_video_browser(tmp_path)


def test_video_guard_rejects_account_and_unverified_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: False)
    with pytest.raises(browser_safety.BrowserSafetyError, match='账户'):
        browser_safety.require_controlled_video_browser(tmp_path)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: True)
    monkeypatch.setattr(browser_safety, '_verified_xhs_browser', lambda path: False)
    with pytest.raises(browser_safety.BrowserSafetyError, match='浏览器'):
        browser_safety.require_controlled_video_browser(tmp_path)


def test_runner_checks_every_launch_and_uses_verified_binary(tmp_path):
    browser = SimpleNamespace(close=AsyncMock())
    class BrowserType:
        name = 'chromium'
        async def launch(self, **kwargs):
            self.called = kwargs
            return browser
    checks = []
    safety = SimpleNamespace(require_controlled_video_browser=lambda path: checks.append(path) or Path('verified.exe'))
    safe_video_runner.install_browser_guard(BrowserType, safety, tmp_path)
    instance = BrowserType()
    asyncio.run(instance.launch(channel='chrome', executable_path='untrusted.exe', headless=True))
    assert len(checks) == 2
    assert instance.called['executable_path'] == 'verified.exe'
    assert 'channel' not in instance.called


def test_runner_rejects_browser_launch_when_session_becomes_locked(tmp_path):
    launched = []
    class BrowserType:
        name = 'chromium'
        async def launch(self, **kwargs):
            launched.append(kwargs)
    safety = SimpleNamespace(require_controlled_video_browser=lambda path: (_ for _ in ()).throw(RuntimeError('locked')))
    safe_video_runner.install_browser_guard(BrowserType, safety, tmp_path)
    with pytest.raises(RuntimeError, match='locked'):
        asyncio.run(BrowserType().launch())
    assert not launched


def test_runner_closes_browser_if_session_changes_during_start(tmp_path):
    browser = SimpleNamespace(close=AsyncMock())
    class BrowserType:
        name = 'chromium'
        async def launch(self, **kwargs):
            return browser
    checks = iter([Path('verified.exe'), RuntimeError('locked')])
    def check(path):
        item = next(checks)
        if isinstance(item, Exception):
            raise item
        return item
    safe_video_runner.install_browser_guard(BrowserType, SimpleNamespace(require_controlled_video_browser=check), tmp_path)
    with pytest.raises(RuntimeError, match='locked'):
        asyncio.run(BrowserType().launch())
    browser.close.assert_awaited_once()


def test_runner_rejects_connect_to_unchecked_existing_browser(tmp_path):
    class BrowserType:
        async def launch(self, **kwargs):
            pass
    safe_video_runner.install_browser_guard(BrowserType, object(), tmp_path)
    with pytest.raises(RuntimeError, match='浏览器'):
        asyncio.run(BrowserType().connect_over_cdp('http://localhost'))


def test_locked_queued_target_is_deferred_without_attempt(tmp_path, monkeypatch):
    from app.workbench import video_publishing as publish
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','douyin','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique','v.mp4','v.mp4',1,'x','title','queued','t','t')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','douyin','user','queued','t','t')")
    manager = VideoPublishManager(database)
    monkeypatch.setattr(manager.adapter, 'configured_project_path', lambda: str(tmp_path))
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: (_ for _ in ()).throw(browser_safety.BrowserSafetyError('请先解锁电脑')))
    monkeypatch.setattr(publish, 'xiaohongshu_login_slot', lambda: nullcontext())
    manager._run_publish('t')
    with database.transaction() as c:
        row = c.execute("SELECT status,attempt_count,message FROM video_publish_targets WHERE id='t'").fetchone()
        assert (row['status'], row['attempt_count']) == ('queued', 0)
        assert '解锁' in row['message']
    assert 't' in manager._deferred_publish


@pytest.mark.parametrize('manual_submit', [False, True])
def test_lock_between_claim_and_process_start_requeues_without_attempt(tmp_path, monkeypatch, manual_submit):
    from app.workbench import video_publishing as publish
    if manual_submit and publish.os.name != 'nt':
        pytest.skip('受控小红书窗口仅用于 Windows')
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    media = tmp_path / 'v.mp4'
    media.write_bytes(b'video')
    platform = 'xiaohongshu' if manual_submit else 'kuaishou'
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a',?,'user','connected','t','t')", (platform,))
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique',?,'v.mp4',1,'x','title','queued','t','t')", (str(media),))
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a',?,'user','failed','t','t')", (platform,))
    publish.request_video_publish_retry(database, 't', confirmed=True, xhs_manual_submit=manual_submit)
    manager = VideoPublishManager(database)
    monkeypatch.setattr(manager.adapter, 'configured_project_path', lambda: str(tmp_path))
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: None)
    monkeypatch.setattr(publish, 'xiaohongshu_login_slot', lambda: nullcontext())
    def defer_upload(target, _on_start):
        assert target['xhs_manual_submit'] is manual_submit
        return SauResult(None, '', launch_error='请先解锁', safety_blocked=True)
    monkeypatch.setattr(manager.adapter, 'upload', defer_upload)
    manager._run_publish('t')
    with database.transaction() as c:
        row = c.execute("SELECT status,attempt_count,message FROM video_publish_targets WHERE id='t'").fetchone()
        assert (row['status'], row['attempt_count']) == ('queued', 0)
        assert '解锁' in row['message']
        assert c.execute("SELECT COUNT(*) FROM video_publish_attempts WHERE target_id='t'").fetchone()[0] == 0
    assert 't' in manager._deferred_publish
    assert publish._claim_video_publish_target(database, 't')['xhs_manual_submit'] is manual_submit


def test_unadapted_platform_remains_paused_without_attempt(tmp_path, monkeypatch):
    from app.workbench import video_publishing as publish
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','weibo','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique','v.mp4','v.mp4',1,'x','title','queued','t','t')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','weibo','user','queued','t','t')")
    manager = VideoPublishManager(database)
    monkeypatch.setattr(manager.adapter, 'configured_project_path', lambda: str(tmp_path))
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: None)
    monkeypatch.setattr(publish, 'xiaohongshu_login_slot', lambda: nullcontext())
    manager._run_publish('t')
    with database.transaction() as c:
        row = c.execute("SELECT status,attempt_count,message FROM video_publish_targets WHERE id='t'").fetchone()
        assert (row['status'], row['attempt_count']) == ('queued', 0)
        assert '暂停' in row['message']


def test_deferred_target_resumes_once_when_session_is_available(tmp_path, monkeypatch):
    from app.workbench import video_publishing as publish
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    media = tmp_path / 'v.mp4'
    media.write_bytes(b'video')
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','kuaishou','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique',?,'v.mp4',1,'x','title','queued','t','t')", (str(media),))
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','kuaishou','user','queued','t','t')")
    manager = VideoPublishManager(database)
    monkeypatch.setattr(manager.adapter, 'configured_project_path', lambda: str(tmp_path))
    monkeypatch.setattr(publish, 'xiaohongshu_login_slot', lambda: nullcontext())
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: (_ for _ in ()).throw(browser_safety.BrowserSafetyError('锁屏')))
    calls = []
    monkeypatch.setattr(manager.adapter, 'upload', lambda *args: calls.append(1) or SauResult(0, ''))
    manager._run_publish('t')
    assert not calls
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: None)
    manager._resume_deferred_once()
    action, target_id = manager._queue.get_nowait()
    assert (action, target_id) == ('publish', 't')
    manager._run_publish(target_id)
    manager._resume_deferred_once()
    assert manager._queue.empty() and len(calls) == 1
    with database.transaction() as c:
        assert tuple(c.execute("SELECT status,attempt_count FROM video_publish_targets WHERE id='t'").fetchone()) == ('pending_review', 1)


def test_wechat_channels_target_enters_controlled_publish_path_on_windows(tmp_path, monkeypatch):
    import os
    from app.workbench import video_publishing as publish
    if os.name != 'nt':
        pytest.skip('Windows controlled-publish behavior')
    database = Database(tmp_path / 'db.sqlite')
    database.migrate()
    media = tmp_path / 'v.mp4'
    media.write_bytes(b'video')
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','wechat_channels','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique',?,'v.mp4',5,'x','title','queued','t','t')", (str(media),))
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','wechat_channels','user','queued','t','t')")
    manager = VideoPublishManager(database)
    monkeypatch.setattr(manager.adapter, 'configured_project_path', lambda: str(tmp_path))
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: None)
    monkeypatch.setattr(publish, 'xiaohongshu_login_slot', lambda: nullcontext())
    calls = []
    monkeypatch.setattr(manager.adapter, 'upload', lambda target, on_start: calls.append(target) or SauResult(0, 'submitted'))

    manager._run_publish('t')

    assert len(calls) == 1
    with database.transaction() as c:
        row = c.execute("SELECT status,attempt_count FROM video_publish_targets WHERE id='t'").fetchone()
    assert tuple(row) == ('pending_review', 1)


def test_tencent_video_upload_uses_controlled_runner_on_windows(tmp_path, monkeypatch):
    import os
    from app.workbench import video_publishing as publish
    if os.name != 'nt':
        pytest.skip('Windows controlled-publish behavior')
    project = tmp_path / 'sau'
    python = project / '.venv' / 'Scripts' / 'python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    adapter = publish.SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (publish.SauCommand(('sau',), project, str(project)), ''))
    monkeypatch.setattr(publish, 'require_safe_browser', lambda operation: pytest.fail('tencent upload bypassed controlled runner'))
    monkeypatch.setattr(publish, 'require_controlled_video_browser', lambda path: None)
    calls = []

    class FakeProcess:
        returncode = 0
        def __init__(self, args, **kwargs):
            calls.append(args)
        def communicate(self, timeout):
            return 'submitted', ''

    monkeypatch.setattr(publish.subprocess, 'Popen', FakeProcess)
    adapter._run_command(['tencent', 'upload-video', '--account', 'user'], lambda process: None, 10, interactive_terminal=False, xhs_login=False)

    assert calls[0][0] == str(python)
    assert calls[0][1].endswith('safe_video_runner.py')
    assert calls[0][2:4] == ['tencent', 'upload-video']


def test_safe_runner_dispatches_tencent_video_upload(tmp_path, monkeypatch):
    import sys
    import types

    project = tmp_path / 'sau'
    project.mkdir()
    checks = []
    fake_safety = SimpleNamespace(require_controlled_video_browser=lambda path: checks.append(path))
    fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(safe_video_runner.importlib.util, 'spec_from_file_location', lambda *args: fake_spec)
    monkeypatch.setattr(safe_video_runner.importlib.util, 'module_from_spec', lambda spec: fake_safety)
    monkeypatch.setattr(Path, 'cwd', classmethod(lambda cls: project))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'argv', ['safe_video_runner.py', 'tencent', 'upload-video', '--draft'])

    generated = types.ModuleType('patchright.async_api._generated')
    class BrowserType:
        async def launch(self, **kwargs):
            return None
    generated.BrowserType = BrowserType
    patchright = types.ModuleType('patchright')
    async_api = types.ModuleType('patchright.async_api')
    async_api._generated = generated
    patchright.async_api = async_api
    monkeypatch.setitem(sys.modules, 'patchright', patchright)
    monkeypatch.setitem(sys.modules, 'patchright.async_api', async_api)
    monkeypatch.setitem(sys.modules, 'patchright.async_api._generated', generated)

    dispatched = []
    sau_cli = types.ModuleType('sau_cli')
    sau_cli.main = lambda args: dispatched.append(args) or 23
    monkeypatch.setitem(sys.modules, 'sau_cli', sau_cli)

    assert safe_video_runner.main() == 23
    assert dispatched == [['tencent', 'upload-video', '--draft']]
    assert checks == [project.resolve()]
