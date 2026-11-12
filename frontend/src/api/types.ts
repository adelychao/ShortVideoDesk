export type StepKind =
  | 'download'
  | 'import'
  | 'transcribe'
  | 'ocr'
  | 'comments'
  | 'analyze_comments'
  | 'analyze_structure'
  | 'knowledge_cards'
  | 'generate_scripts'
  | 'index'
  | 'write_obsidian'

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'interrupted'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'blocked'
  | 'awaiting_authorization'
  | 'cancelled'
  | 'unknown'

export type StepStatus = TaskStatus | 'skipped'

export interface StepConsent {
  stepKind: StepKind
  providerId: string
  modelId: string
  artifactIds: string[]
  authorized: boolean
  authorizedAt?: string
}

export interface CreateTaskRequest {
  sourceUrl?: string
  uploadId?: string
  localSourceId?: string
  steps: StepKind[]
  scriptStyleIds?: string[]
  allowFirstFrame: boolean
  commentSortOrder?: 'likes_desc' | 'time_desc' | 'time_asc'
  consents: StepConsent[]
  transcriptionMode?: 'cloud' | 'local'
  ocrMode?: 'cloud' | 'local'
  ocrIntervalSeconds?: number
  ocrScanProfile?: 'full' | 'subtitles'
  localModel?: string
  localLanguage?: 'auto' | 'zh' | 'en'
  localDevice?: 'auto' | 'cpu' | 'cuda'
  platformAccessMode?: 'anonymous' | 'account'
  platformAccountId?: string
  modelProfileId?: string
}

export interface PlatformAccountStatus {
  id: 'douyin' | 'bilibili' | 'csdn' | 'toutiao'
  name: string
  status: 'connected' | 'login_window_open' | 'not_connected' | 'error'
  authenticated: boolean
  login_window_open: boolean
  message: string
  profile_name?: string | null
  profile_avatar_url?: string | null
}

export interface PlatformStatus {
  id: string
  name: string
  validation_status: string
  checked_at: string | null
  summary: string | null
}

export interface SourcePreview {
  platform: 'douyin' | 'bilibili'
  platform_name: string
  source_id: string | null
  is_short: boolean
  requires_resolution: boolean
  message: string
}

export interface UploadReceipt {
  uploadId: string
  filename: string
  sizeBytes: number
  sha256: string
  detectedMediaType: 'video' | 'audio'
  sourceType?: 'local'
  sourcePath?: string
  previewUrl?: string
}

export interface CollectionMediaSelectResult {
  source: UploadReceipt | null
  overLimit?: boolean
  sizeBytes?: number
}

export interface WorkbenchHealth {
  status: string
  mode: string
  archive_root_configured: boolean
  cloud_model_configured: boolean
  asr_model_configured: boolean
  model_provider: string | null
  model_provider_name: string | null
  model_id: string | null
  asr_model_id: string | null
  local_asr_device: 'auto' | 'cpu' | 'cuda'
  worker_count: number
}

export interface AppSettings {
  archive_root: string | null
  obsidian_reference_root: string | null
  obsidian_output_root: string | null
  model_provider: string | null
  model_id: string | null
  asr_model_id: string | null
  model_base_url: string | null
  vector_search_enabled: boolean
  local_asr_device: 'auto' | 'cpu' | 'cuda'
  knowledge_card_prompt: string
  script_style_templates: ScriptStyleTemplate[]
  video_publish_auto_cleanup_enabled: boolean
  video_publish_retention_days: number
  video_publish_cover_root: string | null
  article_image_root: string | null
}

export interface ScriptStyleTemplate {
  id: string
  name: string
  instructions: string
  enabled: boolean
  reference_paths: string[]
}

export interface LocalAsrStatus {
  installed: boolean
  installation?: { state: 'idle' | 'installing' | 'ready' | 'failed'; message: string }
  model_root: string
  models: Array<{ id: string; ready: boolean; job: { state: string; message: string } | null }>
}
export interface LocalModelPreview { model: string; revision: string; size_bytes: number; directory: string }

export interface TaskTranscriptionConfig {
  mode: 'cloud' | 'local'
  profile_id: string | null
  profile_name: string | null
  provider_id: string | null
  provider_name: string | null
  asr_model_id: string | null
  local_model: string | null
  local_language: 'auto' | 'zh' | 'en' | null
  local_device: 'auto' | 'cpu' | 'cuda' | null
}

export interface UpdateTranscriptionConfigRequest {
  mode: 'cloud' | 'local'
  profile_id?: string | null
  asr_model_id?: string | null
  local_model?: string | null
  local_language?: 'auto' | 'zh' | 'en' | null
  local_device?: 'auto' | 'cpu' | 'cuda' | null
}

export interface CredentialStatus {
  configured: boolean
  available: boolean
  masked_key?: string | null
}

export interface ModelProfile {
  id: string
  name: string
  provider_id: string
  provider_name: string
  supports_audio_transcription: boolean
  model_id: string
  asr_model_id: string | null
  model_base_url: string | null
  capabilities: Array<'text_generation' | 'image_generation'>
  image_model_id: string | null
  image_base_url: string | null
  is_default: boolean
  is_image_default: boolean
  credential: CredentialStatus
}

export interface SaveModelProfileRequest {
  name: string
  provider_id: string
  model_id: string
  asr_model_id: string | null
  model_base_url: string | null
  capabilities: Array<'text_generation' | 'image_generation'>
  image_model_id?: string | null
  image_base_url?: string | null
  api_key?: string | null
  make_default?: boolean | null
  make_image_default?: boolean | null
}

export interface ModelProviderOption {
  id: string
  label: string
  default_base_url: string | null
  supports_audio_transcription: boolean
  description: string
}

export interface ModelCapabilityStatus {
  provider_id: string
  model_id: string
  status: 'verified' | 'unknown'
  modalities: string[]
  display_name: string | null
  source: string | null
  message: string
}

export interface ConfigResponse {
  settings: AppSettings
  default_knowledge_card_prompt: string
  model_providers: ModelProviderOption[]
  model_base_url_default: string | null
  directory_conflicts: Array<{ code: string; message: string }>
  credential: CredentialStatus
}

export interface ConfigSaveResponse {
  settings: AppSettings
  default_knowledge_card_prompt: string
  model_providers: ModelProviderOption[]
  model_base_url_default: string | null
  directory_conflicts: Array<{ code: string; message: string }>
  credential: CredentialStatus
  video_publish_media_cleanup: { cleaned_batches: number; bytes_freed: number }
}

export interface DiagnosticResult {
  id: string
  status: 'available' | 'unconfigured' | 'limited' | 'error'
  message: string
  remediation: string | null
  affected_steps: StepKind[]
}

export interface SourceRecord {
  kind: 'url' | 'file'
  value: string
  platform: string | null
  source_id: string | null
  title: string | null
  description?: string | null
  author: string | null
  published_at: string | null
}

export interface TaskStep {
  id: string
  kind: StepKind
  ordinal: number
  status: StepStatus
  depends_on: string[]
  input_artifact_ids: string[]
  output_artifact_ids: string[]
  progress: number | null
  message: string | null
  error_code: string | null
  retry_count: number
  started_at: string | null
  finished_at: string | null
}

export interface TaskRecord {
  id: string
  asset_id: string
  source: SourceRecord
  status: TaskStatus
  created_at: string
  updated_at: string
  steps: TaskStep[]
  platform_access?: {
    mode: 'anonymous' | 'account'
    account_id: string | null
    account_name: string | null
  }
}

export interface AssetRecord {
  id: string
  source_kind: 'url' | 'file'
  platform: string | null
  source_id: string | null
  title: string | null
  description?: string | null
  author: string | null
  published_at: string | null
  archived_at: string | null
  created_at: string
  archive_relative_path: string | null
  sha256: string | null
}

export interface TaskSummary {
  task: TaskRecord
  asset: AssetRecord
  cover?: { artifact_id: string; file_url: string } | null
  completed_step_kinds: StepKind[]
  error_count: number
}

export interface TaskArtifact {
  id: string
  task_id: string
  step_id: string
  kind: string
  name: string
  relative_path: string | null
  sha256: string
  version: number
  metadata: Record<string, unknown>
  created_at: string
  mime_type: string
  file_url: string
  file_available?: boolean
}

export interface ArticleDocument {
  schema_version: number
  title: string
  title_candidates: string[]
  summary: string
  mode: 'refine' | 'polish' | 'layout'
  analysis: { core_point: string; reason: string }
  cover_image_prompt: string
  cover_image_path: string
  cover_image_name: string
  cover_image_model_id: string
  cover_image_generated_at: string
  author: string
  published_at: string
  outro: string
  blocks: Array<Record<string, unknown>>
  body_markdown: string
  platform_overrides: Record<string, { title: string; summary: string; body_markdown: string }>
}

export interface ArticlePublication {
  id: string
  article_id: string
  platform_id: string
  status: 'draft_saved' | 'published' | 'publishing' | 'failed' | 'unknown' | 'manual_required' | 'manual_published'
  remote_id: string | null
  remote_url: string | null
  message: string
  article_version: number
  account_id: string | null
  account_name: string | null
    options: Record<string, string | boolean | string[] | CsdnPublishOptions | ToutiaoPublishOptions>
  fan_notification_status: 'not_requested' | 'waiting_publication' | 'pending' | 'sending' | 'sent' | 'failed' | 'unknown'
  fan_notification_remote_id: string | null
  fan_notification_message: string
  created_at: string
  updated_at: string
}

export interface ArticleListItem {
  id: string
  title: string
  summary: string
  status: 'draft' | 'finalized'
  version: number
  created_at: string
  updated_at: string
  source_title: string | null
  source_task_id: string | null
  source_artifact_id: string | null
  publication_statuses: Record<string, ArticlePublication['status'] | 'partial'>
  published_platforms: string[]
}

export interface ArticleDeleteResult {
  deleted: boolean
  cleanup_warning: string | null
}

export interface ArticleRecord extends ArticleListItem {
  document: ArticleDocument
  versions: Array<{ version: number; note: string; created_at: string }>
  publications: ArticlePublication[]
}

export interface ArticleSourceScript {
  artifact_id: string
  task_id: string
  title: string
  source_title: string | null
  version: number
  created_at: string
}

export interface CsdnTagCatalog {
  categories: Array<{ name: string; tags: string[] }>
  max_tags: number
  results: string[]
  custom_allowed: boolean | null
  declarations: string[]
}

export interface ArticlePlatformStatus {
  id: string
  name: string
  mode: 'official' | 'manual'
  configured: boolean
  connected: boolean
  message: string
  authorization_url: string | null
  manual_url: string | null
  secret_available: boolean
  public_config: Record<string, string>
  accounts: ArticlePlatformAccount[]
  capabilities?: Record<string, { publish: string; draft: boolean; status_query: boolean }>
  cli_installed?: boolean
  login_in_progress?: boolean
  connection_checking?: boolean
  publish_ready?: boolean
  connection_checked?: boolean
  readiness_checked?: boolean
  developer_verified?: boolean
  service_ready?: boolean
  developer_url?: string | null
  service_url?: string | null
}

export interface ArticlePlatformAccount {
  id: string
  name: string
  public_value: string
  avatar_url?: string | null
  credential_verified_at: string | null
  credential_verified: boolean
  configured: boolean
  secret_available: boolean
  status?: VideoPublishAccountStatus
  message?: string
  auth_type?: 'browser' | 'openapi'
}

export interface ArticlePublishTarget {
  platform_id: string
  account_id: string | null
  options?: { title?: string | null; summary?: string | null; author?: string | null; note?: string | null; tags?: string[] | null; cover_image_path?: string | null; notify_fans?: boolean; csdn?: CsdnPublishOptions | null; toutiao?: ToutiaoPublishOptions | null }
}

export interface CsdnPublishOptions {
  article_type: 'original' | 'reprint' | 'translation'
  source_url: string
  summary: string
  visibility: 'public' | 'private' | 'followers' | 'vip'
  template: 'default' | 'simple' | 'ink'
  declaration: string
  category: string
  activity: string
  topic: string
  backup_gitcode: boolean
  cross_publish: boolean
}

export interface ToutiaoPublishOptions {
  cover_mode: 'single' | 'triple' | 'none'
  cover_image_paths: string[]
  advertising: boolean
  original_first: boolean
  allow_tips: boolean
  sync_micro: boolean
  declarations: string[]
  location: string
  collection: string
  account_options_verified?: boolean
}

export interface ToutiaoAccountPublishOptions {
  declarations: string[]
  collections: string[]
}

export type VideoPublishTargetStatus = 'queued' | 'running' | 'pending_review' | 'published' | 'draft_saved' | 'failed' | 'unknown' | 'cancelled'
export type VideoPublishBatchStatus = VideoPublishTargetStatus | 'partial'
export type VideoPublishAccountStatus = 'not_connected' | 'login_in_progress' | 'checking' | 'connected' | 'expired' | 'error'

export interface VideoPublishSauStatus {
  configured: boolean
  project_path: string | null
  message: string
  command_source: string | null
}

export interface VideoPublishPlatform {
  id: string
  cli_id: string
  name: string
  schedule: boolean
  tid_required: boolean
  max_tags: number
  sau_ready: boolean
  login_available: boolean
}

export interface VideoPublishAccount {
  id: string
  platform_id: string
  account_alias: string
  display_name: string
  profile_name?: string
  avatar_url?: string
  duplicate_notice?: string
  status: VideoPublishAccountStatus
  message: string
  checked_at: string | null
  enabled: boolean
}

export interface VideoPublishMedia {
  asset_id: string
  artifact_id: string
  title: string
  name: string
  size_bytes: number
  sha256: string
  created_at: string
}

export interface VideoPublishCoverUpload {
  upload_id: string
  filename: string
  size_bytes: number
  preview_url?: string
}

export interface VideoPublishDraftCover {
  stored_name: string
  filename: string
  sha256: string
  size_bytes: number
}

export interface VideoPublishDraftForm {
  title: string
  description: string
  tags: string[]
  tag_draft: string
  schedule_mode: 'immediate' | 'scheduled'
  schedule_at: string
  platform_schedules: Record<string, string>
  selected_account_ids: string[]
  active_platform_id: string
  overrides: Record<string, Record<string, unknown>>
  poi_selections: Record<string, VideoPublishPoi | null>
  poi_queries: Record<string, string>
  poi_city_codes: Record<string, string>
  xhs_poi_selections: Record<string, VideoPublishXhsPoi | null>
  xhs_poi_queries: Record<string, string>
  repost_selections: Record<string, { id: string; title: string } | null>
  cover_preferences: { kuaishou: string; xiaohongshu: string }
}

export interface VideoPublishDraft {
  id: string
  title: string
  media_name: string
  media_size: number
  platforms: string[]
  account_names: string[]
  created_at: string
  updated_at: string
  linked_media: boolean
}

export interface VideoPublishDraftDetail extends VideoPublishDraft {
  media_extension: string
  media_sha256: string
  confirmed_over_limit: boolean
  form: VideoPublishDraftForm
  covers: Record<string, VideoPublishDraftCover>
}

export interface VideoPublishDraftSaveRequest {
  upload_id?: string
  local_source_id?: string
  cover_upload_ids: Record<string, string>
  form: VideoPublishDraftForm
}

export interface VideoPublishDraftRestoreResult {
  draft: VideoPublishDraftDetail
  media_upload: UploadReceipt | null
  cover_uploads: Record<string, VideoPublishCoverUpload>
}

export interface VideoPublishPoi {
  id: string
  name: string
  address: string
  city_code: string
  city_name: string
}

export interface VideoPublishPoiSearchResult {
  items: VideoPublishPoi[]
  cities: { code: string; name: string }[]
  selected_city_code: string
}

export interface VideoPublishXhsPoi {
  id: string
  name: string
  address: string
}

export interface VideoPublishDouyinTopic {
  id: string
  name: string
  view_count: string
  view_count_text: string
}

export interface VideoPublishTarget {
  id: string
  batch_id: string
  account_id: string
  platform_id: string
  account_alias: string
  account_name: string
  options: {
    title?: string
    description?: string
    tags?: string[]
    tid?: number
    bilibili_category?: number
    bilibili_category_name?: string
    bilibili_ai?: boolean
    bilibili_statement?: number
    bilibili_statement_name?: string
    bilibili_source?: string
    bilibili_no_reprint?: boolean
    bilibili_private?: boolean
    bilibili_dynamic?: string
    declaration?: string
    baijiahao_declaration?: string
    repost_source?: 'inside' | 'outside'
    repost_title?: string
    repost_id?: string
    schedule_at_override?: string | null
    location?: string
    location_id?: string
    location_address?: string
    location_city_code?: string
    location_city_name?: string
    visibility?: 'public' | 'friends' | 'private'
    xhs_original?: boolean
    xhs_declaration?: 'fiction' | 'ai' | 'advertising' | 'self_shot' | 'repost'
    xhs_repost_source?: string
    xhs_visibility?: 'public' | 'friends' | 'private'
    xhs_location?: string
    xhs_location_id?: string
    xhs_location_address?: string
    allow_download?: boolean
    collection_name?: string
    short_title?: string
    save_as_draft?: boolean
    cover_filename?: string
    cover_4_3_filename?: string
    cover_16_9_filename?: string
    cover_portrait_filename?: string
    cover_landscape_filename?: string
    cover_ratio?: 'original' | 'landscape' | 'portrait' | 'square' | 'tall'
  }
  status: VideoPublishTargetStatus
  progress: number | null
  message: string
  remote_id: string | null
  remote_url: string | null
  platform_status: string | null
  platform_message: string
  platform_checked_at: string | null
  platform_observed_at: string | null
  heartbeat_at?: string | null
  last_output_at?: string | null
  upload_retry_count?: number
  diagnosis?: {
    code: string
    severity: 'warning' | 'danger'
    message: string
  } | null
  attempt_count: number
  attempts?: VideoPublishAttempt[]
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface VideoPublishAttempt {
  attempt_number: number
  status: VideoPublishTargetStatus
  exit_code: number | null
  message: string
  started_at: string
  finished_at: string | null
}

export interface VideoPublishBatch {
  id: string
  asset_id: string | null
  media_name: string
  media_size: number
  media_available: boolean
  media_source_type?: 'local' | 'managed'
  media_sha256: string
  title: string
  description: string
  tags: string[]
  schedule_at: string | null
  status: VideoPublishBatchStatus
  created_at: string
  updated_at: string
  targets: VideoPublishTarget[]
  summary: Record<'total' | 'queued' | 'running' | 'pending_review' | 'published' | 'draft_saved' | 'failed' | 'unknown' | 'cancelled', number>
}

export interface CreateVideoPublishBatchRequest {
  idempotency_key: string
  asset_id?: string | null
  upload_id?: string | null
  local_source_id?: string | null
  cover_portrait_upload_id?: string | null
  cover_landscape_upload_id?: string | null
  title: string
  description: string
  tags: string[]
  schedule_at?: string | null
  targets: Array<{
    account_id: string
    options?: {
      title?: string
      description?: string
      tags?: string[]
      tid?: number
      bilibili_category?: number
      bilibili_category_name?: string
      bilibili_ai?: boolean
      bilibili_statement?: number
      bilibili_statement_name?: string
      bilibili_source?: string
      bilibili_no_reprint?: boolean
      bilibili_private?: boolean
      bilibili_dynamic?: string
      declaration?: string
      baijiahao_declaration?: string
      repost_source?: 'inside' | 'outside'
      repost_title?: string
      repost_id?: string
      location?: string
      location_id?: string
      location_address?: string
      location_city_code?: string
      location_city_name?: string
      visibility?: 'public' | 'friends' | 'private'
      xhs_original?: boolean
      xhs_declaration?: 'fiction' | 'ai' | 'advertising' | 'self_shot' | 'repost'
      xhs_repost_source?: string
      xhs_visibility?: 'public' | 'friends' | 'private'
      xhs_location?: string
      xhs_location_id?: string
      xhs_location_address?: string
      allow_download?: boolean
      cover_upload_id?: string
      cover_portrait_upload_id?: string
      cover_landscape_upload_id?: string
      cover_4_3_upload_id?: string
      cover_16_9_upload_id?: string
      cover_ratio?: 'original' | 'landscape' | 'portrait' | 'square' | 'tall'
      collection_name?: string
      short_title?: string
      save_as_draft?: boolean
      schedule_at_override?: string | null
    }
  }>
  confirmed: boolean
}

export interface VideoPublishTargetEditRequest {
  options: NonNullable<CreateVideoPublishBatchRequest['targets'][number]['options']>
  confirmed: boolean
}

export interface ArticleImageSettings {
  base_url: string | null
  model_id: string
  provider_id?: string | null
  api_key_configured: boolean
  credential_available: boolean
  configured: boolean
  profile_id?: string | null
  profile_name?: string | null
}

export interface TranscriptComparisonData {
  artifact_id: string
  source_segments: Array<{
    text: string
    start_seconds?: number
    end_seconds?: number
  }>
  ocr_corrections: Array<{
    start_seconds?: number
    end_seconds?: number
    asr_text?: string
    corrected_text?: string
    ocr_text?: string
    frame_ids?: string[]
    frame_times_seconds?: number[]
  }>
  ocr_result_artifact_id: string | null
}

export interface TaskConsentRecord {
  step_kind: StepKind
  provider_id: string
  model_id: string
  artifact_ids: string[]
  authorized: boolean
  authorized_at: string | null
}

export interface CommentItem {
  platform_comment_id: string
  parent_id: string | null
  author: string | null
  text: string
  like_count: number | null
  created_at: string | null
  collected_at: string
}

export interface CommentCollectionState {
  status: string
  sort_order: 'likes_desc' | 'time_desc' | 'time_asc'
  page_count: number
  collected_count: number
  missing_like_count: number
  missing_fields: Record<string, number>
  stop_reason: string | null
  updated_at: string
}

export interface CommentPageResponse {
  items: CommentItem[]
  state: CommentCollectionState | null
  sort: 'likes_desc' | 'time_desc' | 'time_asc'
  limit: number
  offset: number
  total: number
  scope: string
}

export interface CommentInsight {
  comment_id: string
  category: '有用' | '高赞' | '有趣'
  reason: string
  text: string
  like_count: number | null
}

export interface CommentInsights {
  schema_version: number
  model_id: string | null
  selected: CommentInsight[]
  themes: string[]
  audience_questions: string[]
  disagreements: string[]
  claims_to_verify: string[]
  processed_comment_ids: string[]
  source_artifact_ids: string[]
}

export interface TaskDetail {
  task: TaskRecord
  asset: AssetRecord
  transcription_config: TaskTranscriptionConfig
  artifacts: TaskArtifact[]
  consents: TaskConsentRecord[]
  model_runs: Array<{
    id: string
    step_id: string | null
    provider_id: string
    model_id: string
    artifact_ids: string[]
    request_id: string | null
    usage: Record<string, unknown>
    cost: Record<string, unknown> | null
    status: string
    error_code: string | null
    created_at: string
    finished_at: string | null
  }>
}

export interface TaskListResponse {
  items: TaskSummary[]
  limit: number
  offset: number
  total: number
}

export interface TaskListFilters {
  limit?: number
  offset?: number
  status?: TaskStatus | ''
  platform?: string
  dateFrom?: string
  dateTo?: string
  search?: string
  sort?: 'newest' | 'oldest' | 'title'
  includeRemoved?: boolean
}

export interface TaskDeletionItem {
  index: number
  name: string
  kind: string
  kind_label?: string
  size_bytes: number | null
  relative_path: string
  full_path: string
  status: string
  reason: string
  action: 'delete' | 'delete_data' | 'keep'
  artifact_id: string | null
  can_open: boolean
  can_reveal: boolean
}

export interface TaskDeletionPreview {
  task_id: string
  title: string
  status: TaskStatus
  items: TaskDeletionItem[]
  token: string
  blocked_reason: string | null
  can_stop: boolean
  delete_file_count: number
  delete_bytes: number
  keep_file_count: number
  shared_comment_count: number
}

export interface TaskDeletionResult {
  deleted: boolean
  deleted_files: number
  deleted_comments: number
  cleanup_pending: string[]
}

export interface ReferenceNote {
  relative_path: string
  title: string
  content: string
  sha256: string
}

export interface VaultPreview {
  target_path: string
  exists: boolean
  current_sha256: string | null
  proposed_sha256: string
  conflict: boolean
  suggested_path: string | null
  title: string
  content: string
}

export interface VaultWriteResult {
  written: boolean
  path: string | null
  conflict_path: string | null
  reason: string | null
}

export interface SearchHit {
  asset_id: string
  task_id: string
  artifact_id: string | null
  snippet: string
  start_offset: number
  score: number
  method: 'full_text' | 'vector'
}

export interface SearchResponse {
  items: SearchHit[]
  limit: number
  offset: number
  total: number
  method: 'full_text' | 'vector'
  scope: string
  semantic_fallback: string | null
}
