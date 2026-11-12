"""Import a local media file by copying, validating, hashing, and registering it."""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import stat
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, ContextManager

from ..archive import (
    _artifact_from_row,
    _assert_contained,
    _prepare_archive_root,
    _publish_staged_file,
    _validated_local_path,
    archive_path,
    local_archive_time,
    safe_title,
)
from ..database import Database
from ..models import AssetRecord, ArtifactRecord, ImportedMedia, MediaRecoverySummary, StepKind
from ..repository import TaskRepository, _asset_from_row


MAX_SOFT_LIMIT_BYTES = 2 * 1024**3
CHUNK_SIZE = 1024 * 1024
_ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".wav"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav"}
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_PUBLICATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_NAME_PATTERN = re.compile(
    r"^\.workbench-import-([0-9a-f]{32})\.manifest\.json$"
)


def import_media(
    source: Path,
    root: Path,
    task_id: str,
    *,
    confirm_over_limit: bool = False,
    database: Database | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    publication_guard: ContextManager[object] | None = None,
    expected_sha256: str | None = None,
) -> ImportedMedia:
    """Copy and register a user-selected local media file without moving it.

    Files larger than the 2 GiB soft limit require an explicit
    ``confirm_over_limit=True`` from the caller after user confirmation.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a non-empty string")
    if not isinstance(confirm_over_limit, bool):
        raise TypeError("confirm_over_limit must be a boolean")
    if expected_sha256 is not None and not _SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("expected sha256 is invalid")

    db = database or Database()
    db.migrate()
    repository = TaskRepository(db)
    task = repository.get_task(task_id)
    if task is None:
        raise KeyError(f"task {task_id!r} does not exist")
    import_step = next((step for step in task.steps if step.kind is StepKind.IMPORT), None)
    if import_step is None:
        raise ValueError("task does not contain an import step")

    source_path = _validated_local_path(source, label="import source")
    if source_path.is_symlink():
        raise ValueError("import source must not be a symbolic link")
    _assert_path_has_no_symlink_components(source_path)
    try:
        resolved_source = source_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("import source is unavailable") from exc
    if not resolved_source.is_file():
        raise ValueError("import source must be a regular file")
    if resolved_source.suffix.casefold() not in _ALLOWED_EXTENSIONS:
        raise ValueError("unsupported media extension")

    resolved_root = _prepare_archive_root(root)
    extension = resolved_source.suffix.casefold()
    _media_type_for_extension(extension)
    publication_id = uuid.uuid4().hex
    staged_path: Path | None = None
    created_archive_directory: Path | None = None
    marker_path: Path | None = None
    marker_digest: str | None = None
    marker_written = False
    staged_digest: str | None = None

    try:
        staged_path, digest, size_bytes = _copy_and_hash(
            resolved_source,
            resolved_root,
            publication_id,
            confirm_over_limit=confirm_over_limit,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        staged_digest = digest
        if expected_sha256 is not None and digest.casefold() != expected_sha256.casefold():
            raise ValueError("upload changed after its receipt was created")
        if cancel_check is not None:
            cancel_check()
        media_type = _validate_media_header(staged_path, extension)
        if progress_callback is not None:
            progress_callback(90.0, "媒体格式已确认，正在登记归档")

        result: ImportedMedia | None = None
        with (publication_guard or nullcontext()), db.transaction(write=True) as connection:
            if cancel_check is not None:
                cancel_check()
            task_row = connection.execute(
                "SELECT id, asset_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"task {task_id!r} no longer exists")
            step_row = connection.execute(
                """SELECT id, output_artifact_ids_json FROM task_steps
                   WHERE task_id = ? AND kind = 'import'
                   ORDER BY ordinal, id LIMIT 1""",
                (task_id,),
            ).fetchone()
            if step_row is None or step_row["id"] != import_step.id:
                raise ValueError("task import step changed before media registration")

            current_asset_row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (task_row["asset_id"],)
            ).fetchone()
            if current_asset_row is None:
                raise ValueError("task asset is missing")
            current_asset = _asset_from_row(current_asset_row)
            duplicate_rows = _duplicate_asset_rows(
                connection, current_asset.platform, current_asset.source_id, digest
            )
            duplicate_asset: AssetRecord | None = None
            if current_asset.platform and current_asset.source_id:
                for duplicate_row in duplicate_rows:
                    if (
                        isinstance(duplicate_row["platform"], str)
                        and duplicate_row["platform"].casefold()
                        == current_asset.platform.casefold()
                        and duplicate_row["source_id"] == current_asset.source_id
                    ):
                        duplicate_asset = _asset_from_row(duplicate_row)
                        break

            for duplicate_row in duplicate_rows:
                duplicate_candidate = _asset_from_row(duplicate_row)
                for fresh_artifact in _media_artifacts_by_hash(
                    connection, duplicate_candidate.id, digest
                ):
                    try:
                        verified_type = _verify_reusable_artifact(
                            fresh_artifact, resolved_root
                        )
                    except (OSError, ValueError):
                        continue
                    if verified_type != media_type:
                        continue
                    result = _register_reuse_alias(
                        connection,
                        task_id,
                        import_step.id,
                        step_row["output_artifact_ids_json"],
                        fresh_artifact,
                        verified_type,
                        resolved_root,
                    )
                    break
                if result is not None:
                    break

            if result is None:
                canonical_asset = duplicate_asset or current_asset
                if canonical_asset.id != current_asset.id:
                    # The manifest records the asset that owns the downloaded
                    # file. Keep the task's foreign key aligned before the
                    # final file is published so startup recovery can verify
                    # that association without guessing.
                    connection.execute(
                        "UPDATE tasks SET asset_id = ?, updated_at = ? WHERE id = ?",
                        (
                            canonical_asset.id,
                            _datetime_to_db(datetime.now(timezone.utc)),
                            task_id,
                        ),
                    )
                title = canonical_asset.title or safe_title(resolved_source.stem, max_length=512)
                archived_at = canonical_asset.archived_at or datetime.now(timezone.utc)
                folder_platform = canonical_asset.platform or "local"
                folder_source_id = canonical_asset.source_id or digest[:16]
                created_archive_directory = archive_path(
                    resolved_root,
                    archived_at,
                    title,
                    folder_platform,
                    folder_source_id,
                )
                target_path = created_archive_directory / f"media{extension}"
                artifact_id = uuid.uuid4().hex
                artifact_version = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                           WHERE task_id = ? AND step_id = ? AND kind = 'media'""",
                        (task_id, import_step.id),
                    ).fetchone()["version"]
                )
                missing_source_fields = [
                    name
                    for name, value in (
                        ("platform", canonical_asset.platform),
                        ("source_id", canonical_asset.source_id),
                        ("published_at", canonical_asset.published_at),
                    )
                    if value is None
                ]
                metadata: dict[str, object] = {
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "source_extension": resolved_source.suffix.casefold(),
                    "source_metadata_status": "missing" if missing_source_fields else "available",
                    "missing_source_fields": missing_source_fields,
                    "source_metadata": {
                        "platform": canonical_asset.platform,
                        "source_id": canonical_asset.source_id,
                        "published_at": (
                            canonical_asset.published_at.isoformat()
                            if canonical_asset.published_at
                            else None
                        ),
                    },
                    "cover_status": "missing" if media_type == "video" else "not_applicable",
                }
                manifest = {
                    "version": 1,
                    "id": publication_id,
                    "task_id": task_id,
                    "step_id": import_step.id,
                    "asset_id": canonical_asset.id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                    "archive_directory": created_archive_directory.relative_to(
                        resolved_root
                    ).as_posix(),
                    "target": target_path.relative_to(resolved_root).as_posix(),
                    "staged": staged_path.relative_to(resolved_root).as_posix(),
                    "sha256": digest,
                    "size_bytes": size_bytes,
                    "media_type": media_type,
                    "extension": extension,
                    "archived_at": local_archive_time(archived_at).isoformat(),
                    "title": title,
                    "metadata": metadata,
                }
                marker_path = created_archive_directory / (
                    f".workbench-import-{publication_id}.manifest.json"
                )
                marker_digest = _write_publication_manifest(
                    marker_path, manifest, resolved_root
                )
                marker_written = True
                _publish_staged_file(staged_path, target_path, resolved_root)
                published_hash, published_size = _hash_regular_file(
                    target_path, resolved_root
                )
                if published_hash != digest or published_size != size_bytes:
                    raise ValueError("published media does not match its staged hash")
                result, _ = _register_media_manifest(
                    connection,
                    manifest,
                    resolved_root,
                )

        assert result is not None
        if marker_written and marker_path is not None and marker_digest is not None:
            _remove_committed_manifest(marker_path, resolved_root, marker_digest)
        return result
    except BaseException:
        if not marker_written:
            if staged_path is not None:
                _remove_owned_staging_file(
                    staged_path,
                    resolved_root,
                    publication_id,
                    expected_sha256=staged_digest,
                )
            if created_archive_directory is not None:
                _remove_empty_archive_directory(created_archive_directory, resolved_root)
        raise
    finally:
        if staged_path is not None and not marker_written:
            _remove_owned_staging_file(
                staged_path,
                resolved_root,
                publication_id,
                expected_sha256=staged_digest,
            )


def _copy_and_hash(
    source: Path,
    root: Path,
    publication_id: str,
    *,
    confirm_over_limit: bool,
    progress_callback: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[Path, str, int]:
    extension = source.suffix.casefold()
    try:
        source_stream = source.open("rb")
    except OSError as exc:
        raise ValueError("import source is unreadable") from exc

    staged_path: Path | None = None
    stage_created = False
    try:
        with source_stream as input_stream:
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("import source must be a regular file")
            if before.st_size <= 0:
                raise ValueError("import source is empty")
            if before.st_size > MAX_SOFT_LIMIT_BYTES and not confirm_over_limit:
                raise ValueError(
                    "media exceeds the 2 GB soft limit; confirm the import before retrying"
                )
            staged_path = root / f".workbench-import-{publication_id}.tmp"
            _assert_contained(staged_path, root)
            with staged_path.open("xb") as output_stream:
                stage_created = True
                _assert_contained(staged_path, root, strict=True)
                digest = hashlib.sha256()
                copied = 0
                last_reported_percent = -1
                if cancel_check is not None:
                    cancel_check()
                if progress_callback is not None:
                    progress_callback(0.0, "正在复制本地媒体到归档目录")
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = input_stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output_stream.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    percent = min(85, int(copied * 85 / before.st_size))
                    if progress_callback is not None and percent != last_reported_percent:
                        progress_callback(float(percent), f"正在复制本地媒体：{copied:,} / {before.st_size:,} 字节")
                        last_reported_percent = percent
                output_stream.flush()
                os.fsync(output_stream.fileno())

            after = os.fstat(input_stream.fileno())
            if (
                copied != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                raise ValueError("import source changed while it was being copied")
            return staged_path, digest.hexdigest(), copied
    except BaseException:
        if staged_path is not None and stage_created:
            _remove_owned_staging_file(staged_path, root, publication_id)
        raise


def _media_type_for_extension(extension: str) -> str:
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    if extension in _AUDIO_EXTENSIONS:
        return "audio"
    raise ValueError("unsupported media extension")


def _validate_media_header(path: Path, extension: str) -> str:
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            fallback_type = _validate_media_stream(stream, size, extension)
    except OSError as exc:
        raise ValueError("copied media is unreadable") from exc
    probed_type = _probe_media_type(path)
    return probed_type or fallback_type


def _validate_media_stream(stream: BinaryIO, file_size: int, extension: str) -> str:
    media_type = _media_type_for_extension(extension)
    if file_size <= 0:
        raise ValueError("media file is empty")
    if extension in {".mp4", ".mov", ".m4a"}:
        return _validate_iso_bmff(stream, file_size, extension)
    elif extension in {".mkv", ".webm"}:
        return _validate_ebml(stream, file_size, extension)
    elif extension == ".wav":
        _validate_wave(stream, file_size)
    elif extension == ".mp3":
        _validate_mp3(stream, file_size)
    return media_type


def _validate_iso_bmff(stream: BinaryIO, file_size: int, extension: str) -> str:
    offset = 0
    saw_ftyp = False
    saw_moov = False
    saw_mdat = False
    stream_types: set[str] = set()
    box_count = 0
    while offset + 8 <= file_size and box_count < 4096:
        stream.seek(offset)
        header = stream.read(16)
        if len(header) < 8:
            break
        size32 = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        if not all(0x20 <= value <= 0x7e for value in box_type):
            raise ValueError("invalid ISO-BMFF box type")
        header_size = 8
        if size32 == 1:
            if len(header) < 16:
                raise ValueError("truncated ISO-BMFF extended box header")
            box_size = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif size32 == 0:
            box_size = file_size - offset
        else:
            box_size = size32
        if box_size < header_size or box_size > file_size - offset:
            raise ValueError("invalid ISO-BMFF box size")

        if box_type == b"ftyp":
            if saw_ftyp or box_size < header_size + 8 or box_size > 1024 * 1024:
                raise ValueError("invalid ISO-BMFF ftyp box")
            stream.seek(offset + header_size)
            brand_data = stream.read(box_size - header_size)
            if len(brand_data) != box_size - header_size or (len(brand_data) - 8) % 4:
                raise ValueError("truncated ISO-BMFF brand table")
            brands = [brand_data[:4]] + [
                brand_data[index : index + 4]
                for index in range(8, len(brand_data), 4)
            ]
            if not brands or not all(
                len(brand) == 4 and any(brand) and all(0x20 <= value <= 0x7e for value in brand)
                for brand in brands
            ):
                raise ValueError("invalid ISO-BMFF brand table")
            brand_set = set(brands)
            if extension == ".mp4" and not brand_set.intersection(
                {
                    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
                    b"mp41", b"mp42", b"mp71", b"avc1", b"hev1", b"hvc1",
                    b"M4V ", b"MSNV", b"3gp4", b"3gp5", b"3gp6", b"dash",
                }
            ):
                raise ValueError("ISO-BMFF brands do not identify an MP4 file")
            if extension == ".m4a" and not brand_set.intersection(
                {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"mp41", b"mp42", b"isom", b"iso2"}
            ):
                raise ValueError("ISO-BMFF brands do not identify an M4A file")
            if extension == ".mov" and not brand_set.intersection({b"qt  "}):
                raise ValueError("ISO-BMFF brands do not identify a QuickTime file")
            saw_ftyp = True
        elif box_type == b"moov":
            saw_moov = True
            moov_children = _read_iso_boxes(
                stream, offset + header_size, offset + box_size
            )
            for child_type, child_start, child_end in moov_children:
                if child_type != b"trak":
                    continue
                track_children = _read_iso_boxes(stream, child_start, child_end)
                for track_type, track_start, track_end in track_children:
                    if track_type != b"mdia":
                        continue
                    media_children = _read_iso_boxes(stream, track_start, track_end)
                    for media_type, media_start, media_end in media_children:
                        if media_type != b"hdlr":
                            continue
                        if media_end - media_start < 12:
                            raise ValueError("ISO-BMFF handler box is truncated")
                        stream.seek(media_start)
                        fullbox = stream.read(4)
                        handler = stream.read(8)
                        if len(fullbox) != 4 or fullbox[0] != 0 or fullbox[1:] != b"\x00\x00\x00":
                            raise ValueError("invalid ISO-BMFF handler box")
                        if len(handler) != 8:
                            raise ValueError("truncated ISO-BMFF handler box")
                        if handler[:4] != b"\x00\x00\x00\x00":
                            raise ValueError("invalid ISO-BMFF handler reserved field")
                        if handler[4:8] == b"vide":
                            stream_types.add("video")
                        elif handler[4:8] == b"soun":
                            stream_types.add("audio")
        elif box_type == b"mdat":
            saw_mdat = True

        box_count += 1
        offset += box_size

    if box_count >= 4096:
        raise ValueError("ISO-BMFF contains too many top-level boxes")
    if offset != file_size:
        raise ValueError("ISO-BMFF has a truncated trailing box")
    if extension in {".mp4", ".m4a"} and not saw_ftyp:
        raise ValueError("ISO-BMFF ftyp box is missing")
    if not saw_moov or not saw_mdat:
        raise ValueError("ISO-BMFF movie or media data box is missing")
    if not stream_types:
        raise ValueError("ISO-BMFF contains no audio or video track")
    return "video" if "video" in stream_types else "audio"


def _read_iso_boxes(
    stream: BinaryIO, start: int, end: int
) -> list[tuple[bytes, int, int]]:
    boxes: list[tuple[bytes, int, int]] = []
    offset = start
    while offset < end and len(boxes) < 4096:
        if end - offset < 8:
            raise ValueError("truncated nested ISO-BMFF box header")
        stream.seek(offset)
        header = stream.read(16)
        if len(header) < 8:
            raise ValueError("truncated nested ISO-BMFF box header")
        size32 = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if size32 == 1:
            if len(header) < 16:
                raise ValueError("truncated nested ISO-BMFF extended header")
            box_size = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif size32 == 0:
            box_size = end - offset
        else:
            box_size = size32
        if (
            not all(0x20 <= value <= 0x7e for value in box_type)
            or box_size < header_size
            or box_size > end - offset
        ):
            raise ValueError("invalid nested ISO-BMFF box")
        boxes.append((box_type, offset + header_size, offset + box_size))
        offset += box_size
    if len(boxes) >= 4096:
        raise ValueError("ISO-BMFF container has too many nested boxes")
    if offset != end:
        raise ValueError("truncated nested ISO-BMFF container")
    return boxes


def _read_vint(stream: BinaryIO, offset: int, *, remove_marker: bool, max_width: int) -> tuple[int | None, int]:
    stream.seek(offset)
    first_data = stream.read(1)
    if len(first_data) != 1 or first_data[0] == 0:
        raise ValueError("invalid EBML variable integer")
    first = first_data[0]
    mask = 0x80
    width = 1
    while width <= max_width and not first & mask:
        mask >>= 1
        width += 1
    if width > max_width or mask == 0:
        raise ValueError("invalid EBML variable integer width")
    rest = stream.read(width - 1)
    if len(rest) != width - 1:
        raise ValueError("truncated EBML variable integer")
    value = first & (mask - 1) if remove_marker else first
    for byte in rest:
        value = (value << 8) | byte
    if remove_marker and value == (1 << (7 * width)) - 1:
        return None, width
    return value, width


def _validate_ebml(stream: BinaryIO, file_size: int, extension: str) -> str:
    stream.seek(0)
    prefix = stream.read(4)
    if prefix != b"\x1a\x45\xdf\xa3":
        raise ValueError("EBML header is missing")
    header_size, size_width = _read_vint(stream, 4, remove_marker=True, max_width=8)
    if header_size is None or header_size < 1 or 4 + size_width + header_size > file_size:
        raise ValueError("invalid EBML header size")
    header_start = 4 + size_width
    header_end = header_start + header_size
    if header_end > 1024 * 1024:
        raise ValueError("EBML header exceeds the validation limit")

    offset = header_start
    doctype: str | None = None
    header_values: dict[int, int] = {}
    while offset < header_end:
        element_id, id_width = _read_vint(stream, offset, remove_marker=False, max_width=4)
        if element_id is None:
            raise ValueError("invalid EBML header element")
        offset += id_width
        element_size, size_width = _read_vint(stream, offset, remove_marker=True, max_width=8)
        if element_size is None:
            raise ValueError("unknown EBML header element size")
        offset += size_width
        if element_size > header_end - offset:
            raise ValueError("truncated EBML header element")
        if element_id == 0x4282:
            if doctype is not None:
                raise ValueError("duplicate EBML DocType")
            stream.seek(offset)
            try:
                doctype = stream.read(element_size).decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("invalid EBML DocType") from exc
            if not doctype or len(doctype) > 32:
                raise ValueError("invalid EBML DocType")
        elif element_id in {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4287, 0x4285}:
            if element_id in header_values or not 1 <= element_size <= 4:
                raise ValueError("invalid or duplicate EBML header integer")
            stream.seek(offset)
            header_values[element_id] = int.from_bytes(stream.read(element_size), "big")
        offset += element_size

    expected_doctype = "webm" if extension == ".webm" else "matroska"
    if doctype != expected_doctype:
        raise ValueError("EBML DocType does not match the selected extension")
    required_header_fields = {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4287, 0x4285}
    if not required_header_fields.issubset(header_values):
        raise ValueError("EBML header is missing required version fields")
    if (
        header_values[0x4286] < 1
        or header_values[0x42F7] < 1
        or not 1 <= header_values[0x42F2] <= 4
        or not 1 <= header_values[0x42F3] <= 8
        or header_values[0x4287] < 1
        or header_values[0x4285] < 1
        or header_values[0x42F7] > header_values[0x4286]
        or header_values[0x4285] > header_values[0x4287]
    ):
        raise ValueError("invalid EBML header version fields")

    segment_id, segment_id_width = _read_vint(stream, header_end, remove_marker=False, max_width=4)
    if segment_id != 0x18538067:
        raise ValueError("EBML Segment is missing")
    segment_size, segment_size_width = _read_vint(
        stream, header_end + segment_id_width, remove_marker=True, max_width=8
    )
    segment_header_end = header_end + segment_id_width + segment_size_width
    if segment_header_end >= file_size:
        raise ValueError("EBML Segment is empty")
    if segment_size is not None and segment_size > file_size - segment_header_end:
        raise ValueError("invalid EBML Segment size")
    segment_end = file_size if segment_size is None else segment_header_end + segment_size
    if segment_end > file_size or segment_header_end >= segment_end:
        raise ValueError("invalid EBML Segment bounds")
    known_segment_elements = {
        0x114D9B74,  # SeekHead
        0x1549A966,  # Info
        0x1654AE6B,  # Tracks
        0x1F43B675,  # Cluster
        0x1C53BB6B,  # Cues
        0x1941A469,  # Attachments
        0x1043A770,  # Chapters
        0x1254C367,  # Tags
        0xBF,  # CRC-32
        0xEC,  # Void
    }
    saw_tracks = False
    saw_cluster = False
    saw_info = False
    media_track_types: set[str] = set()
    track_numbers: set[int] = set()
    block_track_numbers: set[int] = set()
    offset = segment_header_end
    elements = 0
    while offset < segment_end and elements < 100_000:
        element_id, id_width = _read_vint(stream, offset, remove_marker=False, max_width=4)
        offset += id_width
        if offset > segment_end:
            raise ValueError("truncated EBML Segment element id")
        element_size, size_width = _read_vint(stream, offset, remove_marker=True, max_width=8)
        offset += size_width
        if offset > segment_end:
            raise ValueError("truncated EBML Segment element size")
        if element_id not in known_segment_elements:
            raise ValueError("invalid Matroska/WebM Segment element")
        if element_size is None:
            if element_id != 0x1F43B675:
                raise ValueError("unknown size is only valid for an EBML Cluster")
            element_end = segment_end
        else:
            element_end = offset + element_size
            if element_end > segment_end:
                raise ValueError("EBML Segment element exceeds its bounds")
        if element_id == 0x1654AE6B and element_end > offset:
            if saw_tracks:
                raise ValueError("duplicate EBML Tracks element")
            saw_tracks = True
            track_entries = _read_ebml_children(stream, offset, element_end)
            for child_id, child_start, child_end in track_entries:
                if child_id != 0xAE:
                    continue
                entry_children = _read_ebml_children(stream, child_start, child_end)
                track_type: int | None = None
                track_number: int | None = None
                codec_id: str | None = None
                for field_id, field_start, field_end in entry_children:
                    if field_id == 0xD7:
                        if track_number is not None:
                            raise ValueError("duplicate EBML TrackNumber")
                        field_size = field_end - field_start
                        if field_size not in {1, 2, 3, 4, 5, 6, 7, 8}:
                            raise ValueError("invalid EBML TrackNumber")
                        stream.seek(field_start)
                        track_number = int.from_bytes(stream.read(field_size), "big")
                    elif field_id == 0x83:
                        if track_type is not None:
                            raise ValueError("duplicate EBML TrackType")
                        field_size = field_end - field_start
                        if field_size not in {1, 2, 4, 8}:
                            raise ValueError("invalid EBML TrackType")
                        stream.seek(field_start)
                        track_type = int.from_bytes(stream.read(field_size), "big")
                    elif field_id == 0x86:
                        if codec_id is not None:
                            raise ValueError("duplicate EBML CodecID")
                        if field_end <= field_start or field_end - field_start > 256:
                            raise ValueError("invalid EBML CodecID")
                        stream.seek(field_start)
                        try:
                            codec_id = stream.read(field_end - field_start).decode("ascii")
                        except UnicodeDecodeError as exc:
                            raise ValueError("invalid EBML CodecID") from exc
                if (
                    not track_number
                    or track_number in track_numbers
                    or track_type is None
                    or not 1 <= track_type <= 254
                    or not codec_id
                    or not codec_id.isprintable()
                ):
                    raise ValueError("EBML TrackEntry lacks TrackNumber, TrackType, or CodecID")
                track_numbers.add(track_number)
                if track_type == 1:
                    media_track_types.add("video")
                elif track_type == 2:
                    media_track_types.add("audio")
        if element_id == 0x1549A966:
            if saw_info:
                raise ValueError("duplicate EBML Info element")
            info_children = _read_ebml_children(stream, offset, element_end)
            valid_info_ids = {
                0x2AD7B1, 0x4489, 0x4461, 0x7BA9, 0x4D80, 0x5741,
                0x73A4, 0x3CB923, 0x3EB923, 0x4444, 0x6924, 0xBF, 0xEC,
            }
            if not info_children or any(
                child[0] not in valid_info_ids for child in info_children
            ) or not any(child[0] not in {0xBF, 0xEC} for child in info_children):
                raise ValueError("invalid EBML Info element")
            for info_id, info_start, info_end in info_children:
                if info_id == 0x2AD7B1:
                    if not 1 <= info_end - info_start <= 8:
                        raise ValueError("invalid EBML timestamp scale")
                    stream.seek(info_start)
                    if int.from_bytes(stream.read(info_end - info_start), "big") == 0:
                        raise ValueError("invalid EBML timestamp scale")
                elif info_id == 0x4489:
                    if info_end - info_start not in {4, 8}:
                        raise ValueError("invalid EBML Info duration")
                elif info_id in {0x4D80, 0x5741, 0x7BA9}:
                    if info_end <= info_start or info_end - info_start > 4096:
                        raise ValueError("invalid EBML Info text field")
                    stream.seek(info_start)
                    try:
                        if not stream.read(info_end - info_start).decode("utf-8").strip():
                            raise ValueError("empty EBML Info text field")
                    except UnicodeDecodeError as exc:
                        raise ValueError("invalid EBML Info text field") from exc
            saw_info = True
        if element_id == 0x1F43B675 and element_end > offset:
            cluster_tracks = _validate_ebml_cluster(stream, offset, element_end)
            if cluster_tracks:
                block_track_numbers.update(cluster_tracks)
                saw_cluster = True
        if element_end < offset or (element_end == offset and element_id != 0xEC):
            raise ValueError("empty EBML Segment element")
        offset = element_end
        elements += 1
        if element_size is None:
            break
    if elements >= 100_000:
        raise ValueError("EBML Segment contains too many elements")
    if (
        not saw_tracks
        or not media_track_types
        or not saw_cluster
        or not saw_info
        or not block_track_numbers.issubset(track_numbers)
    ):
        raise ValueError("EBML Segment lacks valid Info, Tracks, or media clusters")
    return "video" if "video" in media_track_types else "audio"


def _read_ebml_children(
    stream: BinaryIO, start: int, end: int
) -> list[tuple[int, int, int]]:
    children: list[tuple[int, int, int]] = []
    offset = start
    while offset < end and len(children) < 100_000:
        element_id, id_width = _read_vint(stream, offset, remove_marker=False, max_width=4)
        offset += id_width
        if offset > end:
            raise ValueError("truncated EBML child id")
        element_size, size_width = _read_vint(stream, offset, remove_marker=True, max_width=8)
        offset += size_width
        if offset > end or element_size is None:
            raise ValueError("invalid EBML child size")
        child_end = offset + element_size
        if child_end > end:
            raise ValueError("EBML child exceeds its parent")
        children.append((element_id, offset, child_end))
        if child_end < offset or (child_end == offset and element_id != 0xEC):
            raise ValueError("empty EBML child")
        offset = child_end
    if len(children) >= 100_000:
        raise ValueError("EBML parent contains too many children")
    if offset != end:
        raise ValueError("truncated EBML parent")
    return children


def _validate_ebml_cluster(stream: BinaryIO, start: int, end: int) -> set[int]:
    cluster_children = _read_ebml_children(stream, start, end)
    saw_timestamp = False
    block_track_numbers: set[int] = set()
    known_cluster_children = {0xE7, 0xA7, 0xAB, 0xA3, 0xA0, 0x5854, 0xBF, 0xEC}
    for element_id, child_start, child_end in cluster_children:
        if element_id not in known_cluster_children:
            raise ValueError("invalid Matroska/WebM Cluster element")
        size = child_end - child_start
        if element_id == 0xE7:
            if size not in {1, 2, 3, 4, 5, 6, 7, 8}:
                raise ValueError("invalid EBML Cluster timestamp")
            saw_timestamp = True
        elif element_id == 0xA3:
            block_track, track_width = _read_vint(
                stream, child_start, remove_marker=True, max_width=8
            )
            if block_track is None or size < track_width + 4:
                raise ValueError("invalid EBML SimpleBlock")
            block_track_numbers.add(block_track)
        elif element_id == 0xA0:
            group_children = _read_ebml_children(
                stream, child_start, child_end
            )
            known_group_children = {
                0xA1, 0xA2, 0x75A1, 0x9B, 0xFB, 0x75A2, 0xA4,
                0x8E, 0xC8, 0xBF, 0xEC,
            }
            for group_id, block_start, block_end in group_children:
                if group_id not in known_group_children:
                    raise ValueError("invalid EBML BlockGroup element")
                if group_id == 0xA1:
                    block_track, track_width = _read_vint(
                        stream, block_start, remove_marker=True, max_width=8
                    )
                    if block_track is None or block_end - block_start < track_width + 4:
                        raise ValueError("invalid EBML Block")
                    block_track_numbers.add(block_track)
    if not saw_timestamp or not block_track_numbers:
        raise ValueError("EBML Cluster lacks a timestamp or media block")
    return block_track_numbers


def _validate_wave(stream: BinaryIO, file_size: int) -> None:
    stream.seek(0)
    header = stream.read(12)
    if len(header) != 12 or header[8:12] != b"WAVE":
        raise ValueError("invalid WAVE header")
    signature = header[:4]
    endian = "big" if signature == b"RIFX" else "little"
    riff_size = int.from_bytes(header[4:8], endian)
    if signature in {b"RF64", b"BW64"}:
        if riff_size != 0xffffffff:
            raise ValueError("invalid RF64/BW64 size marker")
        stream.seek(12)
        ds64_header = stream.read(8)
        if len(ds64_header) != 8 or ds64_header[:4] != b"ds64":
            raise ValueError("RF64/BW64 ds64 chunk is missing")
        ds64_size = int.from_bytes(ds64_header[4:8], "little")
        if ds64_size < 28 or ds64_size > file_size - 20:
            raise ValueError("invalid RF64/BW64 ds64 chunk size")
        ds64_data = stream.read(min(ds64_size, 64))
        if len(ds64_data) < 28:
            raise ValueError("truncated RF64/BW64 ds64 chunk")
        riff_size = int.from_bytes(ds64_data[:8], "little")
        data_size_64 = int.from_bytes(ds64_data[8:16], "little")
        offset = 20 + ds64_size + (ds64_size & 1)
        data_size_override: int | None = data_size_64
        endian = "little"
    elif signature in {b"RIFF", b"RIFX"}:
        if riff_size < 4 or riff_size > file_size - 8:
            raise ValueError("invalid RIFF size")
        offset = 12
        data_size_override = None
    else:
        raise ValueError("unsupported WAVE container")

    container_end = 8 + riff_size
    if container_end != file_size or offset >= container_end:
        raise ValueError("invalid WAVE container length")
    saw_format = False
    saw_data = False
    chunks = 0
    while offset + 8 <= container_end and chunks < 4096:
        stream.seek(offset)
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise ValueError("truncated WAVE chunk header")
        chunk_id = chunk_header[:4]
        chunk_size = int.from_bytes(chunk_header[4:8], endian)
        chunk_data_offset = offset + 8
        if chunk_id == b"data" and chunk_size == 0xffffffff and data_size_override is not None:
            chunk_size = data_size_override
        if chunk_size > container_end - chunk_data_offset:
            raise ValueError("WAVE chunk exceeds its container")
        if chunk_id == b"fmt ":
            if saw_format:
                raise ValueError("duplicate WAVE fmt chunk")
            if chunk_size < 16:
                raise ValueError("WAVE fmt chunk is too short")
            stream.seek(chunk_data_offset)
            fmt = stream.read(min(chunk_size, 40))
            if len(fmt) < 16:
                raise ValueError("truncated WAVE fmt chunk")
            format_tag = int.from_bytes(fmt[0:2], endian)
            channels = int.from_bytes(fmt[2:4], endian)
            sample_rate = int.from_bytes(fmt[4:8], endian)
            byte_rate = int.from_bytes(fmt[8:12], endian)
            block_align = int.from_bytes(fmt[12:14], endian)
            bits_per_sample = int.from_bytes(fmt[14:16], endian)
            if (
                not 1 <= channels <= 32
                or not 1 <= sample_rate <= 768_000
                or not byte_rate
                or not block_align
                or format_tag == 0
            ):
                raise ValueError("invalid WAVE format fields")
            if format_tag == 0xfffe:
                if chunk_size < 40 or len(fmt) < 40 or int.from_bytes(fmt[16:18], endian) < 22:
                    raise ValueError("invalid WAVE extensible format chunk")
                if fmt[28:40] != b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71":
                    raise ValueError("invalid WAVE extensible subtype GUID")
            elif not bits_per_sample and format_tag in {1, 3}:
                raise ValueError("invalid WAVE sample size")
            if format_tag in {1, 3, 0xfffe} and not bits_per_sample:
                raise ValueError("invalid WAVE sample size")
            if format_tag in {1, 3}:
                expected_align = channels * ((bits_per_sample + 7) // 8)
                if block_align != expected_align or byte_rate != sample_rate * block_align:
                    raise ValueError("inconsistent WAVE sample format")
            if format_tag == 3 and bits_per_sample not in {32, 64}:
                raise ValueError("invalid IEEE-float WAVE sample size")
            saw_format = True
        elif chunk_id == b"data":
            if not saw_format:
                raise ValueError("WAVE data chunk precedes its format")
            if chunk_size <= 0:
                raise ValueError("WAVE data chunk is empty")
            saw_data = True
        chunks += 1
        next_offset = chunk_data_offset + chunk_size + (chunk_size & 1)
        if next_offset <= offset:
            raise ValueError("invalid WAVE chunk length")
        if next_offset > container_end:
            raise ValueError("truncated WAVE chunk")
        offset = next_offset
    if chunks >= 4096:
        raise ValueError("WAVE contains too many chunks")
    if offset != container_end:
        raise ValueError("WAVE has a truncated trailing chunk")
    if not saw_format or not saw_data:
        raise ValueError("WAVE format or data chunk is missing")


def _parse_mpeg_frame_header(header: bytes, offset: int, file_size: int) -> tuple[int, tuple[int, int, int]] | None:
    if offset < 0 or offset + 4 > len(header):
        return None
    value = int.from_bytes(header[offset : offset + 4], "big")
    if (value >> 21) & 0x7ff != 0x7ff:
        return None
    version = (value >> 19) & 0x03
    layer = (value >> 17) & 0x03
    bitrate_index = (value >> 12) & 0x0f
    sample_index = (value >> 10) & 0x03
    padding = (value >> 9) & 1
    emphasis = value & 0x03
    if version == 1 or layer == 0 or bitrate_index in {0, 15} or sample_index == 3 or emphasis == 2:
        return None
    mpeg1_bitrates = {
        1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
        2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
        3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    }
    mpeg2_bitrates = {
        1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
        2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
        3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    }
    sample_rates = (44100, 48000, 32000)
    sample_rate = sample_rates[sample_index]
    if version == 2:
        sample_rate //= 2
    elif version == 0:
        sample_rate //= 4
    bitrate_table = mpeg1_bitrates if version == 3 else mpeg2_bitrates
    bitrate = bitrate_table[layer][bitrate_index] * 1000
    if layer == 3:
        frame_length = ((12 * bitrate // sample_rate) + padding) * 4
    elif layer == 1 and version != 3:
        frame_length = (72 * bitrate // sample_rate) + padding
    else:
        frame_length = (144 * bitrate // sample_rate) + padding
    if frame_length < 4 or offset + frame_length > file_size:
        return None
    signature = (version, layer, sample_rate)
    return frame_length, signature


def _validate_mp3(stream: BinaryIO, file_size: int) -> None:
    stream.seek(0)
    start = 0
    prefix = stream.read(10)
    if prefix.startswith(b"ID3"):
        if len(prefix) != 10:
            raise ValueError("truncated ID3 header")
        major, revision, flags = prefix[3], prefix[4], prefix[5]
        if major not in {2, 3, 4} or revision == 0xff:
            raise ValueError("unsupported ID3 version")
        allowed_flags = {2: 0xc0, 3: 0xe0, 4: 0xf0}[major]
        if flags & ~allowed_flags:
            raise ValueError("invalid ID3 flags")
        size_bytes = prefix[6:10]
        if any(value & 0x80 for value in size_bytes):
            raise ValueError("invalid ID3 synchsafe size")
        tag_size = (
            (size_bytes[0] << 21)
            | (size_bytes[1] << 14)
            | (size_bytes[2] << 7)
            | size_bytes[3]
        )
        footer_size = 10 if major == 4 and flags & 0x10 else 0
        start = 10 + tag_size + footer_size
        if start + 8 > file_size:
            raise ValueError("ID3 tag leaves no complete MPEG audio frames")
        if footer_size:
            stream.seek(10 + tag_size)
            footer = stream.read(10)
            if (
                len(footer) != 10
                or footer[:3] != b"3DI"
                or footer[3:6] != prefix[3:6]
                or footer[6:10] != size_bytes
            ):
                raise ValueError("invalid ID3v2.4 footer")
    elif len(prefix) < 4:
        raise ValueError("MP3 header is too short")

    scan_limit = min(max(start, 0) + 4096, file_size - 8)
    stream.seek(start)
    window = stream.read(max(0, scan_limit - start + 8))
    first_frame: tuple[int, tuple[int, int, int]] | None = None
    first_offset: int | None = None
    for offset in range(0, max(0, len(window) - 3)):
        parsed = _parse_mpeg_frame_header(window, offset, file_size - start)
        if parsed is None:
            continue
        next_offset = offset + parsed[0]
        next_frame = _parse_mpeg_frame_header(window, next_offset, file_size - start)
        if next_frame is None or next_frame[1] != parsed[1]:
            continue
        first_frame = parsed
        first_offset = offset
        break
    if first_frame is None or first_offset is None:
        raise ValueError("MP3 does not contain consecutive valid MPEG frames")


def _probe_media_type(path: Path) -> str | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("ffprobe could not inspect the media file") from exc
    if result.returncode != 0:
        raise ValueError("ffprobe rejected the media file")
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe returned invalid media information") from exc
    if not isinstance(streams, list):
        raise ValueError("ffprobe returned invalid media information")
    stream_types = {
        item.get("codec_type")
        for item in streams
        if isinstance(item, dict) and isinstance(item.get("codec_type"), str)
    }
    if "video" in stream_types:
        return "video"
    if "audio" in stream_types:
        return "audio"
    raise ValueError("media file has no readable audio or video stream")


def recover_media_publications(
    root: Path,
    *,
    database: Database | None = None,
) -> MediaRecoverySummary:
    """Reconcile media publication manifests left by an interrupted import.

    Call this once during application startup after the configured archive root
    and database are available. It only registers files whose manifest, path,
    size, hash, container structure, task, and import step all validate.
    Conflicting or unrelated files are left in place for manual review.
    """
    resolved_root = _prepare_archive_root(root)
    db = database or Database()
    db.migrate()
    recovered = already_registered = pending = invalid = 0
    warnings: list[str] = []

    for directory, child_directories, filenames in os.walk(
        resolved_root, topdown=True, followlinks=False
    ):
        parent = Path(directory)
        child_directories[:] = [
            name for name in child_directories if not (parent / name).is_symlink()
        ]
        for filename in filenames:
            match = _MANIFEST_NAME_PATTERN.fullmatch(filename)
            if match is None:
                continue
            marker_path = parent / filename
            publication_id = match.group(1)
            try:
                if marker_path.is_symlink() or not marker_path.is_file():
                    raise ValueError("publication manifest is not a regular file")
                if marker_path.stat().st_size > 256 * 1024:
                    raise ValueError("publication manifest is too large")
                marker_stat_before = marker_path.stat()
                with marker_path.open("rb") as marker_stream:
                    marker_bytes = marker_stream.read(256 * 1024 + 1)
                marker_stat_after = marker_path.stat()
                if (
                    len(marker_bytes) > 256 * 1024
                    or len(marker_bytes) != marker_stat_before.st_size
                    or marker_stat_after.st_size != marker_stat_before.st_size
                    or marker_stat_after.st_mtime_ns != marker_stat_before.st_mtime_ns
                ):
                    raise ValueError("publication manifest changed while being read")
                manifest = json.loads(marker_bytes.decode("utf-8"))
                paths = _validate_publication_manifest(
                    manifest, marker_path, resolved_root, publication_id
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                invalid += 1
                warnings.append(f"{marker_path}: invalid publication manifest ({exc})")
                continue

            staged_path, target_path, extension = paths
            try:
                with db.transaction() as connection:
                    _validate_manifest_db_context(connection, manifest)
                stage_exists = staged_path.exists() or staged_path.is_symlink()
                target_exists = target_path.exists() or target_path.is_symlink()
                if stage_exists:
                    stage_hash, stage_size = _hash_regular_file(staged_path, resolved_root)
                    if stage_hash != manifest["sha256"] or stage_size != manifest["size_bytes"]:
                        raise ValueError("staged media does not match its manifest")
                if target_exists:
                    target_hash, target_size = _hash_regular_file(target_path, resolved_root)
                    if target_hash != manifest["sha256"] or target_size != manifest["size_bytes"]:
                        pending += 1
                        warnings.append(f"{target_path}: existing target conflicts with its manifest")
                        continue
                elif stage_exists:
                    _publish_staged_file(staged_path, target_path, resolved_root)
                    target_exists = True
                else:
                    pending += 1
                    warnings.append(f"{marker_path}: staged and published media are both missing")
                    continue

                published_hash, published_size = _hash_regular_file(
                    target_path, resolved_root
                )
                if (
                    published_hash != manifest["sha256"]
                    or published_size != manifest["size_bytes"]
                ):
                    pending += 1
                    warnings.append(f"{target_path}: published file changed during recovery")
                    continue
                detected_type = _validate_media_header(target_path, extension)
                if detected_type != manifest["media_type"]:
                    raise ValueError("media type does not match its publication manifest")
                with db.transaction(write=True) as connection:
                    _, was_registered = _register_media_manifest(
                        connection, manifest, resolved_root
                    )
                if stage_exists:
                    _remove_owned_staging_file(
                        staged_path,
                        resolved_root,
                        publication_id,
                        expected_sha256=manifest["sha256"],
                    )
                marker_digest = hashlib.sha256(marker_bytes).hexdigest()
                _remove_committed_manifest(marker_path, resolved_root, marker_digest)
                if was_registered:
                    already_registered += 1
                else:
                    recovered += 1
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                pending += 1
                warnings.append(f"{marker_path}: publication remains pending ({exc})")

    return MediaRecoverySummary(
        recovered=recovered,
        already_registered=already_registered,
        pending=pending,
        invalid=invalid,
        warnings=warnings,
    )


def _duplicate_asset_rows(
    connection: sqlite3.Connection,
    platform: str | None,
    source_id: str | None,
    sha256: str,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    if isinstance(platform, str) and platform.strip() and isinstance(source_id, str) and source_id.strip():
        identity_rows = connection.execute(
            """SELECT * FROM assets
               WHERE lower(platform) = ? AND source_id = ?
               ORDER BY CASE WHEN archive_path IS NULL THEN 1 ELSE 0 END,
                        archived_at ASC, created_at ASC, id ASC""",
            (platform.strip().casefold(), source_id.strip()),
        ).fetchall()
        for row in identity_rows:
            if row["id"] not in seen:
                rows.append(row)
                seen.add(str(row["id"]))
    hash_rows = connection.execute(
        """SELECT * FROM assets WHERE lower(sha256) = ?
           ORDER BY CASE WHEN archive_path IS NULL THEN 1 ELSE 0 END,
                    archived_at ASC, created_at ASC, id ASC""",
        (sha256.casefold(),),
    ).fetchall()
    for row in hash_rows:
        if row["id"] not in seen:
            rows.append(row)
            seen.add(str(row["id"]))
    return rows


def _media_artifacts_by_hash(
    connection: sqlite3.Connection, asset_id: str, sha256: str
) -> list[ArtifactRecord]:
    rows = connection.execute(
        """SELECT artifacts.* FROM artifacts
           JOIN tasks ON tasks.id = artifacts.task_id
           WHERE tasks.asset_id = ? AND lower(artifacts.sha256) = ?
             AND artifacts.kind IN ('media', 'video', 'audio', 'legacy_video', 'legacy_audio')
           ORDER BY artifacts.created_at DESC, artifacts.version DESC, artifacts.id DESC""",
        (asset_id, sha256.casefold()),
    ).fetchall()
    return [_artifact_from_row(row) for row in rows]


def _verify_reusable_artifact(artifact: ArtifactRecord, root: Path) -> str:
    _validate_reusable_artifact_path(artifact)
    artifact_path = Path(artifact.path).resolve(strict=True)
    _assert_contained(artifact_path, root, strict=True)
    _assert_no_symlink_components(artifact_path, root)
    digest, _ = _hash_regular_file(artifact_path, root)
    if digest.casefold() != artifact.sha256.casefold():
        raise ValueError("existing media artifact hash does not match its record")
    extension = artifact_path.suffix.casefold()
    media_type = _validate_media_header(artifact_path, extension)
    recorded_type = _reusable_media_type(artifact)
    if recorded_type is not None and media_type != recorded_type:
        raise ValueError("existing media artifact type does not match its record")
    return media_type


def _register_reuse_alias(
    connection: sqlite3.Connection,
    task_id: str,
    step_id: str,
    existing_output_ids_json: str,
    artifact: ArtifactRecord,
    media_type: str,
    root: Path,
) -> ImportedMedia:
    _validate_reusable_artifact_path(artifact)
    path = str(Path(artifact.path).resolve(strict=True))
    _assert_contained(Path(path), root, strict=True)
    _assert_no_symlink_components(Path(path), root)
    digest, size_bytes = _hash_regular_file(Path(path), root)
    if digest.casefold() != artifact.sha256.casefold():
        raise ValueError("reused media changed before alias registration")
    task_row = connection.execute(
        "SELECT asset_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task_row is None:
        raise KeyError(f"task {task_id!r} does not exist")
    asset_row = connection.execute(
        "SELECT * FROM assets WHERE id = ?", (task_row["asset_id"],)
    ).fetchone()
    if asset_row is None:
        raise ValueError("task asset is missing")
    asset = _asset_from_row(asset_row)
    version_row = connection.execute(
        """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
           WHERE task_id = ? AND step_id = ? AND kind = 'media'""",
        (task_id, step_id),
    ).fetchone()
    artifact_id = uuid.uuid4().hex
    version = int(version_row["version"])
    metadata = dict(artifact.metadata)
    metadata["media_type"] = media_type
    metadata["reused"] = True
    metadata["size_bytes"] = size_bytes
    missing_source_fields = [
        name
        for name, value in (
            ("platform", asset.platform),
            ("source_id", asset.source_id),
            ("published_at", asset.published_at),
        )
        if value is None
    ]
    metadata["source_metadata_status"] = "missing" if missing_source_fields else "available"
    metadata["missing_source_fields"] = missing_source_fields
    metadata["source_metadata"] = {
        "platform": asset.platform,
        "source_id": asset.source_id,
        "published_at": asset.published_at.isoformat() if asset.published_at else None,
    }
    metadata["cover_status"] = "missing" if media_type == "video" else "not_applicable"
    now = _datetime_to_db(datetime.now(timezone.utc))
    connection.execute(
        """INSERT INTO artifacts (
               id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
           ) VALUES (?, ?, ?, 'media', ?, ?, ?, ?, ?)""",
        (
            artifact_id,
            task_id,
            step_id,
            path,
            artifact.sha256,
            version,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
    connection.execute(
        """UPDATE assets SET archive_path = ?, sha256 = ?, archived_at = COALESCE(archived_at, ?)
           WHERE id = ?""",
        (path, artifact.sha256, now, asset.id),
    )
    _complete_import_step(connection, task_id, step_id, existing_output_ids_json, artifact_id)
    saved_asset = connection.execute("SELECT * FROM assets WHERE id = ?", (asset.id,)).fetchone()
    saved_artifact = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    assert saved_asset is not None and saved_artifact is not None
    return ImportedMedia(
        asset=_asset_from_row(saved_asset),
        artifact=_artifact_from_row(saved_artifact),
        media_type=media_type,  # type: ignore[arg-type]
    )


def _write_publication_manifest(
    path: Path, manifest: dict[str, object], root: Path
) -> str:
    _assert_contained(path, root)
    publication_id = manifest.get("id")
    if not isinstance(publication_id, str) or not _PUBLICATION_ID_PATTERN.fullmatch(publication_id):
        raise ValueError("publication manifest id is invalid")
    if path.name != f".workbench-import-{publication_id}.manifest.json":
        raise ValueError("publication manifest path does not match its id")
    _assert_no_symlink_components(path.parent, root)
    if path.exists() or path.is_symlink():
        raise FileExistsError("publication manifest already exists")
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            _assert_contained(path, root, strict=True)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
        return hashlib.sha256(payload).hexdigest()
    except BaseException:
        if created and path.is_file() and not path.is_symlink():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _validate_publication_manifest(
    value: object, marker_path: Path, root: Path, publication_id: str
) -> tuple[Path, Path, str]:
    if not isinstance(value, dict):
        raise ValueError("publication manifest must be an object")
    if value.get("version") != 1 or value.get("id") != publication_id:
        raise ValueError("publication manifest version or id is invalid")
    for field_name in ("task_id", "step_id", "asset_id", "artifact_id", "title"):
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ValueError(f"publication manifest {field_name} is invalid")
    artifact_version = value.get("artifact_version")
    if isinstance(artifact_version, bool) or not isinstance(artifact_version, int) or artifact_version < 1:
        raise ValueError("publication manifest artifact version is invalid")
    size_bytes = value.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise ValueError("publication manifest size is invalid")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("publication manifest SHA-256 is invalid")
    extension = value.get("extension")
    if not isinstance(extension, str) or extension not in _ALLOWED_EXTENSIONS:
        raise ValueError("publication manifest extension is invalid")
    media_type = value.get("media_type")
    if media_type not in {"audio", "video"}:
        raise ValueError("publication manifest media type is invalid")
    metadata = value.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("media_type") != media_type
        or metadata.get("size_bytes") != size_bytes
        or metadata.get("source_extension") != extension
    ):
        raise ValueError("publication manifest metadata is invalid")
    archived_at = value.get("archived_at")
    if not isinstance(archived_at, str):
        raise ValueError("publication manifest archive time is invalid")
    archived_datetime = datetime.fromisoformat(archived_at)

    archive_directory = _manifest_relative_path(value.get("archive_directory"), root)
    target_relative = _manifest_relative_path_text(value.get("target"))
    staged_relative = _manifest_relative_path_text(value.get("staged"))
    if len(archive_directory.parts) != 2:
        raise ValueError("publication archive directory must be date/sequence")
    date_part, sequence_part = archive_directory.parts
    datetime.strptime(date_part, "%Y-%m-%d")
    if archived_datetime.strftime("%Y-%m-%d") != date_part:
        raise ValueError("publication date does not match archive time")
    if not re.fullmatch(r"[0-9]{4,}_.+", sequence_part):
        raise ValueError("publication sequence directory is invalid")
    expected_target = archive_directory / f"media{extension}"
    expected_staged = PurePosixPath(f".workbench-import-{publication_id}.tmp")
    if target_relative != expected_target or staged_relative != expected_staged:
        raise ValueError("publication target or staging path does not match the manifest")
    if marker_path.name != f".workbench-import-{publication_id}.manifest.json":
        raise ValueError("publication manifest filename does not match its id")
    if marker_path.parent.resolve(strict=True) != (root / Path(*archive_directory.parts)).resolve(strict=True):
        raise ValueError("publication manifest is outside its archive directory")
    staged_path = root / Path(*staged_relative.parts)
    target_path = root / Path(*target_relative.parts)
    _assert_no_symlink_components(staged_path, root)
    _assert_no_symlink_components(target_path.parent, root)
    _assert_contained(staged_path, root)
    _assert_contained(target_path, root)
    return staged_path, target_path, extension


def _manifest_relative_path(value: object, root: Path) -> PurePosixPath:
    relative = _manifest_relative_path_text(value)
    candidate = root / Path(*relative.parts)
    _assert_no_symlink_components(candidate, root)
    _assert_contained(candidate, root)
    return relative


def _manifest_relative_path_text(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ValueError("publication manifest path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("publication manifest path contains traversal")
    if relative.as_posix() != value:
        raise ValueError("publication manifest path is not canonical")
    if re.match(r"^[A-Za-z]:", value) or value.startswith("//"):
        raise ValueError("publication manifest path is not local")
    for part in relative.parts:
        if (
            any(character in '<>:"/\\|?*' or ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold()
            in {"con", "prn", "aux", "nul", "conin$", "conout$"}
        ):
            raise ValueError("publication manifest path is not Windows-safe")
    return relative


def _assert_no_symlink_components(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=True)
    try:
        relative = path.absolute().relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path escapes the configured archive root") from exc
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("archive path contains a symbolic link")


def _assert_path_has_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("media source path contains a symbolic link")


def _hash_regular_file(path: Path, root: Path) -> tuple[str, int]:
    _assert_contained(path, root, strict=True)
    if path.is_symlink():
        raise ValueError("media path must not be a symbolic link")
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("media path must be a regular file")
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (
                size != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                raise ValueError("media changed while it was being verified")
            return digest.hexdigest(), size
    except OSError as exc:
        raise ValueError("media file is unreadable") from exc


def _register_media_manifest(
    connection: sqlite3.Connection, manifest: dict[str, object], root: Path
) -> tuple[ImportedMedia, bool]:
    task_id = str(manifest["task_id"])
    step_id = str(manifest["step_id"])
    asset_id = str(manifest["asset_id"])
    artifact_id = str(manifest["artifact_id"])
    target_relative = _manifest_relative_path_text(manifest["target"])
    target_path = root / Path(*target_relative.parts)
    target_path = _assert_contained(target_path, root, strict=True)
    step_row, asset_row = _validate_manifest_db_context(connection, manifest)
    now = _datetime_to_db(datetime.now(timezone.utc))
    task_asset_row = connection.execute(
        "SELECT asset_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert task_asset_row is not None
    if str(task_asset_row["asset_id"]) != asset_id:
        # A crash may roll back the pre-publication asset reassignment while
        # leaving the final file and its marker. _validate_manifest_db_context
        # only allows this repair when source identity or the registered hash
        # proves the association.
        connection.execute(
            "UPDATE tasks SET asset_id = ?, updated_at = ? WHERE id = ?",
            (asset_id, now, task_id),
        )
    existing = connection.execute(
        "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    was_registered = existing is not None
    if existing is not None:
        if (
            existing["task_id"] != task_id
            or existing["step_id"] != step_id
            or existing["kind"] != "media"
            or Path(existing["path"]).is_symlink()
            or Path(existing["path"]).absolute() != target_path
            or existing["sha256"] != manifest["sha256"]
            or int(existing["version"]) != int(manifest["artifact_version"])
        ):
            raise ValueError("existing artifact conflicts with publication manifest")
        try:
            existing_metadata = json.loads(existing["metadata_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("existing artifact metadata is invalid") from exc
        if existing_metadata != manifest["metadata"]:
            raise ValueError("existing artifact metadata conflicts with publication manifest")
    else:
        connection.execute(
            """INSERT INTO artifacts (
                   id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
               ) VALUES (?, ?, ?, 'media', ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                task_id,
                step_id,
                str(target_path),
                str(manifest["sha256"]),
                int(manifest["artifact_version"]),
                json.dumps(manifest["metadata"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
    _complete_import_step(
        connection,
        task_id,
        step_id,
        str(step_row["output_artifact_ids_json"]),
        artifact_id,
    )
    if not was_registered:
        connection.execute(
            """UPDATE assets SET archive_path = ?, sha256 = ?,
                   archived_at = COALESCE(archived_at, ?)
               WHERE id = ?""",
            (str(target_path), str(manifest["sha256"]), str(manifest["archived_at"]), asset_id),
        )
    saved_asset = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    saved_artifact = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    assert saved_asset is not None and saved_artifact is not None
    return (
        ImportedMedia(
            asset=_asset_from_row(saved_asset),
            artifact=_artifact_from_row(saved_artifact),
            media_type=str(manifest["media_type"]),  # type: ignore[arg-type]
        ),
        was_registered,
    )


def _validate_manifest_db_context(
    connection: sqlite3.Connection, manifest: dict[str, object]
) -> tuple[sqlite3.Row, sqlite3.Row]:
    task_id = str(manifest["task_id"])
    step_id = str(manifest["step_id"])
    asset_id = str(manifest["asset_id"])
    task_row = connection.execute(
        "SELECT id, asset_id, source_json FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task_row is None:
        raise KeyError(f"task {task_id!r} no longer exists")
    asset_row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset_row is None:
        raise ValueError("publication asset no longer exists")
    if str(task_row["asset_id"]) != asset_id:
        current_asset = connection.execute(
            "SELECT platform, source_id FROM assets WHERE id = ?",
            (task_row["asset_id"],),
        ).fetchone()
        if current_asset is None:
            raise ValueError("task's current asset is missing")
        try:
            source_data = json.loads(task_row["source_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("task source metadata is invalid") from exc
        source_platform = source_data.get("platform") if isinstance(source_data, dict) else None
        source_id = source_data.get("source_id") if isinstance(source_data, dict) else None
        same_source_identity = (
            isinstance(source_platform, str)
            and bool(source_platform)
            and isinstance(source_id, str)
            and bool(source_id)
            and isinstance(asset_row["platform"], str)
            and asset_row["platform"].casefold() == source_platform.casefold()
            and asset_row["source_id"] == source_id
        )
        same_registered_hash = (
            isinstance(asset_row["sha256"], str)
            and asset_row["sha256"].casefold() == str(manifest["sha256"]).casefold()
        )
        if not (same_source_identity or same_registered_hash):
            raise ValueError("publication asset cannot be safely associated with this task")
    step_row = connection.execute(
        """SELECT id, kind, output_artifact_ids_json FROM task_steps
           WHERE task_id = ? AND id = ?""",
        (task_id, step_id),
    ).fetchone()
    if step_row is None or step_row["kind"] != "import":
        raise ValueError("publication import step no longer exists")
    return step_row, asset_row


def _remove_committed_manifest(path: Path, root: Path, expected_digest: str) -> None:
    try:
        _assert_contained(path, root, strict=True)
        if path.is_symlink() or not path.is_file():
            return
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() == expected_digest:
            path.unlink()
            _fsync_directory(path.parent)
    except (OSError, ValueError):
        pass


def _remove_owned_staging_file(
    path: Path,
    root: Path,
    publication_id: str,
    *,
    expected_sha256: str | None = None,
) -> None:
    if not _PUBLICATION_ID_PATTERN.fullmatch(publication_id):
        return
    if path.name != f".workbench-import-{publication_id}.tmp":
        return
    try:
        _assert_contained(path, root, strict=True)
        if path.is_symlink() or not path.is_file():
            return
        if expected_sha256 is not None:
            digest, _ = _hash_regular_file(path, root)
            if digest != expected_sha256:
                return
        path.unlink()
    except (OSError, ValueError):
        pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reusable_media_type(artifact: ArtifactRecord | None) -> str | None:
    if artifact is None:
        return None
    value = artifact.metadata.get("media_type")
    if value in {"video", "audio"}:
        return str(value)
    extension = Path(artifact.path).suffix.casefold()
    try:
        return _media_type_for_extension(extension)
    except ValueError:
        return None


def _validate_reusable_artifact_path(artifact: ArtifactRecord) -> None:
    artifact_path = _validated_local_path(Path(artifact.path), label="existing media artifact")
    if artifact_path.is_symlink():
        raise ValueError("existing media artifact must not be a symbolic link")
    try:
        resolved = artifact_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("existing media artifact is unavailable") from exc
    if not resolved.is_file():
        raise ValueError("existing media artifact is unavailable")


def _complete_import_step(
    connection: sqlite3.Connection,
    task_id: str,
    step_id: str,
    existing_output_ids_json: str,
    artifact_id: str,
) -> None:
    try:
        output_ids = json.loads(existing_output_ids_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task import step has invalid output artifact data") from exc
    if not isinstance(output_ids, list) or not all(isinstance(value, str) for value in output_ids):
        raise ValueError("task import step has invalid output artifact data")
    owned_rows = connection.execute(
        "SELECT id FROM artifacts WHERE task_id = ? AND step_id = ?",
        (task_id, step_id),
    ).fetchall()
    owned_ids = {str(row["id"]) for row in owned_rows}
    output_ids = [value for value in output_ids if value in owned_ids]
    if artifact_id not in output_ids:
        output_ids.append(artifact_id)
    now = _datetime_to_db(datetime.now(timezone.utc))
    connection.execute(
        """UPDATE task_steps SET output_artifact_ids_json = ?, status = 'completed',
               progress = 100, message = ?, finished_at = ?
           WHERE task_id = ? AND id = ?""",
        (
            json.dumps(output_ids, ensure_ascii=False, separators=(",", ":")),
            "媒体已导入并归档",
            now,
            task_id,
            step_id,
        ),
    )
    connection.execute(
        """UPDATE tasks SET status = CASE
               WHEN NOT EXISTS (
                   SELECT 1 FROM task_steps WHERE task_id = ? AND status != 'completed'
               ) THEN 'completed' ELSE 'running' END,
               updated_at = ? WHERE id = ?""",
        (task_id, now, task_id),
    )


def _datetime_to_db(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _remove_empty_archive_directory(path: Path, root: Path) -> None:
    try:
        _assert_contained(path, root, strict=True)
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
    except (OSError, ValueError):
        pass
