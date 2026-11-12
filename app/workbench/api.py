"""Same-origin versioned API for local workbench tasks."""

from __future__ import annotations

import json
import hashlib
from io import BytesIO
import ipaddress
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, AsyncIterator, Literal
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from PIL import Image, UnidentifiedImageError

from .archive import _assert_contained
from .models import AssetRecord, StepKind, StepRecord, TaskRecord, sanitize_source_url
from .prompts import DEFAULT_KNOWLEDGE_CARD_PROMPT
from .repository import _asset_from_row
from .database import PROJECT_ROOT, Database
from .article_capabilities import article_platform_capabilities
from .article_studio import (
    ARTICLE_PLATFORMS,
    blocks_to_markdown,
    create_article,
    default_document,
    delete_article,
    finalize_article,
    get_article,
    latest_publication,
    list_articles,
    normalize_document,
    record_publication,
    claim_article_fan_notification,
    recover_article_publications,
    reserve_article_publication,
    retry_article_fan_notification,
    save_article,
    save_article_image,
    update_article_fan_notification,
    update_article_publication,
    utc_now,
)
from .article_platforms import (
    PlatformConnectionError,
    account_credentials,
    add_platform_account,
    platform_statuses,
    prepare_csdn_publication,
    publish_article as publish_article_platform,
    start_weibo_login,
    xiaohongshu_article_images,
    prepare_zhihu_article_file,
    refresh_wechat_fan_notification,
    refresh_wechat_publication,
    send_wechat_fan_notification,
    remove_platform_account,
    save_platform_account,
    update_platform_account,
    verify_wechat_credentials,
    verify_wechat_account_credentials,
)
from .csdn_publishing import ensure_csdn_auto_supported
from .article_image_generation import ArticleImageError, ensure_legacy_image_profile, generate_image, get_image_config, save_image_config
from .article_vector_generation import VectorIllustrationError, generate_vector_illustration
from .image_storage import image_directory, remove_registered_image, resolve_article_image, store_image
from .runner import RunnerError, TaskRunner, get_default_runner, _hash_file
from multi_sources import PLATFORMS, Source, parse_source
from .credentials import (
    CredentialStoreUnavailable,
    delete_model_api_key,
    get_model_api_key,
    set_model_api_key,
)
from .model_profiles import (
    archive_model_profile,
    get_default_model_profile,
    get_model_profile,
    get_transcription_step_config,
    list_model_profiles,
    migrate_legacy_model_profile_key,
    profile_api_key,
    save_model_profile,
    save_task_model_config,
    save_transcription_step_config,
    set_default_model_profile,
    set_default_image_model_profile,
    task_model_settings,
    task_profile_api_key,
    transcription_step_settings,
)
from .providers import (
    ProviderError,
    create_provider,
    get_default_base_url,
    get_provider_preset,
    list_provider_presets,
    lookup_model_capability,
)
from .config import _validate_model_base_url
from .script_styles import (
    MAX_SELECTED_SCRIPT_STYLES,
    build_script_style_snapshot,
    load_task_script_style_snapshot,
    save_task_script_style_snapshot,
)
from .collection_sources import (
    CollectionMediaError,
    delete_collection_media_source,
    register_collection_media_source,
)
from .folder_picker import choose_local_directory, choose_local_media, choose_local_video
from .video_publishing import register_local_video_source, get_local_video_source
from . import local_asr, local_ocr
from .platform_accounts import (
    PlatformAccountError,
    PlatformAccountSelectionRequired,
    get_platform_account_manager,
    get_task_access_account,
    get_task_access_mode,
    resolve_video_publish_account,
    save_task_access_mode,
)
from .uploads import UploadError, delete_upload, save_upload
from .transcript_reconcile import get_transcript_comparison_data, regenerate_transcript_draft
from .steps.reconcile_transcript import update_review_copy_word_count
from .steps.artifact_names import artifact_filename
from .task_archive import (
    TaskArchiveError,
    delete_task as permanently_delete_task,
    deletion_preview as task_deletion_preview,
    hide_task as hide_archived_task,
    open_task_output as resolve_task_output,
    restore_task as restore_archived_task,
)
from .video_notes import render_video_notes
from .bilibili_options import fetch_bilibili_categories, fetch_bilibili_statements
from .douyin_topics import DouyinTopicSearchError, search_douyin_topics
from .video_publishing import (
    SocialAutoUploadAdapter,
    VideoPublishError,
    VideoPublishManager,
    add_video_publish_account,
    clean_video_publish_batch_media,
    cleanup_expired_video_publish_media,
    confirm_video_publish_target,
    create_video_publish_batch,
    delete_video_publish_batch,
    delete_video_publish_draft,
    edit_video_publish_target,
    get_video_publish_batch,
    get_video_publish_batch_cover_path,
    get_video_publish_batch_media_path,
    get_video_publish_cover_upload_path,
    get_video_publish_draft_cover_path,
    get_video_publish_draft_media_path,
    get_video_publish_target,
    keep_video_publish_covers,
    latest_video_publish_login_qr,
    list_video_media,
    list_video_publish_accounts,
    list_video_publish_duplicate_notices,
    list_video_publish_batches,
    list_video_publish_drafts,
    release_video_publish_cover,
    request_video_publish_account_action,
    request_video_publish_retry,
    restore_video_publish_draft,
    save_video_publish_draft,
    save_video_publish_cover,
    video_publish_platforms,
)


_router = APIRouter(prefix="/api/v1")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_KEY = re.compile(r"(?i)(token|signature|sign|auth|authorization|api[_-]?key|cookie|secret)")
_ERROR_TEXT = "请求参数无效。"
_SUPPORTED_SOURCE_HOSTS = {
    "douyin": frozenset({"www.douyin.com", "www.iesdouyin.com", "v.douyin.com"}),
    "bilibili": frozenset({"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}),
}


class StepConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    step_kind: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    authorized: bool
    authorized_at: str | None = Field(default=None, max_length=64)


class CollectionMediaSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confirm_over_limit: bool = False


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_url: str | None = Field(default=None, min_length=8, max_length=4096)
    upload_id: str | None = Field(default=None, min_length=1, max_length=128)
    local_source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    steps: list[str] = Field(default_factory=lambda: ["download"], min_length=1, max_length=16)
    script_style_ids: list[str] = Field(default_factory=list, max_length=MAX_SELECTED_SCRIPT_STYLES)
    allow_first_frame: bool = False
    comment_sort_order: str = Field(default="likes_desc", pattern="^(likes_desc|time_desc|time_asc)$")
    transcription_mode: str = Field(default="cloud", pattern="^(cloud|local)$")
    ocr_mode: str = Field(default="cloud", pattern="^(cloud|local)$")
    ocr_interval_seconds: float = Field(default=2.0, ge=0.5, le=60)
    ocr_scan_profile: str = Field(default="full", pattern="^(full|subtitles)$")
    local_model: str = Field(default="small", pattern="^(tiny|base|small|medium|large-v3)$")
    local_language: str = Field(default="auto", pattern="^(auto|zh|en)$")
    local_device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")
    platform_access_mode: str = Field(default="anonymous", pattern="^(anonymous|account)$")
    platform_account_id: str | None = Field(default=None, min_length=1, max_length=64)
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    consents: list[StepConsentRequest] = Field(default_factory=list, max_length=16)


class LocalModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(pattern="^(tiny|base|small|medium|large-v3)$")


class LocalModelDownloadRequest(LocalModelRequest):
    revision: str = Field(pattern="^[a-f0-9]{40}$")
    confirmed: bool


class SourcePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_text: str = Field(min_length=1, max_length=4096)


class ScriptStyleTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    # 兼容旧版前端提交的字段；配置归一化会丢弃该字段。作者：zhw，时间：2026-11-07 14:25:40。
    description: str = Field(default="", max_length=300)
    instructions: str = Field(min_length=1, max_length=8_000)
    enabled: bool
    reference_paths: list[str] = Field(default_factory=list, max_length=50)


class WorkbenchSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archive_root: str | None = Field(default=None, max_length=2048)
    obsidian_reference_root: str | None = Field(default=None, max_length=2048)
    obsidian_output_root: str | None = Field(default=None, max_length=2048)
    model_provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    asr_model_id: str | None = Field(default=None, max_length=128)
    model_base_url: str | None = Field(default=None, max_length=2048)
    vector_search_enabled: bool = False
    local_asr_device: str = Field(default='auto', pattern='^(auto|cpu|cuda)$')
    knowledge_card_prompt: str = Field(default='', max_length=20_000)
    script_style_templates: list[ScriptStyleTemplateRequest] | None = Field(default=None, max_length=50)
    video_publish_auto_cleanup_enabled: bool = True
    video_publish_retention_days: int = Field(default=15, ge=1, le=3650)
    video_publish_cover_root: str | None = Field(default=None, max_length=2048)
    article_image_root: str | None = Field(default=None, max_length=2048)


class FolderPickerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    api_key: str = Field(min_length=1, max_length=4096)


class ModelConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_provider: str | None = Field(default=None, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    model_base_url: str | None = Field(default=None, max_length=2048)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)


class ModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    asr_model_id: str | None = Field(default=None, max_length=128)
    model_base_url: str | None = Field(default=None, max_length=2048)
    capabilities: list[Literal["text_generation", "image_generation"]] | None = Field(default=None, min_length=1, max_length=2)
    image_model_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    image_base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    make_default: bool | None = None
    make_image_default: bool | None = None


class ReferenceNotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    relative_paths: list[str] = Field(min_length=1, max_length=50)


class ArtifactVaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(min_length=1, max_length=1024)


class ArtifactVaultWriteRequest(ArtifactVaultRequest):
    expected_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")


class AuthorizeOcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_ids: list[str] = Field(min_length=1, max_length=8)


class AuthorizeCommentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    authorized: bool


class ArtifactTextEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    content: str = Field(max_length=5_000_000)


class TaskDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preview_token: str = Field(min_length=1, max_length=128)


class ArtifactReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class GenerateScriptsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    transcript_artifact_id: str = Field(min_length=1, max_length=128)
    comment_ids: list[str] = Field(default_factory=list, max_length=1000)
    style_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_SELECTED_SCRIPT_STYLES)
    authorized: bool


class ArticleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(default="", max_length=200)
    document: dict[str, Any] | None = None


class ArticleFromScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    authorized: bool


class ArticleSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int = Field(ge=1)
    document: dict[str, Any]


class CsdnPublishOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    article_type: Literal["original", "reprint", "translation"] = "original"
    source_url: str = Field(default="", max_length=2048)
    summary: str = Field(default="", max_length=256)
    visibility: Literal["public", "private", "followers", "vip"] = "public"
    template: Literal["default", "simple", "ink"] = "simple"
    declaration: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=200)
    activity: str = Field(default="", max_length=200)
    topic: str = Field(default="", max_length=200)
    backup_gitcode: bool = False
    cross_publish: bool = False


class ToutiaoPublishOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cover_mode: Literal["single", "triple", "none"] = "single"
    cover_image_paths: list[Annotated[str, StringConstraints(min_length=1, max_length=1024)]] = Field(default_factory=list, max_length=3)
    advertising: bool = False
    original_first: bool = False
    allow_tips: bool = False
    sync_micro: bool = False
    declarations: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] = Field(default_factory=list, max_length=10)
    location: str = Field(default="", max_length=200)
    collection: str = Field(default="", max_length=200)
    account_options_verified: bool = False


class CsdnTagLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    keyword: str = Field(default="", max_length=100)


class ArticlePublishOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    author: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] | None = Field(default=None, max_length=50)
    cover_image_path: str | None = Field(default=None, max_length=1024)
    retry_publication_id: str | None = Field(default=None, min_length=1, max_length=64)
    csdn: CsdnPublishOptions | None = None
    toutiao: ToutiaoPublishOptions | None = None
    notify_fans: bool = False


class ArticlePublishTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platform_id: str = Field(min_length=1, max_length=32)
    account_id: str | None = Field(default=None, min_length=1, max_length=64)
    options: ArticlePublishOptions = Field(default_factory=ArticlePublishOptions)


class ArticlePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platforms: list[str] = Field(default_factory=list, max_length=7)
    targets: list[ArticlePublishTarget] = Field(default_factory=list, max_length=20)
    confirmed: bool


class ArticleManualPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platform_id: str = Field(min_length=1, max_length=32)
    confirmed: bool


class ArticlePublishResultConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: str = Field(pattern="^(published|not_published)$")
    confirmed: bool


class ArticleFanNotificationRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confirmed: bool


class VideoPublishSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sau_project_path: str | None = Field(default=None, max_length=2048)


class VideoPublishAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    platform_id: str = Field(min_length=1, max_length=32)
    account_alias: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=80)


class VideoPublishOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=30)
    tid: int | None = Field(default=None, ge=1, le=10000)
    bilibili_category: int | None = Field(default=None, ge=1, le=10000)
    bilibili_category_name: str | None = Field(default=None, max_length=100)
    bilibili_ai: bool | None = None
    bilibili_statement: Literal[-2, -1, 1, 2, 3, 4] | None = None
    bilibili_statement_name: str | None = Field(default=None, max_length=100)
    bilibili_source: str | None = Field(default=None, max_length=500)
    bilibili_no_reprint: bool | None = None
    bilibili_private: bool | None = None
    bilibili_dynamic: str | None = Field(default=None, max_length=233)
    baijiahao_declaration: str | None = Field(default=None, max_length=30)
    declaration: Literal[
        "内容由AI生成",
        "内容为个人观点或见解",
        "内容为转载信息",
        "内容含营销推广信息",
        "虚拟演绎，仅供娱乐",
        "虚构演绎，仅供娱乐",
        "无需添加自主声明",
    ] | None = None
    repost_source: Literal["inside", "outside"] | None = None
    repost_title: str | None = Field(default=None, max_length=500)
    repost_id: str | None = Field(default=None, pattern=r"^\d{5,30}$")
    location: str | None = Field(default=None, max_length=100)
    location_id: str | None = Field(default=None, pattern=r"^\d{5,30}$")
    location_address: str | None = Field(default=None, max_length=300)
    location_city_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    location_city_name: str | None = Field(default=None, max_length=100)
    visibility: Literal["public", "friends", "private"] | None = None
    xhs_original: bool | None = None
    xhs_declaration: Literal["fiction", "ai", "advertising", "self_shot", "repost"] | None = None
    xhs_repost_source: str | None = Field(default=None, max_length=100)
    xhs_visibility: Literal["public", "friends", "private"] | None = None
    xhs_location: str | None = Field(default=None, max_length=100)
    xhs_location_id: str | None = Field(default=None, min_length=1, max_length=128)
    xhs_location_address: str | None = Field(default=None, min_length=1, max_length=300)
    allow_download: bool | None = None
    cover_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_portrait_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_landscape_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_4_3_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_16_9_upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_ratio: Literal["original", "landscape", "portrait", "square", "tall"] | None = None
    collection_name: str | None = Field(default=None, max_length=100)
    short_title: str | None = Field(default=None, max_length=15)
    save_as_draft: bool | None = None
    schedule_at_override: str | None = Field(default=None, max_length=32)


class VideoPublishTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account_id: str = Field(min_length=1, max_length=64)
    options: VideoPublishOptionsRequest = Field(default_factory=VideoPublishOptionsRequest)


class VideoPublishPoiSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    keyword: str = Field(min_length=1, max_length=100)
    city_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class VideoPublishTopicSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    keyword: str = Field(min_length=1, max_length=50)


class VideoPublishBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: str = Field(min_length=16, max_length=64)
    asset_id: str | None = Field(default=None, max_length=128)
    upload_id: str | None = Field(default=None, max_length=128)
    local_source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_portrait_upload_id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    cover_landscape_upload_id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    schedule_at: str | None = Field(default=None, max_length=32)
    targets: list[VideoPublishTargetRequest] = Field(min_length=1, max_length=20)
    confirmed: bool


class VideoPublishDraftSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    upload_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    local_source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    cover_upload_ids: dict[str, str] = Field(default_factory=dict)
    form: dict[str, Any]


class VideoPublishCoverKeepaliveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] = Field(max_length=100)


class VideoPublishTargetConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: str = Field(pattern="^(published|draft_saved|not_published)$")
    confirmed: bool


class VideoPublishTargetRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confirmed: bool
    change_schedule: bool = False
    schedule_at: str | None = Field(default=None, max_length=32)
    xhs_manual_submit: bool = False


class VideoPublishTargetEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    options: VideoPublishOptionsRequest
    confirmed: bool


class ArticlePlatformAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    public_value: str = Field(min_length=1, max_length=256)
    account_name: str | None = Field(default=None, max_length=80)
    secret_value: str | None = Field(default=None, max_length=4096)
    clear_secret: bool = False


class ArticleImageSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_url: str | None = Field(default=None, max_length=2048)
    model_id: str = Field(min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False


class ArticleImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: Literal["cover", "paragraph"]
    block_id: str | None = Field(default=None, max_length=80)
    mode: Literal["provider", "vector"] = "provider"
    confirmed: bool


class StructureAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transcript_artifact_id: str = Field(min_length=1, max_length=128)
    ocr_artifact_id: str | None = Field(default=None, min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    authorized: bool


class VideoNotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    transcript_artifact_id: str = Field(min_length=1, max_length=128)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)
    use_semantic: bool = False


def _parse_supported_source(value: str) -> Source:
    source = parse_source(value)
    allowed_hosts = _SUPPORTED_SOURCE_HOSTS.get(source.platform)
    parsed = urlsplit(source.url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("当前只支持抖音和 B 站官方公开作品链接。")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if allowed_hosts is None or hostname not in allowed_hosts:
        raise ValueError("当前只支持抖音和 B 站官方公开作品链接。")
    return source


class RetryStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_url: str | None = Field(default=None, min_length=8, max_length=4096)
    ocr_action: Literal["continue", "restart"] | None = None
    transcription_authorized: bool | None = None


class TranscriptionStepConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["cloud", "local"]
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    asr_model_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    local_model: str | None = Field(default=None, pattern=r"^(tiny|base|small|medium|large-v3)$")
    local_language: str | None = Field(default=None, pattern=r"^(auto|zh|en)$")
    local_device: str | None = Field(default=None, pattern=r"^(auto|cpu|cuda)$")


async def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        parsed_origin = urlsplit(origin)
        parsed_base = urlsplit(str(request.base_url))
        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
        base_port = parsed_base.port or (443 if parsed_base.scheme == "https" else 80)
        matches = (
            parsed_origin.scheme.casefold() == parsed_base.scheme.casefold()
            and (parsed_origin.hostname or "").casefold() == (parsed_base.hostname or "").casefold()
            and origin_port == base_port
            and parsed_origin.path in {"", "/"}
            and not parsed_origin.query
            and not parsed_origin.fragment
            and not parsed_origin.username
            and not parsed_origin.password
        )
    except ValueError:
        matches = False
    if not matches:
        raise HTTPException(status_code=403, detail="仅接受同源本地页面请求。")


async def _same_origin_qr(request: Request) -> None:
    """Protect the short-lived authentication image from cross-origin embedding."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if origin:
        await _same_origin(request)
    elif fetch_site != "same-origin":
        raise HTTPException(status_code=403, detail="仅接受同源本地页面请求。")
    if fetch_site and fetch_site != "same-origin":
        raise HTTPException(status_code=403, detail="仅接受同源本地页面请求。")


def _runner(request: Request) -> TaskRunner:
    runner = getattr(request.app.state, "workbench_runner", None)
    return runner if isinstance(runner, TaskRunner) else get_default_runner()


def _model_configuration_ready(
    runner: TaskRunner,
    *,
    required_kinds: set[str] | None = None,
    settings_override: dict[str, Any] | None = None,
) -> bool:
    if settings_override is not None:
        settings = settings_override
    else:
        settings = get_default_model_profile(runner.config) or runner.config.get_settings()
    provider_id = settings.get("model_provider")
    preset = get_provider_preset(provider_id)
    if preset is None:
        return False
    if preset.default_base_url is None:
        base_url = settings.get("model_base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            return False
    kinds = required_kinds or {"text"}
    if "text" in kinds and not isinstance(settings.get("model_id"), str):
        return False
    if "audio" in kinds:
        if not isinstance(settings.get("asr_model_id"), str) or preset.transcription_mode == "unsupported":
            return False
    try:
        api_key = task_profile_api_key(settings) if isinstance(settings.get("credential_id"), str) else get_model_api_key(str(provider_id))
        return bool(api_key)
    except CredentialStoreUnavailable:
        return False


def _task_model_configuration_ready(
    runner: TaskRunner,
    task_id: str,
    *,
    required_kinds: set[str] | None = None,
) -> bool:
    return _model_configuration_ready(
        runner,
        required_kinds=required_kinds,
        settings_override=task_model_settings(runner.config, task_id),
    )


def _validate_step_consents(
    runner: TaskRunner,
    requested_steps: list[str],
    consents: list[StepConsentRequest],
    *, transcription_mode: str = 'cloud', ocr_mode: str = 'cloud',
    profile: dict[str, Any] | None = None,
) -> list[StepConsentRequest]:
    model_step_kinds = {
        StepKind.TRANSCRIBE.value: "audio",
        StepKind.OCR.value: "text",
        StepKind.ANALYZE_COMMENTS.value: "text",
        StepKind.ANALYZE_STRUCTURE.value: "text",
        StepKind.KNOWLEDGE_CARDS.value: "text",
        StepKind.GENERATE_SCRIPTS.value: "text",
    }
    requested_model_steps = [step for step in requested_steps if step in model_step_kinds
                             and not (step == StepKind.TRANSCRIBE.value and transcription_mode == 'local')
                             and not (step == StepKind.OCR.value and ocr_mode == 'local')]
    if len(set(requested_steps)) != len(requested_steps):
        raise HTTPException(status_code=422, detail="任务步骤不能重复。")
    if not requested_model_steps:
        if consents:
            raise HTTPException(status_code=422, detail="当前任务没有云端步骤，不能提交模型授权记录。")
        return []
    settings = {**profile, "model_provider": profile.get("provider_id")} if profile is not None else runner.config.get_settings()
    provider_id = settings.get("model_provider")
    if profile is None:
        raise HTTPException(status_code=422, detail="请先选择一个已保存的模型配置档。")
    preset = get_provider_preset(provider_id)
    required_kinds = {model_step_kinds[step] for step in requested_model_steps}
    if "audio" in required_kinds and preset is not None and preset.transcription_mode == "unsupported":
        raise HTTPException(status_code=409, detail="当前服务商没有接入兼容的语音转写端点；请更换服务商或仅选择已支持的处理步骤。")
    if not _model_configuration_ready(runner, required_kinds=required_kinds, settings_override=settings):
        raise HTTPException(status_code=409, detail="所选模型配置档缺少必需的模型 ID、服务地址或 API Key，请先在设置中补全。")
    text_model_id = settings.get("model_id")
    if isinstance(text_model_id, str):
        for step_kind in requested_model_steps:
            if step_kind == StepKind.TRANSCRIBE.value:
                continue
            modality = "image" if step_kind == StepKind.OCR.value else "text"
            capability, _, _ = lookup_model_capability(str(provider_id), text_model_id)
            if capability is not None and not capability.supports(modality):
                label = "图像输入" if modality == "image" else "文本输入"
                raise HTTPException(status_code=409, detail=f"模型目录显示当前模型不支持{label}，请修改模型 ID 或选择其他模型。")
    required_immediate_steps = [step for step in requested_model_steps if step != StepKind.OCR.value]
    consent_by_step = {consent.step_kind: consent for consent in consents}
    if len(consent_by_step) != len(consents) or set(consent_by_step) != set(required_immediate_steps):
        raise HTTPException(status_code=422, detail="语音与文本模型步骤需要提交授权。")
    expected_provider = settings.get("model_provider")
    for step_kind in required_immediate_steps:
        consent = consent_by_step[step_kind]
        expected_model = (
            settings.get("asr_model_id") if step_kind == StepKind.TRANSCRIBE.value else settings.get("model_id")
        )
        if (
            not consent.authorized
            or consent.provider_id != expected_provider
            or consent.model_id != expected_model
            or consent.artifact_ids
        ):
            raise HTTPException(status_code=422, detail="模型服务、模型 ID 或授权材料范围与本机设置不一致。")
    records = [consent_by_step[step] for step in required_immediate_steps]
    if StepKind.OCR.value in requested_model_steps:
        records.append(StepConsentRequest(step_kind=StepKind.OCR.value, provider_id=str(provider_id),
                                          model_id=str(text_model_id), artifact_ids=[], authorized=True))
    return records


def _save_task_consents(
    runner: TaskRunner,
    task_id: str,
    consents: list[StepConsentRequest],
) -> None:
    if not consents:
        return
    now = datetime.now(timezone.utc).isoformat()
    with runner.database.transaction(write=True) as connection:
        for consent in consents:
            connection.execute(
                """INSERT INTO consents (
                       id, task_id, step_kind, provider_id, model_id, artifact_ids_json,
                       authorized, authorized_at
                   ) VALUES (?, ?, ?, ?, ?, '[]', 1, ?)""",
                (
                    uuid.uuid4().hex,
                    task_id,
                    consent.step_kind,
                    consent.provider_id,
                    consent.model_id,
                    now,
                ),
            )


@_router.get("/health", dependencies=[Depends(_same_origin)])
def health(request: Request) -> dict[str, object]:
    runner = _runner(request)
    model_ready = _model_configuration_ready(runner)
    asr_ready = _model_configuration_ready(runner, required_kinds={"audio"})
    settings = get_default_model_profile(runner.config) or runner.config.get_settings()
    preset = get_provider_preset(settings.get("model_provider"))
    return {
        "status": "ok",
        "mode": "local-anonymous",
        "archive_root_configured": runner.config.archive_root is not None,
        "cloud_model_configured": model_ready,
        "asr_model_configured": asr_ready,
        "model_provider": settings.get("model_provider"),
        "model_provider_name": preset.label if preset else None,
        "model_id": settings.get("model_id"),
        "asr_model_id": settings.get("asr_model_id"),
        "local_asr_device": settings.get("local_asr_device", "auto"),
        "worker_count": 1,
    }


@_router.get('/local-asr/models', dependencies=[Depends(_same_origin)])
def local_asr_status() -> dict:
    return local_asr.status()


@_router.post('/local-asr/install', status_code=202, dependencies=[Depends(_same_origin)])
def local_asr_install() -> dict:
    try:
        local_asr.start_installation()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'started': True}


@_router.get('/local-ocr/status', dependencies=[Depends(_same_origin)])
def local_ocr_status() -> dict:
    return local_ocr.status()


@_router.post('/local-asr/models/preview', dependencies=[Depends(_same_origin)])
def local_asr_preview(body: LocalModelRequest) -> dict:
    try:
        return local_asr.preview_model(body.model)
    except Exception as exc:
        raise HTTPException(status_code=503, detail='无法获取模型下载信息，请检查网络后重试。') from exc


@_router.post('/local-asr/models/prepare', status_code=202, dependencies=[Depends(_same_origin)])
def local_asr_prepare(body: LocalModelDownloadRequest) -> dict:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail='请先确认模型下载信息。')
    try:
        local_asr.start_download(body.model, body.revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'started': True}


@_router.get("/platforms", dependencies=[Depends(_same_origin)])
def platforms() -> list[dict[str, object]]:
    from .downloads import get_platform_status

    try:
        statuses = get_platform_status()
    except RecursionError:
        statuses = []
    return [
        {
            "id": item.id,
            "name": item.name,
            "validation_status": _safe_error(item.validation_status),
            "checked_at": item.checked_at,
            "summary": _safe_error(item.summary) if item.summary else None,
        }
        for item in statuses
    ]


@_router.get("/platform-accounts", dependencies=[Depends(_same_origin)])
def platform_accounts() -> dict[str, object]:
    return {"platforms": get_platform_account_manager().all_statuses()}


@_router.post("/platform-accounts/{platform_id}/login", dependencies=[Depends(_same_origin)])
def open_platform_login(platform_id: str) -> dict[str, object]:
    try:
        return get_platform_account_manager().start_login(platform_id)
    except PlatformAccountError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@_router.delete("/platform-accounts/{platform_id}", dependencies=[Depends(_same_origin)])
def clear_platform_login(platform_id: str) -> dict[str, object]:
    try:
        return get_platform_account_manager().clear_account(platform_id)
    except PlatformAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@_router.post("/folders/select", dependencies=[Depends(_same_origin)])
def select_local_folder(body: FolderPickerRequest, request: Request) -> dict[str, str | None]:
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="仅接受本机页面发起的选择请求。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请求格式无效。")
    try:
        selected = choose_local_directory()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="无法打开文件夹选择窗口，请检查本机桌面环境并重试。") from exc
    return {"path": selected}


@_router.get("/config", dependencies=[Depends(_same_origin)])
def get_config(request: Request) -> dict[str, object]:
    runner = _runner(request)
    settings = runner.config.get_settings()
    provider_id = settings.get("model_provider")
    return {
        "settings": settings,
        "default_knowledge_card_prompt": DEFAULT_KNOWLEDGE_CARD_PROMPT,
        "model_providers": list_provider_presets(),
        "model_base_url_default": get_default_base_url(provider_id),
        "directory_conflicts": runner.config.get_directory_conflicts(),
        "credential": _credential_status(str(provider_id or "zhipu")),
    }


@_router.put("/config", dependencies=[Depends(_same_origin)])
def put_config(body: WorkbenchSettingsRequest, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        settings_values = body.model_dump()
        for field in ("script_style_templates", "video_publish_cover_root", "article_image_root"):
            if field not in body.model_fields_set:
                settings_values.pop(field, None)
        saved = runner.config.update_settings(settings_values)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    media_cleanup = cleanup_expired_video_publish_media(runner.database)
    conflicts = saved.pop("directory_conflicts", [])
    provider_id = saved.get("model_provider")
    return {
        "settings": saved,
        "default_knowledge_card_prompt": DEFAULT_KNOWLEDGE_CARD_PROMPT,
        "model_providers": list_provider_presets(),
        "model_base_url_default": get_default_base_url(provider_id),
        "directory_conflicts": conflicts,
        "credential": _credential_status(str(provider_id or "zhipu")),
        "video_publish_media_cleanup": media_cleanup,
    }


@_router.get("/model-profiles", dependencies=[Depends(_same_origin)])
def get_model_profiles(request: Request) -> dict[str, object]:
    runner = _runner(request)
    ensure_legacy_image_profile(runner.config)
    return {"items": list_model_profiles(runner.config)}


def _save_model_profile_response(
    runner: TaskRunner,
    profile_id: str | None,
    body: ModelProfileRequest,
) -> dict[str, object]:
    try:
        return save_model_profile(
            runner.config,
            profile_id=profile_id,
            name=body.name,
            provider_id=body.provider_id,
            model_id=body.model_id,
            asr_model_id=body.asr_model_id,
            model_base_url=body.model_base_url,
            api_key=body.api_key,
            make_default=body.make_default,
            capabilities=body.capabilities,
            image_model_id=body.image_model_id,
            image_base_url=body.image_base_url,
            make_image_default=body.make_image_default,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="配置名称已存在，请使用其他名称。") from exc
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@_router.post("/model-profiles", status_code=201, dependencies=[Depends(_same_origin)])
def create_model_profile(body: ModelProfileRequest, request: Request) -> dict[str, object]:
    return _save_model_profile_response(_runner(request), None, body)


@_router.put("/model-profiles/{profile_id}", dependencies=[Depends(_same_origin)])
def update_model_profile(profile_id: str, body: ModelProfileRequest, request: Request) -> dict[str, object]:
    return _save_model_profile_response(_runner(request), profile_id, body)


@_router.post("/model-profiles/{profile_id}/migrate-legacy-key", dependencies=[Depends(_same_origin)])
def migrate_legacy_model_profile_credential(profile_id: str, request: Request) -> dict[str, object]:
    try:
        return migrate_legacy_model_profile_key(_runner(request).config, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@_router.put("/model-profiles/{profile_id}/default", dependencies=[Depends(_same_origin)])
def make_model_profile_default(profile_id: str, request: Request) -> dict[str, object]:
    try:
        return set_default_model_profile(_runner(request).config, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@_router.put("/model-profiles/{profile_id}/image-default", dependencies=[Depends(_same_origin)])
def make_image_model_profile_default(profile_id: str, request: Request) -> dict[str, object]:
    try:
        return set_default_image_model_profile(_runner(request).config, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@_router.delete("/model-profiles/{profile_id}", dependencies=[Depends(_same_origin)])
def delete_model_profile(profile_id: str, request: Request) -> dict[str, object]:
    try:
        return archive_model_profile(_runner(request).config, profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@_router.get("/models/capability", dependencies=[Depends(_same_origin)])
def model_capability(
    provider_id: str = Query(min_length=1, max_length=64),
    model_id: str = Query(min_length=1, max_length=128),
) -> dict[str, object]:
    if get_provider_preset(provider_id) is None:
        raise HTTPException(status_code=422, detail="模型服务商标识无效。")
    capability, catalog_available, display_name = lookup_model_capability(provider_id, model_id)
    verified = capability is not None
    modalities = sorted(capability.modalities) if capability else []
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "status": "verified" if verified else "unknown",
        "modalities": modalities,
        "display_name": display_name,
        "source": "local-known-model" if verified and not catalog_available else "models.dev" if verified else None,
        "message": (
            "已从模型目录识别输入能力。"
            if verified
            else "模型目录未收录或当前不可用；手动模型 ID 仍可保存，调用能力由服务商实际支持情况决定。"
        ),
    }


@_router.get("/credentials/model-api-key", dependencies=[Depends(_same_origin)])
def get_model_credential_status(
    request: Request,
    provider_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> dict[str, object]:
    selected_provider = provider_id
    if selected_provider is None:
        current = _runner(request).config.get_settings().get("model_provider")
        selected_provider = str(current or "zhipu")
    if get_provider_preset(selected_provider) is None:
        raise HTTPException(status_code=422, detail="模型服务商标识无效。")
    return _credential_status(selected_provider)


@_router.put("/credentials/model-api-key", dependencies=[Depends(_same_origin)])
def put_model_credential(
    body: ModelApiKeyRequest,
    provider_id: str = Query(default="zhipu", min_length=1, max_length=64),
) -> dict[str, object]:
    if get_provider_preset(provider_id) is None:
        raise HTTPException(status_code=422, detail="模型服务商标识无效。")
    try:
        set_model_api_key(body.api_key, provider_id)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _credential_status(provider_id)


@_router.delete("/credentials/model-api-key", dependencies=[Depends(_same_origin)])
def delete_model_credential(
    provider_id: str = Query(default="zhipu", min_length=1, max_length=64),
) -> dict[str, object]:
    if get_provider_preset(provider_id) is None:
        raise HTTPException(status_code=422, detail="模型服务商标识无效。")
    try:
        delete_model_api_key(provider_id)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _credential_status(provider_id)


@_router.get("/diagnostics", dependencies=[Depends(_same_origin)])
def diagnostics(request: Request) -> list[dict[str, object]]:
    from .diagnostics import run_diagnostics
    from .static import FRONTEND_DIST

    return run_diagnostics(_runner(request).config, FRONTEND_DIST)


@_router.post("/obsidian/references", dependencies=[Depends(_same_origin)])
def get_selected_reference_notes(body: ReferenceNotesRequest, request: Request) -> dict[str, object]:
    from .obsidian import list_reference_notes

    root_value = _runner(request).config.get_settings().get("obsidian_reference_root")
    if not isinstance(root_value, str) or not root_value:
        raise HTTPException(status_code=409, detail="请先配置知识库参考目录。")
    try:
        notes = list_reference_notes(Path(root_value), body.relative_paths)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    return {
        "items": [
            {
                "relative_path": note.relative_path,
                "title": note.title,
                "content": note.content,
                "sha256": note.sha256,
            }
            for note in notes
        ],
        "scope": "仅包含本次明确指定的 Markdown 文件",
    }


@_router.post("/obsidian/preview-write", dependencies=[Depends(_same_origin)])
def preview_obsidian_artifact(body: ArtifactVaultRequest, request: Request) -> dict[str, object]:
    from .obsidian import conflict_path_candidate, preview_vault_write

    runner = _runner(request)
    root_value = runner.config.get_settings().get("obsidian_output_root")
    if not isinstance(root_value, str) or not root_value:
        raise HTTPException(status_code=409, detail="请先配置知识库输出目录。")
    try:
        content, title = _vault_artifact_content(runner, body.artifact_id)
        preview = preview_vault_write(Path(root_value), body.relative_path, content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="指定的定稿或知识卡不存在。") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    suggested = conflict_path_candidate(Path(root_value), body.relative_path) if preview.conflict else None
    return {**preview.__dict__, "suggested_path": suggested, "title": title, "content": content}


@_router.post("/obsidian/write", dependencies=[Depends(_same_origin)])
def write_obsidian_artifact(body: ArtifactVaultWriteRequest, request: Request) -> dict[str, object]:
    from .obsidian import write_vault_note

    runner = _runner(request)
    root_value = runner.config.get_settings().get("obsidian_output_root")
    if not isinstance(root_value, str) or not root_value:
        raise HTTPException(status_code=409, detail="请先配置知识库输出目录。")
    try:
        content, _ = _vault_artifact_content(runner, body.artifact_id)
        result = write_vault_note(
            Path(root_value),
            body.relative_path,
            content,
            body.expected_sha256,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="指定的定稿或知识卡不存在。") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    if result.written:
        with runner.database.transaction(write=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO obsidian_writes (
                       id, artifact_id, output_root, relative_path, sha256, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    body.artifact_id,
                    str(Path(root_value).resolve(strict=True)),
                    result.path,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
    return result.__dict__


@_router.post("/search", dependencies=[Depends(_same_origin)])
def search_workbench(body: SearchRequest, request: Request) -> dict[str, object]:
    from .steps.search_documents import search_documents

    runner = _runner(request)
    archive_root = runner.config.archive_root
    if archive_root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    try:
        index_result, hits, total = search_documents(
            runner.database,
            archive_root,
            body.query,
            limit=body.limit,
            offset=body.offset,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    semantic_fallback = None
    if body.use_semantic:
        semantic_fallback = "向量检索尚未配置；已使用本地全文检索。"
    return {
        "items": [hit.__dict__ for hit in hits],
        "limit": body.limit,
        "offset": body.offset,
        "total": total,
        "method": "full_text",
        "scope": "当前归档目录内已登记的 Markdown/文本产物，以及具有匹配归档媒体登记的素材标题、作者和来源 ID",
        "index": index_result,
        "semantic_fallback": semantic_fallback,
    }


@_router.post("/diagnostics/model-connection", dependencies=[Depends(_same_origin)])
def model_connection_diagnostic(
    request: Request,
    body: ModelConnectionRequest,
) -> dict[str, object]:
    profile: dict[str, Any] | None = None
    if body.profile_id:
        profile = get_model_profile(_runner(request).config, body.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="模型配置不存在。")
    model_provider = str(profile["provider_id"]) if profile else body.model_provider
    model_id = str(profile["model_id"]) if profile else body.model_id
    model_base_url = profile.get("model_base_url") if profile else body.model_base_url
    if not isinstance(model_provider, str) or not isinstance(model_id, str):
        raise HTTPException(status_code=422, detail="请选择一个模型配置档或填写服务商和模型 ID。")
    preset = get_provider_preset(model_provider)
    if preset is None:
        raise HTTPException(status_code=422, detail="模型服务商标识无效。")
    try:
        configured_api_key = profile_api_key(profile) if profile else get_model_api_key(model_provider)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not configured_api_key:
        raise HTTPException(status_code=409, detail="请先将此配置档的 API Key 保存到 Windows 凭据管理器。")

    try:
        base_url = _validate_model_base_url(model_base_url)
        if preset.default_base_url is None and not base_url:
            raise HTTPException(status_code=422, detail="自定义 OpenAI 兼容服务需要填写 API 根地址。")
        provider = create_provider(
            configured_api_key,
            base_url=base_url,
            provider_id=model_provider,
        )
        result = provider.analyze_text(
            "你正在执行连通性检查。只回复 OK。",
            "请回复 OK。",
            model_id,
        )
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="本机系统凭据存储不可用。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    except ProviderError as exc:
        code = getattr(exc, "code", "provider_error")
        messages = {
            "authentication_failed": "模型服务商 API Key 无效或已失效。",
            "rate_limited": "模型服务暂时达到请求限制。",
            "provider_unavailable": "模型服务暂时不可用。",
            "provider_timeout": "连接测试超时；请求是否计费未知。",
            "provider_network_error": "无法连接所选模型服务。",
            "provider_not_configured": "本机模型配置无效。",
            "invalid_provider_response": "服务商返回了无法识别的连接测试结果。",
        }
        raise HTTPException(status_code=502, detail=messages.get(code, "模型连接检查失败。")) from None
    return {
        "status": "available",
        "message": f"已收到 {preset.label} / {model_id} 响应。连接检查只发送固定测试提示，不包含本机文件或任务材料；费用未知。",
        "request_id": result.request_id,
    }


@_router.post("/sources/preview", dependencies=[Depends(_same_origin)])
def preview_source(body: SourcePreviewRequest) -> dict[str, object]:
    """Identify a supported share URL without following redirects or making network requests."""
    try:
        source = _parse_supported_source(body.source_text)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_source_validation_message(exc)) from exc
    is_short = bool(source.is_short)
    return {
        "platform": source.platform,
        "platform_name": PLATFORMS[source.platform],
        "source_id": source.media_id,
        "is_short": is_short,
        "requires_resolution": is_short,
        "message": (
            "已识别短链接；提交后由下载步骤按平台规则解析。此预览未访问外部网络。"
            if is_short
            else "已识别作品链接；此预览未访问外部网络。"
        ),
    }


@_router.post("/uploads", status_code=201, dependencies=[Depends(_same_origin)])
async def upload_media(
    file: UploadFile = File(...),
    confirm_over_limit: bool = Form(default=False),
) -> dict[str, object]:
    """Stream a validated local media file into private task staging."""
    try:
        receipt = await save_upload(file, confirm_over_limit=confirm_over_limit)
    except UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法写入本机上传暂存目录。") from exc
    finally:
        await file.close()
    return receipt.to_payload()


@_router.delete("/uploads/{upload_id}", dependencies=[Depends(_same_origin)])
def discard_upload(upload_id: str, request: Request) -> dict[str, bool]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        rows = connection.execute(
            "SELECT source_json FROM tasks WHERE status != 'completed'"
        ).fetchall()
    for row in rows:
        try:
            source = json.loads(row["source_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(source, dict) and source.get("value") == f"workbench-upload:{upload_id}":
            raise HTTPException(status_code=409, detail="该上传文件已关联到未完成任务，暂时不能移除。")
    if not delete_upload(upload_id):
        raise HTTPException(status_code=404, detail="上传文件不存在或已清理。")
    return {"deleted": True}


@_router.post("/collection/local-media/select", dependencies=[Depends(_same_origin)])
def select_collection_media(body: CollectionMediaSelectRequest, request: Request) -> dict[str, object]:
    """Open the native picker and register the original media path without copying it."""
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="仅接受本机页面发起的选择请求。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请求格式无效。")
    try:
        selected = choose_local_media()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="无法打开文件选择窗口，请检查本机桌面环境。") from exc
    if not selected:
        return {"source": None}
    try:
        source = register_collection_media_source(
            _runner(request).database,
            Path(selected),
            confirm_over_limit=body.confirm_over_limit,
        )
    except CollectionMediaError as exc:
        if exc.code == "over_limit":
            return {"source": None, "overLimit": True, "sizeBytes": exc.size_bytes}
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    return {"source": source}


@_router.delete("/collection/local-media/{source_id}", dependencies=[Depends(_same_origin)])
def discard_collection_media(source_id: str, request: Request) -> dict[str, bool]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        rows = connection.execute(
            "SELECT source_json FROM tasks WHERE status != 'completed'"
        ).fetchall()
    for row in rows:
        try:
            source = json.loads(row["source_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(source, dict) and source.get("value") == f"workbench-local-media:{source_id}":
            raise HTTPException(status_code=409, detail="该文件已关联到未完成任务，暂时不能移除。")
    return {"deleted": delete_collection_media_source(runner.database, source_id)}


@_router.post("/tasks", status_code=202, dependencies=[Depends(_same_origin)])
def create_task(body: CreateTaskRequest, request: Request) -> dict[str, object]:
    runner = _runner(request)
    requested = body.steps
    script_style_snapshot: dict[str, object] | None = None
    if StepKind.GENERATE_SCRIPTS.value in requested:
        try:
            script_style_snapshot = build_script_style_snapshot(
                runner.config.get_settings(), body.script_style_ids
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    elif body.script_style_ids:
        raise HTTPException(status_code=422, detail="未选择生成口播稿时，不能提交风格模板选择。")
    model_profile = get_model_profile(runner.config, body.model_profile_id) if body.model_profile_id else None
    if body.model_profile_id and model_profile is None:
        raise HTTPException(status_code=422, detail="所选模型配置档不存在或已删除，请重新选择。")
    validated_consents = _validate_step_consents(
        runner,
        requested,
        body.consents,
        transcription_mode=body.transcription_mode,
        ocr_mode=body.ocr_mode,
        profile=model_profile,
    )
    if body.ocr_mode == 'local' and StepKind.OCR.value in requested and not local_ocr.status()['ready']:
        raise HTTPException(status_code=409, detail='本地 OCR 依赖或模型未准备完成，请重新安装项目依赖。')
    use_local = body.transcription_mode == 'local' and StepKind.TRANSCRIBE.value in requested
    if use_local:
        if not local_asr.status()['installed']:
            raise HTTPException(status_code=409, detail='本机转写组件尚未安装，请在设置中启用本机转写。')
        if not local_asr.model_ready(body.local_model):
            raise HTTPException(status_code=409, detail='所选本地模型尚未准备完成，请前往设置下载。')
    if body.upload_id is not None and body.local_source_id is not None:
        raise HTTPException(status_code=422, detail="本地导入只能选择一种媒体来源。")
    if body.upload_id is not None or body.local_source_id is not None:
        if body.platform_access_mode != "anonymous":
            raise HTTPException(status_code=422, detail="本地导入任务不使用平台账号。")
        if body.source_url is not None:
            raise HTTPException(status_code=422, detail="本地导入不能同时提供公开视频链接。")
        allowed_local = {
            StepKind.IMPORT.value, StepKind.TRANSCRIBE.value, StepKind.OCR.value,
            StepKind.ANALYZE_STRUCTURE.value, StepKind.KNOWLEDGE_CARDS.value,
            StepKind.GENERATE_SCRIPTS.value, StepKind.INDEX.value,
        }
        if not requested or requested[0] != StepKind.IMPORT.value or any(step not in allowed_local for step in requested) or len(set(requested)) != len(requested):
            raise HTTPException(status_code=422, detail="本地任务需要先导入媒体；文本分析、转写、OCR 和索引步骤需要符合依赖关系。")
        if any(step in requested for step in {StepKind.ANALYZE_STRUCTURE.value, StepKind.KNOWLEDGE_CARDS.value, StepKind.GENERATE_SCRIPTS.value}) and StepKind.TRANSCRIBE.value not in requested:
            raise HTTPException(status_code=422, detail="结构分析、知识卡和口播稿需要先选择语音转写。")
        if body.allow_first_frame:
            raise HTTPException(status_code=422, detail="本地导入不使用视频首帧回退选项。")
        try:
            asset, task, _receipt = runner.create_import_task(
                body.upload_id,
                local_source_id=body.local_source_id,
                step_kinds=[StepKind(step) for step in requested],
            )
            if script_style_snapshot is not None:
                save_task_script_style_snapshot(runner.database, task.id, script_style_snapshot)
            if model_profile is not None and validated_consents:
                save_task_model_config(runner.database, task.id, model_profile)
            _save_task_consents(runner, task.id, validated_consents)
            if StepKind.OCR.value in requested:
                local_ocr.save_task_options(runner.database, task.id, mode=body.ocr_mode,
                                            interval=body.ocr_interval_seconds, scan_profile=body.ocr_scan_profile)
            if use_local:
                local_asr.save_task_options(runner.database, task.id, model=body.local_model,
                                            language=body.local_language, device=body.local_device)
            runner.enqueue(task.id)
        except UploadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RunnerError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="本地媒体无法创建导入任务，请重新选择文件。") from exc
        return _summary_payload(task, asset, runner.config.archive_root)
    allowed_url = {
        StepKind.DOWNLOAD.value, StepKind.COMMENTS.value, StepKind.ANALYZE_COMMENTS.value,
        StepKind.TRANSCRIBE.value, StepKind.OCR.value, StepKind.ANALYZE_STRUCTURE.value,
        StepKind.KNOWLEDGE_CARDS.value, StepKind.GENERATE_SCRIPTS.value, StepKind.INDEX.value,
    }
    if any(step not in allowed_url for step in requested):
        raise HTTPException(status_code=422, detail="存在未开放的处理步骤。")
    if not requested or requested[0] != StepKind.DOWNLOAD.value or len(set(requested)) != len(requested):
        raise HTTPException(status_code=422, detail="评论采集依赖下载步骤；请选择下载后再按需采集评论。")
    if StepKind.ANALYZE_COMMENTS.value in requested and StepKind.COMMENTS.value not in requested:
        raise HTTPException(status_code=422, detail="评论分析需要同时选择公开评论采集。")
    if any(step in requested for step in {StepKind.ANALYZE_STRUCTURE.value, StepKind.KNOWLEDGE_CARDS.value, StepKind.GENERATE_SCRIPTS.value}) and StepKind.TRANSCRIBE.value not in requested:
        raise HTTPException(status_code=422, detail="结构分析、知识卡和口播稿需要先选择语音转写。")
    if len(requested) > 9:
        raise HTTPException(status_code=422, detail="URL 任务处理步骤数量超过上限。")
    if body.source_url is None:
        raise HTTPException(status_code=422, detail="请提供抖音或 B 站的公开作品链接或分享文案。")
    try:
        source = _parse_supported_source(body.source_url)
        platform_account_id = None
        platform_account_name = None
        if body.platform_access_mode == "account":
            try:
                platform_account, _cookies = resolve_video_publish_account(
                    runner.database, source.platform, body.platform_account_id
                )
            except PlatformAccountSelectionRequired as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except PlatformAccountError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            platform_account_id = str(platform_account["id"])
            platform_account_name = str(
                platform_account.get("profile_name")
                or platform_account.get("display_name")
                or platform_account.get("account_alias")
                or "视频账号"
            )
        safe_url = sanitize_source_url(source.url)
        asset, task = runner.create_download_task(
            safe_url,
            allow_first_frame=body.allow_first_frame,
            step_kinds=[StepKind(step) for step in requested],
            comment_sort_order=body.comment_sort_order,
        )
        if script_style_snapshot is not None:
            save_task_script_style_snapshot(runner.database, task.id, script_style_snapshot)
        if model_profile is not None and validated_consents:
            save_task_model_config(runner.database, task.id, model_profile)
        _save_task_consents(runner, task.id, validated_consents)
        if StepKind.OCR.value in requested:
            local_ocr.save_task_options(runner.database, task.id, mode=body.ocr_mode,
                                        interval=body.ocr_interval_seconds, scan_profile=body.ocr_scan_profile)
        if use_local:
            local_asr.save_task_options(runner.database, task.id, model=body.local_model,
                                        language=body.local_language, device=body.local_device)
        save_task_access_mode(
            runner.database,
            task.id,
            body.platform_access_mode,
            platform_account_id,
            platform_account_name,
        )
        runner.enqueue(
            task.id,
            transient_source_url=body.source_url,
            allow_first_frame=body.allow_first_frame,
        )
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_source_validation_message(exc)) from exc
    return _summary_payload(task, asset, runner.config.archive_root)


@_router.get("/tasks/{task_id}/comments", dependencies=[Depends(_same_origin)])
def task_comments(
    task_id: str,
    request: Request,
    sort: str = Query(default="likes_desc", pattern="^(likes_desc|time_desc|time_asc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    with runner.database.transaction() as connection:
        state_row = connection.execute(
            "SELECT * FROM comment_collection_state WHERE task_id = ?", (task_id,)
        ).fetchone()
        total_row = connection.execute(
            "SELECT count(*) AS total FROM comment_collection_items WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        order_by = {
            "likes_desc": "CASE WHEN like_count IS NULL THEN 1 ELSE 0 END, like_count DESC, created_at DESC, platform_comment_id ASC",
            "time_desc": "CASE WHEN created_at IS NULL THEN 1 ELSE 0 END, created_at DESC, platform_comment_id ASC",
            "time_asc": "CASE WHEN created_at IS NULL THEN 1 ELSE 0 END, created_at ASC, platform_comment_id ASC",
        }[sort]
        rows = connection.execute(
            f"""SELECT c.platform_comment_id, c.parent_id, c.author, c.text, c.like_count,
                       c.created_at, c.collected_at
                FROM comments AS c JOIN comment_collection_items AS ci ON ci.comment_id = c.id
                WHERE c.asset_id = ? AND ci.task_id = ?
                ORDER BY {order_by} LIMIT ? OFFSET ?""",
            (task.asset_id, task_id, limit, offset),
        ).fetchall()
    state: dict[str, object] | None = None
    if state_row is not None:
        try:
            missing = json.loads(state_row["missing_fields_json"])
        except (TypeError, json.JSONDecodeError):
            missing = {}
        if not isinstance(missing, dict):
            missing = {}
        state = {
            "status": str(state_row["status"]),
            "sort_order": str(state_row["sort_order"]),
            "page_count": int(state_row["page_count"]),
            "collected_count": int(state_row["collected_count"]),
            "missing_like_count": int(state_row["missing_like_count"]),
            "missing_fields": missing,
            "stop_reason": _safe_error(state_row["stop_reason"]) if state_row["stop_reason"] else None,
            "updated_at": state_row["updated_at"],
        }
    return {
        "items": [
            {
                "platform_comment_id": str(row["platform_comment_id"]),
                "parent_id": row["parent_id"],
                "author": _safe_error(row["author"]) if row["author"] else None,
                "text": _safe_error(row["text"]),
                "like_count": int(row["like_count"]) if row["like_count"] is not None else None,
                "created_at": row["created_at"],
                "collected_at": row["collected_at"],
            }
            for row in rows
        ],
        "state": state,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": int(total_row["total"]) if total_row is not None else 0,
        "scope": "本次采集到的部分公开评论",
    }


@_router.get("/tasks/{task_id}/comment-insights", dependencies=[Depends(_same_origin)])
def task_comment_insights(task_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    if runner.repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    with runner.database.transaction() as connection:
        row = connection.execute(
            """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'comment_analysis_json'
               ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="当前任务还没有评论分析结果。")
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    try:
        path = Path(str(row["path"]))
        if path.is_symlink():
            raise ValueError("symlink")
        safe_path = _assert_contained(path, root, strict=True)
        digest, size = _hash_file(safe_path)
        if not safe_path.is_file() or size > 25_000_000 or digest.casefold() != str(row["sha256"]).casefold():
            raise ValueError("changed")
        payload = json.loads(safe_path.read_text(encoding="utf-8-sig"))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=409, detail="评论分析产物不可用或哈希已变化。") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="评论分析产物结构无效。")
    return payload


@_router.post("/tasks/{task_id}/generate-scripts", status_code=202, dependencies=[Depends(_same_origin)])
def generate_scripts_from_selection(
    task_id: str,
    body: GenerateScriptsRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    if not body.authorized:
        raise HTTPException(status_code=422, detail="请先确认本次向所选服务商发送的稿件材料范围。")
    if len(set(body.comment_ids)) != len(body.comment_ids):
        raise HTTPException(status_code=422, detail="评论选择中不能包含重复 ID。")
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if not _task_model_configuration_ready(runner, task_id):
        raise HTTPException(status_code=409, detail="原任务所用模型配置或 API Key 当前不可用。")
    try:
        style_snapshot = (
            build_script_style_snapshot(runner.config.get_settings(), body.style_ids)
            if body.style_ids is not None
            else load_task_script_style_snapshot(runner.database, task_id)
        )
        if style_snapshot is None:
            style_snapshot = build_script_style_snapshot(
                runner.config.get_settings(), [], use_all_enabled_when_empty=True
            )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_safe_error(exc)) from exc
    analysis_artifact_id: str | None = None
    if body.comment_ids:
        try:
            analysis = task_comment_insights(task_id, request)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(status_code=422, detail="所选评论没有对应的精选分析结果。") from None
            raise
        selected = analysis.get("selected")
        valid_ids = {
            item.get("comment_id")
            for item in selected
            if isinstance(item, dict) and isinstance(item.get("comment_id"), str)
        } if isinstance(selected, list) else set()
        if any(comment_id not in valid_ids for comment_id in body.comment_ids):
            raise HTTPException(status_code=422, detail="稿件评论选择超出当前任务的已精选评论。")
        with runner.database.transaction() as connection:
            row = connection.execute(
                """SELECT id FROM artifacts WHERE task_id = ? AND kind = 'comment_analysis_json'
                   ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        analysis_artifact_id = str(row["id"]) if row is not None else None
    try:
        step_id = runner.enqueue_followup_scripts(
            task_id,
            transcript_artifact_id=body.transcript_artifact_id,
            comment_analysis_artifact_id=analysis_artifact_id,
            comment_ids=body.comment_ids,
            style_snapshot=style_snapshot,
        )
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="无法创建本次稿件生成任务。") from exc
    return {"step_id": step_id, "task": task_detail(task_id, request)["task"]}


@_router.post("/tasks/{task_id}/structure-analysis", dependencies=[Depends(_same_origin)])
def generate_structure_from_selection(task_id: str, body: StructureAnalysisRequest, request: Request) -> dict[str, object]:
    runner = _runner(request)
    if not body.authorized:
        raise HTTPException(status_code=422, detail="请先确认本次发送的文本范围。")
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    settings = task_model_settings(runner.config, task_id)
    if not _task_model_configuration_ready(runner, task_id):
        raise HTTPException(status_code=409, detail="原任务所用模型配置或 API Key 当前不可用。")
    if body.provider_id != settings.get("model_provider") or body.model_id != settings.get("model_id"):
        raise HTTPException(status_code=409, detail="模型配置已变化，请刷新后重新确认。")
    try:
        step_id = runner.enqueue_followup_structure(task_id, transcript_artifact_id=body.transcript_artifact_id, ocr_artifact_id=body.ocr_artifact_id, provider_id=body.provider_id, model_id=body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在。") from None
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"step_id": step_id, "task": task_detail(task_id, request)["task"]}


@_router.get("/articles", dependencies=[Depends(_same_origin)])
def list_articles_endpoint(
    request: Request,
    q: str = Query(default="", max_length=300),
    status: str = Query(default="all", pattern="^(all|draft|finalized|published)$"),
    platform: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    runner = _runner(request)
    try:
        return list_articles(runner.database, query=q, status=status, platform_id=platform, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.get("/article-source-scripts", dependencies=[Depends(_same_origin)])
def list_article_source_scripts(request: Request, q: str = Query(default="", max_length=300), limit: int = Query(default=100, ge=1, le=200)) -> dict[str, object]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        rows = connection.execute(
            """SELECT ar.id, ar.task_id, ar.version, ar.created_at, ar.metadata_json, a.title AS source_title
               FROM artifacts AS ar JOIN tasks AS t ON t.id = ar.task_id
               LEFT JOIN assets AS a ON a.id = t.asset_id
               WHERE ar.kind = 'final_script'
               ORDER BY ar.created_at DESC, ar.version DESC LIMIT ?""",
            (limit * 3,),
        ).fetchall()
    items: list[dict[str, object]] = []
    query_text = q.strip().casefold()
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        if metadata.get("finalized") is not True or metadata.get("human_reviewed") is not True:
            continue
        title = str(metadata.get("title") or "口播稿定稿")[:200]
        source_title = str(row["source_title"])[:300] if row["source_title"] else None
        if query_text and query_text not in title.casefold() and query_text not in str(source_title or "").casefold():
            continue
        items.append({"artifact_id": str(row["id"]), "task_id": str(row["task_id"]), "title": title, "source_title": source_title, "version": int(row["version"]), "created_at": str(row["created_at"])})
        if len(items) >= limit:
            break
    return {"items": items}


@_router.post("/articles", status_code=201, dependencies=[Depends(_same_origin)])
def create_manual_article(body: ArticleCreateRequest, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        if body.document is not None:
            return create_article(runner.database, body.document, allow_empty_body=True)
        return create_article(runner.database, default_document(body.title), allow_empty_body=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.post("/articles/from-script", status_code=201, dependencies=[Depends(_same_origin)])
def create_article_from_script(body: ArticleFromScriptRequest, request: Request) -> dict[str, object]:
    if not body.authorized:
        raise HTTPException(status_code=422, detail="请先确认本次向所选模型发送这篇定稿口播稿。")
    runner = _runner(request)
    with runner.database.transaction() as connection:
        source = connection.execute("SELECT * FROM artifacts WHERE id = ?", (body.artifact_id,)).fetchone()
    if source is None:
        raise HTTPException(status_code=404, detail="口播稿不存在。")
    metadata = _json_object(source["metadata_json"])
    if str(source["kind"]) != "final_script" or metadata.get("finalized") is not True or metadata.get("human_reviewed") is not True:
        raise HTTPException(status_code=409, detail="请先将口播稿审核并设为定稿，再生成博文。")
    task_id = str(source["task_id"])
    settings = task_model_settings(runner.config, task_id)
    if not _task_model_configuration_ready(runner, task_id):
        raise HTTPException(status_code=409, detail="该任务的文本模型配置或 API Key 当前不可用。")
    provider_id = settings.get("model_provider")
    model_id = settings.get("model_id")
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise HTTPException(status_code=409, detail="该任务没有可用的文本模型配置。")
    try:
        source_text = read_text_artifact(body.artifact_id, request)["content"]
    except HTTPException:
        raise
    except (TypeError, ValueError, OSError):
        raise HTTPException(status_code=409, detail="口播稿文件不可用或已变化。") from None

    prompt = (
        "你是中文文章编辑。把用户提供的已定稿口播稿转成适合公众号与博客平台阅读的文章。"
        "忠实保留原稿事实、数字、专有名词和作者意图；不能编造数据、案例、引用或经历。"
        "先判断 refine（结构需要重组）、polish（表达和阅读节奏需要润色）或 layout（只需排版），mode 必须只选这三个值中的一个。"
        "给出三个方向不同的标题、推荐标题、摘要、核心观点、判断原因、封面配图提示词和正文 blocks。"
        "每个主要正文模块最多给一个核心 paragraph 配图提示词，放在该 paragraph 的 image_prompt 字段；"
        "配图提示词说明主体、场景、构图、风格、色彩、画幅、安全区与无字无 Logo 等约束。"
        "正文段落适合手机阅读。outro 字段单独写回应开头问题的结束语和具体互动问题，blocks 正文中不要重复包含 outro。"
        "仅返回符合下列结构的 JSON，不要代码围栏或附加说明："
        '{"title":"推荐标题","title_candidates":["结果型","问题型","经验型"],"summary":"简介",'
        '"mode":"polish","analysis":{"core_point":"核心观点","reason":"判断依据"},'
        '"cover_image_prompt":"封面提示词","author":"老赵","published_at":"YYYY-MM-DD",'
        '"outro":"结束语","blocks":[{"id":"b1","type":"paragraph","text":"正文段落",'
        '"image_role":"module","image_prompt":"配图提示词"}]}. '
        "blocks 的 type 每次只能选 heading、paragraph、quote、code、bullet_list、ordered_list、divider、table 中的一种；"
        "列表使用 items，表格使用 headers 和 rows，标题块使用 level。"
    )
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    usage_json = json.dumps({"input_tokens": None, "audio_seconds": None, "image_count": None, "output_tokens": None, "input_detail": {"operation": "article_from_final_script", "source_artifact_ids": [body.artifact_id], "authorized": True}}, ensure_ascii=False, separators=(",", ":"))
    with runner.database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO model_runs (id, task_id, step_id, provider_id, model_id, artifact_ids_json,
                   request_id, usage_json, cost_json, status, error_code, created_at, finished_at)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, NULL)""",
            (run_id, task_id, provider_id, model_id, json.dumps([body.artifact_id]), run_id, usage_json,
             json.dumps({"currency": "CNY", "amount": None, "known": False}), now),
        )
    try:
        api_key = task_profile_api_key(settings)
        if not api_key:
            raise HTTPException(status_code=409, detail="请先在本机设置中保存当前模型配置的 API Key。")
        base_url = settings.get("model_base_url")
        provider = create_provider(api_key, base_url=base_url if isinstance(base_url, str) else None, provider_id=provider_id)
        if hasattr(provider, "set_client_request_id"):
            provider.set_client_request_id(run_id)
        response = provider.analyze_text(prompt, str(source_text), model_id)
        raw = str(response.text)
        from .steps.text_analysis_common import parse_json_object

        generated = normalize_document(parse_json_object(raw))
        article = create_article(runner.database, generated, source_task_id=task_id, source_artifact_id=body.artifact_id, note="由定稿口播稿生成")
        usage = getattr(response, "usage", None)
        if hasattr(usage, "__dataclass_fields__"):
            usage_value = {key: str(value) if hasattr(value, "as_tuple") else value for key, value in usage.__dict__.items()}
            usage_json = json.dumps({**usage_value, "input_detail": {"operation": "article_from_final_script", "source_artifact_ids": [body.artifact_id], "authorized": True}}, ensure_ascii=False, separators=(",", ":"))
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE model_runs SET request_id = ?, usage_json = ?, status = 'completed', finished_at = ? WHERE id = ?", (getattr(response, "request_id", None) or run_id, usage_json, datetime.now(timezone.utc).isoformat(), run_id))
        return article
    except HTTPException as exc:
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE model_runs SET status = 'failed', error_code = ?, finished_at = ? WHERE id = ?", (f"http_{exc.status_code}", datetime.now(timezone.utc).isoformat(), run_id))
        raise
    except ProviderError as exc:
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE model_runs SET status = 'failed', error_code = ?, finished_at = ? WHERE id = ?", (str(getattr(exc, "code", "provider_error"))[:64], datetime.now(timezone.utc).isoformat(), run_id))
        raise HTTPException(status_code=502, detail=_safe_error(str(exc)) or "文章生成模型调用失败。") from None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE model_runs SET status = 'failed', error_code = 'invalid_article_output', finished_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), run_id))
        raise HTTPException(status_code=502, detail=_safe_error(str(exc)) or "模型返回的文章结构无效。") from None
    except Exception:
        with runner.database.transaction(write=True) as connection:
            connection.execute("UPDATE model_runs SET status = 'failed', error_code = 'article_generation_failed', finished_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), run_id))
        raise HTTPException(status_code=502, detail="文章生成失败；请查看本地模型调用记录后再决定是否重试。") from None


@_router.get("/articles/{article_id}", dependencies=[Depends(_same_origin)])
def get_article_endpoint(article_id: str, request: Request) -> dict[str, Any]:
    article = get_article(_runner(request).database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    return article


@_router.put("/articles/{article_id}", dependencies=[Depends(_same_origin)])
def save_article_endpoint(article_id: str, body: ArticleSaveRequest, request: Request) -> dict[str, Any]:
    try:
        return save_article(_runner(request).database, article_id, body.expected_version, body.document)
    except KeyError:
        raise HTTPException(status_code=404, detail="文章不存在。") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.delete("/articles/{article_id}", dependencies=[Depends(_same_origin)])
def delete_article_endpoint(article_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    assets_root = (PROJECT_ROOT / "data" / "article-assets").resolve(strict=False)
    article_assets_path = assets_root / article_id
    if article_assets_path.is_symlink():
        raise HTTPException(status_code=400, detail="文章资源路径无效。")
    try:
        article_assets = article_assets_path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="文章资源路径无效。") from None
    if article_assets == assets_root:
        raise HTTPException(status_code=400, detail="文章资源路径无效。")
    try:
        article_assets.relative_to(assets_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="文章资源路径无效。") from None
    if article_assets.parent != assets_root or article_assets.name != article_id:
        raise HTTPException(status_code=400, detail="文章资源路径无效。")

    if not delete_article(runner.database, article_id):
        raise HTTPException(status_code=404, detail="文章不存在。")

    cleanup_warning = None
    with runner.database.transaction(write=True) as connection:
        images = connection.execute(
            "SELECT * FROM workbench_images WHERE kind = 'article_image' AND article_id = ?", (article_id,),
        ).fetchall()
        for image in images:
            try:
                remove_registered_image(image)
                connection.execute("DELETE FROM workbench_images WHERE id = ?", (image["id"],))
            except (OSError, ValueError, RuntimeError):
                cleanup_warning = "文章已删除，但部分图片暂时无法清理，请检查图片保存目录。"
    try:
        if article_assets.exists():
            if article_assets.is_dir():
                shutil.rmtree(article_assets)
            else:
                article_assets.unlink()
    except OSError:
        cleanup_warning = "文章已删除，但本地图片清理失败，请检查文章资源目录。"
    return {"deleted": True, "cleanup_warning": cleanup_warning}


@_router.post("/articles/{article_id}/finalize", dependencies=[Depends(_same_origin)])
def finalize_article_endpoint(article_id: str, request: Request) -> dict[str, Any]:
    try:
        article = finalize_article(_runner(request).database, article_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    return article


@_router.get("/article-platforms", dependencies=[Depends(_same_origin)])
def article_platform_statuses(
    request: Request,
    live_status: bool = Query(default=True),
) -> dict[str, object]:
    runner = _runner(request)
    return {
        "platforms": _article_platform_statuses(
            runner.config,
            runner.database,
            include_live_status=live_status,
        ),
    }


@_router.post("/article-platforms/weibo/login", dependencies=[Depends(_same_origin)])
def start_weibo_login_endpoint() -> dict[str, Any]:
    try:
        return start_weibo_login()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PlatformConnectionError as exc:
        raise HTTPException(status_code=409, detail=_safe_error(str(exc))) from None


@_router.post("/article-platforms/csdn/editor", dependencies=[Depends(_same_origin)])
def open_csdn_editor_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().open_workbench("csdn")
    except PlatformAccountError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@_router.post("/articles/{article_id}/csdn-tags", dependencies=[Depends(_same_origin)])
def lookup_csdn_article_tags(article_id: str, body: CsdnTagLookupRequest, request: Request) -> dict[str, Any]:
    article = get_article(_runner(request).database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    document = _article_platform_document(article_id, article["document"], "csdn")
    try:
        return get_platform_account_manager().get_csdn_tags(document, body.keyword.strip())
    except (PlatformAccountError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=_safe_error(str(exc)) or "无法读取 CSDN 标签，请检查登录状态后重试。") from exc


@_router.delete("/article-platforms/csdn/session", dependencies=[Depends(_same_origin)])
def clear_csdn_session_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().clear_account("csdn")
    except PlatformAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@_router.post("/article-platforms/toutiao/editor", dependencies=[Depends(_same_origin)])
def open_toutiao_editor_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().open_workbench("toutiao")
    except PlatformAccountError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@_router.post("/article-platforms/toutiao/profile", dependencies=[Depends(_same_origin)])
def ensure_toutiao_profile_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().ensure_toutiao_profile()
    except PlatformAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@_router.get("/article-platforms/toutiao/publish-options", dependencies=[Depends(_same_origin)])
def toutiao_publish_options_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().get_toutiao_publish_options()
    except PlatformAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@_router.delete("/article-platforms/toutiao/session", dependencies=[Depends(_same_origin)])
def clear_toutiao_session_endpoint() -> dict[str, object]:
    try:
        return get_platform_account_manager().clear_account("toutiao")
    except PlatformAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _article_platform_statuses(
    config: Any,
    database: Any,
    *,
    include_live_status: bool = True,
) -> list[dict[str, Any]]:
    statuses = platform_statuses(config, include_live_status=include_live_status)
    csdn = next((item for item in statuses if item.get("id") == "csdn"), None)
    if csdn is not None and include_live_status:
        try:
            session = get_platform_account_manager().account_status("csdn")
        except PlatformAccountError as exc:
            session = {
                "status": "error",
                "authenticated": False,
                "login_window_open": False,
                "message": str(exc),
            }
        connected = bool(session.get("authenticated"))
        session_status = str(session.get("status") or "error")
        csdn.update({
            "configured": connected,
            "connected": connected,
            "login_in_progress": bool(session.get("login_window_open")) and not connected,
            "connection_checked": session_status != "error",
            "accounts": [{
                "id": "csdn-local-profile",
                "name": str(session.get("profile_name") or "当前 CSDN 账号"),
                "public_value": str(session.get("profile_username") or ""),
                "credential_verified_at": None,
                "credential_verified": connected,
                "configured": connected,
                "secret_available": True,
                "status": "connected" if connected else "not_connected",
                "message": str(session.get("message") or ""),
                "auth_type": "browser",
                "avatar_url": session.get("profile_avatar_url"),
            }] if connected else [],
            "message": str(session.get("message") or "CSDN 登录状态尚未确认。"),
        })
    elif csdn is not None:
        csdn.update({
            "connected": False,
            "connection_checked": False,
            "connection_checking": True,
            "message": "正在检查 CSDN 登录状态。",
        })
    toutiao = next((item for item in statuses if item.get("id") == "toutiao"), None)
    if toutiao is not None and include_live_status:
        try:
            session = get_platform_account_manager().account_status("toutiao")
        except PlatformAccountError as exc:
            session = {
                "status": "error",
                "authenticated": False,
                "login_window_open": False,
                "message": str(exc),
            }
        connected = bool(session.get("authenticated"))
        session_status = str(session.get("status") or "error")
        toutiao.update({
            "configured": connected,
            "connected": connected,
            "login_in_progress": bool(session.get("login_window_open")) and not connected,
            "connection_checked": session_status != "error",
            "accounts": [{
                "id": "toutiao-local-profile",
                "name": str(session.get("profile_name") or "当前今日头条账号"),
                "public_value": "",
                "avatar_url": session.get("profile_avatar_url"),
                "credential_verified_at": None,
                "credential_verified": connected,
                "configured": connected,
                "secret_available": True,
                "status": "connected" if connected else "not_connected",
                "message": str(session.get("message") or ""),
                "auth_type": "browser",
            }] if connected else [],
            "message": str(session.get("message") or "今日头条登录状态尚未确认。"),
        })
    elif toutiao is not None:
        toutiao.update({
            "connected": False,
            "connection_checked": False,
            "connection_checking": True,
            "message": "正在检查今日头条登录状态。",
        })
    zhihu = next((item for item in statuses if item.get("id") == "zhihu"), None)
    if zhihu is not None:
        legacy_accounts = zhihu.get("accounts") if isinstance(zhihu.get("accounts"), list) else []
        legacy_accounts = [account for account in legacy_accounts if isinstance(account, dict)]
        for account in legacy_accounts:
            account.setdefault("auth_type", "openapi")
        linked_accounts = [
            item for item in list_video_publish_accounts(database)
            if item.get("platform_id") == "zhihu"
        ]
        accounts = [
            {
                "id": "browser:" + str(item["id"]),
                "name": item.get("profile_name") or item.get("display_name") or item.get("account_alias") or "知乎账号",
                "public_value": item.get("account_alias") or "",
                "avatar_url": item.get("avatar_url") or "",
                "credential_verified_at": item.get("checked_at"),
                "credential_verified": item.get("status") == "connected",
                "configured": item.get("status") == "connected",
                "secret_available": True,
                "status": item.get("status"),
                "message": item.get("message") or "",
                "auth_type": "browser",
            }
            for item in linked_accounts
        ]
        accounts = [*legacy_accounts, *accounts]
        configured = any(account.get("configured") for account in accounts)
        connected = any(
            account.get("configured") and (
                account.get("credential_verified") or account.get("status") == "connected"
            )
            for account in accounts
        )
        zhihu.update({
            "configured": configured,
            "connected": connected,
            "accounts": accounts,
            "secret_available": True,
            "message": "可使用 SAU 浏览器登录发布文章；已保存的知乎 OpenAPI 账号仍按原接口发布。",
        })
    baijiahao = next((item for item in statuses if item.get("id") == "baijiahao"), None)
    if baijiahao is not None:
        linked_accounts = [
            item for item in list_video_publish_accounts(database)
            if item.get("platform_id") == "baijiahao"
        ]
        accounts = [
            {
                "id": item["id"],
                "name": item.get("profile_name") or item.get("display_name") or item.get("account_alias") or "百家号账号",
                "public_value": item.get("account_alias") or "",
                "avatar_url": item.get("avatar_url") or "",
                "credential_verified_at": item.get("checked_at"),
                "credential_verified": item.get("status") == "connected",
                "configured": item.get("status") == "connected",
                "secret_available": True,
                "status": item.get("status"),
                "message": item.get("message") or "",
            }
            for item in linked_accounts
        ]
        connected = any(account["configured"] for account in accounts)
        baijiahao.update({
            "configured": connected,
            "connected": connected,
            "accounts": accounts,
            "message": "可使用 SAU 扫码登录并检查百家号账号；文章仍需在官方编辑页手动发布。",
        })
    xiaohongshu = next((item for item in statuses if item.get("id") == "xiaohongshu"), None)
    if xiaohongshu is None:
        return statuses
    linked_accounts = [
        item for item in list_video_publish_accounts(database)
        if item.get("platform_id") == "xiaohongshu"
    ]
    accounts = [
        {
            "id": item["id"],
            "name": item.get("profile_name") or item.get("display_name") or item.get("account_alias") or "小红书账号",
            "public_value": item.get("account_alias") or "",
            "avatar_url": item.get("avatar_url") or "",
            "credential_verified_at": item.get("checked_at"),
            "credential_verified": item.get("status") == "connected",
            "configured": item.get("status") == "connected",
            "secret_available": True,
            "status": item.get("status"),
            "message": item.get("message") or "",
        }
        for item in linked_accounts
    ]
    connected = any(account["configured"] for account in accounts)
    xiaohongshu.update({
        "configured": connected,
        "connected": connected,
        "accounts": accounts,
        "message": "文章发布与视频发布共用小红书账号。文章会将封面与正文配图整理为图文笔记；提交后需到创作中心核对结果。",
    })
    return statuses


@_router.get("/article-platforms/public-ip", dependencies=[Depends(_same_origin)])
def article_platform_public_ip() -> dict[str, str]:
    request = urllib.request.Request(
        "https://myip.ipip.net/",
        headers={"Accept": "text/plain", "User-Agent": "LocalVideoWorkbench/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(2049)
        if len(raw) > 2048:
            raise HTTPException(status_code=502, detail="公网 IP 查询服务返回内容过长。")
        text = raw.decode("utf-8")
        match = re.search(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text)
        address = ipaddress.ip_address(match.group(0)) if match else None
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=502, detail="无法自动识别公网 IP；请检查网络后重试，或手动查询本机公网出口 IP。") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise HTTPException(status_code=502, detail="查询结果不是可加入白名单的公网 IPv4 地址；请手动确认本机出口 IP。")
    return {"ip": str(address)}


@_router.put("/article-platforms/{platform_id}", dependencies=[Depends(_same_origin)])
def save_article_platform_account(platform_id: str, body: ArticlePlatformAccountRequest, request: Request) -> dict[str, Any]:
    try:
        return save_platform_account(_runner(request).config, platform_id, public_value=body.public_value, secret_value=body.secret_value, clear_secret=body.clear_secret)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.post("/article-platforms/{platform_id}/accounts", dependencies=[Depends(_same_origin)])
def add_article_platform_account(platform_id: str, body: ArticlePlatformAccountRequest, request: Request) -> dict[str, Any]:
    secret_value = (body.secret_value or "").strip()
    if not secret_value:
        raise HTTPException(status_code=422, detail="请填写该账号的 App Secret。")
    try:
        credential_verified_at = verify_wechat_credentials(body.public_value, secret_value) if platform_id == "wechat" else None
        account_name = (body.account_name or "").strip()
        if not account_name:
            raise ValueError("请填写账号名称。")
        return add_platform_account(
            _runner(request).config, platform_id,
            account_name=account_name, public_value=body.public_value,
            secret_value=secret_value,
            credential_verified_at=credential_verified_at,
        )
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(str(exc))) from None


@_router.put("/article-platforms/{platform_id}/accounts/{account_id}", dependencies=[Depends(_same_origin)])
def update_article_platform_account(platform_id: str, account_id: str, body: ArticlePlatformAccountRequest, request: Request) -> dict[str, Any]:
    try:
        credential_verified_at = None
        account_name = (body.account_name or "").strip()
        if platform_id == "wechat":
            config = _runner(request).config
            stored_app_id, stored_secret = account_credentials(config, "wechat", account_id)
            if body.public_value.strip() != stored_app_id and not (body.secret_value or "").strip():
                raise ValueError("更换 AppID 时，请同时填写对应的新 AppSecret。")
            credential_verified_at = verify_wechat_credentials(
                body.public_value,
                (body.secret_value or "").strip() or stored_secret,
            )
        if not account_name:
            raise ValueError("请填写账号名称。")
        return update_platform_account(
            _runner(request).config, platform_id, account_id,
            account_name=account_name, public_value=body.public_value,
            secret_value=(body.secret_value or "").strip() or None,
            credential_verified_at=credential_verified_at,
        )
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(str(exc))) from None


@_router.post("/article-platforms/wechat/accounts/{account_id}/verify", dependencies=[Depends(_same_origin)])
def verify_wechat_article_account_endpoint(account_id: str, request: Request) -> dict[str, Any]:
    try:
        return verify_wechat_account_credentials(_runner(request).config, account_id)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(str(exc))) from None


@_router.delete("/article-platforms/{platform_id}/accounts/{account_id}", dependencies=[Depends(_same_origin)])
def delete_article_platform_account(platform_id: str, account_id: str, request: Request) -> dict[str, Any]:
    try:
        return remove_platform_account(_runner(request).config, platform_id, account_id)
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.get("/article-image-settings", dependencies=[Depends(_same_origin)])
def article_image_settings(request: Request) -> dict[str, Any]:
    try:
        return get_image_config(_runner(request).config)
    except CredentialStoreUnavailable:
        return {"base_url": None, "model_id": "", "provider_id": None, "api_key_configured": False, "credential_available": False, "configured": False}


@_router.put("/article-image-settings", dependencies=[Depends(_same_origin)])
def update_article_image_settings(body: ArticleImageSettingsRequest, request: Request) -> dict[str, Any]:
    try:
        return save_image_config(
            _runner(request).config,
            base_url=body.base_url,
            model_id=body.model_id,
            api_key=body.api_key,
            clear_api_key=body.clear_api_key,
        )
    except CredentialStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _article_vector_image_context(
    document: dict[str, Any],
    *,
    target: str,
    block_index: int | None,
) -> str:
    parts: list[str] = []
    title = str(document.get("title") or "").strip()
    summary = str(document.get("summary") or "").strip()
    analysis = document.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    if title:
        parts.append(f"文章标题：{title}")
    if summary:
        parts.append(f"文章摘要：{summary}")
    core_point = str(analysis.get("core_point") or "").strip()
    if core_point:
        parts.append(f"文章核心观点：{core_point}")

    blocks = document.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    if target == "cover":
        body = str(document.get("body_markdown") or "").strip()
        if not body:
            body = blocks_to_markdown([block for block in blocks if isinstance(block, dict)])
        if body:
            parts.append(f"文章正文：\n{body}")
    elif block_index is not None and 0 <= block_index < len(blocks):
        heading = ""
        for candidate in blocks[:block_index]:
            if isinstance(candidate, dict) and candidate.get("type") == "heading":
                heading = str(candidate.get("text") or "").strip()
        if heading:
            parts.append(f"本段所属小标题：{heading}")
        focus = blocks[block_index]
        focus_text = str(focus.get("text") or "").strip() if isinstance(focus, dict) else ""
        if focus_text:
            parts.append(f"配图对应段落：{focus_text[:4_000]}")
        related: list[str] = []
        for index in (block_index - 1, block_index + 1):
            if not 0 <= index < len(blocks):
                continue
            block = blocks[index]
            if not isinstance(block, dict) or block.get("type") not in {"heading", "paragraph", "quote"}:
                continue
            text = str(block.get("text") or "").strip()
            if text:
                related.append(text[:800])
        if related:
            parts.append("相邻上下文：\n" + "\n".join(related))
    return "\n\n".join(parts)[:8_000]


@_router.post("/articles/{article_id}/images/generate", dependencies=[Depends(_same_origin)])
def generate_article_image(article_id: str, body: ArticleImageGenerationRequest, request: Request) -> dict[str, Any]:
    if not body.confirmed:
        detail = "请先确认本次文本模型调用及可能产生的费用。" if body.mode == "vector" else "请先确认本次图片模型调用及服务商费用。"
        raise HTTPException(status_code=422, detail=detail)
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    document = dict(article["document"])
    block_index: int | None = None
    if body.target == "cover":
        prompt = str(document.get("cover_image_prompt") or "").strip()
    else:
        block_index = next((index for index, block in enumerate(document.get("blocks", [])) if isinstance(block, dict) and str(block.get("id")) == str(body.block_id)), None)
        if block_index is None:
            raise HTTPException(status_code=404, detail="正文配图位置不存在，请刷新文章后重试。")
        prompt = str(document["blocks"][block_index].get("image_prompt") or "").strip()
    if body.mode == "provider" and not prompt:
        raise HTTPException(status_code=422, detail="请先填写配图提示词。")
    try:
        if body.mode == "vector":
            context = _article_vector_image_context(document, target=body.target, block_index=block_index)
            image_bytes, metadata = generate_vector_illustration(
                runner.config,
                prompt,
                target=body.target,
                context=context,
            )
            suffix = ".png"
        else:
            image_settings = get_image_config(runner.config)
            if not image_settings["configured"]:
                raise HTTPException(status_code=409, detail="请先在设置 → 模型与转写中配置图片模型地址、模型 ID 和 API Key。")
            image_bytes, suffix, metadata = generate_image(runner.config, prompt)
    except VectorIllustrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_safe_error(str(exc))) from None
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(str(exc)) or "默认文本模型调用失败，请稍后重试。") from None
    except (ArticleImageError, CredentialStoreUnavailable) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    note = (
        ("本地知识图解封面" if body.target == "cover" else "本地知识图解正文配图")
        if body.mode == "vector"
        else ("生成封面配图" if body.target == "cover" else "生成正文配图")
    )
    try:
        updated, filename = save_article_image(
            runner.database, article, image_bytes, suffix, name=note + suffix,
            model_id=metadata["model_id"], generated_at=metadata["generated_at"], block_index=block_index, note=note,
        )
    except OSError:
        raise HTTPException(status_code=500, detail="无法写入文章图片目录，请检查目录设置。") from None
    except (KeyError, RuntimeError, ValueError) as exc:
        status = 404 if isinstance(exc, KeyError) else 409 if isinstance(exc, RuntimeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return {"article": updated, "image_url": f"/api/v1/articles/{article_id}/images/{filename}", "model_id": metadata["model_id"], "request_id": metadata["request_id"] or None, "cost_known": False}


@_router.post("/articles/{article_id}/publish", dependencies=[Depends(_same_origin)])
def publish_article_endpoint(
    article_id: str,
    body: ArticlePublishRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请确认要将文章发送到所选平台。")
    if body.platforms and body.targets:
        raise HTTPException(status_code=422, detail="请使用平台列表或账号目标其中一种发布方式。")
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    if article["status"] != "finalized":
        raise HTTPException(status_code=409, detail="请先将文章设为定稿，再进行发布。")
    resolved_targets = _resolve_article_publish_targets(body, runner.config, runner.database)
    if not resolved_targets:
        raise HTTPException(status_code=422, detail="请选择至少一个发布账号或平台。")
    if len(resolved_targets) > 20:
        raise HTTPException(status_code=422, detail="一次最多选择 20 个发布目标。")
    keys = [(platform_id, account_id) for platform_id, account_id, _, _ in resolved_targets]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="同一发布账号不能重复选择。")

    document = dict(article["document"])
    if any(platform_id == "wechat" and not (options.get("cover_image_path") or document.get("cover_image_path")) for platform_id, _, _, options in resolved_targets):
        raise HTTPException(status_code=422, detail="微信公众号发布需要封面图，请先在文章编辑页上传封面。")
    for platform_id, _, _, options in resolved_targets:
        cover_ref = options.get("cover_image_path")
        if cover_ref and resolve_article_image(runner.database, article_id, cover_ref, project_root=PROJECT_ROOT) is None:
            raise HTTPException(status_code=422, detail="平台封面不可用或不属于当前文章，请重新选择封面。")
        if platform_id == "csdn" and ("tags" in options or "csdn" in options):
            try:
                prepare_csdn_publication(_article_platform_document(article_id, document, platform_id, options), options.get("tags") or [])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
        if platform_id == "toutiao" and "toutiao" in options:
            try:
                from .toutiao_publishing import validate_toutiao_cover_selection, validate_toutiao_title
                validate_toutiao_title(str(options.get("title") or document.get("title") or ""))
                references = validate_toutiao_cover_selection(options["toutiao"])
                for reference in references:
                    if resolve_article_image(runner.database, article_id, reference, project_root=PROJECT_ROOT) is None:
                        raise ValueError("今日头条封面不可用或不属于当前文章，请重新选择。")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
    for platform_id, _, _, options in resolved_targets:
        if platform_id == "xiaohongshu":
            try:
                platform_document = _article_platform_document(article_id, document, platform_id, options)
                if not xiaohongshu_article_images(article_id, platform_document, database=runner.database):
                    raise HTTPException(status_code=422, detail="小红书图文至少需要一张有效图片，请先上传文章封面或正文配图。")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
    version = int(article["version"])
    toutiao_connected = True
    if any(platform_id == "toutiao" for platform_id, _, _, _ in resolved_targets):
        current_toutiao = next((item for item in _article_platform_statuses(runner.config, runner.database) if item.get("id") == "toutiao"), {})
        toutiao_connected = bool(current_toutiao.get("connected"))
    results: list[dict[str, Any]] = []
    for platform_id, account_id, account_name, options in resolved_targets:
        retry_manual_publication_id = options.get("retry_publication_id") if platform_id == "toutiao" else None
        if retry_manual_publication_id:
            previous = next((item for item in article["publications"] if item.get("id") == retry_manual_publication_id), None)
            if (
                previous is None
                or previous.get("platform_id") != "toutiao"
                or previous.get("status") != "manual_required"
                or previous.get("remote_id")
                or int(previous.get("article_version") or 0) != version
                or previous.get("account_id") != account_id
            ):
                raise HTTPException(status_code=409, detail="今日头条待手动记录已变化，请刷新后重新确认。")
            if not toutiao_connected:
                raise HTTPException(status_code=409, detail="今日头条账号当前未连接，无法自动重试。")
            if not isinstance(options.get("toutiao"), dict) or options["toutiao"].get("account_options_verified") is not True:
                raise HTTPException(status_code=409, detail="请先读取当前头条号发表选项并确认后再重试。")
            try:
                from .toutiao_publishing import ensure_toutiao_auto_supported, prepare_toutiao_options
                if article_platform_capabilities("toutiao").get("article", {}).get("publish") != "browser":
                    raise ValueError("当前版本的今日头条浏览器发表适配不可用。")
                platform_document = _article_platform_document(article_id, document, platform_id, options)
                settings = prepare_toutiao_options(options, platform_document)
                ensure_toutiao_auto_supported(platform_document, settings)
                settings["account_options_verified"] = True
                options["toutiao"] = settings
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            record, created = reserve_article_publication(
                runner.database, article_id, platform_id, version,
                account_id=account_id, account_name=account_name, options=options,
                retry_manual_publication_id=retry_manual_publication_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="文章不存在。") from None
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None


        if not created:
            results.append(record)
            continue
        capabilities = article_platform_capabilities(platform_id)
        content_type = "image_note" if platform_id == "xiaohongshu" else "article"
        publish_mode = capabilities.get(content_type, {}).get("publish", "manual")
        manual_reason = ""
        if platform_id == "csdn" and publish_mode == "browser":
            if not options.get("tags") or not isinstance(options.get("csdn"), dict):
                publish_mode = "manual"
            else:
                try:
                    ensure_csdn_auto_supported(_article_platform_document(article_id, document, platform_id, options), options)
                except ValueError as exc:
                    publish_mode = "manual"
                    manual_reason = str(exc)
        if platform_id == "toutiao" and publish_mode == "browser":
            if not toutiao_connected:
                publish_mode = "manual"
                manual_reason = "今日头条账号当前未连接，请连接账号后重新创建发布任务。"
            elif not isinstance(options.get("toutiao"), dict):
                publish_mode = "manual"
            elif options["toutiao"].get("account_options_verified") is not True:
                publish_mode = "manual"
                manual_reason = "无法读取当前头条号发表选项；请在平台核对作品声明与合集后手动发布。"
            else:
                try:
                    from .toutiao_publishing import ensure_toutiao_auto_supported, prepare_toutiao_options
                    platform_document = _article_platform_document(article_id, document, platform_id, options)
                    settings = prepare_toutiao_options(options, platform_document)
                    ensure_toutiao_auto_supported(platform_document, settings)
                    options["toutiao"] = settings
                except ValueError as exc:
                    publish_mode = "manual"
                    manual_reason = str(exc)
        if publish_mode not in {"official_api", "official_api_or_browser", "official_cli", "browser"}:
            record = update_article_publication(
                runner.database, record["id"], "manual_required",
                message=("请由账号持有人在小红书创作中心手动发布图文笔记，完成后回来确认。"
                         if platform_id == "xiaohongshu" else manual_reason or "请使用平台格式、复制正文并在平台网页手动发布。"),
            )
            results.append(record)
            continue
        platform_document = _article_platform_document(article_id, document, platform_id, options)
        if platform_id == "csdn":
            platform_document["_publish_options"] = options
        if platform_id == "toutiao":
            platform_document["_publish_options"] = options
        needs_browser_manager = platform_id == "zhihu" and account_id is not None and account_id.startswith("browser:")
        video_manager = _video_publish_manager(request) if needs_browser_manager else None
        background_tasks.add_task(
            _execute_article_publication,
            runner.database, runner.config, record["id"], platform_id,
            account_id, platform_document, video_manager,
        )
        results.append(record)
    return {"results": results, "article": get_article(runner.database, article_id)}


@_router.post("/articles/{article_id}/publications/{publication_id}/refresh", dependencies=[Depends(_same_origin)])
def refresh_article_publication_endpoint(
    article_id: str,
    publication_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    publication = next((item for item in article["publications"] if item["id"] == publication_id), None)
    if publication is None:
        raise HTTPException(status_code=404, detail="发布记录不存在。")
    if publication["platform_id"] != "wechat" or not publication.get("account_id"):
        return publication
    if publication["status"] in {"publishing", "unknown"} and publication.get("remote_id"):
        try:
            result = refresh_wechat_publication(
                runner.config, publication["account_id"], str(publication.get("remote_id") or "")
            )
        except (RuntimeError, ValueError, OSError, CredentialStoreUnavailable) as exc:
            result = {
                "status": "unknown", "remote_id": publication.get("remote_id"),
                "remote_url": publication.get("remote_url"),
                "message": f"暂时无法确认公众号发布状态。请检查账号凭据与 IP 白名单，到公众号后台核对后再刷新：{_safe_error(str(exc))}",
            }
        try:
            publication = update_article_publication(
                runner.database, publication_id, str(result["status"]),
                remote_id=result.get("remote_id"), remote_url=result.get("remote_url"),
                message=str(result.get("message", "")),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="发布记录不存在。") from None

    if publication["status"] == "published" and publication.get("fan_notification_status") == "pending":
        claimed = claim_article_fan_notification(runner.database, publication_id)
        if claimed is not None:
            platform_document = _article_platform_document(
                article_id, article["document"], "wechat", publication.get("options") or {},
            )
            background_tasks.add_task(
                _execute_wechat_fan_notification,
                runner.database, runner.config, publication_id,
                str(publication["account_id"]), platform_document,
            )
            return claimed
        publication = get_article(runner.database, article_id) or article
        publication = next((item for item in publication["publications"] if item["id"] == publication_id), publication)

    if publication.get("fan_notification_status") in {"sending", "unknown"} and publication.get("fan_notification_remote_id"):
        try:
            result = refresh_wechat_fan_notification(
                runner.config, str(publication["account_id"]), str(publication["fan_notification_remote_id"]),
            )
        except (RuntimeError, ValueError, OSError, CredentialStoreUnavailable) as exc:
            result = {
                "status": "sending", "remote_id": publication.get("fan_notification_remote_id"),
                "message": f"暂时无法确认粉丝群发状态，请稍后刷新：{_safe_error(str(exc))}",
            }
        try:
            return update_article_fan_notification(
                runner.database, publication_id, str(result["status"]),
                remote_id=result.get("remote_id"), message=str(result.get("message", "")),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="发布记录不存在。") from None
    return publication


@_router.post(
    "/articles/{article_id}/publications/{publication_id}/fan-notification/retry",
    dependencies=[Depends(_same_origin)],
)
def retry_article_fan_notification_endpoint(
    article_id: str,
    publication_id: str,
    body: ArticleFanNotificationRetryRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请确认重新向公众号全体粉丝发送文章。")
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    publication = next((item for item in article["publications"] if item["id"] == publication_id), None)
    if publication is None:
        raise HTTPException(status_code=404, detail="发布记录不存在。")
    if (
        publication["platform_id"] != "wechat"
        or not publication.get("account_id")
        or publication["status"] != "published"
        or int(publication["article_version"]) != int(article["version"])
        or article["status"] != "finalized"
    ):
        raise HTTPException(status_code=409, detail="只能重试当前定稿版本中已发表的公众号文章通知。")
    if publication.get("fan_notification_status") != "failed" or publication.get("fan_notification_remote_id"):
        raise HTTPException(
            status_code=409,
            detail="只有微信明确拒绝且未返回群发消息 ID 的通知失败可以重试；其他状态请先查询或到公众号后台核对。",
        )
    claimed = retry_article_fan_notification(runner.database, publication_id)
    if claimed is None:
        raise HTTPException(status_code=409, detail="该通知已被处理，不能重复提交。")
    platform_document = _article_platform_document(
        article_id, article["document"], "wechat", publication.get("options") or {},
    )
    background_tasks.add_task(
        _execute_wechat_fan_notification,
        runner.database, runner.config, publication_id,
        str(publication["account_id"]), platform_document,
    )
    return claimed


def _resolve_article_publish_targets(
    body: ArticlePublishRequest,
    config: Any,
    database: Any,
) -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
    valid_ids = {str(platform["id"]) for platform in ARTICLE_PLATFORMS}
    statuses = {str(item["id"]): item for item in _article_platform_statuses(config, database)}
    if body.targets:
        requested = []
        for item in body.targets:
            options = item.options.model_dump(exclude_none=True, exclude_defaults=True)
            if item.options.csdn is not None:
                options["csdn"] = item.options.csdn.model_dump()
            if item.options.toutiao is not None:
                options["toutiao"] = item.options.toutiao.model_dump()
            requested.append((item.platform_id, item.account_id, options))
    else:
        requested = []
        for platform_id in body.platforms:
            status = statuses.get(platform_id, {})
            configured_accounts = [
                item for item in status.get("accounts", [])
                if isinstance(item, dict) and item.get("configured")
            ] if isinstance(status.get("accounts"), list) else []
            if platform_id in {"wechat", "zhihu", "xiaohongshu"} and configured_accounts:
                chosen = configured_accounts[0]
                requested.append((platform_id, str(chosen["id"]), {}))
            else:
                requested.append((platform_id, None, {}))

    targets: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    for platform_id, account_id, options in requested:
        if platform_id not in valid_ids:
            raise HTTPException(status_code=422, detail="包含无效的发布平台。")
        if "title" in options and (not isinstance(options["title"], str) or not options["title"].strip()):
            raise HTTPException(status_code=422, detail="平台发布标题不能为空。")
        if platform_id == "xiaohongshu":
            title = str(options.get("title") or "").strip()
            note = options.get("note")
            if not 1 <= len(title) <= 20:
                raise HTTPException(status_code=422, detail="小红书笔记标题须为 1 到 20 个字符。")
            if not isinstance(note, str) or not note.strip() or len(note) > 1000:
                raise HTTPException(status_code=422, detail="请填写不超过 1000 个字符的小红书笔记正文。")
        if bool(options.get("notify_fans")) and platform_id != "wechat":
            raise HTTPException(status_code=422, detail="只有微信公众号支持粉丝群发通知。")
        status = statuses.get(platform_id, {})
        if platform_id in {"wechat", "zhihu", "xiaohongshu"}:
            accounts = status.get("accounts", []) if isinstance(status.get("accounts"), list) else []
            if account_id is None:
                configured = [item for item in accounts if isinstance(item, dict) and item.get("configured")]
                if len(configured) == 1:
                    account_id = str(configured[0]["id"])
                elif len(configured) > 1:
                    raise HTTPException(status_code=422, detail=f"{status.get('name', platform_id)}已配置多个账号，请逐个选择账号。")
            account = next((item for item in accounts if isinstance(item, dict) and item.get("id") == account_id), None)
            if account is None or not account.get("configured"):
                raise HTTPException(status_code=409, detail=f"请先连接并配置{status.get('name', platform_id)}发布账号。")
            targets.append((platform_id, account_id, str(account.get("name") or ""), options))
        else:
            if account_id is not None:
                raise HTTPException(status_code=422, detail="该平台当前不支持选择多个发布账号。")
            account_name = None
            if platform_id == "toutiao":
                accounts = status.get("accounts", []) if isinstance(status.get("accounts"), list) else []
                account = next((
                    item for item in accounts
                    if isinstance(item, dict) and item.get("configured")
                ), None)
                profile_name = str(account.get("name") or "").strip() if account else ""
                if profile_name and profile_name not in {"当前今日头条账号", "今日头条账号"}:
                    account_name = profile_name
            if platform_id == "weibo" and not status.get("publish_ready"):
                if not status.get("cli_installed"):
                    detail = "请先安装微博官方 CLI。"
                elif not status.get("connection_checked"):
                    detail = "无法确认微博 CLI 当前账号连接状态；请检查 CLI 或网络后刷新。"
                elif not status.get("connected"):
                    detail = "请先登录微博官方 CLI 当前账号。"
                else:
                    detail = str(status.get("message") or "请先完成微博开放平台发布准备检查。")
                raise HTTPException(status_code=409, detail=detail)
            targets.append((platform_id, None, account_name, options))
    return targets


def _article_platform_document(
    article_id: str,
    document: dict[str, Any],
    platform_id: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    platform_document = dict(document)
    platform_document["_article_id"] = article_id
    overrides = document.get("platform_overrides")
    override = overrides.get(platform_id) if isinstance(overrides, dict) else None
    if isinstance(override, dict):
        platform_document.update({key: override[key] for key in ("title", "summary", "body_markdown") if isinstance(override.get(key), str) and override[key].strip()})
    for key in ("title", "summary", "author"):
        value = (options or {}).get(key)
        if isinstance(value, str) and (value.strip() or key in {"summary", "author"}):
            platform_document[key] = value.strip()
    if isinstance((options or {}).get("cover_image_path"), str) and options["cover_image_path"]:
        platform_document["cover_image_path"] = options["cover_image_path"]
    note = (options or {}).get("note")
    if isinstance(note, str):
        platform_document["note"] = note
    outro = str(platform_document.get("outro") or "").strip()
    article_body = str(platform_document.get("body_markdown") or "").rstrip()
    if outro and not article_body.endswith(outro):
        platform_document["body_markdown"] = f"{article_body}\n\n{outro}" if article_body else outro
    return platform_document


def _execute_article_publication(
    database: Any,
    config: Any,
    publication_id: str,
    platform_id: str,
    account_id: str | None,
    platform_document: dict[str, Any],
    video_manager: VideoPublishManager | None = None,
) -> None:
    try:
        if platform_id == "csdn":
            reference = str(platform_document.get("cover_image_path") or "")
            cover_path = resolve_article_image(database, str(platform_document.get("_article_id") or ""), reference, project_root=PROJECT_ROOT) if reference else None
            if reference and cover_path is None:
                raise ValueError("CSDN 封面不可用，请重新选择封面。")

            def before_submit() -> None:
                update_article_publication(database, publication_id, "unknown", message="CSDN 即将提交；若结果未确认，请先核对平台，避免重复发布。")

            result = get_platform_account_manager().publish_csdn_article(platform_document, platform_document.get("_publish_options") or {}, cover_path, before_submit)
            update_article_publication(database, publication_id, result["status"], remote_id=result.get("remote_id"), remote_url=result.get("remote_url"), message=result.get("message", ""))
            return
        if platform_id == "toutiao":
            options = platform_document.get("_publish_options") or {}
            settings = options.get("toutiao") if isinstance(options, dict) else None
            if not isinstance(settings, dict):
                raise ValueError("请先填写今日头条专属发表设置。")
            from .toutiao_publishing import ensure_toutiao_auto_supported, prepare_toutiao_options
            settings = prepare_toutiao_options(options, platform_document)
            ensure_toutiao_auto_supported(platform_document, settings)
            cover_paths = [
                resolve_article_image(
                    database, str(platform_document.get("_article_id") or ""), reference,
                    project_root=PROJECT_ROOT,
                )
                for reference in settings["cover_image_paths"]
            ]
            if any(path is None for path in cover_paths):
                raise ValueError("今日头条封面不可用，请重新选择封面。")
            article_id = str(platform_document.get("_article_id") or "")
            body_image_paths = [
                resolve_article_image(
                    database, article_id, str(block.get("image_path") or ""),
                    project_root=PROJECT_ROOT,
                )
                for block in platform_document.get("blocks", [])
                if isinstance(block, dict) and isinstance(block.get("image_path"), str) and block["image_path"]
            ] if isinstance(platform_document.get("blocks"), list) else []
            if any(path is None for path in body_image_paths):
                raise ValueError("今日头条正文配图不可用，请重新上传配图后发布。")

            def before_submit() -> None:
                update_article_publication(
                    database, publication_id, "unknown",
                    message="今日头条即将提交最终发表；若回执暂未确认，请先核对平台，避免重复发布。",
                )

            result = get_platform_account_manager().publish_toutiao_article(
                platform_document, settings, cover_paths, body_image_paths, before_submit,
            )
            update_article_publication(
                database, publication_id, result["status"],
                remote_id=result.get("remote_id"), remote_url=result.get("remote_url"),
                message=result.get("message", ""),
            )
            return
        if platform_id == "xiaohongshu":
            update_article_publication(
                database, publication_id, "manual_required",
                message="请由账号持有人在小红书创作中心手动发布图文笔记，完成后回来确认。",
            )
            return
        if platform_id == "zhihu" and account_id and account_id.startswith("browser:"):
            if video_manager is None:
                raise RuntimeError("知乎 SAU 浏览器账号不可用，请刷新账号状态后重试。")
            with prepare_zhihu_article_file(
                str(platform_document.get("_article_id") or ""), platform_document, database=database,
            ) as content_file:
                result = video_manager.publish_zhihu_article(
                    account_id.removeprefix("browser:"), publication_id,
                    str(platform_document.get("title") or ""), content_file,
                )
            if result.launch_error:
                update_article_publication(database, publication_id, "failed", message=_safe_error(result.launch_error))
            elif result.platform_status == "published" and result.remote_url:
                update_article_publication(
                    database, publication_id, "published", remote_id=result.remote_id,
                    remote_url=result.remote_url,
                    message="知乎已确认文章详情页和标题；作品链接已保存。",
                )
            elif result.platform_status == "failed":
                detail = _safe_error(next((line for line in reversed(result.output.splitlines()) if line.strip()), ""))
                update_article_publication(
                    database, publication_id, "failed",
                    message="知乎在提交前未完成文章发布。" + (f" {detail}" if detail else ""),
                )
            else:
                detail = _safe_error(next((line for line in reversed(result.output.splitlines()) if line.strip()), ""))
                message = "知乎文章提交结果尚未确认；请先到创作中心核对，再决定是否重试。"
                if detail:
                    message += f" {detail}"
                update_article_publication(database, publication_id, "unknown", message=message)
            return
        result = publish_article_platform(config, platform_id, platform_document, account_id=account_id)
        inline_image_count = sum(
            1 for block in platform_document.get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("image_path"), str) and block.get("image_path")
        )
        if platform_id == "weibo" and (inline_image_count or platform_document.get("cover_image_path")):
            result["message"] = str(result.get("message", "")) + " 当前工作台未配置微博文章配图上传；封面和正文配图需在微博端手动补充。"
        update_article_publication(
            database, publication_id, str(result["status"]),
            remote_id=result.get("remote_id"), remote_url=result.get("remote_url"),
            message=str(result.get("message", "")),
        )
    except Exception as exc:
        try:
            if platform_id in {"csdn", "toutiao"}:
                with database.transaction() as connection:
                    row = connection.execute("SELECT status FROM article_publications WHERE id = ?", (publication_id,)).fetchone()
                if row is not None and row["status"] == "unknown":
                    platform_name = "CSDN" if platform_id == "csdn" else "今日头条"
                    update_article_publication(database, publication_id, "unknown", message=f"{platform_name}提交结果未确认，请先在平台核对，不要直接重试。")
                    return
            update_article_publication(
                database, publication_id, "failed",
                message=_safe_error(str(exc)) or "平台发布失败。",
            )
        except KeyError:
            return


@_router.post(
    "/articles/{article_id}/publications/{publication_id}/confirm-csdn",
    dependencies=[Depends(_same_origin)],
)
def confirm_csdn_article_publication(
    article_id: str,
    publication_id: str,
    body: ArticlePublishResultConfirmRequest,
    request: Request,
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请先在 CSDN 内容管理中核对发布结果。")
    database = _runner(request).database
    status = "published" if body.outcome == "published" else "failed"
    message = ("用户已在 CSDN 内容管理中确认文章已发布。" if body.outcome == "published"
               else "用户已在 CSDN 内容管理中确认文章未发布；现在可以重新提交。")
    now = utc_now()
    with database.transaction(write=True) as connection:
        article = connection.execute("SELECT status, version FROM articles WHERE id = ?", (article_id,)).fetchone()
        if article is None:
            raise HTTPException(status_code=404, detail="文章不存在。")
        record = connection.execute("SELECT platform_id, status, article_version FROM article_publications WHERE id = ? AND article_id = ?", (publication_id, article_id)).fetchone()
        if record is None:
            raise HTTPException(status_code=404, detail="发布记录不存在。")
        if record["platform_id"] != "csdn" or record["status"] != "unknown" or int(record["article_version"]) != int(article["version"]) or article["status"] != "finalized":
            raise HTTPException(status_code=409, detail="只能核对当前定稿版本中待核实的 CSDN 发布记录。")
        connection.execute("UPDATE article_publications SET status = ?, message = ?, updated_at = ? WHERE id = ? AND status = 'unknown'", (status, message, now, publication_id))
        connection.execute("UPDATE articles SET updated_at = ? WHERE id = ?", (now, article_id))
    return next(item for item in get_article(database, article_id)["publications"] if item["id"] == publication_id)


@_router.post(
    "/articles/{article_id}/publications/{publication_id}/confirm-toutiao",
    dependencies=[Depends(_same_origin)],
)
def confirm_toutiao_article_publication(
    article_id: str,
    publication_id: str,
    body: ArticlePublishResultConfirmRequest,
    request: Request,
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请先在今日头条内容管理中核对发布结果。")
    database = _runner(request).database
    status = "published" if body.outcome == "published" else "failed"
    message = ("用户已在今日头条内容管理中确认文章已发表。" if body.outcome == "published"
               else "用户已在今日头条内容管理中确认文章未发表；现在可以重新提交。")
    now = utc_now()
    with database.transaction(write=True) as connection:
        article = connection.execute("SELECT status, version FROM articles WHERE id = ?", (article_id,)).fetchone()
        if article is None:
            raise HTTPException(status_code=404, detail="文章不存在。")
        record = connection.execute("SELECT platform_id, status, article_version FROM article_publications WHERE id = ? AND article_id = ?", (publication_id, article_id)).fetchone()
        if record is None:
            raise HTTPException(status_code=404, detail="发布记录不存在。")
        if record["platform_id"] != "toutiao" or record["status"] != "unknown" or int(record["article_version"]) != int(article["version"]) or article["status"] != "finalized":
            raise HTTPException(status_code=409, detail="只能核对当前定稿版本中待核实的今日头条发布记录。")
        connection.execute("UPDATE article_publications SET status = ?, message = ?, updated_at = ? WHERE id = ? AND status = 'unknown'", (status, message, now, publication_id))
        connection.execute("UPDATE articles SET updated_at = ? WHERE id = ?", (now, article_id))
    return next(item for item in get_article(database, article_id)["publications"] if item["id"] == publication_id)


@_router.post(
    "/articles/{article_id}/publications/{publication_id}/confirm-xiaohongshu",
    dependencies=[Depends(_same_origin)],
)
def confirm_xiaohongshu_article_publication(
    article_id: str,
    publication_id: str,
    body: ArticlePublishResultConfirmRequest,
    request: Request,
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请确认已在小红书创作中心核对发布结果。")
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    publication = next((item for item in article["publications"] if item["id"] == publication_id), None)
    if publication is None:
        raise HTTPException(status_code=404, detail="发布记录不存在。")
    if (
        publication["platform_id"] != "xiaohongshu"
        or not publication.get("account_id")
        or publication["status"] not in {"unknown", "manual_required"}
        or int(publication["article_version"]) != int(article["version"])
        or article["status"] != "finalized"
    ):
        raise HTTPException(status_code=409, detail="只能核对当前定稿版本中待核实的小红书发布记录。")
    outcome = body.outcome
    status = ("manual_published" if publication["status"] == "manual_required" else "published") if outcome == "published" else "failed"
    message = (
        "用户已在小红书创作中心确认发布成功。"
        if outcome == "published"
        else "用户已在小红书创作中心确认未发布；现在可以重新提交。"
    )
    try:
        return update_article_publication(runner.database, publication_id, status, message=message)
    except KeyError:
        raise HTTPException(status_code=404, detail="发布记录不存在。") from None


def _execute_wechat_fan_notification(
    database: Any,
    config: Any,
    publication_id: str,
    account_id: str,
    platform_document: dict[str, Any],
) -> None:
    try:
        result = send_wechat_fan_notification(
            config, account_id, publication_id, platform_document,
        )
        update_article_fan_notification(
            database, publication_id, str(result["status"]),
            remote_id=result.get("remote_id"), message=str(result.get("message", "")),
        )
    except Exception as exc:
        try:
            update_article_fan_notification(
                database, publication_id, "failed",
                message=_safe_error(str(exc)) or "微信公众号粉丝通知提交失败。",
            )
        except KeyError:
            return


@_router.post("/articles/{article_id}/manual-published", dependencies=[Depends(_same_origin)])
def confirm_manual_article_publication(article_id: str, body: ArticleManualPublishRequest, request: Request) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请先确认文章已在平台端发布。")
    if body.platform_id in {"zhihu", "weibo"}:
        raise HTTPException(status_code=422, detail="该平台使用官方接口，不能标记为手动发布。")
    runner = _runner(request)
    if body.platform_id not in {platform["id"] for platform in ARTICLE_PLATFORMS}:
        raise HTTPException(status_code=422, detail="发布平台无效。")
    expected_status = "draft_saved" if body.platform_id == "wechat" else "manual_required"
    with runner.database.transaction() as connection:
        latest = connection.execute(
            "SELECT status, article_version FROM article_publications WHERE article_id = ? AND platform_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (article_id, body.platform_id),
        ).fetchone()
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    if article["status"] != "finalized" or latest is None or str(latest["status"]) != expected_status or int(latest["article_version"]) != int(article["version"]):
        raise HTTPException(status_code=409, detail="请先完成该平台的发布步骤，再确认发布状态。")
    try:
        return record_publication(runner.database, article_id, body.platform_id, "manual_published", message="用户已确认在平台端发布。")
    except KeyError:
        raise HTTPException(status_code=404, detail="文章不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_router.post("/articles/{article_id}/publish-covers", status_code=201, dependencies=[Depends(_same_origin)])
async def upload_article_publish_cover(article_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, str]:
    database = _runner(request).database
    if get_article(database, article_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    data = await file.read(10_000_001)
    if not data or len(data) > 10_000_000:
        raise HTTPException(status_code=413, detail="封面图片必须小于 10 MB。")
    suffix = Path(file.filename or "cover.jpg").suffix.casefold()
    signatures = {".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"), ".png": data.startswith(b"\x89PNG\r\n\x1a\n"), ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    if suffix not in signatures or not signatures[suffix]:
        raise HTTPException(status_code=415, detail="封面仅支持 JPEG、PNG 或 WebP 图片。")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width <= 0 or image.height <= 0 or image.width * image.height > 20_000_000:
                raise ValueError("图片尺寸无效。")
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise HTTPException(status_code=415, detail="封面图片损坏或尺寸过大，请重新选择。") from None
    try:
        directory = image_directory(database, "article_image_root")
        with store_image(database, directory, article_id=article_id, original_name=Path(file.filename or "cover").name[:255], suffix=suffix, content=data) as (connection, image):
            if connection.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None:
                raise KeyError("文章不存在。")
            image_id = str(image["id"])
            reference = f"data/article-assets/{article_id}/{image_id}{suffix}"
    except KeyError:
        raise HTTPException(status_code=404, detail="文章不存在。") from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "封面保存失败。") from None
    return {"id": image_id, "reference": reference, "preview_url": f"/api/v1/articles/{article_id}/publish-covers/{image_id}"}


@_router.get("/articles/{article_id}/publish-covers/{image_id}", dependencies=[Depends(_same_origin)])
def get_article_publish_cover(article_id: str, image_id: str, request: Request) -> FileResponse:
    database = _runner(request).database
    if not re.fullmatch(r"[0-9a-f]{32}", image_id) or get_article(database, article_id) is None:
        raise HTTPException(status_code=404, detail="平台封面不可用。")
    with database.transaction() as connection:
        row = connection.execute("SELECT suffix FROM workbench_images WHERE id = ? AND article_id = ? AND kind = 'article_image'", (image_id, article_id)).fetchone()
    reference = f"data/article-assets/{article_id}/{image_id}{row['suffix']}" if row else ""
    path = resolve_article_image(database, article_id, reference, project_root=PROJECT_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="平台封面不可用。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@_router.post("/articles/{article_id}/cover", status_code=201, dependencies=[Depends(_same_origin)])
async def upload_article_cover(article_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    runner = _runner(request)
    article = get_article(runner.database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    data = await file.read(10_000_001)
    if not data or len(data) > 10_000_000:
        raise HTTPException(status_code=413, detail="封面图片必须小于 10 MB。")
    suffix = Path(file.filename or "cover.jpg").suffix.casefold()
    signatures = {".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"), ".png": data.startswith(b"\x89PNG\r\n\x1a\n"), ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    if suffix not in signatures or not signatures[suffix]:
        raise HTTPException(status_code=415, detail="封面仅支持 JPEG、PNG 或 WebP 图片。")
    try:
        updated, _ = save_article_image(
            runner.database, article, data, suffix, name=Path(file.filename or "cover").name[:255],
        )
    except OSError:
        raise HTTPException(status_code=500, detail="无法写入文章图片目录，请检查目录设置。") from None
    except (KeyError, RuntimeError, ValueError) as exc:
        status = 404 if isinstance(exc, KeyError) else 409 if isinstance(exc, RuntimeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return {"article": updated, "cover_url": f"/api/v1/articles/{article_id}/cover"}


@_router.get("/articles/{article_id}/cover", dependencies=[Depends(_same_origin)])
def get_article_cover(article_id: str, request: Request) -> FileResponse:
    database = _runner(request).database
    article = get_article(database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在。")
    reference = article["document"].get("cover_image_path")
    path = resolve_article_image(database, article_id, str(reference or ""), project_root=PROJECT_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="文章封面不可用。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@_router.get("/articles/{article_id}/images/{filename}", dependencies=[Depends(_same_origin)])
def get_article_image(article_id: str, filename: str, request: Request) -> FileResponse:
    database = _runner(request).database
    article = get_article(database, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章配图不可用。")
    document = article["document"]
    referenced_paths = [document.get("cover_image_path")]
    referenced_paths.extend(block.get("image_path") for block in document.get("blocks", []) if isinstance(block, dict))
    reference = f"data/article-assets/{article_id}/{filename}"
    if reference not in referenced_paths:
        with database.transaction() as connection:
            historical = connection.execute(
                """SELECT 1 FROM article_versions
                   WHERE article_id = ? AND (
                       json_extract(document_json, '$.cover_image_path') = ?
                       OR EXISTS (
                           SELECT 1 FROM json_each(document_json, '$.blocks')
                           WHERE type = 'object' AND json_extract(value, '$.image_path') = ?
                       )
                   ) LIMIT 1""",
                (article_id, reference, reference),
            ).fetchone()
        if historical is None:
            raise HTTPException(status_code=404, detail="文章配图不可用。")
    path = resolve_article_image(database, article_id, reference, project_root=PROJECT_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="文章配图不可用。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@_router.get("/tasks", dependencies=[Depends(_same_origin)])
def list_tasks(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = None,
    platform: str | None = Query(default=None, min_length=1, max_length=64),
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="newest", pattern="^(newest|oldest|title)$"),
    include_removed: bool = False,
) -> dict[str, object]:
    allowed = {
        "queued", "running", "interrupted", "awaiting_authorization", "completed", "partial",
        "failed", "blocked", "cancelled",
    }
    if status is not None and status not in allowed:
        raise HTTPException(status_code=422, detail="任务状态筛选无效。")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="日期范围无效：开始日期不能晚于结束日期。")
    runner = _runner(request)
    tasks = runner.repository.list_tasks(
        limit,
        offset,
        status,
        platform=platform,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        search=search,
        sort=sort,
        include_removed=include_removed,
    )
    items = []
    for task in tasks:
        asset = _asset_for_task(runner, task)
        cover = _task_cover_summary(runner.database, task.id, runner.config.archive_root)
        video_author = _task_video_author(runner.database, task.id)
        items.append(_summary_payload(
            task,
            asset,
            runner.config.archive_root,
            cover=cover,
            video_author=video_author,
        ))
    total = runner.repository.count_tasks(
        status,
        platform=platform,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        search=search,
        include_removed=include_removed,
    )
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@_router.post("/tasks/{task_id}/archive/remove", dependencies=[Depends(_same_origin)])
def remove_task_from_archive(task_id: str, request: Request) -> dict[str, bool]:
    try:
        hide_archived_task(_runner(request).database, task_id)
    except TaskArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return {"removed": True}


@_router.post("/tasks/{task_id}/archive/restore", dependencies=[Depends(_same_origin)])
def restore_task_to_archive(task_id: str, request: Request) -> dict[str, bool]:
    try:
        restore_archived_task(_runner(request).database, task_id)
    except TaskArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return {"restored": True}


@_router.get("/tasks/{task_id}/delete-preview", dependencies=[Depends(_same_origin)])
def preview_task_deletion(task_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        preview = task_deletion_preview(
            runner.database,
            task_id,
            runner.config.archive_root,
            runner_active=runner.is_task_active(task_id),
        )
    except TaskArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    preview.pop("deletable_comment_ids", None)
    preview.pop("asset_id", None)
    preview.pop("clear_asset_archive_path", None)
    for item in preview["items"]:
        for internal_field in ("file_path", "artifact_ids", "inside_archive", "mtime_ns"):
            item.pop(internal_field, None)
    return preview


@_router.delete("/tasks/{task_id}", dependencies=[Depends(_same_origin)])
def permanently_delete_task_endpoint(
    task_id: str,
    body: TaskDeleteRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    try:
        return permanently_delete_task(
            runner.database,
            task_id,
            runner.config.archive_root,
            body.preview_token,
            runner_active=runner.is_task_active(task_id),
        )
    except TaskArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@_router.get("/tasks/{task_id}", dependencies=[Depends(_same_origin)])
def task_detail(task_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    asset = _asset_for_task(runner, task)
    with runner.database.transaction() as connection:
        artifacts = connection.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        ).fetchall()
        consent_rows = connection.execute(
            "SELECT step_kind, provider_id, model_id, artifact_ids_json, authorized, authorized_at "
            "FROM consents WHERE task_id = ? ORDER BY authorized_at, rowid",
            (task_id,),
        ).fetchall()
        model_runs = connection.execute(
            """SELECT id, step_id, provider_id, model_id, artifact_ids_json, request_id,
                      usage_json, cost_json, status, error_code, created_at, finished_at
               FROM model_runs WHERE task_id = ? ORDER BY created_at, id""",
            (task_id,),
        ).fetchall()
    access_mode = get_task_access_mode(runner.database, task_id)
    account_id, account_name = get_task_access_account(runner.database, task_id)
    task_payload = _task_payload(task)
    task_payload["platform_access"] = {
        "mode": access_mode,
        "account_id": account_id,
        "account_name": account_name,
    }
    return {
        "task": task_payload,
        "asset": _asset_payload(asset, runner.config.archive_root),
        "transcription_config": _transcription_config_payload(runner, task_id),
        "artifacts": [
            _artifact_payload(row, runner.config.archive_root)
            for row in artifacts
        ],
        "consents": [
            {
                "step_kind": str(row["step_kind"]),
                "provider_id": str(row["provider_id"]),
                "model_id": str(row["model_id"]),
                "artifact_ids": _json_list(row["artifact_ids_json"]),
                "authorized": bool(row["authorized"]),
                "authorized_at": row["authorized_at"],
            }
            for row in consent_rows
        ],
        "model_runs": [
            {
                "id": str(row["id"]),
                "step_id": row["step_id"],
                "provider_id": str(row["provider_id"]),
                "model_id": str(row["model_id"]),
                "artifact_ids": _json_list(row["artifact_ids_json"]),
                "request_id": row["request_id"],
                "usage": _json_object(row["usage_json"]),
                "cost": _json_object(row["cost_json"]) if row["cost_json"] else None,
                "status": str(row["status"]),
                "error_code": row["error_code"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
            for row in model_runs
        ],
    }


def _transcription_config_payload(runner: TaskRunner, task_id: str) -> dict[str, object]:
    saved = get_transcription_step_config(runner.database, task_id)
    if saved is not None and saved["mode"] == "local":
        return {
            "mode": "local",
            "profile_id": None,
            "profile_name": None,
            "provider_id": None,
            "provider_name": None,
            "asr_model_id": None,
            "local_model": saved.get("local_model"),
            "local_language": saved.get("local_language"),
            "local_device": saved.get("local_device"),
        }
    if saved is not None:
        provider_id = saved.get("provider_id")
        profile_id = saved.get("profile_id")
        profile_name = saved.get("profile_name")
        asr_model_id = saved.get("asr_model_id")
    else:
        local_options = local_asr.get_task_options(runner.database, task_id)
        if local_options is not None:
            return {
                "mode": "local",
                "profile_id": None,
                "profile_name": None,
                "provider_id": None,
                "provider_name": None,
                "asr_model_id": None,
                "local_model": local_options.get("model"),
                "local_language": local_options.get("language"),
                "local_device": local_options.get("device"),
            }
        settings = task_model_settings(runner.config, task_id)
        provider_id = settings.get("model_provider")
        profile_id = settings.get("model_profile_id")
        profile_name = settings.get("model_profile_name")
        asr_model_id = settings.get("asr_model_id")
    preset = get_provider_preset(provider_id)
    return {
        "mode": "cloud",
        "profile_id": profile_id,
        "profile_name": profile_name,
        "provider_id": provider_id,
        "provider_name": preset.label if preset else provider_id,
        "asr_model_id": asr_model_id,
        "local_model": None,
        "local_language": None,
        "local_device": None,
    }


@_router.put("/tasks/{task_id}/transcription-config", dependencies=[Depends(_same_origin)])
def update_task_transcription_config(
    task_id: str,
    body: TranscriptionStepConfigRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    step = next((item for item in task.steps if item.kind is StepKind.TRANSCRIBE), None)
    if step is None:
        raise HTTPException(status_code=404, detail="当前任务没有语音转写步骤。")
    if task.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务正在处理，请结束后再修改转写配置。")
    if step.status not in {"failed", "blocked", "interrupted", "cancelled", "partial"}:
        raise HTTPException(status_code=409, detail="只有失败或未完成的转写步骤可以修改配置。")

    if body.mode == "cloud":
        if not body.profile_id or not body.asr_model_id:
            raise HTTPException(status_code=422, detail="请选择云端模型配置并填写 ASR 模型 ID。")
        profile = get_model_profile(runner.config, body.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="所选模型配置不存在或已归档。")
        preset = get_provider_preset(profile.get("provider_id"))
        if preset is None or preset.transcription_mode == "unsupported":
            raise HTTPException(status_code=409, detail="所选服务商没有接入兼容的语音转写端点。")
        asr_model_id = body.asr_model_id.strip()
        capability, _, _ = lookup_model_capability(str(profile["provider_id"]), asr_model_id)
        if capability is not None and not capability.supports("audio"):
            raise HTTPException(
                status_code=409,
                detail="模型目录确认该模型不支持音频输入，请填写服务商提供的 ASR 模型 ID。",
            )
        profile_settings = {
            **profile,
            "model_provider": profile.get("provider_id"),
            "asr_model_id": asr_model_id,
        }
        if not _model_configuration_ready(
            runner,
            required_kinds={"audio"},
            settings_override=profile_settings,
        ):
            raise HTTPException(status_code=409, detail="所选配置档的 API Key 或服务地址不可用，请先检查模型配置。")
        save_transcription_step_config(
            runner.database,
            task_id,
            mode="cloud",
            profile=profile,
            asr_model_id=asr_model_id,
        )
    else:
        if body.local_model is None or body.local_language is None or body.local_device is None:
            raise HTTPException(status_code=422, detail="请选择本地模型、语音语言和设备。")
        if not local_asr.status()["installed"]:
            raise HTTPException(status_code=409, detail="本机转写组件尚未安装，请在设置中启用本机转写。")
        if not local_asr.model_ready(body.local_model):
            raise HTTPException(status_code=409, detail="所选本地模型尚未准备完成，请先前往设置下载并校验模型。")
        save_transcription_step_config(
            runner.database,
            task_id,
            mode="local",
            local_model=body.local_model,
            local_language=body.local_language,
            local_device=body.local_device,
        )
    return task_detail(task_id, request)


@_router.post("/tasks/{task_id}/reconcile-transcript", dependencies=[Depends(_same_origin)])
def reconcile_task_transcript(task_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        regenerate_transcript_draft(runner, task_id)
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在。") from exc
    return task_detail(task_id, request)


@_router.get("/tasks/{task_id}/transcript-comparison", dependencies=[Depends(_same_origin)])
def transcript_comparison(
    task_id: str,
    request: Request,
    artifact_id: str = Query(min_length=1, max_length=128),
) -> dict[str, object]:
    runner = _runner(request)
    try:
        return get_transcript_comparison_data(runner, task_id, artifact_id)
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务或校对版本不存在。") from exc


@_router.get("/artifacts/{artifact_id}", dependencies=[Depends(_same_origin)])
def artifact_detail(artifact_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="产物记录不存在。")
    return _artifact_payload(row, runner.config.archive_root)


@_router.get("/artifacts/{artifact_id}/text", dependencies=[Depends(_same_origin)])
def read_text_artifact(artifact_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文本产物不存在。")
    kind = str(row["kind"])
    if kind not in {
        "transcript", "transcript_revision", "transcript_draft", "ocr_result", "ocr_revision",
        "script", "script_revision", "knowledge_card", "knowledge_card_revision",
        "knowledge_card_approved", "final_script", "analysis", "structure_analysis_json",
        "video_notes", "note", "comment_analysis",
    }:
        raise HTTPException(status_code=422, detail="该产物不支持人工文本校对。")
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    try:
        registered = Path(str(row["path"]))
        if registered.is_symlink():
            raise ValueError("symbolic link")
        path = _assert_contained(registered, root, strict=True)
        digest, size = _hash_file(path)
        if path.is_symlink() or not path.is_file() or size > 5_000_000 or digest.casefold() != str(row["sha256"]).casefold():
            raise ValueError("text artifact changed")
        content = path.read_text(encoding="utf-8")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="文本产物不可用或已变化。") from None
    return {"artifact_id": artifact_id, "sha256": digest, "content": content}


@_router.put("/artifacts/{artifact_id}/text", dependencies=[Depends(_same_origin)])
def save_text_artifact_revision(
    artifact_id: str,
    body: ArtifactTextEditRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        original = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if original is None:
        raise HTTPException(status_code=404, detail="文本产物不存在。")
    original_kind = str(original["kind"])
    if original_kind not in {
        "transcript", "transcript_revision", "transcript_draft", "ocr_result", "ocr_revision",
        "script", "script_revision", "knowledge_card", "knowledge_card_revision",
        "knowledge_card_approved", "final_script",
    }:
        raise HTTPException(status_code=422, detail="该产物不支持人工文本校对。")
    source_metadata = _json_object(original["metadata_json"])
    review_copy = original_kind == "transcript_draft" or source_metadata.get("review_copy") is True
    content = body.content
    word_count: int | None = None
    if review_copy:
        content, word_count = update_review_copy_word_count(content)
    content_bytes = content.encode("utf-8")
    if b"\x00" in content_bytes or len(content_bytes) > 5_000_000:
        raise HTTPException(status_code=422, detail="校对文本格式无效或超过 5 MB。")
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    original_path = Path(str(original["path"]))
    try:
        if original_path.is_symlink():
            raise ValueError("symbolic link")
        safe_original = _assert_contained(original_path, root, strict=True)
        existing_digest, existing_size = _hash_file(safe_original)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=409, detail="原始文本文件不可用；未保存修改。") from None
    if (
        existing_size > 5_000_000
        or str(original["sha256"]).casefold() != body.expected_sha256.casefold()
    ):
        raise HTTPException(status_code=409, detail="原文已变化；请重新加载后再保存校对版本。")
    source_conflicted = existing_digest.casefold() != str(original["sha256"]).casefold()

    if original_kind.startswith("transcript"):
        new_kind = "transcript_revision"
    elif original_kind.startswith("ocr"):
        new_kind = "ocr_revision"
    elif original_kind in {"script", "script_revision", "final_script"}:
        new_kind = "script_revision"
    else:
        # Knowledge cards are a single living document per task: editing an
        # approved card keeps its approved state; everything else collapses
        # back to the base kind instead of stacking revision rows.
        new_kind = "knowledge_card_approved" if original_kind == "knowledge_card_approved" else "knowledge_card"
    new_id = uuid.uuid4().hex
    target = safe_original.parent / artifact_filename(new_kind, '.md', new_id, source_metadata, conflict=source_conflicted)
    digest = hashlib.sha256(content_bytes).hexdigest()
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_contained(target, root, strict=True)
        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "media_type": "text/markdown",
            "human_edited": True,
            "source_artifact_id": artifact_id,
            "source_sha256": str(original["sha256"]),
            "conflict_copy": source_conflicted,
        }
        if source_conflicted:
            metadata["observed_source_sha256"] = existing_digest
        for key in (
            "style", "model_id", "source_artifact_ids", "source_comment_ids",
            "card_type", "title", "raw_artifact_id", "asset_title", "platform",
            "source_id", "source_url", "ocr_guided_corrections", "ocr_correction_count",
            "ocr_is_separate_evidence", "manual_reconciliation", "human_review_required",
            "uncertain_terms",
        ):
            if key in source_metadata:
                metadata[key] = source_metadata[key]
        if new_kind == "script_revision":
            metadata.update({"finalized": False, "review_status": "edited"})
        elif new_kind.startswith("knowledge_card"):
            metadata["review_status"] = "approved" if new_kind == "knowledge_card_approved" else "pending"
            if new_kind == "knowledge_card_approved":
                metadata["human_reviewed"] = True
        if review_copy and word_count is not None:
            metadata["review_copy"] = True
            metadata["word_count"] = word_count
            metadata["word_count_rule"] = "Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded"
            source_ids = source_metadata.get("source_artifact_ids")
            if isinstance(source_ids, list):
                metadata["source_artifact_ids"] = [item for item in source_ids if isinstance(item, str)]
        with runner.database.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT sha256, path FROM artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, str(original["task_id"])),
            ).fetchone()
            if current is None or current["sha256"] != original["sha256"] or Path(str(current["path"])) != original_path:
                raise ValueError("source artifact changed")
            if new_kind.startswith("knowledge_card"):
                version_row = connection.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                       WHERE task_id = ? AND step_id = ? AND kind IN
                       ('knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved')""",
                    (str(original["task_id"]), str(original["step_id"])),
                ).fetchone()
            else:
                version_row = connection.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts
                       WHERE task_id = ? AND step_id = ? AND kind = ?""",
                    (str(original["task_id"]), str(original["step_id"]), new_kind),
                ).fetchone()
            version = int(version_row["version"])
            connection.execute(
                """INSERT INTO artifacts (
                       id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id,
                    str(original["task_id"]),
                    str(original["step_id"]),
                    new_kind,
                    str(target),
                    digest,
                    version,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            step_row = connection.execute(
                "SELECT output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
                (str(original["step_id"]), str(original["task_id"])),
            ).fetchone()
            if step_row is not None:
                outputs = _json_list(step_row["output_artifact_ids_json"])
                if new_id not in outputs:
                    outputs.append(new_id)
                if new_kind.startswith("knowledge_card"):
                    # Single-card overwrite: drop superseded card rows from the
                    # ledger (files stay archived) and from the step outputs.
                    stale_rows = connection.execute(
                        """SELECT id FROM artifacts
                           WHERE task_id = ? AND step_id = ? AND id != ?
                             AND kind IN ('knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved')""",
                        (str(original["task_id"]), str(original["step_id"]), new_id),
                    ).fetchall()
                    stale_ids = {str(row["id"]) for row in stale_rows}
                    if stale_ids:
                        connection.executemany(
                            "DELETE FROM artifacts WHERE id = ?",
                            [(stale_id,) for stale_id in sorted(stale_ids)],
                        )
                        outputs = [item for item in outputs if item not in stale_ids]
                connection.execute(
                    "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                    (json.dumps(outputs, ensure_ascii=False, separators=(",", ":")), str(original["step_id"]), str(original["task_id"])),
                )
            updated = connection.execute("SELECT * FROM artifacts WHERE id = ?", (new_id,)).fetchone()
            assert updated is not None
            result = _artifact_payload(updated, root)
    except ValueError:
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=409, detail="原始产物已变化；请重新加载后保存校对版本。") from None
    except BaseException:
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


@_router.post("/artifacts/{artifact_id}/review", dependencies=[Depends(_same_origin)])
def review_generated_artifact(
    artifact_id: str,
    body: ArtifactReviewRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    with runner.database.transaction() as connection:
        original = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if original is None:
        raise HTTPException(status_code=404, detail="待审核产物不存在。")
    original_kind = str(original["kind"])
    is_script = original_kind in {"script", "script_revision"}
    is_card = original_kind in {"knowledge_card", "knowledge_card_revision"}
    if not is_script and not is_card:
        raise HTTPException(status_code=422, detail="仅能审核口播稿或知识卡。")
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    source_metadata = _json_object(original["metadata_json"])
    try:
        source_path = Path(str(original["path"]))
        if source_path.is_symlink():
            raise ValueError("symlink")
        safe_source = _assert_contained(source_path, root, strict=True)
        source_digest, source_size = _hash_file(safe_source)
        if (
            not safe_source.is_file()
            or source_size > 5_000_000
            or source_digest.casefold() != str(original["sha256"]).casefold()
            or source_digest.casefold() != body.expected_sha256.casefold()
        ):
            raise ValueError("changed")
        content = safe_source.read_bytes()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=409, detail="产物已变化或不可用；请刷新后重新审核。") from None

    if is_card:
        # Single-card model: approving flips this card's status inline instead
        # of copying it into a separate approved artifact. The archived file
        # is unchanged, so its sha256 ledger entry stays valid.
        metadata = {
            **source_metadata,
            "review_status": "approved",
            "human_reviewed": True,
        }
        with runner.database.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT sha256, path FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if current is None or current["sha256"] != original["sha256"] or Path(str(current["path"])) != source_path:
                raise HTTPException(status_code=409, detail="产物已变化；请刷新后重新审核。") from None
            connection.execute(
                "UPDATE artifacts SET kind = 'knowledge_card_approved', metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), artifact_id),
            )
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            assert row is not None
        return {
            **_artifact_payload(row, root),
            "vault_write": _auto_write_card_to_vault(runner, artifact_id),
        }

    new_kind = "final_script" if is_script else "knowledge_card_approved"
    new_id = uuid.uuid4().hex
    target = safe_source.parent / artifact_filename(new_kind, '.md', new_id, source_metadata)
    metadata = {
        **source_metadata,
        "media_type": "text/markdown",
        "source_artifact_id": artifact_id,
        "source_sha256": source_digest,
        "review_status": "finalized" if is_script else "approved",
        "human_reviewed": True,
        "finalized": is_script,
    }
    digest = hashlib.sha256(content).hexdigest()
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_contained(target, root, strict=True)
        with runner.database.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT sha256, path FROM artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, str(original["task_id"])),
            ).fetchone()
            if current is None or current["sha256"] != original["sha256"] or Path(str(current["path"])) != source_path:
                raise ValueError("source changed")
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = ?",
                (str(original["task_id"]), str(original["step_id"]), new_kind),
            ).fetchone()
            connection.execute(
                """INSERT INTO artifacts (
                       id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id, str(original["task_id"]), str(original["step_id"]), new_kind,
                    str(target), digest, int(version_row["version"]),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            step_row = connection.execute(
                "SELECT output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
                (str(original["step_id"]), str(original["task_id"])),
            ).fetchone()
            if step_row is not None:
                output_ids = _json_list(step_row["output_artifact_ids_json"])
                if new_id not in output_ids:
                    output_ids.append(new_id)
                connection.execute(
                    "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                    (json.dumps(output_ids, separators=(",", ":")), str(original["step_id"]), str(original["task_id"])),
                )
            row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (new_id,)).fetchone()
            assert row is not None
            return _artifact_payload(row, root)
    except (OSError, ValueError):
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=409, detail="审核产物保存失败或来源已变化。") from None


@_router.post("/tasks/{task_id}/video-notes", dependencies=[Depends(_same_origin)])
def create_video_notes(
    task_id: str,
    body: VideoNotesRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if task.status in {"queued", "running", "awaiting_authorization"}:
        raise HTTPException(status_code=409, detail="请等待素材处理步骤结束后再生成视频资料。")
    archive_root = runner.config.archive_root
    if archive_root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    with runner.database.transaction() as connection:
        transcript_row = connection.execute(
            "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
            (body.transcript_artifact_id, task_id),
        ).fetchone()
        comment_rows = connection.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND kind = 'note' ORDER BY created_at DESC, id DESC",
            (task_id,),
        ).fetchall()
        supplemental_rows = connection.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND kind IN ('metadata', 'comment_analysis') ORDER BY created_at DESC, id DESC",
            (task_id,),
        ).fetchall()
    if transcript_row is None or str(transcript_row["kind"]) != "transcript_revision":
        raise HTTPException(status_code=422, detail="请选择本任务中人工另存的转写校对版本。")
    transcript_metadata = _json_object(transcript_row["metadata_json"])
    if transcript_metadata.get("human_edited") is not True or transcript_metadata.get("review_copy") is not True:
        raise HTTPException(status_code=422, detail="所选转写版本尚未完成校对，未生成视频资料。")
    transcript_path = Path(str(transcript_row["path"]))
    try:
        if transcript_path.is_symlink():
            raise ValueError("symbolic link")
        safe_transcript_path = _assert_contained(transcript_path, archive_root, strict=True)
        transcript_sha, transcript_size = _hash_file(safe_transcript_path)
        if (
            safe_transcript_path.is_symlink()
            or not safe_transcript_path.is_file()
            or transcript_size > 5_000_000
            or transcript_sha.casefold() != str(transcript_row["sha256"]).casefold()
        ):
            raise ValueError("text artifact changed")
        transcript_markdown = safe_transcript_path.read_text(encoding="utf-8")
    except (OSError, RuntimeError, ValueError, UnicodeError):
        raise HTTPException(status_code=409, detail="校对稿已变化或不可用；请重新加载后再生成。") from None

    comments_markdown: str | None = None
    comments_artifact_id: str | None = None
    for row in comment_rows:
        metadata = _json_object(row["metadata_json"])
        if metadata.get("scope") != "comments":
            continue
        comments_path = Path(str(row["path"]))
        try:
            if comments_path.is_symlink():
                raise ValueError("symbolic link")
            safe_comments_path = _assert_contained(comments_path, archive_root, strict=True)
            comment_sha, comment_size = _hash_file(safe_comments_path)
            if (
                safe_comments_path.is_symlink()
                or not safe_comments_path.is_file()
                or comment_size > 25_000_000
                or comment_sha.casefold() != str(row["sha256"]).casefold()
            ):
                raise ValueError("comment note changed")
            comments_markdown = safe_comments_path.read_text(encoding="utf-8")
            comments_artifact_id = str(row["id"])
            break
        except (OSError, RuntimeError, ValueError, UnicodeError):
            raise HTTPException(status_code=409, detail="评论归档已变化或不可用；未生成视频资料。") from None

    archive_metadata: dict[str, object] | None = None
    comment_analysis_markdown: str | None = None
    supplemental_ids: list[str] = []
    for row in supplemental_rows:
        kind = str(row["kind"])
        if (kind == "metadata" and archive_metadata is not None) or (kind == "comment_analysis" and comment_analysis_markdown is not None):
            continue
        if kind == "comment_analysis" and comments_artifact_id:
            analysis_sources = _json_object(row["metadata_json"]).get("source_artifact_ids")
            if not isinstance(analysis_sources, list) or comments_artifact_id not in analysis_sources:
                continue
        try:
            path = Path(str(row["path"]))
            if path.is_symlink():
                raise ValueError("symbolic link")
            safe_path = _assert_contained(path, archive_root, strict=True)
            digest, size = _hash_file(safe_path)
            if safe_path.is_symlink() or not safe_path.is_file() or size > 5_000_000 or digest.casefold() != str(row["sha256"]).casefold():
                raise ValueError("supplemental artifact changed")
            content_text = safe_path.read_text(encoding="utf-8")
            if kind == "metadata":
                parsed = json.loads(content_text)
                if not isinstance(parsed, dict):
                    raise ValueError("metadata is not an object")
                archive_metadata = parsed
            else:
                comment_analysis_markdown = content_text
            supplemental_ids.append(str(row["id"]))
        except (OSError, RuntimeError, ValueError, UnicodeError):
            raise HTTPException(status_code=409, detail="元数据或评论摘要已变化或不可用；未生成视频资料。") from None

    asset = _asset_for_task(runner, task)
    source_url = task.source.value if task.source.kind.value == "url" else "本地导入（未获取平台来源）"
    published_at = asset.published_at.isoformat() if asset.published_at else None
    archived_at = asset.archived_at.isoformat() if asset.archived_at else None
    source_ids = transcript_metadata.get("source_artifact_ids")
    if not isinstance(source_ids, list):
        source_ids = []
    source_ids = [item for item in source_ids if isinstance(item, str)]
    if comments_artifact_id:
        source_ids.append(comments_artifact_id)
    source_ids.extend(supplemental_ids)
    created_at = datetime.now(timezone.utc)
    content, word_count = render_video_notes(
        title=asset.title,
        platform=asset.platform,
        source_id=asset.source_id,
        source_url=source_url,
        author=asset.author,
        published_at=published_at,
        archived_at=archived_at,
        transcript_markdown=transcript_markdown,
        transcript_artifact_id=body.transcript_artifact_id,
        source_artifact_ids=source_ids,
        comments_markdown=comments_markdown,
        created_at=created_at,
        archive_metadata=archive_metadata,
        comment_analysis_markdown=comment_analysis_markdown,
    )
    folder = _assert_contained(safe_transcript_path.parent, archive_root, strict=True)
    target = folder / "视频资料.md"
    version_number = 1
    while target.exists() or target.is_symlink():
        if target.is_symlink():
            raise HTTPException(status_code=409, detail="视频资料目标是符号链接，未写入。")
        version_number += 1
        target = folder / f"视频资料-版本{version_number}.md"
        if version_number > 10_000:
            raise HTTPException(status_code=409, detail="视频资料安全版本名已用完，请检查归档目录。")
    digest = hashlib.sha256(content).hexdigest()
    descriptor: int | None = None
    artifact_id = uuid.uuid4().hex
    now = created_at.isoformat()
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_contained(target, archive_root, strict=True)
        if target.is_symlink() or target.stat().st_size != len(content):
            raise ValueError("video notes changed while being written")
        metadata = {
            "media_type": "text/markdown",
            "scope": "video_notes",
            "review_copy_artifact_id": body.transcript_artifact_id,
            "source_artifact_ids": list(dict.fromkeys([body.transcript_artifact_id, *source_ids])),
            "comments_artifact_id": comments_artifact_id,
            "word_count": word_count,
            "word_count_rule": "Han characters plus contiguous ASCII letters/digits; whitespace/punctuation excluded",
            "immutable_version": True,
        }
        with runner.database.transaction(write=True) as connection:
            step_row = connection.execute(
                "SELECT output_artifact_ids_json FROM task_steps WHERE id = ? AND task_id = ?",
                (str(transcript_row["step_id"]), task_id),
            ).fetchone()
            if step_row is None:
                raise ValueError("transcription step is missing")
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE task_id = ? AND step_id = ? AND kind = 'note'",
                (task_id, str(transcript_row["step_id"])),
            ).fetchone()
            connection.execute(
                """INSERT INTO artifacts (
                       id, task_id, step_id, kind, path, sha256, version, metadata_json, created_at
                   ) VALUES (?, ?, ?, 'note', ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    task_id,
                    str(transcript_row["step_id"]),
                    str(target),
                    digest,
                    int(version_row["version"]),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            outputs = _json_list(step_row["output_artifact_ids_json"])
            if artifact_id not in outputs:
                outputs.append(artifact_id)
            connection.execute(
                "UPDATE task_steps SET output_artifact_ids_json = ? WHERE id = ? AND task_id = ?",
                (json.dumps(outputs, ensure_ascii=False, separators=(",", ":")), str(transcript_row["step_id"]), task_id),
            )
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))
            saved = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            assert saved is not None
            result = _artifact_payload(saved, archive_root)
    except ValueError:
        try:
            if target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                target.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=409, detail="产物状态已变化；请刷新任务后重试。") from None
    except BaseException:
        try:
            if target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                target.unlink()
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


@_router.post(
    "/tasks/{task_id}/steps/{step_id}/authorize-comment-analysis",
    status_code=202,
    dependencies=[Depends(_same_origin)],
)
def authorize_comment_analysis(
    task_id: str,
    step_id: str,
    body: AuthorizeCommentAnalysisRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    if not body.authorized:
        raise HTTPException(status_code=422, detail="请先确认授权发送更新后的评论。")
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")

    with runner._lock:
        task = runner.repository.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在。")
        if not _task_model_configuration_ready(runner, task_id):
            raise HTTPException(status_code=409, detail="原任务所用模型配置或 API Key 当前不可用。")
        step = next((item for item in task.steps if item.id == step_id), None)
        if (step is None or step.kind is not StepKind.ANALYZE_COMMENTS
                or step.status not in {"interrupted", "blocked"}
                or step.error_code != "comments_analysis_stale"
                or task.status in {"running", "queued"}):
            raise HTTPException(status_code=409, detail="评论分析状态已变化，请刷新后重试。")

        with runner.database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM artifacts WHERE task_id = ? AND kind = 'note'
                   ORDER BY created_at DESC, version DESC, id DESC""", (task_id,),
            ).fetchall()
            state = connection.execute(
                "SELECT collected_count FROM comment_collection_state WHERE task_id = ?", (task_id,),
            ).fetchone()
        latest = None
        for row in rows:
            if _json_object(row["metadata_json"]).get("scope") != "comments":
                continue
            try:
                path = Path(str(row["path"]))
                if path.is_symlink():
                    continue
                safe = _assert_contained(path, root, strict=True)
                digest, _ = _hash_file(safe)
                if safe.is_file() and digest.casefold() == str(row["sha256"]).casefold():
                    latest = row
                    break
            except (OSError, RuntimeError, ValueError):
                continue
        if latest is None or str(latest["id"]) != body.artifact_id:
            raise HTTPException(status_code=409, detail="评论资料已变化或不可用，请刷新并重新确认。")
        if state is None or _json_object(latest["metadata_json"]).get("collected_count") != state["collected_count"]:
            raise HTTPException(status_code=409, detail="评论资料尚未包含全部已保存评论，请先重试评论采集。")
        now = datetime.now(timezone.utc).isoformat()
        settings = task_model_settings(runner.config, task_id)
        provider_id = str(settings.get("model_provider"))
        with runner.database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO consents (id, task_id, step_kind, provider_id, model_id,
                       artifact_ids_json, authorized, authorized_at)
                   VALUES (?, ?, 'analyze_comments', ?, ?, ?, 1, ?)""",
                (uuid.uuid4().hex, task_id, provider_id, str(settings.get("model_id")),
                 json.dumps([body.artifact_id], separators=(",", ":")), now),
            )
        try:
            runner.retry_step(task_id, step_id)
        except RunnerError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"queued": True, "authorized_artifact_ids": [body.artifact_id]}


@_router.post(
    "/tasks/{task_id}/steps/{step_id}/authorize-ocr",
    status_code=202,
    dependencies=[Depends(_same_origin)],
)
def authorize_ocr_frames(
    task_id: str,
    step_id: str,
    body: AuthorizeOcrRequest,
    request: Request,
) -> dict[str, object]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if not _task_model_configuration_ready(runner, task_id, required_kinds={"text"}):
        raise HTTPException(status_code=409, detail="原任务所用模型配置或 API Key 当前不可用。")
    settings = task_model_settings(runner.config, task_id)
    provider_id = settings.get("model_provider")
    model_id = settings.get("model_id")
    if isinstance(provider_id, str) and isinstance(model_id, str):
        capability, _, _ = lookup_model_capability(provider_id, model_id)
        if capability is not None and not capability.supports("image"):
            raise HTTPException(status_code=409, detail="模型目录显示当前模型不支持图像输入，请修改模型 ID 或选择其他模型。")
    step = next((item for item in task.steps if item.id == step_id), None)
    if step is None:
        raise HTTPException(status_code=404, detail="任务步骤不存在。")
    if step.kind is not StepKind.OCR or step.status != "awaiting_authorization":
        raise HTTPException(status_code=409, detail="该步骤当前没有等待图片授权。")
    if len(set(body.artifact_ids)) != len(body.artifact_ids):
        raise HTTPException(status_code=422, detail="关键帧不能重复选择。")

    archive_root = runner.config.archive_root
    if archive_root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    placeholders = ",".join("?" for _ in body.artifact_ids)
    with runner.database.transaction() as connection:
        rows = connection.execute(
            f"""SELECT * FROM artifacts WHERE task_id = ? AND step_id = ?
               AND kind = 'ocr_frame' AND id IN ({placeholders})""",
            (task_id, step_id, *body.artifact_ids),
        ).fetchall()
    if {str(row["id"]) for row in rows} != set(body.artifact_ids):
        raise HTTPException(status_code=422, detail="选择的图片不属于当前 OCR 步骤。")

    source_ids: set[str] = set()
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        source_id = metadata.get("source_artifact_id")
        if not isinstance(source_id, str):
            raise HTTPException(status_code=422, detail="关键帧来源无法核实。")
        source_ids.add(source_id)
        path = Path(str(row["path"]))
        try:
            if path.is_symlink():
                raise ValueError("symbolic link")
            safe_path = _assert_contained(path, archive_root, strict=True)
            digest, size = _hash_file(safe_path)
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=422, detail="关键帧文件不可用或已变化。") from None
        if not safe_path.is_file() or size > 8 * 1024 * 1024 or digest.casefold() != str(row["sha256"]).casefold():
            raise HTTPException(status_code=422, detail="关键帧文件不可用或已变化。")
    if len(source_ids) != 1:
        raise HTTPException(status_code=422, detail="所选关键帧必须来自同一份已归档素材。")
    source_artifact_id = next(iter(source_ids))
    with runner.database.transaction() as connection:
        source_row = connection.execute(
            "SELECT id FROM artifacts WHERE task_id = ? AND id = ? AND kind IN ('video', 'media')",
            (task_id, source_artifact_id),
        ).fetchone()
    if source_row is None:
        raise HTTPException(status_code=422, detail="关键帧对应的归档媒体不存在。")

    now = datetime.now(timezone.utc).isoformat()
    # Preserve the user's checkbox order as shown in the preview list.
    row_by_id = {str(row["id"]): row for row in rows}
    ordered_ids = [artifact_id for artifact_id in body.artifact_ids if artifact_id in row_by_id]
    with runner.database.transaction(write=True) as connection:
        prior = connection.execute(
            "SELECT id FROM consents WHERE task_id = ? AND step_kind = 'ocr' ORDER BY authorized_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if prior is None:
            connection.execute(
                """INSERT INTO consents (
                       id, task_id, step_kind, provider_id, model_id, artifact_ids_json,
                       authorized, authorized_at
                   ) VALUES (?, ?, 'ocr', ?, ?, ?, 1, ?)""",
                (
                    uuid.uuid4().hex,
                    task_id,
                    str(settings.get("model_provider")),
                    str(settings.get("model_id")),
                    _json_list(ordered_ids),
                    now,
                ),
            )
        else:
            connection.execute(
                """UPDATE consents SET provider_id = ?, model_id = ?,
                       artifact_ids_json = ?, authorized = 1, authorized_at = ? WHERE id = ?""",
                (str(settings.get("model_provider")), str(settings.get("model_id")), _json_list(ordered_ids), now, str(prior["id"])),
            )
    try:
        runner.retry_step(task_id, step_id)
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"queued": True, "authorized_artifact_ids": ordered_ids}


@_router.post(
    "/tasks/{task_id}/steps/{step_id}/retry",
    status_code=202,
    dependencies=[Depends(_same_origin)],
)
def retry_step(
    task_id: str,
    step_id: str,
    request: Request,
    body: RetryStepRequest | None = None,
) -> dict[str, object]:
    runner = _runner(request)
    try:
        task = runner.repository.get_task(task_id)
        if task is None:
            raise KeyError("task not found")
        step = next((item for item in task.steps if item.id == step_id), None)
        if step is None:
            raise KeyError("step not found")
        transcription_consent: tuple[str, str] | None = None
        if step.kind is StepKind.TRANSCRIBE:
            if local_asr.get_task_options(runner.database, task_id) is None:
                if body is None or body.transcription_authorized is not True:
                    raise HTTPException(
                        status_code=422,
                        detail="请确认本次会将音频重新发送给云端模型，并可能产生费用。",
                    )
                settings = transcription_step_settings(runner.config, task_id)
                provider_id = settings.get("model_provider")
                model_id = settings.get("asr_model_id")
                preset = get_provider_preset(provider_id)
                if not isinstance(provider_id, str) or not isinstance(model_id, str) or preset is None:
                    raise HTTPException(status_code=409, detail="当前任务的云端 ASR 配置不完整，请先修改本步配置。")
                if preset.transcription_mode == "unsupported":
                    raise HTTPException(status_code=409, detail="当前服务商没有接入兼容的语音转写端点。")
                capability, _, _ = lookup_model_capability(provider_id, model_id)
                if capability is not None and not capability.supports("audio"):
                    raise HTTPException(
                        status_code=409,
                        detail="模型目录确认该模型不支持音频输入，请修改本步 ASR 模型 ID。",
                    )
                if not _model_configuration_ready(
                    runner,
                    required_kinds={"audio"},
                    settings_override=settings,
                ):
                    raise HTTPException(status_code=409, detail="当前任务的 ASR 模型、服务地址或 API Key 不可用。")
                transcription_consent = (provider_id, model_id)
        if task.source.kind.value == "file":
            if body is not None and body.source_url:
                raise ValueError("本地导入任务不能更换为公开视频链接。")
            runner.retry_step(
                task_id,
                step_id,
                ocr_action=body.ocr_action if body else None,
                transcription_consent=transcription_consent,
            )
        else:
            candidate = body.source_url if body and body.source_url else task.source.value
            parsed = _parse_supported_source(candidate)
            if task.source.platform and parsed.platform != task.source.platform:
                raise ValueError("重试链接的平台与原任务不一致。")
            if task.source.source_id and parsed.media_id and task.source.source_id != parsed.media_id:
                raise ValueError("重试链接的作品 ID 与原任务不一致。")
            runner.retry_step(
                task_id,
                step_id,
                source_url=parsed.url if body and body.source_url else None,
                ocr_action=body.ocr_action if body else None,
                transcription_consent=transcription_consent,
            )
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务或步骤不存在。") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_source_validation_message(exc)) from exc
    return task_detail(task_id, request)


@_router.post("/tasks/{task_id}/cancel", dependencies=[Depends(_same_origin)])
def cancel_task(task_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        runner.cancel(task_id)
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在。") from exc
    return task_detail(task_id, request)


@_router.post("/tasks/{task_id}/steps/{step_id}/skip", dependencies=[Depends(_same_origin)])
def skip_task_step(task_id: str, step_id: str, request: Request) -> dict[str, object]:
    runner = _runner(request)
    try:
        runner.skip_step(task_id, step_id)
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务或步骤不存在。") from exc
    return task_detail(task_id, request)


@_router.post("/tasks/{task_id}/open-folder", dependencies=[Depends(_same_origin)])
def open_task_folder(task_id: str, request: Request) -> dict[str, bool]:
    runner = _runner(request)
    task = runner.repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    asset = _asset_for_task(runner, task)
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="任务归档目录尚不可用。")
    try:
        registered_value = asset.archive_path
        if not registered_value:
            with runner.database.transaction() as connection:
                artifact_row = connection.execute(
                    "SELECT path FROM artifacts WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            registered_value = str(artifact_row["path"]) if artifact_row is not None else None
        if not registered_value:
            raise ValueError("archive path is unavailable")
        registered_path = Path(registered_value)
        if registered_path.is_symlink():
            raise ValueError("archive path is a symbolic link")
        safe_path = _assert_contained(registered_path, root)
        folder = safe_path if safe_path.is_dir() else safe_path.parent
        safe_folder = _assert_contained(folder, root, strict=True)
        if safe_folder.is_symlink() or not safe_folder.is_dir():
            raise ValueError("archive folder is unavailable")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=409, detail="任务归档目录不存在或不可访问。") from None
    try:
        _open_local_path(safe_folder)
    except OSError:
        raise HTTPException(status_code=503, detail="无法打开本机文件管理器。") from None
    return {"opened": True}


@_router.post("/tasks/{task_id}/open-output", dependencies=[Depends(_same_origin)])
def open_task_output(
    task_id: str,
    request: Request,
    relative_path: str = Query(min_length=1, max_length=2048),
    reveal: bool = Query(default=False),
) -> dict[str, bool]:
    runner = _runner(request)
    try:
        path = resolve_task_output(
            runner.database,
            task_id,
            runner.config.archive_root,
            relative_path,
            reveal=reveal,
        )
        _open_local_path(path, reveal=reveal)
    except TaskArchiveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=503, detail="无法打开本机文件或文件管理器。") from None
    return {"opened": True}


@_router.post("/artifacts/{artifact_id}/open", dependencies=[Depends(_same_origin)])
def open_artifact(
    artifact_id: str,
    request: Request,
    reveal: bool = Query(default=False),
) -> dict[str, bool]:
    runner = _runner(request)
    root = runner.config.archive_root
    if root is None:
        raise HTTPException(status_code=409, detail="请先配置归档目录。")
    with runner.database.transaction() as connection:
        row = connection.execute("SELECT path FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    try:
        registered_path = Path(str(row["path"]))
        if registered_path.is_symlink():
            raise ValueError("artifact is a symbolic link")
        path = _assert_contained(registered_path, root, strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("artifact is unavailable")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=409, detail="文件不存在或不可访问。") from None
    if not reveal and path.suffix.casefold() not in {
        ".aac", ".flac", ".jpeg", ".jpg", ".json", ".m4a", ".md", ".mkv", ".mov", ".mp3", ".mp4",
        ".oga", ".ogg", ".opus", ".png", ".srt", ".txt", ".vtt", ".wav", ".webm", ".webp",
    }:
        raise HTTPException(status_code=422, detail="该文件类型不支持直接打开；可以选择在文件夹中定位。")
    try:
        _open_local_path(path, reveal=reveal)
    except OSError:
        raise HTTPException(status_code=503, detail="无法打开本机文件或文件管理器。") from None
    return {"opened": True}


@_router.post("/artifacts/{artifact_id}/open-vault-note", dependencies=[Depends(_same_origin)])
def open_vault_note(
    artifact_id: str,
    request: Request,
    reveal: bool = Query(default=False),
) -> dict[str, bool]:
    """Open an approved card's vault note, or reveal it in the file manager.

    The note location is resolved server-side from the card metadata that the
    auto-write step recorded; clients never pass a path.
    """
    runner = _runner(request)
    root_value = runner.config.get_settings().get("obsidian_output_root")
    if not isinstance(root_value, str) or not root_value:
        raise HTTPException(status_code=409, detail="请先配置知识库输出目录。")
    with runner.database.transaction() as connection:
        row = connection.execute("SELECT kind, metadata_json FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="知识卡不存在。")
    if str(row["kind"]) != "knowledge_card_approved":
        raise HTTPException(status_code=422, detail="仅已审核知识卡支持打开知识库笔记。")
    note_path_value = _json_object(row["metadata_json"]).get("vault_note_path")
    if not isinstance(note_path_value, str) or not note_path_value.strip():
        raise HTTPException(status_code=409, detail="该知识卡尚未写入知识库。")
    try:
        vault_root = Path(root_value)
        note_path = vault_root / note_path_value.strip()
        if note_path.is_symlink() or not note_path.is_file() or note_path.suffix.casefold() != ".md":
            raise ValueError("note unavailable")
        safe_note = _assert_contained(note_path, vault_root, strict=True)
        if safe_note.is_symlink() or not safe_note.is_file():
            raise ValueError("note unavailable")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=409, detail="知识库笔记不存在或已被移动。") from None
    try:
        _open_local_path(safe_note, reveal=reveal)
    except OSError:
        raise HTTPException(status_code=503, detail="无法打开本机文件或文件管理器。") from None
    return {"opened": True}


@_router.get("/artifacts/{artifact_id}/file", dependencies=[Depends(_same_origin)])
def get_artifact_file(artifact_id: str, request: Request) -> FileResponse:
    runner = _runner(request)
    root = runner.config.archive_root
    with runner.database.transaction() as connection:
        row = connection.execute(
            """SELECT artifacts.path, artifacts.sha256, tasks.legacy_path
               FROM artifacts JOIN tasks ON tasks.id = artifacts.task_id
               WHERE artifacts.id = ?""",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    try:
        registered_path = Path(str(row["path"]))
        if registered_path.is_symlink():
            raise ValueError("registered file is a symbolic link")
        path: Path | None = None
        if root is not None:
            try:
                path = _assert_contained(registered_path, root, strict=True)
            except (OSError, ValueError):
                path = None
        if path is None:
            legacy_path_value = row["legacy_path"]
            if not isinstance(legacy_path_value, str) or not legacy_path_value:
                raise ValueError("artifact is outside the configured archive")
            legacy_source = Path(legacy_path_value)
            if legacy_source.is_symlink():
                raise ValueError("legacy source record is not a regular file")
            legacy_source = legacy_source.resolve(strict=True)
            if not legacy_source.is_file():
                raise ValueError("legacy source record is unavailable")
            approved_legacy_roots = (
                PROJECT_ROOT / "data" / "anonymous-jobs",
                PROJECT_ROOT / "data" / "state",
            )
            approved = False
            for allowed_root in approved_legacy_roots:
                if allowed_root.is_symlink():
                    continue
                try:
                    _assert_contained(legacy_source, allowed_root, strict=True)
                    approved = True
                    break
                except (OSError, ValueError):
                    continue
            if not approved:
                raise ValueError("artifact task is not an approved legacy record")
            downloads_root = PROJECT_ROOT / "downloads"
            if downloads_root.is_symlink():
                raise ValueError("legacy downloads directory is not safe")
            path = _assert_contained(registered_path, downloads_root, strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError("registered file is unavailable")
        digest, _ = _hash_file(path)
        if digest.casefold() != str(row["sha256"]).casefold():
            raise ValueError("registered file hash changed")
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="文件不存在。") from None
    inline_extensions = {
        ".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".wav",
        ".jpg", ".jpeg", ".png", ".webp",
    }
    wants_inline = request.query_params.get("inline") == "1"
    disposition = "inline" if wants_inline and path.suffix.casefold() in inline_extensions else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        content_disposition_type=disposition,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _video_publish_manager(request: Request) -> VideoPublishManager:
    manager = getattr(request.app.state, "video_publish_manager", None)
    if not isinstance(manager, VideoPublishManager):
        raise HTTPException(status_code=503, detail="视频发布执行器尚未启动，请重启工作台。")
    return manager


@_router.get("/video-publish/status", dependencies=[Depends(_same_origin)])
def video_publish_status(request: Request) -> dict[str, object]:
    return {"sau": SocialAutoUploadAdapter(_runner(request).database).status()}


@_router.put("/video-publish/settings", dependencies=[Depends(_same_origin)])
def save_video_publish_settings(body: VideoPublishSettingsRequest, request: Request) -> dict[str, object]:
    try:
        adapter = SocialAutoUploadAdapter(_runner(request).database)
        adapter.set_project_path(body.sau_project_path)
        return {"sau": adapter.status()}
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.get("/video-publish/platforms", dependencies=[Depends(_same_origin)])
def get_video_publish_platforms(
    request: Request,
    scope: Literal["video", "article"] = Query(default="video"),
) -> dict[str, object]:
    return {"platforms": video_publish_platforms(_runner(request).database, scope=scope)}


@_router.get("/video-publish/accounts", dependencies=[Depends(_same_origin)])
def get_video_publish_accounts(request: Request) -> dict[str, object]:
    database = _runner(request).database
    return {"items": list_video_publish_accounts(database), "notices": list_video_publish_duplicate_notices(database)}


@_router.get("/video-publish/accounts/{account_id}/login-qr", dependencies=[Depends(_same_origin_qr)])
def get_video_publish_account_login_qr(account_id: str, request: Request) -> Response:
    image = latest_video_publish_login_qr(_runner(request).database, account_id)
    if image is None:
        raise HTTPException(status_code=404, detail="账号登录二维码尚未生成或已失效。")
    return Response(
        content=image,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@_router.post("/video-publish/accounts", status_code=201, dependencies=[Depends(_same_origin)])
def create_video_publish_account(body: VideoPublishAccountCreateRequest, request: Request) -> dict[str, object]:
    try:
        return add_video_publish_account(
            _runner(request).database, body.platform_id, body.account_alias, body.display_name,
        )
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/login", dependencies=[Depends(_same_origin)])
def login_video_publish_account(account_id: str, request: Request) -> dict[str, object]:
    manager = _video_publish_manager(request)
    status = manager.adapter.status()
    if not status["configured"]:
        raise HTTPException(status_code=409, detail=str(status["message"]))
    try:
        account = request_video_publish_account_action(manager.database, account_id, "login")
        manager.enqueue_account_action(account_id, "login")
        return account
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/cancel-login", dependencies=[Depends(_same_origin)])
def cancel_video_publish_account_login(account_id: str, request: Request) -> dict[str, str]:
    try:
        _video_publish_manager(request).cancel_account_login(account_id)
        return {"message": "正在取消登录。"}
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/check", dependencies=[Depends(_same_origin)])
def check_video_publish_account(account_id: str, request: Request) -> dict[str, object]:
    manager = _video_publish_manager(request)
    status = manager.adapter.status()
    if not status["configured"]:
        raise HTTPException(status_code=409, detail=str(status["message"]))
    try:
        account = request_video_publish_account_action(manager.database, account_id, "check")
        manager.enqueue_account_action(account_id, "check")
        return account
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/workbench", dependencies=[Depends(_same_origin)])
def open_video_publish_account_workbench(account_id: str, request: Request) -> dict[str, str]:
    try:
        return _video_publish_manager(request).open_account_workbench(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/repost-history", dependencies=[Depends(_same_origin)])
def get_douyin_repost_history(account_id: str, request: Request) -> dict[str, object]:
    try:
        return {"items": _video_publish_manager(request).get_repost_history(account_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/accounts/{account_id}/poi-search", dependencies=[Depends(_same_origin)])
def search_douyin_poi(account_id: str, body: VideoPublishPoiSearchRequest, request: Request) -> dict[str, object]:
    try:
        return _video_publish_manager(request).search_douyin_poi(account_id, body.keyword, body.city_code)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/douyin/topic-search", dependencies=[Depends(_same_origin)])
def search_douyin_topics_endpoint(body: VideoPublishTopicSearchRequest, request: Request) -> dict[str, object]:
    try:
        return {"items": search_douyin_topics(body.keyword)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except DouyinTopicSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@_router.post("/video-publish/accounts/{account_id}/xiaohongshu-poi-search", dependencies=[Depends(_same_origin)])
def search_xhs_poi(account_id: str, body: VideoPublishPoiSearchRequest, request: Request) -> dict[str, object]:
    try:
        return _video_publish_manager(request).search_xhs_poi(account_id, body.keyword)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


def _bilibili_account_file(account_id: str, request: Request) -> Path:
    manager = _video_publish_manager(request)
    accounts = list_video_publish_accounts(manager.database)
    account = next((item for item in accounts if item['id'] == account_id and item['platform_id'] == 'bilibili' and item['status'] == 'connected'), None)
    if not account:
        raise HTTPException(status_code=404, detail='B站账号未连接，请先登录。')
    alias = account['account_alias']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', alias):
        raise HTTPException(status_code=400, detail='B站账号信息无效。')
    command, message = manager.adapter.resolve_command()
    if command is None:
        raise HTTPException(status_code=409, detail=message)
    return command.cwd / 'cookies' / f'bilibili_{alias}.json'


@_router.get("/video-publish/accounts/{account_id}/bilibili-categories", dependencies=[Depends(_same_origin)])
def get_bilibili_categories(account_id: str, request: Request) -> dict[str, object]:
    try:
        return {'items': fetch_bilibili_categories(_bilibili_account_file(account_id, request))}
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@_router.get("/video-publish/accounts/{account_id}/bilibili-statements", dependencies=[Depends(_same_origin)])
def get_bilibili_statements(account_id: str, request: Request) -> dict[str, object]:
    try:
        return {'items': fetch_bilibili_statements(_bilibili_account_file(account_id, request))}
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@_router.delete("/video-publish/accounts/{account_id}", status_code=204, dependencies=[Depends(_same_origin)])
def delete_video_publish_account(account_id: str, request: Request) -> None:
    try:
        _video_publish_manager(request).logout_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布账号不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/covers", status_code=201, dependencies=[Depends(_same_origin)])
async def upload_video_publish_cover(request: Request, file: UploadFile = File(...)) -> dict[str, object]:
    try:
        receipt = await save_video_publish_cover(file, database=_runner(request).database)
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=500, detail="无法写入视频封面目录，请检查目录设置。") from None
    finally:
        await file.close()
    return receipt.to_payload()


@_router.get("/video-publish/covers/{upload_id}", dependencies=[Depends(_same_origin)])
def preview_video_publish_cover(upload_id: str, request: Request) -> FileResponse:
    try:
        path = get_video_publish_cover_upload_path(_runner(request).database, upload_id)
    except VideoPublishError:
        raise HTTPException(status_code=404, detail="封面图片不存在或已清理。") from None
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@_router.delete("/video-publish/covers/{upload_id}", dependencies=[Depends(_same_origin)])
def discard_video_publish_cover(upload_id: str, request: Request) -> dict[str, bool]:
    if not release_video_publish_cover(_runner(request).database, upload_id):
        raise HTTPException(status_code=404, detail="封面图片不存在或已清理。")
    return {"deleted": True}


@_router.post("/video-publish/covers/keepalive", dependencies=[Depends(_same_origin)])
def keep_video_publish_covers_endpoint(body: VideoPublishCoverKeepaliveRequest, request: Request) -> dict[str, int]:
    if any(not re.fullmatch(r"[0-9a-f]{32}", image_id) for image_id in body.ids):
        raise HTTPException(status_code=422, detail="封面标识无效。")
    return {"kept": keep_video_publish_covers(_runner(request).database, list(dict.fromkeys(body.ids)))}


@_router.get("/video-publish/media", dependencies=[Depends(_same_origin)])
def get_video_publish_media(request: Request, limit: int = Query(default=100, ge=1, le=200)) -> dict[str, object]:
    runner = _runner(request)
    return {"items": list_video_media(runner.database, runner.config.archive_root, limit=limit)}


@_router.get("/video-publish/drafts", dependencies=[Depends(_same_origin)])
def get_video_publish_drafts(request: Request) -> dict[str, object]:
    return {"items": list_video_publish_drafts(_runner(request).database)}


@_router.post("/video-publish/local-sources/select", dependencies=[Depends(_same_origin)])
def select_local_video_source(request: Request) -> dict[str, object]:
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="仅接受本机页面发起的选择请求。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请求格式无效。")
    try:
        selected = choose_local_video()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="无法打开视频选择窗口，请检查本机桌面环境。") from exc
    if not selected:
        return {"source": None}
    try:
        return {"source": register_local_video_source(_runner(request).database, Path(selected))}
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.get("/video-publish/local-sources/{source_id}/media", dependencies=[Depends(_same_origin_qr)])
def get_local_video_source_media(source_id: str, request: Request) -> FileResponse:
    try:
        path = get_local_video_source(_runner(request).database, source_id).path
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@_router.post("/video-publish/drafts", status_code=201, dependencies=[Depends(_same_origin)])
def create_video_publish_draft_endpoint(body: VideoPublishDraftSaveRequest, request: Request) -> dict[str, object]:
    try:
        return save_video_publish_draft(
            _runner(request).database,
            draft_id=None,
            upload_id=body.upload_id,
            local_source_id=body.local_source_id,
            cover_upload_ids=body.cover_upload_ids,
            form=body.form,
        )
    except (VideoPublishError, UploadError) as exc:
        status_code = exc.status_code if isinstance(exc, VideoPublishError) else 422
        message = exc.message if isinstance(exc, VideoPublishError) else str(exc)
        raise HTTPException(status_code=status_code, detail=message) from None
    except OSError:
        raise HTTPException(status_code=500, detail="无法写入本地草稿文件，请检查磁盘空间。") from None


@_router.put("/video-publish/drafts/{draft_id}", dependencies=[Depends(_same_origin)])
def update_video_publish_draft_endpoint(
    draft_id: str,
    body: VideoPublishDraftSaveRequest,
    request: Request,
) -> dict[str, object]:
    try:
        return save_video_publish_draft(
            _runner(request).database,
            draft_id=draft_id,
            upload_id=body.upload_id,
            local_source_id=body.local_source_id,
            cover_upload_ids=body.cover_upload_ids,
            form=body.form,
        )
    except (VideoPublishError, UploadError) as exc:
        status_code = exc.status_code if isinstance(exc, VideoPublishError) else 422
        message = exc.message if isinstance(exc, VideoPublishError) else str(exc)
        raise HTTPException(status_code=status_code, detail=message) from None
    except OSError:
        raise HTTPException(status_code=500, detail="无法写入本地草稿文件，请检查磁盘空间。") from None


@_router.post("/video-publish/drafts/{draft_id}/restore", dependencies=[Depends(_same_origin)])
async def restore_video_publish_draft_endpoint(draft_id: str, request: Request) -> dict[str, object]:
    try:
        restored = await restore_video_publish_draft(_runner(request).database, draft_id)
    except (VideoPublishError, UploadError) as exc:
        status_code = exc.status_code if isinstance(exc, VideoPublishError) else 409
        message = exc.message if isinstance(exc, VideoPublishError) else str(exc)
        raise HTTPException(status_code=status_code, detail=message) from None
    except OSError:
        raise HTTPException(status_code=500, detail="无法恢复本地草稿文件，请检查磁盘空间。") from None
    if restored is None:
        raise HTTPException(status_code=404, detail="本地草稿不存在或已删除。")
    return restored


@_router.get("/video-publish/drafts/{draft_id}/media", dependencies=[Depends(_same_origin)])
def get_video_publish_draft_media_endpoint(draft_id: str, request: Request) -> FileResponse:
    try:
        path = get_video_publish_draft_media_path(_runner(request).database, draft_id)
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if path is None:
        raise HTTPException(status_code=404, detail="本地草稿不存在或已删除。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@_router.get("/video-publish/drafts/{draft_id}/covers/{slot}", dependencies=[Depends(_same_origin)])
def get_video_publish_draft_cover_endpoint(draft_id: str, slot: str, request: Request) -> FileResponse:
    try:
        path = get_video_publish_draft_cover_path(_runner(request).database, draft_id, slot)
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if path is None:
        raise HTTPException(status_code=404, detail="本地草稿封面不存在或已删除。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@_router.delete("/video-publish/drafts/{draft_id}", dependencies=[Depends(_same_origin)])
def delete_video_publish_draft_endpoint(draft_id: str, request: Request) -> dict[str, int | bool]:
    try:
        result = delete_video_publish_draft(_runner(request).database, draft_id)
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if result is None:
        raise HTTPException(status_code=404, detail="本地草稿不存在或已删除。")
    return result


@_router.get("/video-publish/batches", dependencies=[Depends(_same_origin)])
def get_video_publish_batches(
    request: Request,
    q: str = Query(default="", max_length=200),
    status: str = Query(default="all", max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    try:
        return list_video_publish_batches(
            _runner(request).database, query=q, status=status, limit=limit, offset=offset,
        )
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/batches", status_code=201, dependencies=[Depends(_same_origin)])
def create_video_publish_batch_endpoint(body: VideoPublishBatchCreateRequest, request: Request) -> dict[str, object]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请确认将视频发送到所选平台账号。")
    manager = _video_publish_manager(request)
    status = manager.adapter.status()
    if not status["configured"]:
        raise HTTPException(status_code=409, detail=str(status["message"]))
    runner = _runner(request)
    try:
        batch = create_video_publish_batch(
            runner.database,
            archive_root=runner.config.archive_root,
            idempotency_key=body.idempotency_key,
            asset_id=body.asset_id,
            upload_id=body.upload_id,
            local_source_id=body.local_source_id,
            cover_portrait_upload_id=body.cover_portrait_upload_id,
            cover_landscape_upload_id=body.cover_landscape_upload_id,
            title=body.title,
            description=body.description,
            tags=body.tags,
            schedule_at=body.schedule_at,
            targets=[
                {"account_id": item.account_id, "options": item.options.model_dump(exclude_none=True)}
                for item in body.targets
            ],
        )
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    for target in batch.get("targets", []):
        if isinstance(target, dict) and target.get("status") == "queued":
            manager.enqueue_publish(str(target["id"]))
    return get_video_publish_batch(runner.database, str(batch["id"])) or batch


@_router.get("/video-publish/batches/{batch_id}", dependencies=[Depends(_same_origin)])
def get_video_publish_batch_endpoint(batch_id: str, request: Request) -> dict[str, object]:
    result = get_video_publish_batch(_runner(request).database, batch_id, include_attempts=True)
    if result is None:
        raise HTTPException(status_code=404, detail="视频发布批次不存在。")
    return result


@_router.get("/video-publish/batches/{batch_id}/covers/{filename}", dependencies=[Depends(_same_origin)])
def get_video_publish_batch_cover_endpoint(batch_id: str, filename: str, request: Request) -> FileResponse:
    try:
        path = get_video_publish_batch_cover_path(_runner(request).database, batch_id, filename)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布封面不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@_router.get("/video-publish/batches/{batch_id}/media", dependencies=[Depends(_same_origin)])
def get_video_publish_batch_media_endpoint(batch_id: str, request: Request) -> FileResponse:
    try:
        path = get_video_publish_batch_media_path(_runner(request).database, batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布素材不存在或已清理。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if path is None:
        raise HTTPException(status_code=404, detail="视频发布素材不存在或已清理。")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@_router.delete("/video-publish/batches/{batch_id}", dependencies=[Depends(_same_origin)])
def delete_video_publish_batch_endpoint(batch_id: str, request: Request) -> dict[str, int | bool]:
    try:
        return delete_video_publish_batch(_runner(request).database, batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布批次不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.delete("/video-publish/batches/{batch_id}/media", dependencies=[Depends(_same_origin)])
def delete_video_publish_batch_media_endpoint(batch_id: str, request: Request) -> dict[str, int | bool]:
    try:
        return clean_video_publish_batch_media(_runner(request).database, batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="视频发布批次不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/targets/{target_id}/sync", status_code=202, dependencies=[Depends(_same_origin)])
def sync_video_publish_target(target_id: str, request: Request) -> dict[str, bool]:
    try:
        return {"queued": _video_publish_manager(request).enqueue_status_sync(target_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="发布目标不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/targets/{target_id}/retry", dependencies=[Depends(_same_origin)])
def retry_video_publish_target(target_id: str, body: VideoPublishTargetRetryRequest, request: Request) -> dict[str, object]:
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="请确认重新发布该平台账号。")
    manager = _video_publish_manager(request)
    try:
        request_video_publish_retry(
            manager.database, target_id, confirmed=True, change_schedule=body.change_schedule,
            schedule_at=body.schedule_at, xhs_manual_submit=body.xhs_manual_submit,
        )
        manager.enqueue_publish(target_id)
        target = get_video_publish_target(manager.database, target_id)
        return get_video_publish_batch(manager.database, str(target["batch_id"])) if target else {}
    except KeyError:
        raise HTTPException(status_code=404, detail="发布目标不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/targets/{target_id}/edit", dependencies=[Depends(_same_origin)])
def edit_video_publish_target_endpoint(target_id: str, body: VideoPublishTargetEditRequest, request: Request) -> dict[str, object]:
    manager = _video_publish_manager(request)
    try:
        edited_target_id = edit_video_publish_target(
            manager.database,
            target_id,
            body.options.model_dump(exclude_unset=True),
            confirmed=body.confirmed,
        )
        manager.enqueue_publish(edited_target_id)
        target = get_video_publish_target(manager.database, edited_target_id)
        return get_video_publish_batch(manager.database, str(target["batch_id"]), include_attempts=True) if target else {}
    except KeyError:
        raise HTTPException(status_code=404, detail="发布目标不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/targets/{target_id}/confirm", dependencies=[Depends(_same_origin)])
def confirm_video_publish_target_endpoint(target_id: str, body: VideoPublishTargetConfirmRequest, request: Request) -> dict[str, object]:
    try:
        target = confirm_video_publish_target(
            _runner(request).database, target_id, body.state, confirmed=body.confirmed,
        )
        return get_video_publish_batch(_runner(request).database, str(target["batch_id"])) or {}
    except KeyError:
        raise HTTPException(status_code=404, detail="发布目标不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@_router.post("/video-publish/targets/{target_id}/cancel", dependencies=[Depends(_same_origin)])
def cancel_video_publish_target(target_id: str, request: Request) -> dict[str, object]:
    manager = _video_publish_manager(request)
    try:
        manager.cancel_target(target_id)
        target = get_video_publish_target(manager.database, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="发布目标不存在。")
        return get_video_publish_batch(manager.database, str(target["batch_id"])) or {}
    except KeyError:
        raise HTTPException(status_code=404, detail="发布目标不存在。") from None
    except VideoPublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


def mount_workbench_api(app: FastAPI, runner: TaskRunner | None = None) -> TaskRunner:
    """Mount versioned routes and attach the single process worker lifecycle."""
    selected_runner = runner or get_default_runner()
    app.state.workbench_runner = selected_runner
    video_publish_manager = VideoPublishManager(selected_runner.database)
    app.state.video_publish_manager = video_publish_manager
    app.include_router(_router)
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def workbench_lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with previous_lifespan(application):
            selected_runner.start()
            try:
                video_publish_manager.start()
                recover_article_publications(selected_runner.database)
                yield
            finally:
                video_publish_manager.stop()
                selected_runner.stop()
                get_platform_account_manager().shutdown()

    app.router.lifespan_context = workbench_lifespan
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    return selected_runner


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _ERROR_TEXT})


def _vault_artifact_content(runner: TaskRunner, artifact_id: str) -> tuple[str, str]:
    with runner.database.transaction() as connection:
        row = connection.execute(
            """SELECT ar.kind, ar.path, ar.sha256, ar.metadata_json, a.title, a.id AS asset_id,
                      (SELECT source.path FROM artifacts AS source
                       JOIN tasks AS source_task ON source_task.id = source.task_id
                       WHERE source_task.asset_id = a.id AND source.kind = 'video'
                       ORDER BY source.version DESC LIMIT 1) AS media_path
               FROM artifacts AS ar
               JOIN tasks AS ar_task ON ar_task.id = ar.task_id
               JOIN assets AS a ON a.id = ar_task.asset_id
               WHERE ar.id = ?""",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise KeyError("artifact not found")
    artifact_kind = str(row["kind"]).casefold()
    metadata = _json_object(row["metadata_json"])
    approved_card = artifact_kind == "knowledge_card_approved" and metadata.get("human_reviewed") is True
    finalized_script = artifact_kind == "final_script" and metadata.get("finalized") is True and metadata.get("human_reviewed") is True
    if not approved_card and not finalized_script:
        raise ValueError("只能选择已审核的知识卡或定稿写入知识库。")
    root = runner.config.archive_root
    if root is None:
        raise ValueError("归档目录未配置。")
    registered = Path(str(row["path"]))
    if registered.is_symlink():
        raise ValueError("指定产物不可用。")
    path = _assert_contained(registered, root, strict=True)
    if not path.is_file() or path.suffix.casefold() not in {".md", ".txt"}:
        raise ValueError("指定产物不是可写入的文本资料。")
    digest, size = _hash_file(path)
    if digest.casefold() != str(row["sha256"]).casefold() or size > 5_000_000:
        raise ValueError("指定产物内容已变化或超过 5 MB。")
    try:
        original = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("指定产物不是 UTF-8 文本。") from exc
    # Platform title sanitizers can leave trailing underscores; trim them so
    # note titles and filenames read naturally in the vault.
    raw_title = str(row["title"] or path.stem).strip()
    title = raw_title[:300].rstrip("_").strip() or raw_title[:300] or path.stem
    # The generated card links the source transcript by vault-relative name,
    # but transcripts are not written into the vault, so the link is dead.
    # Drop it while keeping the working video and workbench links.
    original = re.sub(r"\s*·\s*\[完整口播稿\]\([^)]*\)", "", original)
    media_ref: str | None = None
    output_root = runner.config.get_settings().get("obsidian_output_root")
    if isinstance(output_root, str) and isinstance(row["media_path"], str):
        media_path = Path(str(row["media_path"]))
        try:
            if media_path.is_symlink():
                raise ValueError("registered media is not a regular file")
            safe_media = _assert_contained(media_path, Path(output_root), strict=True)
            if safe_media.is_file():
                media_ref = "[[" + safe_media.relative_to(Path(output_root).resolve(strict=True)).as_posix().replace("]", "\\]") + "]]"
        except (OSError, RuntimeError, ValueError):
            media_ref = None
    # Keep the note frontmatter human-readable: just title and media. Workbench
    # provenance stays queryable through the obsidian_writes ledger instead.
    metadata_lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
    ]
    if media_ref:
        metadata_lines.append(f"media: {json.dumps(media_ref, ensure_ascii=False)}")
    metadata_lines.extend(["---", ""])
    frontmatter = "\n".join([
        *metadata_lines,
    ])
    media_embed = f"\n{media_ref}\n\n" if media_ref else ""
    return frontmatter + media_embed + original, title


def _auto_write_card_to_vault(runner: TaskRunner, artifact_id: str) -> dict[str, object]:
    """Write an approved knowledge card into the configured vault automatically.

    Runs after the card's approval flip commits; a vault failure never blocks
    approval, it is reported back to the caller instead. Uses the same note
    path convention as the manual write panel (asset title + .md) and falls
    back to the suggested conflict path when a different note already exists.
    """
    from .obsidian import write_vault_note

    root_value = runner.config.get_settings().get("obsidian_output_root")
    if not isinstance(root_value, str) or not root_value:
        return {"written": False, "path": None, "reason": "未配置知识库输出目录，已仅完成审核。"}
    try:
        content, title = _vault_artifact_content(runner, artifact_id)
        base = (title or "知识卡").replace('\\/:*?"<>|', "_").strip()[:100].rstrip("_").strip() or "知识卡"
        result = write_vault_note(Path(root_value), f"{base}.md", content, None)
        if not result.written and result.conflict_path:
            result = write_vault_note(Path(root_value), result.conflict_path, content, None)
        if result.written and result.path:
            with runner.database.transaction(write=True) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO obsidian_writes (
                           id, artifact_id, output_root, relative_path, sha256, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        artifact_id,
                        str(Path(root_value).resolve(strict=True)),
                        result.path,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                card_row = connection.execute("SELECT metadata_json FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
                if card_row is not None:
                    metadata = _json_object(card_row["metadata_json"])
                    metadata["vault_note_path"] = result.path
                    connection.execute(
                        "UPDATE artifacts SET metadata_json = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), artifact_id),
                    )
        return {"written": result.written, "path": result.path, "reason": result.reason}
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        return {"written": False, "path": None, "reason": _safe_error(exc)}
def _task_payload(task: TaskRecord) -> dict[str, object]:
    source = task.source.to_dict()
    source["value"] = "公开链接（已隐藏地址）" if task.source.kind.value == "url" else "本地文件（原路径已隐藏）"
    return {
        "id": task.id,
        "asset_id": task.asset_id,
        "source": source,
        "status": task.status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "steps": [_step_payload(step) for step in task.steps],
    }


def _step_payload(step: StepRecord) -> dict[str, object]:
    return {
        "id": step.id,
        "kind": step.kind.value,
        "ordinal": step.ordinal,
        "status": step.status,
        "depends_on": list(step.depends_on),
        "input_artifact_ids": list(step.input_artifact_ids),
        "output_artifact_ids": list(step.output_artifact_ids),
        "progress": step.progress,
        "message": _safe_error(step.message or ""),
        "error_code": step.error_code,
        "retry_count": step.retry_count,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def _asset_payload(asset: AssetRecord, archive_root: Path | None = None) -> dict[str, object]:
    return {
        "id": asset.id,
        "source_kind": asset.source_kind.value,
        "platform": asset.platform,
        "source_id": asset.source_id,
        "title": _safe_error(asset.title) if asset.title else None,
        "description": _public_value(asset.description) if asset.description else None,
        "author": _safe_error(asset.author) if asset.author else None,
        "published_at": asset.published_at,
        "archived_at": asset.archived_at,
        "created_at": asset.created_at,
        "sha256": asset.sha256,
        "archive_relative_path": _safe_error(relative_path) if (relative_path := _relative_archive_path(
            Path(asset.archive_path) if asset.archive_path else None,
            archive_root,
        )) else None,
    }


def _artifact_payload(row: Any, archive_root: Path | None = None) -> dict[str, object]:
    path = Path(str(row["path"]))
    name = _safe_error(path.name[:240]) or "已归档文件"
    relative_path = _relative_archive_path(path, archive_root)
    file_available = False
    if archive_root is not None and not path.is_symlink():
        try:
            file_available = _assert_contained(path, archive_root, strict=True).is_file()
        except (OSError, RuntimeError, ValueError):
            file_available = False
    artifact_id = str(row["id"])
    return {
        "id": artifact_id,
        "task_id": str(row["task_id"]),
        "step_id": str(row["step_id"]),
        "kind": str(row["kind"]),
        "name": name,
        "relative_path": _safe_error(relative_path) if relative_path else None,
        "file_available": file_available,
        "sha256": str(row["sha256"]),
        "version": int(row["version"]),
        "metadata": _public_value(_json_object(row["metadata_json"])),
        "created_at": row["created_at"],
        "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "file_url": f"/api/v1/artifacts/{artifact_id}/file",
    }


def _task_cover_summary(
    database: Database,
    task_id: str,
    archive_root: Path | None,
) -> dict[str, object] | None:
    """Return the latest archived cover artifact payload for one task, when usable."""
    if archive_root is None:
        return None
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT id, path FROM artifacts
               WHERE task_id = ? AND kind = 'cover'
               ORDER BY created_at DESC, version DESC
               LIMIT 1""",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        path = Path(str(row["path"]))
        if path.is_symlink():
            return None
        _assert_contained(path, archive_root, strict=True)
        if not path.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    artifact_id = str(row["id"])
    return {"artifact_id": artifact_id, "file_url": f"/api/v1/artifacts/{artifact_id}/file"}


def _task_video_author(database: Database, task_id: str) -> str | None:
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT metadata_json FROM artifacts
               WHERE task_id = ? AND kind = 'video'
               ORDER BY created_at DESC, version DESC
               LIMIT 1""",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    author = _json_object(row["metadata_json"]).get("author")
    return author.strip() if isinstance(author, str) and author.strip() else None


def _summary_payload(
    task: TaskRecord,
    asset: AssetRecord,
    archive_root: Path | None = None,
    *,
    cover: dict[str, object] | None = None,
    video_author: str | None = None,
) -> dict[str, object]:
    asset_payload = _asset_payload(asset, archive_root)
    if not asset_payload["author"] and video_author:
        asset_payload["author"] = _safe_error(video_author)
    return {
        "task": _task_payload(task),
        "asset": asset_payload,
        "cover": cover,
        "completed_step_kinds": [step.kind.value for step in task.steps if step.status == "completed"],
        "error_count": sum(1 for step in task.steps if step.error_code),
    }


def _relative_archive_path(path: Path | None, archive_root: Path | None) -> str | None:
    if path is None or archive_root is None:
        return None
    try:
        root = archive_root.resolve(strict=True)
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        return PurePosixPath(*relative.parts).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _open_local_path(path: Path, *, reveal: bool = False) -> None:
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("native file opening is unavailable")
        startfile(str(path.parent if reveal else path))
        return
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if reveal else ["open", str(path)]
    else:
        command = ["xdg-open", str(path.parent if reveal else path)]
    subprocess.Popen(command, shell=False, close_fds=True)


def _asset_for_task(runner: TaskRunner, task: TaskRecord) -> AssetRecord:
    with runner.database.transaction() as connection:
        row = connection.execute("SELECT * FROM assets WHERE id = ?", (task.asset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="素材记录不存在。")
    return _asset_from_row(row)


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized == "author":
                output[str(key)] = _public_value(item)
                continue
            if _SENSITIVE_KEY.search(normalized) or normalized in {
                "source_url", "original_share_url", "raw_url", "url", "webpage_url",
                "api_key", "authorization", "cookie", "traceback", "exception",
            }:
                continue
            output[str(key)] = _public_value(item)
        return output
    if isinstance(value, list):
        return [_public_value(item) for item in value[:500]]
    if isinstance(value, str):
        return _URL_PATTERN.sub("<链接已隐藏>", value)[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _safe_error(exc: object) -> str:
    text = str(exc)
    text = _URL_PATTERN.sub("<链接已隐藏>", text)
    text = re.sub(
        r"(?i)(token|signature|sign|auth|authorization|api[_-]?key|cookie|secret)=([^&\s]+)",
        r"\1=<已隐藏>",
        text,
    )
    return text[:500]


def _source_validation_message(exc: object) -> str:
    text = _safe_error(exc)
    if any("\u4e00" <= character <= "\u9fff" for character in text):
        return text
    return "链接无效。请粘贴抖音或 B 站官方公开作品链接或分享文案。"


def _credential_status(provider_id: str = "zhipu") -> dict[str, object]:
    try:
        api_key = get_model_api_key(provider_id)
        configured = bool(api_key)
        return {
            "configured": configured,
            "available": True,
            "masked_key": _mask_api_key(api_key) if configured else None,
        }
    except CredentialStoreUnavailable:
        return {"configured": False, "available": False, "masked_key": None}


def _mask_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "••••••••"
    return f"{api_key[:4]}…{api_key[-4:]}"
