import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.workbench.bilibili_options import fetch_bilibili_categories


def test_bilibili_categories_use_login_and_reject_invalid_responses(tmp_path):
    account = tmp_path / 'account.json'
    account.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'example'}]}}), encoding='utf-8')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'code': 0, 'data': {'type_list': [{'id': 1010, 'name': '知识'}, {'id': 1028, 'name': 'vlog'}]}}
    with patch('app.workbench.bilibili_options.requests.get', return_value=Response()) as get:
        assert fetch_bilibili_categories(account) == [{'id': 1010, 'name': '知识'}, {'id': 1028, 'name': 'vlog'}]
        assert get.call_args.kwargs['cookies']['SESSDATA'] == 'example'
    class Expired(Response):
        def json(self): return {'code': -101, 'message': 'expired'}
    with patch('app.workbench.bilibili_options.requests.get', return_value=Expired()):
        with pytest.raises(ValueError): fetch_bilibili_categories(account)


def test_predicted_legacy_tid_uses_creator_result(tmp_path):
    from app.workbench.bilibili_options import predict_bilibili_legacy_tid
    account = tmp_path / 'account.json'
    account.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'example'}, {'name': 'bili_jct', 'value': 'csrf'}]}}), encoding='utf-8')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'code': 0, 'data': [{'id': 122}]}
    with patch('app.workbench.bilibili_options.requests.post', return_value=Response()):
        assert predict_bilibili_legacy_tid(account, '视频标题') == 122


def test_adapter_passes_predicted_legacy_tid_and_new_settings(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand, SauResult
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_categories', lambda account: [{'id': 1028, 'name': 'vlog'}])
    monkeypatch.setattr('app.workbench.bilibili_options.predict_bilibili_legacy_tid', lambda account, title: 21)
    captured = []
    monkeypatch.setattr(adapter, 'run', lambda argv, on_start, timeout: captured.extend(argv) or SauResult(0, ''))
    result = adapter.upload({'platform_id': 'bilibili', 'account_alias': 'creator', 'media_path': str(tmp_path / 'video.mp4'), 'title': '标题', 'description': '简介', 'tags': [], 'options': {'bilibili_category': 1028, 'bilibili_ai': True, 'bilibili_private': True, 'bilibili_dynamic': '动态'}}, lambda proc: None)
    assert result.returncode == 0
    assert captured[captured.index('--tid') + 1] == '21'
    assert captured[captured.index('--human-type2') + 1] == '1028'
    assert '--private' in captured and '--creation-statement' in captured
    assert captured[captured.index('--dynamic') + 1] == '动态'


def test_adapter_stops_when_prediction_fails(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_categories', lambda account: [{'id': 1028, 'name': 'vlog'}])
    def fail(*args): raise ValueError('获取分区失败')
    monkeypatch.setattr('app.workbench.bilibili_options.predict_bilibili_legacy_tid', fail)
    monkeypatch.setattr(adapter, 'run', lambda *args: pytest.fail('must not submit'))
    result = adapter.upload({'platform_id': 'bilibili', 'account_alias': 'creator', 'media_path': str(tmp_path / 'video.mp4'), 'title': '标题', 'description': '简介', 'tags': [], 'options': {'bilibili_category': 1028}}, lambda proc: None)
    assert result.launch_error == '获取分区失败'


def test_category_endpoint_uses_selected_connected_account(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.workbench import api
    from app.workbench.video_publishing import SauCommand
    app = FastAPI()
    app.include_router(api._router)
    manager = SimpleNamespace(database=object(), adapter=SimpleNamespace(resolve_command=lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), '')))
    monkeypatch.setattr(api, '_video_publish_manager', lambda request: manager)
    monkeypatch.setattr(api, 'list_video_publish_accounts', lambda db: [{'id': 'a', 'platform_id': 'bilibili', 'status': 'connected', 'account_alias': 'creator'}])
    monkeypatch.setattr(api, 'fetch_bilibili_categories', lambda p: [{'id': 1028, 'name': 'vlog'}] if p == tmp_path / 'cookies' / 'bilibili_creator.json' else [])
    with TestClient(app, base_url='http://testserver') as client:
        result = client.get('/api/v1/video-publish/accounts/a/bilibili-categories')
        assert result.status_code == 200
        assert result.json() == {'items': [{'id': 1028, 'name': 'vlog'}]}
        assert client.get('/api/v1/video-publish/accounts/missing/bilibili-categories').status_code == 404


def test_bilibili_schedule_requires_five_minutes_and_within_fifteen_days():
    from datetime import datetime
    from app.workbench.video_publishing import normalize_video_publish_schedule, VideoPublishError
    now = datetime(2026, 11, 3, 12, 0)
    with pytest.raises(VideoPublishError): normalize_video_publish_schedule('2026-11-03T12:04', ['bilibili'], now=now)
    assert normalize_video_publish_schedule('2026-11-03T12:05', ['bilibili'], now=now) == '2026-11-03 12:05'
    with pytest.raises(VideoPublishError): normalize_video_publish_schedule('2026-11-18T12:01', ['bilibili'], now=now)


def test_batch_preserves_bilibili_selection_and_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.workbench.database import Database
    from app.workbench import video_publishing as publish
    db = Database(tmp_path / 'test.sqlite')
    db.migrate()
    with db.transaction(write=True) as connection:
        connection.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','bilibili','creator','connected','2026-11-02','2026-11-02')")
    media = tmp_path / 'video.mp4'
    media.write_bytes(b'test')
    monkeypatch.setattr(publish, 'get_upload', lambda *a, **kw: SimpleNamespace(path=media, filename='video.mp4', sha256='hash', detected_media_type='video'))
    monkeypatch.setattr(publish, '_copy_video_to_managed_storage', lambda *a: media)
    monkeypatch.setattr(publish, 'delete_upload', lambda *a: None)
    monkeypatch.setattr(publish, 'get_video_publish_cover_upload', lambda key: object())
    monkeypatch.setattr(publish, '_copy_video_publish_cover_to_batch', lambda cover, batch, key: key + '.jpg')
    monkeypatch.setattr(publish, 'delete_video_publish_cover_upload', lambda *a: None)
    options = {'bilibili_category': 1028, 'bilibili_statement': 3, 'bilibili_no_reprint': True, 'bilibili_private': True, 'bilibili_dynamic': '动态', 'cover_4_3_upload_id': 'a' * 32, 'cover_16_9_upload_id': 'b' * 32}
    batch = publish.create_video_publish_batch(db, archive_root=None, idempotency_key='1234567890abcdef', asset_id=None, upload_id='u', title='标题', description='简介', tags=[], schedule_at=None, targets=[{'account_id': 'a', 'options': options}])
    saved = batch['targets'][0]['options']
    for field in ('bilibili_category', 'bilibili_statement', 'bilibili_no_reprint', 'bilibili_private', 'bilibili_dynamic'):
        assert saved[field] == options[field]


def test_fetch_creation_statements_from_creator_pre(tmp_path):
    from app.workbench.bilibili_options import fetch_bilibili_statements
    account = tmp_path / 'account.json'
    account.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'example'}]}}), encoding='utf-8')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'code': 0, 'data': {'neutral_mark': {'mark_list': [{'id': -1, 'content': '内容无需标注'}, {'id': 1, 'content': '含AI生成内容'}, {'id': -2, 'content': '内容为转载'}]}}}
    with patch('app.workbench.bilibili_options.requests.get', return_value=Response()):
        assert fetch_bilibili_statements(account) == [{'id': -1, 'name': '内容无需标注'}, {'id': 1, 'name': '含AI生成内容'}, {'id': -2, 'name': '内容为转载'}]


def test_creation_statement_api_fields_support_repost_and_authorization():
    from app.workbench.api import VideoPublishOptionsRequest
    options = VideoPublishOptionsRequest(bilibili_statement=-2, bilibili_source='https://example.com', bilibili_no_reprint=False)
    assert options.model_dump(exclude_none=True)['bilibili_statement'] == -2
    with pytest.raises(ValueError): VideoPublishOptionsRequest(bilibili_statement=99)


def test_statement_endpoint_returns_creator_options(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.workbench import api
    from app.workbench.video_publishing import SauCommand
    app = FastAPI()
    app.include_router(api._router)
    manager = SimpleNamespace(database=object(), adapter=SimpleNamespace(resolve_command=lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), '')))
    monkeypatch.setattr(api, '_video_publish_manager', lambda request: manager)
    monkeypatch.setattr(api, 'list_video_publish_accounts', lambda db: [{'id': 'a', 'platform_id': 'bilibili', 'status': 'connected', 'account_alias': 'creator'}])
    monkeypatch.setattr(api, 'fetch_bilibili_statements', lambda p: [{'id': 1, 'name': '含AI生成内容'}])
    with TestClient(app, base_url='http://testserver') as client:
        result = client.get('/api/v1/video-publish/accounts/a/bilibili-statements')
        assert result.status_code == 200
        assert result.json()['items'][0]['id'] == 1


def test_adapter_passes_repost_statement_and_source(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand, SauResult
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_categories', lambda account: [{'id': 1028, 'name': 'vlog'}])
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_statements', lambda account: [{'id': -2, 'name': '内容为转载'}])
    monkeypatch.setattr('app.workbench.bilibili_options.predict_bilibili_legacy_tid', lambda account, title: 21)
    captured = []
    monkeypatch.setattr(adapter, 'run', lambda argv, on_start, timeout: captured.extend(argv) or SauResult(0, ''))
    target = {'platform_id': 'bilibili', 'account_alias': 'creator', 'media_path': str(tmp_path / 'video.mp4'), 'title': '标题', 'description': '简介', 'tags': [], 'options': {'bilibili_category': 1028, 'bilibili_statement': -2, 'bilibili_source': 'https://example.com'}}
    assert adapter.upload(target, lambda proc: None).returncode == 0
    assert captured[captured.index('--creation-statement') + 1] == '-2'
    assert captured[captured.index('--source') + 1] == 'https://example.com'


def test_adapter_stops_when_statement_is_no_longer_available(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SocialAutoUploadAdapter, SauCommand
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_categories', lambda account: [{'id': 1028, 'name': 'vlog'}])
    monkeypatch.setattr('app.workbench.bilibili_options.fetch_bilibili_statements', lambda account: [{'id': 1, 'name': '含AI生成内容'}])
    monkeypatch.setattr(adapter, 'run', lambda *args: pytest.fail('must not submit'))
    target = {'platform_id': 'bilibili', 'account_alias': 'creator', 'media_path': str(tmp_path / 'video.mp4'), 'title': '标题', 'description': '简介', 'tags': [], 'options': {'bilibili_category': 1028, 'bilibili_statement': 3}}
    assert adapter.upload(target, lambda proc: None).launch_error == '该B站账号的创作声明已变化，请重新选择。'


@pytest.mark.parametrize('options, message', [
    ({'bilibili_category': 1028, 'bilibili_statement': -2}, '转载视频需要填写转载来源'),
    ({'bilibili_category': 1028, 'bilibili_statement': -2, 'bilibili_source': 'https://example.com', 'bilibili_no_reprint': True}, '转载视频不能选择'),
    ({'bilibili_category': 1028, 'bilibili_statement': 4, 'bilibili_source': 'https://example.com'}, '只有转载视频'),
])
def test_invalid_bilibili_repost_options_are_rejected(tmp_path, options, message):
    from app.workbench.database import Database
    from app.workbench.video_publishing import create_video_publish_batch, VideoPublishError
    db = Database(tmp_path / 'test.sqlite')
    db.migrate()
    with db.transaction(write=True) as connection:
        connection.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','bilibili','creator','connected','2026-11-02','2026-11-02')")
    with pytest.raises(VideoPublishError, match=message):
        create_video_publish_batch(db, archive_root=None, idempotency_key='1234567890abcdef', asset_id=None, upload_id='u', title='标题', description='简介', tags=[], schedule_at=None, targets=[{'account_id': 'a', 'options': options}])
