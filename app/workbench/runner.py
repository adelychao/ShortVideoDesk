"""Bounded dependency-aware execution for persisted workbench tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import re
import sqlite3
import stat
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Protocol
import uuid

from multi_sources import classify_failure, parse_source

from .archive import (
    _assert_contained,
    _prepare_archive_root,
    _publish_staged_file,
    safe_title,
    archive_path,
    local_archive_time,
)
from .config import AppConfig
from .database import Database
from .database import PROJECT_ROOT
from .models import (
    AssetRecord,
    ArtifactRecord,
    SourceKind,
    SourceRecord,
    StepKind,
    StepRecord,
    StepStatus,
    StepUpdate,
    TaskRecord,
    TaskStatus,
    sanitize_source_url,
)
from .collection_sources import (
    CollectionMediaError,
    get_collection_media_source,
)
from .repository import TaskRepository, _asset_from_row, _artifact_from_row
from .script_styles import MAX_SELECTED_SCRIPT_STYLES
from .uploads import UploadError, UploadReceipt, delete_upload, get_upload


ProgressCallback = Callable[[str, float | None, str], None]
EventCallback = Callable[[dict[str, object]], None]


class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TaskCancelled(Exception):
    pass


class TaskStepSkipped(Exception):
    pass


class AwaitingAuthorization(Exception):
    pass


_DOWNLOAD_MANIFEST_PATTERN = re.compile(
    r"\.workbench-download-([0-9a-f]{32})\.manifest\.json$"
)
_HEX_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_SOURCE_PATTERN = re.compile(r"^workbench-upload:([0-9a-f]{32})$")
_LOCAL_MEDIA_SOURCE_PATTERN = re.compile(r"^workbench-local-media:([0-9a-f]{32})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ARCHIVE_SEQUENCE_PATTERN = re.compile(r"^[0-9]{4,}_.+$")
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_URL_TEXT_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s<>\]]+|\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|tv|io|me|co|top|xyz|cc|app|live)(?:/[^\s<>\]]*)?"
)


@dataclass(frozen=True)
class StepResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class StepContext:
    task: TaskRecord
    step: StepRecord
    repository: TaskRepository
    archive_root: Path
    cancel_event: threading.Event
    progress: ProgressCallback
    transient_source_url: str
    allow_first_frame: bool = False
    app_config: AppConfig | None = None
    media_artifact: ArtifactRecord | None = None
    skip_event: threading.Event | None = None
    ocr_resume_mode: str | None = None


class StepHandler(Protocol):
    def run(self, context: StepContext) -> StepResult: ...


@dataclass
class _QueueItem:
    task_id: str
    source_url: str
    allow_first_frame: bool
    callback: EventCallback | None
    selected_step_id: str | None = None
    ocr_resume_mode: str | None = None


def _new_task_steps(selected: list[StepKind]) -> list[StepRecord]:
    ids = {kind: uuid.uuid4().hex for kind in selected}
    source = StepKind.DOWNLOAD if StepKind.DOWNLOAD in ids else StepKind.IMPORT
    result = []
    for ordinal, kind in enumerate(selected):
        if kind is source:
            dependencies = []
        elif kind is StepKind.ANALYZE_COMMENTS:
            dependencies = [ids[StepKind.COMMENTS]]
        elif kind is StepKind.OCR and StepKind.TRANSCRIBE in ids:
            dependencies = [ids[source], ids[StepKind.TRANSCRIBE]]
        elif kind is StepKind.GENERATE_SCRIPTS:
            dependencies = [ids[StepKind.TRANSCRIBE]]
            if StepKind.OCR in ids:
                dependencies.append(ids[StepKind.OCR])
        elif kind in {StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS}:
            dependencies = [ids[StepKind.TRANSCRIBE]]
        else:
            dependencies = [ids[source]]
        result.append(StepRecord(id=ids[kind], kind=kind, ordinal=ordinal,
                                 status=StepStatus.QUEUED.value, depends_on=dependencies))
    return result


class _DownloadStepHandler:
    def __init__(self, runner: "TaskRunner") -> None:
        self.runner = runner

    def run(self, context: StepContext) -> StepResult:
        if context.cancel_event.is_set():
            raise TaskCancelled
        from .downloads import download_source
        from .platform_accounts import get_task_access_account, get_task_access_mode

        account_id, _account_name = get_task_access_account(
            context.repository.database, context.task.id
        )

        result = download_source(
            context.transient_source_url,
            context.archive_root,
            context.progress,
            allow_first_frame=context.allow_first_frame,
            task_id=context.task.id,
            access_mode=get_task_access_mode(context.repository.database, context.task.id),
            account_id=account_id,
            database=context.repository.database,
        )
        # Serialize the final cancellation check with publication/registration.
        # cancel() uses the same lock, so it either wins before publication or
        # observes a completed task and refuses to rewrite it as cancelled.
        with self.runner._lock:
            if context.cancel_event.is_set():
                raise TaskCancelled
            artifacts, public_result = self.runner._archive_download_result(
                context.task, context.step, result, context.archive_root
            )
        return StepResult(
            status="completed",
            artifact_ids=[artifact.id for artifact in artifacts],
            metadata={"result": public_result},
        )


class _ImportStepHandler:
    def __init__(self, runner: "TaskRunner") -> None:
        self.runner = runner

    def run(self, context: StepContext) -> StepResult:
        if context.cancel_event.is_set():
            raise TaskCancelled
        source_value = context.task.source.value
        upload_match = _UPLOAD_SOURCE_PATTERN.fullmatch(source_value)
        local_match = _LOCAL_MEDIA_SOURCE_PATTERN.fullmatch(source_value)
        try:
            if upload_match is not None:
                receipt: UploadReceipt = get_upload(upload_match.group(1), verify_hash=False)
            elif local_match is not None:
                # 素材采集本地媒体：直接使用原文件，不经过上传暂存副本。
                receipt = get_collection_media_source(self.runner.database, local_match.group(1), verify_hash=False)
            else:
                raise RunnerError("upload_missing", "本地导入来源已失效，请重新选择文件。", status_code=409)
        except (CollectionMediaError, UploadError) as exc:
            raise RunnerError("upload_missing", str(exc), status_code=409) from exc
        transient_path = Path(context.transient_source_url)
        if transient_path.resolve(strict=True) != receipt.path:
            raise RunnerError("upload_changed", "本地导入文件与任务不匹配。", status_code=409)

        def progress(percent: float, message: str) -> None:
            context.progress(context.step.id, percent, message)

        def cancel_check() -> None:
            if context.cancel_event.is_set():
                raise TaskCancelled

        from .steps.import_media import import_media

        imported = import_media(
            receipt.path,
            context.archive_root,
            context.task.id,
            confirm_over_limit=receipt.confirmed_over_limit,
            database=self.runner.database,
            progress_callback=progress,
            cancel_check=cancel_check,
            publication_guard=self.runner._lock,
            expected_sha256=receipt.sha256,
        )
        # Keep failed/interrupted uploads available for explicit retry. A
        # successful import has an independent, verified archive copy.
        # 本地媒体登记指向原文件，导入完成后不做任何删除。
        if upload_match is not None:
            delete_upload(receipt.upload_id)
        return StepResult(
            status="completed",
            artifact_ids=[imported.artifact.id],
            metadata={
                "result": {
                    "media_type": imported.media_type,
                    "sha256": imported.artifact.sha256,
                }
            },
        )


class _CommentStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.comments import collect_comments

        return collect_comments(context)


class _IndexStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.index_documents import index_documents

        return index_documents(context)


class _TranscribeStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .local_asr import get_task_options
        options = get_task_options(context.repository.database, context.task.id)
        if options is not None:
            from .steps.local_transcribe import transcribe_local
            return transcribe_local(context, options)
        from .steps.transcribe import transcribe_media

        return transcribe_media(context)


class _OcrStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .local_ocr import get_scan_profile, get_task_options
        options = get_task_options(context.repository.database, context.task.id)
        if options["mode"] == "local":
            from .steps.local_ocr import ocr_local
            options["scan_profile"] = get_scan_profile(context.repository.database, context.task.id)
            return ocr_local(context, options)
        from .steps.ocr import ocr_media

        return ocr_media(context)


class _AnalyzeCommentsStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.analyze_comments import analyze_collected_comments

        return analyze_collected_comments(context)


class _AnalyzeStructureStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.analyze_structure import analyze_structure_step

        return analyze_structure_step(context)


class _KnowledgeCardsStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.create_cards import create_cards_step

        return create_cards_step(context)


class _GenerateScriptsStepHandler:
    def run(self, context: StepContext) -> StepResult:
        from .steps.generate_scripts import generate_scripts_step

        return generate_scripts_step(context)


class TaskRunner:
    """Persisted tasks scheduled with bounded concurrent step execution."""

    def __init__(
        self,
        database: Database | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.database = database or Database()
        self.config = config or AppConfig(self.database)
        self.repository = TaskRepository(self.database)
        self._queue: queue.Queue[_QueueItem | None] = queue.Queue()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._step_executor: ThreadPoolExecutor | None = None
        self._transcribe_lock = threading.Lock()
        self._started = False
        self._queued_ids: set[str] = set()
        self._rescheduled_ids: set[str] = set()
        self._scheduling_tasks: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._step_skip_events: dict[tuple[str, str], threading.Event] = {}
        self._handlers: dict[StepKind, StepHandler] = {
            StepKind.DOWNLOAD: _DownloadStepHandler(self),
            StepKind.IMPORT: _ImportStepHandler(self),
            StepKind.COMMENTS: _CommentStepHandler(),
            StepKind.TRANSCRIBE: _TranscribeStepHandler(),
            StepKind.OCR: _OcrStepHandler(),
            StepKind.ANALYZE_COMMENTS: _AnalyzeCommentsStepHandler(),
            StepKind.ANALYZE_STRUCTURE: _AnalyzeStructureStepHandler(),
            StepKind.KNOWLEDGE_CARDS: _KnowledgeCardsStepHandler(),
            StepKind.GENERATE_SCRIPTS: _GenerateScriptsStepHandler(),
            StepKind.INDEX: _IndexStepHandler(),
        }

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.database.migrate()
            from .migration import import_legacy_jobs

            import_legacy_jobs(
                self.database,
                [
                    PROJECT_ROOT / "data" / "anonymous-jobs",
                    PROJECT_ROOT / "data" / "state",
                ],
            )
            self.recover_after_restart()
            self._step_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workbench-step")
            self._workers = [threading.Thread(target=self._work_loop, name=f"workbench-task-{i}", daemon=True) for i in range(2)]
            self._worker = self._workers[0]
            for worker in self._workers:
                worker.start()
            self._started = True

    def stop(self) -> None:
        """Stop accepting queued work during application shutdown."""
        with self._lock:
            if not self._started:
                return
            workers = self._workers
            executor = self._step_executor
            self._started = False
            for _ in workers:
                self._queue.put(None)
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=2)
        if executor is not None:
            executor.shutdown(wait=False)

    def require_archive_root(self) -> Path:
        root = self.config.archive_root
        if root is None:
            raise RunnerError(
                "archive_root_unconfigured",
                "请先在本机设置中选择归档目录，然后重新提交任务。",
                status_code=409,
            )
        return root

    def create_download_task(
        self,
        source_url: str,
        *,
        allow_first_frame: bool = False,
        step_kinds: list[StepKind] | None = None,
        comment_sort_order: str = "likes_desc",
    ) -> tuple[AssetRecord, TaskRecord]:
        """Create a task with only a sanitized source stored in SQLite."""
        self.start()
        self.require_archive_root()
        if not isinstance(source_url, str) or not source_url.strip() or len(source_url) > 4096:
            raise ValueError("source URL is invalid")
        parsed = parse_source(source_url)
        safe_url = sanitize_source_url(parsed.url)
        source = SourceRecord(
            kind=SourceKind.URL,
            value=safe_url,
            platform=parsed.platform,
            source_id=parsed.media_id,
        )
        selected = step_kinds or [StepKind.DOWNLOAD]
        allowed = {
            StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS,
            StepKind.TRANSCRIBE, StepKind.OCR, StepKind.ANALYZE_STRUCTURE,
            StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS, StepKind.INDEX,
        }
        if not selected or StepKind.DOWNLOAD not in selected or any(kind not in allowed for kind in selected) or len(set(selected)) != len(selected):
            raise ValueError("URL task steps are invalid")
        selected = [kind for kind in (
            StepKind.DOWNLOAD, StepKind.COMMENTS, StepKind.ANALYZE_COMMENTS,
            StepKind.TRANSCRIBE, StepKind.OCR, StepKind.ANALYZE_STRUCTURE,
            StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS, StepKind.INDEX,
        ) if kind in selected or kind is StepKind.INDEX]
        if StepKind.ANALYZE_COMMENTS in selected and StepKind.COMMENTS not in selected:
            raise ValueError("comment analysis requires comment collection")
        if any(kind in selected for kind in (StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS)) and StepKind.TRANSCRIBE not in selected:
            raise ValueError("text analysis requires transcription")
        if comment_sort_order not in {"likes_desc", "time_desc", "time_asc"}:
            raise ValueError("comment sort order is invalid")
        steps = _new_task_steps(selected)
        asset, task = self.repository.create_task(source, steps)
        if StepKind.COMMENTS in selected:
            with self.database.transaction(write=True) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO comment_collection_state (
                           task_id, asset_id, sort_order, status, updated_at
                       ) VALUES (?, ?, ?, 'queued', ?)""",
                    (task.id, asset.id, comment_sort_order, _utc_now_iso()),
                )
        return asset, task

    def create_import_task(
        self,
        upload_id: str | None = None,
        *,
        local_source_id: str | None = None,
        step_kinds: list[StepKind] | None = None,
    ) -> tuple[AssetRecord, TaskRecord, UploadReceipt]:
        """Create an import task from staged upload or registered local media."""
        self.start()
        self.require_archive_root()
        if (upload_id is None) == (local_source_id is None):
            raise ValueError("import task requires exactly one media source")
        try:
            if upload_id is not None:
                receipt = get_upload(upload_id, verify_hash=False)
                source_value = f"workbench-upload:{receipt.upload_id}"
            else:
                assert local_source_id is not None
                # 本地媒体不复制：任务来源只登记原文件路径。
                receipt = get_collection_media_source(self.database, local_source_id, verify_hash=False)
                source_value = f"workbench-local-media:{receipt.upload_id}"
        except (CollectionMediaError, UploadError) as exc:
            raise RunnerError("media_source_invalid", str(exc), status_code=409) from exc
        source = SourceRecord(
            kind=SourceKind.FILE,
            value=source_value,
            title=safe_title(Path(receipt.filename).stem, max_length=512),
        )
        selected = step_kinds or [StepKind.IMPORT]
        allowed = {
            StepKind.IMPORT, StepKind.TRANSCRIBE, StepKind.OCR,
            StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS,
            StepKind.GENERATE_SCRIPTS, StepKind.INDEX,
        }
        if not selected or StepKind.IMPORT not in selected or any(kind not in allowed for kind in selected) or len(set(selected)) != len(selected):
            raise ValueError("local import steps are invalid")
        if receipt.detected_media_type == "audio" and StepKind.OCR in selected:
            raise ValueError("audio-only media does not support OCR")
        selected = [kind for kind in (
            StepKind.IMPORT, StepKind.TRANSCRIBE, StepKind.OCR,
            StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS,
            StepKind.GENERATE_SCRIPTS, StepKind.INDEX,
        ) if kind in selected or kind is StepKind.INDEX]
        if any(kind in selected for kind in (StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS)) and StepKind.TRANSCRIBE not in selected:
            raise ValueError("text analysis requires transcription")
        steps = _new_task_steps(selected)
        asset, task = self.repository.create_task(source, steps)
        return asset, task, receipt

    def enqueue(
        self,
        task_id: str,
        *,
        transient_source_url: str | None = None,
        allow_first_frame: bool = False,
        callback: EventCallback | None = None,
    ) -> None:
        self.start()
        self.require_archive_root()
        with self._lock:
            task = self.repository.get_task(task_id)
            if task is None:
                raise KeyError("task not found")
            if task.status in {TaskStatus.CANCELLED.value, TaskStatus.COMPLETED.value}:
                raise RunnerError("task_not_enqueueable", "已取消或已完成的任务不能直接排队。")
            if task_id in self._queued_ids:
                return
            if task.status == TaskStatus.RUNNING.value:
                raise RunnerError("task_active", "任务仍在执行中，暂时不能重复排队。")
            pending_cancel = self._cancel_events.get(task_id)
            if pending_cancel is not None and pending_cancel.is_set():
                raise RunnerError("task_cancelled", "任务已取消；请使用重试操作重新启动。")

            ordered_steps = self._validate_dependency_graph(task)
            step = next((item for item in ordered_steps if item.status in {
                StepStatus.QUEUED.value,
                StepStatus.INTERRUPTED.value,
                StepStatus.FAILED.value,
                StepStatus.PARTIAL.value,
                StepStatus.CANCELLED.value,
            }), None)
            if step is None:
                raise RunnerError("step_not_retryable", "任务没有可执行的步骤。")
            if step.kind not in self._handlers:
                raise RunnerError("unsupported_step", "所选步骤尚未实现，未启动任务。", status_code=422)
            if any(
                next(dep for dep in task.steps if dep.id == dependency_id).status
                not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}
                for dependency_id in step.depends_on
            ):
                raise RunnerError("dependencies_incomplete", "前置步骤尚未完成。")
            source_text = self._transient_source_for_task(
                task,
                step,
                override=transient_source_url,
            )

            self._update_task_step(
                task_id,
                step.id,
                task_status=TaskStatus.QUEUED.value,
                step_status=StepStatus.QUEUED.value,
                progress=None,
                message="等待处理",
                error_code=None,
                finished_at=None,
            )
            self._queued_ids.add(task_id)
            self._cancel_events[task_id] = threading.Event()
            self._queue.put(_QueueItem(task_id, source_text, bool(allow_first_frame), callback))
        self._emit(callback, status="queued", message="等待处理")

    def enqueue_followup_structure(self, task_id: str, *, transcript_artifact_id: str, ocr_artifact_id: str | None, provider_id: str, model_id: str) -> str:
        self.start()
        root = self.require_archive_root()
        source_ids = [transcript_artifact_id] + ([ocr_artifact_id] if ocr_artifact_id else [])
        if len(set(source_ids)) != len(source_ids):
            raise RunnerError("text_artifact_invalid", "结构分析输入重复。", status_code=422)
        with self._lock:
            task = self.repository.get_task(task_id)
            if task is None:
                raise KeyError("task not found")
            if task_id in self._queued_ids or task.status in {"queued", "running", "cancelled"}:
                raise RunnerError("task_active", "任务正在执行或已取消，暂不能追加分析。")
            with self.database.transaction() as connection:
                rows = [connection.execute("SELECT * FROM artifacts WHERE id = ? AND task_id = ?", (item, task_id)).fetchone() for item in source_ids]
            if not rows[0] or rows[0]["kind"] not in {"transcript", "transcript_draft", "transcript_revision"} or (len(rows) == 2 and (not rows[1] or rows[1]["kind"] not in {"ocr_result", "ocr_revision"})):
                raise RunnerError("text_artifact_invalid", "所选转写或 OCR 不属于本次任务。", status_code=422)
            for row in rows:
                try:
                    path = Path(str(row["path"]))
                    if path.is_symlink():
                        raise ValueError("symlink")
                    safe = _assert_contained(path, root, strict=True)
                    if not safe.is_file() or safe.suffix.casefold() not in {".md", ".txt"} or safe.stat().st_size > 5_000_000 or _hash_file(safe)[0].casefold() != str(row["sha256"]).casefold():
                        raise ValueError("changed")
                except (OSError, RuntimeError, ValueError):
                    raise RunnerError("text_artifact_invalid", "所选文本不可用或已被外部修改，请刷新后重新选择。", status_code=409) from None
            from .model_profiles import task_model_settings
            settings = task_model_settings(self.config, task_id)
            if settings.get("model_provider") != provider_id or settings.get("model_id") != model_id:
                raise RunnerError("model_changed", "模型配置已变化，请刷新后重新确认。")
            step_id, now = uuid.uuid4().hex, _utc_now_iso()
            with self.database.transaction(write=True) as connection:
                current = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if current is None or current["status"] != task.status:
                    raise RunnerError("task_state_changed", "任务状态已变化，请刷新后重试。")
                for row in rows:
                    current_source = connection.execute("SELECT sha256, kind FROM artifacts WHERE id = ? AND task_id = ?", (row["id"], task_id)).fetchone()
                    if current_source is None or current_source["sha256"] != row["sha256"] or current_source["kind"] != row["kind"]:
                        raise RunnerError("text_artifact_invalid", "所选文本已变化，请刷新后重试。")
                ordinal = connection.execute("SELECT COALESCE(MAX(ordinal), -1) + 1 FROM task_steps WHERE task_id = ?", (task_id,)).fetchone()[0]
                connection.execute("""INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json, progress, message, error_code, retry_count, started_at, finished_at) VALUES (?, ?, ?, ?, 'queued', '[]', ?, '[]', NULL, ?, NULL, 0, NULL, NULL)""", (step_id, task_id, StepKind.ANALYZE_STRUCTURE.value, ordinal, json.dumps(source_ids, separators=(",", ":")), "等待按需分析"))
                connection.execute("""INSERT INTO consents (id, task_id, step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)""", (uuid.uuid4().hex, task_id, StepKind.ANALYZE_STRUCTURE.value, settings.get("model_provider"), settings.get("model_id"), json.dumps(source_ids, separators=(",", ":")), now))
                connection.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (TaskStatus.QUEUED.value, now, task_id))
            self._queued_ids.add(task_id)
            self._cancel_events[task_id] = threading.Event()
            self._queue.put(_QueueItem(task_id, "", False, None, step_id))
        return step_id

    def enqueue_followup_scripts(
        self,
        task_id: str,
        *,
        transcript_artifact_id: str,
        comment_analysis_artifact_id: str | None,
        comment_ids: list[str],
        style_snapshot: dict[str, object],
    ) -> str:
        """Append an explicitly authorized script-generation step to a finished task."""
        self.start()
        root = self.require_archive_root()
        if not transcript_artifact_id or len(set(comment_ids)) != len(comment_ids):
            raise RunnerError("script_selection_invalid", "稿件输入选择无效。", status_code=422)
        styles = style_snapshot.get("styles")
        if not isinstance(styles, list):
            raise RunnerError("script_style_invalid", "稿件风格选择无效。", status_code=422)
        style_ids = [
            str(item.get("id"))
            for item in styles
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if (
            not style_ids
            or len(style_ids) > MAX_SELECTED_SCRIPT_STYLES
            or len(style_ids) != len(styles)
            or len(set(style_ids)) != len(style_ids)
        ):
            raise RunnerError("script_style_invalid", "稿件风格选择无效。", status_code=422)

        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        if task.status not in {TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value}:
            raise RunnerError("task_not_terminal", "只有已结束的任务可以追加稿件生成。")
        if not task.steps or any(step.status not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value} for step in task.steps):
            raise RunnerError("task_steps_incomplete", "原任务仍有未完成步骤，暂不能追加稿件生成。")

        with self.database.transaction() as connection:
            source_row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
                (transcript_artifact_id, task_id),
            ).fetchone()
            if source_row is None or str(source_row["kind"]) not in {"transcript", "transcript_draft", "transcript_revision"}:
                raise RunnerError("script_source_invalid", "稿件来源必须是本次任务登记的语音转写或人工校对版本。", status_code=422)
            comment_row = None
            if comment_ids:
                if not comment_analysis_artifact_id:
                    raise RunnerError("script_comment_selection_invalid", "已选择评论，但没有对应的评论精选产物。", status_code=422)
                comment_row = connection.execute(
                    "SELECT * FROM artifacts WHERE id = ? AND task_id = ? AND kind = 'comment_analysis_json'",
                    (comment_analysis_artifact_id, task_id),
                ).fetchone()
                if comment_row is None:
                    raise RunnerError("script_comment_selection_invalid", "评论精选产物不属于本次任务。", status_code=422)
            media_row = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind IN ('video', 'media')
                   ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        if media_row is None:
            raise RunnerError("archive_missing", "本次任务没有可用的媒体归档目录。", status_code=409)

        try:
            media_path = _assert_contained(Path(str(media_row["path"])), root, strict=True)
            media_digest, _ = _hash_file(media_path)
            if not media_path.is_file() or media_digest.casefold() != str(media_row["sha256"]).casefold():
                raise ValueError("media changed")
            folder = _assert_contained(media_path.parent, root, strict=True)
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError("archive unavailable")
        except (OSError, RuntimeError, ValueError):
            raise RunnerError("archive_missing", "本次任务的媒体归档文件校验失败。", status_code=409) from None

        step_id = uuid.uuid4().hex
        artifact_id = uuid.uuid4().hex
        target = folder / f"script_input_selection-{artifact_id}.json"
        source_ids = [transcript_artifact_id]
        content = json.dumps({
            "schema_version": 1,
            "source_artifact_ids": source_ids,
            "comment_analysis_artifact_id": comment_analysis_artifact_id if comment_ids else None,
            "comment_ids": comment_ids,
            "style_snapshot": style_snapshot,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor: int | None = None
        try:
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _assert_contained(target, root, strict=True)
            now = _utc_now_iso()
            from .model_profiles import task_model_settings
            model_settings = task_model_settings(self.config, task_id)
            provider_id = str(model_settings.get("model_provider"))
            model_id = str(model_settings.get("model_id"))
            consent_ids = [*source_ids]
            if comment_ids and comment_analysis_artifact_id:
                consent_ids.append(comment_analysis_artifact_id)
            consent_ids.append(artifact_id)
            with self.database.transaction(write=True) as connection:
                current_task = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                current_source = connection.execute("SELECT kind, sha256 FROM artifacts WHERE id = ? AND task_id = ?", (transcript_artifact_id, task_id)).fetchone()
                current_comment = None
                if comment_ids and comment_analysis_artifact_id:
                    current_comment = connection.execute(
                        "SELECT kind, sha256 FROM artifacts WHERE id = ? AND task_id = ?",
                        (comment_analysis_artifact_id, task_id),
                    ).fetchone()
                if current_task is None or current_task["status"] not in {TaskStatus.COMPLETED.value, TaskStatus.PARTIAL.value}:
                    raise RunnerError("task_state_changed", "任务状态已变化，请刷新后重新选择稿件输入。")
                if current_source is None or current_source["sha256"] != source_row["sha256"] or current_source["kind"] not in {"transcript", "transcript_draft", "transcript_revision"}:
                    raise RunnerError("script_source_invalid", "稿件来源已变化，请刷新任务详情。")
                if comment_ids and (
                    current_comment is None
                    or comment_row is None
                    or current_comment["kind"] != "comment_analysis_json"
                    or current_comment["sha256"] != comment_row["sha256"]
                ):
                    raise RunnerError(
                        "script_comment_selection_invalid",
                        "\u8bc4\u8bba\u7cbe\u9009\u6765\u6e90\u5df2\u53d8\u5316\uff0c\u8bf7\u5237\u65b0\u540e\u91cd\u65b0\u9009\u62e9\u8bc4\u8bba\u3002",
                        status_code=422,
                    )
                step_rows = connection.execute(
                    "SELECT id, ordinal, status FROM task_steps WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if step_rows is None or step_rows["status"] not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}:
                    raise RunnerError("task_steps_incomplete", "原任务仍有未完成步骤，暂不能追加稿件生成。")
                ordinal_row = connection.execute("SELECT COALESCE(MAX(ordinal), -1) + 1 AS ordinal FROM task_steps WHERE task_id = ?", (task_id,)).fetchone()
                ordinal = int(ordinal_row["ordinal"])
                connection.execute(
                    """INSERT INTO task_steps (
                           id, task_id, kind, ordinal, status, depends_on_json,
                           input_artifact_ids_json, output_artifact_ids_json, progress,
                           message, error_code, retry_count, started_at, finished_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', NULL, ?, NULL, 0, NULL, NULL)""",
                    (step_id, task_id, StepKind.GENERATE_SCRIPTS.value, ordinal, StepStatus.QUEUED.value,
                     json.dumps([str(step_rows["id"])], separators=(",", ":")),
                     json.dumps(consent_ids, separators=(",", ":")), "等待用户确认的稿件输入"),
                )
                connection.execute(
                    """INSERT INTO artifacts (
                           id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                       ) VALUES (?, ?, ?, 'script_input_selection', ?, ?, 1, ?, ?)""",
                    (artifact_id, task_id, step_id, str(target), digest,
                     json.dumps({"media_type": "application/json", "source_artifact_ids": source_ids, "comment_ids": comment_ids, "style_ids": style_ids, "style_names": [str(item.get("name", "")) for item in styles if isinstance(item, dict)], "reference_paths": [str(item.get("relative_path", "")) for item in style_snapshot.get("reference_notes", []) if isinstance(item, dict)] if isinstance(style_snapshot.get("reference_notes"), list) else []}, ensure_ascii=False, separators=(",", ":")), now),
                )
                connection.execute(
                    """INSERT INTO consents (
                           id, task_id, step_kind, provider_id, model_id, artifact_ids_json,
                           authorized, authorized_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                    (uuid.uuid4().hex, task_id, StepKind.GENERATE_SCRIPTS.value, provider_id, model_id,
                     json.dumps(consent_ids, separators=(",", ":")), now),
                )
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.INTERRUPTED.value, now, task_id),
                )
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if target.is_file() and not target.is_symlink():
                    target.unlink()
            except OSError:
                pass
            raise
        self.enqueue(task_id)
        return step_id

    def retry_step(
        self,
        task_id: str,
        step_id: str,
        *,
        source_url: str | None = None,
        callback: EventCallback | None = None,
        ocr_action: str | None = None,
        transcription_consent: tuple[str, str] | None = None,
    ) -> None:
        self.start()
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        self._validate_dependency_graph(task)
        step = next((item for item in task.steps if item.id == step_id), None)
        if step is None:
            raise KeyError("step not found")
        if step.kind not in self._handlers:
            raise RunnerError("unsupported_step", "所选步骤尚未实现，未重试。", status_code=422)
        retryable = {
            StepStatus.FAILED.value,
            StepStatus.INTERRUPTED.value,
            StepStatus.BLOCKED.value,
            StepStatus.CANCELLED.value,
            StepStatus.PARTIAL.value,
            StepStatus.AWAITING_AUTHORIZATION.value,
        }
        if step.status not in retryable:
            raise RunnerError("step_not_retryable", "只有失败、中断、阻塞或取消的步骤可以重试。")
        self.require_archive_root()
        source_text = self._transient_source_for_task(task, step, override=source_url)
        with self._lock:
            latest = self.repository.get_task(task_id)
            if latest is None:
                raise KeyError("task not found")
            latest_step = next((item for item in latest.steps if item.id == step_id), None)
            if latest_step is None or latest_step.status not in retryable:
                raise RunnerError("step_not_retryable", "步骤状态已变化，请刷新任务后重试。")
            if task_id in self._queued_ids or latest.status in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
            }:
                if latest_step.kind is StepKind.COMMENTS and task_id in self._queued_ids:
                    self._retry_active_comment_step(task_id, step_id, source_text, callback)
                    return
                raise RunnerError("task_active", "任务仍在队列或执行中，暂时不能重试。")
            from .local_ocr import get_task_options as get_ocr_options
            ocr_options = get_ocr_options(self.database, task_id) if latest_step.kind is StepKind.OCR else None
            ocr_resume_mode = None
            if ocr_options is not None and ocr_options["mode"] == "local":
                ocr_resume_mode = ocr_action or "continue"
                if ocr_resume_mode not in {"continue", "restart"}:
                    raise RunnerError("invalid_ocr_action", "本地 OCR 操作无效。", status_code=422)
            elif ocr_action is not None:
                raise RunnerError("invalid_ocr_action", "继续或重新开始仅适用于本地 OCR。", status_code=422)
            if ocr_options is not None and ocr_options["mode"] == "cloud":
                from .model_profiles import task_model_settings
                ocr_settings = task_model_settings(self.config, task_id)
            else:
                ocr_settings = None
            with self.database.transaction(write=True) as connection:
                if ocr_resume_mode == "restart":
                    connection.execute(
                        "DELETE FROM local_ocr_checkpoints WHERE task_id = ? AND step_id = ?",
                        (task_id, step_id),
                    )
                if ocr_settings is not None:
                    consent = connection.execute(
                        "SELECT id FROM consents WHERE task_id = ? AND step_kind = 'ocr' ORDER BY authorized_at DESC, rowid DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if consent is None:
                        provider_id, model_id = ocr_settings.get("model_provider"), ocr_settings.get("model_id")
                        if not isinstance(provider_id, str) or not provider_id or not isinstance(model_id, str) or not model_id:
                            raise RunnerError("model_not_configured", "请先配置 OCR 使用的云端服务商和模型。")
                        connection.execute(
                            """INSERT INTO consents (id, task_id, step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at)
                               VALUES (?, ?, 'ocr', ?, ?, '[]', 1, ?)""",
                            (uuid.uuid4().hex, task_id, provider_id, model_id, _utc_now_iso()),
                        )
                connection.execute(
                    """UPDATE task_steps SET retry_count = retry_count + 1,
                           status = 'queued', progress = NULL, message = ?, error_code = NULL,
                           started_at = NULL, finished_at = NULL
                       WHERE task_id = ? AND id = ?""",
                    ("等待重试", task_id, step_id),
                )
                if latest_step.kind is StepKind.TRANSCRIBE:
                    connection.execute(
                        """UPDATE task_steps SET status = 'queued', progress = NULL,
                               message = ?, error_code = NULL, started_at = NULL, finished_at = NULL
                           WHERE task_id = ? AND ordinal > ? AND status = 'cancelled'""",
                        ("等待语音转写重试完成后继续", task_id, latest_step.ordinal),
                    )
                if latest_step.kind is StepKind.TRANSCRIBE and transcription_consent is not None:
                    provider_id, model_id = transcription_consent
                    prior_consent = connection.execute(
                        "SELECT id FROM consents WHERE task_id = ? AND step_kind = 'transcribe' "
                        "ORDER BY authorized_at DESC, rowid DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    now = _utc_now_iso()
                    if prior_consent is None:
                        connection.execute(
                            """INSERT INTO consents (
                                   id, task_id, step_kind, provider_id, model_id,
                                   artifact_ids_json, authorized, authorized_at
                               ) VALUES (?, ?, 'transcribe', ?, ?, '[]', 1, ?)""",
                            (uuid.uuid4().hex, task_id, provider_id, model_id, now),
                        )
                    else:
                        connection.execute(
                            """UPDATE consents SET provider_id = ?, model_id = ?,
                                   artifact_ids_json = '[]', authorized = 1, authorized_at = ?
                               WHERE id = ? AND task_id = ? AND step_kind = 'transcribe'""",
                            (provider_id, model_id, now, str(prior_consent["id"]), task_id),
                        )
                if latest_step.kind is StepKind.COMMENTS:
                    consent = connection.execute(
                        """SELECT artifact_ids_json, authorized FROM consents
                           WHERE task_id = ? AND step_kind = 'analyze_comments'
                           ORDER BY authorized_at DESC, rowid DESC LIMIT 1""", (task_id,)
                    ).fetchone()
                    try:
                        scope = json.loads(consent["artifact_ids_json"]) if consent else None
                    except (TypeError, json.JSONDecodeError):
                        scope = None
                    if consent is not None and bool(consent["authorized"]) and scope == []:
                        connection.execute(
                            """UPDATE task_steps SET status = 'queued', progress = NULL,
                                   message = ?, error_code = NULL, started_at = NULL, finished_at = NULL
                               WHERE task_id = ? AND kind = 'analyze_comments'
                                 AND error_code = 'comments_analysis_stale'
                                 AND NOT EXISTS (SELECT 1 FROM model_runs WHERE model_runs.step_id = task_steps.id)""",
                            ("等待评论采集后继续筛选", task_id),
                        )
                if latest_step.kind is not StepKind.INDEX:
                    connection.execute(
                        """UPDATE task_steps SET status = 'queued', progress = NULL,
                               message = ?, error_code = NULL, started_at = NULL, finished_at = NULL
                           WHERE task_id = ? AND kind = 'index' AND status IN ('completed', 'partial')""",
                        ("等待更新索引", task_id),
                    )
                connection.execute(
                    "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ?",
                    (_utc_now_iso(), task_id),
                )
            self._queued_ids.add(task_id)
            self._cancel_events[task_id] = threading.Event()
            self._queue.put(_QueueItem(task_id, source_text, False, callback, ocr_resume_mode=ocr_resume_mode))
        self._emit(callback, status="queued", message="等待重试")

    def _retry_active_comment_step(
        self, task_id: str, step_id: str, source_text: str, callback: EventCallback | None
    ) -> None:
        """Queue a comment retry into the current scheduler without stopping peer steps."""
        latest = self.repository.get_task(task_id)
        if latest is None:
            raise KeyError("task not found")
        step = next((item for item in latest.steps if item.id == step_id), None)
        retryable = {
            StepStatus.FAILED.value, StepStatus.INTERRUPTED.value, StepStatus.BLOCKED.value,
            StepStatus.CANCELLED.value, StepStatus.PARTIAL.value,
        }
        if step is None or step.kind is not StepKind.COMMENTS or step.status not in retryable:
            raise RunnerError("step_not_retryable", "评论步骤状态已变化，请刷新后重试。")
        if self._cancel_events.get(task_id, threading.Event()).is_set():
            raise RunnerError("task_active", "任务正在取消，请稍后重试。")
        scheduler_active = task_id in self._scheduling_tasks
        now = _utc_now_iso()
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE task_steps SET retry_count = retry_count + 1, status = 'queued',
                       progress = NULL, message = ?, error_code = NULL, started_at = NULL, finished_at = NULL
                   WHERE task_id = ? AND id = ? AND status IN ('failed','interrupted','blocked','cancelled','partial')""",
                ("用户已请求重试评论；等待可用工作线程", task_id, step_id),
            )
            consent = connection.execute(
                """SELECT artifact_ids_json, authorized FROM consents
                   WHERE task_id = ? AND step_kind = 'analyze_comments'
                   ORDER BY authorized_at DESC, rowid DESC LIMIT 1""", (task_id,)
            ).fetchone()
            try:
                scope = json.loads(consent["artifact_ids_json"]) if consent else None
            except (TypeError, json.JSONDecodeError):
                scope = None
            if consent is not None and bool(consent["authorized"]) and scope == []:
                connection.execute(
                    """UPDATE task_steps SET status = 'queued', progress = NULL, message = ?,
                           error_code = NULL, started_at = NULL, finished_at = NULL
                       WHERE task_id = ? AND kind = 'analyze_comments'
                         AND error_code = 'comments_analysis_stale'
                         AND NOT EXISTS (SELECT 1 FROM model_runs WHERE model_runs.step_id = task_steps.id)""",
                    ("等待评论采集后继续筛选", task_id),
                )
            connection.execute(
                """UPDATE task_steps SET status = 'queued', progress = NULL,
                       message = ?, error_code = NULL, started_at = NULL, finished_at = NULL
                   WHERE task_id = ? AND kind = 'index' AND status IN ('completed', 'partial')""",
                ("等待更新索引", task_id),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING.value if scheduler_active else TaskStatus.QUEUED.value, now, task_id),
            )
        if not scheduler_active:
            self._rescheduled_ids.add(task_id)
            self._cancel_events[task_id] = threading.Event()
            self._queue.put(_QueueItem(task_id, source_text, False, callback, step_id))
        self._emit(
            callback,
            status=TaskStatus.RUNNING.value if scheduler_active else TaskStatus.QUEUED.value,
            message="评论重试已排队，其他步骤继续运行" if scheduler_active else "等待评论重试",
        )

    def _transient_source_for_task(
        self,
        task: TaskRecord,
        step: StepRecord,
        *,
        override: str | None,
    ) -> str:
        if step.kind in {
            StepKind.INDEX, StepKind.TRANSCRIBE, StepKind.OCR,
            StepKind.ANALYZE_COMMENTS, StepKind.ANALYZE_STRUCTURE,
            StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS,
        }:
            return ""
        if task.source.kind is SourceKind.FILE:
            if step.kind is not StepKind.IMPORT or override is not None:
                raise RunnerError("upload_source_mismatch", "本地导入任务来源无效。", status_code=422)
            upload_match = _UPLOAD_SOURCE_PATTERN.fullmatch(task.source.value)
            local_match = _LOCAL_MEDIA_SOURCE_PATTERN.fullmatch(task.source.value)
            if upload_match is not None:
                try:
                    receipt = get_upload(upload_match.group(1), verify_hash=False)
                except (OSError, UploadError) as exc:
                    raise RunnerError("upload_missing", str(exc), status_code=409) from exc
            elif local_match is not None:
                try:
                    receipt = get_collection_media_source(self.database, local_match.group(1), verify_hash=False)
                except (OSError, CollectionMediaError) as exc:
                    raise RunnerError("upload_missing", str(exc), status_code=409) from exc
            else:
                raise RunnerError("upload_missing", "本地导入来源已失效，请重新选择文件。", status_code=409)
            return str(receipt.path)

        if step.kind is StepKind.COMMENTS:
            if task.source.platform not in {"douyin", "bilibili"}:
                raise RunnerError("comments_source_id_missing", "评论采集需要抖音或 B 站公开作品链接。", status_code=422)
            with self.database.transaction() as connection:
                asset = connection.execute(
                    "SELECT platform, source_id FROM assets WHERE id = ?", (task.asset_id,)
                ).fetchone()
            asset_platform = str(asset["platform"] or "").casefold() if asset is not None else ""
            asset_source_id = str(asset["source_id"] or "").strip() if asset is not None else ""
            if asset_platform != task.source.platform or not asset_source_id:
                raise RunnerError("comments_source_id_missing", "下载步骤尚未保存可验证的作品 ID，无法重试评论采集。", status_code=422)
            if task.source.source_id and task.source.source_id != asset_source_id:
                raise RunnerError("comments_source_mismatch", "任务来源与归档作品 ID 不一致，已停止评论采集。", status_code=422)
            if override is not None:
                try:
                    parsed = parse_source(override)
                except (TypeError, ValueError) as exc:
                    raise RunnerError("source_reentry_required", "请重新提交原作品链接以继续评论采集。") from exc
                if (
                    task.source.platform != parsed.platform
                    or (parsed.media_id is not None and asset_source_id != parsed.media_id)
                ):
                    raise RunnerError("source_identity_mismatch", "重试链接的作品身份与原任务不一致。", status_code=422)
            return task.source.value
        if step.kind is not StepKind.DOWNLOAD:
            raise RunnerError("source_step_mismatch", "公开链接任务步骤无效。", status_code=422)
        source_text = override if override is not None else task.source.value
        try:
            parsed = parse_source(source_text)
        except (TypeError, ValueError) as exc:
            raise RunnerError(
                "source_reentry_required",
                "保存的脱敏链接不足以重新下载，请在重试请求中重新提交分享链接。",
            ) from exc
        if task.source.platform and parsed.platform != task.source.platform:
            raise RunnerError("source_identity_mismatch", "重试链接的平台与原任务不一致。", status_code=422)
        if task.source.source_id and parsed.media_id and task.source.source_id != parsed.media_id:
            raise RunnerError("source_identity_mismatch", "重试链接的作品 ID 与原任务不一致。", status_code=422)
        return source_text

    def skip_step(self, task_id: str, step_id: str) -> None:
        """Skip a queued step, or cooperatively stop a running OCR step."""
        self.start()
        with self._lock:
            task = self.repository.get_task(task_id)
            if task is None:
                raise KeyError("task not found")
            if task.status not in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.AWAITING_AUTHORIZATION.value,
            }:
                raise RunnerError("task_not_skippable", "当前任务状态不能跳过步骤。")
            by_id = {step.id: step for step in task.steps}
            target = by_id.get(step_id)
            if target is None:
                raise KeyError("step not found")
            queued_target = target.status == StepStatus.QUEUED.value
            running_ocr = target.status == StepStatus.RUNNING.value and target.kind is StepKind.OCR
            if not queued_target and not running_ocr:
                raise RunnerError("step_not_skippable", "只能跳过排队步骤或正在识别的字幕 OCR 步骤。")

            skip_event = None
            if running_ocr:
                skip_event = self._step_skip_events.get((task_id, step_id))
                if skip_event is None:
                    raise RunnerError("step_not_skippable", "OCR 步骤正在结束，请刷新任务后重试。")
                if skip_event.is_set():
                    raise RunnerError("step_skip_requested", "正在停止并跳过 OCR 步骤。")

            self._validate_dependency_graph(task)
            children: dict[str, list[str]] = {step.id: [] for step in task.steps}
            for step in task.steps:
                for dependency_id in step.depends_on:
                    children[dependency_id].append(step.id)
            descendants: set[str] = set()
            frontier = list(children.get(step_id, []))
            while frontier:
                child_id = frontier.pop()
                if child_id in descendants:
                    continue
                descendants.add(child_id)
                frontier.extend(children.get(child_id, []))

            already_started = {
                StepStatus.RUNNING.value,
                StepStatus.COMPLETED.value,
                StepStatus.PARTIAL.value,
                StepStatus.AWAITING_AUTHORIZATION.value,
            }
            if any(by_id[item].status in already_started for item in descendants):
                raise RunnerError("dependent_step_started", "依赖此步骤的后续步骤已开始，无法跳过。")

            queued_descendants = [
                item for item in descendants if by_id[item].status == StepStatus.QUEUED.value
            ]
            now = _utc_now_iso()
            with self.database.transaction(write=True) as connection:
                if queued_target:
                    updated = connection.execute(
                        """UPDATE task_steps SET status = 'skipped', progress = NULL,
                               message = '用户手动跳过', error_code = NULL,
                               started_at = NULL, finished_at = ?
                           WHERE task_id = ? AND id = ? AND status = 'queued'""",
                        (now, task_id, step_id),
                    )
                else:
                    updated = connection.execute(
                        """UPDATE task_steps SET message = '正在停止并跳过 OCR 步骤',
                               error_code = 'step_skip_requested'
                           WHERE task_id = ? AND id = ? AND status = 'running'""",
                        (task_id, step_id),
                    )
                if updated.rowcount != 1:
                    raise RunnerError("step_not_skippable", "步骤状态已变化，请刷新任务后重试。")
                for dependent_id in queued_descendants:
                    updated = connection.execute(
                        """UPDATE task_steps SET status = 'skipped', progress = NULL,
                               message = '依赖步骤已跳过', error_code = NULL,
                               started_at = NULL, finished_at = ?
                           WHERE task_id = ? AND id = ? AND status = 'queued'""",
                        (now, task_id, dependent_id),
                    )
                    if updated.rowcount != 1:
                        raise RunnerError("step_state_changed", "后续步骤状态已变化，请刷新任务后重试。")
                statuses = [
                    str(row["status"])
                    for row in connection.execute(
                        "SELECT status FROM task_steps WHERE task_id = ?", (task_id,)
                    ).fetchall()
                ]
                successful = {
                    StepStatus.COMPLETED.value,
                    StepStatus.PARTIAL.value,
                    StepStatus.SKIPPED.value,
                }
                if queued_target and statuses and all(status in successful for status in statuses):
                    task_status = (
                        TaskStatus.PARTIAL.value
                        if StepStatus.PARTIAL.value in statuses
                        else TaskStatus.COMPLETED.value
                    )
                    connection.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (task_status, now, task_id),
                    )
                else:
                    connection.execute(
                        "UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id)
                    )
            if skip_event is not None:
                skip_event.set()

    def _finish_step_skip(self, task_id: str, step_id: str) -> None:
        with self._lock:
            now = _utc_now_iso()
            with self.database.transaction(write=True) as connection:
                task = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if task is None or task["status"] == TaskStatus.CANCELLED.value:
                    return
                updated = connection.execute(
                    """UPDATE task_steps SET status = 'skipped', progress = NULL,
                           message = '用户手动跳过', error_code = NULL, finished_at = ?
                       WHERE task_id = ? AND id = ? AND status = 'running'
                         AND error_code = 'step_skip_requested'""",
                    (now, task_id, step_id),
                )
                if updated.rowcount != 1:
                    return
                statuses = [
                    str(row["status"])
                    for row in connection.execute(
                        "SELECT status FROM task_steps WHERE task_id = ?", (task_id,)
                    ).fetchall()
                ]
                successful = {
                    StepStatus.COMPLETED.value,
                    StepStatus.PARTIAL.value,
                    StepStatus.SKIPPED.value,
                }
                if statuses and all(status in successful for status in statuses):
                    task_status = (
                        TaskStatus.PARTIAL.value
                        if StepStatus.PARTIAL.value in statuses
                        else TaskStatus.COMPLETED.value
                    )
                    connection.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (task_status, now, task_id),
                    )
                else:
                    connection.execute(
                        "UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id)
                    )

    def cancel(self, task_id: str, *, callback: EventCallback | None = None) -> None:
        self.start()
        with self._lock:
            task = self.repository.get_task(task_id)
            if task is None:
                raise KeyError("task not found")
            if task.status not in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.INTERRUPTED.value,
                TaskStatus.AWAITING_AUTHORIZATION.value,
                TaskStatus.FAILED.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.PARTIAL.value,
            }:
                raise RunnerError("task_not_cancellable", "当前任务状态不能取消。")
            scheduled = task_id in self._queued_ids or task_id in self._scheduling_tasks
            if scheduled:
                self._cancel_events.setdefault(task_id, threading.Event()).set()
            with self.database.transaction(write=True) as connection:
                now = _utc_now_iso()
                connection.execute(
                    """UPDATE task_steps SET status = 'cancelled', message = ?, error_code = 'cancelled', finished_at = ?
                       WHERE task_id = ? AND status IN ('queued', 'running', 'interrupted', 'awaiting_authorization')""",
                    ("任务已取消", now, task_id),
                )
                next_status = TaskStatus.CANCELLED.value if scheduled or task.status in {
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.INTERRUPTED.value,
                    TaskStatus.AWAITING_AUTHORIZATION.value,
                } else task.status
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, task_id),
                )
        self._emit(callback, status=next_status, message="任务已取消" if next_status == TaskStatus.CANCELLED.value else "已清理任务中待执行的步骤")

    def is_task_active(self, task_id: str) -> bool:
        """Return whether this task still has a queued scheduler item or live step coordinator."""
        with self._lock:
            return task_id in self._queued_ids or task_id in self._scheduling_tasks

    def recover_after_restart(self) -> None:
        """Mark lost in-memory work interrupted and reconcile import manifests."""
        self.database.migrate()
        root = self.config.archive_root
        if root is not None:
            from .steps.import_media import recover_media_publications

            recover_media_publications(root, database=self.database)
            self.recover_download_publications(root)

        now = _utc_now_iso()
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE model_runs SET status = 'interrupted', error_code = 'process_restarted',
                       finished_at = ? WHERE status = 'running'""",
                (now,),
            )
            rows = connection.execute(
                "SELECT id, source_json FROM tasks WHERE status IN ('running', 'queued')"
            ).fetchall()
            for row in rows:
                task_id = str(row["id"])
                try:
                    source_data = json.loads(row["source_json"])
                except (TypeError, json.JSONDecodeError):
                    source_data = {}
                is_local_media = isinstance(source_data, dict) and source_data.get("kind") == SourceKind.FILE.value
                restart_message = (
                    "程序重启后本地导入已中断；上传暂存文件保留，请显式继续。"
                    if is_local_media
                    else "程序重启后，内存中的分享链接已丢失；请显式重试。"
                )
                connection.execute(
                    """UPDATE task_steps SET status = 'interrupted',
                           message = ?, error_code = 'process_restarted',
                           finished_at = ?
                       WHERE task_id = ? AND status IN ('running', 'queued')""",
                    (restart_message, now, task_id),
                )
                connection.execute(
                    "UPDATE tasks SET status = 'interrupted', updated_at = ? WHERE id = ?",
                    (now, task_id),
                )
            completed_file_tasks = connection.execute(
                "SELECT source_json FROM tasks WHERE status = 'completed'"
            ).fetchall()
        for row in completed_file_tasks:
            try:
                source_data = json.loads(row["source_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(source_data, dict) or source_data.get("kind") != SourceKind.FILE.value:
                continue
            source_value = source_data.get("value")
            match = _UPLOAD_SOURCE_PATTERN.fullmatch(source_value) if isinstance(source_value, str) else None
            if match is not None:
                delete_upload(match.group(1))

    def recover_download_publications(self, archive_root: Path) -> int:
        """Reconcile fsynced download markers before queued work is interrupted."""
        root = _prepare_archive_root(archive_root)
        recovered = 0
        for marker_path in sorted(root.glob(".workbench-download-*.manifest.json")):
            if marker_path.is_symlink() or not marker_path.is_file():
                continue
            match = _DOWNLOAD_MANIFEST_PATTERN.fullmatch(marker_path.name)
            if match is None:
                continue
            try:
                manifest = _read_download_publication_manifest(marker_path, root)
                if manifest.get("id") != match.group(1):
                    continue
                if self._recover_download_manifest(root, marker_path, manifest):
                    recovered += 1
            except Exception:
                # Unmatched or corrupt markers remain in place for inspection.
                continue
        return recovered

    def _recover_download_manifest(
        self, root: Path, marker_path: Path, manifest: dict[str, object]
    ) -> bool:
        validated = _validate_download_manifest(manifest, root, marker_path)
        task_id = validated["task_id"]
        step_id = validated["step_id"]
        asset_id = validated["asset_id"]
        entries = validated["artifacts"]
        video_entry = next(entry for entry in entries if entry["kind"] == "video")
        task = self.repository.get_task(task_id)
        if task is None or task.legacy_path is not None:
            return False
        ordered = self._validate_dependency_graph(task)
        step = next((item for item in ordered if item.id == step_id), None)
        if step is None or step.kind is not StepKind.DOWNLOAD:
            return False
        if task.status == TaskStatus.CANCELLED.value:
            return False
        if any(
            next(dep for dep in task.steps if dep.id == dependency_id).status
            != StepStatus.COMPLETED.value
            for dependency_id in step.depends_on
        ):
            return False

        with self.database.transaction() as connection:
            asset_row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if asset_row is None or not _manifest_asset_matches_task(
                task, asset_row, str(video_entry["sha256"])
            ):
                return False
            for entry in entries:
                reuse_id = entry.get("reuse_artifact_id")
                if reuse_id is None:
                    continue
                source_artifact = connection.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (reuse_id,)
                ).fetchone()
                if source_artifact is None:
                    return False
                if (
                    str(source_artifact["kind"]) != str(entry["kind"])
                    or str(source_artifact["sha256"]).casefold() != str(entry["sha256"]).casefold()
                    or Path(str(source_artifact["path"])).resolve(strict=False)
                    != Path(entry["final_path"])
                ):
                    return False

        final_paths: dict[str, Path] = {}
        for entry in entries:
            final_path = Path(entry["final_path"])
            staged_path = entry["staged_path"]
            source_path = final_path if final_path.exists() else staged_path
            if source_path is None or not source_path.exists():
                return False
            _verify_manifest_file(source_path, root, entry)
            if entry["kind"] == "video":
                from .downloads import inspect_video

                video_metadata = entry["metadata"]
                duration = video_metadata.get("duration_seconds")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    return False
                inspection = inspect_video(
                    source_path,
                    float(duration),
                    require_audio=bool(video_metadata.get("has_audio")),
                )
                if (
                    not inspection.full_decode_verified
                    or inspection.sha256.casefold() != str(entry["sha256"]).casefold()
                    or inspection.has_audio != bool(video_metadata.get("has_audio"))
                    or inspection.width != video_metadata.get("width")
                    or inspection.height != video_metadata.get("height")
                ):
                    return False
            elif entry["kind"] == "cover":
                if _validated_cover(source_path, root) is None:
                    return False
            elif entry["kind"] == "metadata":
                _verify_safe_metadata_sidecar(source_path, video_entry["metadata"])
            elif entry["kind"] == "note":
                _verify_safe_note_sidecar(source_path, video_entry["metadata"])
            else:
                return False

        # All planned files have passed verification before the first recovery
        # rename. Existing final files are never replaced.
        for entry in entries:
            final_path = Path(entry["final_path"])
            staged_path = entry["staged_path"]
            if final_path.exists():
                continue
            if staged_path is None or not staged_path.exists():
                return False
            try:
                _publish_staged_file(staged_path, final_path, root)
                _fsync_directory(final_path.parent)
                _fsync_directory(staged_path.parent)
            except FileExistsError:
                if not final_path.exists():
                    return False
                _verify_manifest_file(final_path, root, entry)
            _verify_manifest_file(final_path, root, entry)

        archive_time = validated["archived_at"]
        inserted_video = False
        with self.database.transaction(write=True) as connection:
            task_row = connection.execute(
                "SELECT asset_id, source_json, status, legacy_path FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            step_row = connection.execute(
                "SELECT * FROM task_steps WHERE task_id = ? AND id = ?",
                (task_id, step_id),
            ).fetchone()
            if task_row is None or step_row is None or task_row["legacy_path"] is not None:
                raise ValueError("download publication task context changed")
            if task_row["status"] == TaskStatus.CANCELLED.value:
                raise ValueError("download publication task was cancelled")
            current_task = self.repository.get_task(task_id)
            if current_task is None:
                raise ValueError("download publication task disappeared")
            asset_row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if asset_row is None or not _manifest_asset_matches_task(
                current_task, asset_row, str(video_entry["sha256"])
            ):
                raise ValueError("download publication asset identity changed")

            registered_ids: list[str] = []
            for entry in entries:
                final_path = Path(entry["final_path"])
                _verify_manifest_file(final_path, root, entry)
                artifact_id = str(entry["artifact_id"])
                artifact_row = connection.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if artifact_row is not None:
                    if not _manifest_artifact_row_matches(artifact_row, task_id, step_id, entry):
                        raise ValueError("download publication artifact ID conflicts")
                else:
                    path_conflict = connection.execute(
                        "SELECT id FROM artifacts WHERE path = ? AND id != ? LIMIT 1",
                        (str(final_path), artifact_id),
                    ).fetchone()
                    reuse_id = entry.get("reuse_artifact_id")
                    if path_conflict is not None and reuse_id is None:
                        raise ValueError("download publication path is already registered")
                    version_conflict = connection.execute(
                        """SELECT 1 FROM artifacts WHERE task_id = ? AND step_id = ?
                           AND kind = ? AND version = ? LIMIT 1""",
                        (task_id, step_id, entry["kind"], entry["version"]),
                    ).fetchone()
                    if version_conflict is not None:
                        raise ValueError("download publication artifact version conflicts")
                    connection.execute(
                        """INSERT INTO artifacts (
                               id, task_id, step_id, kind, path, sha256, version,
                               metadata_json, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            artifact_id,
                            task_id,
                            step_id,
                            entry["kind"],
                            str(final_path),
                            entry["sha256"],
                            entry["version"],
                            json.dumps(entry["metadata"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                            validated["archived_at"].isoformat(),
                        ),
                    )
                    if entry["kind"] == "video":
                        inserted_video = True
                registered_ids.append(artifact_id)

            if str(task_row["asset_id"]) != asset_id:
                connection.execute(
                    "UPDATE tasks SET asset_id = ?, updated_at = ? WHERE id = ?",
                    (asset_id, _utc_now_iso(), task_id),
                )
            if inserted_video:
                _update_asset_archive_pointer(
                    connection,
                    asset_id,
                    Path(video_entry["final_path"]),
                    str(video_entry["sha256"]),
                    archive_time,
                    str(video_entry["metadata"]["title"]),
                )
            self._complete_step_in_transaction(
                connection,
                task_id,
                step_id,
                str(step_row["output_artifact_ids_json"]),
                registered_ids,
            )

        staging_clean = True
        for entry in entries:
            staged_path = entry["staged_path"]
            if staged_path is not None:
                staging_clean = _remove_manifest_stage_file(
                    staged_path, root, task_id, entry
                ) and staging_clean
        for cleanup_entry in validated["cleanup_staging"]:
            staging_clean = _remove_manifest_stage_file(
                cleanup_entry["path"], root, task_id, cleanup_entry
            ) and staging_clean
        if staging_clean:
            _remove_download_manifest(marker_path, root, str(manifest["id"]))
        return True

    def _validate_dependency_graph(self, task: TaskRecord) -> list[StepRecord]:
        by_id = {step.id: step for step in task.steps}
        if len(by_id) != len(task.steps):
            raise RunnerError("invalid_dependency_graph", "任务步骤图无效。", status_code=422)
        indegree = {step.id: 0 for step in task.steps}
        children: dict[str, list[str]] = {step.id: [] for step in task.steps}
        for step in task.steps:
            if step.kind not in self._handlers:
                raise RunnerError("unsupported_step", "所选步骤尚未实现，未启动任务。", status_code=422)
            if len(set(step.depends_on)) != len(step.depends_on):
                raise RunnerError("invalid_dependency_graph", "任务步骤图无效。", status_code=422)
            for dependency_id in step.depends_on:
                dependency = by_id.get(dependency_id)
                if dependency is None or dependency.id == step.id or dependency.ordinal >= step.ordinal:
                    raise RunnerError("invalid_dependency_graph", "任务步骤依赖无效。", status_code=422)
                indegree[step.id] += 1
                children[dependency_id].append(step.id)
        ready = sorted(
            (step for step in task.steps if indegree[step.id] == 0),
            key=lambda item: (item.ordinal, item.id),
        )
        order: list[StepRecord] = []
        while ready:
            step = ready.pop(0)
            order.append(step)
            for child_id in children[step.id]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(by_id[child_id])
                    ready.sort(key=lambda item: (item.ordinal, item.id))
        if len(order) != len(task.steps):
            raise RunnerError("invalid_dependency_graph", "任务步骤图包含循环。", status_code=422)
        return order

    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._execute_item(item)
            except RunnerError as exc:
                self._set_terminal(item.task_id, TaskStatus.BLOCKED.value, StepStatus.BLOCKED.value, str(exc), exc.code)
                self._emit(item.callback, status="blocked", message=str(exc), error_code=exc.code)
            except Exception as exc:
                code, message = classify_failure(exc)
                self._set_terminal(item.task_id, TaskStatus.FAILED.value, StepStatus.FAILED.value, _safe_message(message), code)
                self._emit(item.callback, status="failed", message=_safe_message(message), error_code=code)
            finally:
                with self._lock:
                    self._scheduling_tasks.discard(item.task_id)
                    if item.task_id in self._rescheduled_ids:
                        self._rescheduled_ids.discard(item.task_id)
                    else:
                        self._queued_ids.discard(item.task_id)
                        self._cancel_events.pop(item.task_id, None)
                self._queue.task_done()

    def _execute_item(self, item: _QueueItem) -> None:
        executor = self._step_executor
        if executor is None:
            return
        with self._lock:
            self._scheduling_tasks.add(item.task_id)
        active = {}
        success = {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}
        finished = success | {StepStatus.BLOCKED.value, StepStatus.FAILED.value,
                              StepStatus.INTERRUPTED.value, StepStatus.CANCELLED.value,
                              StepStatus.SKIPPED.value}
        while True:
            task = self.repository.get_task(item.task_id)
            if task is None:
                return
            event = self._cancel_events[item.task_id]
            by_id = {step.id: step for step in self._validate_dependency_graph(task)}
            by_kind = {step.kind: step for step in task.steps}
            def can_still_progress(step: StepRecord) -> bool:
                if step.status in {StepStatus.RUNNING.value, StepStatus.AWAITING_AUTHORIZATION.value}:
                    return True
                if step.status != StepStatus.QUEUED.value:
                    return False
                return all(by_id[dependency].status in success or can_still_progress(by_id[dependency])
                           for dependency in step.depends_on)
            if not event.is_set():
                for step in task.steps:
                    if len(active) >= 2:
                        break
                    if (item.selected_step_id and step.id != item.selected_step_id) or step.status != StepStatus.QUEUED.value or step.id in active.values():
                        continue
                    if any(by_id[dependency].status not in success for dependency in step.depends_on):
                        continue
                    # zhw 2026-10-29 20:13:33: OCR 当前会生成合并稿，先等语音结果，避免覆盖或遗漏。
                    speech = by_kind.get(StepKind.TRANSCRIBE)
                    if step.kind is StepKind.OCR and speech is not None and speech.status not in finished:
                        continue
                    if not item.selected_step_id and step.kind in {StepKind.ANALYZE_STRUCTURE, StepKind.KNOWLEDGE_CARDS, StepKind.GENERATE_SCRIPTS}:
                        optional = (by_kind.get(StepKind.OCR), by_kind.get(StepKind.ANALYZE_COMMENTS))
                        if any(other is not None and can_still_progress(other) for other in optional):
                            continue
                    if step.kind is StepKind.INDEX and any(
                        other.id != step.id and can_still_progress(other) for other in task.steps
                    ):
                        continue
                    self._mark_task_only(item.task_id, TaskStatus.RUNNING.value)
                    future = executor.submit(self._execute_step, item, step.id)
                    active[future] = step.id
            if active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    active.pop(future)
                    future.result()
                continue
            task = self.repository.get_task(item.task_id)
            if task is None:
                return
            with self._lock:
                task = self.repository.get_task(item.task_id)
                if task is None:
                    return
                if event.is_set():
                    self._mark_task_only(item.task_id, TaskStatus.CANCELLED.value)
                    self._scheduling_tasks.discard(item.task_id)
                    return
                latest_by_id = {step.id: step for step in task.steps}
                if any(
                    step.kind is StepKind.COMMENTS
                    and step.status == StepStatus.QUEUED.value
                    and all(latest_by_id[dependency].status in success for dependency in step.depends_on)
                    for step in task.steps
                ):
                    continue
                statuses = {step.status for step in task.steps}
                if StepStatus.AWAITING_AUTHORIZATION.value in statuses:
                    status = TaskStatus.AWAITING_AUTHORIZATION.value
                elif statuses <= {StepStatus.COMPLETED.value, StepStatus.SKIPPED.value}:
                    status = TaskStatus.COMPLETED.value
                elif statuses & {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}:
                    status = TaskStatus.PARTIAL.value
                elif StepStatus.BLOCKED.value in statuses:
                    status = TaskStatus.BLOCKED.value
                else:
                    status = TaskStatus.FAILED.value
                self._mark_task_only(item.task_id, status)
                self._scheduling_tasks.discard(item.task_id)
            self._emit(item.callback, status=status, message="任务已部分完成" if status == 'partial' else "任务处理已结束")
            return

    def _execute_step(self, item: _QueueItem, step_id: str) -> None:
        task = self.repository.get_task(item.task_id)
        if task is None:
            self._emit(item.callback, status="failed", message="任务不存在", error_code="task_not_found")
            return
        skip_event: threading.Event | None = None
        try:
            with self._lock:
                current_task = self.repository.get_task(item.task_id)
                if current_task is None:
                    raise RunnerError("task_not_found", "任务不存在。", status_code=404)
                if current_task.status == TaskStatus.COMPLETED.value:
                    self._emit(item.callback, status="completed", message="任务已完成")
                    return
                event = self._cancel_events.setdefault(item.task_id, threading.Event())
                if event.is_set() or current_task.status == TaskStatus.CANCELLED.value:
                    raise TaskCancelled
                ordered_steps = self._validate_dependency_graph(current_task)
                pending = next((step for step in ordered_steps if step.id == step_id), None)
                if pending is None:
                    self._mark_task_only(item.task_id, TaskStatus.COMPLETED.value)
                    self._emit(item.callback, status="completed", message="任务已完成")
                    return
                if pending.status == StepStatus.CANCELLED.value:
                    raise TaskCancelled
                if pending.status == StepStatus.SKIPPED.value:
                    return
                if pending.status != StepStatus.QUEUED.value:
                    if pending.error_code == "comments_analysis_stale":
                        raise RunnerError("comments_analysis_stale", pending.message or "评论已更新，请重新授权并运行评论分析。")
                    raise RunnerError("step_not_runnable", "步骤状态已变化，无法开始执行。")
                if any(
                    next(dep for dep in current_task.steps if dep.id == dependency_id).status
                    not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}
                    for dependency_id in pending.depends_on
                ):
                    raise RunnerError("dependencies_incomplete", "前置步骤尚未完成。")
                skip_event = self._step_skip_events.setdefault(
                    (item.task_id, step_id), threading.Event()
                )
                root = _prepare_archive_root(self.require_archive_root())
                initial_message = {
                    StepKind.IMPORT: "正在准备本地媒体导入",
                    StepKind.DOWNLOAD: "正在开始下载",
                    StepKind.COMMENTS: "正在准备匿名评论采集",
                    StepKind.TRANSCRIBE: "正在准备语音转写",
                    StepKind.OCR: "正在准备视频画面识别",
                    StepKind.ANALYZE_COMMENTS: "正在整理本次采集到的评论并请求云端分析",
                    StepKind.ANALYZE_STRUCTURE: "正在分析转写文本结构",
                    StepKind.KNOWLEDGE_CARDS: "正在生成待审核知识卡",
                    StepKind.GENERATE_SCRIPTS: "正在生成多风格口播稿草稿",
                    StepKind.INDEX: "正在准备更新本地索引",
                }.get(pending.kind, "正在准备处理步骤")
                self._update_task_step(
                    item.task_id,
                    pending.id,
                    task_status=TaskStatus.RUNNING.value,
                    step_status=StepStatus.RUNNING.value,
                    progress=0,
                    message=initial_message,
                    error_code=None,
                    started_at=_utc_now_iso(),
                    finished_at=None,
                )
                self._emit(item.callback, status="running", message=initial_message, progress=0)
                task = current_task

            def update(step_id: str, percent: float | None, message: str) -> None:
                if event.is_set():
                    raise TaskCancelled
                if skip_event is not None and skip_event.is_set():
                    raise TaskStepSkipped
                if not self._persist_progress(item.task_id, step_id, percent, message):
                    return
                self._emit(
                    item.callback,
                    status="running",
                    message=_safe_message(message),
                    progress=percent,
                    phase={
                        StepKind.IMPORT: "导入", StepKind.DOWNLOAD: "下载", StepKind.COMMENTS: "评论采集",
                        StepKind.TRANSCRIBE: "语音转写", StepKind.OCR: "关键帧 OCR",
                        StepKind.ANALYZE_COMMENTS: "评论分析", StepKind.ANALYZE_STRUCTURE: "结构分析",
                        StepKind.KNOWLEDGE_CARDS: "知识卡片", StepKind.GENERATE_SCRIPTS: "口播稿生成",
                        StepKind.INDEX: "索引",
                    }.get(pending.kind, "处理"),
                )

            context = StepContext(
                task=task,
                step=pending,
                repository=self.repository,
                archive_root=root,
                cancel_event=event,
                progress=update,
                transient_source_url=item.source_url,
                allow_first_frame=item.allow_first_frame,
                app_config=self.config,
                skip_event=skip_event,
                ocr_resume_mode=item.ocr_resume_mode,
            )
            if pending.kind is StepKind.ANALYZE_COMMENTS and any(
                step.kind is StepKind.COMMENTS and step.status == StepStatus.PARTIAL.value
                for step in task.steps
            ):
                from .steps.analyze_comments import _comment_note_artifact

                note = _comment_note_artifact(context)
                with self.database.transaction() as connection:
                    consent = connection.execute(
                        """SELECT artifact_ids_json, authorized FROM consents WHERE task_id = ? AND step_kind = 'analyze_comments'
                           AND authorized = 1 ORDER BY authorized_at DESC, rowid DESC LIMIT 1""",
                        (item.task_id,),
                    ).fetchone()
                    previous_runs = connection.execute(
                        "SELECT COUNT(*) FROM model_runs WHERE task_id = ? AND step_id = ?",
                        (item.task_id, pending.id),
                    ).fetchone()[0]
                try:
                    approved = json.loads(consent["artifact_ids_json"]) if consent else None
                except (TypeError, json.JSONDecodeError):
                    approved = None
                first_authorized_call = consent is not None and bool(consent["authorized"]) and approved == [] and previous_runs == 0
                if approved != [note.id] and not first_authorized_call:
                    raise RunnerError("comments_analysis_stale", "评论资料已更新或授权不可用，请确认是否使用已保存的评论继续分析。")
            if pending.kind is StepKind.TRANSCRIBE:
                with self._transcribe_lock:
                    step_result = self._handlers[pending.kind].run(context)
            else:
                step_result = self._handlers[pending.kind].run(context)
            if event.is_set():
                raise TaskCancelled
            if skip_event is not None and skip_event.is_set():
                raise TaskStepSkipped
            self._complete_task_step(
                item.task_id,
                pending.id,
                step_result.artifact_ids,
                status=step_result.status,
                message=(
                    "评论已采集，部分结果已保存"
                    if pending.kind is StepKind.COMMENTS and step_result.status == StepStatus.PARTIAL.value
                    else "公开评论采集完成"
                    if pending.kind is StepKind.COMMENTS
                    else "本地资料索引已更新"
                    if pending.kind is StepKind.INDEX
                    else "语音转写完成"
                    if pending.kind is StepKind.TRANSCRIBE
                    else "画面文字识别完成，结果待人工校对"
                    if pending.kind is StepKind.OCR
                    else "本地媒体导入完成"
                    if pending.kind is StepKind.IMPORT
                    else "评论分析完成；精选结果可逐条审核"
                    if pending.kind is StepKind.ANALYZE_COMMENTS
                    else "结构分析已保存，部分引用待核对" if pending.kind is StepKind.ANALYZE_STRUCTURE and step_result.status == StepStatus.PARTIAL.value
                    else "内容结构分析完成"
                    if pending.kind is StepKind.ANALYZE_STRUCTURE
                    else "知识卡已生成并等待人工审核"
                    if pending.kind is StepKind.KNOWLEDGE_CARDS
                    else "三种风格口播稿已生成草稿"
                    if pending.kind is StepKind.GENERATE_SCRIPTS
                    else "本地资料索引已更新"
                    if pending.kind is StepKind.INDEX
                    else "下载与本地媒体校验完成"
                ),
            )
            public_result = step_result.metadata.get("result")
            self._emit(item.callback, status="running", message="步骤已完成", result=public_result if isinstance(public_result, dict) else None)
        except AwaitingAuthorization as exc:
            self._set_waiting_authorization(item.task_id, pending.id, str(exc))
            self._emit(
                item.callback,
                status=TaskStatus.RUNNING.value,
                message=str(exc),
            )
        except TaskCancelled:
            self._set_terminal(item.task_id, TaskStatus.CANCELLED.value, StepStatus.CANCELLED.value,
                               "任务已取消", "cancelled", step_id=step_id)
            self._emit(item.callback, status="cancelled", message="任务已取消", error_code="cancelled")
        except TaskStepSkipped:
            self._finish_step_skip(item.task_id, step_id)
            self._emit(item.callback, status=TaskStatus.RUNNING.value, message="字幕 OCR 步骤已跳过")
        except RunnerError as exc:
            self._set_terminal(item.task_id, TaskStatus.BLOCKED.value, StepStatus.BLOCKED.value,
                               str(exc), exc.code, step_id=step_id)
            self._emit(item.callback, status="running", message=str(exc), error_code=exc.code)
        except Exception as exc:
            failed_step = next((step for step in task.steps if step.id == step_id), None)
            if failed_step is not None and failed_step.kind is StepKind.INDEX:
                code, message = "index_failed", "本地索引更新失败，请检查归档目录和数据库状态后重试。"
            else:
                code, message = classify_failure(exc)
            message = _safe_message(message)
            self._set_terminal(item.task_id, TaskStatus.FAILED.value, StepStatus.FAILED.value,
                               message, code, step_id=step_id)
            self._emit(item.callback, status="running", message=message, error_code=code)
        finally:
            if skip_event is not None:
                with self._lock:
                    self._step_skip_events.pop((item.task_id, step_id), None)

    def _archive_download_result(self, task: TaskRecord, step: StepRecord, result, root: Path):
        video_path = Path(result.files.get("video", ""))
        _assert_task_staging_file(video_path, root, task.id)
        if video_path.is_symlink() or not video_path.is_file():
            raise ValueError("download video is not a regular task staging file")
        digest, size_bytes = _hash_file(video_path)
        if digest != result.inspection.sha256 or not result.inspection.full_decode_verified:
            raise ValueError("download changed after media verification")
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        platform = task.source.platform or _text(metadata.get("platform")) or "unknown"
        source_id = _text(metadata.get("id")) or _text(metadata.get("media_id")) or task.source.source_id
        if not source_id:
            raise ValueError("download result has no verified work ID")
        if task.source.source_id and task.source.source_id != source_id:
            raise ValueError("download result work ID differs from the requested work")
        title_value = _safe_display_text(
            _text(metadata.get("title")) or task.source.title or "untitled", max_length=512
        )
        title = safe_title(title_value or "untitled", max_length=512)
        raw_description = _text(metadata.get("description"))
        description = _safe_display_text(raw_description, max_length=5000) if raw_description else ""
        description = description or None
        extension = video_path.suffix.casefold()
        if extension not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise ValueError("download video extension is unsupported")
        staged_cover = _validated_cover(result.files.get("cover"), root)
        author = _safe_display_text(_text(metadata.get("author")) or "", max_length=256) or None
        published_at = _safe_published_at(metadata.get("published_at"))
        safe_metadata: dict[str, object] = {
            "media_type": "video",
            "platform": platform,
            "source_id": source_id,
            "title": title,
            "author": author,
            "published_at": published_at,
            "sha256": digest,
            "size_bytes": size_bytes,
            "duration_seconds": float(result.inspection.duration_seconds),
            "width": result.inspection.width,
            "height": result.inspection.height,
            "has_audio": bool(result.inspection.has_audio),
            "full_decode_verified": True,
            "cover_source": "missing",
        }
        if description:
            safe_metadata["description"] = description
        metadata_stage = video_path.parent / "metadata.json"
        note_stage = video_path.parent / "metadata.md"
        metadata_digest = ""
        metadata_size = 0
        note_digest = ""
        note_size = 0
        artifacts: list[ArtifactRecord] = []
        published_video_path: Path | None = None
        published_cover_path: Path | None = None
        reused_video: ArtifactRecord | None = None
        reused_cover: ArtifactRecord | None = None
        cover_source = "missing"
        published_by_attempt: list[tuple[Path, str]] = []
        created_archive_directories: list[Path] = []
        publication_id = uuid.uuid4().hex
        marker_path = root / f".workbench-download-{publication_id}.manifest.json"
        folder: Path | None = None

        with _publication_transaction(
            self.database,
            root,
            published_by_attempt,
            created_archive_directories,
            marker_path=marker_path,
            publication_id=publication_id,
        ) as connection:
            task_row = connection.execute(
                "SELECT id, asset_id FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            if task_row is None:
                raise KeyError("task no longer exists")
            step_row = connection.execute(
                "SELECT output_artifact_ids_json FROM task_steps "
                "WHERE id = ? AND task_id = ? AND kind = 'download'",
                (step.id, task.id),
            ).fetchone()
            if step_row is None:
                raise KeyError("download step no longer exists")
            candidates = connection.execute(
                """SELECT DISTINCT assets.* FROM assets
                   WHERE (lower(platform) = ? AND source_id = ?) OR lower(sha256) = ?
                   ORDER BY CASE WHEN archive_path IS NULL THEN 1 ELSE 0 END,
                            archived_at ASC, created_at ASC, id ASC""",
                (platform.casefold(), source_id, digest),
            ).fetchall()
            asset_id = str(task_row["asset_id"])
            for candidate in candidates:
                artifact_rows = connection.execute(
                    """SELECT DISTINCT artifacts.* FROM artifacts
                       JOIN tasks ON tasks.id = artifacts.task_id
                       WHERE tasks.asset_id = ? AND artifacts.kind = 'video'
                         AND lower(artifacts.sha256) = ?
                       ORDER BY artifacts.created_at DESC, artifacts.version DESC""",
                    (candidate["id"], digest),
                ).fetchall()
                for artifact_row in artifact_rows:
                    candidate_artifact = _artifact_from_row(artifact_row)
                    try:
                        _verified_archive_file(
                            Path(candidate_artifact.path), root, candidate_artifact.sha256
                        )
                    except (OSError, ValueError):
                        continue
                    reused_video = candidate_artifact
                    asset_id = str(candidate["id"])
                    break
                if reused_video is not None:
                    break

            if reused_video is not None:
                published_video_path = Path(reused_video.path).resolve(strict=True)
                cover_match = self._find_verified_cover(connection, asset_id, root)
                if cover_match is not None:
                    reused_cover, published_cover_path, cover_source = cover_match
            else:
                current_asset = connection.execute(
                    "SELECT * FROM assets WHERE id = ?", (task_row["asset_id"],)
                ).fetchone()
                if current_asset is None:
                    raise ValueError("task asset no longer exists")
                canonical = next(
                    (row for row in candidates
                     if isinstance(row["platform"], str)
                     and row["platform"].casefold() == platform.casefold()
                     and row["source_id"] == source_id),
                    None,
                )
                if canonical is not None:
                    asset_id = str(canonical["id"])
                else:
                    connection.execute(
                        """UPDATE assets SET platform = ?, source_id = ?,
                               title = COALESCE(NULLIF(title, ''), ?)
                           WHERE id = ?""",
                        (platform, source_id, title, asset_id),
                    )

            safe_description_title = safe_title(description, max_length=512) if description else None
            connection.execute(
                """UPDATE assets SET
                       title = CASE WHEN ? IS NOT NULL AND title = ? THEN ?
                                    ELSE COALESCE(NULLIF(title, ''), ?) END,
                       description = COALESCE(NULLIF(description, ''), ?)
                   WHERE id = ?""",
                (description, safe_description_title, title, title, description, asset_id),
            )

            asset_row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            # 作者：zhw；日期：2026-10-24 19:46:06。短链任务入库时可能没有作品 ID；这里的 source_id 已由下载适配器与解析器交叉核验。
            if asset_row is None or not _manifest_asset_matches_task(
                task, asset_row, digest, verified_identity=(platform, source_id)
            ):
                raise ValueError("download asset identity is not safely associated with the task")
            self._set_task_asset(connection, task.id, asset_id)

            archive_time = datetime.now(timezone.utc)
            folder = archive_path(root, archive_time, title, platform, source_id)
            created_archive_directories.append(folder)
            _fsync_directory(folder.parent)
            _fsync_directory(root)
            if reused_video is None:
                published_video_path = folder / f"video{extension}"
            if reused_cover is None and staged_cover is not None:
                cover_source = _cover_source(metadata.get("cover_source"))
                published_cover_path = folder / f"cover{staged_cover[0].suffix.casefold()}"
            safe_metadata["cover_source"] = cover_source

            _write_task_stage_payload(
                metadata_stage,
                json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
                root,
                task.id,
            )
            _write_task_stage_payload(
                note_stage,
                _download_summary_markdown(safe_metadata).encode("utf-8"),
                root,
                task.id,
            )
            metadata_digest, metadata_size = _hash_file(metadata_stage)
            note_digest, note_size = _hash_file(note_stage)

            plans: list[dict[str, object]] = []

            def add_plan(
                kind: str,
                final_path: Path,
                staged_path: Path | None,
                sha256: str,
                file_size: int,
                artifact_metadata: dict[str, object],
                reused_artifact: ArtifactRecord | None = None,
            ) -> None:
                next_row = connection.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                       WHERE task_id = ? AND step_id = ? AND kind = ?""",
                    (task.id, step.id, kind),
                ).fetchone()
                plans.append({
                    "artifact_id": uuid.uuid4().hex,
                    "version": int(next_row["version"]),
                    "kind": kind,
                    "extension": final_path.suffix.casefold(),
                    "staged_path": staged_path,
                    "final_path": final_path,
                    "sha256": sha256,
                    "size_bytes": file_size,
                    "metadata": artifact_metadata,
                    "reuse_artifact_id": reused_artifact.id if reused_artifact else None,
                })

            video_metadata = dict(safe_metadata)
            if reused_video is not None:
                video_metadata["reused"] = True
            add_plan(
                "video", published_video_path,
                None if reused_video is not None else video_path,
                digest, size_bytes, video_metadata, reused_video,
            )
            if published_cover_path is not None:
                if reused_cover is not None:
                    cover_digest, cover_size = _hash_file(published_cover_path)
                    add_plan(
                        "cover", published_cover_path, None, cover_digest, cover_size,
                        {"cover_source": cover_source, "reused": True}, reused_cover,
                    )
                elif staged_cover is not None:
                    add_plan(
                        "cover", published_cover_path, staged_cover[0], staged_cover[1],
                        staged_cover[2],
                        {"cover_source": cover_source, "size_bytes": staged_cover[2]},
                    )
            add_plan(
                "metadata", folder / "metadata.json", metadata_stage,
                metadata_digest, metadata_size,
                {"media_type": "safe_metadata_json", "size_bytes": metadata_size},
            )
            add_plan(
                "note", folder / "metadata.md", note_stage,
                note_digest, note_size,
                {"media_type": "safe_summary_markdown", "size_bytes": note_size},
            )

            cleanup_staging: list[dict[str, object]] = []
            if reused_video is not None:
                cleanup_staging.append({
                    "path": video_path, "kind": "video", "sha256": digest,
                    "size_bytes": size_bytes,
                })
            if staged_cover is not None and reused_cover is not None:
                cleanup_staging.append({
                    "path": staged_cover[0], "kind": "cover", "sha256": staged_cover[1],
                    "size_bytes": staged_cover[2],
                })
            planned_stage_paths = {
                Path(plan["staged_path"]).resolve(strict=False)
                for plan in plans if isinstance(plan["staged_path"], Path)
            }
            for sidecar_kind in ("metadata", "note"):
                raw_sidecar = result.files.get(sidecar_kind)
                if not isinstance(raw_sidecar, (str, Path)):
                    continue
                sidecar_path = Path(raw_sidecar)
                if sidecar_path.resolve(strict=False) in planned_stage_paths:
                    continue
                try:
                    checked_sidecar = _assert_task_staging_file(sidecar_path, root, task.id)
                    sidecar_digest, sidecar_size = _hash_file(checked_sidecar)
                except (OSError, ValueError):
                    continue
                cleanup_staging.append({
                    "path": checked_sidecar, "kind": "sidecar", "sha256": sidecar_digest,
                    "size_bytes": sidecar_size,
                })

            manifest = _download_publication_manifest(
                publication_id, task.id, step.id, asset_id, archive_time,
                folder, root, plans,
                cleanup_staging=cleanup_staging,
            )
            _write_download_publication_manifest(marker_path, root, manifest)
            for plan in plans:
                staged_path = plan["staged_path"]
                final_path = Path(plan["final_path"])
                if isinstance(staged_path, Path):
                    _fsync_regular_file(staged_path)
                    _fsync_directory(staged_path.parent)
                    final_path = _publish_staged_file(staged_path, final_path, root)
                    _fsync_directory(final_path.parent)
                    _fsync_directory(staged_path.parent)
                    published_by_attempt.append((final_path, str(plan["sha256"])))
                    plan["final_path"] = final_path
                final_digest, final_size = _hash_file(final_path)
                if final_digest.casefold() != str(plan["sha256"]).casefold() or final_size != int(plan["size_bytes"]):
                    raise ValueError("published artifact changed before registration")

            for plan in plans:
                artifacts.append(self._insert_artifact(
                    connection,
                    task.id,
                    step.id,
                    str(plan["kind"]),
                    Path(plan["final_path"]),
                    str(plan["sha256"]),
                    dict(plan["metadata"]),
                    artifact_id=str(plan["artifact_id"]),
                    version=int(plan["version"]),
                ))
            _update_asset_archive_pointer(
                connection, asset_id, Path(published_video_path), digest, archive_time, title
            )
            self._complete_step_in_transaction(
                connection,
                task.id,
                step.id,
                step_row["output_artifact_ids_json"],
                [artifact.id for artifact in artifacts],
            )

        staging_clean = True
        for cleanup_entry in cleanup_staging:
            staging_clean = _remove_manifest_stage_file(
                Path(cleanup_entry["path"]),
                root,
                task.id,
                cleanup_entry,
            ) and staging_clean
        if staging_clean:
            _remove_download_manifest(marker_path, root, publication_id)

        public_metadata = dict(safe_metadata)
        files = {
            "video": f"/api/jobs/{task.id}/files/video",
            "metadata": f"/api/jobs/{task.id}/files/metadata",
            "note": f"/api/jobs/{task.id}/files/note",
        }
        if published_cover_path is not None:
            files["cover"] = f"/api/jobs/{task.id}/files/cover"
        public_result = {
            "metadata": public_metadata,
            "output_dir": str(folder),
            "files": files,
            "inspection": {
                "duration_seconds": result.inspection.duration_seconds,
                "width": result.inspection.width,
                "height": result.inspection.height,
                "sha256": digest,
                "full_decode_verified": True,
                "has_audio": result.inspection.has_audio,
                "size_bytes": size_bytes,
            },
        }
        return artifacts, public_result

    def _find_verified_cover(
        self, connection: sqlite3.Connection, asset_id: str, root: Path
    ) -> tuple[ArtifactRecord, Path, str] | None:
        rows = connection.execute(
            """SELECT artifacts.* FROM artifacts
               JOIN tasks ON tasks.id = artifacts.task_id
               WHERE tasks.asset_id = ? AND artifacts.kind = 'cover'
               ORDER BY artifacts.created_at DESC, artifacts.version DESC""",
            (asset_id,),
        ).fetchall()
        for row in rows:
            artifact = _artifact_from_row(row)
            verified_cover = _validated_cover(Path(artifact.path), root)
            if verified_cover is None or verified_cover[1].casefold() != artifact.sha256.casefold():
                continue
            path = verified_cover[0].resolve(strict=True)
            source = artifact.metadata.get("cover_source")
            return artifact, path, _cover_source(source)
        return None

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        step_id: str,
        kind: str,
        path: Path,
        digest: str,
        metadata: dict[str, object],
        *,
        artifact_id: str | None = None,
        version: int | None = None,
    ) -> ArtifactRecord:
        if artifact_id is None or version is None:
            version_row = connection.execute(
                """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                   WHERE task_id = ? AND step_id = ? AND kind = ?""",
                (task_id, step_id, kind),
            ).fetchone()
            artifact_id = artifact_id or uuid.uuid4().hex
            version = version or int(version_row["version"])
        connection.execute(
            """INSERT INTO artifacts (
                   id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                task_id,
                step_id,
                kind,
                str(path),
                digest,
                version,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                _utc_now_iso(),
            ),
        )
        row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        assert row is not None
        return _artifact_from_row(row)

    def _set_task_asset(self, connection: sqlite3.Connection, task_id: str, asset_id: str) -> None:
        connection.execute(
            "UPDATE tasks SET asset_id = ?, updated_at = ? WHERE id = ?",
            (asset_id, _utc_now_iso(), task_id),
        )

    def _complete_step_in_transaction(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        step_id: str,
        prior_ids_json: str,
        artifact_ids: list[str],
    ) -> None:
        try:
            prior_ids = json.loads(prior_ids_json)
        except (TypeError, json.JSONDecodeError):
            prior_ids = []
        if not isinstance(prior_ids, list):
            prior_ids = []
        own_rows = connection.execute(
            "SELECT id FROM artifacts WHERE task_id = ? AND step_id = ?",
            (task_id, step_id),
        ).fetchall()
        own_ids = {str(row["id"]) for row in own_rows}
        outputs = [value for value in prior_ids if isinstance(value, str) and value in own_ids]
        for artifact_id in artifact_ids:
            if artifact_id in own_ids and artifact_id not in outputs:
                outputs.append(artifact_id)
        now = _utc_now_iso()
        connection.execute(
            """UPDATE task_steps SET status = 'completed', progress = 100,
                   message = ?, error_code = NULL, finished_at = ?,
                   output_artifact_ids_json = ?
               WHERE task_id = ? AND id = ?""",
            (
                "下载完成并已登记媒体",
                now,
                json.dumps(outputs, ensure_ascii=False, separators=(",", ":")),
                task_id,
                step_id,
            ),
        )
        statuses = connection.execute(
            "SELECT status FROM task_steps WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        terminal_success = {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value, StepStatus.SKIPPED.value}
        if all(row["status"] in terminal_success for row in statuses):
            task_status = (
                TaskStatus.PARTIAL.value
                if any(row["status"] == StepStatus.PARTIAL.value for row in statuses)
                else TaskStatus.COMPLETED.value
            )
        else:
            task_status = TaskStatus.RUNNING.value if any(row["status"] == StepStatus.RUNNING.value for row in statuses) else TaskStatus.QUEUED.value
        connection.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (task_status, now, task_id),
        )

    def _update_task_step(
        self,
        task_id: str,
        step_id: str,
        *,
        task_status: str,
        step_status: str,
        progress: float | None,
        message: str,
        error_code: str | None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE task_steps SET status = ?, progress = ?, message = ?,
                       error_code = ?, started_at = COALESCE(?, started_at),
                       finished_at = ?
                   WHERE task_id = ? AND id = ?""",
                (
                    step_status,
                    progress,
                    _safe_message(message),
                    error_code,
                    started_at,
                    finished_at,
                    task_id,
                    step_id,
                ),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (task_status, _utc_now_iso(), task_id),
            )

    def _persist_progress(
        self, task_id: str, step_id: str, percent: float | None, message: str
    ) -> bool:
        if percent is not None:
            percent = max(0.0, min(100.0, float(percent)))
        with self.database.transaction(write=True) as connection:
            updated = connection.execute(
                """UPDATE task_steps SET progress = COALESCE(?, progress), message = ?
                   WHERE task_id = ? AND id = ? AND status = 'running'
                     AND error_code IS NOT 'step_skip_requested'""",
                (percent, _safe_message(message), task_id, step_id),
            )
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (_utc_now_iso(), task_id),
            )
            return updated.rowcount == 1

    def _complete_task_step(
        self,
        task_id: str,
        step_id: str,
        artifact_ids: list[str],
        *,
        status: str = StepStatus.COMPLETED.value,
        message: str = "步骤完成",
    ) -> None:
        if status not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}:
            raise ValueError("workbench step result status is invalid")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("workbench step returned duplicate artifact IDs")
        with self._lock, self.database.transaction(write=True) as connection:
            task_row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task_row is None or task_row["status"] == TaskStatus.CANCELLED.value:
                raise TaskCancelled
            step = connection.execute(
                "SELECT status, kind FROM task_steps WHERE task_id = ? AND id = ?",
                (task_id, step_id),
            ).fetchone()
            if step is None:
                raise ValueError("workbench step state changed before completion")
            if not artifact_ids and step["kind"] != StepKind.INDEX.value:
                raise ValueError("completed workbench step has no registered artifact")
            if artifact_ids:
                placeholders = ",".join("?" for _ in artifact_ids)
                owned = connection.execute(
                    f"SELECT id FROM artifacts WHERE task_id = ? AND step_id = ? AND id IN ({placeholders})",
                    (task_id, step_id, *artifact_ids),
                ).fetchall()
                if {str(row["id"]) for row in owned} != set(artifact_ids):
                    raise ValueError("workbench step returned an unregistered artifact")
            now = _utc_now_iso()
            if step["status"] == StepStatus.RUNNING.value:
                connection.execute(
                    """UPDATE task_steps SET status = ?, progress = 100, message = ?,
                           error_code = NULL, output_artifact_ids_json = ?, finished_at = ?
                       WHERE task_id = ? AND id = ?""",
                    (status, _safe_message(message), json.dumps(artifact_ids), now, task_id, step_id),
                )
            elif step["status"] not in {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value}:
                raise ValueError("workbench step state changed before completion")
            if step["kind"] == StepKind.OCR.value:
                connection.execute(
                    "DELETE FROM local_ocr_checkpoints WHERE task_id = ? AND step_id = ?",
                    (task_id, step_id),
                )
            all_steps = connection.execute(
                "SELECT status FROM task_steps WHERE task_id = ? ORDER BY ordinal",
                (task_id,),
            ).fetchall()
            successful = {StepStatus.COMPLETED.value, StepStatus.PARTIAL.value, StepStatus.SKIPPED.value}
            if all(row["status"] in successful for row in all_steps):
                task_status = (
                    TaskStatus.PARTIAL.value
                    if any(row["status"] == StepStatus.PARTIAL.value for row in all_steps)
                    else TaskStatus.COMPLETED.value
                )
            else:
                task_status = TaskStatus.RUNNING.value if any(row["status"] == StepStatus.RUNNING.value for row in all_steps) else TaskStatus.QUEUED.value
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (task_status, now, task_id),
            )

    def _mark_task_only(self, task_id: str, status: str) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND (status != 'cancelled' OR ? = 'cancelled')",
                (status, _utc_now_iso(), task_id, status),
            )

    def _set_waiting_authorization(self, task_id: str, step_id: str, message: str) -> None:
        now = _utc_now_iso()
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """UPDATE task_steps SET status = ?, message = ?, error_code = NULL,
                       finished_at = ? WHERE task_id = ? AND id = ? AND status = 'running'""",
                (StepStatus.AWAITING_AUTHORIZATION.value, _safe_message(message), now, task_id, step_id),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != 'cancelled'",
                (TaskStatus.RUNNING.value, now, task_id),
            )

    def _set_terminal(
        self,
        task_id: str,
        task_status: str,
        step_status: str,
        message: str,
        error_code: str,
        *,
        step_id: str | None = None,
    ) -> None:
        with self._lock:
            now = _utc_now_iso()
            with self.database.transaction(write=True) as connection:
                task_row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task_row is None or task_row["status"] == TaskStatus.COMPLETED.value:
                    return
                if (
                    task_row["status"] == TaskStatus.CANCELLED.value
                    and task_status != TaskStatus.CANCELLED.value
                ):
                    return
                if step_id is None:
                    current = connection.execute(
                        """SELECT id, progress FROM task_steps WHERE task_id = ?
                           AND status NOT IN ('completed', 'partial', 'skipped') ORDER BY ordinal, id LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                else:
                    current = connection.execute(
                        "SELECT id, progress FROM task_steps WHERE task_id = ? AND id = ? AND status NOT IN ('completed', 'partial', 'skipped')",
                        (task_id, step_id),
                    ).fetchone()
                if current is not None:
                    connection.execute(
                        """UPDATE task_steps SET status = ?, progress = ?, message = ?,
                               error_code = ?, finished_at = ?
                           WHERE task_id = ? AND id = ? AND status != 'completed'""",
                        (
                            step_status,
                            current["progress"],
                            _safe_message(message),
                            error_code,
                            now,
                            task_id,
                            current["id"],
                        ),
                    )
                elif step_id is None:
                    # A completed step is authoritative; terminal error/cancel
                    # handling must not regress its task.
                    task_status = TaskStatus.COMPLETED.value
                if step_id is not None and task_status != TaskStatus.CANCELLED.value:
                    task_status = TaskStatus.RUNNING.value
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != 'completed'",
                    (task_status, now, task_id),
                )

    def _emit(self, callback: EventCallback | None, **event: object) -> None:
        if callback is None:
            return
        safe = dict(event)
        if isinstance(safe.get("message"), str):
            safe["message"] = _safe_message(str(safe["message"]))
        try:
            callback(safe)
        except Exception:
            return


_DEFAULT_RUNNER: TaskRunner | None = None
_DEFAULT_RUNNER_LOCK = threading.Lock()


def get_default_runner() -> TaskRunner:
    global _DEFAULT_RUNNER
    with _DEFAULT_RUNNER_LOCK:
        if _DEFAULT_RUNNER is None:
            database = Database()
            _DEFAULT_RUNNER = TaskRunner(database, AppConfig(database))
        return _DEFAULT_RUNNER


def _hash_file(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise ValueError("media file must not be a symbolic link")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("media file must be a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(stream.fileno())
    if (
        before.st_size != size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ValueError("media changed while being verified")
    return digest.hexdigest(), size


@contextmanager
def _publication_transaction(
    database: Database,
    root: Path,
    published: list[tuple[Path, str]],
    created_directories: list[Path],
    *,
    marker_path: Path | None = None,
    publication_id: str | None = None,
):
    """Remove only this transaction's unreferenced files if DB registration rolls back."""
    try:
        with database.transaction(write=True) as connection:
            yield connection
    except BaseException:
        cleanup_complete = True
        for path, expected_digest in reversed(published):
            try:
                if not path.exists() and not path.is_symlink():
                    continue
                if path.is_symlink():
                    cleanup_complete = False
                    continue
                contained = _assert_contained(path, root, strict=True)
                if contained.is_symlink() or not contained.is_file():
                    cleanup_complete = False
                    continue
                digest, _ = _hash_file(contained)
                if digest.casefold() != expected_digest.casefold():
                    cleanup_complete = False
                    continue
                with database.transaction(write=True) as connection:
                    referenced = connection.execute(
                        "SELECT 1 FROM artifacts WHERE path = ? LIMIT 1",
                        (str(contained),),
                    ).fetchone()
                    if referenced is None:
                        # Recheck containment/hash while holding the DB write lock;
                        # no other artifact registration can race this unlink.
                        checked = _assert_contained(path, root, strict=True)
                        if checked != contained or path.is_symlink() or checked.is_symlink():
                            cleanup_complete = False
                            continue
                        current_digest, _ = _hash_file(checked)
                        if current_digest.casefold() == expected_digest.casefold():
                            checked.unlink()
                        else:
                            cleanup_complete = False
                    else:
                        cleanup_complete = False
            except (OSError, sqlite3.Error, ValueError):
                cleanup_complete = False
                continue
        for directory in reversed(created_directories):
            try:
                contained = _assert_contained(directory, root, strict=True)
                if contained.is_symlink() or not contained.is_dir():
                    cleanup_complete = False
                    continue
                if not any(contained.iterdir()):
                    contained.rmdir()
                else:
                    cleanup_complete = False
            except (OSError, ValueError):
                cleanup_complete = False
                continue
        if cleanup_complete and marker_path is not None and publication_id is not None:
            _remove_download_manifest(marker_path, root, publication_id)
        raise


def _verified_archive_file(path: Path, root: Path, expected_sha256: str) -> Path:
    _assert_contained(path, root, strict=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("registered media path is unavailable")
    digest, _ = _hash_file(path)
    if digest.casefold() != expected_sha256.casefold():
        raise ValueError("registered media hash no longer matches")
    return path.resolve(strict=True)


def _remove_task_stage_file(path: Path, root: Path, task_id: str) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        stage_root = resolved_root / ".workbench-staging"
        _assert_contained(path, stage_root, strict=True)
        if not path.parent.name.endswith(f"_{task_id}"):
            return
        if path.is_symlink() or not path.is_file():
            return
        path.unlink()
    except (OSError, ValueError):
        return


def _remove_download_sidecar(path: Path, root: Path, task_id: str) -> None:
    if path.name not in {"metadata.json", "metadata.md"}:
        return
    _remove_task_stage_file(path, root, task_id)


def _validated_cover(value: object, root: Path) -> tuple[Path, str, int] | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    try:
        _assert_contained(path, root, strict=True)
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in {
            ".jpg", ".jpeg", ".png", ".webp"
        }:
            return None
        with path.open("rb") as stream:
            header = stream.read(16)
        valid = (
            path.suffix.casefold() in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")
        ) or (
            path.suffix.casefold() == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            path.suffix.casefold() == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
        )
        if not valid:
            return None
        digest, size_bytes = _hash_file(path)
        return path, digest, size_bytes
    except (OSError, ValueError):
        return None


def _safe_message(value: str) -> str:
    value = re.sub(r"https?://\S+", "<链接已隐藏>", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?i)(xsec_token|share_token|token|signature|sign|auth|authorization|api[_-]?key)=([^&\s]+)",
        r"\1=<已隐藏>",
        value,
    )
    return value[:500]


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:512]
    return None


def _cover_source(value: object) -> str:
    return value if value in {"original", "video_frame", "missing"} else "missing"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime_to_db(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _assert_task_staging_file(path: Path, root: Path, task_id: str) -> Path:
    if not _HEX_ID_PATTERN.fullmatch(task_id):
        raise ValueError("invalid task staging identity")
    resolved_root = root.resolve(strict=True)
    stage_root = resolved_root / ".workbench-staging"
    _assert_no_symlink_components(stage_root, resolved_root, require_leaf=True)
    checked = _assert_no_symlink_components(path, stage_root, require_leaf=True)
    if not checked.parent.name.endswith(f"_{task_id}"):
        raise ValueError("staging file does not belong to this task")
    if checked.is_symlink() or not checked.is_file():
        raise ValueError("staging path must be a regular file")
    return checked


def _assert_no_symlink_components(
    path: Path, root: Path, *, require_leaf: bool
) -> Path:
    root = root.resolve(strict=True)
    raw = Path(path)
    if not raw.is_absolute():
        raise ValueError("archive path must be absolute")
    try:
        lexical = Path(os.path.abspath(raw))
        relative = lexical.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("archive path escapes its root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("archive path is invalid")
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("archive path contains a symbolic link")
        if index < len(relative.parts) - 1 and (not current.exists() or not current.is_dir()):
            raise ValueError("archive path parent is unavailable")
    if require_leaf and not current.exists():
        raise ValueError("archive path is unavailable")
    resolved = _assert_contained(current, root, strict=require_leaf)
    return resolved


def _safe_display_text(value: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _URL_TEXT_PATTERN.sub("<链接已隐藏>", value)
    cleaned = _safe_message(cleaned)
    cleaned = " ".join("".join(char for char in cleaned if ord(char) >= 32 and ord(char) != 127).split())
    return cleaned[:max_length].strip()


def _safe_published_at(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _write_task_stage_payload(path: Path, payload: bytes, root: Path, task_id: str) -> None:
    checked_root = root.resolve(strict=True)
    stage_root = checked_root / ".workbench-staging"
    if path.name not in {"metadata.json", "metadata.md"}:
        raise ValueError("unsupported task sidecar name")
    parent = _assert_no_symlink_components(path.parent, stage_root, require_leaf=True)
    if not parent.name.endswith(f"_{task_id}"):
        raise ValueError("sidecar parent does not belong to this task")
    target = parent / path.name
    if target.is_symlink():
        raise ValueError("sidecar target must not be a symbolic link")
    if target.exists() and not target.is_file():
        raise ValueError("sidecar target must be a regular file")
    with target.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(parent)
    _assert_task_staging_file(target, checked_root, task_id)


def _download_summary_markdown(metadata: dict[str, object]) -> str:
    fields = (
        ("标题", metadata.get("title")),
        ("简介", metadata.get("description")),
        ("平台", metadata.get("platform")),
        ("作品 ID", metadata.get("source_id")),
        ("作者", metadata.get("author")),
        ("发布时间", metadata.get("published_at")),
        ("时长（秒）", metadata.get("duration_seconds")),
        ("分辨率", _resolution_text(metadata.get("width"), metadata.get("height"))),
        ("SHA-256", metadata.get("sha256")),
        ("封面来源", metadata.get("cover_source")),
    )
    lines = ["# 视频归档摘要", ""]
    for label, value in fields:
        if value is None or value == "":
            continue
        text_value = _safe_display_text(str(value), max_length=5000 if label == "简介" else 512)
        escaped = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text_value)
        lines.append(f"- **{label}**：{escaped}")
    lines.extend(["", "媒体完整解码校验：通过", ""])
    return "\n".join(lines)


def _resolution_text(width: object, height: object) -> str | None:
    if isinstance(width, int) and not isinstance(width, bool) and isinstance(height, int) and not isinstance(height, bool):
        return f"{width}×{height}"
    return None


def _manifest_asset_matches_task(
    task: TaskRecord,
    asset_row: sqlite3.Row,
    digest: str,
    *,
    verified_identity: tuple[str, str] | None = None,
) -> bool:
    """Accept only the task's identity, or the exact verified content duplicate."""
    asset_id_matches = task.asset_id == str(asset_row["id"])
    source_matches = (
        isinstance(task.source.platform, str)
        and isinstance(task.source.source_id, str)
        and isinstance(asset_row["platform"], str)
        and task.source.platform.casefold() == str(asset_row["platform"]).casefold()
        and task.source.source_id == asset_row["source_id"]
    )
    verified_source_matches = (
        verified_identity is not None
        and isinstance(verified_identity[0], str)
        and isinstance(verified_identity[1], str)
        and bool(verified_identity[0])
        and bool(verified_identity[1])
        and (task.source.platform is None or task.source.platform.casefold() == verified_identity[0].casefold())
        and (task.source.source_id is None or task.source.source_id == verified_identity[1])
        and isinstance(asset_row["platform"], str)
        and asset_row["platform"].casefold() == verified_identity[0].casefold()
        and asset_row["source_id"] == verified_identity[1]
    )
    hash_matches = (
        isinstance(asset_row["sha256"], str)
        and str(asset_row["sha256"]).casefold() == digest.casefold()
    )
    return (
        (asset_id_matches and (source_matches or hash_matches or verified_source_matches))
        or source_matches
        or hash_matches
        or verified_source_matches
    )


def _update_asset_archive_pointer(
    connection: sqlite3.Connection,
    asset_id: str,
    path: Path,
    digest: str,
    archived_at: datetime,
    title: str,
) -> None:
    row = connection.execute(
        "SELECT archived_at, archive_path, sha256 FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()
    if row is None:
        raise KeyError("asset no longer exists")
    current_archived = row["archived_at"]
    if isinstance(current_archived, str) and current_archived:
        try:
            current_time = datetime.fromisoformat(current_archived.replace("Z", "+00:00"))
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            if current_time.astimezone(timezone.utc) > archived_at.astimezone(timezone.utc):
                return
        except ValueError:
            if row["archive_path"] != str(path) or str(row["sha256"] or "").casefold() != digest.casefold():
                return
    connection.execute(
        """UPDATE assets SET archive_path = ?, sha256 = ?, archived_at = ?,
               title = COALESCE(NULLIF(title, ''), ?) WHERE id = ?""",
        (str(path), digest.casefold(), _datetime_to_db(archived_at), title, asset_id),
    )


def _manifest_relative_path(path: Path, root: Path) -> str:
    checked_root = root.resolve(strict=True)
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(checked_root)
    except ValueError as exc:
        raise ValueError("manifest path is outside the archive root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("manifest path is invalid")
    return PurePosixPath(*relative.parts).as_posix()


def _manifest_path_from_relative(value: object, root: Path, *, strict: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\\" in value:
        raise ValueError("manifest path is invalid")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise ValueError("manifest path must be root-relative")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError("manifest path traversal is invalid")
    path = root.resolve(strict=True).joinpath(*posix.parts)
    return _assert_no_symlink_components(path, root, require_leaf=strict)


def _download_publication_manifest(
    publication_id: str,
    task_id: str,
    step_id: str,
    asset_id: str,
    archived_at: datetime,
    folder: Path,
    root: Path,
    plans: list[dict[str, object]],
    *,
    cleanup_staging: list[dict[str, object]],
) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    for plan in plans:
        staged_path = plan["staged_path"]
        kind = str(plan["kind"])
        artifact: dict[str, object] = {
            "artifact_id": str(plan["artifact_id"]),
            "version": int(plan["version"]),
            "kind": kind,
            "extension": str(plan["extension"]),
            "final_path": _manifest_relative_path(Path(plan["final_path"]), root),
            "staged_path": _manifest_relative_path(staged_path, root) if isinstance(staged_path, Path) else None,
            "sha256": str(plan["sha256"]).casefold(),
            "size_bytes": int(plan["size_bytes"]),
            "metadata": dict(plan["metadata"]),
        }
        if plan.get("reuse_artifact_id") is not None:
            artifact["reuse_artifact_id"] = str(plan["reuse_artifact_id"])
        artifacts.append(artifact)
    cleanup: list[dict[str, object]] = []
    for item in cleanup_staging:
        cleanup.append({
            "path": _manifest_relative_path(Path(item["path"]), root),
            "kind": str(item["kind"]),
            "sha256": str(item["sha256"]).casefold(),
            "size_bytes": int(item["size_bytes"]),
        })
    return {
        "schema": 1,
        "id": publication_id,
        "task_id": task_id,
        "step_id": step_id,
        "asset_id": asset_id,
        "archived_at": local_archive_time(archived_at).isoformat(),
        "archive_directory": _manifest_relative_path(folder, root),
        "artifacts": artifacts,
        "cleanup_staging": cleanup,
    }


def _write_download_publication_manifest(
    marker_path: Path, root: Path, manifest: dict[str, object]
) -> None:
    resolved_root = root.resolve(strict=True)
    if marker_path.parent.resolve(strict=True) != resolved_root:
        raise ValueError("download publication marker must be in the archive root")
    match = _DOWNLOAD_MANIFEST_PATTERN.fullmatch(marker_path.name)
    if match is None or manifest.get("id") != match.group(1):
        raise ValueError("download publication marker identity is invalid")
    _assert_no_symlink_components(marker_path, resolved_root, require_leaf=False)
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("download publication manifest is too large")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(marker_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            marker_path.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(resolved_root)


def _read_download_publication_manifest(marker_path: Path, root: Path) -> dict[str, object]:
    checked = _assert_no_symlink_components(marker_path, root, require_leaf=True)
    if checked.parent != root.resolve(strict=True) or checked.is_symlink() or not checked.is_file():
        raise ValueError("download publication manifest is not a root file")
    if checked.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("download publication manifest is too large")
    payload = checked.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("download publication manifest must be an object")
    return value


def _validate_download_manifest(
    manifest: dict[str, object], root: Path, marker_path: Path
) -> dict[str, object]:
    if set(manifest) != {
        "schema", "id", "task_id", "step_id", "asset_id", "archived_at",
        "archive_directory", "artifacts", "cleanup_staging",
    }:
        raise ValueError("download publication manifest has unsupported fields")
    if manifest.get("schema") != 1:
        raise ValueError("unsupported download publication manifest schema")
    publication_id = manifest.get("id")
    task_id = manifest.get("task_id")
    step_id = manifest.get("step_id")
    asset_id = manifest.get("asset_id")
    if not all(isinstance(value, str) and _HEX_ID_PATTERN.fullmatch(value) for value in (
        publication_id, task_id, step_id, asset_id
    )):
        raise ValueError("download publication IDs are invalid")
    if marker_path.name != f".workbench-download-{publication_id}.manifest.json":
        raise ValueError("download publication marker does not match its ID")
    archived_text = manifest.get("archived_at")
    if not isinstance(archived_text, str):
        raise ValueError("download publication time is missing")
    try:
        archived_at = datetime.fromisoformat(archived_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("download publication time is invalid") from exc
    if archived_at.tzinfo is None:
        raise ValueError("download publication time must include a timezone")
    directory = _manifest_path_from_relative(manifest.get("archive_directory"), root)
    rel_directory = directory.relative_to(root.resolve(strict=True))
    if (
        len(rel_directory.parts) != 2
        or not _ARCHIVE_DATE_PATTERN.fullmatch(rel_directory.parts[0])
        or not _ARCHIVE_SEQUENCE_PATTERN.fullmatch(rel_directory.parts[1])
        or directory.is_symlink()
        or not directory.is_dir()
    ):
        raise ValueError("download archive directory is invalid")
    if rel_directory.parts[0] != archived_at.strftime("%Y-%m-%d"):
        raise ValueError("download archive date does not match its timestamp")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not 3 <= len(raw_artifacts) <= 4:
        raise ValueError("download manifest artifact list is invalid")
    artifacts: list[dict[str, object]] = []
    artifact_ids: set[str] = set()
    kinds: set[str] = set()
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise ValueError("download manifest artifact entry is invalid")
        if set(raw) - {
            "artifact_id", "version", "kind", "extension", "final_path", "staged_path",
            "sha256", "size_bytes", "metadata", "reuse_artifact_id",
        }:
            raise ValueError("download manifest artifact contains unsupported fields")
        artifact_id = raw.get("artifact_id")
        kind = raw.get("kind")
        version = raw.get("version")
        extension = raw.get("extension")
        digest = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        metadata = raw.get("metadata")
        if not isinstance(artifact_id, str) or not _HEX_ID_PATTERN.fullmatch(artifact_id):
            raise ValueError("download manifest artifact ID is invalid")
        if artifact_id in artifact_ids:
            raise ValueError("download manifest contains duplicate artifact IDs")
        artifact_ids.add(artifact_id)
        if kind not in {"video", "cover", "metadata", "note"} or kind in kinds:
            raise ValueError("download manifest artifact kind is invalid")
        kinds.add(str(kind))
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("download manifest artifact version is invalid")
        if not isinstance(extension, str) or extension != extension.casefold():
            raise ValueError("download manifest artifact extension is invalid")
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("download manifest artifact hash is invalid")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise ValueError("download manifest artifact size is invalid")
        if not isinstance(metadata, dict):
            raise ValueError("download manifest artifact metadata is invalid")
        reuse_id = raw.get("reuse_artifact_id")
        if reuse_id is not None and (not isinstance(reuse_id, str) or not _HEX_ID_PATTERN.fullmatch(reuse_id)):
            raise ValueError("reused artifact ID is invalid")
        final_path = _manifest_path_from_relative(raw.get("final_path"), root)
        if reuse_id is None:
            if final_path.parent != directory:
                raise ValueError("new artifact final path is outside its archive directory")
        else:
            reused_relative = final_path.relative_to(root.resolve(strict=True))
            if (
                kind not in {"video", "cover"}
                or len(reused_relative.parts) != 3
                or not _ARCHIVE_DATE_PATTERN.fullmatch(reused_relative.parts[0])
                or not _ARCHIVE_SEQUENCE_PATTERN.fullmatch(reused_relative.parts[1])
            ):
                raise ValueError("reused artifact path is not a verified archive path")
        if kind == "video":
            if extension not in _VIDEO_EXTENSIONS or final_path.name != f"video{extension}":
                raise ValueError("video artifact path or extension is invalid")
            _validate_safe_video_metadata(metadata, digest, size_bytes)
        elif kind == "cover":
            if extension not in _COVER_EXTENSIONS or final_path.name != f"cover{extension}":
                raise ValueError("cover artifact path or extension is invalid")
            if set(metadata) - {"cover_source", "reused", "size_bytes"}:
                raise ValueError("cover artifact metadata contains unsupported fields")
            if metadata.get("cover_source") not in {"original", "video_frame", "missing"}:
                raise ValueError("cover artifact metadata is invalid")
            if "reused" in metadata and not isinstance(metadata["reused"], bool):
                raise ValueError("cover reuse metadata is invalid")
            if "size_bytes" in metadata and (
                isinstance(metadata["size_bytes"], bool)
                or not isinstance(metadata["size_bytes"], int)
            ):
                raise ValueError("cover size metadata is invalid")
            if metadata.get("size_bytes", size_bytes) != size_bytes:
                raise ValueError("cover artifact size metadata is inconsistent")
        elif kind == "metadata":
            if extension != ".json" or final_path.name != "metadata.json":
                raise ValueError("metadata artifact path is invalid")
            if metadata.get("media_type") != "safe_metadata_json" or metadata.get("size_bytes") != size_bytes:
                raise ValueError("metadata artifact descriptor is invalid")
            if set(metadata) != {"media_type", "size_bytes"}:
                raise ValueError("metadata artifact descriptor has extra fields")
        else:
            if extension != ".md" or final_path.name != "metadata.md":
                raise ValueError("note artifact path is invalid")
            if metadata.get("media_type") != "safe_summary_markdown" or metadata.get("size_bytes") != size_bytes:
                raise ValueError("note artifact descriptor is invalid")
            if set(metadata) != {"media_type", "size_bytes"}:
                raise ValueError("note artifact descriptor has extra fields")

        staged_value = raw.get("staged_path")
        staged_path = None
        if staged_value is not None:
            staged_path = _manifest_path_from_relative(staged_value, root)
            stage_relative = staged_path.relative_to(root.resolve(strict=True))
            if (
                len(stage_relative.parts) < 3
                or stage_relative.parts[0] != ".workbench-staging"
                or not stage_relative.parts[1].endswith(f"_{task_id}")
                or staged_path.suffix.casefold() != extension
            ):
                raise ValueError("artifact staging path is invalid")
            if kind == "metadata" and staged_path.name != "metadata.json":
                raise ValueError("metadata staging path is invalid")
            if kind == "note" and staged_path.name != "metadata.md":
                raise ValueError("note staging path is invalid")
        if (reuse_id is None) != (staged_path is not None):
            raise ValueError("artifact reuse and staging fields disagree")
        if reuse_id is not None and kind not in {"video", "cover"}:
            raise ValueError("sidecar artifact cannot reuse another artifact")
        artifacts.append({
            "artifact_id": artifact_id,
            "version": version,
            "kind": str(kind),
            "extension": extension,
            "final_path": final_path,
            "staged_path": staged_path,
            "sha256": digest.casefold(),
            "size_bytes": size_bytes,
            "metadata": metadata,
            "reuse_artifact_id": reuse_id,
        })
    if kinds not in ({"video", "metadata", "note"}, {"video", "cover", "metadata", "note"}):
        raise ValueError("download manifest must describe video and safe sidecars")
    video = next(entry for entry in artifacts if entry["kind"] == "video")
    for entry in artifacts:
        if entry["kind"] != "video" and entry["kind"] != "cover":
            continue
        if entry["reuse_artifact_id"] is None and entry["staged_path"] is None:
            raise ValueError("newly published media artifact is missing staging path")
        if entry["reuse_artifact_id"] is not None and entry["staged_path"] is not None:
            raise ValueError("reused media artifact cannot have a staging path")
    raw_cleanup = manifest.get("cleanup_staging")
    if not isinstance(raw_cleanup, list) or len(raw_cleanup) > 4:
        raise ValueError("download staging cleanup list is invalid")
    cleanup_staging: list[dict[str, object]] = []
    for raw in raw_cleanup:
        if not isinstance(raw, dict) or raw.get("kind") not in {"video", "cover", "sidecar"}:
            raise ValueError("download staging cleanup entry is invalid")
        if set(raw) != {"path", "kind", "sha256", "size_bytes"}:
            raise ValueError("download staging cleanup entry contains unsupported fields")
        path = _manifest_path_from_relative(raw.get("path"), root)
        relative = path.relative_to(root.resolve(strict=True))
        extension = path.suffix.casefold()
        allowed = (
            _VIDEO_EXTENSIONS if raw["kind"] == "video"
            else _COVER_EXTENSIONS if raw["kind"] == "cover"
            else {".json", ".md"}
        )
        if (
            len(relative.parts) < 3
            or relative.parts[0] != ".workbench-staging"
            or not relative.parts[1].endswith(f"_{task_id}")
            or extension not in allowed
            or (raw["kind"] == "sidecar" and path.name not in {"metadata.json", "metadata.md"})
        ):
            raise ValueError("download staging cleanup path is invalid")
        digest = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("download staging cleanup hash is invalid")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise ValueError("download staging cleanup size is invalid")
        cleanup_staging.append({
            "path": path,
            "kind": raw["kind"],
            "sha256": digest.casefold(),
            "size_bytes": size_bytes,
        })
    return {
        "id": publication_id,
        "task_id": task_id,
        "step_id": step_id,
        "asset_id": asset_id,
        "archived_at": archived_at,
        "archive_directory": directory,
        "artifacts": artifacts,
        "cleanup_staging": cleanup_staging,
        "video_entry": video,
    }


def _validate_safe_video_metadata(metadata: dict[str, object], digest: str, size_bytes: int) -> None:
    allowed = {
        "media_type", "platform", "source_id", "title", "description", "author", "published_at",
        "sha256", "size_bytes", "duration_seconds", "width", "height", "has_audio",
        "full_decode_verified", "cover_source", "reused",
    }
    if set(metadata) - allowed:
        raise ValueError("video manifest metadata contains unsupported fields")
    if metadata.get("media_type") != "video" or metadata.get("sha256") != digest:
        raise ValueError("video manifest metadata hash is inconsistent")
    if metadata.get("size_bytes") != size_bytes or metadata.get("full_decode_verified") is not True:
        raise ValueError("video manifest metadata is not verified")
    if not isinstance(metadata.get("reused", False), bool):
        raise ValueError("video reuse metadata is invalid")
    for key in ("platform", "source_id", "title"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("video safe identity metadata is invalid")
    if safe_title(str(metadata["title"]), max_length=512) != metadata["title"]:
        raise ValueError("video title is not safely normalized")
    description = metadata.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 5000):
        raise ValueError("video description metadata is invalid")
    for key in ("author", "published_at"):
        value = metadata.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise ValueError("video optional metadata is invalid")
    for value in metadata.values():
        if isinstance(value, str) and _URL_TEXT_PATTERN.search(value):
            raise ValueError("video manifest metadata contains a URL")
    duration = metadata.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or duration <= 0:
        raise ValueError("video duration metadata is invalid")
    for key in ("width", "height"):
        value = metadata.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError("video dimension metadata is invalid")
    if not isinstance(metadata.get("has_audio"), bool):
        raise ValueError("video audio metadata is invalid")
    if metadata.get("cover_source") not in {"original", "video_frame", "missing"}:
        raise ValueError("video cover source metadata is invalid")


def _verify_manifest_file(path: Path, root: Path, entry: dict[str, object]) -> None:
    checked = _assert_no_symlink_components(path, root, require_leaf=True)
    if checked.is_symlink() or not checked.is_file():
        raise ValueError("manifest media path is not a regular file")
    digest, size_bytes = _hash_file(checked)
    if digest.casefold() != str(entry["sha256"]).casefold() or size_bytes != int(entry["size_bytes"]):
        raise ValueError("manifest media does not match its hash and size")


def _verify_safe_metadata_sidecar(path: Path, video_metadata: dict[str, object]) -> None:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("safe metadata sidecar is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {key: item for key, item in video_metadata.items() if key != "reused"}
    if not isinstance(value, dict) or value != expected:
        raise ValueError("safe metadata sidecar does not match its manifest")


def _verify_safe_note_sidecar(path: Path, video_metadata: dict[str, object]) -> None:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("safe note sidecar is too large")
    expected = _download_summary_markdown(
        {key: item for key, item in video_metadata.items() if key != "reused"}
    )
    if path.read_text(encoding="utf-8") != expected:
        raise ValueError("safe note sidecar does not match its manifest")


def _manifest_artifact_row_matches(
    row: sqlite3.Row, task_id: str, step_id: str, entry: dict[str, object]
) -> bool:
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        str(row["task_id"]) == task_id
        and str(row["step_id"]) == step_id
        and str(row["kind"]) == str(entry["kind"])
        and str(row["path"]) == str(entry["final_path"])
        and str(row["sha256"]).casefold() == str(entry["sha256"]).casefold()
        and int(row["version"]) == int(entry["version"])
        and metadata == entry["metadata"]
    )


def _remove_manifest_stage_file(
    path: Path, root: Path, task_id: str, entry: dict[str, object]
) -> bool:
    try:
        stage_root = root.resolve(strict=True) / ".workbench-staging"
        checked_path = _assert_no_symlink_components(path, stage_root, require_leaf=False)
        if not checked_path.parent.name.endswith(f"_{task_id}"):
            return False
        if not checked_path.exists() and not checked_path.is_symlink():
            return True
        checked = _assert_task_staging_file(path, root, task_id)
        kind = entry.get("kind")
        allowed_extensions = (
            _VIDEO_EXTENSIONS if kind == "video"
            else _COVER_EXTENSIONS if kind == "cover"
            else {".json", ".md"} if kind == "sidecar"
            else set()
        )
        if checked.suffix.casefold() not in allowed_extensions:
            return False
        if kind == "sidecar" and checked.name not in {"metadata.json", "metadata.md"}:
            return False
        digest, size_bytes = _hash_file(checked)
        if (
            digest.casefold() != str(entry.get("sha256", "")).casefold()
            or size_bytes != int(entry.get("size_bytes", -1))
        ):
            return False
        checked.unlink()
        return True
    except (OSError, TypeError, ValueError):
        return False


def _remove_download_manifest(marker_path: Path, root: Path, publication_id: str) -> None:
    if not _HEX_ID_PATTERN.fullmatch(publication_id):
        return
    if marker_path.name != f".workbench-download-{publication_id}.manifest.json":
        return
    try:
        checked = _assert_no_symlink_components(marker_path, root, require_leaf=True)
        if checked.parent != root.resolve(strict=True) or not checked.is_file() or checked.is_symlink():
            return
        checked.unlink()
        _fsync_directory(root.resolve(strict=True))
    except (OSError, ValueError):
        return


def _fsync_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("cannot fsync a non-regular publication file")
    # 作者：zhw；日期：2026-10-24 19:46:51。Windows 对只读文件描述符调用 os.fsync 会报 EBADF；读写打开不会改写文件内容。
    with path.open("rb+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("cannot fsync a non-regular publication file")
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
