from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace

import pytest

from app.workbench.database import Database
from app.workbench.models import StepKind
from app.workbench.providers import TEXT_VISION_MODEL_ID
from app.workbench.providers.openai_compatible import ZhipuOpenAICompatibleProvider
from app.workbench.runner import RunnerError
from app.workbench.steps import model_call


def test_chat_has_longer_timeout_and_sends_client_request_id(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self, _):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def urlopen(request, timeout):
        captured['body'] = json.loads(request.data)
        captured['timeout'] = timeout
        return Response()

    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    provider = ZhipuOpenAICompatibleProvider('key')
    provider.set_client_request_id('local-request-123')
    assert provider.analyze_text('prompt', 'text', TEXT_VISION_MODEL_ID).text == 'ok'
    assert captured['timeout'] == 600
    assert captured['body']['request_id'] == 'local-request-123'


def test_explicit_timeout_override_is_respected_for_chat(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self, _): return b'{"choices":[{"message":{"content":"ok"}}]}'
    seen = []
    monkeypatch.setattr('urllib.request.urlopen', lambda _request, timeout: (seen.append(timeout) or Response()))
    provider = ZhipuOpenAICompatibleProvider('key', timeout_seconds=3)
    provider.analyze_text('prompt', 'text', TEXT_VISION_MODEL_ID)
    assert seen == [3]


def test_audio_request_keeps_existing_timeout_and_does_not_add_chat_request_id(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self, _): return b'{"text":"ok"}'
    seen = []
    def urlopen(request, timeout):
        seen.append((request.data, timeout))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    provider = ZhipuOpenAICompatibleProvider('key')
    provider.set_client_request_id('local-request-123')
    provider._request('/audio/transcriptions', b'original audio body', 'audio/wav')
    assert seen == [(b'original audio body', 90)]


def test_chat_timeout_keeps_client_id_separate_from_server_id(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','url','now')")
        c.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','running','now','now')")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','analyze_comments',0,'running','[]','[]','[]')")
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('a1','t','s','note','p','h',1,'{}','now')")
        c.execute("INSERT INTO consents (id,task_id,step_kind,provider_id,model_id,artifact_ids_json,authorized,authorized_at) VALUES ('c','t','analyze_comments','zhipu',?,'[]',1,'now')", (TEXT_VISION_MODEL_ID,))
    context = SimpleNamespace(task=SimpleNamespace(id='t'), step=SimpleNamespace(id='s',kind=StepKind.ANALYZE_COMMENTS), repository=SimpleNamespace(database=db), cancel_event=threading.Event(), app_config=SimpleNamespace(get_settings=lambda: {'model_provider':'zhipu','model_id':TEXT_VISION_MODEL_ID}))
    sent = []
    def timeout(request, timeout):
        sent.append(json.loads(request.data)['request_id'])
        raise socket.timeout()
    monkeypatch.setattr('urllib.request.urlopen', timeout)
    monkeypatch.setattr(model_call, 'get_model_api_key', lambda: 'key')
    with pytest.raises(RunnerError) as err:
        model_call.authorized_model_call(context,model_id=TEXT_VISION_MODEL_ID,input_artifact_ids=['a1'],operation=lambda provider: provider.analyze_text('prompt','text',TEXT_VISION_MODEL_ID))
    assert err.value.code == 'provider_timeout'
    with db.transaction() as c:
        row = c.execute('select request_id,usage_json,error_code from model_runs').fetchone()
    assert row['request_id'] is None
    assert json.loads(row['usage_json'])['input_detail']['client_request_id'] == sent[0]
    assert row['error_code'] == 'provider_timeout'


def test_latest_consent_wins_when_authorization_timestamps_match(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','url','now')")
        c.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','running','now','now')")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','analyze_comments',0,'running','[]','[]','[]')")
        for artifact_id, version in [('old', 1), ('new', 2)]:
            c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES (?,'t','s','note','p','h',?,'{}','now')", (artifact_id, version))
        for consent_id, artifact_id in [('old-consent', 'old'), ('new-consent', 'new')]:
            c.execute("INSERT INTO consents (id,task_id,step_kind,provider_id,model_id,artifact_ids_json,authorized,authorized_at) VALUES (?, 't','analyze_comments','zhipu',?,?,1,'same-time')", (consent_id,TEXT_VISION_MODEL_ID,json.dumps([artifact_id])))
    context = SimpleNamespace(task=SimpleNamespace(id='t'), step=SimpleNamespace(id='s',kind=StepKind.ANALYZE_COMMENTS), repository=SimpleNamespace(database=db), cancel_event=threading.Event(), app_config=SimpleNamespace(get_settings=lambda: {'model_provider':'zhipu','model_id':TEXT_VISION_MODEL_ID}))
    provider = SimpleNamespace(set_client_request_id=lambda _: None)
    monkeypatch.setattr(model_call, 'get_model_api_key', lambda: 'key')
    monkeypatch.setattr(model_call, 'create_provider', lambda *_args, **_kwargs: provider)
    response = SimpleNamespace(text='ok', request_id='provider-id')
    assert model_call.authorized_model_call(context,model_id=TEXT_VISION_MODEL_ID,input_artifact_ids=['new'],operation=lambda _: response) is response
