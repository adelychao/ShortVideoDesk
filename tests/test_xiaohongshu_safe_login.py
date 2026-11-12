import pytest
import uuid

from app import browser_safety
from app.workbench.database import Database
from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand, VideoPublishManager, SauResult, add_video_publish_account, list_video_publish_accounts


@pytest.fixture(autouse=True)
def isolated_login_mutex(monkeypatch):
    monkeypatch.setattr(browser_safety, '_XHS_LOGIN_MUTEX_NAME', f'Local\\VideoWorkbenchTest-{uuid.uuid4().hex}')


def test_xhs_login_uses_only_reserved_browser_path(monkeypatch, tmp_path):
    db = Database(tmp_path / 'data.sqlite')
    db.migrate()
    adapter = SocialAutoUploadAdapter(db)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.video_publishing.reserve_xiaohongshu_login', lambda database, root: None)
    captured = {}
    class FakeProcess:
        returncode = 0
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
        def communicate(self, timeout):
            return ('done', None)
    monkeypatch.setattr('app.workbench.video_publishing.subprocess.Popen', FakeProcess)
    assert adapter.login('xiaohongshu', 'user', lambda process: None).returncode == 0
    assert captured['env']['SAU_XHS_SAFE_LOGIN'] == '1'


def test_locked_or_unknown_session_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: False)
    with pytest.raises(browser_safety.BrowserSafetyError, match='解锁'):
        browser_safety.reserve_xiaohongshu_login(Database(tmp_path / 'db'), tmp_path)


def test_successful_login_does_not_launch_second_check(tmp_path):
    import json
    db = Database(tmp_path / 'data.sqlite')
    db.migrate()
    account = add_video_publish_account(db, 'xiaohongshu')
    manager = VideoPublishManager(db)
    project = tmp_path / 'sau'; project.mkdir(); (project / 'pyproject.toml').write_text('[project]\nname="sau"', encoding='utf-8')
    manager.adapter.set_project_path(str(project))
    def login(platform, alias, on_start):
        (project / 'cookies').mkdir(exist_ok=True)
        state = {'origins': [{'origin': 'https://creator.xiaohongshu.com', 'localStorage': [{'name': 'USER_INFO_FOR_BIZ', 'value': json.dumps({'userId': 'test-id'})}]}]}
        (project / 'cookies' / f'xiaohongshu_{alias}.json').write_text(json.dumps(state), encoding='utf-8')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    manager.adapter.login = login
    manager.adapter.check = lambda *args: pytest.fail('must not launch a second browser')
    manager._run_account_action(account['id'], 'login')
    assert list_video_publish_accounts(db)[0]['status'] == 'connected'


def test_completed_login_can_be_retried_without_waiting(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: True)
    monkeypatch.setattr(browser_safety, '_verified_xhs_browser', lambda root: True)
    database = Database(tmp_path / 'db.sqlite3')
    database.migrate()
    browser_safety.reserve_xiaohongshu_login(database, tmp_path)
    browser_safety.reserve_xiaohongshu_login(database, tmp_path)


def test_account_failures_block_login_before_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_safety, '_is_windows', lambda: True)
    monkeypatch.setattr(browser_safety, '_session_unlocked', lambda: True)
    monkeypatch.setattr(browser_safety, '_account_safe', lambda: False)
    with pytest.raises(browser_safety.BrowserSafetyError, match='暂时无法'):
        browser_safety.reserve_xiaohongshu_login(Database(tmp_path / 'db'), tmp_path)


def test_login_slot_blocks_parallel_start_and_releases_after_completion():
    import threading
    results = []
    with browser_safety.xiaohongshu_login_slot():
        def try_another():
            try:
                with browser_safety.xiaohongshu_login_slot():
                    results.append('opened')
            except browser_safety.BrowserSafetyError:
                results.append('blocked')
        worker = threading.Thread(target=try_another)
        worker.start()
        worker.join(timeout=5)
        assert results == ['blocked']
    with browser_safety.xiaohongshu_login_slot():
        pass


def test_login_slot_is_held_until_login_process_exits(monkeypatch, tmp_path):
    import threading
    db = Database(tmp_path / 'db.sqlite3'); db.migrate()
    adapter = SocialAutoUploadAdapter(db)
    monkeypatch.setattr(browser_safety, '_XHS_LOGIN_MUTEX_NAME', f'Local\\VideoWorkbenchTest-{uuid.uuid4()}')
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.video_publishing.reserve_xiaohongshu_login', lambda database, root: None)
    started, finish = threading.Event(), threading.Event()
    class FakeProcess:
        returncode = 0
        def __init__(self, *args, **kwargs): pass
        def communicate(self, timeout):
            started.set()
            assert finish.wait(timeout=5)
            return ('done', None)
    monkeypatch.setattr('app.workbench.video_publishing.subprocess.Popen', FakeProcess)
    results = []
    first = threading.Thread(target=lambda: results.append(adapter.login('xiaohongshu', 'first', lambda p: None)))
    first.start()
    try:
        assert started.wait(timeout=5)
        second = adapter.login('xiaohongshu', 'second', lambda p: None)
        assert second.launch_error == '其他浏览器任务正在运行，请稍后再试。'
    finally:
        finish.set()
        first.join(timeout=5)
    assert len(results) == 1 and results[0].returncode == 0


def test_old_cooldown_error_is_cleared_on_start(tmp_path):
    db = Database(tmp_path / 'db.sqlite3'); db.migrate()
    account = add_video_publish_account(db, 'xiaohongshu')
    with db.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_accounts SET status='error', message=? WHERE id=?",
                           ('为保护 Windows 账户，请在上次启动 15 分钟后再试。', account['id']))
    manager = VideoPublishManager(db)
    manager.start()
    try:
        refreshed = list_video_publish_accounts(db)[0]
        assert refreshed['status'] == 'not_connected'
        assert not refreshed['message']
    finally:
        manager.stop()
