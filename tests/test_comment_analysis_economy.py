from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace

from app.workbench.database import Database
from app.workbench.models import CommentRecord
from app.workbench.steps import analyze_comments_economy as economy
from app.workbench.steps import analyze_comments as legacy


def make_comment(index, text):
    return CommentRecord(id=str(index), platform_comment_id='platform-' + str(index), parent_id=None, author=None,
                         text=text, like_count=0, created_at=None, collected_at=datetime.now(timezone.utc))


def make_context(tmp_path, monkeypatch):
    db = Database(tmp_path / 'isolated.sqlite3')
    db.migrate()
    context = SimpleNamespace(task=SimpleNamespace(id='t'), step=SimpleNamespace(id='s'),
        repository=SimpleNamespace(database=db), archive_root=tmp_path, cancel_event=threading.Event(), progress=lambda *_: None)
    monkeypatch.setattr(economy, 'configured_model_id', lambda _: 'model')
    monkeypatch.setattr(economy, 'configured_provider_id', lambda _: 'provider')
    monkeypatch.setattr(legacy, '_comment_note_artifact', lambda _: SimpleNamespace(id='source'))
    monkeypatch.setattr(economy, 'register_bytes', lambda *_args, **_kwargs: SimpleNamespace(id='raw'))
    return context


def options(**overrides):
    return {'mode': 'economy', 'candidate_limit': 100, 'batch_size': 25, 'budget_cny': 1.0,
        'token_budget': 100000, 'max_output_tokens': 1000, 'input_price_per_million': 0.8,
        'output_price_per_million': 2.8, **overrides}


def test_economy_maps_one_deduplicated_result_to_original_comments(tmp_path, monkeypatch):
    context = make_context(tmp_path, monkeypatch)
    calls = []
    def call(_context, **kwargs):
        calls.append(kwargs['input_detail'])
        count = kwargs['input_detail']['comment_count']
        return SimpleNamespace(text=json.dumps({'items': [{'n': i, 'labels': ['Q']} for i in range(1, count + 1)]}))
    monkeypatch.setattr(economy, 'authorized_model_call', call)
    result = economy.analyze_economically(context, [make_comment(1, '为什么？'), make_comment(2, ' 为什么？ ')], options())
    assert len(calls) == 1 and calls[0]['comment_count'] == 1
    assert len(result.classifications) == 2
    assert all(row.labels == ['观众提问'] for row in result.classifications)
    assert context._comment_analysis_coverage['analyzed_original_count'] == 2


def test_economy_does_not_send_when_next_batch_exceeds_budget(tmp_path, monkeypatch):
    context = make_context(tmp_path, monkeypatch)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('model request must not be sent')
    monkeypatch.setattr(economy, 'authorized_model_call', forbidden)
    result = economy.analyze_economically(context, [make_comment(1, '为什么？')], options(budget_cny=0.000001))
    assert result.processed_comment_ids == []
    assert context._comment_analysis_coverage['unanalysed_original_count'] == 1
    assert '预算' in context._comment_analysis_coverage['stop_reason']


def test_unknown_previous_usage_stops_before_another_request(tmp_path, monkeypatch):
    from decimal import Decimal
    context = make_context(tmp_path, monkeypatch)
    monkeypatch.setattr(economy, '_spent', lambda *_: (0, 0, Decimal('0'), True))
    def forbidden(*_args, **_kwargs):
        raise AssertionError('unknown usage must prevent another request')
    monkeypatch.setattr(economy, 'authorized_model_call', forbidden)
    result = economy.analyze_economically(context, [make_comment(1, '下一期能讲什么？')], options())
    assert not result.processed_comment_ids
    assert context._comment_analysis_coverage['unknown_cost'] is True


def test_missing_cached_result_does_not_repeat_a_previous_request(tmp_path, monkeypatch):
    context = make_context(tmp_path, monkeypatch)
    monkeypatch.setattr(economy, '_has_prior_attempt', lambda *_: True)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('previously billed batch must not be sent again')
    monkeypatch.setattr(economy, 'authorized_model_call', forbidden)
    result = economy.analyze_economically(context, [make_comment(1, '为什么？')], options())
    assert not result.processed_comment_ids
    assert '重复计费' in context._comment_analysis_coverage['stop_reason']


def test_incomplete_model_response_is_not_claimed_as_analyzed(tmp_path, monkeypatch):
    context = make_context(tmp_path, monkeypatch)
    monkeypatch.setattr(economy, 'authorized_model_call', lambda *_args, **_kwargs: SimpleNamespace(text='{"items": []}'))
    result = economy.analyze_economically(context, [make_comment(1, '怎么处理？')], options())
    assert not result.processed_comment_ids
    assert context._comment_analysis_coverage['unanalysed_original_count'] == 1


def test_provider_sends_configured_output_limit(monkeypatch):
    from app.workbench.providers.openai_compatible import ZhipuOpenAICompatibleProvider
    provider = ZhipuOpenAICompatibleProvider('fake-key')
    monkeypatch.setattr(provider, '_require_model_modality', lambda *_: None)
    captured = {}
    def request(_endpoint, body, _content_type, **_kwargs):
        captured.update(json.loads(body))
        return {'choices': [{'message': {'content': '{}'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
    monkeypatch.setattr(provider, '_request', request)
    provider.analyze_text('prompt', 'text', 'model', max_output_tokens=1024)
    assert captured['max_tokens'] == 1024
