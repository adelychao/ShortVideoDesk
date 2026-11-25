"""分层评论分析：本地去重、有限预算和可回查的逐批结果。"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from ..archive import _assert_contained
from ..comment_analysis_policy import batch_input, choose_groups, group_comments, partition_groups
from ..runner import RunnerError, TaskCancelled
from .artifact_store import register_bytes
from .model_call import authorized_model_call, configured_model_id, configured_provider_id

_VERSION = 'comment-economy-v1'
_PROMPT = ('请对每条评论只返回分类，JSON格式为{"items":[{"n":1,"labels":["Q"]}]}。'
           'n必须与输入序号一致，每条最多三个标签。Q提问，T选题请求，V经验观点，F纠错反馈，'
           'I普通互动，S垃圾信息，U无法判断；不要输出评论原文、理由或总结。')
_LABELS = {'Q': '观众提问', 'T': '选题请求', 'V': '经验与观点', 'F': '纠错与具体反馈',
           'I': '普通互动', 'S': '垃圾信息', 'U': '待复核'}
_MAX_REPAIR_ITEMS = 25


def _spent(context, options):
    input_tokens = output_tokens = 0
    unknown = False
    with context.repository.database.transaction() as connection:
        rows = connection.execute('SELECT status, usage_json FROM model_runs WHERE task_id=? AND step_id=?', (context.task.id, context.step.id)).fetchall()
    for row in rows:
        usage = json.loads(row['usage_json'] or '{}')
        inp, out = usage.get('input_tokens'), usage.get('output_tokens')
        if row['status'] != 'completed' or not isinstance(inp, int) or not isinstance(out, int):
            unknown = True
            continue
        input_tokens += inp
        output_tokens += out
    price_in = Decimal(str(options['input_price_per_million']))
    price_out = Decimal(str(options['output_price_per_million']))
    amount = (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / Decimal(1_000_000)
    return input_tokens, output_tokens, amount, unknown


def _cached(context, source_id, model_id, provider_id, option_hash):
    with context.repository.database.transaction() as connection:
        rows = connection.execute("SELECT * FROM artifacts WHERE task_id=? AND step_id=? AND kind='comment_analysis_raw' ORDER BY created_at DESC", (context.task.id, context.step.id)).fetchall()
    result = {}
    for row in rows:
        try:
            metadata = json.loads(row['metadata_json'])
            if (not isinstance(metadata, dict) or metadata.get('prompt_version') != _VERSION or metadata.get('option_hash') != option_hash
                    or metadata.get('source_artifact_ids') != [source_id] or metadata.get('model_id') != model_id
                    or metadata.get('provider_id') != provider_id):
                continue
            original_path = Path(row['path'])
            if original_path.is_symlink():
                continue
            path = _assert_contained(original_path, context.archive_root, strict=True)
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != row['sha256']:
                continue
            value = json.loads(content.decode('utf-8'))
            if isinstance(value, dict) and isinstance(value.get('raw_response_text'), str):
                key = (int(value['batch_index']), value.get('repair') is True)
                result.setdefault(key, (value, str(row['id'])))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return result


def _has_prior_attempt(context, batch_index, repair):
    with context.repository.database.transaction() as connection:
        rows = connection.execute('SELECT usage_json FROM model_runs WHERE task_id=? AND step_id=?', (context.task.id, context.step.id)).fetchall()
    for row in rows:
        try:
            detail = json.loads(row['usage_json'] or '{}').get('input_detail', {})
            if (detail.get('prompt_version') == _VERSION and detail.get('batch_index') == batch_index
                    and (detail.get('repair') is True) == repair):
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def _read_labels(raw: str, batch_size: int) -> dict[int, list[str]]:
    from .analyze_comments import _parse_object

    try:
        items = _parse_object(raw).get('items')
    except RunnerError:
        return {}
    by_index = {}
    if not isinstance(items, list):
        return by_index
    for item in items:
        if not isinstance(item, dict) or type(item.get('n')) is not int or not isinstance(item.get('labels'), list):
            continue
        n = item['n']
        if n < 1 or n > batch_size or n in by_index:
            continue
        labels = list(dict.fromkeys(_LABELS[label] for label in item['labels'] if isinstance(label, str) and label in _LABELS))[:3]
        if labels:
            by_index[n] = labels
    return by_index


def analyze_economically(context, comments, options):
    from .analyze_comments import CommentAnalysis, ClassifiedComment, SelectedComment, _comment_note_artifact

    groups = group_comments(comments)
    candidates = choose_groups(groups, str(options['mode']), int(options['candidate_limit']))
    batches = partition_groups(candidates, int(options['batch_size']), 96 * 1024)
    source_id = _comment_note_artifact(context).id
    model_id, provider_id = configured_model_id(context), configured_provider_id(context)
    cache_options = {key: options[key] for key in ('mode', 'batch_size', 'candidate_limit', 'max_output_tokens')}
    option_hash = hashlib.sha256(json.dumps(cache_options, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    cached = _cached(context, source_id, model_id, provider_id, option_hash)
    classified = []
    selected = []
    questions = []
    themes = []
    processed_ids = []
    attempted_ids = set()
    raw_batches = []
    completed_batches = 0
    stop_reason = None

    def publish_partial():
        inp, out, spent, unknown = _spent(context, options)
        counts = {label: 0 for label in _LABELS.values()}
        for row in classified:
            for label in row.labels:
                counts[label] += 1
        payload = {'schema_version': 3, 'model_id': model_id, 'provider_id': provider_id,
            'source_artifact_ids': [source_id], 'selected': [asdict(item) for item in selected],
            'classifications': [asdict(item) for item in classified], 'processed_comment_ids': list(processed_ids),
            'themes': list(themes), 'audience_questions': list(questions), 'disagreements': [], 'claims_to_verify': [],
            'classification_counts': counts, 'batches': list(raw_batches),
            'coverage': {'mode': options['mode'], 'original_count': len(comments), 'unique_text_count': len(groups),
                'candidate_text_count': len(candidates), 'analyzed_original_count': len(processed_ids),
                'attempted_original_count': len(attempted_ids), 'not_selected_original_count': len(comments) - sum(len(group.comments) for group in candidates),
                'unanalysed_original_count': len(comments) - len(processed_ids), 'completed_batches': completed_batches,
                'total_batches': len(batches), 'input_tokens': inp, 'output_tokens': out,
                'estimated_cost_cny': str(spent), 'unknown_cost': unknown, 'budget_cny': options['budget_cny'], 'token_budget': options['token_budget'],
                'stop_reason': None, 'running': True}}
        register_bytes(context, 'comment_analysis_json', '.json', json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            {'media_type': 'application/json', 'partial': True, 'source_artifact_ids': [source_id]})

    def request_batch(index, batch, *, repair=False):
        nonlocal stop_reason
        if context.cancel_event.is_set():
            raise TaskCancelled
        fingerprints = [group.fingerprint for group in batch]
        text = batch_input(batch)
        existing = cached.get((index, repair))
        if existing and existing[0].get('fingerprints') == fingerprints:
            attempted_ids.update(item.platform_comment_id for group in batch for item in group.comments)
            return existing[0]['raw_response_text'], existing[1]
        if _has_prior_attempt(context, index, repair):
            stop_reason = '该批次已有调用记录，但可复用结果缺失或不匹配；已停止，避免重复计费。'
            return None
        inp, out, spent, unknown = _spent(context, options)
        if unknown:
            stop_reason = '调用用量未知，已停止发送新批次，请核对服务商记录。'
            return None
        estimated_input = len((_PROMPT + text).encode('utf-8')) + 256
        output_limit = int(options['max_output_tokens'])
        if inp + out + estimated_input + output_limit > int(options['token_budget']):
            stop_reason = '预计下一次请求可能超过 Token 预算，已停止发送。'
            return None
        reservation = (Decimal(estimated_input) * Decimal(str(options['input_price_per_million'])) + Decimal(output_limit) * Decimal(str(options['output_price_per_million']))) / Decimal(1_000_000)
        if spent + reservation > Decimal(str(options['budget_cny'])):
            stop_reason = '预计下一次请求可能超过本次预算，已停止发送。'
            return None
        try:
            attempted_ids.update(item.platform_comment_id for group in batch for item in group.comments)
            response = authorized_model_call(context, model_id=model_id, input_artifact_ids=[source_id], consent_artifact_ids=[source_id],
                input_detail={'batch_index': index, 'batch_count': len(batches), 'comment_count': len(batch),
                    'mode': options['mode'], 'prompt_version': _VERSION, 'repair': repair},
                operation=lambda provider: provider.analyze_text(_PROMPT, text, model_id, max_output_tokens=output_limit))
        except RunnerError as exc:
            if context.cancel_event.is_set():
                raise TaskCancelled from exc
            stop_reason = '本批请求未完成，可能存在未知费用；请查看模型调用记录。'
            return None
        raw = str(response.text)
        artifact = register_bytes(context, 'comment_analysis_raw', '.json', json.dumps(
            {'batch_index': index, 'fingerprints': fingerprints, 'raw_response_text': raw, 'repair': repair}, ensure_ascii=False).encode('utf-8'),
            {'model_id': model_id, 'provider_id': provider_id, 'prompt_version': _VERSION, 'option_hash': option_hash,
                'batch_index': index, 'repair': repair, 'source_artifact_ids': [source_id]})
        return raw, artifact.id

    oversized = False
    for index, batch in enumerate(batches, 1):
        if context.cancel_event.is_set():
            raise TaskCancelled
        if len(batch_input(batch).encode('utf-8')) > 96 * 1024:
            oversized = True
            continue
        progress = round((index - 1) / max(len(batches), 1) * 90)
        context.progress(context.step.id, progress,
                         f'已完成 {completed_batches}/{len(batches)} 批，已分析 {len(processed_ids)} 条原评论')
        response = request_batch(index, batch)
        if response is None:
            break
        raw, artifact_id = response
        by_index = _read_labels(raw, len(batch))
        missing = [n for n in range(1, len(batch) + 1) if n not in by_index]
        repair_artifact_id = None
        if by_index and 0 < len(missing) <= _MAX_REPAIR_ITEMS:
            context.progress(context.step.id, progress, f'正在补查第 {index} 批缺失的 {len(missing)} 组评论')
            repair = request_batch(index, [batch[n - 1] for n in missing], repair=True)
            if repair is not None:
                repair_raw, repair_artifact_id = repair
                for n, labels in _read_labels(repair_raw, len(missing)).items():
                    by_index[missing[n - 1]] = labels
        batch_ids = []
        for n, group in enumerate(batch, 1):
            labels = by_index.get(n)
            if labels is None:
                continue
            for comment in group.comments:
                classified.append(ClassifiedComment(comment.platform_comment_id, labels))
                batch_ids.append(comment.platform_comment_id)
            if len(selected) < 20 and any(label in labels for label in ('观众提问', '选题请求', '经验与观点', '纠错与具体反馈')):
                representative = max(group.comments, key=lambda item: item.like_count or 0)
                selected.append(SelectedComment(representative.platform_comment_id, '有用', '模型分类的候选评论，请人工复核。', representative.text, representative.like_count))
            if '观众提问' in labels and len(questions) < 20:
                questions.append('评论原文：' + group.text[:500])
            if any(label in labels for label in ('选题请求', '经验与观点', '纠错与具体反馈')) and len(themes) < 20:
                themes.append('评论原文：' + group.text[:500])
        processed_ids.extend(batch_ids)
        report = {'batch_index': index, 'processed_comment_ids': batch_ids, 'raw_artifact_id': artifact_id}
        if repair_artifact_id is not None:
            report['repair_raw_artifact_id'] = repair_artifact_id
        raw_batches.append(report)
        if len(by_index) < len(batch):
            stop_reason = stop_reason or f'第 {index} 批只返回 {len(by_index)}/{len(batch)} 组有效分类，已保留有效结果。'
            break
        completed_batches += 1
        publish_partial()
    if stop_reason is None and oversized:
        stop_reason = '存在超过单批输入上限的评论，请查看原文并单独处理。'
    setattr(context, '_comment_analysis_raw', raw_batches)
    inp, out, spent, unknown = _spent(context, options)
    setattr(context, '_comment_analysis_coverage', {'mode': options['mode'], 'original_count': len(comments), 'unique_text_count': len(groups),
        'candidate_text_count': len(candidates), 'analyzed_original_count': len(processed_ids), 'unanalysed_original_count': len(comments) - len(processed_ids),
        'attempted_original_count': len(attempted_ids), 'not_selected_original_count': len(comments) - sum(len(group.comments) for group in candidates),
        'completed_batches': completed_batches, 'total_batches': len(batches), 'input_tokens': inp, 'output_tokens': out,
        'estimated_cost_cny': str(spent), 'unknown_cost': unknown, 'budget_cny': options['budget_cny'], 'token_budget': options['token_budget'], 'stop_reason': stop_reason})
    return CommentAnalysis(selected, classified, themes, questions, [], [], processed_ids)
