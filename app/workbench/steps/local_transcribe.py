"""Offline transcription of registered media with a prepared faster-whisper model."""
from __future__ import annotations

import json
from pathlib import Path

from .. import local_asr
from ..models import StepStatus
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled
from .artifact_store import register_bytes, registered_media
from .media_processing import extract_audio_chunks, private_tempdir, probe_media
from .reconcile_transcript import count_copy_words, reconcile_transcript, render_review_markdown

_SIMPLIFIED_PROMPT = '以下是普通话的简体中文转写。'


def transcribe_local(context: StepContext, options: dict) -> StepResult:
    if context.cancel_event.is_set():
        raise TaskCancelled
    model_name, language, device = options['model'], options['language'], options['device']
    if not local_asr.model_ready(model_name):
        raise RunnerError('local_asr_model_missing', '本地模型未准备完成，请在设置中准备模型后重试。', status_code=409)
    media = registered_media(context)
    path = Path(media.path)
    probe = probe_media(path)
    if not probe.has_audio:
        raise RunnerError('audio_track_missing', '所选媒体没有可转写的音轨。', status_code=422)
    context.progress(context.step.id, 1, '正在加载本地语音模型')
    try:
        from faster_whisper import WhisperModel, decode_audio
        actual_device = local_asr.resolve_device(device)
        context.progress(context.step.id, 2, f'正在使用本机 {"GPU" if actual_device == "cuda" else "CPU"} 转写')
        model = WhisperModel(str(local_asr._model_dir(model_name)), device=actual_device, local_files_only=True)
        chunks = []
        detected = None
        converter = None
        converted_chinese = False
        with private_tempdir('local-asr') as temp_path:
            for audio in extract_audio_chunks(path, Path(temp_path), probe.duration_seconds):
                if context.cancel_event.is_set():
                    raise TaskCancelled
                if language == 'auto':
                    audio_data = decode_audio(str(audio.path), sampling_rate=16000)
                    chunk_language, _, _ = model.detect_language(audio=audio_data)
                else:
                    audio_data = str(audio.path)
                    chunk_language = language
                prompt = _SIMPLIFIED_PROMPT if chunk_language == 'zh' else None
                if chunk_language == 'zh' and converter is None:
                    try:
                        from opencc import OpenCC
                        converter = OpenCC('t2s')
                    except (ImportError, OSError) as exc:
                        raise RunnerError('local_asr_opencc_missing', '简体转换组件不可用，请安装项目依赖后重试。', status_code=503) from exc
                segments, info = model.transcribe(audio_data, language=chunk_language, initial_prompt=prompt)
                detected = getattr(info, 'language', None) or detected
                for segment in segments:
                    if context.cancel_event.is_set():
                        raise TaskCancelled
                    start = audio.start_seconds + float(segment.start)
                    end = min(audio.end_seconds, audio.start_seconds + float(segment.end))
                    original = segment.text.strip()
                    chunk = {'start_seconds': round(start, 3), 'end_seconds': round(end, 3),
                             'text': original, 'language': chunk_language}
                    if chunk_language == 'zh':
                        chunk['normalized_text'] = converter.convert(original)
                        converted_chinese = True
                    chunks.append(chunk)
                    context.progress(context.step.id, min(94, max(2, end / max(probe.duration_seconds, 0.001) * 90)),
                                     f'本地转写进行中，已处理至 {end:.1f} 秒')
    except TaskCancelled:
        raise
    except RunnerError:
        raise
    except Exception as exc:
        raise RunnerError('local_asr_failed', '本地转写失败，请检查模型文件和设备环境；未切换云端。', status_code=503) from exc
    if context.cancel_event.is_set():
        raise TaskCancelled
    text = '\n'.join(item.get('normalized_text', item['text']) for item in chunks if item['text'])
    metadata = {'provider_id': 'local-faster-whisper', 'model_id': model_name,
                'device': actual_device, 'language': detected or language, 'source_artifact_id': media.id,
                'source_sha256': media.sha256, 'source_duration_seconds': probe.duration_seconds,
                'timing_note': '时间范围由本地转写模型生成，可能存在偏差。', 'chunk_count': len(chunks)}
    raw = register_bytes(context, 'transcript_raw', '.json',
                         json.dumps({'schema_version': 1, **metadata, 'chunks': chunks}, ensure_ascii=False).encode('utf-8'),
                         {'media_type': 'application/json', **metadata})
    lines = ['# 语音转写', '', f'- 转写方式：本地 faster-whisper / {model_name}',
             f'- 输入素材：{media.id}', f"- 时间说明：{metadata['timing_note']}", '']
    for item in chunks:
        lines.extend([f"**{item['start_seconds']:.1f}–{item['end_seconds']:.1f} 秒**", '', item.get('normalized_text', item['text']), ''])
    transcript_metadata = {'media_type': 'text/markdown', **metadata, 'raw_response_artifact_id': raw.id}
    if converted_chinese:
        transcript_metadata['script_conversion'] = 'opencc_t2s'
    transcript = register_bytes(context, 'transcript', '.md', '\n'.join(lines).encode('utf-8'),
                                transcript_metadata)
    draft = reconcile_transcript(text, source_artifact_ids=[media.id, raw.id, transcript.id])
    review_metadata = {'media_type': 'text/markdown', 'source_artifact_ids': draft.source_artifact_ids,
                       'uncertain_terms': draft.uncertain_terms, 'word_count': count_copy_words(draft.text),
                       'word_count_rule': 'Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded',
                       'human_review_required': True}
    if converted_chinese:
        review_metadata['script_conversion'] = 'opencc_t2s'
    review = register_bytes(context, 'transcript_draft', '.md', render_review_markdown(draft), review_metadata)
    context.progress(context.step.id, 100, '本地语音转写已保存，等待校对')
    return StepResult(status=StepStatus.COMPLETED.value, artifact_ids=[raw.id, transcript.id, review.id],
                      metadata={'result': {'transcript_artifact_id': transcript.id, 'raw_artifact_id': raw.id,
                                           'draft_artifact_id': review.id, 'chunk_count': len(chunks),
                                           'timing_is_approximate': True, 'character_count': len(text),
                                           'review_word_count': count_copy_words(text)}})
