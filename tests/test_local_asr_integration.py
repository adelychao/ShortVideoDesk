from types import SimpleNamespace
import sys
import threading
import time

import pytest

from app.workbench import api
from app.workbench.database import Database
from app.workbench.models import StepKind


def test_local_transcription_needs_no_cloud_credential_or_consent():
    runner = SimpleNamespace(config=SimpleNamespace(get_settings=lambda: {}))
    result = api._validate_step_consents(runner, ['import', 'transcribe'], [], transcription_mode='local')
    assert result == []


def test_auto_device_uses_cpu_if_cuda_runtime_missing(monkeypatch):
    from app.workbench import local_asr
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace(get_cuda_device_count=lambda: 1))
    monkeypatch.setattr(local_asr.ctypes, 'WinDLL' if local_asr.os.name == 'nt' else 'CDLL',
                        lambda _: (_ for _ in ()).throw(OSError('missing library')))
    assert local_asr.resolve_device('auto') == 'cpu'
    assert local_asr.resolve_device('cuda') == 'cuda'


def test_task_request_supports_local_transcription_choices():
    body = api.CreateTaskRequest(steps=['import', 'transcribe'], upload_id='abc',
                                 transcription_mode='local', local_model='small',
                                 local_language='zh', local_device='cpu')
    assert (body.transcription_mode, body.local_model, body.local_language, body.local_device) == ('local', 'small', 'zh', 'cpu')


def test_local_choice_is_persisted_per_task(tmp_path):
    from app.workbench.local_asr import save_task_options, get_task_options
    db = Database(tmp_path / 'state.sqlite3')
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','file','now')")
        c.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','queued','now','now')")
    save_task_options(db, 't', model='small', language='zh', device='cpu')
    assert get_task_options(db, 't') == {'model': 'small', 'language': 'zh', 'device': 'cpu'}
    assert get_task_options(db, 'other') is None


def test_local_model_requires_complete_files(tmp_path, monkeypatch):
    from app.workbench import local_asr
    monkeypatch.setattr(local_asr, 'MODEL_ROOT', tmp_path)
    assert not local_asr.model_ready('small')
    model_dir = tmp_path / 'small'
    model_dir.mkdir()
    (model_dir / 'model.bin').write_bytes(b'example')
    assert not local_asr.model_ready('small')


def test_model_without_vocabulary_is_not_ready_even_with_legacy_manifest(tmp_path, monkeypatch):
    import json
    from app.workbench import local_asr
    monkeypatch.setattr(local_asr, 'MODEL_ROOT', tmp_path)
    model_dir = tmp_path / 'small'
    model_dir.mkdir()
    files = {'model.bin': 3, 'config.json': 3, 'tokenizer.json': 3}
    for name in files:
        (model_dir / name).write_bytes(b'123')
    (model_dir / 'workbench-model.json').write_text(json.dumps({'files': files}), encoding='utf-8')
    assert not local_asr.model_ready('small')


def test_local_transcription_saves_text_and_does_not_call_cloud(tmp_path, monkeypatch):
    from app.workbench import local_asr
    from app.workbench.steps import local_transcribe
    monkeypatch.setattr(local_asr, 'model_ready', lambda _: True)
    monkeypatch.setattr(local_asr, '_model_dir', lambda _: tmp_path)
    media = SimpleNamespace(id='media', path=str(tmp_path / 'video.wav'), sha256='abc')
    monkeypatch.setattr(local_transcribe, 'registered_media', lambda _: media)
    monkeypatch.setattr(local_transcribe, 'probe_media', lambda _: SimpleNamespace(has_audio=True, duration_seconds=10))
    monkeypatch.setattr(local_transcribe, 'extract_audio_chunks', lambda *_: [SimpleNamespace(path=tmp_path / 'audio.wav', start_seconds=0, end_seconds=10)])
    observed = {}
    class FakeModel:
        def __init__(self, path, **kwargs): observed.update(path=path, **kwargs)
        def detect_language(self, **kwargs): return ('zh', 0.99, [])
        def transcribe(self, path, **kwargs):
            observed.update(media=path, **kwargs)
            return iter([SimpleNamespace(start=0.5, end=2.0, text='  繁體中文的語音轉寫  ')]), SimpleNamespace(language='zh')
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=FakeModel, decode_audio=lambda *args, **kwargs: 'audio'))
    artifacts = {}
    def register(_context, kind, ext, content, metadata):
        artifacts[kind] = (content, metadata)
        return SimpleNamespace(id=kind)
    monkeypatch.setattr(local_transcribe, 'register_bytes', register)
    context = SimpleNamespace(cancel_event=threading.Event(), step=SimpleNamespace(id='step'), progress=lambda *_: None)
    result = local_transcribe.transcribe_local(context, {'model':'small', 'language':'auto', 'device':'cpu'})
    assert observed['local_files_only'] is True and observed['language'] == 'zh'
    assert observed['initial_prompt']
    assert observed['device'] == 'cpu'
    import json
    raw = json.loads(artifacts['transcript_raw'][0])
    assert raw['chunks'][0]['text'] == '繁體中文的語音轉寫'
    assert raw['chunks'][0]['normalized_text'] == '繁体中文的语音转写'
    assert '繁体中文的语音转写' in artifacts['transcript'][0].decode('utf-8')
    assert '繁体中文的语音转写' in artifacts['transcript_draft'][0].decode('utf-8')
    assert artifacts['transcript'][1]['script_conversion'] == 'opencc_t2s'
    assert artifacts['transcript'][1]['provider_id'] == 'local-faster-whisper'
    assert result.artifact_ids == ['transcript_raw', 'transcript', 'transcript_draft']


def test_local_english_does_not_receive_chinese_prompt_or_conversion(tmp_path, monkeypatch):
    import json
    from app.workbench import local_asr
    from app.workbench.steps import local_transcribe
    monkeypatch.setattr(local_asr, 'model_ready', lambda _: True)
    monkeypatch.setattr(local_asr, '_model_dir', lambda _: tmp_path)
    monkeypatch.setattr(local_transcribe, 'registered_media', lambda _: SimpleNamespace(id='m',path='audio',sha256='s'))
    monkeypatch.setattr(local_transcribe, 'probe_media', lambda _: SimpleNamespace(has_audio=True,duration_seconds=2))
    monkeypatch.setattr(local_transcribe, 'extract_audio_chunks', lambda *_: [SimpleNamespace(path=tmp_path/'a.wav',start_seconds=0,end_seconds=2)])
    observed = {}
    class FakeModel:
        def __init__(self, *_args, **_kwargs): pass
        def detect_language(self, **kwargs): return ('en',0.99,[])
        def transcribe(self, path, **kwargs):
            observed.update(kwargs)
            return iter([SimpleNamespace(start=0,end=1,text='English 漢字')]), SimpleNamespace(language='en')
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=FakeModel, decode_audio=lambda *args, **kwargs: 'audio'))
    output = {}
    def register(_, kind, ext, content, metadata):
        output[kind] = content
        return SimpleNamespace(id=kind)
    monkeypatch.setattr(local_transcribe, 'register_bytes', register)
    ctx = SimpleNamespace(cancel_event=threading.Event(),step=SimpleNamespace(id='s'),progress=lambda *_:None)
    local_transcribe.transcribe_local(ctx, {'model':'small','language':'auto','device':'cpu'})
    assert observed['language'] == 'en' and not observed.get('initial_prompt')
    assert 'English 漢字' in output['transcript'].decode('utf-8')
    assert json.loads(output['transcript_raw'])['chunks'][0]['text'] == 'English 漢字'


def test_ocr_uses_normalized_local_text_and_supports_legacy_cloud():
    from app.workbench.steps.ocr import _asr_chunk_text
    assert _asr_chunk_text({'text':'原始繁體','normalized_text':'原始繁体'}) == '原始繁体'
    assert _asr_chunk_text({'text':'本地旧记录'}) == '本地旧记录'
    assert _asr_chunk_text({'raw_response_text':'云端旧记录'}) == '云端旧记录'


@pytest.mark.parametrize(('chunk', 'expected'), [
    ({'text':'原始繁體', 'normalized_text':'原始繁体'}, '原始繁体'),
    ({'text':'舊版原文'}, '舊版原文'),
    ({'raw_response_text':'云端原文'}, '云端原文'),
])
def test_ocr_reads_verified_asr_records_compatibly(tmp_path, chunk, expected):
    import hashlib
    import json
    from app.workbench.repository import TaskRepository
    from app.workbench.steps.ocr import _latest_asr_text
    archive = tmp_path / 'archive'
    output = archive / 'asset'
    output.mkdir(parents=True)
    payload = json.dumps({'chunks': [chunk]}, ensure_ascii=False).encode('utf-8')
    raw_path = output / 'raw.json'
    raw_path.write_bytes(payload)
    database = Database(tmp_path / 'state.sqlite3')
    database.migrate()
    with database.transaction(write=True) as c:
        c.execute("INSERT INTO assets (id,identity_key,source_kind,created_at) VALUES ('a','a','file','now')")
        c.execute("INSERT INTO tasks (id,asset_id,source_json,status,created_at,updated_at) VALUES ('t','a','{}','running','now','now')")
        c.execute("INSERT INTO task_steps (id,task_id,kind,ordinal,status,depends_on_json,input_artifact_ids_json,output_artifact_ids_json) VALUES ('s','t','transcribe',0,'completed','[]','[]','[]')")
        c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('raw','t','s','transcript_raw',?,?,1,?,'now')", (str(raw_path),hashlib.sha256(payload).hexdigest(),json.dumps({'source_artifact_id':'media','source_sha256':'abc'})))
    context = SimpleNamespace(task=SimpleNamespace(id='t'),repository=TaskRepository(database),archive_root=archive)
    assert _latest_asr_text(context, 'media', 'abc') == (expected, ['raw'])


def test_local_transcription_never_falls_back_to_cloud_when_model_missing(tmp_path, monkeypatch):
    from app.workbench import local_asr
    from app.workbench.runner import RunnerError
    from app.workbench.steps.local_transcribe import transcribe_local
    monkeypatch.setattr(local_asr, 'model_ready', lambda _: False)
    context = SimpleNamespace(cancel_event=threading.Event())
    with pytest.raises(RunnerError) as error:
        transcribe_local(context, {'model':'small', 'language':'auto', 'device':'cpu'})
    assert error.value.code == 'local_asr_model_missing'


def test_prepare_requires_preview_and_creates_ready_marker_after_download(tmp_path, monkeypatch):
    from app.workbench import local_asr
    monkeypatch.setattr(local_asr, 'MODEL_ROOT', tmp_path)
    monkeypatch.setattr(local_asr, '_previews', {})
    monkeypatch.setattr(local_asr, '_jobs', {})
    names = ['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt']
    revision = 'a' * 40
    info = SimpleNamespace(sha=revision, siblings=[SimpleNamespace(rfilename=name, size=3) for name in names])
    def download(**kwargs):
        assert kwargs['revision'] == revision and kwargs['token'] is False
        for name in names:
            (tmp_path / 'tiny' / name).write_bytes(b'123')
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda: SimpleNamespace(model_info=lambda *args, **kwargs: info), snapshot_download=download))
    with pytest.raises(ValueError):
        local_asr.start_download('tiny', revision)
    preview = local_asr.preview_model('tiny')
    assert preview['size_bytes'] == 12 and preview['revision'] == revision
    local_asr.start_download('tiny', revision)
    for _ in range(100):
        if local_asr.status()['models'][0]['job']['state'] != 'downloading':
            break
        time.sleep(.01)
    assert local_asr.model_ready('tiny')
    assert local_asr.status()['models'][0]['job']['state'] == 'ready'


def test_local_task_creation_persists_choice_before_enqueue(tmp_path, monkeypatch):
    from app.workbench.config import AppConfig
    from app.workbench.local_asr import get_task_options
    from app.workbench.runner import TaskRunner
    db = Database(tmp_path / 'state.sqlite3')
    db.migrate()
    archive = tmp_path / 'archive'
    archive.mkdir()
    config = AppConfig(db)
    config.update_settings({'archive_root': str(archive)})
    runner = TaskRunner(database=db, config=config)
    monkeypatch.setattr(runner, 'start', lambda: None)
    captured = []
    monkeypatch.setattr(runner, 'enqueue', lambda task_id, **kwargs: captured.append(get_task_options(db, task_id)))
    monkeypatch.setattr(api, '_runner', lambda _: runner)
    monkeypatch.setattr(api.local_asr, 'status', lambda: {'installed': True})
    monkeypatch.setattr(api.local_asr, 'model_ready', lambda _: True)
    body = api.CreateTaskRequest(source_url='https://www.bilibili.com/video/BV1xx411c7mD',
                                 steps=['download','transcribe'], transcription_mode='local',
                                 local_model='small', local_language='zh', local_device='cpu')
    api.create_task(body, None)
    assert captured == [{'model':'small', 'language':'zh', 'device':'cpu'}]
    with db.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM consents').fetchone()[0] == 0


def test_local_task_still_requires_consent_for_cloud_text(monkeypatch):
    from fastapi import HTTPException
    runner = SimpleNamespace(config=SimpleNamespace(get_settings=lambda: {'model_provider':'zhipu','model_id':'glm-5.3-flash'}))
    monkeypatch.setattr(api, '_model_configuration_ready', lambda *args, **kwargs: True)
    monkeypatch.setattr(api, 'lookup_model_capability', lambda *args: (None, None, None))
    with pytest.raises(HTTPException) as error:
        api._validate_step_consents(runner, ['download','transcribe','analyze_structure'], [], transcription_mode='local')
    assert error.value.status_code == 422
