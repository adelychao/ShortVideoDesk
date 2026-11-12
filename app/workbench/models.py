"""Typed records shared by the local workbench persistence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class SourceKind(str, Enum):
    URL = "url"
    FILE = "file"


class StepKind(str, Enum):
    DOWNLOAD = "download"
    IMPORT = "import"
    TRANSCRIBE = "transcribe"
    OCR = "ocr"
    COMMENTS = "comments"
    ANALYZE_COMMENTS = "analyze_comments"
    ANALYZE_STRUCTURE = "analyze_structure"
    KNOWLEDGE_CARDS = "knowledge_cards"
    GENERATE_SCRIPTS = "generate_scripts"
    INDEX = "index"
    WRITE_OBSIDIAN = "write_obsidian"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SourceRecord:
    kind: SourceKind
    value: str
    platform: str | None = None
    source_id: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            object.__setattr__(self, "kind", SourceKind(self.kind))
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("source value must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        value = sanitize_source_url(self.value) if self.kind is SourceKind.URL else self.value
        return {
            "kind": self.kind.value,
            "value": value,
            "platform": self.platform,
            "source_id": self.source_id,
            "title": self.title,
            "author": self.author,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceRecord:
        published_at = value.get("published_at")
        if isinstance(published_at, str):
            published_at = datetime.fromisoformat(published_at)
        else:
            published_at = None
        source_kind = SourceKind(value["kind"])
        source_value = str(value["value"])
        if source_kind is SourceKind.URL:
            source_value = sanitize_source_url(source_value)
        return cls(
            kind=source_kind,
            value=source_value,
            platform=value.get("platform"),
            source_id=value.get("source_id"),
            title=value.get("title"),
            author=value.get("author"),
            published_at=published_at,
        )


def sanitize_source_url(value: str) -> str:
    """Return a URL safe for persistence while retaining required numeric IDs."""
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("source URL is invalid")
    from multi_sources import redact_url

    try:
        redacted = redact_url(value.strip())
        parsed = urlsplit(redacted)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            raise ValueError("source URL must be HTTP or HTTPS")
        hostname = hostname.casefold().rstrip(".")
        port = parsed.port
        if port == (443 if scheme == "https" else 80):
            port = None
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{port}" if port is not None else hostname

        host = parsed.hostname.casefold().rstrip(".")
        if host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com"):
            allowed_query_names = {"p"}
        elif host == "douyin.com" or host.endswith(".douyin.com"):
            allowed_query_names = {"modal_id", "vid"}
        else:
            allowed_query_names = set()

        query_items = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256)
        grouped: dict[str, list[str]] = {}
        for key, item in query_items:
            normalized_key = key.casefold()
            if normalized_key in allowed_query_names:
                grouped.setdefault(normalized_key, []).append(item)
        valid_values: dict[str, list[str]] = {}
        for key, values in grouped.items():
            if key == "p":
                if len(values) != 1 or not re.fullmatch(r"[0-9]{1,10}", values[0]):
                    raise ValueError("Bilibili part query is invalid")
                part = int(values[0])
                if not 1 <= part <= 10000:
                    raise ValueError("Bilibili part query is invalid")
                valid_values[key] = [str(part)]
            else:
                if len(set(values)) != 1 or not all(
                    re.fullmatch(r"[0-9]{1,30}", item) for item in values
                ):
                    raise ValueError("Douyin item query is invalid")
                valid_values[key] = values
        safe_query: list[tuple[str, str]] = []
        emitted: set[str] = set()
        for key, item in query_items:
            normalized_key = key.casefold()
            if normalized_key not in valid_values:
                continue
            if normalized_key == "p":
                if normalized_key in emitted:
                    continue
                safe_query.append((normalized_key, valid_values[normalized_key][0]))
                emitted.add(normalized_key)
            else:
                safe_query.append((normalized_key, item))
        sanitized = urlunsplit((scheme, netloc, parsed.path, urlencode(safe_query), ""))
        if len(sanitized) > 8192:
            raise ValueError("source URL is too long")
        return sanitized
    except (UnicodeError, ValueError) as exc:
        raise ValueError("source URL is invalid") from exc


@dataclass(frozen=True)
class AssetRecord:
    id: str
    source_kind: SourceKind
    platform: str | None
    source_id: str | None
    title: str | None
    author: str | None
    published_at: datetime | None
    archived_at: datetime | None
    archive_path: str | None
    sha256: str | None
    created_at: datetime
    description: str | None = None


@dataclass(frozen=True)
class StepRecord:
    id: str
    kind: StepKind
    ordinal: int
    status: str
    depends_on: list[str] = field(default_factory=list)
    input_artifact_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    progress: float | None = None
    message: str | None = None
    error_code: str | None = None
    retry_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StepKind):
            object.__setattr__(self, "kind", StepKind(self.kind))
        if self.ordinal < 0:
            raise ValueError("step ordinal must be non-negative")


@dataclass(frozen=True)
class TaskRecord:
    id: str
    asset_id: str
    source: SourceRecord
    status: str
    created_at: datetime
    updated_at: datetime
    steps: list[StepRecord] = field(default_factory=list)


@dataclass(frozen=True)
class StepUpdate:
    status: str | None = None
    progress: float | None = None
    message: str | None = None
    error_code: str | None = None
    retry_count: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    task_id: str
    step_id: str
    kind: str
    path: str
    sha256: str
    version: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportedMedia:
    asset: AssetRecord
    artifact: ArtifactRecord
    media_type: Literal["video", "audio"]


@dataclass(frozen=True)
class MediaRecoverySummary:
    recovered: int
    already_registered: int
    pending: int
    invalid: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommentRecord:
    id: str
    platform_comment_id: str
    parent_id: str | None
    author: str | None
    text: str
    like_count: int | None
    created_at: datetime | None
    collected_at: datetime
    raw_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommentPage:
    comments: list[CommentRecord]
    next_cursor: str | None
    stop_reason: str | None = None
    has_more: bool | None = None
    missing_fields: dict[str, int] = field(default_factory=dict)
    reply_warning: str | None = None


@dataclass(frozen=True)
class StepConsent:
    id: str
    task_id: str
    step_kind: StepKind
    provider_id: str
    model_id: str
    artifact_ids: list[str]
    authorized: bool
    authorized_at: datetime


@dataclass(frozen=True)
class TaskSummary:
    task: TaskRecord
    asset: AssetRecord
    completed_step_kinds: list[StepKind]
    error_count: int


@dataclass(frozen=True)
class TaskDetail:
    task: TaskRecord
    asset: AssetRecord
    artifacts: list[ArtifactRecord]
    consents: list[StepConsent]


@dataclass(frozen=True)
class MigrationSummary:
    imported: int
    skipped: int
    invalid: int
    warnings: list[str] = field(default_factory=list)
