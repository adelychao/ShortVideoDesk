from types import SimpleNamespace
import importlib
import json
import sys
import types
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workbench import api
from app.workbench.database import Database
from app.workbench.video_publishing import (
    SauResult, SocialAutoUploadAdapter, VideoPublishManager, add_video_publish_account,
    list_video_publish_accounts, update_video_publish_account_status, video_publish_platforms,
    _parse_zhihu_login_profile, _parse_zhihu_profile_output, _safe_zhihu_avatar,
)


def test_create_accounts_without_manual_alias_and_keep_explicit_alias(tmp_path, monkeypatch):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    monkeypatch.setattr(api, '_runner', lambda request: SimpleNamespace(database=database))
    app = FastAPI()
    app.include_router(api._router)
    with TestClient(app, base_url='http://testserver') as client:
        first = client.post('/api/v1/video-publish/accounts', json={'platform_id': 'xiaohongshu'})
        second = client.post('/api/v1/video-publish/accounts', json={'platform_id': 'xiaohongshu'})
        explicit = client.post('/api/v1/video-publish/accounts', json={'platform_id': 'xiaohongshu', 'account_alias': 'legacy', 'display_name': '老账号'})
    assert first.status_code == second.status_code == explicit.status_code == 201
    assert first.json()['account_alias'] != second.json()['account_alias']
    assert first.json()['account_alias'].startswith('account_')
    assert explicit.json()['account_alias'] == 'legacy'
    assert explicit.json()['display_name'] == '老账号'


@pytest.mark.parametrize('action', ['login', 'check'])
def test_login_populates_profile_without_overwriting_custom_name(tmp_path, action, monkeypatch):
    monkeypatch.setattr('app.workbench.video_publishing.browser_automation_paused', lambda: False)
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'xiaohongshu', 'existing', '我的备注')
    manager = VideoPublishManager(database)
    project = tmp_path / 'sau'; project.mkdir(); (project / 'pyproject.toml').write_text('[project]\nname="sau"', encoding='utf-8')
    manager.adapter.set_project_path(str(project))
    def login(platform, alias, on_start):
        (project / 'cookies').mkdir(exist_ok=True)
        state = {'origins': [{'origin': 'https://creator.xiaohongshu.com', 'localStorage': [{'name': 'USER_INFO_FOR_BIZ', 'value': json.dumps({'userId': 'test-id'})}]}]}
        (project / 'cookies' / f'xiaohongshu_{alias}.json').write_text(json.dumps(state), encoding='utf-8')
        return SauResult(0, '')
    manager.adapter.login = login
    manager.adapter.check = lambda *_args: SauResult(0, '')
    manager.adapter.read_account_profile = lambda *_args: {'name': '平台昵称', 'avatar_url': 'https://sns-avatar-qc.xhscdn.com/avatar.jpg'}
    manager._run_account_action(account['id'], action)
    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'
    assert saved['display_name'] == '我的备注'
    assert saved['profile_name'] == '平台昵称'
    assert saved['avatar_url'] == 'https://sns-avatar-qc.xhscdn.com/avatar.jpg'


def test_profile_failure_does_not_invalidate_successful_login(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'douyin')
    manager = VideoPublishManager(database)
    manager.adapter.check = lambda *_args: SauResult(0, '')
    def fail(*_args):
        raise RuntimeError('profile unavailable')
    manager.adapter.read_account_profile = fail
    manager._run_account_action(account['id'], 'check')
    assert list_video_publish_accounts(database)[0]['status'] == 'connected'


@pytest.mark.parametrize('action', ['login', 'check'])
def test_zhihu_browser_account_uses_sau_login_and_check(tmp_path, action):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'zhihu', 'creator')
    manager = VideoPublishManager(database)
    calls = []
    manager.adapter.login = lambda platform, alias, _on_start: calls.append(('login', platform, alias)) or SauResult(0, '')
    manager.adapter.check = lambda platform, alias, _on_start: calls.append(('check', platform, alias)) or SauResult(0, '')

    if action == 'check':
        from app.workbench.video_publishing import update_video_publish_account_status
        update_video_publish_account_status(database, account['id'], 'connected', '登录成功。')
    manager._run_account_action(account['id'], action)

    assert calls == [(action, 'zhihu', 'creator')]
    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'


def test_zhihu_login_platform_is_exposed_for_articles_only(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()

    video_platforms = video_publish_platforms(database)
    article_platforms = video_publish_platforms(database, scope='article')

    assert 'zhihu' not in {item['id'] for item in video_platforms}
    assert next(item for item in article_platforms if item['id'] == 'zhihu')['login_available'] is True


def test_baijiahao_login_platform_is_available_for_articles(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()

    article_platforms = video_publish_platforms(database, scope='article')

    baijiahao = next(item for item in article_platforms if item['id'] == 'baijiahao')
    assert baijiahao['login_available'] is True
    assert next(item for item in video_publish_platforms(database) if item['id'] == 'baijiahao')['login_available'] is False


@pytest.mark.parametrize('action', ['login', 'check'])
def test_baijiahao_login_and_check_save_profile_without_overwriting_custom_name(tmp_path, action):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'baijiahao', 'writer', '我的备注')
    manager = VideoPublishManager(database)
    profile = {'name': '百家号昵称', 'avatar_url': 'https://pic.rmb.bdstatic.com/bjh/portrait/avatar.jpeg'}
    manager.adapter.login = lambda *_args: SauResult(0, '')
    manager.adapter.check = lambda *_args: SauResult(0, '')
    manager.adapter.read_account_profile = lambda *_args: profile

    if action == 'check':
        update_video_publish_account_status(database, account['id'], 'connected', '登录成功。')
    manager._run_account_action(account['id'], action)

    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'
    assert saved['display_name'] == '我的备注'
    assert saved['profile_name'] == profile['name']
    assert saved['avatar_url'] == profile['avatar_url']


@pytest.mark.parametrize('action', ['login', 'check'])
def test_baijiahao_article_account_uses_sau_login_and_check(tmp_path, action):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'baijiahao', 'writer')
    manager = VideoPublishManager(database)
    calls = []
    manager.adapter.login = lambda platform, alias, _on_start: calls.append(('login', platform, alias)) or SauResult(0, '')
    manager.adapter.check = lambda platform, alias, _on_start: calls.append(('check', platform, alias)) or SauResult(0, '')

    if action == 'check':
        update_video_publish_account_status(database, account['id'], 'connected', '登录成功。')
    manager._run_account_action(account['id'], action)

    assert calls == [(action, 'baijiahao', 'writer')]
    assert list_video_publish_accounts(database)[0]['status'] == 'connected'


@pytest.mark.parametrize(
    ('avatar', 'expected'),
    [
        ('https://pic.rmb.bdstatic.com/bjh/portrait/avatar.jpeg', 'https://pic.rmb.bdstatic.com/bjh/portrait/avatar.jpeg'),
        ('https://pic.rmb.bdstatic.com.attacker.example/avatar.jpeg', ''),
        ('http://pic.rmb.bdstatic.com/avatar.jpeg', ''),
        ('https://user@pic.rmb.bdstatic.com/avatar.jpeg', ''),
        ('https://pic.rmb.bdstatic.com:8443/avatar.jpeg', ''),
    ],
)
def test_baijiahao_profile_read_accepts_only_https_bdstatic_avatar(tmp_path, monkeypatch, avatar, expected):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    project = tmp_path / 'sau'
    (project / 'cookies').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname="sau"\n', encoding='utf-8')
    (project / 'cookies' / 'baijiahao_writer.json').write_text('{}', encoding='utf-8')
    python = project / '.venv' / 'Scripts' / 'python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    adapter = SocialAutoUploadAdapter(database)
    adapter.set_project_path(str(project))

    class FakeProcess:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, timeout):
            return json.dumps({'name': '百家号昵称', 'avatar_url': avatar}, ensure_ascii=False), None

    monkeypatch.setattr('app.workbench.video_publishing.subprocess.Popen', FakeProcess)
    monkeypatch.setattr('app.workbench.video_publishing.require_safe_browser', lambda _operation: None)

    profile = adapter.read_account_profile('baijiahao', 'writer', lambda _process: None)

    assert profile.get('avatar_url', '') == expected
    if expected:
        assert profile['name'] == '百家号昵称'
    else:
        assert profile == {}


def test_baijiahao_profile_probe_extracts_only_creator_name_and_avatar(monkeypatch):
    patchright = types.ModuleType('patchright')
    async_api = types.ModuleType('patchright.async_api')
    async_api.async_playwright = lambda: None
    patchright.async_api = async_api
    monkeypatch.setitem(sys.modules, 'patchright', patchright)
    monkeypatch.setitem(sys.modules, 'patchright.async_api', async_api)
    probe = importlib.import_module('app.workbench.video_account_profile_probe')

    profile = probe._parse_baijiahao_profile_payload({
        'errno': 0,
        'data': {
            'name': '百家号昵称',
            'owner_name': '真实姓名字段不应使用',
            'avatar': 'https://pic.rmb.bdstatic.com/bjh/portrait/avatar.jpeg',
            'owner_id': 'sensitive-id-not-returned',
        },
    })

    assert profile == {
        'name': '百家号昵称',
        'avatar_url': 'https://pic.rmb.bdstatic.com/bjh/portrait/avatar.jpeg',
    }


def test_baijiahao_profile_probe_ignores_failed_or_incomplete_api_payload(monkeypatch):
    patchright = types.ModuleType('patchright')
    async_api = types.ModuleType('patchright.async_api')
    async_api.async_playwright = lambda: None
    patchright.async_api = async_api
    monkeypatch.setitem(sys.modules, 'patchright', patchright)
    monkeypatch.setitem(sys.modules, 'patchright.async_api', async_api)
    probe = importlib.import_module('app.workbench.video_account_profile_probe')

    assert probe._parse_baijiahao_profile_payload({'errno': 1, 'data': {'name': '昵称', 'avatar': 'https://pic.rmb.bdstatic.com/a.jpg'}}) == {}
    assert probe._parse_baijiahao_profile_payload({'errno': 0, 'data': {'name': ' ', 'avatar': 'https://pic.rmb.bdstatic.com/a.jpg'}}) == {}


def test_zhihu_login_does_not_pass_unsupported_headed_flag(tmp_path):
    adapter = SocialAutoUploadAdapter(Database(tmp_path / 'workbench.sqlite3'))
    calls = []

    def run(arguments, _on_start, timeout, *, interactive_terminal=False):
        calls.append((arguments, timeout, interactive_terminal))
        return SauResult(0, '')

    adapter.run = run

    adapter.login('zhihu', 'creator', lambda _process: None)

    assert calls == [(['zhihu', 'login', '--account', 'creator'], 1800, False)]


def test_zhihu_check_requests_profile_from_same_browser_session(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    adapter = SocialAutoUploadAdapter(database)
    calls = []
    adapter.run = lambda arguments, _on_start, timeout: calls.append((arguments, timeout)) or SauResult(0, 'valid')

    adapter.check('zhihu', 'creator', lambda _process: None)

    assert calls == [(['zhihu', 'check', '--account', 'creator', '--with-profile'], 120)]


def test_zhihu_login_saves_verified_profile(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'zhihu', 'creator')
    manager = VideoPublishManager(database)
    manager.adapter.login = lambda *_args: SauResult(0, json.dumps({
        'success': True,
        'profile': {'name': '知乎作者', 'avatar_url': 'https://picx.zhimg.com/avatar/a.jpg'},
    }, ensure_ascii=False))

    manager._run_account_action(account['id'], 'login')

    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == '知乎作者'
    assert saved['avatar_url'] == 'https://picx.zhimg.com/avatar/a.jpg'


def test_zhihu_login_rejects_untrusted_avatar_and_preserves_saved_avatar(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'zhihu', 'creator')
    saved_avatar = 'https://picx.zhimg.com/avatar/previous.jpg'
    with database.transaction(write=True) as connection:
        connection.execute(
            'UPDATE video_publish_accounts SET profile_name=?, avatar_url=? WHERE id=?',
            ('旧昵称', saved_avatar, account['id']),
        )
    manager = VideoPublishManager(database)
    manager.adapter.login = lambda *_args: SauResult(0, json.dumps({
        'success': True,
        'profile': {'name': '新昵称', 'avatar_url': 'https://avatar.attacker.example/new.jpg'},
    }, ensure_ascii=False))

    manager._run_account_action(account['id'], 'login')

    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == '新昵称'
    assert saved['avatar_url'] == saved_avatar


def test_zhihu_check_refreshes_profile_without_rechecking_browser(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'zhihu', 'creator')
    update_video_publish_account_status(database, account['id'], 'connected', '登录状态有效。')
    manager = VideoPublishManager(database)
    profile = {'name': '知乎新昵称', 'avatar_url': 'https://picx.zhimg.com/avatar/new.jpg'}
    manager.adapter.check = lambda *_args: SauResult(
        0, 'valid\nZHIHU_PROFILE_JSON:' + json.dumps(profile, ensure_ascii=False),
    )
    manager.adapter.read_account_profile = lambda *_args: (_ for _ in ()).throw(AssertionError('不应启动第二个浏览器探测'))

    manager._run_account_action(account['id'], 'check')

    saved = list_video_publish_accounts(database)[0]
    assert saved['status'] == 'connected'
    assert saved['profile_name'] == '知乎新昵称'
    assert saved['avatar_url'] == profile['avatar_url']


def test_zhihu_avatar_rejects_untrusted_hosts_and_schemes():
    assert _safe_zhihu_avatar('https://picx.zhimg.com/avatar/a.jpg') == 'https://picx.zhimg.com/avatar/a.jpg'
    assert _safe_zhihu_avatar('https://zhimg.com/avatar/a.jpg') == 'https://zhimg.com/avatar/a.jpg'
    assert _safe_zhihu_avatar('https://zhimg.com.attacker.example/a.jpg') == ''
    assert _safe_zhihu_avatar('http://picx.zhimg.com/a.jpg') == ''
    assert _safe_zhihu_avatar('https://user@picx.zhimg.com/a.jpg') == ''


def test_zhihu_profile_parsers_accept_contract_and_ignore_malformed_output():
    profile = {'name': '知乎作者', 'avatar_url': 'https://picx.zhimg.com/avatar/a.jpg'}
    marker = 'valid\nZHIHU_PROFILE_JSON:' + json.dumps(profile, ensure_ascii=False)
    login = '登录完成\n' + json.dumps({'success': True, 'profile': profile}, ensure_ascii=False)

    assert _parse_zhihu_profile_output(marker) == profile
    assert _parse_zhihu_login_profile(login) == profile
    assert _parse_zhihu_profile_output('valid\nZHIHU_PROFILE_JSON:not-json') == {}
    assert _parse_zhihu_profile_output('ZHIHU_PROFILE_JSON:' + json.dumps({'name': '', 'avatar_url': 'x'})) == {}
    assert _parse_zhihu_login_profile(json.dumps({'success': True, 'profile': []})) == {}


def test_zhihu_article_adapter_accepts_only_verified_article_detail_receipt(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    project = tmp_path / 'sau'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname="sau"\n', encoding='utf-8')
    adapter = SocialAutoUploadAdapter(database)
    adapter.set_project_path(str(project))
    source = tmp_path / 'article.md'
    source.write_text('正文', encoding='utf-8')
    calls = []
    receipt = json.dumps({
        'success': True, 'status': 'published', 'type': 'article',
        'url': 'https://zhuanlan.zhihu.com/p/123456789',
    }, ensure_ascii=False)
    adapter.run = lambda arguments, _on_start, timeout: calls.append((arguments, timeout)) or SauResult(0, receipt)

    result = adapter.upload_zhihu_article('writer', '文章标题', source, lambda _process: None)

    assert result.platform_status == 'published'
    assert result.remote_id == '123456789'
    assert result.remote_url == 'https://zhuanlan.zhihu.com/p/123456789'
    assert calls[0][0] == [
        'zhihu', 'upload-article', '--account', 'writer', '--title', '文章标题',
        '--content-file', str(source), '--headed',
    ]
    assert calls[0][1] == 30 * 60


def test_zhihu_article_adapter_marks_submission_without_detail_as_unknown(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    project = tmp_path / 'sau'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname="sau"\n', encoding='utf-8')
    adapter = SocialAutoUploadAdapter(database)
    adapter.set_project_path(str(project))
    source = tmp_path / 'article.md'
    source.write_text('正文', encoding='utf-8')
    adapter.run = lambda *_args: SauResult(1, '知乎文章阶段=submission_unknown')

    result = adapter.upload_zhihu_article('writer', '文章标题', source, lambda _process: None)

    assert result.platform_status == 'unknown'
    assert result.remote_url is None


def test_profile_rejects_foreign_avatar_host(tmp_path, monkeypatch):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    project = tmp_path / 'sau'
    (project / 'cookies').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname="sau"\n', encoding='utf-8')
    (project / 'cookies' / 'xiaohongshu_test.json').write_text('{}', encoding='utf-8')
    executable = project / '.venv' / ('Scripts/sau.exe' if __import__('os').name == 'nt' else 'bin/sau')
    executable.parent.mkdir(parents=True)
    executable.touch()
    executable.with_name('python.exe' if __import__('os').name == 'nt' else 'python').touch()
    adapter = SocialAutoUploadAdapter(database)
    adapter.set_project_path(str(project))
    class FakeProcess:
        returncode = 0
        def __init__(self, *_args, **_kwargs): pass
        def communicate(self, timeout):
            return json.dumps({'name': '昵称', 'avatar_url': 'https://xhscdn.com.attacker.example/avatar.png'}), None
    monkeypatch.setattr('app.workbench.video_publishing.subprocess.Popen', FakeProcess)
    monkeypatch.setattr('app.workbench.video_publishing.require_safe_browser', lambda operation: None)
    assert adapter.read_account_profile('xiaohongshu', 'test', lambda process: None) == {}
