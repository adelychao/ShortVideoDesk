import hashlib
import json
import threading
from pathlib import Path

import pytest

from app.workbench.database import Database
from app.workbench.video_publishing import (
    SauResult, VideoPublishError, VideoPublishManager, add_video_publish_account, list_video_publish_accounts,
    latest_video_publish_login_qr, request_video_publish_account_action,
    video_publish_platforms,
)


def write_session(root: Path, alias: str, user_id: str, name: str = '相同昵称') -> Path:
    path = root / 'cookies' / f'xiaohongshu_{alias}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {'userId': user_id, 'userName': name, 'userAvatar': 'https://sns-avatar-qc.xhscdn.com/a.jpg'}
    path.write_text(json.dumps({'origins': [{'origin': 'https://creator.xiaohongshu.com', 'localStorage': [{'name': 'USER_INFO_FOR_BIZ', 'value': json.dumps(info)}]}]}), encoding='utf-8')
    return path


@pytest.fixture
def manager(tmp_path, monkeypatch):
    database = Database(tmp_path / 'workbench.sqlite3'); database.migrate()
    project = tmp_path / 'sau'; project.mkdir(); (project / 'pyproject.toml').write_text('[project]\nname="sau"', encoding='utf-8')
    manager = VideoPublishManager(database)
    manager.adapter.set_project_path(str(project))
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: True)
    return manager, project


def test_duplicate_login_is_rejected_without_altering_existing_session(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    original = write_session(project, 'old', 'same-id').read_bytes()
    with runner.database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (old['id'],))
    def login(platform, alias, on_start):
        write_session(project, alias, 'same-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(new['id'], 'login')
    accounts = {x['id']: x for x in list_video_publish_accounts(runner.database)}
    assert accounts[old['id']]['status'] == 'connected'
    assert new['id'] not in accounts
    from app.workbench.video_publishing import list_video_publish_duplicate_notices
    assert list_video_publish_duplicate_notices(runner.database) == [
        {'account_id': new['id'], 'platform_id': 'xiaohongshu', 'message': '该账号已添加，无需重复登录。'}
    ]
    assert (project / 'cookies/xiaohongshu_old.json').read_bytes() == original
    assert not (project / 'cookies/xiaohongshu_new.json').exists()
    assert list((project / 'cookies').glob('*_pending_*.json')) == []


def test_different_ids_with_same_nickname_are_allowed(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    write_session(project, 'old', 'first-id')
    with runner.database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (old['id'],))
    def login(platform, alias, on_start):
        write_session(project, alias, 'second-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(new['id'], 'login')
    accounts = {x['id']: x for x in list_video_publish_accounts(runner.database)}
    assert accounts[new['id']]['status'] == 'connected'
    assert (project / 'cookies/xiaohongshu_new.json').is_file()


def test_legacy_duplicate_is_not_deleted_and_is_marked(manager):
    runner, project = manager
    for alias in ('first', 'second'):
        account = add_video_publish_account(runner.database, 'xiaohongshu', alias)
        write_session(project, alias, 'same-id')
        with runner.database.transaction(write=True) as c:
            c.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (account['id'],))
    accounts = list_video_publish_accounts(runner.database)
    assert all(x['status'] == 'connected' for x in accounts)
    assert sum(bool(x.get('duplicate_notice')) for x in accounts) == 1
    assert len(list((project / 'cookies').glob('*.json'))) == 2


@pytest.mark.parametrize('platform', ['xiaohongshu', 'douyin'])
def test_legacy_failed_duplicate_placeholder_is_removed_on_startup(manager, platform):
    runner, project = manager
    placeholder = add_video_publish_account(runner.database, platform, 'placeholder')
    with runner.database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_accounts SET status='error', message=? WHERE id=?",
                           ('该账号已添加，请使用已有账号。', placeholder['id']))
    runner.start()
    try:
        assert list_video_publish_accounts(runner.database) == []
        assert list_video_publish_accounts(runner.database, include_disabled=True)[0]['enabled'] is False
    finally:
        runner.stop()


def test_legacy_duplicate_with_existing_session_is_not_removed(manager):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'existing')
    original = write_session(project, 'existing', 'original-id').read_bytes()
    with runner.database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_accounts SET status='error', message=? WHERE id=?",
                           ('该账号已添加，请使用已有账号。', account['id']))
    runner.start()
    try:
        assert list_video_publish_accounts(runner.database)[0]['enabled'] is True
        assert (project / 'cookies/xiaohongshu_existing.json').read_bytes() == original
    finally:
        runner.stop()


def test_missing_identity_does_not_bind_or_overwrite_session(manager):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'existing')
    original = write_session(project, 'existing', 'old-id').read_bytes()
    def login(platform, alias, on_start):
        write_session(project, alias, '')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(account['id'], 'login')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'error'
    assert '身份' in saved['message']
    assert (project / 'cookies/xiaohongshu_existing.json').read_bytes() == original


def test_existing_account_cannot_be_replaced_by_different_platform_identity(manager):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'existing')
    original = write_session(project, 'existing', 'old-id').read_bytes()
    def login(platform, alias, on_start):
        write_session(project, alias, 'different-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(account['id'], 'login')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'error'
    assert '其他账号' in saved['message']
    assert (project / 'cookies/xiaohongshu_existing.json').read_bytes() == original


def test_unverifiable_connected_existing_account_blocks_new_binding(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    with runner.database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (old['id'],))
    def login(platform, alias, on_start):
        write_session(project, alias, 'candidate-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(new['id'], 'login')
    accounts = {a['id']: a for a in list_video_publish_accounts(runner.database)}
    assert accounts[new['id']]['status'] == 'error'
    assert '核验已有账号' in accounts[new['id']]['message']
    assert not (project / 'cookies/xiaohongshu_new.json').exists()


@pytest.mark.parametrize('platform', ['douyin', 'wechat_channels', 'baijiahao', 'weibo'])
def test_unsupported_identity_is_not_silently_connected(manager, platform):
    runner, _ = manager
    account = add_video_publish_account(runner.database, platform)
    runner.adapter.login = lambda *args: pytest.fail('unverified platform must not launch login')
    runner._run_account_action(account['id'], 'login')
    assert list_video_publish_accounts(runner.database)[0]['status'] == 'error'


def test_platforms_report_which_login_flows_are_available(manager):
    runner, _ = manager
    platforms = {item['id']: item for item in video_publish_platforms(runner.database)}
    assert platforms['xiaohongshu']['login_available'] is True
    assert platforms['bilibili']['login_available'] is True
    assert platforms['kuaishou']['login_available'] is True
    assert platforms['wechat_channels']['login_available'] is True
    for platform in ('douyin',):
        assert platforms[platform]['login_available'] is False


def test_wechat_channels_qr_lookup_uses_pending_alias(manager):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'wechat_channels', 'creator')
    request_video_publish_account_action(runner.database, account['id'], 'login')
    with runner.database.transaction() as connection:
        updated_at = connection.execute(
            'SELECT updated_at FROM video_publish_accounts WHERE id=?', (account['id'],)
        ).fetchone()['updated_at']
    token = hashlib.sha256((str(account['id']) + '\0' + updated_at).encode()).hexdigest()[:16]
    pending_alias = f'creator_pending_{token}'
    qr = project / 'cookies' / f'tencent_{pending_alias}_login_qrcode.png'
    qr.parent.mkdir(parents=True, exist_ok=True)
    png = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
    qr.write_bytes(png)

    assert latest_video_publish_login_qr(runner.database, account['id']) == png


def write_wechat_channels_session(root: Path, alias: str, username: str = 'creator-123') -> Path:
    path = root / 'cookies' / f'tencent_{alias}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'cookies': [],
        'origins': [{
            'origin': 'https://channels.weixin.qq.com',
            'localStorage': [{'name': 'finder_username', 'value': username}],
        }],
    }), encoding='utf-8')
    return path


def test_wechat_channels_identity_is_read_from_saved_official_storage(manager):
    _runner, project = manager
    from app.workbench.video_publishing import _read_wechat_channels_identity

    session = write_wechat_channels_session(project, 'creator')

    assert _read_wechat_channels_identity(session) == hashlib.sha256(
        b'wechat_channels\0creator-123'
    ).hexdigest()
    session.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
    assert _read_wechat_channels_identity(session) is None


def test_wechat_channels_login_saves_tencent_session_and_profile(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'wechat_channels', 'creator')
    identity = hashlib.sha256(b'wechat_channels\0creator-123').hexdigest()
    png = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')

    def login(_account_id, pending, qr, *, check=False):
        assert not check
        write_wechat_channels_session(project, pending.name.removeprefix('tencent_').removesuffix('.json'))
        qr.write_bytes(png)
        return {
            'status': 'connected',
            'identity': identity,
            'name': '视频号昵称',
            'avatar_url': 'https://wx.qlogo.cn/mmhead/channel/132',
        }

    monkeypatch.setattr(runner, '_launch_wechat_channels_login', login, raising=False)
    runner._run_account_action(account['id'], 'login')

    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == '视频号昵称'
    assert saved['avatar_url'] == 'https://wx.qlogo.cn/mmhead/channel/132'
    assert (project / 'cookies/tencent_creator.json').is_file()
    assert list((project / 'cookies').glob('tencent_*pending*')) == []
    assert list((project / 'cookies').glob('tencent_*qrcode*.png')) == []


def test_wechat_channels_duplicate_login_removes_only_new_placeholder(manager, monkeypatch):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'wechat_channels', 'old')
    new = add_video_publish_account(runner.database, 'wechat_channels', 'new')
    identity = hashlib.sha256(b'wechat_channels\0creator-123').hexdigest()
    original = None

    def login(_account_id, pending, _qr, *, check=False):
        nonlocal original
        assert not check
        alias = pending.name.removeprefix('tencent_').removesuffix('.json')
        write_wechat_channels_session(project, alias)
        if alias.startswith('old_'):
            original = pending.read_bytes()
        return {'status': 'connected', 'identity': identity, 'name': '视频号昵称', 'avatar_url': ''}

    monkeypatch.setattr(runner, '_launch_wechat_channels_login', login, raising=False)
    runner._run_account_action(old['id'], 'login')
    original = (project / 'cookies/tencent_old.json').read_bytes()
    runner._run_account_action(new['id'], 'login')

    visible = list_video_publish_accounts(runner.database)
    assert len(visible) == 1 and visible[0]['id'] == old['id'] and visible[0]['status'] == 'connected'
    assert (project / 'cookies/tencent_old.json').read_bytes() == original
    assert not (project / 'cookies/tencent_new.json').exists()
    assert list((project / 'cookies').glob('tencent_*pending*')) == []
    from app.workbench.video_publishing import list_video_publish_duplicate_notices
    assert list_video_publish_duplicate_notices(runner.database) == [
        {'account_id': new['id'], 'platform_id': 'wechat_channels', 'message': '该账号已添加，无需重复登录。'}
    ]


def test_wechat_channels_check_uses_saved_session_and_refreshes_profile(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'wechat_channels', 'creator')
    identity = hashlib.sha256(b'wechat_channels\0creator-123').hexdigest()

    def login(_account_id, session_file, _qr, *, check=False):
        if check:
            assert session_file == project / 'cookies/tencent_creator.json'
            return {
                'status': 'connected',
                'identity': identity,
                'name': '更新后昵称',
                'avatar_url': 'https://wx.qlogo.cn/mmhead/updated/132',
            }
        alias = session_file.name.removeprefix('tencent_').removesuffix('.json')
        write_wechat_channels_session(project, alias)
        return {'status': 'connected', 'identity': identity, 'name': '旧昵称', 'avatar_url': ''}

    monkeypatch.setattr(runner, '_launch_wechat_channels_login', login, raising=False)
    runner.adapter.check = lambda *_args: pytest.fail('video account check must load the saved guarded session')
    runner._run_account_action(account['id'], 'login')
    runner._run_account_action(account['id'], 'check')

    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == '更新后昵称'
    assert saved['avatar_url'] == 'https://wx.qlogo.cn/mmhead/updated/132'


def test_wechat_channels_temporary_check_failure_keeps_last_verified_connection(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'wechat_channels', 'creator')
    identity = hashlib.sha256(b'wechat_channels\0creator-123').hexdigest()

    def login(_account_id, session_file, _qr, *, check=False):
        if check:
            return {'status': 'unavailable', 'identity': '', 'name': '', 'avatar_url': ''}
        alias = session_file.name.removeprefix('tencent_').removesuffix('.json')
        write_wechat_channels_session(project, alias)
        return {'status': 'connected', 'identity': identity, 'name': '视频号昵称', 'avatar_url': ''}

    monkeypatch.setattr(runner, '_launch_wechat_channels_login', login, raising=False)
    runner._run_account_action(account['id'], 'login')
    before = list_video_publish_accounts(runner.database)[0]
    request_video_publish_account_action(runner.database, account['id'], 'check')
    runner._run_account_action(account['id'], 'check')

    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected'
    assert '本次检查未完成' in saved['message']
    assert saved['checked_at'] == before['checked_at']


def test_wechat_channels_simultaneous_duplicate_login_keeps_one_account(manager, monkeypatch):
    runner, project = manager
    accounts = [add_video_publish_account(runner.database, 'wechat_channels', alias) for alias in ('first', 'second')]
    identity = hashlib.sha256(b'wechat_channels\0creator-123').hexdigest()
    barrier = threading.Barrier(2)

    def login(_account_id, pending, _qr, *, check=False):
        assert not check
        alias = pending.name.removeprefix('tencent_').removesuffix('.json')
        write_wechat_channels_session(project, alias)
        barrier.wait(timeout=5)
        return {'status': 'connected', 'identity': identity, 'name': '', 'avatar_url': ''}

    monkeypatch.setattr(runner, '_launch_wechat_channels_login', login, raising=False)
    threads = [threading.Thread(target=runner._run_account_action, args=(item['id'], 'login')) for item in accounts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    visible = list_video_publish_accounts(runner.database)
    assert all(not thread.is_alive() for thread in threads)
    assert len(visible) == 1 and visible[0]['status'] == 'connected'
    assert len(list((project / 'cookies').glob('tencent_*.json'))) == 1


def test_kuaishou_login_uses_staged_session_and_rejects_duplicate(manager, monkeypatch):
    import hashlib
    runner, project = manager
    first = add_video_publish_account(runner.database, 'kuaishou', 'first')
    second = add_video_publish_account(runner.database, 'kuaishou', 'second')
    identity = hashlib.sha256(b'kuaishou\0user123').hexdigest()
    def login(account_id, pending, qr):
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        return {'identity': identity, 'name': '测试快手用户', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner._run_account_action(first['id'], 'login')
    original = (project / 'cookies/kuaishou_first.json').read_bytes()
    runner._run_account_action(second['id'], 'login')
    visible = list_video_publish_accounts(runner.database)
    assert len(visible) == 1 and visible[0]['id'] == first['id']
    assert visible[0]['profile_name'] == '测试快手用户'
    assert (project / 'cookies/kuaishou_first.json').read_bytes() == original
    assert not (project / 'cookies/kuaishou_second.json').exists()
    from app.workbench.video_publishing import list_video_publish_duplicate_notices
    assert list_video_publish_duplicate_notices(runner.database)[0]['account_id'] == second['id']


def test_kuaishou_login_refuses_unverifiable_existing_account(manager, monkeypatch):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'kuaishou', 'old')
    new = add_video_publish_account(runner.database, 'kuaishou', 'new')
    with runner.database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (old['id'],))
    def login(account_id, pending, qr):
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        return {'identity': 'a' * 64, 'name': '', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner._run_account_action(new['id'], 'login')
    saved = {a['id']: a for a in list_video_publish_accounts(runner.database)}
    assert saved[old['id']]['status'] == 'connected'
    assert saved[new['id']]['status'] == 'error'
    assert not (project / 'cookies/kuaishou_new.json').exists()


def test_kuaishou_check_uses_saved_session_and_updates_avatar(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'kuaishou', 'first')
    def login(account_id, pending, qr, *, check=False):
        if not check:
            pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
            return {'identity': 'a' * 64, 'name': '', 'avatar_url': ''}
        return {'identity': 'a' * 64, 'name': '快手昵称', 'avatar_url': 'https://p1.a.yximgs.com/a.jpg'}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner.adapter.check = lambda *args: pytest.fail('must use the guarded login helper')
    runner._run_account_action(account['id'], 'login')
    runner._run_account_action(account['id'], 'check')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected' and saved['profile_name'] == '快手昵称'
    assert saved['avatar_url'] == 'https://p1.a.yximgs.com/a.jpg'


def test_kuaishou_check_temporary_failure_preserves_last_verified_status(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'kuaishou', 'first')
    def login(account_id, pending, qr, *, check=False):
        if check:
            raise VideoPublishError('account_login_failed', '请求失败', 409)
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        return {'identity': 'a' * 64, 'name': '昵称', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner._run_account_action(account['id'], 'login')
    request_video_publish_account_action(runner.database, account['id'], 'check')
    runner._run_account_action(account['id'], 'check')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected'
    assert '本次检查未完成' in saved['message']


def test_kuaishou_startup_recovers_only_previously_bound_failed_check(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'kuaishou', 'first')
    def login(account_id, pending, qr):
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        return {'identity': 'a' * 64, 'name': '昵称', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner._run_account_action(account['id'], 'login')
    with runner.database.transaction(write=True) as c:
        c.execute("UPDATE video_publish_accounts SET status='error',message=? WHERE id=?", ('暂时无法检查登录状态，请稍后重试。', account['id']))
    runner.start()
    try:
        saved = list_video_publish_accounts(runner.database)[0]
        assert saved['status'] == 'connected'
        assert '本次检查未完成' in saved['message']
        assert (project / 'cookies/kuaishou_first.json').is_file()
    finally:
        runner.stop()


def test_kuaishou_concurrent_duplicate_logins_keep_one_account(manager, monkeypatch):
    runner, project = manager
    accounts = [add_video_publish_account(runner.database, 'kuaishou', alias) for alias in ('first', 'second')]
    barrier = threading.Barrier(2)
    def login(account_id, pending, qr):
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        barrier.wait(timeout=5)
        return {'identity': 'a' * 64, 'name': '', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    threads = [threading.Thread(target=runner._run_account_action, args=(a['id'], 'login')) for a in accounts]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert [a['status'] for a in list_video_publish_accounts(runner.database)] == ['connected']
    assert len(list((project / 'cookies').glob('kuaishou_*.json'))) == 1


def test_kuaishou_waiting_page_shows_the_current_account_qr(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'kuaishou', 'first')
    request_video_publish_account_action(runner.database, account['id'], 'login')
    png = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
    def login(account_id, pending, qr):
        qr.write_bytes(png)
        assert latest_video_publish_login_qr(runner.database, account_id) == png
        pending.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        return {'identity': 'a' * 64, 'name': '', 'avatar_url': ''}
    monkeypatch.setattr(runner, '_launch_kuaishou_login', login)
    runner._run_account_action(account['id'], 'login')
    assert list_video_publish_accounts(runner.database)[0]['status'] == 'connected'
    assert list((project / 'cookies').glob('*qrcode*.png')) == []


def bili_credentials(mid):
    return {'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'session'}, {'name': 'bili_jct', 'value': 'csrf'}]},
            'sso': [], 'token_info': {'mid': mid, 'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600},
            'platform': 'BiliTV'}


def test_bilibili_login_saves_compatible_credentials_and_shows_qr(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'bilibili', 'new')
    request_video_publish_account_action(runner.database, account['id'], 'login')
    png = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
    def login_qr(on_qr, stop):
        on_qr(png)
        assert latest_video_publish_login_qr(runner.database, account['id']) == png
        return bili_credentials(123), {'name': '测试用户', 'avatar_url': ''}
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', login_qr)
    runner._run_account_action(account['id'], 'login')
    assert list_video_publish_accounts(runner.database)[0]['status'] == 'connected'
    assert json.loads((project / 'cookies/bilibili_new.json').read_text(encoding='utf-8')) == bili_credentials(123)
    assert list((project / 'cookies').glob('*qrcode*.png')) == []


def test_bilibili_duplicate_removes_placeholder_and_preserves_existing_session(manager, monkeypatch):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'bilibili', 'old')
    new = add_video_publish_account(runner.database, 'bilibili', 'new')
    def login_qr(on_qr, stop):
        return bili_credentials(123), {}
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', login_qr)
    runner._run_account_action(old['id'], 'login')
    old_file = project / 'cookies/bilibili_old.json'
    original = old_file.read_bytes()
    runner._run_account_action(new['id'], 'login')
    assert [item['id'] for item in list_video_publish_accounts(runner.database)] == [old['id']]
    assert old_file.read_bytes() == original
    assert not (project / 'cookies/bilibili_new.json').exists()
    from app.workbench.video_publishing import list_video_publish_duplicate_notices
    assert list_video_publish_duplicate_notices(runner.database)[0]['account_id'] == new['id']


def test_bilibili_different_accounts_are_allowed(manager, monkeypatch):
    runner, project = manager
    first = add_video_publish_account(runner.database, 'bilibili', 'first')
    second = add_video_publish_account(runner.database, 'bilibili', 'second')
    ids = iter([1, 2])
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', lambda on_qr, stop: (bili_credentials(next(ids)), {}))
    runner._run_account_action(first['id'], 'login')
    runner._run_account_action(second['id'], 'login')
    assert [a['status'] for a in list_video_publish_accounts(runner.database)] == ['connected', 'connected']


def test_bilibili_simultaneous_duplicate_login_only_binds_one_account(manager, monkeypatch):
    runner, project = manager
    accounts = [add_video_publish_account(runner.database, 'bilibili', alias) for alias in ('first', 'second')]
    barrier = threading.Barrier(2)
    def login_qr(on_qr, stop):
        barrier.wait(timeout=5)
        return bili_credentials(123), {}
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', login_qr)
    threads = [threading.Thread(target=runner._run_account_action, args=(a['id'], 'login')) for a in accounts]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert [a['status'] for a in list_video_publish_accounts(runner.database)] == ['connected']
    assert len(list((project / 'cookies').glob('bilibili_*.json'))) == 1


def test_bilibili_check_uses_browser_free_status_and_preserves_account(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'bilibili', 'first')
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', lambda on_qr, stop: (bili_credentials(1), {}))
    monkeypatch.setattr('app.workbench.video_publishing.check_bilibili_login', lambda data: (True, {'name': 'B站用户', 'avatar_url': ''}))
    runner.adapter.check = lambda *args: pytest.fail('must not launch browser or SAU check')
    runner._run_account_action(account['id'], 'login')
    runner._run_account_action(account['id'], 'check')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == 'B站用户'


def test_bilibili_expired_qr_reports_error_and_removes_temporary_image(manager, monkeypatch):
    from app.workbench.bilibili_login import BilibiliLoginError
    runner, project = manager
    account = add_video_publish_account(runner.database, 'bilibili', 'first')
    def login_qr(on_qr, stop):
        on_qr(b'\x89PNG\r\n\x1a\nimage')
        raise BilibiliLoginError('二维码已过期，请重新登录。')
    monkeypatch.setattr('app.workbench.video_publishing.run_bilibili_login', login_qr)
    runner._run_account_action(account['id'], 'login')
    saved = list_video_publish_accounts(runner.database)[0]
    assert saved['status'] == 'error'
    assert '二维码已过期' in saved['message']
    assert list((project / 'cookies').glob('*qrcode*.png')) == []


def test_concurrent_same_identity_only_one_is_accepted(manager):
    runner, project = manager
    accounts = [add_video_publish_account(runner.database, 'xiaohongshu', alias) for alias in ('first', 'second')]
    barrier = threading.Barrier(2)
    def login(platform, alias, on_start):
        write_session(project, alias, 'same-id')
        barrier.wait(timeout=5)
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    threads = [threading.Thread(target=runner._run_account_action, args=(a['id'], 'login')) for a in accounts]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert all(not t.is_alive() for t in threads)
    assert [a['status'] for a in list_video_publish_accounts(runner.database)] == ['connected']
    assert sorted(a['status'] for a in list_video_publish_accounts(runner.database, include_disabled=True)) == ['connected', 'error']


def test_login_qr_is_found_for_temporary_alias(manager):
    from app.workbench.video_publishing import _xiaohongshu_pending_alias
    runner, project = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'original')
    request_video_publish_account_action(runner.database, account['id'], 'login')
    with runner.database.transaction() as c:
        updated_at = c.execute('SELECT updated_at FROM video_publish_accounts WHERE id=?', (account['id'],)).fetchone()['updated_at']
    pending_alias = _xiaohongshu_pending_alias('original', account['id'], updated_at)
    qr = project / 'cookies' / f'xiaohongshu_{pending_alias}_login_qrcode.png'
    qr.parent.mkdir(parents=True, exist_ok=True)
    png = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
    qr.write_bytes(png)
    assert latest_video_publish_login_qr(runner.database, account['id']) == png


def test_unconnected_account_cannot_bypass_login_identity_by_checking(manager):
    runner, _ = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    with pytest.raises(VideoPublishError, match='先登录'):
        request_video_publish_account_action(runner.database, account['id'], 'check')


def test_login_cannot_be_queued_twice_for_same_account(manager):
    runner, _ = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    request_video_publish_account_action(runner.database, account['id'], 'login')
    with pytest.raises(VideoPublishError, match='正在'):
        request_video_publish_account_action(runner.database, account['id'], 'login')


def test_one_account_cannot_pick_up_another_accounts_qr(manager):
    from app.workbench.video_publishing import _xiaohongshu_pending_alias
    runner, project = manager
    first = add_video_publish_account(runner.database, 'xiaohongshu', 'same')
    second = add_video_publish_account(runner.database, 'xiaohongshu', 'same_longer')
    request_video_publish_account_action(runner.database, first['id'], 'login')
    request_video_publish_account_action(runner.database, second['id'], 'login')
    with runner.database.transaction() as c:
        updated_at = c.execute('SELECT updated_at FROM video_publish_accounts WHERE id=?', (second['id'],)).fetchone()['updated_at']
    alias = _xiaohongshu_pending_alias('same_longer', second['id'], updated_at)
    qr = project / 'cookies' / f'xiaohongshu_{alias}_login_qrcode.png'
    qr.parent.mkdir(parents=True, exist_ok=True)
    qr.write_bytes(bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082'))
    assert latest_video_publish_login_qr(runner.database, first['id']) is None
    assert latest_video_publish_login_qr(runner.database, second['id']) is not None


def test_existing_temporary_session_is_never_deleted(manager, monkeypatch):
    runner, project = manager
    account = add_video_publish_account(runner.database, 'xiaohongshu', 'first')
    pending = write_session(project, 'first_pending_existing', 'previous-id')
    before = pending.read_bytes()
    monkeypatch.setattr('app.workbench.video_publishing._xiaohongshu_pending_alias', lambda *args: 'first_pending_existing')
    runner.adapter.login = lambda *args: pytest.fail('existing temporary session must not be overwritten')
    runner._run_account_action(account['id'], 'login')
    assert pending.read_bytes() == before


def test_duplicate_is_blocked_even_if_previous_session_file_disappears(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    def login(platform, alias, on_start):
        write_session(project, alias, 'same-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(old['id'], 'login')
    assert next(a for a in list_video_publish_accounts(runner.database) if a['id'] == old['id'])['status'] == 'connected'
    (project / 'cookies/xiaohongshu_old.json').unlink()
    runner._run_account_action(new['id'], 'login')
    accounts = {a['id']: a for a in list_video_publish_accounts(runner.database)}
    assert new['id'] not in accounts


def test_changed_saved_session_cannot_silently_change_bound_identity(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    def login(platform, alias, on_start):
        write_session(project, alias, 'original-id' if alias.startswith('old_') else 'new-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(old['id'], 'login')
    write_session(project, 'old', 'tampered-id')
    runner._run_account_action(new['id'], 'login')
    accounts = {a['id']: a for a in list_video_publish_accounts(runner.database)}
    assert accounts[new['id']]['status'] == 'error'
    assert '核验已有账号' in accounts[new['id']]['message']


def test_legacy_duplicates_are_backfilled_without_deleting_any_record(manager):
    runner, project = manager
    for alias in ('first', 'second'):
        account = add_video_publish_account(runner.database, 'xiaohongshu', alias)
        write_session(project, alias, 'same-id')
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE video_publish_accounts SET status='connected' WHERE id=?", (account['id'],))
    runner.start()
    try:
        with runner.database.transaction() as connection:
            stored = connection.execute("SELECT value_json FROM settings WHERE key LIKE 'video_publish_identity:%'").fetchall()
        assert len(stored) == 2
        assert stored[0]['value_json'] == stored[1]['value_json']
        (project / 'cookies/xiaohongshu_second.json').unlink()
        assert sum(bool(account.get('duplicate_notice')) for account in list_video_publish_accounts(runner.database)) == 1
    finally:
        runner.stop()


def test_removed_account_does_not_block_readding_same_identity(manager):
    runner, project = manager
    old = add_video_publish_account(runner.database, 'xiaohongshu', 'old')
    new = add_video_publish_account(runner.database, 'xiaohongshu', 'new')
    def login(platform, alias, on_start):
        write_session(project, alias, 'same-id')
        return SauResult(0, 'XHS_SAFE_LOGIN_VERIFIED')
    runner.adapter.login = login
    runner._run_account_action(old['id'], 'login')
    runner.logout_account(old['id'])
    runner._run_account_action(new['id'], 'login')
    assert next(a for a in list_video_publish_accounts(runner.database) if a['id'] == new['id'])['status'] == 'connected'
