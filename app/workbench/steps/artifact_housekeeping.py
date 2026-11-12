"""Organize registered internal records without losing task references."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from .artifact_store import is_internal_artifact


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def organize_internal_records(database, archive_root: Path, folder: Path) -> dict[str, int]:
    root = Path(archive_root).resolve(strict=True)
    original_folder = Path(folder)
    if not original_folder.is_absolute() or original_folder.is_symlink():
        raise ValueError('invalid archive folder')
    folder = original_folder.resolve(strict=True)
    relative = folder.relative_to(root)
    if not relative.parts or not folder.is_dir():
        raise ValueError('invalid archive folder')
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('archive path contains a symbolic link')
    internal = folder / '.workbench'
    if internal.is_symlink():
        raise ValueError('internal folder is a symbolic link')
    internal.mkdir(exist_ok=True)
    if internal.resolve(strict=True).parent != folder or internal.is_symlink():
        raise ValueError('internal folder is unavailable')

    with database.transaction() as connection:
        rows = connection.execute("""SELECT artifacts.id, artifacts.kind, artifacts.path, artifacts.sha256, tasks.status
            FROM artifacts JOIN tasks ON tasks.id = artifacts.task_id""").fetchall()
    moved = skipped = 0
    for row in rows:
        source = Path(str(row['path']))
        if not is_internal_artifact(str(row['kind'])) or source.parent != folder:
            continue
        if row['status'] in {'queued', 'running', 'awaiting_authorization'}:
            skipped += 1
            continue
        target = internal / source.name
        created = False
        committed = False
        try:
            if source.is_symlink() or not source.is_file() or _digest(source).casefold() != str(row['sha256']).casefold():
                skipped += 1
                continue
            with source.open('rb') as source_stream, target.open('xb') as target_stream:
                created = True
                while chunk := source_stream.read(1024 * 1024):
                    target_stream.write(chunk)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if target.is_symlink() or _digest(target).casefold() != str(row['sha256']).casefold():
                raise ValueError('record copy failed verification')
            with database.transaction(write=True) as connection:
                updated = connection.execute("""UPDATE artifacts SET path = ?
                    WHERE id = ? AND path = ? AND sha256 = ?
                    AND task_id IN (SELECT id FROM tasks WHERE status NOT IN ('queued','running','awaiting_authorization'))""",
                    (str(target), row['id'], str(source), row['sha256']))
                if updated.rowcount != 1:
                    raise ValueError('record changed during organization')
                referenced = connection.execute('SELECT 1 FROM artifacts WHERE path = ? LIMIT 1', (str(source),)).fetchone()
            committed = True
            moved += 1
            if not referenced and not source.is_symlink() and source.is_file() and _digest(source).casefold() == str(row['sha256']).casefold():
                source.unlink()
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            if not committed:
                skipped += 1
                if created and not target.is_symlink():
                    target.unlink(missing_ok=True)
    return {'moved': moved, 'skipped': skipped}
