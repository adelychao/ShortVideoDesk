"""Source-grounded structure analysis for registered transcript materials."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from ..runner import RunnerError, StepContext, StepResult, TaskCancelled
from .artifact_store import register_bytes
from .model_call import authorized_model_call, configured_model_id, configured_provider_id
from .text_analysis_common import (
    excerpt_in_source,
    json_bytes,
    markdown_safe,
    parse_json_object,
    partition_text,
    read_artifact_text,
    string_field,
    text_inputs,
    task_artifact,
)


_STRUCTURE_KINDS = ("开头", "观点", "论证顺序", "案例", "转折", "结尾")


@dataclass(frozen=True)
class StructureSection:
    kind: str
    present: bool
    source_excerpt: str | None
    analysis: str
    source_artifact_id: str
    verified: bool


@dataclass(frozen=True)
class StructureAnalysis:
    sections: list[StructureSection]


def analyze_structure(context: StepContext, text_artifact_ids: list[str]) -> StructureAnalysis:
    model_id = configured_model_id(context)
    if not text_artifact_ids:
        text_artifact_ids = [item.id for item in text_inputs(context, include_comments=False)]
    sources = {item: read_artifact_text(context, item) for item in text_artifact_ids}

    sections: list[StructureSection] = []
    raw_responses: list[dict[str, object]] = []
    input_ids = list(dict.fromkeys(text_artifact_ids))
    prompt = (
        "你是短视频结构分析助手。只依据给定原文，分别分析开头、观点、论证顺序、案例、转折、结尾。"
        "不存在的部分 present=false 且 source_excerpt=null；存在时必须给出输入原文中连续、可核对的短片段。"
        "不得补写原文未出现的内容。仅返回 JSON：{\"sections\":[{\"kind\":\"开头|观点|论证顺序|案例|转折|结尾\","
        "\"present\":true,\"source_excerpt\":\"原文片段\",\"analysis\":\"分析\"}]}。"
    )
    chunks: list[tuple[str, int, int, str]] = []
    for artifact_id in input_ids:
        chunks.extend((artifact_id, start, end, text) for start, end, text in partition_text(sources[artifact_id]))

    for index, (artifact_id, start, end, text) in enumerate(chunks, start=1):
        if context.cancel_event.is_set():
            raise TaskCancelled
        response = authorized_model_call(
            context,
            model_id=model_id,
            input_artifact_ids=[artifact_id],
            consent_artifact_ids=input_ids,
            input_detail={"source_artifact_id": artifact_id, "char_start": start, "char_end": end, "chunk_index": index, "chunk_count": len(chunks)},
            operation=lambda provider, prompt=prompt, text=text: provider.analyze_text(  # type: ignore[attr-defined]
                prompt, text, model_id
            ),
        )
        raw = str(response.text)  # type: ignore[attr-defined]
        raw_artifact = register_bytes(
            context,
            "structure_analysis_raw",
            ".json",
            json_bytes({"source_artifact_id": artifact_id, "char_start": start, "char_end": end, "request_id": getattr(response, "request_id", None), "raw_response": raw}),
            {"media_type": "application/json", "model_id": model_id, "source_artifact_ids": [artifact_id]},
        )
        raw_responses.append({"artifact_id": raw_artifact.id, "source_artifact_id": artifact_id})
        parsed = parse_json_object(raw)
        rows = parsed.get("sections")
        if not isinstance(rows, list):
            raise RunnerError("invalid_analysis_output", "结构分析缺少 sections 列表；原始响应已保存。")
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = string_field(row.get("kind"), maximum=80)
            if kind not in _STRUCTURE_KINDS:
                continue
            present = row.get("present") is True
            excerpt = string_field(row.get("source_excerpt"), maximum=2000)
            analysis = string_field(row.get("analysis"), maximum=3000) or "模型未提供分析说明。"
            verified = not present or bool(excerpt and excerpt_in_source(excerpt, text))
            if not present:
                excerpt = None
            sections.append(StructureSection(kind, present, excerpt, analysis, artifact_id, verified))
        context.progress(context.step.id, round(index / max(1, len(chunks)) * 95), f"已分析第 {index}/{len(chunks)} 段文本")

    if not sections:
        raise RunnerError("invalid_analysis_output", "结构分析没有可识别的结果；原始响应已保存。")
    return StructureAnalysis(sections)


def analyze_structure_step(context: StepContext) -> StepResult:
    model_id = configured_model_id(context)
    provider_id = configured_provider_id(context)
    selected = getattr(context.step, "input_artifact_ids", [])
    artifacts = [task_artifact(context, item) for item in selected] if selected else text_inputs(context, include_comments=False)
    if not artifacts or artifacts[0].kind not in {"transcript", "transcript_draft", "transcript_revision"} or any(item.kind not in {"transcript", "transcript_draft", "transcript_revision", "ocr_result", "ocr_revision"} for item in artifacts):
        raise RunnerError("text_artifact_invalid", "结构分析需要本任务的转写文本及可选 OCR 文本。", status_code=422)
    analysis = analyze_structure(context, [item.id for item in artifacts])
    raw_manifest = register_bytes(
        context,
        "structure_analysis_json",
        ".json",
        json_bytes({"schema_version": 1, **asdict(analysis), "source_artifact_ids": [item.id for item in artifacts], "raw_response_artifacts": [item.id for item in _step_artifacts(context, "structure_analysis_raw")]}),
        {"media_type": "application/json", "model_id": model_id, "source_artifact_ids": [item.id for item in artifacts]},
    )
    lines = ["# 内容结构分析", "", f"- 模型：{provider_id} / {model_id}", "- 依据：所选转写及可选的 OCR 文本；不包含评论", ""]
    for kind in _STRUCTURE_KINDS:
        matches = [item for item in analysis.sections if item.kind == kind]
        lines.extend([f"## {kind}", ""])
        if not matches or not any(item.present for item in matches):
            lines.append("- 状态：未在输入文本中识别到")
        for item in matches:
            if not item.present:
                continue
            lines.extend([
                f"- 来源产物 ID：`{item.source_artifact_id}`",
                f"- {'已匹配原文的片段' if item.verified else '待核对的模型引用（未确认是原文）'}：> {markdown_safe(item.source_excerpt or '未提供')}",
                f"- 分析：{markdown_safe(item.analysis)}",
            ])
        lines.append("")
    markdown = register_bytes(
        context,
        "analysis",
        ".md",
        "\n".join(lines).encode("utf-8"),
        {"media_type": "text/markdown", "model_id": model_id, "raw_artifact_id": raw_manifest.id, "source_artifact_ids": [item.id for item in artifacts]},
    )
    pending = sum(1 for item in analysis.sections if item.present and not item.verified)
    return StepResult("partial" if pending else "completed", [raw_manifest.id, markdown.id], {"result": {"section_count": len(analysis.sections), "pending_count": pending, "source_artifact_ids": [item.id for item in artifacts]}})


def _step_artifacts(context: StepContext, kind: str):
    with context.repository.database.transaction() as connection:
        rows = connection.execute("SELECT * FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = ? ORDER BY created_at, id", (context.task.id, context.step.id, kind)).fetchall()
    from ..repository import _artifact_from_row

    return [_artifact_from_row(row) for row in rows]


__all__ = ["StructureAnalysis", "StructureSection", "analyze_structure", "analyze_structure_step"]
