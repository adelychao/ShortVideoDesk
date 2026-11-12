"""Local article documents, revision history, and platform formatting helpers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .database import Database
from .image_storage import image_directory, store_image


ARTICLE_PLATFORMS: tuple[dict[str, str], ...] = (
    {"id": "wechat", "name": "微信公众号", "mode": "official"},
    {"id": "csdn", "name": "CSDN", "mode": "manual"},
    {"id": "zhihu", "name": "知乎", "mode": "official"},
    {"id": "xiaohongshu", "name": "小红书", "mode": "manual"},
    {"id": "toutiao", "name": "今日头条", "mode": "manual"},
    {"id": "baijiahao", "name": "百家号", "mode": "manual"},
    {"id": "weibo", "name": "微博", "mode": "official"},
)
_PLATFORM_BY_ID = {item["id"]: item for item in ARTICLE_PLATFORMS}
_BLOCK_TYPES = {"heading", "paragraph", "quote", "code", "bullet_list", "ordered_list", "divider", "table"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_document(title: str = "") -> dict[str, Any]:
    clean_title = _clean_text(title, 200)
    return {
        "schema_version": 1,
        "title": clean_title,
        "title_candidates": [clean_title, f"关于{clean_title}，你需要知道的几件事", f"我对{clean_title}的实践与思考"] if clean_title else [],
        "summary": "",
        "mode": "layout",
        "analysis": {"core_point": "", "reason": "手动新建文章"},
        "cover_image_prompt": "",
        "cover_image_path": "",
        "cover_image_name": "",
        "author": "老赵",
        "published_at": datetime.now().date().isoformat(),
        "outro": "",
        "blocks": [],
        "body_markdown": "",
        "platform_overrides": {},
    }


def normalize_document(value: object, *, allow_empty_body: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("文章数据格式无效。")
    title = _clean_text(value.get("title"), 200)
    summary = _clean_text(value.get("summary"), 2000)
    if not summary:
        summary = ""
    candidates_value = value.get("title_candidates")
    candidates = [
        _clean_text(item, 200)
        for item in candidates_value
    ] if isinstance(candidates_value, list) else []
    candidates = [item for item in candidates if item][:3]
    if title:
        while len(candidates) < 3:
            candidates.append(title if not candidates else f"{title} · {len(candidates) + 1}")
    mode = value.get("mode")
    if mode not in {"refine", "polish", "layout"}:
        mode = "polish"
    analysis_value = value.get("analysis")
    analysis = analysis_value if isinstance(analysis_value, dict) else {}
    blocks_value = value.get("blocks")
    if not isinstance(blocks_value, list) or len(blocks_value) > 1000:
        raise ValueError("文章正文结构无效。")
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in blocks_value:
        if not isinstance(raw, dict) or raw.get("type") not in _BLOCK_TYPES:
            continue
        block_type = str(raw["type"])
        block_id = _clean_text(raw.get("id"), 80) or "block-" + uuid.uuid4().hex[:10]
        if block_id in seen:
            block_id += "-" + uuid.uuid4().hex[:5]
        seen.add(block_id)
        block: dict[str, Any] = {"id": block_id, "type": block_type}
        if block_type == "heading":
            text = _clean_text(raw.get("text"), 500)
            if not text:
                continue
            block["level"] = min(4, max(1, int(raw.get("level", 2) or 2)))
            block["text"] = text
        elif block_type in {"paragraph", "quote", "code"}:
            text = _clean_text(raw.get("text"), 100_000)
            if not text:
                continue
            block["text"] = text
            if block_type == "code":
                block["language"] = _clean_text(raw.get("language"), 40) or "text"
            prompt = _clean_text(raw.get("image_prompt"), 4000)
            if block_type == "paragraph" and (prompt or raw.get("image_role") == "module"):
                block["image_role"] = "module"
                block["image_prompt"] = prompt
            if block_type == "paragraph":
                image_path = _clean_text(raw.get("image_path"), 1024)
                image_name = _clean_text(raw.get("image_name"), 255)
                image_model_id = _clean_text(raw.get("image_model_id"), 128)
                image_generated_at = _clean_text(raw.get("image_generated_at"), 64)
                if image_path:
                    block["image_path"] = image_path
                    block["image_name"] = image_name
                    block["image_model_id"] = image_model_id
                    block["image_generated_at"] = image_generated_at
        elif block_type in {"bullet_list", "ordered_list"}:
            items = raw.get("items")
            clean_items = [_clean_text(item, 2000) for item in items] if isinstance(items, list) else []
            block["items"] = [item for item in clean_items if item][:200]
            if not block["items"]:
                continue
        elif block_type == "divider":
            pass
        elif block_type == "table":
            headers = raw.get("headers")
            rows = raw.get("rows")
            clean_headers = [_clean_text(item, 200) for item in headers] if isinstance(headers, list) else []
            clean_rows = [
                [_clean_text(cell, 2000) for cell in row]
                for row in rows if isinstance(row, list)
            ] if isinstance(rows, list) else []
            if not clean_headers or not clean_rows or any(len(row) != len(clean_headers) or any(not cell for cell in row) for row in clean_rows):
                continue
            block["headers"] = clean_headers
            block["rows"] = clean_rows[:200]
        blocks.append(block)
    body_markdown = _clean_text(value.get("body_markdown"), 5_000_000)
    if "body_markdown" not in value:
        body_markdown = blocks_to_markdown(blocks)
    if not allow_empty_body and not body_markdown.strip():
        raise ValueError("文章正文不能为空。")
    overrides_value = value.get("platform_overrides")
    overrides: dict[str, dict[str, str]] = {}
    if isinstance(overrides_value, dict):
        for platform_id, raw in overrides_value.items():
            if platform_id not in _PLATFORM_BY_ID or not isinstance(raw, dict):
                continue
            overrides[platform_id] = {
                "title": _clean_text(raw.get("title"), 200),
                "summary": _clean_text(raw.get("summary"), 2000),
                "body_markdown": _clean_text(raw.get("body_markdown"), 5_000_000),
            }
    return {
        "schema_version": 1,
        "title": title,
        "title_candidates": candidates,
        "summary": summary,
        "mode": mode,
        "analysis": {
            "core_point": _clean_text(analysis.get("core_point"), 2000),
            "reason": _clean_text(analysis.get("reason"), 4000),
        },
        "cover_image_prompt": _clean_text(value.get("cover_image_prompt"), 4000),
        "cover_image_path": _clean_text(value.get("cover_image_path"), 1024),
        "cover_image_name": _clean_text(value.get("cover_image_name"), 255),
        "cover_image_model_id": _clean_text(value.get("cover_image_model_id"), 128),
        "cover_image_generated_at": _clean_text(value.get("cover_image_generated_at"), 64),
        "author": _clean_text(value.get("author"), 200) or "老赵",
        "published_at": _clean_text(value.get("published_at"), 64) or datetime.now().date().isoformat(),
        "outro": _clean_text(value.get("outro"), 4000),
        "blocks": blocks,
        "body_markdown": body_markdown,
        "platform_overrides": overrides,
    }


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        text = str(block.get("text", ""))
        if kind == "heading":
            level = min(4, max(1, int(block.get("level", 2) or 2)))
            parts.append(f"{'#' * level} {text}")
        elif kind == "paragraph":
            parts.append(text)
        elif kind == "quote":
            parts.append("\n".join("> " + line for line in text.splitlines()))
        elif kind == "code":
            language = str(block.get("language") or "text")
            parts.append(f"```{language}\n{text}\n```")
        elif kind == "bullet_list":
            parts.append("\n".join(f"- {item}" for item in block.get("items", [])))
        elif kind == "ordered_list":
            parts.append("\n".join(f"{index}. {item}" for index, item in enumerate(block.get("items", []), start=1)))
        elif kind == "divider":
            parts.append("---")
        elif kind == "table":
            headers = block.get("headers", [])
            rows = block.get("rows", [])
            if headers:
                parts.append("| " + " | ".join(map(str, headers)) + " |")
                parts.append("| " + " | ".join("---" for _ in headers) + " |")
                parts.extend("| " + " | ".join(map(str, row)) + " |" for row in rows)
    return "\n\n".join(part for part in parts if part).strip()


def create_article(
    database: Database,
    document: dict[str, Any],
    *,
    allow_empty_body: bool = False,
    source_task_id: str | None = None,
    source_artifact_id: str | None = None,
    note: str = "初始版本",
) -> dict[str, Any]:
    clean = normalize_document(document, allow_empty_body=allow_empty_body)
    article_id = uuid.uuid4().hex
    now = utc_now()
    encoded = _encode(clean)
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO articles (
                   id, title, summary, document_json, status, version,
                   source_task_id, source_artifact_id, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'draft', 1, ?, ?, ?, ?)""",
            (article_id, clean["title"], clean["summary"], encoded, source_task_id, source_artifact_id, now, now),
        )
        connection.execute(
            """INSERT INTO article_versions (id, article_id, version, title, document_json, note, created_at)
               VALUES (?, ?, 1, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, article_id, clean["title"], encoded, note, now),
        )
    return get_article(database, article_id) or {}


def list_articles(
    database: Database,
    *,
    query: str = "",
    status: str = "all",
    platform_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    clean_query = _clean_text(query, 300)
    sql = """SELECT a.*, (SELECT json_group_object(platform_summary.platform_id, platform_summary.status)
                    FROM (
                        SELECT p.platform_id,
                               CASE
                                   WHEN SUM(CASE WHEN p.status IN ('published', 'manual_published') THEN 1 ELSE 0 END) > 0
                                    AND SUM(CASE WHEN p.status NOT IN ('published', 'manual_published') THEN 1 ELSE 0 END) > 0 THEN 'partial'
                                   WHEN SUM(CASE WHEN p.status IN ('published', 'manual_published') THEN 1 ELSE 0 END) > 0 THEN 'published'
                                   WHEN SUM(CASE WHEN p.status = 'publishing' THEN 1 ELSE 0 END) > 0 THEN 'publishing'
                                   WHEN SUM(CASE WHEN p.status = 'unknown' THEN 1 ELSE 0 END) > 0 THEN 'unknown'
                                   WHEN SUM(CASE WHEN p.status = 'failed' THEN 1 ELSE 0 END) > 0 THEN 'failed'
                                   WHEN SUM(CASE WHEN p.status = 'manual_required' THEN 1 ELSE 0 END) > 0 THEN 'manual_required'
                                   ELSE 'draft_saved'
                               END AS status
                        FROM article_publications AS p
                        WHERE p.article_id = a.id AND p.article_version = a.version
                          AND p.id = (
                              SELECT p2.id FROM article_publications AS p2
                              WHERE p2.article_id = p.article_id
                                AND p2.platform_id = p.platform_id
                                AND p2.article_version = p.article_version
                                AND p2.account_id IS p.account_id
                              ORDER BY p2.created_at DESC, p2.id DESC LIMIT 1
                          )
                        GROUP BY p.platform_id
                    ) AS platform_summary) AS publication_json,
                    (SELECT ar.metadata_json FROM artifacts AS ar WHERE ar.id = a.source_artifact_id) AS source_metadata
                FROM articles AS a"""
    clauses: list[str] = []
    params: list[object] = []
    if clean_query:
        clauses.append("(a.title LIKE ? OR a.summary LIKE ?)")
        params.extend([f"%{clean_query}%", f"%{clean_query}%"])
    if status == "draft":
        clauses.append("a.status = 'draft'")
    elif status == "finalized":
        clauses.append("a.status = 'finalized'")
    elif status == "published":
        clauses.append("EXISTS (SELECT 1 FROM article_publications p WHERE p.article_id = a.id AND p.article_version = a.version AND p.status IN ('published', 'manual_published'))")
    elif status != "all":
        raise ValueError("文章状态筛选无效。")
    if platform_id:
        if platform_id not in _PLATFORM_BY_ID:
            raise ValueError("发布平台筛选无效。")
        clauses.append("EXISTS (SELECT 1 FROM article_publications p WHERE p.article_id = a.id AND p.platform_id = ?)")
        params.append(platform_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with database.transaction() as connection:
        total_sql = "SELECT COUNT(*) FROM articles AS a"
        count_params: list[object] = []
        if clauses:
            total_sql += " WHERE " + " AND ".join(clauses)
            count_params = params[:-2]
        total = int(connection.execute(total_sql, count_params).fetchone()[0])
        rows = connection.execute(sql, params).fetchall()
    items = [_article_list_item(row) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def delete_article(database: Database, article_id: str) -> bool:
    with database.transaction(write=True) as connection:
        cursor = connection.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        return cursor.rowcount > 0


def get_article(database: Database, article_id: str) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT a.*, t.asset_id, ar.metadata_json AS source_metadata
               FROM articles AS a
               LEFT JOIN tasks AS t ON t.id = a.source_task_id
               LEFT JOIN artifacts AS ar ON ar.id = a.source_artifact_id
               WHERE a.id = ?""",
            (article_id,),
        ).fetchone()
        if row is None:
            return None
        versions = connection.execute(
            "SELECT version, note, created_at FROM article_versions WHERE article_id = ? ORDER BY version DESC",
            (article_id,),
        ).fetchall()
        publications = connection.execute(
            "SELECT * FROM article_publications WHERE article_id = ? ORDER BY created_at DESC",
            (article_id,),
        ).fetchall()
        source_title = None
        if row["asset_id"]:
            asset = connection.execute("SELECT title FROM assets WHERE id = ?", (row["asset_id"],)).fetchone()
            source_title = asset["title"] if asset is not None else None
    try:
        document = json.loads(str(row["document_json"]))
    except (TypeError, json.JSONDecodeError):
        document = default_document(str(row["title"]))
    source_metadata = _decode_object(row["source_metadata"])
    source_title = source_title or source_metadata.get("title")
    result = _article_list_item(row)
    result.update({
        "document": document,
        "versions": [{"version": int(item["version"]), "note": str(item["note"]), "created_at": str(item["created_at"])} for item in versions],
        "publications": [_publication_payload(item) for item in publications],
        "source_title": source_title,
        "source_task_id": row["source_task_id"],
        "source_artifact_id": row["source_artifact_id"],
    })
    return result


def save_article(database: Database, article_id: str, expected_version: int, document: dict[str, Any], *, note: str = "人工编辑") -> dict[str, Any]:
    with database.transaction(write=True) as connection:
        _write_article_version(connection, article_id, expected_version, document, note)
    return get_article(database, article_id) or {}


def _write_article_version(
    connection: sqlite3.Connection, article_id: str, expected_version: int, document: dict[str, Any], note: str,
) -> None:
    clean = normalize_document(document, allow_empty_body=True)
    now = utc_now()
    encoded = _encode(clean)
    row = connection.execute("SELECT version FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise KeyError("文章不存在。")
    if int(row["version"]) != expected_version:
        raise RuntimeError("文章已在其他位置更新，请刷新版本后重试。")
    next_version = expected_version + 1
    connection.execute(
        """UPDATE articles SET title = ?, summary = ?, document_json = ?, status = 'draft', version = ?, updated_at = ?
           WHERE id = ? AND version = ?""",
        (clean["title"], clean["summary"], encoded, next_version, now, article_id, expected_version),
    )
    connection.execute(
        """INSERT INTO article_versions (id, article_id, version, title, document_json, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uuid.uuid4().hex, article_id, next_version, clean["title"], encoded, _clean_text(note, 200) or "人工编辑", now),
    )


def save_article_image(
    database: Database, article: dict[str, Any], content: bytes, suffix: str, *,
    name: str, model_id: str = "", generated_at: str = "", block_index: int | None = None, note: str = "人工编辑",
) -> tuple[dict[str, Any], str]:
    article_id = str(article["id"])
    if len(content) > 10_000_000:
        raise ValueError("文章图片不能超过 10 MB。")
    directory = image_directory(database, "article_image_root")
    document = dict(article["document"])
    with store_image(database, directory, article_id=article_id, original_name=name, suffix=suffix, content=content) as (connection, image):
        filename = f'{image["id"]}{image["suffix"]}'
        reference = f"data/article-assets/{article_id}/{filename}"
        fields = {
            "image_path": reference, "image_name": name,
            "image_model_id": model_id, "image_generated_at": generated_at,
        }
        if block_index is None:
            document.update({f"cover_{key}": value for key, value in fields.items()})
        else:
            blocks = list(document["blocks"])
            blocks[block_index] = {**blocks[block_index], **fields}
            document["blocks"] = blocks
        _write_article_version(connection, article_id, int(article["version"]), document, note)
    return get_article(database, article_id) or {}, filename


def finalize_article(database: Database, article_id: str) -> dict[str, Any] | None:
    with database.transaction(write=True) as connection:
        row = connection.execute("SELECT document_json FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(str(row["document_json"]))
        except (TypeError, json.JSONDecodeError):
            document = {}
        body_markdown = document.get("body_markdown") if isinstance(document, dict) else ""
        if not isinstance(body_markdown, str) or not body_markdown.strip():
            raise ValueError("文章正文不能为空。")
        connection.execute("UPDATE articles SET status = 'finalized', updated_at = ? WHERE id = ?", (utc_now(), article_id))
    return get_article(database, article_id)


def record_publication(
    database: Database,
    article_id: str,
    platform_id: str,
    status: str,
    *,
    remote_id: str | None = None,
    remote_url: str | None = None,
    message: str = "",
    account_id: str | None = None,
    account_name: str | None = None,
) -> dict[str, Any]:
    if platform_id not in _PLATFORM_BY_ID:
        raise ValueError("发布平台无效。")
    if status not in {"draft_saved", "published", "publishing", "failed", "unknown", "manual_required", "manual_published"}:
        raise ValueError("发布状态无效。")
    clean_message = _clean_text(message, 1000)
    now = utc_now()
    publication_id = uuid.uuid4().hex
    with database.transaction(write=True) as connection:
        if connection.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
            raise KeyError("文章不存在。")
        connection.execute(
            """INSERT INTO article_publications
                   (id, article_id, platform_id, status, remote_id, remote_url, message, article_version, created_at, updated_at, account_id, account_name)
               SELECT ?, id, ?, ?, ?, ?, ?, version, ?, ?, ?, ? FROM articles WHERE id = ?""",
            (publication_id, platform_id, status, remote_id, remote_url, clean_message, now, now, account_id, _clean_text(account_name, 120) or None, article_id),
        )
        connection.execute("UPDATE articles SET updated_at = ? WHERE id = ?", (now, article_id))
        row = connection.execute("SELECT * FROM article_publications WHERE id = ?", (publication_id,)).fetchone()
    return _publication_payload(row)


def reserve_article_publication(
    database: Database,
    article_id: str,
    platform_id: str,
    article_version: int,
    *,
    account_id: str | None = None,
    account_name: str | None = None,
    options: dict[str, Any] | None = None,
    retry_manual_publication_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Atomically reserve one send per article version, platform, and account."""
    if platform_id not in _PLATFORM_BY_ID:
        raise ValueError("发布平台无效。")
    now = utc_now()
    publication_id = uuid.uuid4().hex
    clean_account_name = _clean_text(account_name, 120) or None
    fan_notification_status = (
        "waiting_publication"
        if platform_id == "wechat" and bool((options or {}).get("notify_fans"))
        else "not_requested"
    )
    with database.transaction(write=True) as connection:
        article = connection.execute(
            "SELECT status, version FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if article is None:
            raise KeyError("文章不存在。")
        if str(article["status"]) != "finalized" or int(article["version"]) != article_version:
            raise RuntimeError("文章版本已变化或尚未定稿，请刷新后重试。")
        existing = connection.execute(
            """SELECT * FROM article_publications
               WHERE article_id = ? AND platform_id = ? AND article_version = ?
                 AND account_id IS ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (article_id, platform_id, article_version, account_id),
        ).fetchone()
        if existing is not None and str(existing["status"]) != "failed":
            explicit_toutiao_retry = (
                platform_id == "toutiao"
                and retry_manual_publication_id == str(existing["id"])
                and str(existing["status"]) == "manual_required"
                and not existing["remote_id"]
            )
            if not explicit_toutiao_retry:
                return _publication_payload(existing), False
        connection.execute(
            """INSERT INTO article_publications
                   (id, article_id, platform_id, status, remote_id, remote_url, message,
                    article_version, created_at, updated_at, account_id, account_name, options_json,
                    fan_notification_status)
               VALUES (?, ?, ?, 'publishing', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                publication_id, article_id, platform_id, "发布任务已排队。", article_version,
                now, now, account_id, clean_account_name,
                json.dumps(options or {}, ensure_ascii=False, separators=(",", ":")),
                fan_notification_status,
            ),
        )
        connection.execute("UPDATE articles SET updated_at = ? WHERE id = ?", (now, article_id))
        row = connection.execute(
            "SELECT * FROM article_publications WHERE id = ?", (publication_id,)
        ).fetchone()
    return _publication_payload(row), True


def update_article_publication(
    database: Database,
    publication_id: str,
    status: str,
    *,
    remote_id: str | None = None,
    remote_url: str | None = None,
    message: str = "",
) -> dict[str, Any]:
    allowed = {"draft_saved", "published", "publishing", "failed", "unknown", "manual_required", "manual_published"}
    if status not in allowed:
        raise ValueError("发布状态无效。")
    now = utc_now()
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            """UPDATE article_publications
               SET status = ?, remote_id = COALESCE(?, remote_id), remote_url = COALESCE(?, remote_url),
                   message = ?,
                   fan_notification_status = CASE
                       WHEN ? = 'published' AND fan_notification_status = 'waiting_publication' THEN 'pending'
                       WHEN ? = 'failed' AND fan_notification_status = 'waiting_publication' THEN 'failed'
                       ELSE fan_notification_status END,
                   fan_notification_message = CASE
                       WHEN ? = 'failed' AND fan_notification_status = 'waiting_publication' THEN '文章发表未成功，系统没有通知粉丝。'
                       ELSE fan_notification_message END,
                   updated_at = ?
               WHERE id = ?""",
            (status, remote_id, remote_url, _clean_text(message, 1000), status, status, status, now, publication_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("发布记录不存在。")
        row = connection.execute(
            "SELECT * FROM article_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        connection.execute(
            "UPDATE articles SET updated_at = ? WHERE id = ?", (now, str(row["article_id"]))
        )
    return _publication_payload(row)


def claim_article_fan_notification(database: Database, publication_id: str) -> dict[str, Any] | None:
    """Atomically start one requested fan notification after confirmed publication."""
    now = utc_now()
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            """UPDATE article_publications
               SET fan_notification_status = 'sending', fan_notification_message = ?, updated_at = ?
               WHERE id = ? AND platform_id = 'wechat' AND status = 'published'
                 AND fan_notification_status = 'pending'""",
            ("正在提交公众号粉丝群发任务。", now, publication_id),
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM article_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        connection.execute(
            "UPDATE articles SET updated_at = ? WHERE id = ?", (now, str(row["article_id"]))
        )
    return _publication_payload(row)


def retry_article_fan_notification(database: Database, publication_id: str) -> dict[str, Any] | None:
    """Retry only a definitive rejection that has no WeChat message ID to query."""
    now = utc_now()
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            """UPDATE article_publications
               SET fan_notification_status = 'sending', fan_notification_message = ?, updated_at = ?
               WHERE id = ? AND platform_id = 'wechat' AND status = 'published'
                 AND fan_notification_status = 'failed' AND fan_notification_remote_id IS NULL""",
            ("用户确认后重新提交公众号粉丝群发任务。", now, publication_id),
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM article_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        connection.execute(
            "UPDATE articles SET updated_at = ? WHERE id = ?", (now, str(row["article_id"]))
        )
    return _publication_payload(row)


def update_article_fan_notification(
    database: Database,
    publication_id: str,
    status: str,
    *,
    remote_id: str | None = None,
    message: str = "",
) -> dict[str, Any]:
    allowed = {"not_requested", "waiting_publication", "pending", "sending", "sent", "failed", "unknown"}
    if status not in allowed:
        raise ValueError("粉丝通知状态无效。")
    now = utc_now()
    with database.transaction(write=True) as connection:
        cursor = connection.execute(
            """UPDATE article_publications
               SET fan_notification_status = ?,
                   fan_notification_remote_id = COALESCE(?, fan_notification_remote_id),
                   fan_notification_message = ?, updated_at = ?
               WHERE id = ?""",
            (status, remote_id, _clean_text(message, 1000), now, publication_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("发布记录不存在。")
        row = connection.execute(
            "SELECT * FROM article_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        connection.execute(
            "UPDATE articles SET updated_at = ? WHERE id = ?", (now, str(row["article_id"]))
        )
    return _publication_payload(row)


def recover_article_publications(database: Database) -> list[dict[str, Any]]:
    """Mark interrupted external sends as ambiguous, except WeChat jobs with a pollable ID."""
    now = utc_now()
    message = "应用重启时发布任务中断，结果待核实；请先检查平台后台，不要自动重发。"
    with database.transaction(write=True) as connection:
        rows = connection.execute(
            """SELECT id FROM article_publications
               WHERE status = 'publishing' AND (platform_id <> 'wechat' OR remote_id IS NULL)"""
        ).fetchall()
        publication_ids = [str(row["id"]) for row in rows]
        notification_rows = connection.execute(
            """SELECT id FROM article_publications
               WHERE fan_notification_status = 'sending' AND fan_notification_remote_id IS NULL"""
        ).fetchall()
        notification_ids = [str(row["id"]) for row in notification_rows]
        ids = list(dict.fromkeys([*publication_ids, *notification_ids]))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        if publication_ids:
            publication_placeholders = ",".join("?" for _ in publication_ids)
            connection.execute(
                f"""UPDATE article_publications SET status = 'unknown', message = ?, updated_at = ?
                    WHERE id IN ({publication_placeholders})""",
                (message, now, *publication_ids),
            )
        if notification_ids:
            notification_placeholders = ",".join("?" for _ in notification_ids)
            connection.execute(
                f"""UPDATE article_publications
                    SET fan_notification_status = 'unknown',
                        fan_notification_message = '应用中断时群发结果待核实，请先检查公众号后台，不要重发。',
                        updated_at = ? WHERE id IN ({notification_placeholders})""",
                (now, *notification_ids),
            )
        result = connection.execute(
            f"SELECT * FROM article_publications WHERE id IN ({placeholders}) ORDER BY created_at DESC",
            ids,
        ).fetchall()
    return [_publication_payload(row) for row in result]


def latest_publication(
    database: Database,
    article_id: str,
    platform_id: str,
    article_version: int,
    account_id: str | None = None,
) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT * FROM article_publications WHERE article_id = ? AND platform_id = ?
               AND article_version = ? AND account_id IS ? ORDER BY created_at DESC, id DESC LIMIT 1""",
            (article_id, platform_id, article_version, account_id),
        ).fetchone()
    return _publication_payload(row) if row is not None else None


def _article_list_item(row: sqlite3.Row) -> dict[str, Any]:
    publication_map = _decode_object(row["publication_json"] if "publication_json" in row.keys() else None)
    source_metadata = _decode_object(row["source_metadata"] if "source_metadata" in row.keys() else None)
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "summary": str(row["summary"]),
        "status": str(row["status"]),
        "version": int(row["version"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "source_title": source_metadata.get("title"),
        "source_task_id": row["source_task_id"],
        "source_artifact_id": row["source_artifact_id"],
        "publication_statuses": publication_map,
        "published_platforms": [key for key, value in publication_map.items() if value in {"published", "manual_published", "partial"}],
    }


def _publication_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "article_id": str(row["article_id"]),
        "platform_id": str(row["platform_id"]),
        "status": str(row["status"]),
        "remote_id": row["remote_id"],
        "remote_url": row["remote_url"],
        "message": str(row["message"]),
        "article_version": int(row["article_version"]),
        "account_id": row["account_id"] if "account_id" in row.keys() else None,
        "account_name": row["account_name"] if "account_name" in row.keys() else None,
        "options": _decode_object(row["options_json"] if "options_json" in row.keys() else None),
        "fan_notification_status": str(row["fan_notification_status"]) if "fan_notification_status" in row.keys() else "not_requested",
        "fan_notification_remote_id": row["fan_notification_remote_id"] if "fan_notification_remote_id" in row.keys() else None,
        "fan_notification_message": str(row["fan_notification_message"] or "") if "fan_notification_message" in row.keys() else "",
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _decode_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value)) if value is not None else {}
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clean_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:maximum]
