from app.workbench.database import Database
from app.workbench.video_publishing import (
    SauResult, VideoPublishManager, add_video_publish_account,
)


def test_missing_browser_login_error_is_shown(tmp_path):
    database = Database(tmp_path / 'workbench.sqlite3')
    database.migrate()
    account = add_video_publish_account(database, 'xiaohongshu', 'test_account', '测试')
    manager = VideoPublishManager(database)
    project = tmp_path / 'sau'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname="sau"', encoding='utf-8')
    manager.adapter.set_project_path(str(project))
    manager.adapter.login = lambda *_args: SauResult(1, "BrowserType.launch: Executable doesn't exist at C:\\browser\\chrome.exe\nPlease run playwright install")
    manager._run_account_action(account['id'], 'login')
    with database.transaction() as connection:
        row = connection.execute('SELECT status, message FROM video_publish_accounts WHERE id = ?', (account['id'],)).fetchone()
    assert row['status'] == 'error'
    assert '浏览器' in row['message']
    assert '安装' in row['message']
