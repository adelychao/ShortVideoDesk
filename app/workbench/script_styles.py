"""User-configurable writing styles used to create short-video scripts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .obsidian import list_reference_notes


MAX_SCRIPT_STYLE_TEMPLATES = 50
MAX_SELECTED_SCRIPT_STYLES = 20
MAX_SCRIPT_STYLE_INSTRUCTIONS = 8_000
_STYLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STYLE_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,64}$")


def default_script_style_templates() -> list[dict[str, object]]:
    return [
        {
            "id": "science-explainer",
            "name": "科普讲解",
            "instructions": "面向普通观众讲清一个概念。先提出观众容易产生的问题，再用准确、易懂的语言解释，最后收束为清晰结论。避免堆砌术语；需要术语时先解释。",
            "enabled": True,
            "reference_paths": [],
        },
        {
            "id": "story-oral",
            "name": "故事口语",
            "instructions": "像和朋友讲一件真实发生的事，保留原文中的事件顺序和因果关系。使用自然口语、清楚的转折和适合朗读的短句，不虚构人物、经历或情节。",
            "enabled": True,
            "reference_paths": [],
        },
        {
            "id": "light-humor",
            "name": "轻松幽默",
            "instructions": "整体轻松、节奏明快，可以用温和的反差或不冒犯人的自嘲增强可听性。幽默只调整表达方式，不能夸大或改变原文事实。",
            "enabled": True,
            "reference_paths": [],
        },
    ]


def normalize_script_style_templates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_SCRIPT_STYLE_TEMPLATES:
        raise ValueError(f"口播稿风格模板最多保存 {MAX_SCRIPT_STYLE_TEMPLATES} 个。")
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("口播稿风格模板格式无效。")
        style_id = row.get("id")
        name = row.get("name")
        instructions = row.get("instructions")
        enabled = row.get("enabled")
        reference_paths = row.get("reference_paths", [])
        if not isinstance(style_id, str) or not _STYLE_ID.fullmatch(style_id):
            raise ValueError("风格模板 ID 格式无效。")
        if style_id in seen_ids:
            raise ValueError("风格模板 ID 不能重复。")
        if not isinstance(name, str) or not _STYLE_NAME.fullmatch(name.strip()):
            raise ValueError("风格模板名称不能为空、不能超过 64 个字符，也不能包含文件路径或特殊字符。")
        name = name.strip()
        if name.casefold() in seen_names:
            raise ValueError("风格模板名称不能重复。")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_SCRIPT_STYLE_INSTRUCTIONS:
            raise ValueError(f"风格模板写作要求不能为空，且不能超过 {MAX_SCRIPT_STYLE_INSTRUCTIONS} 个字符。")
        if not isinstance(enabled, bool):
            raise ValueError("风格模板启用状态无效。")
        if not isinstance(reference_paths, list) or len(reference_paths) > 50:
            raise ValueError("每个风格模板最多关联 50 篇参考 Markdown。")
        normalized_paths: list[str] = []
        seen_paths: set[str] = set()
        for raw_path in reference_paths:
            if not isinstance(raw_path, str):
                raise ValueError("参考 Markdown 路径必须是文本。")
            path = raw_path.strip()
            if not path:
                continue
            if len(path) > 1024:
                raise ValueError("参考 Markdown 路径不能超过 1,024 个字符。")
            if "\\" in path or path.startswith(("/", "//")) or ":" in path or "\x00" in path:
                raise ValueError("参考 Markdown 只能填写知识库参考目录下的相对路径。")
            parts = path.split("/")
            if any(part in {"", ".", ".."} for part in parts) or not path.casefold().endswith(".md"):
                raise ValueError("参考资料路径必须是有效的相对 .md 文件路径。")
            key = path.casefold()
            if key not in seen_paths:
                normalized_paths.append(path)
                seen_paths.add(key)
        result.append({
            "id": style_id,
            "name": name,
            "instructions": instructions.strip(),
            "enabled": enabled,
            "reference_paths": normalized_paths,
        })
        seen_ids.add(style_id)
        seen_names.add(name.casefold())
    return result


def build_script_style_snapshot(
    settings: dict[str, object],
    selected_ids: list[str],
    *,
    use_all_enabled_when_empty: bool = False,
) -> dict[str, object]:
    templates = normalize_script_style_templates(settings.get("script_style_templates"))
    if not selected_ids and use_all_enabled_when_empty:
        selected_ids = [str(item["id"]) for item in templates if item["enabled"] is True]
    if not selected_ids:
        raise ValueError("请至少选择一个已启用的口播稿风格模板。")
    if len(selected_ids) > MAX_SELECTED_SCRIPT_STYLES or len(set(selected_ids)) != len(selected_ids):
        raise ValueError(f"风格模板不能重复选择，一次最多选择 {MAX_SELECTED_SCRIPT_STYLES} 个。")
    by_id = {str(item["id"]): item for item in templates if item["enabled"] is True}
    if any(style_id not in by_id for style_id in selected_ids):
        raise ValueError("所选风格模板不存在或已停用，请刷新后重新选择。")
    selected = [by_id[style_id] for style_id in selected_ids]
    all_paths = list(dict.fromkeys(
        path
        for template in selected
        for path in template["reference_paths"]  # type: ignore[union-attr]
    ))
    reference_notes: list[dict[str, str]] = []
    if all_paths:
        root_value = settings.get("obsidian_reference_root")
        if not isinstance(root_value, str) or not root_value:
            raise ValueError("所选风格模板包含参考 Markdown，请先在“目录与知识库”设置知识库风格参考目录。")
        notes = list_reference_notes(Path(root_value), all_paths)
        reference_notes = [
            {
                "relative_path": note.relative_path,
                "title": note.title,
                "content": note.content,
                "sha256": note.sha256,
            }
            for note in notes
        ]
    path_lookup = {note["relative_path"].casefold(): note["relative_path"] for note in reference_notes}
    style_snapshots = [
        {
            "id": template["id"],
            "name": template["name"],
            "instructions": template["instructions"],
            "reference_paths": [path_lookup[path.casefold()] for path in template["reference_paths"]],
        }
        for template in selected
    ]
    return {
        "schema_version": 1,
        "styles": style_snapshots,
        "reference_notes": reference_notes,
    }


def save_task_script_style_snapshot(
    database: Any,
    task_id: str,
    snapshot: dict[str, object],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO task_script_style_selections (task_id, selection_json, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   selection_json = excluded.selection_json,
                   created_at = excluded.created_at""",
            (task_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now),
        )


def load_task_script_style_snapshot(database: Any, task_id: str) -> dict[str, object] | None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT selection_json FROM task_script_style_selections WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["selection_json"]))
    except (TypeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("styles"), list):
        return None
    return payload


__all__ = [
    "MAX_SELECTED_SCRIPT_STYLES",
    "MAX_SCRIPT_STYLE_INSTRUCTIONS",
    "MAX_SCRIPT_STYLE_TEMPLATES",
    "build_script_style_snapshot",
    "default_script_style_templates",
    "load_task_script_style_snapshot",
    "normalize_script_style_templates",
    "save_task_script_style_snapshot",
]
