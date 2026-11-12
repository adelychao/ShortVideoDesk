"""Version-26 migration collapses knowledge-card versions to one row per step."""

from __future__ import annotations

import json
from pathlib import Path

from app.workbench.database import Database

CARD_KINDS = ("knowledge_card", "knowledge_card_revision", "knowledge_card_approved")


def _rollback_to_v25(database: Database) -> None:
    """Simulate a pre-migration ledger by replaying version 26 later."""
    with database.transaction(write=True) as connection:
        connection.execute("DELETE FROM schema_version WHERE version >= 26")


def _insert_card(
    database: Database,
    *,
    task_id: str,
    step_id: str,
    artifact_id: str,
    kind: str,
    version: int,
    created_at: str,
    title: str = "示例知识卡",
) -> None:
    metadata = json.dumps({"title": title, "media_type": "text/markdown"}, ensure_ascii=False)
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO artifacts (id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, task_id, step_id, kind, f"archived/{artifact_id}.md", f"sha-{artifact_id}", version, metadata, created_at),
        )


def _card_rows(database: Database) -> list[tuple[str, str]]:
    with database.transaction() as connection:
        rows = connection.execute(
            f"""SELECT id, kind FROM artifacts
                WHERE kind IN ({','.join('?' * len(CARD_KINDS))})
                ORDER BY task_id, step_id, created_at DESC""",
            CARD_KINDS,
        ).fetchall()
    return [(str(row["id"]), str(row["kind"])) for row in rows]


def _step_outputs(database: Database, task_id: str, step_id: str) -> list[str]:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
            (step_id, task_id),
        ).fetchone()
    assert row is not None
    return [str(item) for item in json.loads(str(row["output_artifact_ids_json"]))]


def _seed_task(database: Database, task_id: str, asset_id: str) -> str:
    step_id = f"{task_id}-cards"
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO assets (id, identity_key, source_kind, platform, source_id, title, author,
                                    published_at, archived_at, archive_path, sha256, created_at)
               VALUES (?, ?, 'file', NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, 't0')""",
            (asset_id, f"key-{asset_id}", f"素材 {asset_id}"),
        )
        connection.execute(
            """INSERT INTO tasks (id, asset_id, source_json, status, legacy_path, legacy_status, created_at, updated_at)
               VALUES (?, ?, ?, 'completed', NULL, NULL, 't0', 't0')""",
            (task_id, asset_id, json.dumps({"kind": "file", "value": "local.mp4"})),
        )
        connection.execute(
            """INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json,
                                       input_artifact_ids_json, output_artifact_ids_json)
               VALUES (?, ?, 'knowledge_cards', 4, 'completed', '[]', '[]', '[]')""",
            (step_id, task_id),
        )
    return step_id


def test_migration_keeps_only_newest_card_per_task(tmp_path: Path) -> None:
    database = Database(tmp_path / "merge.sqlite3")
    database.migrate()
    _rollback_to_v25(database)

    step_one = _seed_task(database, "task-one", "asset-one")
    _insert_card(database, task_id="task-one", step_id=step_one, artifact_id="card-old", kind="knowledge_card", version=1, created_at="2026-01-01T00:00:00+00:00")
    _insert_card(database, task_id="task-one", step_id=step_one, artifact_id="card-edit", kind="knowledge_card_revision", version=2, created_at="2026-01-02T00:00:00+00:00")
    _insert_card(database, task_id="task-one", step_id=step_one, artifact_id="card-new", kind="knowledge_card_approved", version=3, created_at="2026-01-03T00:00:00+00:00")

    step_two = _seed_task(database, "task-two", "asset-two")
    _insert_card(database, task_id="task-two", step_id=step_two, artifact_id="solo-card", kind="knowledge_card", version=1, created_at="2026-02-01T00:00:00+00:00")

    with database.transaction(write=True) as connection:
        connection.execute(
            "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
            (json.dumps(["card-old", "card-edit", "card-new", "other-output"]), step_one, "task-one"),
        )

    database.migrate()

    assert _card_rows(database) == [("card-new", "knowledge_card_approved"), ("solo-card", "knowledge_card")]
    assert _step_outputs(database, "task-one", step_one) == ["card-new", "other-output"]


def test_migration_is_idempotent_without_cards(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.sqlite3")
    database.migrate()
    _rollback_to_v25(database)
    database.migrate()
    with database.transaction() as connection:
        version = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    assert int(version["version"]) == 26
    assert _card_rows(database) == []
