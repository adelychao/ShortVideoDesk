import json
from types import SimpleNamespace

import pytest

from app.workbench import xhs_poi
from app.workbench.api import VideoPublishOptionsRequest
from app.workbench.database import Database
from app.workbench.video_publishing import VideoPublishManager


def test_poi_results_keep_the_platform_identity_and_address(tmp_path, monkeypatch):
    account = tmp_path / 'account.json'
    account.write_text(json.dumps({'cookies': [{'name': 'a1', 'value': 'secret', 'domain': '.xiaohongshu.com', 'path': '/', 'expires': -1}]}), encoding='utf-8')
    cli = tmp_path / 'main.js'
    cli.touch()
    monkeypatch.setattr(xhs_poi, 'CATBUS_CLI', cli)
    monkeypatch.setattr(xhs_poi.shutil, 'which', lambda name: 'node')
    def run(args, **kwargs):
        assert 'secret' not in ' '.join(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'data': [{'id': 'B123', 'name': '公园', 'address': '上海市公园路'}, {'id': '', 'name': '无标识', 'address': '地址'}]}), stderr='')
    monkeypatch.setattr(xhs_poi.subprocess, 'run', run)
    assert xhs_poi.search_poi(account, '公园') == [{'id': 'B123', 'name': '公园', 'address': '上海市公园路'}]


def test_platform_failure_is_not_treated_as_an_empty_result(tmp_path, monkeypatch):
    account = tmp_path / 'account.json'
    account.write_text('{"cookies": []}', encoding='utf-8')
    cli = tmp_path / 'main.js'; cli.touch()
    monkeypatch.setattr(xhs_poi, 'CATBUS_CLI', cli)
    monkeypatch.setattr(xhs_poi.shutil, 'which', lambda name: 'node')
    monkeypatch.setattr(xhs_poi.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"ok": false}', stderr=''))
    with pytest.raises(xhs_poi.XhsPoiError):
        xhs_poi.search_poi(account, '上海')


def test_selected_identity_is_preserved_in_request():
    request = VideoPublishOptionsRequest(xhs_location='公园', xhs_location_id='B123', xhs_location_address='上海市公园路')
    assert request.model_dump(exclude_none=True)['xhs_location_id'] == 'B123'


def test_search_uses_the_selected_connected_xhs_account(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite'); db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','xiaohongshu','user','connected','2026-11-02','2026-11-02')")
    account = tmp_path / 'account.json'; account.write_text('{}', encoding='utf-8')
    manager = VideoPublishManager(db)
    monkeypatch.setattr(manager, '_xiaohongshu_cookie_paths', lambda *args: (account, account))
    monkeypatch.setattr(xhs_poi, 'search_poi', lambda path, keyword: [{'id': 'B123', 'name': keyword, 'address': '上海市公园路'}] if path == account else [])
    assert manager.search_xhs_poi('a', '公园')['items'][0]['id'] == 'B123'
