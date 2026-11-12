"""Local search API helpers shared by task and HTTP routes."""

from __future__ import annotations

from pathlib import Path

from ..database import Database
from ..search import SearchHit, refresh_local_index, search_local


def search_documents(
    database: Database,
    archive_root: Path,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[dict[str, int], list[SearchHit], int]:
    index_summary = refresh_local_index(database, archive_root)
    hits, total = search_local(database, query, limit=limit, offset=offset)
    return index_summary, hits, total
