"""Compose one immutable, local 视频资料.md from reviewed task artifacts."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from .steps.reconcile_transcript import count_copy_words


def extract_review_copy(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == "## 校对文案"), None)
    if start is None:
        raise ValueError("人工校对稿缺少‘校对文案’章节。")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = [line for line in lines[start + 1:end] if not line.strip().startswith("字数：")]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if len(body) == 1 and body[0].strip() == "（未取得语音转写文本）":
        return ""
    return "\n".join(body)


def extract_ocr_section(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == "## 画面文字证据（OCR）"), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = lines[start + 1:end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def render_initial_video_notes(*, comments_markdown: str, archive_metadata: dict[str, Any], comment_analysis_markdown: str) -> bytes:
    lines = [comments_markdown.rstrip(), "", "## 媒体归档信息", ""]
    for key, label in (("author", "作者"), ("published_at", "发布时间"), ("duration_seconds", "时长（秒）"), ("sha256", "媒体 SHA-256"), ("cover_source", "封面来源")):
        value = archive_metadata.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            lines.append(f"- {label}：{_plain(str(value))}")
    width, height = archive_metadata.get("width"), archive_metadata.get("height")
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        lines.append(f"- 分辨率：{width}×{height}")
    if archive_metadata.get("full_decode_verified") is True:
        lines.append("- 媒体完整解码校验：通过")
    analysis = comment_analysis_markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if analysis and analysis[0].startswith("# "):
        analysis = analysis[1:]
    lines.extend(["", "## 评论精选与摘要", "", *[f"#{line}" if line.startswith("#") else line for line in analysis], ""])
    return "\n".join(lines).encode("utf-8")


def render_video_notes(
    *,
    title: str | None,
    platform: str | None,
    source_id: str | None,
    source_url: str,
    author: str | None,
    published_at: str | None,
    archived_at: str | None,
    transcript_markdown: str,
    transcript_artifact_id: str,
    source_artifact_ids: list[str],
    comments_markdown: str | None,
    created_at: datetime,
    archive_metadata: dict[str, Any] | None = None,
    comment_analysis_markdown: str | None = None,
) -> tuple[bytes, int]:
    copy = extract_review_copy(transcript_markdown)
    word_count = count_copy_words(copy)
    ocr = extract_ocr_section(transcript_markdown)
    safe_title = _plain(title or source_id or "未命名素材")
    archive_metadata = archive_metadata or {}
    lines = [
        f"# {safe_title} · 视频资料",
        "",
        f"- 平台：{_plain(platform or '未获取')}",
        f"- 作品 ID：{_plain(source_id or '未获取')}",
        f"- 来源：{_plain(source_url or '未获取')}",
        f"- 作者：{_plain(author or '未获取')}",
        f"- 发布时间：{_plain(published_at or '未获取')}",
        f"- 归档时间：{_plain(archived_at or '未获取')}",
        f"- 处理时间：{created_at.astimezone().isoformat(timespec='seconds')}",
    ]
    duration = archive_metadata.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        lines.append(f"- 时长（秒）：{duration}")
    width, height = archive_metadata.get("width"), archive_metadata.get("height")
    if type(width) is int and type(height) is int and width > 0 and height > 0:
        lines.append(f"- 分辨率：{width}×{height}")
    for field, label in (("sha256", "媒体 SHA-256"), ("cover_source", "封面来源")):
        if isinstance(archive_metadata.get(field), str):
            lines.append(f"- {label}：{_plain(archive_metadata[field])}")
    if archive_metadata.get("full_decode_verified") is True:
        lines.append("- 媒体完整解码校验：通过")
    lines.extend([
        f"- 校对文案字数：{word_count}（汉字逐字计数，连续英文/数字串计 1；排除标题、元数据、空白和标点）",
        f"- 校对稿产物 ID：`{_code(transcript_artifact_id)}`",
        "",
        "## 校对文案",
        "",
        copy or "（未取得语音转写文本）",
        "",
        "## 画面文字证据（OCR，独立于语音）",
        "",
        ocr or "（没有已授权的 OCR 结果）",
        "",
    ])
    if comments_markdown:
        lines.extend(["## 评论采集", "", _comment_sections(comments_markdown), ""])
    else:
        lines.extend(["## 评论采集", "", "（尚无已生成的评论归档章节。）", ""])
    if comment_analysis_markdown:
        analysis_lines = comment_analysis_markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if analysis_lines and analysis_lines[0].startswith("# "):
            analysis_lines = analysis_lines[1:]
        lines.extend(["## 评论精选与摘要", "", *[f"#{line}" if line.startswith("# ") or line.startswith("## ") or line.startswith("### ") else line for line in analysis_lines], ""])
    lines.extend([
        "## 产物来源",
        "",
        "原始 ASR、OCR JSON 和关键帧仍以独立产物保存；此文档只引用 ID，不替换原始记录。",
        "",
        f"- 人工校对稿：`{_code(transcript_artifact_id)}`",
    ])
    for artifact_id in dict.fromkeys(source_artifact_ids):
        if artifact_id and artifact_id != transcript_artifact_id:
            lines.append(f"- 输入证据：`{_code(artifact_id)}`")
    lines.append("")
    return "\n".join(lines).encode("utf-8"), word_count


def _comment_sections(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heading = next((i for i, line in enumerate(lines) if line.strip() == "## 评论"), None)
    if heading is None:
        return "（评论任务未生成可合并的评论章节。）"
    separator = next((i for i in range(heading + 1, len(lines)) if lines[i].strip() == "---"), len(lines))
    selected_labels = ("评论范围：", "本次新增：", "评论排序：", "停止原因：", "采集时间：")
    metadata = [line for line in lines[1:heading] if line.startswith("- ") and any(label in line for label in selected_labels)]
    body = lines[heading + 1:separator]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if separator < len(lines):
        missing = next((line for line in lines[separator + 1:] if line.startswith("字段缺失计数：")), None)
        if missing:
            metadata.append(f"- {missing}")
    return "\n".join([*metadata, "", *body]).strip() or "（本次没有可归档的评论。）"


def _plain(value: str) -> str:
    escaped = html.escape(value.replace("\x00", "").replace("\r", " ").replace("\n", " "), quote=False)
    return re.sub(r"([\\`*_{}\[\]#+|>])", r"\\\1", escaped)[:2000]


def _code(value: str) -> str:
    return value.replace("`", "&#96;").replace("\n", "")[:128]


__all__ = ["extract_ocr_section", "extract_review_copy", "render_video_notes"]
