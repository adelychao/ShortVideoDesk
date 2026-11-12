"""Build a source-separated local transcript review draft."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Sequence


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_OR_NUMBER = re.compile(r"[A-Za-z0-9]+")
_OCR_CLAUSE_SEPARATOR = re.compile(r"[，,。！？!?；;：:\n\r]+")
_REVIEW_COPY_EVIDENCE_HEADINGS = frozenset({
    "## OCR 证据支持的自动校正（待人工复核）",
    "## 画面文字证据（OCR）",
    "## 人工核对提示",
    "## 评论反馈参考",
})


@dataclass(frozen=True)
class TranscriptDraft:
    text: str
    uncertain_terms: list[dict[str, str]]
    source_artifact_ids: list[str]
    corrections: list[dict[str, Any]] = field(default_factory=list)


def reconcile_transcript(
    asr_text: str,
    ocr_frames: Sequence[dict[str, Any]] | None = None,
    source_artifact_ids: Sequence[str] = (),
    asr_segments: Sequence[dict[str, Any]] | None = None,
) -> TranscriptDraft:
    """Build a review draft with only repeated, time-aligned OCR corrections."""
    text = _normalize_text(asr_text)
    corrections: list[dict[str, Any]] = []
    segments = list(asr_segments or [])
    segment_texts = [_segment_text(item) for item in segments]
    # Require the timed source segments to reproduce the exact ASR body before
    # applying edits. This prevents a stale or mismatched timing map from
    # changing unrelated text.
    if text and segments and "\n".join(item for item in segment_texts if item) == text:
        corrected_segments: list[str] = []
        for segment, segment_text in zip(segments, segment_texts):
            if not segment_text:
                continue
            start = _finite_time(segment.get("start_seconds"))
            end = _finite_time(segment.get("end_seconds"))
            if start is None or end is None or end <= start:
                corrected_segments.append(segment_text)
                continue
            corrected, segment_corrections = _correct_segment_with_ocr(
                segment_text, start, end, ocr_frames or ()
            )
            corrected_segments.append(corrected)
            corrections.extend(segment_corrections)
        text = "\n".join(corrected_segments)

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in _LATIN_OR_NUMBER.findall(text):
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "term": token,
            "source": "asr",
            "reason": "自动提取的英文或数字片段，请人工对照原音核对；系统未判定其正确或错误。",
        })
    return TranscriptDraft(
        text=text,
        uncertain_terms=candidates,
        source_artifact_ids=list(dict.fromkeys(item for item in source_artifact_ids if item)),
        corrections=corrections,
    )


def render_review_markdown(
    draft: TranscriptDraft,
    *,
    ocr_frames: Sequence[dict[str, Any]] | None = None,
) -> bytes:
    frames = list(ocr_frames or [])
    lines = [
        "# 视频资料 · 转写校对草稿",
        "",
        "> 草稿待人工核对。高置信度 OCR 证据可在正文中生成校正建议；原始 ASR 与 OCR 证据分别保存，自动校正仍需人工复核。",
        "",
        "## 校对文案",
        "",
        _escape_markdown(draft.text) if draft.text else "（未取得语音转写文本）",
        "",
        f"字数：{count_copy_words(draft.text)}（仅计算本段正文；汉字逐字计数，连续英文/数字串计 1，排除空白和标点）",
        "",
    ]
    lines.extend(["## OCR 证据支持的自动校正（待人工复核）", ""])
    if draft.corrections:
        for item in draft.corrections:
            at = _safe_float(item.get("start_seconds"))
            end = _safe_float(item.get("end_seconds"))
            frame_ids = ", ".join(_safe_label(value, "未标记关键帧") for value in item.get("frame_ids", []))
            frame_times = ", ".join(f"{_safe_float(value):.1f} 秒" for value in item.get("frame_times_seconds", []))
            asr_phrase = _escape_markdown(str(item.get("asr_text", "")))
            corrected_phrase = _escape_markdown(str(item.get("corrected_text", "")))
            ocr_phrase = _escape_markdown(str(item.get("ocr_text", "")))
            evidence_detail = "、".join(value for value in (frame_ids, frame_times) if value) or "时间未知"
            lines.append(
                f"- 约 {at:.1f}–{end:.1f} 秒：ASR「{asr_phrase}」→ 校对稿「{corrected_phrase}」；"
                f"OCR 两帧一致证据「{ocr_phrase}」（{evidence_detail}）。"
            )
        lines.append("")
    else:
        lines.extend(["未发现满足多帧一致且时间重叠条件的自动校正；OCR 结果保留在下方供人工对照。", ""])

    lines.extend([
        "## 画面文字证据（OCR）",
        "",
    ])
    if frames:
        for frame in sorted(frames, key=lambda item: _safe_float(item.get("sampled_at_seconds"))):
            frame_id = _safe_label(frame.get("frame_id"), "未标记关键帧")
            at = _safe_float(frame.get("sampled_at_seconds"))
            text = frame.get("text") if isinstance(frame.get("text"), str) else ""
            lines.extend([
                f"**{frame_id} · 约 {at:.1f} 秒**",
                "",
                _escape_markdown(text) if text.strip() else "（未识别到可见文字）",
                "",
            ])
    else:
        lines.extend(["（尚无已授权的 OCR 结果）", ""])

    lines.extend(["## 人工核对提示", ""])
    if draft.uncertain_terms:
        lines.extend([
            "以下英文或数字片段仅按字符模式提取，供对照原音复核，不代表模型置信度或已判定错误：",
            "",
        ])
        for item in draft.uncertain_terms:
            lines.append(f"- `{_escape_code(item['term'])}`：{item['reason']}")
        lines.append("")
    else:
        lines.extend(["- 未发现可自动列出的英文或数字片段。", "- 专有名词没有可靠置信度数据；请结合原音人工核对。", ""])
    if draft.uncertain_terms:
        lines.extend(["- 专有名词没有可靠置信度数据；请结合原音人工核对。", ""])
    return "\n".join(lines).encode("utf-8")


def count_copy_words(text: str) -> int:
    """Count Han characters plus contiguous ASCII letter/digit runs."""
    normalized = _normalize_text(text)
    return len(_HAN.findall(normalized)) + len(_LATIN_OR_NUMBER.findall(normalized))


def count_review_copy_words(markdown: str) -> int:
    return count_copy_words(extract_review_copy_text(markdown))


def extract_review_copy_text(markdown: str) -> str:
    """Return only the editable body from a review Markdown document."""
    lines = _normalize_text(markdown).split("\n")
    heading = next((index for index, line in enumerate(lines) if line.strip() == "## 校对文案"), None)
    if heading is None:
        return _normalize_text(markdown)
    end = next((index for index in range(heading + 1, len(lines))
                if lines[index].strip() in _REVIEW_COPY_EVIDENCE_HEADINGS), len(lines))
    body = [line for line in lines[heading + 1:end] if not line.strip().startswith("字数：")]
    if len(body) == 1 and body[0].strip() == "（未取得语音转写文本）":
        return ""
    return "\n".join(body).strip()


def update_review_copy_word_count(markdown: str) -> tuple[str, int]:
    lines = _normalize_text(markdown).split("\n")
    heading = next((index for index, line in enumerate(lines) if line.strip() == "## 校对文案"), None)
    if heading is None:
        return markdown, count_copy_words(markdown)
    end = next((index for index in range(heading + 1, len(lines))
                if lines[index].strip() in _REVIEW_COPY_EVIDENCE_HEADINGS), len(lines))
    body = [line for line in lines[heading + 1:end] if not line.strip().startswith("字数：")]
    copy_text = "\n".join(body)
    count = 0 if copy_text.strip() == "（未取得语音转写文本）" else count_copy_words(copy_text)
    count_line = f"字数：{count}（仅计算本段正文；汉字逐字计数，连续英文/数字串计 1，排除空白和标点）"
    lines = lines[:heading + 1] + body + ["", count_line, ""] + lines[end:]
    return "\n".join(lines), count


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _segment_text(segment: object) -> str:
    if not isinstance(segment, dict):
        return ""
    for key in ("normalized_text", "raw_response_text", "text"):
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)
    return ""


def _finite_time(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 and result < float("inf") else None


def _correct_segment_with_ocr(
    source: str,
    start: float,
    end: float,
    frames: Sequence[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    eligible_frames = _frames_overlapping(start, end, frames)
    if len(eligible_frames) < 2:
        return source, []

    replacements: list[tuple[int, str]] = []
    corrections: list[dict[str, Any]] = []
    clauses = list(re.finditer(r"[^，,。！？!?；;：:\n\r]+", source))
    for match in clauses:
        clause = match.group(0)
        source_positions = [index for index, char in enumerate(clause) if _HAN.fullmatch(char)]
        source_han = "".join(clause[index] for index in source_positions)
        if len(source_han) < 5:
            continue

        proposals: dict[str, dict[str, Any]] = {}
        for frame in eligible_frames:
            frame_text = frame.get("text") if isinstance(frame.get("text"), str) else ""
            frame_id = _safe_label(frame.get("frame_id"), "未标记关键帧")
            frame_time = _frame_time(frame)
            for raw_ocr_clause in _OCR_CLAUSE_SEPARATOR.split(frame_text):
                ocr_han = "".join(_HAN.findall(raw_ocr_clause))
                if len(ocr_han) < 5:
                    continue
                alignment = _align_han(source_han, ocr_han)
                if alignment is None:
                    continue
                distance, operations = alignment
                substitutions = [(source_index, target_char) for operation, source_index, target_char in operations
                                 if operation == "substitute"]
                insertions_or_deletions = sum(operation in {"insert", "delete"} for operation, _, _ in operations)
                equal_count = sum(operation == "equal" for operation, _, _ in operations)
                similarity = (len(source_han) + len(ocr_han) - distance) / max(1, len(source_han) + len(ocr_han))
                if (
                    not substitutions
                    or insertions_or_deletions > 1
                    or equal_count < 4
                    or distance > 2
                    or similarity < 0.78
                ):
                    continue

                corrected_han = list(source_han)
                for source_index, target_char in substitutions:
                    corrected_han[source_index] = target_char
                corrected_clause = list(clause)
                for source_index, target_char in substitutions:
                    corrected_clause[source_positions[source_index]] = target_char
                corrected_clause_text = "".join(corrected_clause)
                proposal = proposals.setdefault(corrected_clause_text, {
                    "frame_ids": set(),
                    "frame_times_seconds": set(),
                    "ocr_text_counts": {},
                    "similarity": similarity,
                })
                proposal["frame_ids"].add(frame_id)
                if frame_time is not None:
                    proposal["frame_times_seconds"].add(frame_time)
                evidence_text = raw_ocr_clause.strip()
                if evidence_text:
                    counts = proposal["ocr_text_counts"]
                    counts[evidence_text] = counts.get(evidence_text, 0) + 1

        qualified = [
            (candidate, evidence)
            for candidate, evidence in proposals.items()
            if len(evidence["frame_ids"]) >= 2
        ]
        if len(qualified) != 1:
            continue
        corrected_clause_text, evidence = qualified[0]
        if corrected_clause_text == clause:
            continue
        counts = evidence["ocr_text_counts"]
        ocr_text = max(counts, key=counts.get) if counts else ""
        corrections.append({
            "start_seconds": start,
            "end_seconds": end,
            "asr_text": clause.strip(),
            "corrected_text": corrected_clause_text.strip(),
            "ocr_text": ocr_text,
            "frame_ids": sorted(evidence["frame_ids"]),
            "frame_times_seconds": sorted(evidence["frame_times_seconds"]),
            "confidence": "high_repeated_time_aligned_evidence",
            "reason": "至少两张时间重叠画面给出一致 OCR 候选，仅替换存在差异的汉字；需人工复核。",
        })
        replacements.append((match.start(), corrected_clause_text))

    for start_index, corrected_clause in reversed(replacements):
        end_index = start_index + len(next(match.group(0) for match in clauses if match.start() == start_index))
        source = source[:start_index] + corrected_clause + source[end_index:]
    return source, corrections


def _frames_overlapping(start: float, end: float, frames: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        frame_start = _finite_time(frame.get("start_seconds"))
        frame_end = _finite_time(frame.get("end_seconds"))
        if frame_start is not None and frame_end is not None and frame_end > frame_start:
            overlaps = max(start, frame_start) < min(end, frame_end)
        else:
            sampled_at = _frame_time(frame)
            # OCR sample timestamps are approximate; a small tolerance keeps
            # samples at chunk boundaries aligned without crossing distant speech.
            overlaps = sampled_at is not None and start - 0.5 <= sampled_at <= end + 0.5
        if not overlaps:
            continue
        frame_id = _safe_label(frame.get("frame_id"), f"frame-{index + 1:05d}")
        if frame_id in seen:
            continue
        seen.add(frame_id)
        normalized = dict(frame)
        normalized["frame_id"] = frame_id
        result.append(normalized)
    return result


def _frame_time(frame: dict[str, Any]) -> float | None:
    sampled = _finite_time(frame.get("sampled_at_seconds"))
    if sampled is not None:
        return sampled
    frame_start = _finite_time(frame.get("start_seconds"))
    frame_end = _finite_time(frame.get("end_seconds"))
    if frame_start is not None and frame_end is not None and frame_end >= frame_start:
        return (frame_start + frame_end) / 2
    return None


def _align_han(source: str, target: str) -> tuple[int, list[tuple[str, int, str]]] | None:
    if not source or not target or len(source) > 100 or len(target) > 100:
        return None
    rows, columns = len(source) + 1, len(target) + 1
    distance = [[0] * columns for _ in range(rows)]
    for i in range(rows):
        distance[i][0] = i
    for j in range(columns):
        distance[0][j] = j
    for i in range(1, rows):
        for j in range(1, columns):
            distance[i][j] = min(
                distance[i - 1][j] + 1,
                distance[i][j - 1] + 1,
                distance[i - 1][j - 1] + (source[i - 1] != target[j - 1]),
            )
    i, j = len(source), len(target)
    operations: list[tuple[str, int, str]] = []
    while i or j:
        if i and j and source[i - 1] == target[j - 1] and distance[i][j] == distance[i - 1][j - 1]:
            operations.append(("equal", i - 1, target[j - 1]))
            i -= 1
            j -= 1
        elif i and j and distance[i][j] == distance[i - 1][j - 1] + 1:
            operations.append(("substitute", i - 1, target[j - 1]))
            i -= 1
            j -= 1
        elif i and distance[i][j] == distance[i - 1][j] + 1:
            operations.append(("delete", i - 1, ""))
            i -= 1
        elif j and distance[i][j] == distance[i][j - 1] + 1:
            operations.append(("insert", i, target[j - 1]))
            j -= 1
        else:
            return None
    operations.reverse()
    return distance[-1][-1], operations


def _escape_markdown(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]#+|>])", r"\\\1", escaped)


def _escape_code(value: str) -> str:
    return value.replace("`", "&#96;").replace("\n", " ")


def _safe_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result >= 0 else 0.0


def _safe_label(value: object, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return re.sub(r"[^A-Za-z0-9_-]", "", value)[:64] or default


__all__ = [
    "TranscriptDraft",
    "count_copy_words",
    "count_review_copy_words",
    "extract_review_copy_text",
    "reconcile_transcript",
    "render_review_markdown",
    "update_review_copy_word_count",
]
