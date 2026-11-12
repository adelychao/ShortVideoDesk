import json

import pytest

from app.workbench.database import Database
from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand, SauResult, VideoPublishManager, add_video_publish_account, list_video_publish_accounts


def write_session(path, *, name='小红书昵称', avatar='https://sns-avatar-qc.xhscdn.com/avatar.png', origin='https://creator.xiaohongshu.com'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'origins': [{'origin': origin, 'localStorage': [{'name': 'USER_INFO_FOR_BIZ', 'value': json.dumps({'userId': '123', 'userName': name, 'userAvatar': avatar})}]}]}), encoding='utf-8')


def test_reads_saved_profile_without_browser_or_cookie_output(monkeypatch, tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.migrate()
    adapter = SocialAutoUploadAdapter(db)
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: True)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.video_publishing.subprocess.Popen', lambda *a, **k: pytest.fail('browser started'))
    write_session(tmp_path / 'cookies/xiaohongshu_test.json')
    assert adapter.read_account_profile('xiaohongshu', 'test', lambda p: None) == {'name': '小红书昵称', 'avatar_url': 'https://sns-avatar-qc.xhscdn.com/avatar.png'}


@pytest.mark.parametrize('origin,avatar', [
    ('https://evil.example', 'https://sns-avatar-qc.xhscdn.com/avatar.png'),
    ('https://creator.xiaohongshu.com', 'https://xhscdn.com.attacker.example/a.png'),
    ('https://creator.xiaohongshu.com', 'http://sns-avatar-qc.xhscdn.com/a.png'),
])
def test_saved_profile_rejects_untrusted_origin_or_avatar(monkeypatch, tmp_path, origin, avatar):
    db = Database(tmp_path / 'db.sqlite'); db.migrate()
    adapter = SocialAutoUploadAdapter(db)
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: True)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    write_session(tmp_path / 'cookies/xiaohongshu_test.json', origin=origin, avatar=avatar)
    assert adapter.read_account_profile('xiaohongshu', 'test', lambda p: None) == {}


def test_connected_xhs_profile_is_restored_at_startup(monkeypatch, tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.migrate()
    account = add_video_publish_account(db, 'xiaohongshu', 'test', '我的备注')
    with db.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (account['id'],))
    write_session(tmp_path / 'cookies/xiaohongshu_test.json')
    manager = VideoPublishManager(db)
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: True)
    monkeypatch.setattr(manager.adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    manager.start()
    try:
        result = list_video_publish_accounts(db)[0]
        assert result['profile_name'] == '小红书昵称'
        assert result['avatar_url'] == 'https://sns-avatar-qc.xhscdn.com/avatar.png'
        assert result['display_name'] == '我的备注'
    finally:
        manager.stop()


def test_successful_login_reads_saved_profile_without_second_browser(monkeypatch, tmp_path):
    db = Database(tmp_path / 'db.sqlite'); db.migrate()
    account = add_video_publish_account(db, 'xiaohongshu', 'test')
    write_session(tmp_path / 'cookies/xiaohongshu_test.json')
    manager = VideoPublishManager(db)
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: True)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="sau"', encoding='utf-8')
    manager.adapter.set_project_path(str(tmp_path))
    monkeypatch.setattr(manager.adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    def login(platform, alias, on_start):
        write_session(tmp_path / f'cookies/xiaohongshu_{alias}.json')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    manager.adapter.login = login
    manager.adapter.check = lambda *a: pytest.fail('a second browser must not start')
    manager._run_account_action(account['id'], 'login')
    result = list_video_publish_accounts(db)[0]
    assert result['status'] == 'connected'
    assert result['profile_name'] == '小红书昵称'
    assert result['avatar_url'] == 'https://sns-avatar-qc.xhscdn.com/avatar.png'
