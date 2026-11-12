"""Local full-text indexing over registered, human-readable artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .archive import _assert_contained, _prepare_archive_root
from .database import Database


_TEXT_KINDS = frozenset({
    "note", "transcript", "transcript_draft", "transcript_revision", "ocr", "ocr_result", "ocr_revision",
    "analysis", "script", "script_revision", "final_script", "knowledge_card",
    "knowledge_card_revision", "knowledge_card_approved", "comment_analysis",
})
_TEXT_SUFFIXES = frozenset({".md", ".txt"})
_CJK = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9][a-z0-9._'-]{1,}", re.IGNORECASE)
_MAX_DOCUMENT_BYTES = 5_000_000


@dataclass(frozen=True)
class SearchHit:
    asset_id: str
    task_id: str
    artifact_id: str | None
    snippet: str
    start_offset: int
    score: float
    method: str = "full_text"


def refresh_local_index(database: Database, archive_root: Path) -> dict[str, int]:
    """Refresh only registered text artifacts inside the configured archive."""
    # Validate the configured path before resolving it. Resolving first would
    # hide a symlink if the configured directory were replaced after saving.
    configured_root = Path(archive_root)
    if _path_has_link_ancestor(configured_root):
        raise ValueError("归档目录路径不能包含符号链接或目录联接。")
    root = _prepare_archive_root(configured_root)
    if _path_has_link_ancestor(configured_root):
        raise ValueError("归档目录路径不能包含符号链接或目录联接。")
    added_or_updated = 0
    stale = 0
    seen: set[str] = set()
    document_count = 0
    with database.transaction(write=True) as connection:
        fts_available = _fts_available(connection)
        if fts_available:
            fts_count = int(connection.execute("SELECT COUNT(*) AS count FROM search_documents_fts").fetchone()["count"])
            stored_count = int(connection.execute("SELECT COUNT(*) AS count FROM search_documents").fetchone()["count"])
            if fts_count == 0 and stored_count:
                connection.execute(
                    "INSERT INTO search_documents_fts(doc_id, content) SELECT id, content FROM search_documents"
                )
        title_assets = connection.execute(
            "SELECT id, title, source_id, author, platform, archive_path, archived_at, sha256 FROM assets"
        ).fetchall()
        archived_asset_ids: set[str] = set()
        for asset in title_assets:
            if _has_verified_archive_registration(connection, asset, root):
                archived_asset_ids.add(str(asset["id"]))
        for asset in title_assets:
            title_doc_id = f"asset-title-{asset['id']}"
            if str(asset["id"]) not in archived_asset_ids:
                if fts_available:
                    _remove_fts(connection, title_doc_id)
                connection.execute("DELETE FROM search_documents WHERE id = ?", (title_doc_id,))
                continue
            title_parts = [asset[key] for key in ("title", "source_id", "author", "platform") if isinstance(asset[key], str) and asset[key].strip()]
            if not title_parts:
                if fts_available:
                    _remove_fts(connection, title_doc_id)
                connection.execute("DELETE FROM search_documents WHERE id = ?", (title_doc_id,))
                continue
            title_content = "\n".join(str(value) for value in title_parts)
            title_digest = hashlib.sha256(title_content.encode("utf-8")).hexdigest()
            current = connection.execute("SELECT sha256, content FROM search_documents WHERE id = ?", (title_doc_id,)).fetchone()
            if current is not None and current["sha256"] == title_digest and current["content"] == title_content:
                if fts_available:
                    _upsert_fts(connection, title_doc_id, title_content)
                continue
            connection.execute(
                """INSERT INTO search_documents(id, asset_id, artifact_id, source_path, sha256, content, indexed_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       asset_id = excluded.asset_id,
                       source_path = excluded.source_path,
                       sha256 = excluded.sha256,
                       content = excluded.content,
                       indexed_at = excluded.indexed_at""",
                (title_doc_id, str(asset["id"]), f"asset-title:{asset['id']}", title_digest, title_content, _utc_now_iso()),
            )
            if fts_available:
                _upsert_fts(connection, title_doc_id, title_content)
        artifacts = connection.execute(
            """SELECT ar.id, t.asset_id, ar.task_id, ar.kind, ar.path, ar.sha256,
                      a.title, a.source_id, a.author
               FROM artifacts AS ar JOIN tasks AS t ON t.id = ar.task_id
               JOIN assets AS a ON a.id = t.asset_id
               WHERE lower(ar.kind) IN (""" + ",".join("?" for _ in _TEXT_KINDS) + ")",
            tuple(sorted(_TEXT_KINDS)),
        ).fetchall()
        for row in artifacts:
            artifact_id = str(row["id"])
            seen.add(artifact_id)
            path = Path(str(row["path"]))
            try:
                if path.is_symlink():
                    raise ValueError("symlink")
                safe_path = _assert_contained(path, root, strict=True)
                if safe_path.suffix.casefold() not in _TEXT_SUFFIXES or not safe_path.is_file():
                    raise ValueError("unsupported")
                if safe_path.stat().st_size > _MAX_DOCUMENT_BYTES:
                    raise ValueError("too_large")
                raw = safe_path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                content = raw.decode("utf-8-sig")
            except (OSError, RuntimeError, UnicodeError, ValueError):
                if fts_available:
                    _remove_fts(connection, artifact_id)
                connection.execute("DELETE FROM search_documents WHERE artifact_id = ?", (artifact_id,))
                stale += 1
                continue
            title_lines = [value for value in (row["title"], row["source_id"], row["author"]) if isinstance(value, str) and value.strip()]
            indexed_content = "\n".join([*title_lines, content])
            current = connection.execute(
                "SELECT sha256, content FROM search_documents WHERE artifact_id = ? ORDER BY indexed_at DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
            if current is not None and current["sha256"] == digest and current["content"] == indexed_content:
                if fts_available:
                    _upsert_fts(connection, artifact_id, indexed_content)
                continue
            connection.execute(
                """INSERT INTO search_documents(id, asset_id, artifact_id, source_path, sha256, content, indexed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(artifact_id) DO UPDATE SET
                       asset_id = excluded.asset_id,
                       source_path = excluded.source_path,
                       sha256 = excluded.sha256,
                       content = excluded.content,
                       indexed_at = excluded.indexed_at""",
                (
                    uuid.uuid4().hex,
                    str(row["asset_id"]),
                    artifact_id,
                    str(safe_path),
                    digest,
                    indexed_content,
                    _utc_now_iso(),
                ),
            )
            if fts_available:
                _upsert_fts(connection, artifact_id, indexed_content)
            added_or_updated += 1
        existing = connection.execute("SELECT artifact_id FROM search_documents WHERE artifact_id IS NOT NULL").fetchall()
        for row in existing:
            if str(row["artifact_id"]) not in seen:
                if fts_available:
                    _remove_fts(connection, str(row["artifact_id"]))
                connection.execute("DELETE FROM search_documents WHERE artifact_id = ?", (row["artifact_id"],))
                stale += 1
        if fts_available:
            connection.execute(
                "DELETE FROM search_documents_fts WHERE doc_id NOT IN (SELECT id FROM search_documents)"
            )
        document_count = int(connection.execute("SELECT COUNT(*) AS count FROM search_documents").fetchone()["count"])
    return {"indexed": added_or_updated, "removed": stale, "documents": document_count}


def search_local(database: Database, query: str, *, limit: int = 20, offset: int = 0) -> tuple[list[SearchHit], int]:
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("请输入不超过 500 个字符的检索内容。")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100_000:
        raise ValueError("检索分页参数无效。")
    clean_query = " ".join(query.split()).casefold()
    terms = _search_terms(clean_query)
    if not terms:
        raise ValueError("请至少输入两个英文字母/数字或一个中文字符。")
    clauses = " OR ".join("lower(d.content) LIKE lower(?) ESCAPE '\\'" for _ in terms)
    params = tuple("%" + _escape_like(term) + "%" for term in terms)
    with database.transaction() as connection:
        rows_by_id: dict[str, sqlite3.Row] = {}
        select_sql = """SELECT d.id AS document_id, d.asset_id, d.artifact_id, d.content,
                           COALESCE(ar.task_id, (SELECT t.id FROM tasks AS t WHERE t.asset_id = d.asset_id ORDER BY t.created_at DESC LIMIT 1)) AS task_id
                    FROM search_documents AS d
                    LEFT JOIN artifacts AS ar ON ar.id = d.artifact_id"""
        if _fts_available(connection):
            fts_terms = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            try:
                fts_rows = connection.execute(
                    select_sql + " JOIN search_documents_fts ON d.id = search_documents_fts.doc_id WHERE search_documents_fts MATCH ?",
                    (fts_terms,),
                ).fetchall()
            except sqlite3.OperationalError:
                fts_rows = []
            rows_by_id.update({str(row["document_id"]): row for row in fts_rows})
        # Always merge parameterized LIKE matches. FTS tokenizers can return a
        # partial set for mixed Chinese/Latin queries, which would otherwise
        # corrupt totals and hide later pages.
        like_rows = connection.execute(select_sql + f" WHERE {clauses}", params).fetchall()
        rows_by_id.update({str(row["document_id"]): row for row in like_rows})
        rows = list(rows_by_id.values())
    hits: list[SearchHit] = []
    for row in rows:
        if not isinstance(row["task_id"], str):
            continue
        content = str(row["content"])
        score, start = _score(content, clean_query, terms)
        if score <= 0:
            continue
        left = max(0, start - 100)
        right = min(len(content), start + max(220, len(clean_query) + 100))
        snippet = " ".join(content[left:right].split())
        if left:
            snippet = "…" + snippet
        if right < len(content):
            snippet += "…"
        hits.append(SearchHit(
            asset_id=str(row["asset_id"]),
            task_id=str(row["task_id"]),
            artifact_id=str(row["artifact_id"]) if row["artifact_id"] is not None else None,
            snippet=snippet,
            start_offset=start,
            score=score,
        ))
    hits.sort(key=lambda hit: (-hit.score, hit.asset_id, hit.artifact_id or ""))
    total = len(hits)
    return hits[offset:offset + limit], total


def _has_verified_archive_registration(connection, asset, root: Path) -> bool:
    archive_path = asset["archive_path"]
    if not isinstance(archive_path, str) or not archive_path or not asset["archived_at"]:
        return False
    try:
        registered_media = _lexical_path_within_root(Path(archive_path), root)
        if not registered_media.is_file() or _path_has_symlink_component(registered_media, root):
            return False
        checked_media = _assert_contained(registered_media, root, strict=True)
        if checked_media != registered_media:
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    rows = connection.execute(
        """SELECT ar.path, ar.sha256 FROM artifacts AS ar
           JOIN tasks AS t ON t.id = ar.task_id WHERE t.asset_id = ?
           AND lower(ar.kind) IN ('media', 'video', 'audio', 'legacy_video', 'legacy_audio')""",
        (str(asset["id"]),),
    ).fetchall()
    for row in rows:
        try:
            media_path = _lexical_path_within_root(Path(str(row["path"])), root)
            if _path_has_symlink_component(media_path, root):
                continue
            checked_media = _assert_contained(media_path, root, strict=True)
            same_registered_path = os.path.normcase(os.path.normpath(str(media_path))) == os.path.normcase(
                os.path.normpath(str(registered_media))
            )
            same_registered_hash = str(row["sha256"] or "").casefold() == str(asset["sha256"] or "").casefold()
            if checked_media == media_path == registered_media and media_path.is_file() and same_registered_path and same_registered_hash:
                return True
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return False


def _lexical_path_within_root(candidate: Path, root: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError("registered archive path is outside the configured root") from exc
    if not relative.parts:
        raise ValueError("registered archive path must be below the configured root")
    return absolute


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _path_has_link_ancestor(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            attributes = current.lstat().st_file_attributes if os.name == "nt" else 0
            if current.is_symlink() or attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("归档目录路径不可检查。") from exc
    return False


_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _search_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _CJK.findall(query):
        if len(match) == 1:
            terms.append(match)
        else:
            terms.extend(match[index:index + 2] for index in range(len(match) - 1))
    terms.extend(_LATIN.findall(query))
    if query:
        terms.append(query)
    deduped: list[str] = []
    for term in terms:
        if term and term not in deduped:
            deduped.append(term)
        if len(deduped) >= 40:
            break
    return deduped


def _score(content: str, query: str, terms: list[str]) -> tuple[float, int]:
    lowered = content.casefold()
    exact = lowered.find(query)
    score = 8.0 if exact >= 0 else 0.0
    first = exact if exact >= 0 else len(content)
    for term in terms:
        position = lowered.find(term)
        if position < 0:
            continue
        first = min(first, position)
        score += min(4, lowered.count(term)) * (2.0 if len(term) > 1 else 1.0)
    return score, first if first < len(content) else 0


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_available(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_documents_fts'"
    ).fetchone() is not None


def _upsert_fts(connection, doc_id: str, content: str) -> None:
    _remove_fts(connection, doc_id)
    connection.execute(
        "INSERT INTO search_documents_fts(doc_id, content) VALUES (?, ?)",
        (doc_id, content),
    )


def _remove_fts(connection, doc_id: str) -> None:
    connection.execute("DELETE FROM search_documents_fts WHERE doc_id = ?", (doc_id,))


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
