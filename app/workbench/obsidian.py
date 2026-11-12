"""Bounded Markdown access for explicitly selected Obsidian files."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import uuid


_MAX_REFERENCE_BYTES = 1_000_000
_MAX_REFERENCE_TOTAL_BYTES = 5_000_000
_MAX_NOTE_BYTES = 5_000_000


@dataclass(frozen=True)
class ReferenceNote:
    relative_path: str
    title: str
    content: str
    sha256: str


@dataclass(frozen=True)
class WritePreview:
    target_path: str
    exists: bool
    current_sha256: str | None
    proposed_sha256: str
    conflict: bool


@dataclass(frozen=True)
class WriteResult:
    written: bool
    path: str | None
    conflict_path: str | None
    reason: str | None


def list_reference_notes(root: Path, relative_paths: list[str]) -> list[ReferenceNote]:
    """Read only named Markdown files; never enumerate a Vault."""
    if len(relative_paths) > 50:
        raise ValueError("一次最多读取 50 篇指定参考文档。")
    resolved_root = _root(root)
    result: list[ReferenceNote] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in relative_paths:
        target, relative = _resolve_target(resolved_root, raw, require_exists=True)
        if relative.casefold() in seen:
            continue
        seen.add(relative.casefold())
        if target.is_symlink() or not target.is_file():
            raise ValueError("参考文档必须是普通 Markdown 文件。")
        if target.stat().st_size > _MAX_REFERENCE_BYTES:
            raise ValueError("参考文档超过 1 MB，未读取。")
        data = target.read_bytes()
        total_bytes += len(data)
        if total_bytes > _MAX_REFERENCE_TOTAL_BYTES:
            raise ValueError("本次参考文档总量超过 5 MB，未返回内容。")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("参考文档必须使用 UTF-8 编码。") from exc
        result.append(ReferenceNote(
            relative_path=relative,
            title=target.stem,
            content=content,
            sha256=hashlib.sha256(data).hexdigest(),
        ))
    return result


def preview_vault_write(root: Path, relative_path: str, content: str) -> WritePreview:
    resolved_root = _root(root)
    target, relative = _resolve_target(resolved_root, relative_path, require_exists=False)
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("笔记内容无效。")
    data = content.encode("utf-8")
    if len(data) > _MAX_NOTE_BYTES:
        raise ValueError("笔记超过 5 MB。")
    exists = target.exists()
    current_hash: str | None = None
    if exists:
        if target.is_symlink() or not target.is_file():
            raise ValueError("目标必须是普通 Markdown 文件。")
        current_hash = _hash_bytes(target.read_bytes())
    proposed_hash = _hash_bytes(data)
    conflict = exists and current_hash != proposed_hash
    return WritePreview(relative, exists, current_hash, proposed_hash, conflict)


def conflict_path_candidate(root: Path, relative_path: str) -> str:
    resolved_root = _root(root)
    target, relative = _resolve_target(resolved_root, relative_path, require_exists=False)
    return _next_conflict_path(target, relative)


def write_vault_note(
    root: Path,
    relative_path: str,
    content: str,
    expected_sha256: str | None,
) -> WriteResult:
    """Create a new note atomically; existing notes are never overwritten."""
    preview = preview_vault_write(root, relative_path, content)
    resolved_root = _root(root)
    target, relative = _resolve_target(resolved_root, relative_path, require_exists=False)
    if preview.exists and preview.current_sha256 == preview.proposed_sha256:
        return WriteResult(True, relative, None, None)
    if preview.exists:
        return WriteResult(False, None, _next_conflict_path(target, relative), "目标内容已存在或发生变化，未覆盖。")
    if expected_sha256 is not None:
        return WriteResult(False, None, _next_conflict_path(target, relative), "预览后目标状态已变化，未写入。")

    # Re-check all path components immediately before exclusive publication.
    target, relative = _resolve_target(resolved_root, relative_path, require_exists=False)
    payload = content.encode("utf-8")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, payload)
        if os.name == "nt":
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink()
    except FileExistsError:
        return WriteResult(False, None, _next_conflict_path(target, relative), "预览后目标已创建，未覆盖。")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return WriteResult(True, relative, None, None)


def _root(root: Path) -> Path:
    candidate = Path(root)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("知识库目录不可用。")
    return candidate.resolve(strict=True)


def _resolve_target(root: Path, raw: str, *, require_exists: bool) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("请指定相对 Markdown 路径。")
    if "\\" in raw or raw.startswith(("/", "//")) or ":" in raw:
        raise ValueError("只接受根目录下的相对 Markdown 路径。")
    relative = PurePosixPath(raw.strip())
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("相对路径无效或包含路径穿越。")
    if relative.suffix.casefold() != ".md":
        raise ValueError("知识库只读取或写入 .md 文件。")
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("知识库路径不能经过符号链接。")
        if index < len(relative.parts) - 1 and current.exists() and not current.is_dir():
            raise ValueError("知识库目标路径中的上级部分不是文件夹。")
    try:
        resolved_parent = current.parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("知识库目标路径必须位于已配置目录内，且上级文件夹必须存在。") from exc
    if require_exists and (not current.exists() or current.resolve(strict=True).parent != resolved_parent):
        raise ValueError("指定的 Markdown 文件不存在。")
    if current.exists():
        resolved = current.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("知识库目标路径超出已配置目录。") from exc
        if resolved.parent != resolved_parent:
            raise ValueError("知识库目标路径发生变化。")
        current = resolved
    return current, relative.as_posix()


def _next_conflict_path(target: Path, relative: str) -> str:
    stem = target.stem
    suffix = target.suffix
    for number in range(2, 10000):
        candidate_name = f"{stem} (冲突 {number}){suffix}"
        candidate = target.with_name(candidate_name)
        if not candidate.exists() and not candidate.is_symlink():
            return f"{Path(relative).parent.as_posix()}/{candidate_name}" if "/" in relative else candidate_name
    raise ValueError("无法分配冲突副本路径。")


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
