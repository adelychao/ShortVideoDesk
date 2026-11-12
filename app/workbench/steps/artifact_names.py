"""Readable names for user-facing Markdown artifacts."""
from __future__ import annotations

_NAMES = {
    'transcript': '语音转写', 'transcript_draft': '转写校对草稿', 'transcript_revision': '转写人工校对',
    'ocr_result': '画面文字OCR', 'ocr_revision': '画面文字人工校对',
    'comment_analysis': '评论精选与摘要', 'analysis': '内容结构分析',
    'script': '口播稿', 'script_revision': '口播稿人工修订', 'final_script': '口播稿定稿',
    'knowledge_card': '知识卡片', 'knowledge_card_revision': '知识卡片人工修订',
    'knowledge_card_approved': '知识卡片已审核', 'video_notes': '视频资料',
}

def _safe_style_label(value: str) -> str:
    cleaned = ''.join(
        character for character in value
        if character not in '<>:"/\\|?*' and ord(character) >= 32
    ).strip().rstrip('.')
    return cleaned[:40]


def artifact_filename(kind: str, extension: str, artifact_id: str, metadata: dict[str, object] | None = None, *, conflict: bool = False) -> str:
    if extension != '.md' or kind not in _NAMES:
        return f'{kind}-{artifact_id}{extension}'
    label = _NAMES[kind]
    style = (metadata or {}).get('style')
    if kind in {'script', 'script_revision', 'final_script'} and isinstance(style, str):
        style_label = _safe_style_label(style)
        if style_label:
            label += f'-{style_label}'
    if conflict:
        label += '-冲突副本'
    return f'{label}-{artifact_id[:12]}{extension}'
