"""Analyze every collected comment without imposing a candidate-count cap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Callable

from ..archive import _assert_contained
from ..models import CommentRecord, StepStatus
from ..providers import get_model_capability
from ..model_profiles import task_model_settings
from ..runner import RunnerError, StepContext, StepResult, TaskCancelled
from ..repository import _artifact_from_row
from .artifact_store import register_bytes
from .model_call import authorized_model_call, configured_model_id, configured_provider_id


_FALLBACK_INPUT_BUDGET = 800_000
_CATEGORY_NAMES = frozenset({"有用", "高赞", "有趣"})


@dataclass(frozen=True)
class SelectedComment:
    comment_id: str
    category: str
    reason: str
    text: str
    like_count: int | None


@dataclass(frozen=True)
class CommentAnalysis:
    selected: list[SelectedComment]
    themes: list[str]
    audience_questions: list[str]
    disagreements: list[str]
    claims_to_verify: list[str]
    processed_comment_ids: list[str]


def rank_comment_candidates(comments: list[CommentRecord]) -> list[CommentRecord]:
    """Order by the actual platform likes; unknown likes always come last."""
    return sorted(
        comments,
        key=lambda comment: (
            comment.like_count is None,
            -(comment.like_count or 0),
            comment.platform_comment_id,
        ),
    )


def partition_comments(
    comments: list[CommentRecord],
    max_input_tokens: int,
    count_tokens: Callable[[str], int | None],
) -> list[list[CommentRecord]]:
    """Partition by measured tokens, or a conservative UTF-8 byte upper bound.

    The fallback budget leaves room for the system prompt and output. It limits
    request size, never the number of comments considered, and never truncates a
    comment. A single oversized comment is returned as its own batch so the
    provider can reject it explicitly rather than silently omitting text.
    """
    if max_input_tokens < 1:
        raise ValueError("max_input_tokens must be positive")
    if not comments:
        return []

    batches: list[list[CommentRecord]] = []
    current: list[CommentRecord] = []
    budget = min(max_input_tokens, _FALLBACK_INPUT_BUDGET)

    current_bytes = 2  # JSON list brackets
    for comment in comments:
        item_bytes = len(_comments_json([comment]).encode("utf-8")) - 2
        candidate_bytes = current_bytes + item_bytes + (1 if current else 0)
        if current and candidate_bytes > budget:
            if current:
                batches.append(current)
            current = [comment]
            current_bytes = item_bytes + 2
            continue
        candidate = [*current, comment]
        if candidate_bytes > budget:
            measured = count_tokens(_comments_json(candidate))
            if measured is not None and 0 <= measured <= max_input_tokens:
                current = candidate
                current_bytes = candidate_bytes
                continue
            if current:
                batches.append(current)
            current = [comment]
            current_bytes = item_bytes + 2
            continue
        current = candidate
        current_bytes = candidate_bytes
    if current:
        batches.append(current)
    return batches


def analyze_comments(
    context: StepContext,
    comments: list[CommentRecord],
    select_count: int = 20,
) -> CommentAnalysis:
    if select_count < 0:
        raise ValueError("select_count must not be negative")
    ranked = rank_comment_candidates(comments)
    if not ranked:
        return CommentAnalysis([], [], [], [], [], [])
    model_id = configured_model_id(context)

    comment_note = _comment_note_artifact(context)
    source_artifact_id = comment_note.id
    max_tokens = _model_input_limit(context)
    batches = partition_comments(ranked, max_tokens, lambda _text: None)
    raw_batches: list[dict[str, object]] = []
    selected_by_id: dict[str, tuple[str, str]] = {}
    themes: list[str] = []
    questions: list[str] = []
    disagreements: list[str] = []
    claims: list[str] = []

    for index, batch in enumerate(batches, start=1):
        if context.cancel_event.is_set():
            raise TaskCancelled
        ids = [comment.platform_comment_id for comment in batch]
        prompt = _analysis_prompt()
        user_text = _comments_json(batch)
        context.progress(
            context.step.id,
            round((index - 1) / max(1, len(batches)) * 90),
            f"正在分析第 {index}/{len(batches)} 批评论（本批 {len(batch)} 条）",
        )
        response = authorized_model_call(
            context,
            model_id=model_id,
            input_artifact_ids=[source_artifact_id],
            consent_artifact_ids=[source_artifact_id],
            input_detail={
                "batch_index": index,
                "batch_count": len(batches),
                "comment_ids": ids,
                "comment_count": len(ids),
                "partition_basis": "all-comments; token count unavailable, conservative UTF-8 byte budget",
            },
            operation=lambda provider, prompt=prompt, text=user_text: provider.analyze_text(  # type: ignore[attr-defined]
                prompt, text, model_id
            ),
        )
        if context.cancel_event.is_set():
            raise TaskCancelled
        raw_text = str(response.text)  # type: ignore[attr-defined]
        raw_artifact = register_bytes(
            context,
            "comment_analysis_raw",
            ".json",
            _json_bytes({"batch_index": index, "comment_ids": ids, "request_id": getattr(response, "request_id", None), "raw_response_text": raw_text}),
            {"media_type": "application/json", "model_id": model_id, "comment_ids": ids, "source_artifact_ids": [source_artifact_id]},
        )
        parsed = _parse_object(raw_text)
        batch_known = set(ids)
        for row in _object_list(parsed.get("selected")):
            comment_id = row.get("comment_id")
            category = row.get("category")
            reason = row.get("reason")
            if (
                isinstance(comment_id, str)
                and comment_id in batch_known
                and isinstance(category, str)
                and category in _CATEGORY_NAMES
                and isinstance(reason, str)
                and reason.strip()
            ):
                selected_by_id.setdefault(comment_id, (category, reason.strip()[:500]))
        themes.extend(_string_list(parsed.get("themes")))
        questions.extend(_string_list(parsed.get("audience_questions")))
        disagreements.extend(_string_list(parsed.get("disagreements")))
        claims.extend(_string_list(parsed.get("claims_to_verify")))
        raw_batches.append({
            "batch_index": index,
            "processed_comment_ids": ids,
            "request_id": getattr(response, "request_id", None),
            "raw_artifact_id": raw_artifact.id,
        })
    setattr(context, "_comment_analysis_raw", raw_batches)

    selected: list[SelectedComment] = []
    for comment in ranked if select_count else []:
        selected_value = selected_by_id.get(comment.platform_comment_id)
        if selected_value is None:
            continue
        selected.append(SelectedComment(
            comment_id=comment.platform_comment_id,
            category=selected_value[0],
            reason=selected_value[1],
            text=comment.text,
            like_count=comment.like_count,
        ))
        if len(selected) >= select_count:
            break

    return CommentAnalysis(
        selected=selected,
        themes=_unique_strings(themes),
        audience_questions=_unique_strings(questions),
        disagreements=_unique_strings(disagreements),
        claims_to_verify=_unique_strings(claims),
        processed_comment_ids=[comment.platform_comment_id for comment in ranked],
    )


def analyze_collected_comments(context: StepContext) -> StepResult:
    comments = _load_collected_comments(context)
    if context.cancel_event.is_set():
        raise TaskCancelled
    analysis = analyze_comments(context, comments)
    model_id = configured_model_id(context) if comments else None
    provider_id = configured_provider_id(context) if comments else None
    payload = {
        "schema_version": 1,
        "provider_id": provider_id,
        "model_id": model_id,
        "source_artifact_ids": [_comment_note_artifact(context).id] if comments else [],
        **asdict(analysis),
    }
    raw_batches = getattr(context, "_comment_analysis_raw", [])
    if raw_batches:
        payload["batches"] = raw_batches
    json_artifact = register_bytes(
        context,
        "comment_analysis_json",
        ".json",
        _json_bytes(payload),
        {
            "media_type": "application/json",
            "model_id": model_id,
            "processed_comment_ids": analysis.processed_comment_ids,
            "selected_count": len(analysis.selected),
            "comment_count": len(comments),
        },
    )
    markdown_artifact = register_bytes(
        context,
        "comment_analysis",
        ".md",
        _analysis_markdown(analysis).encode("utf-8"),
        {
            "media_type": "text/markdown",
            "model_id": model_id,
            "raw_artifact_id": json_artifact.id,
            "processed_comment_ids": analysis.processed_comment_ids,
            "source_artifact_ids": payload["source_artifact_ids"],
        },
    )
    summary_artifact = None
    summary_error = None
    try:
        from ..video_notes import render_initial_video_notes

        comment_note = _comment_note_artifact(context)
        comment_path = _assert_contained(Path(comment_note.path), context.archive_root, strict=True)
        with context.repository.database.transaction() as connection:
            metadata_row = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND kind = 'metadata' ORDER BY created_at DESC, id DESC LIMIT 1",
                (context.task.id,),
            ).fetchone()
        archive_metadata: dict[str, object] = {}
        source_ids = [comment_note.id, markdown_artifact.id]
        if metadata_row is not None:
            metadata_path = Path(str(metadata_row["path"]))
            if metadata_path.is_symlink():
                raise ValueError("metadata symbolic link")
            metadata_path = _assert_contained(metadata_path, context.archive_root, strict=True)
            if not metadata_path.is_file() or metadata_path.stat().st_size > 5_000_000 or _sha256(metadata_path) != str(metadata_row["sha256"]):
                raise ValueError("metadata changed")
            parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("metadata is not an object")
            archive_metadata = parsed
            source_ids.append(str(metadata_row["id"]))
        summary_artifact = register_bytes(
            context, "video_notes", ".md",
            render_initial_video_notes(
                comments_markdown=comment_path.read_text(encoding="utf-8"),
                archive_metadata=archive_metadata,
                comment_analysis_markdown=_analysis_markdown(analysis),
            ),
            {"media_type": "text/markdown", "scope": "video_notes", "source_artifact_ids": source_ids, "comments_artifact_id": comment_note.id},
        )
    except (OSError, RuntimeError, ValueError, UnicodeError, sqlite3.Error) as exc:
        summary_error = f"汇总资料未生成：{exc}；评论和分析原件仍可查看。"
    return StepResult(
        status=StepStatus.COMPLETED.value,
        artifact_ids=[
            *[str(item["raw_artifact_id"]) for item in raw_batches if isinstance(item.get("raw_artifact_id"), str)],
            json_artifact.id,
            markdown_artifact.id,
            *([summary_artifact.id] if summary_artifact else []),
        ],
        metadata={"result": {
            "comment_count": len(comments),
            "selected_count": len(analysis.selected),
            "processed_comment_count": len(analysis.processed_comment_ids),
            "scope": "本次采集到的公开评论；观点与待核实事实分开标注",
            "summary_warning": summary_error,
        }},
    )


def _load_collected_comments(context: StepContext) -> list[CommentRecord]:
    with context.repository.database.transaction() as connection:
        rows = connection.execute(
            """SELECT c.* FROM comments AS c
               JOIN comment_collection_items AS ci ON ci.comment_id = c.id
               WHERE ci.task_id = ? AND c.asset_id = ?""",
            (context.task.id, context.task.asset_id),
        ).fetchall()
    values: list[CommentRecord] = []
    for row in rows:
        try:
            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
            collected_at = datetime.fromisoformat(row["collected_at"])
            raw = json.loads(row["raw_fields_json"]) if row["raw_fields_json"] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values.append(CommentRecord(
            id=str(row["id"]),
            platform_comment_id=str(row["platform_comment_id"]),
            parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
            author=str(row["author"]) if row["author"] is not None else None,
            text=str(row["text"]),
            like_count=int(row["like_count"]) if row["like_count"] is not None else None,
            created_at=created_at,
            collected_at=collected_at,
            raw_fields=raw if isinstance(raw, dict) else {},
        ))
    return values


def _comment_note_artifact(context: StepContext):
    with context.repository.database.transaction() as connection:
        rows = connection.execute(
            """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'note'
               ORDER BY created_at DESC, version DESC, id DESC""",
            (context.task.id,),
        ).fetchall()
    for row in rows:
        artifact = _artifact_from_row(row)
        if artifact.metadata.get("scope") == "comments":
            try:
                path = _assert_contained(Path(artifact.path), context.archive_root, strict=True)
                if path.is_file() and not path.is_symlink() and _sha256(path) == artifact.sha256:
                    return artifact
            except (OSError, RuntimeError, ValueError):
                continue
    raise RunnerError("comments_artifact_missing", "本次任务没有可校验的评论资料产物。", status_code=409)


def _model_input_limit(context: StepContext) -> int:
    if context.app_config is not None:
        settings = task_model_settings(context.app_config, context.task.id)
        model_id = settings.get("model_id")
        provider_id = settings.get("model_provider")
        capability = (
            get_model_capability(provider_id, model_id)
            if isinstance(provider_id, str) and isinstance(model_id, str)
            else None
        )
        if capability is not None and capability.max_input_tokens is not None:
            return capability.max_input_tokens
    return 1_000_000


def _comments_json(comments: list[CommentRecord]) -> str:
    return json.dumps(
        [
            {
                "comment_id": comment.platform_comment_id,
                "text": comment.text,
                "like_count": comment.like_count,
                "author": comment.author,
                "created_at": comment.created_at.isoformat() if comment.created_at else None,
                "parent_id": comment.parent_id,
            }
            for comment in comments
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _analysis_prompt() -> str:
    return (
        "你正在整理本次采集到的公开短视频评论。所有输入评论都必须逐条纳入分析；"
        "不得补造、改写或合并评论，不得推断点赞数；只能引用输入中的 comment_id。"
        "选择有代表性的评论并标为‘有用’、‘高赞’或‘有趣’，每条给出简短理由。"
        "区分评论者的个人经历、观点与可核实事实，不得把评论经历说成创作者经历。"
        "请仅返回 JSON 对象，字段为 selected[{comment_id,category,reason}], themes[], "
        "audience_questions[], disagreements[], claims_to_verify[]。"
        "主题、问题、分歧及事实主张应依据输入；没有内容时返回空数组。"
    )


def _parse_object(text: str) -> dict[str, object]:
    candidates = [text.strip()]
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RunnerError("invalid_analysis_output", "评论分析返回格式无效；原始响应已保留在本机调用记录中。")


def _object_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    return [item.strip()[:1000] for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split()).casefold()
        if normalized and normalized not in seen:
            result.append(value.strip())
            seen.add(normalized)
    return result


def _analysis_markdown(analysis: CommentAnalysis) -> str:
    comment_ids = {
        value.strip()
        for value in [*analysis.processed_comment_ids, *(item.comment_id for item in analysis.selected)]
        if value.strip()
    }
    alternatives = "|".join(re.escape(value) for value in sorted(comment_ids, key=len, reverse=True))
    comment_id_pattern = (
        re.compile(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])")
        if alternatives
        else None
    )
    lines = [
        "# 评论精选与摘要",
        "",
        f"- 已分析评论：{len(analysis.processed_comment_ids)} 条",
        f"- 精选评论：{len(analysis.selected)} 条；仅依据已采集评论，不代表全部平台评论",
        "",
        "## 精选评论",
        "",
    ]
    if analysis.selected:
        for index, item in enumerate(analysis.selected, start=1):
            category = _without_comment_ids(item.category, comment_id_pattern)
            text = _without_comment_ids(item.text, comment_id_pattern)
            reason = _without_comment_ids(item.reason, comment_id_pattern)
            like_count = item.like_count if item.like_count is not None else "未提供"
            lines.extend([
                f"{index}. **{_markdown(category)}** · 点赞 {like_count}",
                "",
                f"   - 评论：{_markdown(text)}",
                f"   - 精选理由：{_markdown(reason)}",
                "",
            ])
    else:
        lines.append("（没有可精选的评论。）\n")
    for heading, values in (
        ("主题", analysis.themes),
        ("受众问题", analysis.audience_questions),
        ("分歧", analysis.disagreements),
        ("待核实事实主张", analysis.claims_to_verify),
    ):
        lines.extend([f"## {heading}", ""])
        sanitized_values = [
            cleaned
            for value in values
            if (cleaned := _without_comment_ids(value, comment_id_pattern))
        ]
        lines.extend(f"- {_markdown(value)}" for value in sanitized_values) if sanitized_values else lines.append("（无）")
        lines.append("")
    lines.extend([
        "## 来源范围",
        "",
        "观点、个人经历和待核实事实分开呈现。评论者经历不属于创作者经历。",
        "",
    ])
    return "\n".join(lines)


def _without_comment_ids(value: str, pattern: re.Pattern[str] | None) -> str:
    result = pattern.sub("", value) if pattern is not None else value
    return re.sub(r"[ \t]{2,}", " ", result).strip()


def _markdown(value: str) -> str:
    return value.replace("\x00", "").replace("\r", " ").replace("\n", " ").replace("`", "&#96;")[:20_000]


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "CommentAnalysis",
    "SelectedComment",
    "analyze_collected_comments",
    "analyze_comments",
    "partition_comments",
    "rank_comment_candidates",
]
