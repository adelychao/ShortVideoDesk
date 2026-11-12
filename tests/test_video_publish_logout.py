from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workbench import api
from app.workbench.database import Database
from app.workbench.video_publishing import VideoPublishManager, add_video_publish_account, update_video_publish_account_status


def setup_account(tmp_path, monkeypatch):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    project = tmp_path / 'sau'
    (project / 'cookies').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname="sau"\n', encoding='utf-8')
    manager = VideoPublishManager(database)
    manager.adapter.set_project_path(str(project))
    account = add_video_publish_account(database, 'xiaohongshu', 'my_account', '我的账号')
    update_video_publish_account_status(database, account['id'], 'connected', '有效')
    app = FastAPI()
    app.include_router(api._router)
    monkeypatch.setattr(api, '_video_publish_manager', lambda request: manager)
    monkeypatch.setattr(api, '_runner', lambda request: SimpleNamespace(database=database))
    return database, project, account, TestClient(app, base_url='http://testserver')


def test_logout_removes_only_the_selected_credential_and_preserves_record(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    credential = project / 'cookies' / 'xiaohongshu_my_account.json'
    other = project / 'cookies' / 'xiaohongshu_someone_else.json'
    credential.write_text('credential', encoding='utf-8')
    other.write_text('other', encoding='utf-8')
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 204
    assert not credential.exists()
    assert other.read_text(encoding='utf-8') == 'other'
    with database.transaction() as connection:
        row = connection.execute('SELECT enabled, status FROM video_publish_accounts WHERE id = ?', (account['id'],)).fetchone()
    assert row['enabled'] == 0
    assert row['status'] == 'not_connected'


def test_logout_refuses_during_login_and_keeps_credentials(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    credential = project / 'cookies' / 'xiaohongshu_my_account.json'
    credential.write_text('credential', encoding='utf-8')
    update_video_publish_account_status(database, account['id'], 'login_in_progress', '登录中')
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 409
    assert credential.exists()


def test_logout_keeps_account_when_sau_directory_unavailable(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    credential = project / 'cookies' / 'xiaohongshu_my_account.json'
    credential.write_text('credential', encoding='utf-8')
    (project / 'pyproject.toml').unlink()
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 409
    assert credential.exists()
    with database.transaction() as connection:
        assert connection.execute('SELECT enabled FROM video_publish_accounts WHERE id = ?', (account['id'],)).fetchone()['enabled'] == 1


def create_target(database, account, status):
    with database.transaction(write=True) as connection:
        connection.execute("""INSERT INTO video_publish_batches
            (id, idempotency_key, media_path, media_name, media_size, media_sha256, title, status, created_at, updated_at)
            VALUES ('batch', 'idempotency', 'video.mp4', 'video.mp4', 1, 'sha', 'title', ?, 't', 't')""", (status,))
        connection.execute("""INSERT INTO video_publish_targets
            (id, batch_id, account_id, platform_id, account_alias, status, created_at, updated_at)
            VALUES ('target', 'batch', ?, 'xiaohongshu', 'my_account', ?, 't', 't')""", (account['id'], status))


def test_logout_refuses_queued_publish_and_keeps_credentials(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    credential = project / 'cookies' / 'xiaohongshu_my_account.json'
    credential.write_text('credential', encoding='utf-8')
    create_target(database, account, 'queued')
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 409
    assert credential.exists()


def test_logout_preserves_completed_publish_history(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    (project / 'cookies' / 'xiaohongshu_my_account.json').write_text('credential', encoding='utf-8')
    create_target(database, account, 'published')
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 204
    with database.transaction() as connection:
        row = connection.execute("SELECT status, account_name FROM video_publish_targets WHERE id = 'target'").fetchone()
        assert row['status'] == 'published'
        assert row['account_name'] == '我的账号'


def test_logout_keeps_existing_historical_account_name(tmp_path, monkeypatch):
    database, project, account, client = setup_account(tmp_path, monkeypatch)
    (project / 'cookies' / 'xiaohongshu_my_account.json').write_text('credential', encoding='utf-8')
    create_target(database, account, 'published')
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_targets SET account_name = '发布时昵称' WHERE id = 'target'")
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 204
    with database.transaction() as connection:
        assert connection.execute("SELECT account_name FROM video_publish_targets WHERE id = 'target'").fetchone()['account_name'] == '发布时昵称'


def test_logout_uses_sau_platform_name_for_wechat_channels(tmp_path, monkeypatch):
    database, project, _account, client = setup_account(tmp_path, monkeypatch)
    account = add_video_publish_account(database, 'wechat_channels', 'wx_account')
    update_video_publish_account_status(database, account['id'], 'connected', '有效')
    credential = project / 'cookies' / 'tencent_wx_account.json'
    credential.write_text('credential', encoding='utf-8')
    with client:
        response = client.delete('/api/v1/video-publish/accounts/' + account['id'])
    assert response.status_code == 204
    assert not credential.exists()
