"""SQLite connection and versioned schema management for the workbench."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "workbench.sqlite3"
SCHEMA_VERSION = 31
BUSY_TIMEOUT_MS = 15_000


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            source_kind TEXT NOT NULL CHECK (source_kind IN ('url', 'file')),
            platform TEXT,
            source_id TEXT,
            title TEXT,
            author TEXT,
            published_at TEXT,
            archived_at TEXT,
            archive_path TEXT,
            sha256 TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES assets(id),
            source_json TEXT NOT NULL,
            status TEXT NOT NULL,
            legacy_path TEXT UNIQUE,
            legacy_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS task_steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN (
                'download', 'import', 'transcribe', 'ocr', 'comments',
                'analyze_comments', 'analyze_structure', 'knowledge_cards',
                'generate_scripts', 'index', 'write_obsidian'
            )),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            status TEXT NOT NULL,
            depends_on_json TEXT NOT NULL,
            input_artifact_ids_json TEXT NOT NULL,
            output_artifact_ids_json TEXT NOT NULL,
            progress REAL,
            message TEXT,
            error_code TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            started_at TEXT,
            finished_at TEXT,
            UNIQUE (task_id, ordinal)
        )""",
        """CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (task_id, step_id, kind, version)
        )""",
        """CREATE TABLE IF NOT EXISTS consents (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step_kind TEXT NOT NULL CHECK (step_kind IN (
                'download', 'import', 'transcribe', 'ocr', 'comments',
                'analyze_comments', 'analyze_structure', 'knowledge_cards',
                'generate_scripts', 'index', 'write_obsidian'
            )),
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            artifact_ids_json TEXT NOT NULL,
            authorized INTEGER NOT NULL CHECK (authorized IN (0, 1)),
            authorized_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS model_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step_id TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            artifact_ids_json TEXT NOT NULL,
            request_id TEXT,
            usage_json TEXT NOT NULL,
            cost_json TEXT,
            status TEXT NOT NULL,
            error_code TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            platform_comment_id TEXT NOT NULL,
            parent_id TEXT,
            author TEXT,
            text TEXT NOT NULL,
            like_count INTEGER,
            created_at TEXT,
            collected_at TEXT NOT NULL,
            raw_fields_json TEXT NOT NULL,
            UNIQUE (asset_id, platform_comment_id)
        )""",
        """CREATE TABLE IF NOT EXISTS search_documents (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            artifact_id TEXT REFERENCES artifacts(id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            content TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at ON tasks(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_assets_platform_source_id ON assets(platform, source_id)",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path)",
        "CREATE INDEX IF NOT EXISTS idx_task_steps_order ON task_steps(task_id, ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_model_runs_task_created ON model_runs(task_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_comments_asset_created ON comments(asset_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_search_documents_asset ON search_documents(asset_id)",
    ),
    # Version 2 is a data migration. It scrubs URL source records that may
    # have been persisted before URL sanitization was enforced on write.
    2: (),
    3: (
        """CREATE TABLE IF NOT EXISTS comment_collection_state (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            next_cursor TEXT,
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            collected_count INTEGER NOT NULL DEFAULT 0 CHECK (collected_count >= 0),
            missing_like_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_like_count >= 0),
            missing_fields_json TEXT NOT NULL DEFAULT '{}',
            stop_reason TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS comment_collection_items (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (task_id, comment_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_comment_collection_items_comment ON comment_collection_items(comment_id)",
        "CREATE INDEX IF NOT EXISTS idx_comment_state_asset ON comment_collection_state(asset_id, updated_at DESC)",
    ),
    4: (
        "ALTER TABLE comment_collection_state ADD COLUMN sort_order TEXT NOT NULL DEFAULT 'likes_desc' CHECK (sort_order IN ('likes_desc', 'time_desc', 'time_asc'))",
    ),
    5: (
        "DELETE FROM search_documents WHERE artifact_id IS NOT NULL AND rowid NOT IN (SELECT MAX(rowid) FROM search_documents WHERE artifact_id IS NOT NULL GROUP BY artifact_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_documents_artifact ON search_documents(artifact_id)",
    ),
    6: (
        """CREATE TABLE IF NOT EXISTS obsidian_writes (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
            output_root TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (artifact_id, output_root, relative_path, sha256)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_obsidian_writes_artifact ON obsidian_writes(artifact_id, created_at DESC)",
    ),
    7: (
        """CREATE TABLE IF NOT EXISTS local_transcription_options (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            language TEXT NOT NULL,
            device TEXT NOT NULL
        )""",
    ),
    8: (
        """CREATE TABLE IF NOT EXISTS platform_access_options (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('anonymous', 'account'))
        )""",
    ),
    9: (
        """CREATE TABLE IF NOT EXISTS ocr_task_options (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('local', 'cloud')),
            interval_seconds REAL NOT NULL CHECK (interval_seconds >= 0.5 AND interval_seconds <= 60)
        )""",
    ),
    10: (
        """CREATE TABLE IF NOT EXISTS local_ocr_checkpoints (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
            frame_index INTEGER NOT NULL CHECK (frame_index >= 1),
            frame_id TEXT NOT NULL,
            sampled_at_seconds REAL NOT NULL CHECK (sampled_at_seconds >= 0),
            source_artifact_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            interval_seconds REAL NOT NULL CHECK (interval_seconds >= 0.5 AND interval_seconds <= 60),
            text TEXT NOT NULL,
            processing_seconds REAL NOT NULL CHECK (processing_seconds >= 0),
            created_at TEXT NOT NULL,
            PRIMARY KEY (task_id, step_id, frame_index)
        )""",
    ),
    11: (
        "ALTER TABLE ocr_task_options ADD COLUMN scan_profile TEXT NOT NULL DEFAULT 'full' CHECK (scan_profile IN ('full', 'subtitles'))",
        "ALTER TABLE local_ocr_checkpoints ADD COLUMN scan_profile TEXT NOT NULL DEFAULT 'full' CHECK (scan_profile IN ('full', 'subtitles'))",
        "ALTER TABLE local_ocr_checkpoints ADD COLUMN sampling_version INTEGER NOT NULL DEFAULT 1 CHECK (sampling_version >= 1)",
        "ALTER TABLE local_ocr_checkpoints ADD COLUMN extraction_seconds REAL NOT NULL DEFAULT 0 CHECK (extraction_seconds >= 0)",
        "ALTER TABLE local_ocr_checkpoints ADD COLUMN inference_seconds REAL NOT NULL DEFAULT 0 CHECK (inference_seconds >= 0)",
    ),
    12: (
        """CREATE TABLE IF NOT EXISTS model_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            asr_model_id TEXT,
            model_base_url TEXT,
            credential_id TEXT NOT NULL,
            legacy_provider_id TEXT,
            is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_profiles_active_name ON model_profiles(name COLLATE NOCASE) WHERE archived = 0",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_profiles_default ON model_profiles(is_default) WHERE is_default = 1 AND archived = 0",
        """CREATE TABLE IF NOT EXISTS task_model_configs (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            credential_id TEXT NOT NULL,
            legacy_provider_id TEXT,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            asr_model_id TEXT,
            model_base_url TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_task_model_configs_profile ON task_model_configs(profile_id)",
    ),
    13: (
        """CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            document_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'finalized')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            source_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            source_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS article_versions (
            id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK (version >= 1),
            title TEXT NOT NULL,
            document_json TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (article_id, version)
        )""",
        """CREATE TABLE IF NOT EXISTS article_publications (
            id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            platform_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft_saved', 'published', 'publishing', 'failed', 'manual_required', 'manual_published')),
            remote_id TEXT,
            remote_url TEXT,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_articles_updated ON articles(updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_task_id, source_artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_article_versions_article ON article_versions(article_id, version DESC)",
        "CREATE INDEX IF NOT EXISTS idx_article_publications_article ON article_publications(article_id, created_at DESC)",
    ),
    14: (
        "ALTER TABLE article_publications ADD COLUMN article_version INTEGER NOT NULL DEFAULT 1 CHECK (article_version >= 1)",
    ),
    15: (
        "ALTER TABLE model_profiles ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '[\"text_generation\"]'",
        "ALTER TABLE model_profiles ADD COLUMN image_model_id TEXT",
        "ALTER TABLE model_profiles ADD COLUMN image_base_url TEXT",
        "ALTER TABLE model_profiles ADD COLUMN is_image_default INTEGER NOT NULL DEFAULT 0 CHECK (is_image_default IN (0, 1))",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_profiles_image_default ON model_profiles(is_image_default) WHERE is_image_default = 1 AND archived = 0",
    ),
    16: (
        "ALTER TABLE article_publications RENAME TO article_publications_v15",
        """CREATE TABLE article_publications_new (
            id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            platform_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft_saved', 'published', 'publishing', 'failed', 'unknown', 'manual_required', 'manual_published')),
            remote_id TEXT,
            remote_url TEXT,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            article_version INTEGER NOT NULL DEFAULT 1 CHECK (article_version >= 1),
            account_id TEXT,
            account_name TEXT,
            options_json TEXT NOT NULL DEFAULT '{}'
        )""",
        """INSERT INTO article_publications_new
               (id, article_id, platform_id, status, remote_id, remote_url, message, created_at, updated_at, article_version)
           SELECT id, article_id, platform_id, status, remote_id, remote_url, message, created_at, updated_at, article_version
           FROM article_publications_v15""",
        "DROP TABLE article_publications_v15",
        "ALTER TABLE article_publications_new RENAME TO article_publications",
        "CREATE INDEX IF NOT EXISTS idx_article_publications_article ON article_publications(article_id, created_at DESC)",
    ),
    17: (
        "ALTER TABLE article_publications ADD COLUMN fan_notification_status TEXT NOT NULL DEFAULT 'not_requested' CHECK (fan_notification_status IN ('not_requested', 'waiting_publication', 'pending', 'sending', 'sent', 'failed', 'unknown'))",
        "ALTER TABLE article_publications ADD COLUMN fan_notification_remote_id TEXT",
        "ALTER TABLE article_publications ADD COLUMN fan_notification_message TEXT NOT NULL DEFAULT ''",
    ),
    18: (
        """CREATE TABLE video_publish_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sau_project_path TEXT,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE video_publish_accounts (
            id TEXT PRIMARY KEY,
            platform_id TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'not_connected' CHECK (status IN ('not_connected', 'login_in_progress', 'checking', 'connected', 'expired', 'error')),
            message TEXT NOT NULL DEFAULT '',
            checked_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (platform_id, account_alias)
        )""",
        """CREATE TABLE video_publish_batches (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
            media_path TEXT NOT NULL,
            media_name TEXT NOT NULL,
            media_size INTEGER NOT NULL CHECK (media_size > 0),
            media_sha256 TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            schedule_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'pending_review', 'published', 'partial', 'failed', 'unknown', 'cancelled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE video_publish_targets (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES video_publish_batches(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL REFERENCES video_publish_accounts(id) ON DELETE RESTRICT,
            platform_id TEXT NOT NULL,
            account_alias TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'pending_review', 'published', 'failed', 'unknown', 'cancelled')),
            progress REAL,
            message TEXT NOT NULL DEFAULT '',
            remote_id TEXT,
            remote_url TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (batch_id, account_id)
        )""",
        """CREATE TABLE video_publish_attempts (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL REFERENCES video_publish_targets(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('running', 'pending_review', 'published', 'failed', 'unknown', 'cancelled')),
            exit_code INTEGER,
            message TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE (target_id, attempt_number)
        )""",
        "CREATE INDEX idx_video_publish_batches_created ON video_publish_batches(created_at DESC, id DESC)",
        "CREATE INDEX idx_video_publish_batches_status ON video_publish_batches(status, updated_at DESC)",
        "CREATE INDEX idx_video_publish_targets_batch ON video_publish_targets(batch_id, created_at, id)",
        "CREATE INDEX idx_video_publish_targets_queue ON video_publish_targets(status, created_at) ",
        "CREATE INDEX idx_video_publish_attempts_target ON video_publish_attempts(target_id, attempt_number DESC)",
    ),
    19: (
        "ALTER TABLE video_publish_targets ADD COLUMN platform_status TEXT",
        "ALTER TABLE video_publish_targets ADD COLUMN platform_message TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE video_publish_targets ADD COLUMN platform_checked_at TEXT",
        "ALTER TABLE video_publish_targets ADD COLUMN platform_observed_at TEXT",
    ),
    20: (
        "ALTER TABLE video_publish_accounts ADD COLUMN profile_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE video_publish_accounts ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''",
    ),
    21: (
        "ALTER TABLE platform_access_options ADD COLUMN account_id TEXT REFERENCES video_publish_accounts(id) ON DELETE SET NULL",
        "ALTER TABLE platform_access_options ADD COLUMN account_name TEXT NOT NULL DEFAULT ''",
    ),
    22: (
        "ALTER TABLE tasks ADD COLUMN archive_removed_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_tasks_archive_removed ON tasks(archive_removed_at, created_at DESC)",
    ),
    23: (
        """CREATE TABLE IF NOT EXISTS transcription_step_configs (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('cloud', 'local')),
            profile_id TEXT,
            profile_name TEXT,
            credential_id TEXT,
            legacy_provider_id TEXT,
            provider_id TEXT,
            model_base_url TEXT,
            asr_model_id TEXT,
            local_model TEXT,
            local_language TEXT,
            local_device TEXT,
            updated_at TEXT NOT NULL
        )""",
    ),
    24: (
        "ALTER TABLE video_publish_targets ADD COLUMN heartbeat_at TEXT",
        "ALTER TABLE video_publish_targets ADD COLUMN last_output_at TEXT",
        "ALTER TABLE video_publish_targets ADD COLUMN upload_retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE video_publish_targets ADD COLUMN diagnostic_code TEXT",
    ),
    25: (
        "ALTER TABLE assets ADD COLUMN description TEXT",
    ),
    # Version 26 is a data migration. Knowledge cards collapse to a single
    # ledger row per task step; superseded revision/approved copies are
    # removed in migrate() below (archived files stay on disk).
    26: (),
    27: (
        """CREATE TABLE IF NOT EXISTS task_script_style_selections (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            selection_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ),
    28: (
        """CREATE TABLE IF NOT EXISTS video_publish_drafts (
            id TEXT PRIMARY KEY,
            media_name TEXT NOT NULL,
            media_extension TEXT NOT NULL,
            media_size INTEGER NOT NULL CHECK (media_size > 0),
            media_sha256 TEXT NOT NULL,
            confirmed_over_limit INTEGER NOT NULL CHECK (confirmed_over_limit IN (0, 1)),
            form_json TEXT NOT NULL,
            covers_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_video_publish_drafts_updated ON video_publish_drafts(updated_at DESC, id DESC)",
    ),
    29: (
        """CREATE TABLE workbench_images (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('video_cover', 'article_image')),
            article_id TEXT,
            directory TEXT NOT NULL,
            original_name TEXT NOT NULL,
            suffix TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            sha256 TEXT NOT NULL,
            keep_until REAL NOT NULL,
            CHECK ((kind = 'article_image') = (article_id IS NOT NULL))
        )""",
        "CREATE INDEX idx_workbench_images_article ON workbench_images(article_id, sha256)",
        "CREATE INDEX idx_workbench_images_expiry ON workbench_images(kind, keep_until)",
        "ALTER TABLE video_publish_batches ADD COLUMN media_cleaned INTEGER NOT NULL DEFAULT 0",
    ),
    30: (
        """CREATE TABLE video_publish_local_sources (
            id TEXT PRIMARY KEY, path TEXT NOT NULL, filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL
        )""",
        "ALTER TABLE video_publish_drafts ADD COLUMN local_source_id TEXT REFERENCES video_publish_local_sources(id)",
        "ALTER TABLE video_publish_batches ADD COLUMN local_source_id TEXT REFERENCES video_publish_local_sources(id)",
    ),
    31: (
        """CREATE TABLE IF NOT EXISTS collection_media_sources (
            id TEXT PRIMARY KEY, path TEXT NOT NULL, filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
            confirmed_over_limit INTEGER NOT NULL DEFAULT 0
        )""",
    ),
}


class Database:
    """Opens a fresh configured SQLite connection for each unit of work."""

    def __init__(self, path: Path | str | None = None, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DATABASE_PATH
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def connect(self) -> sqlite3.Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )"""
            )
            row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
            current_version = int(row["version"] or 0)
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            for version in sorted(_MIGRATIONS):
                if version <= current_version:
                    continue
                for statement in _MIGRATIONS[version]:
                    connection.execute(statement)
                if version == 2:
                    _sanitize_existing_source_json(connection)
                if version == 26:
                    _merge_knowledge_card_versions(connection)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, _utc_now_iso()),
                )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(doc_id UNINDEXED, content, tokenize='unicode61')"
                )
            except sqlite3.OperationalError:
                # Some system SQLite builds omit FTS5; local search has a
                # parameterized LIKE fallback and remains available.
                pass


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _sanitize_existing_source_json(connection: sqlite3.Connection) -> None:
    """Rewrite legacy URL sources using the current persistence sanitizer.

    The caller runs this inside the schema migration transaction, so source
    rewrites and the schema version marker commit or roll back together.
    """
    import json

    from .models import sanitize_source_url

    invalid_source = {
        "kind": "url",
        "value": "https://invalid.invalid/",
        "platform": None,
        "source_id": None,
        "title": None,
        "author": None,
        "published_at": None,
    }
    rows = connection.execute("SELECT id, source_json FROM tasks").fetchall()
    for row in rows:
        raw_json = row["source_json"]
        try:
            source = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            # Corrupt source JSON cannot be safely interpreted, so replace it
            # with a valid opaque source instead of retaining its raw bytes.
            source = None

        if not isinstance(source, dict):
            cleaned = invalid_source
        else:
            kind = source.get("kind")
            if kind == "file":
                # File paths are handled by the separate safe-path migration
                # policy and are outside this URL-only migration.
                continue
            if kind != "url":
                cleaned = invalid_source
            else:
                try:
                    safe_url = sanitize_source_url(source.get("value"))
                except (TypeError, ValueError):
                    cleaned = invalid_source
                else:
                    # Whitelist the persisted SourceRecord fields so unknown
                    # legacy keys cannot retain a duplicate raw URL.
                    cleaned = {
                        "kind": "url",
                        "value": safe_url,
                        "platform": _safe_source_text(source.get("platform")),
                        "source_id": _safe_source_text(source.get("source_id")),
                        "title": _safe_source_text(source.get("title")),
                        "author": _safe_source_text(source.get("author")),
                        "published_at": _safe_source_text(source.get("published_at")),
                    }

        connection.execute(
            "UPDATE tasks SET source_json = ? WHERE id = ?",
            (json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )


def _safe_source_text(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    return value


def _merge_knowledge_card_versions(connection: sqlite3.Connection) -> None:
    """Collapse knowledge-card artifact versions to the newest row per step.

    A task's knowledge card is a single living document now: editing
    overwrites the same logical card and approving flips its status inline.
    This migration drops superseded revision/approved ledger rows so every
    task keeps exactly one card row; the archived markdown files stay on
    disk untouched. Runs inside the schema migration transaction so the
    cleanup and the version marker commit or roll back together.
    """
    import json

    card_kinds = ("knowledge_card", "knowledge_card_revision", "knowledge_card_approved")
    placeholders = ",".join("?" * len(card_kinds))
    rows = connection.execute(
        f"""SELECT id, task_id, step_id FROM artifacts
            WHERE kind IN ({placeholders})
            ORDER BY task_id, step_id, created_at DESC, version DESC, id DESC""",
        card_kinds,
    ).fetchall()
    superseded: list[str] = []
    affected_steps: set[tuple[str, str]] = set()
    seen_steps: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["task_id"]), str(row["step_id"]))
        if key in seen_steps:
            superseded.append(str(row["id"]))
            affected_steps.add(key)
        else:
            seen_steps.add(key)
    if not superseded:
        return
    superseded_set = set(superseded)
    connection.executemany(
        "DELETE FROM artifacts WHERE id = ?",
        [(artifact_id,) for artifact_id in superseded],
    )
    for task_id, step_id in sorted(affected_steps):
        step_row = connection.execute(
            "SELECT output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
            (step_id, task_id),
        ).fetchone()
        if step_row is None:
            continue
        try:
            outputs = json.loads(str(step_row["output_artifact_ids_json"] or "[]"))
        except json.JSONDecodeError:
            outputs = []
        kept = [item for item in outputs if isinstance(item, str) and item not in superseded_set]
        if len(kept) != len(outputs):
            connection.execute(
                "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                (json.dumps(kept, ensure_ascii=False, separators=(",", ":")), step_id, task_id),
            )
