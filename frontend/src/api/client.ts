import type {
  LocalAsrStatus,
  LocalModelPreview,
  CreateTaskRequest,
  AppSettings,
  ConfigResponse,
  ConfigSaveResponse,
  CommentPageResponse,
  CommentInsights,
  CredentialStatus,
  ModelProfile,
  SaveModelProfileRequest,
  ModelCapabilityStatus,
  DiagnosticResult,
  PlatformStatus,
  PlatformAccountStatus,
  ReferenceNote,
  SearchResponse,
  SourcePreview,
  TaskDetail,
  UpdateTranscriptionConfigRequest,
  TaskListResponse,
  TaskListFilters,
  TaskSummary,
  TaskArtifact,
  TaskDeletionPreview,
  TaskDeletionResult,
  TranscriptComparisonData,
  VaultPreview,
  VaultWriteResult,
  UploadReceipt,
  CollectionMediaSelectResult,
  VideoPublishPoiSearchResult,
  WorkbenchHealth,
  ArticleDocument,
  ArticleDeleteResult,
  ArticleListItem,
  ArticlePlatformStatus,
  ArticleImageSettings,
  ArticleRecord,
  ArticlePublication,
  ArticleSourceScript,
  ArticlePublishTarget,
  CsdnTagCatalog,
  ToutiaoAccountPublishOptions,
  VideoPublishSauStatus,
  VideoPublishPlatform,
  VideoPublishAccount,
  VideoPublishMedia,
  VideoPublishBatch,
  VideoPublishCoverUpload,
  VideoPublishDraft,
  VideoPublishDraftDetail,
  VideoPublishDraftRestoreResult,
  VideoPublishDraftSaveRequest,
  CreateVideoPublishBatchRequest,
  VideoPublishTargetEditRequest,
} from './types'

const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function safeMessage(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '本机工作台请求失败，请稍后重试。'
  return value
    .replace(/https?:\/\/[^\s"'<>]+/gi, '<链接已隐藏>')
    .replace(/(token|signature|sign|auth|authorization|api[_-]?key|cookie|secret)=([^&\s]+)/gi, '$1=<已隐藏>')
    .slice(0, 500)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      mode: 'same-origin',
      credentials: 'same-origin',
      cache: 'no-store',
      headers,
    })
  } catch {
    throw new ApiError(0, '无法连接本机工作台服务，请确认服务正在运行。')
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload
      ? (payload as { detail?: unknown }).detail
      : null
    throw new ApiError(response.status, safeMessage(detail))
  }
  return payload as T
}

export const api = {
  getVideoPublishStatus(): Promise<{ sau: VideoPublishSauStatus }> {
    return request('/video-publish/status')
  },

  saveVideoPublishSettings(sauProjectPath: string | null): Promise<{ sau: VideoPublishSauStatus }> {
    return request('/video-publish/settings', {
      method: 'PUT',
      body: JSON.stringify({ sau_project_path: sauProjectPath }),
    })
  },

  getVideoPublishPlatforms(scope: 'video' | 'article' = 'video'): Promise<{ platforms: VideoPublishPlatform[] }> {
    return request(`/video-publish/platforms?scope=${scope}`)
  },

  getVideoPublishAccounts(): Promise<{ items: VideoPublishAccount[]; notices?: { account_id: string; platform_id: string; message: string }[] }> {
    return request('/video-publish/accounts')
  },

  getBilibiliCategories(accountId: string): Promise<{ items: { id: number; name: string }[] }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/bilibili-categories`)
  },

  getBilibiliStatements(accountId: string): Promise<{ items: { id: number; name: string }[] }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/bilibili-statements`)
  },

  videoPublishAccountLoginQrUrl(accountId: string, cacheKey: number): string {
    return `${API_BASE}/video-publish/accounts/${encodeURIComponent(accountId)}/login-qr?v=${cacheKey}`
  },

  addVideoPublishAccount(input: { platform_id: string; account_alias?: string; display_name?: string }): Promise<VideoPublishAccount> {
    return request('/video-publish/accounts', { method: 'POST', body: JSON.stringify(input) })
  },

  loginVideoPublishAccount(accountId: string): Promise<VideoPublishAccount> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/login`, { method: 'POST', body: JSON.stringify({}) })
  },

  cancelVideoPublishAccountLogin(accountId: string): Promise<{ message: string }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/cancel-login`, { method: 'POST', body: JSON.stringify({}) })
  },

  checkVideoPublishAccount(accountId: string): Promise<VideoPublishAccount> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/check`, { method: 'POST', body: JSON.stringify({}) })
  },

  openVideoPublishAccountWorkbench(accountId: string): Promise<{ message: string }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/workbench`, { method: 'POST', body: JSON.stringify({}) })
  },

  getDouyinRepostHistory(accountId: string): Promise<{ items: { id: string; title: string }[] }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/repost-history`, { method: 'POST', body: JSON.stringify({}) })
  },

  searchDouyinPoi(accountId: string, keyword: string, cityCode: string | null): Promise<VideoPublishPoiSearchResult> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/poi-search`, { method: 'POST', body: JSON.stringify({ keyword, city_code: cityCode }) })
  },

  searchXhsPoi(accountId: string, keyword: string): Promise<{ items: import('./types').VideoPublishXhsPoi[] }> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}/xiaohongshu-poi-search`, { method: 'POST', body: JSON.stringify({ keyword }) })
  },

  searchDouyinTopics(keyword: string): Promise<{ items: import('./types').VideoPublishDouyinTopic[] }> {
    return request('/video-publish/douyin/topic-search', { method: 'POST', body: JSON.stringify({ keyword }) })
  },

  logoutVideoPublishAccount(accountId: string): Promise<void> {
    return request(`/video-publish/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' })
  },

  listVideoPublishMedia(): Promise<{ items: VideoPublishMedia[] }> {
    return request('/video-publish/media')
  },

  uploadVideoPublishCover(file: File): Promise<VideoPublishCoverUpload> {
    const form = new FormData()
    form.append('file', file)
    return request('/video-publish/covers', { method: 'POST', body: form })
  },

  deleteVideoPublishCover(uploadId: string): Promise<void> {
    return request(`/video-publish/covers/${encodeURIComponent(uploadId)}`, { method: 'DELETE' })
  },

  keepVideoPublishCovers(ids: string[]): Promise<{ kept: number }> {
    return request('/video-publish/covers/keepalive', { method: 'POST', body: JSON.stringify({ ids }) })
  },

  listVideoPublishDrafts(): Promise<{ items: VideoPublishDraft[] }> {
    return request('/video-publish/drafts')
  },

  saveVideoPublishDraft(input: VideoPublishDraftSaveRequest): Promise<VideoPublishDraftDetail> {
    return request('/video-publish/drafts', { method: 'POST', body: JSON.stringify(input) })
  },

  updateVideoPublishDraft(draftId: string, input: VideoPublishDraftSaveRequest): Promise<VideoPublishDraftDetail> {
    return request(`/video-publish/drafts/${encodeURIComponent(draftId)}`, { method: 'PUT', body: JSON.stringify(input) })
  },

  restoreVideoPublishDraft(draftId: string): Promise<VideoPublishDraftRestoreResult> {
    return request(`/video-publish/drafts/${encodeURIComponent(draftId)}/restore`, { method: 'POST', body: JSON.stringify({}) })
  },

  deleteVideoPublishDraft(draftId: string): Promise<{ deleted: boolean; bytes_freed: number }> {
    return request(`/video-publish/drafts/${encodeURIComponent(draftId)}`, { method: 'DELETE' })
  },

  videoPublishDraftMediaUrl(draftId: string): string {
    return `${API_BASE}/video-publish/drafts/${encodeURIComponent(draftId)}/media`
  },

  videoPublishDraftCoverUrl(draftId: string, slot: string): string {
    return `${API_BASE}/video-publish/drafts/${encodeURIComponent(draftId)}/covers/${encodeURIComponent(slot)}`
  },

  listVideoPublishBatches(options: { q?: string; status?: string; limit?: number; offset?: number } = {}): Promise<{ items: VideoPublishBatch[]; total: number; limit: number; offset: number; status_counts: Record<string, number> }> {
    const params = new URLSearchParams()
    if (options.q) params.set('q', options.q)
    if (options.status) params.set('status', options.status)
    if (options.limit) params.set('limit', String(options.limit))
    if (options.offset) params.set('offset', String(options.offset))
    return request(`/video-publish/batches${params.toString() ? `?${params.toString()}` : ''}`)
  },

  getVideoPublishBatch(batchId: string): Promise<VideoPublishBatch> {
    return request(`/video-publish/batches/${encodeURIComponent(batchId)}`)
  },

  videoPublishBatchCoverUrl(batchId: string, filename: string): string {
    return `${API_BASE}/video-publish/batches/${encodeURIComponent(batchId)}/covers/${encodeURIComponent(filename)}`
  },

  videoPublishBatchMediaUrl(batchId: string): string {
    return `${API_BASE}/video-publish/batches/${encodeURIComponent(batchId)}/media`
  },

  deleteVideoPublishBatch(batchId: string): Promise<{ deleted: boolean; bytes_freed: number }> {
    return request(`/video-publish/batches/${encodeURIComponent(batchId)}`, { method: 'DELETE' })
  },

  cleanVideoPublishBatchMedia(batchId: string): Promise<{ deleted: boolean; bytes_freed: number }> {
    return request(`/video-publish/batches/${encodeURIComponent(batchId)}/media`, { method: 'DELETE' })
  },

  createVideoPublishBatch(input: CreateVideoPublishBatchRequest): Promise<VideoPublishBatch> {
    return request('/video-publish/batches', { method: 'POST', body: JSON.stringify(input) })
  },

  editVideoPublishTarget(targetId: string, input: VideoPublishTargetEditRequest): Promise<VideoPublishBatch> {
    return request(`/video-publish/targets/${encodeURIComponent(targetId)}/edit`, { method: 'POST', body: JSON.stringify(input) })
  },

  syncVideoPublishTarget(targetId: string): Promise<{ queued: boolean }> {
    return request(`/video-publish/targets/${encodeURIComponent(targetId)}/sync`, { method: 'POST', body: JSON.stringify({}) })
  },

  retryVideoPublishTarget(targetId: string, confirmed: boolean, changeSchedule = false, scheduleAt: string | null = null, xhsManualSubmit = false): Promise<VideoPublishBatch> {
    return request(`/video-publish/targets/${encodeURIComponent(targetId)}/retry`, {
      method: 'POST',
      body: JSON.stringify({
        confirmed, change_schedule: changeSchedule, schedule_at: scheduleAt,
        ...(xhsManualSubmit ? { xhs_manual_submit: true } : {}),
      }),
    })
  },

  confirmVideoPublishTarget(targetId: string, state: 'published' | 'draft_saved' | 'not_published', confirmed: boolean): Promise<VideoPublishBatch> {
    return request(`/video-publish/targets/${encodeURIComponent(targetId)}/confirm`, {
      method: 'POST', body: JSON.stringify({ state, confirmed }),
    })
  },

  cancelVideoPublishTarget(targetId: string): Promise<VideoPublishBatch> {
    return request(`/video-publish/targets/${encodeURIComponent(targetId)}/cancel`, { method: 'POST', body: JSON.stringify({}) })
  },

  listArticleSourceScripts(query = ''): Promise<{ items: ArticleSourceScript[] }> {
    const params = new URLSearchParams()
    if (query.trim()) params.set('q', query.trim())
    return request<{ items: ArticleSourceScript[] }>(`/article-source-scripts${params.toString() ? `?${params.toString()}` : ''}`)
  },

  listArticles(options: { query?: string; status?: string; platform?: string; limit?: number; offset?: number } = {}): Promise<{ items: ArticleListItem[]; total: number; limit: number; offset: number }> {
    const params = new URLSearchParams()
    if (options.query) params.set('q', options.query)
    if (options.status) params.set('status', options.status)
    if (options.platform) params.set('platform', options.platform)
    if (options.limit) params.set('limit', String(options.limit))
    if (options.offset) params.set('offset', String(options.offset))
    const query = params.toString()
    return request(`/articles${query ? `?${query}` : ''}`)
  },

  deleteArticle(articleId: string): Promise<ArticleDeleteResult> {
    return request<ArticleDeleteResult>(`/articles/${encodeURIComponent(articleId)}`, { method: 'DELETE' })
  },

  createArticle(title = '', document?: ArticleDocument): Promise<ArticleRecord> {
    const body = document ? { title: document.title, document } : { title }
    return request<ArticleRecord>('/articles', { method: 'POST', body: JSON.stringify(body) })
  },

  createArticleFromScript(artifactId: string, authorized: boolean): Promise<ArticleRecord> {
    return request<ArticleRecord>('/articles/from-script', {
      method: 'POST',
      body: JSON.stringify({ artifact_id: artifactId, authorized }),
    })
  },

  getArticle(articleId: string): Promise<ArticleRecord> {
    return request<ArticleRecord>(`/articles/${encodeURIComponent(articleId)}`)
  },

  getCsdnArticleTags(articleId: string, keyword = ''): Promise<CsdnTagCatalog> {
    return request<CsdnTagCatalog>(`/articles/${encodeURIComponent(articleId)}/csdn-tags`, {
      method: 'POST', body: JSON.stringify({ keyword }),
    })
  },

  saveArticle(articleId: string, expectedVersion: number, document: ArticleDocument): Promise<ArticleRecord> {
    return request<ArticleRecord>(`/articles/${encodeURIComponent(articleId)}`, {
      method: 'PUT',
      body: JSON.stringify({ expected_version: expectedVersion, document }),
    })
  },

  finalizeArticle(articleId: string): Promise<ArticleRecord> {
    return request<ArticleRecord>(`/articles/${encodeURIComponent(articleId)}/finalize`, { method: 'POST', body: JSON.stringify({}) })
  },

  getArticlePlatforms(includeLiveStatus = true): Promise<{ platforms: ArticlePlatformStatus[] }> {
    return request('/article-platforms' + (includeLiveStatus ? '' : '?live_status=false'))
  },

  startWeiboLogin(): Promise<{ started: boolean; message: string }> {
    return request('/article-platforms/weibo/login', { method: 'POST', body: JSON.stringify({}) })
  },

  openCsdnEditor(): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>('/article-platforms/csdn/editor', { method: 'POST', body: JSON.stringify({}) })
  },

  clearCsdnSession(): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>('/article-platforms/csdn/session', { method: 'DELETE' })
  },

  openToutiaoEditor(): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>('/article-platforms/toutiao/editor', { method: 'POST', body: JSON.stringify({}) })
  },

  ensureToutiaoProfile(): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>('/article-platforms/toutiao/profile', { method: 'POST', body: JSON.stringify({}) })
  },

  getToutiaoPublishOptions(): Promise<ToutiaoAccountPublishOptions> {
    return request<ToutiaoAccountPublishOptions>('/article-platforms/toutiao/publish-options')
  },

  clearToutiaoSession(): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>('/article-platforms/toutiao/session', { method: 'DELETE' })
  },

  getPublicIpv4(): Promise<{ ip: string }> {
    return request('/article-platforms/public-ip')
  },

  saveArticlePlatformAccount(platformId: string, publicValue: string, secretValue: string, clearSecret = false): Promise<ArticlePlatformStatus> {
    return request<ArticlePlatformStatus>(`/article-platforms/${encodeURIComponent(platformId)}`, {
      method: 'PUT',
      body: JSON.stringify({ public_value: publicValue, secret_value: secretValue || null, clear_secret: clearSecret }),
    })
  },

  addArticlePlatformAccount(platformId: string, accountName: string, publicValue: string, secretValue: string): Promise<ArticlePlatformStatus> {
    return request<ArticlePlatformStatus>(`/article-platforms/${encodeURIComponent(platformId)}/accounts`, {
      method: 'POST',
      body: JSON.stringify({ account_name: accountName, public_value: publicValue, secret_value: secretValue }),
    })
  },

  updateArticlePlatformAccount(platformId: string, accountId: string, accountName: string, publicValue: string, secretValue: string): Promise<ArticlePlatformStatus> {
    return request<ArticlePlatformStatus>(`/article-platforms/${encodeURIComponent(platformId)}/accounts/${encodeURIComponent(accountId)}`, {
      method: 'PUT',
      body: JSON.stringify({ account_name: accountName, public_value: publicValue, secret_value: secretValue || null }),
    })
  },

  verifyWechatAccountCredentials(accountId: string): Promise<ArticlePlatformStatus> {
    return request<ArticlePlatformStatus>(
      '/article-platforms/wechat/accounts/' + encodeURIComponent(accountId) + '/verify',
      { method: 'POST', body: JSON.stringify({}) },
    )
  },

  removeArticlePlatformAccount(platformId: string, accountId: string): Promise<ArticlePlatformStatus> {
    return request<ArticlePlatformStatus>(`/article-platforms/${encodeURIComponent(platformId)}/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' })
  },

  getArticleImageSettings(): Promise<ArticleImageSettings> {
    return request('/article-image-settings')
  },

  saveArticleImageSettings(input: { base_url: string; model_id: string; api_key?: string; clear_api_key?: boolean }): Promise<ArticleImageSettings> {
    return request<ArticleImageSettings>('/article-image-settings', {
      method: 'PUT',
      body: JSON.stringify({ ...input, api_key: input.api_key || null, clear_api_key: input.clear_api_key === true }),
    })
  },

  generateArticleImage(articleId: string, target: 'cover' | 'paragraph', blockId: string | null, confirmed: boolean, mode: 'provider' | 'vector' = 'provider'): Promise<{ article: ArticleRecord; image_url: string; model_id: string; request_id: string | null; cost_known: boolean }> {
    return request(`/articles/${encodeURIComponent(articleId)}/images/generate`, {
      method: 'POST',
      body: JSON.stringify({ target, block_id: blockId, ...(mode === 'vector' ? { mode } : {}), confirmed }),
    })
  },

  articleImageUrl(articleId: string, filename: string): string {
    return `${API_BASE}/articles/${encodeURIComponent(articleId)}/images/${encodeURIComponent(filename)}`
  },

  uploadArticlePublishCover(articleId: string, file: File): Promise<{ id: string; reference: string; preview_url: string }> {
    const form = new FormData()
    form.append('file', file)
    return request(`/articles/${encodeURIComponent(articleId)}/publish-covers`, { method: 'POST', body: form })
  },

  publishArticle(articleId: string, targets: ArticlePublishTarget[], confirmed: boolean): Promise<{ results: ArticlePublication[]; article: ArticleRecord }> {
    return request(`/articles/${encodeURIComponent(articleId)}/publish`, {
      method: 'POST',
      body: JSON.stringify({ targets, confirmed }),
    })
  },

  refreshArticlePublication(articleId: string, publicationId: string): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/publications/${encodeURIComponent(publicationId)}/refresh`, { method: 'POST', body: JSON.stringify({}) })
  },

  retryArticleFanNotification(articleId: string, publicationId: string, confirmed: boolean): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/publications/${encodeURIComponent(publicationId)}/fan-notification/retry`, {
      method: 'POST',
      body: JSON.stringify({ confirmed }),
    })
  },

  confirmManualArticlePublication(articleId: string, platformId: string, confirmed: boolean): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/manual-published`, {
      method: 'POST',
      body: JSON.stringify({ platform_id: platformId, confirmed }),
    })
  },

  confirmXiaohongshuArticlePublication(articleId: string, publicationId: string, outcome: 'published' | 'not_published'): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/publications/${encodeURIComponent(publicationId)}/confirm-xiaohongshu`, {
      method: 'POST',
      body: JSON.stringify({ outcome, confirmed: true }),
    })
  },

  confirmCsdnArticlePublication(articleId: string, publicationId: string, outcome: 'published' | 'not_published'): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/publications/${encodeURIComponent(publicationId)}/confirm-csdn`, {
      method: 'POST', body: JSON.stringify({ confirmed: true, outcome }),
    })
  },

  confirmToutiaoArticlePublication(articleId: string, publicationId: string, outcome: 'published' | 'not_published'): Promise<ArticlePublication> {
    return request<ArticlePublication>(`/articles/${encodeURIComponent(articleId)}/publications/${encodeURIComponent(publicationId)}/confirm-toutiao`, {
      method: 'POST', body: JSON.stringify({ confirmed: true, outcome }),
    })
  },

  async uploadArticleCover(articleId: string, file: File): Promise<{ article: ArticleRecord; cover_url: string }> {
    const form = new FormData()
    form.set('file', file)
    let response: Response
    try {
      response = await fetch(`${API_BASE}/articles/${encodeURIComponent(articleId)}/cover`, {
        method: 'POST', mode: 'same-origin', credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json' }, body: form,
      })
    } catch {
      throw new ApiError(0, '无法连接本机工作台服务，请确认服务正在运行。')
    }
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    if (!response.ok) throw new ApiError(response.status, safeMessage(payload?.detail))
    return payload as { article: ArticleRecord; cover_url: string }
  },

  articleCoverUrl(articleId: string): string {
    return `${API_BASE}/articles/${encodeURIComponent(articleId)}/cover`
  },

  uploadMedia(file: File, confirmOverLimit: boolean, onProgress: (percent: number) => void): Promise<UploadReceipt> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      const form = new FormData()
      form.set('file', file)
      form.set('confirm_over_limit', String(confirmOverLimit))
      xhr.open('POST', `${API_BASE}/uploads`)
      xhr.withCredentials = true
      xhr.responseType = 'json'
      xhr.setRequestHeader('Accept', 'application/json')
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)))
        }
      }
      xhr.onerror = () => reject(new ApiError(0, '无法连接本机工作台服务，请确认服务正在运行。'))
      xhr.onabort = () => reject(new ApiError(0, '文件上传已取消。'))
      xhr.onload = () => {
        const payload = xhr.response as { detail?: unknown } | UploadReceipt | null
        if (xhr.status >= 200 && xhr.status < 300 && payload && 'uploadId' in payload) {
          onProgress(100)
          resolve(payload as UploadReceipt)
          return
        }
        const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null
        reject(new ApiError(xhr.status, safeMessage(detail)))
      }
      xhr.send(form)
    })
  },

  deleteUpload(uploadId: string): Promise<{ deleted: boolean }> {
    return request<{ deleted: boolean }>(`/uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' })
  },

  selectLocalVideo(): Promise<{ source: UploadReceipt | null }> {
    return request('/video-publish/local-sources/select', { method: 'POST', body: '{}' })
  },

  selectCollectionMedia(confirmOverLimit: boolean): Promise<CollectionMediaSelectResult> {
    return request('/collection/local-media/select', {
      method: 'POST',
      body: JSON.stringify({ confirm_over_limit: confirmOverLimit }),
    })
  },

  deleteCollectionMedia(sourceId: string): Promise<{ deleted: boolean }> {
    return request(`/collection/local-media/${encodeURIComponent(sourceId)}`, { method: 'DELETE' })
  },

  getHealth(): Promise<WorkbenchHealth> {
    return request<WorkbenchHealth>('/health')
  },

  getLocalOcrStatus(): Promise<{ installed: boolean; ready: boolean; model: string; device: string }> {
    return request('/local-ocr/status')
  },

  getPlatforms(): Promise<PlatformStatus[]> {
    return request<PlatformStatus[]>('/platforms')
  },

  getPlatformAccounts(): Promise<{ platforms: PlatformAccountStatus[] }> {
    return request<{ platforms: PlatformAccountStatus[] }>('/platform-accounts')
  },

  openPlatformLogin(platform: PlatformAccountStatus['id']): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>(`/platform-accounts/${encodeURIComponent(platform)}/login`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  },

  clearPlatformLogin(platform: PlatformAccountStatus['id']): Promise<PlatformAccountStatus> {
    return request<PlatformAccountStatus>(`/platform-accounts/${encodeURIComponent(platform)}`, { method: 'DELETE' })
  },

  previewSource(sourceText: string): Promise<SourcePreview> {
    return request<SourcePreview>('/sources/preview', {
      method: 'POST',
      body: JSON.stringify({ source_text: sourceText }),
    })
  },

  createTask(input: CreateTaskRequest): Promise<TaskSummary> {
    const body = {
      source_url: input.sourceUrl,
      upload_id: input.uploadId,
      local_source_id: input.localSourceId,
      steps: input.steps,
      script_style_ids: input.scriptStyleIds ?? [],
      allow_first_frame: input.allowFirstFrame,
      comment_sort_order: input.commentSortOrder ?? 'likes_desc',
      transcription_mode: input.transcriptionMode ?? 'cloud',
      ocr_mode: input.ocrMode ?? 'cloud',
      ocr_interval_seconds: input.ocrIntervalSeconds ?? 2,
      ocr_scan_profile: input.ocrScanProfile ?? 'full',
      local_model: input.localModel ?? 'small',
      local_language: input.localLanguage ?? 'auto',
      local_device: input.localDevice ?? 'auto',
      platform_access_mode: input.platformAccessMode ?? 'anonymous',
      platform_account_id: input.platformAccountId,
      model_profile_id: input.modelProfileId,
      consents: input.consents.map((consent) => ({
        step_kind: consent.stepKind,
        provider_id: consent.providerId,
        model_id: consent.modelId,
        artifact_ids: consent.artifactIds,
        authorized: consent.authorized,
        authorized_at: consent.authorizedAt,
      })),
    }
    return request<TaskSummary>('/tasks', { method: 'POST', body: JSON.stringify(body) })
  },

  getTask(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`)
  },

  openTaskFolder(taskId: string): Promise<{ opened: boolean }> {
    return request<{ opened: boolean }>(`/tasks/${encodeURIComponent(taskId)}/open-folder`, { method: 'POST' })
  },

  openTaskOutput(taskId: string, relativePath: string, reveal = false): Promise<{ opened: boolean }> {
    const query = new URLSearchParams({ relative_path: relativePath, reveal: String(reveal) })
    return request<{ opened: boolean }>(`/tasks/${encodeURIComponent(taskId)}/open-output?${query.toString()}`, { method: 'POST' })
  },

  removeTaskFromArchive(taskId: string): Promise<{ removed: boolean }> {
    return request(`/tasks/${encodeURIComponent(taskId)}/archive/remove`, { method: 'POST' })
  },

  restoreTaskToArchive(taskId: string): Promise<{ restored: boolean }> {
    return request(`/tasks/${encodeURIComponent(taskId)}/archive/restore`, { method: 'POST' })
  },

  getTaskDeletionPreview(taskId: string): Promise<TaskDeletionPreview> {
    return request(`/tasks/${encodeURIComponent(taskId)}/delete-preview`)
  },

  permanentlyDeleteTask(taskId: string, previewToken: string): Promise<TaskDeletionResult> {
    return request(`/tasks/${encodeURIComponent(taskId)}`, {
      method: 'DELETE',
      body: JSON.stringify({ preview_token: previewToken }),
    })
  },

  openArtifact(artifactId: string, reveal = false): Promise<{ opened: boolean }> {
    const query = reveal ? '?reveal=true' : ''
    return request<{ opened: boolean }>(`/artifacts/${encodeURIComponent(artifactId)}/open${query}`, { method: 'POST' })
  },

  openVaultNote(artifactId: string, reveal = false): Promise<{ opened: boolean }> {
    const query = reveal ? '?reveal=true' : ''
    return request<{ opened: boolean }>(`/artifacts/${encodeURIComponent(artifactId)}/open-vault-note${query}`, { method: 'POST' })
  },

  getTextArtifact(artifactId: string): Promise<{ artifact_id: string; sha256: string; content: string }> {
    return request<{ artifact_id: string; sha256: string; content: string }>(`/artifacts/${encodeURIComponent(artifactId)}/text`)
  },

  saveTextArtifactRevision(artifactId: string, expectedSha256: string, content: string): Promise<TaskArtifact> {
    return request<TaskArtifact>(`/artifacts/${encodeURIComponent(artifactId)}/text`, {
      method: 'PUT',
      body: JSON.stringify({ expected_sha256: expectedSha256, content }),
    })
  },

  reviewGeneratedArtifact(artifactId: string, expectedSha256: string): Promise<TaskArtifact & { vault_write?: { written: boolean; path: string | null; reason: string | null } }> {
    return request<TaskArtifact & { vault_write?: { written: boolean; path: string | null; reason: string | null } }>(`/artifacts/${encodeURIComponent(artifactId)}/review`, {
      method: 'POST',
      body: JSON.stringify({ expected_sha256: expectedSha256 }),
    })
  },

  generateStructure(taskId: string, transcriptId: string, ocrId: string | null, provider: string, model: string): Promise<{ step_id: string; task: TaskDetail['task'] }> {
    return request(`/tasks/${encodeURIComponent(taskId)}/structure-analysis`, {
      method: 'POST',
      body: JSON.stringify({ transcript_artifact_id: transcriptId, ocr_artifact_id: ocrId, provider_id: provider, model_id: model, authorized: true }),
    })
  },

  generateScriptsFromSelection(
    taskId: string,
    transcriptArtifactId: string,
    commentIds: string[],
    styleIds: string[],
  ): Promise<{ step_id: string; task: TaskDetail['task'] }> {
    return request<{ step_id: string; task: TaskDetail['task'] }>(`/tasks/${encodeURIComponent(taskId)}/generate-scripts`, {
      method: 'POST',
      body: JSON.stringify({
        transcript_artifact_id: transcriptArtifactId,
        comment_ids: commentIds,
        style_ids: styleIds,
        authorized: true,
      }),
    })
  },

  createVideoNotes(taskId: string, transcriptArtifactId: string): Promise<TaskArtifact> {
    return request<TaskArtifact>(`/tasks/${encodeURIComponent(taskId)}/video-notes`, {
      method: 'POST',
      body: JSON.stringify({ transcript_artifact_id: transcriptArtifactId }),
    })
  },

  getComments(taskId: string, sort: CommentPageResponse['sort'], offset: number): Promise<CommentPageResponse> {
    const query = new URLSearchParams({ sort, limit: '100', offset: String(offset) })
    return request<CommentPageResponse>(`/tasks/${encodeURIComponent(taskId)}/comments?${query.toString()}`)
  },

  getCommentInsights(taskId: string): Promise<CommentInsights> {
    return request<CommentInsights>(`/tasks/${encodeURIComponent(taskId)}/comment-insights`)
  },

  listTasks(filters: TaskListFilters = {}): Promise<TaskListResponse> {
    const query = new URLSearchParams({
      limit: String(filters.limit ?? 50),
      offset: String(filters.offset ?? 0),
    })
    if (filters.status) query.set('status', filters.status)
    if (filters.platform) query.set('platform', filters.platform)
    if (filters.dateFrom) query.set('date_from', filters.dateFrom)
    if (filters.dateTo) query.set('date_to', filters.dateTo)
    if (filters.search?.trim()) query.set('search', filters.search.trim())
    if (filters.sort) query.set('sort', filters.sort)
    if (filters.includeRemoved) query.set('include_removed', 'true')
    return request<TaskListResponse>(`/tasks?${query.toString()}`)
  },

  updateTranscriptionConfig(taskId: string, input: UpdateTranscriptionConfigRequest): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/tasks/${encodeURIComponent(taskId)}/transcription-config`,
      { method: 'PUT', body: JSON.stringify(input) },
    )
  },

  retryStep(
    taskId: string,
    stepId: string,
    ocrAction?: 'continue' | 'restart',
    transcriptionAuthorized = false,
  ): Promise<TaskDetail> {
    const body: Record<string, unknown> = {}
    if (ocrAction) body.ocr_action = ocrAction
    if (transcriptionAuthorized) body.transcription_authorized = true
    return request<TaskDetail>(
      `/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepId)}/retry`,
      { method: 'POST', body: JSON.stringify(body) },
    )
  },

  reconcileTranscript(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/tasks/${encodeURIComponent(taskId)}/reconcile-transcript`,
      { method: 'POST' },
    )
  },

  getTranscriptComparison(taskId: string, artifactId: string): Promise<TranscriptComparisonData> {
    const query = new URLSearchParams({ artifact_id: artifactId })
    return request<TranscriptComparisonData>(
      `/tasks/${encodeURIComponent(taskId)}/transcript-comparison?${query.toString()}`,
    )
  },

  authorizeOcrFrames(taskId: string, stepId: string, artifactIds: string[]): Promise<{ queued: boolean; authorized_artifact_ids: string[] }> {
    return request<{ queued: boolean; authorized_artifact_ids: string[] }>(
      `/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepId)}/authorize-ocr`,
      { method: 'POST', body: JSON.stringify({ artifact_ids: artifactIds }) },
    )
  },

  authorizeCommentAnalysis(taskId: string, stepId: string, artifactId: string): Promise<{ queued: boolean; authorized_artifact_ids: string[] }> {
    return request<{ queued: boolean; authorized_artifact_ids: string[] }>(
      `/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepId)}/authorize-comment-analysis`,
      { method: 'POST', body: JSON.stringify({ artifact_id: artifactId, authorized: true }) },
    )
  },

  cancelTask(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  },

  skipTaskStep(taskId: string, stepId: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepId)}/skip`,
      { method: 'POST', body: JSON.stringify({}) },
    )
  },

  getArtifact(artifactId: string): Promise<TaskArtifact> {
    return request<TaskArtifact>(`/artifacts/${encodeURIComponent(artifactId)}`)
  },

  getConfig(): Promise<ConfigResponse> {
    return request<ConfigResponse>('/config')
  },

  async getLocalAsrStatus(): Promise<LocalAsrStatus> {
    const value = await request<LocalAsrStatus>('/local-asr/models')
    if (!value || typeof value.installed !== 'boolean' || typeof value.model_root !== 'string' || !Array.isArray(value.models) ||
        !value.models.every((item) => item && typeof item.id === 'string' && typeof item.ready === 'boolean')) {
      throw new ApiError(502, '本机服务版本不匹配或模型状态数据无效，请重启工作台。')
    }
    return value
  },
  installLocalAsr(): Promise<{ started: boolean }> {
    return request<{ started: boolean }>('/local-asr/install', { method: 'POST' })
  },
  previewLocalModel(model: string): Promise<LocalModelPreview> {
    return request<LocalModelPreview>('/local-asr/models/preview', { method: 'POST', body: JSON.stringify({ model }) })
  },
  prepareLocalModel(model: string, revision: string): Promise<{ started: boolean }> {
    return request<{ started: boolean }>('/local-asr/models/prepare', { method: 'POST', body: JSON.stringify({ model, revision, confirmed: true }) })
  },

  selectLocalFolder(): Promise<{ path: string | null }> {
    return request<{ path: string | null }>('/folders/select', { method: 'POST', body: JSON.stringify({}) })
  },

  saveConfig(settings: AppSettings): Promise<ConfigSaveResponse> {
    return request<ConfigSaveResponse>('/config', {
      method: 'PUT',
      body: JSON.stringify(settings),
    })
  },

  getCredentialStatus(providerId = 'zhipu'): Promise<CredentialStatus> {
    return request<CredentialStatus>(`/credentials/model-api-key?provider_id=${encodeURIComponent(providerId)}`)
  },

  saveModelApiKey(apiKey: string, providerId = 'zhipu'): Promise<CredentialStatus> {
    return request<CredentialStatus>(`/credentials/model-api-key?provider_id=${encodeURIComponent(providerId)}`, {
      method: 'PUT',
      body: JSON.stringify({ api_key: apiKey }),
    })
  },

  deleteModelApiKey(providerId = 'zhipu'): Promise<CredentialStatus> {
    return request<CredentialStatus>(`/credentials/model-api-key?provider_id=${encodeURIComponent(providerId)}`, { method: 'DELETE' })
  },

  getModelProfiles(): Promise<{ items: ModelProfile[] }> {
    return request<{ items: ModelProfile[] }>('/model-profiles')
  },

  createModelProfile(input: SaveModelProfileRequest): Promise<ModelProfile> {
    return request<ModelProfile>('/model-profiles', { method: 'POST', body: JSON.stringify(input) })
  },

  updateModelProfile(profileId: string, input: SaveModelProfileRequest): Promise<ModelProfile> {
    return request<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}`, { method: 'PUT', body: JSON.stringify(input) })
  },

  migrateLegacyModelProfileKey(profileId: string): Promise<ModelProfile> {
    return request<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}/migrate-legacy-key`, { method: 'POST' })
  },

  setDefaultModelProfile(profileId: string): Promise<ModelProfile> {
    return request<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}/default`, { method: 'PUT', body: JSON.stringify({}) })
  },

  setDefaultImageModelProfile(profileId: string): Promise<ModelProfile> {
    return request<ModelProfile>(`/model-profiles/${encodeURIComponent(profileId)}/image-default`, { method: 'PUT', body: JSON.stringify({}) })
  },

  deleteModelProfile(profileId: string): Promise<{ archived: boolean; credential_retained_for_tasks: boolean; default_profile: ModelProfile | null; image_default_profile: ModelProfile | null }> {
    return request(`/model-profiles/${encodeURIComponent(profileId)}`, { method: 'DELETE' })
  },

  getDiagnostics(): Promise<DiagnosticResult[]> {
    return request<DiagnosticResult[]>('/diagnostics')
  },

  getModelCapability(providerId: string, modelId: string): Promise<ModelCapabilityStatus> {
    const query = new URLSearchParams({ provider_id: providerId, model_id: modelId })
    return request<ModelCapabilityStatus>(`/models/capability?${query.toString()}`)
  },

  testModelConnection(settings: Partial<Pick<AppSettings, 'model_provider' | 'model_id' | 'model_base_url'>> & { profile_id?: string }): Promise<{ status: string; message: string; request_id: string | null }> {
    return request<{ status: string; message: string; request_id: string | null }>('/diagnostics/model-connection', {
      method: 'POST',
      body: JSON.stringify({
        model_provider: settings.model_provider,
        model_id: settings.model_id,
        model_base_url: settings.model_base_url,
        profile_id: settings.profile_id,
      }),
    })
  },

  readReferenceNotes(relativePaths: string[]): Promise<{ items: ReferenceNote[]; scope: string }> {
    return request<{ items: ReferenceNote[]; scope: string }>('/obsidian/references', {
      method: 'POST',
      body: JSON.stringify({ relative_paths: relativePaths }),
    })
  },

  previewVaultWrite(artifactId: string, relativePath: string): Promise<VaultPreview> {
    return request<VaultPreview>('/obsidian/preview-write', {
      method: 'POST',
      body: JSON.stringify({ artifact_id: artifactId, relative_path: relativePath }),
    })
  },

  writeVaultNote(artifactId: string, relativePath: string, expectedSha256: string | null): Promise<VaultWriteResult> {
    return request<VaultWriteResult>('/obsidian/write', {
      method: 'POST',
      body: JSON.stringify({
        artifact_id: artifactId,
        relative_path: relativePath,
        expected_sha256: expectedSha256,
      }),
    })
  },

  search(query: string, options: { limit?: number; offset?: number; useSemantic?: boolean } = {}): Promise<SearchResponse> {
    return request<SearchResponse>('/search', {
      method: 'POST',
      body: JSON.stringify({
        query,
        limit: options.limit ?? 20,
        offset: options.offset ?? 0,
        use_semantic: options.useSemantic ?? false,
      }),
    })
  },
}
