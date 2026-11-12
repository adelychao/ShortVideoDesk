<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { ApiError, api } from '../api/client'
import type { VideoPublishBatch, VideoPublishDraft, VideoPublishTarget } from '../api/types'
import { videoPublishUserMessage } from '../utils/videoPublishMessages'

const router = useRouter()
const route = useRoute()
const items = ref<VideoPublishBatch[]>([])
const viewMode = ref<'tasks' | 'drafts'>('tasks')
const drafts = ref<VideoPublishDraft[]>([])
const draftsLoading = ref(false)
const draftLoadError = ref('')
const deletingDraftId = ref('')
const total = ref(0)
const query = ref('')
const searchDraft = ref('')
const status = ref('all')
const page = ref(1)
const pageSize = ref(10)
const statusCounts = ref<Record<string, number>>({})
const loading = ref(false)
const busyTarget = ref('')
const cleaningBatchId = ref('')
const deletingBatchId = ref('')
const error = ref('')
const deleteError = ref('')
const syncNotice = ref('')
const retryEditorId = ref('')
const retryMode = ref<'immediate' | 'scheduled'>('scheduled')
const retryScheduleAt = ref('')
const expandedId = ref('')
const failedBatchCoverIds = ref(new Set<string>())
const failedBatchMediaIds = ref(new Set<string>())
const failedDraftMediaIds = ref(new Set<string>())
let refreshTimer: number | undefined
let loadRequestId = 0
let activeLoadCount = 0

const statusTabs = [
  { id: 'all', label: '全部任务' },
  { id: 'in_progress', label: '进行中' },
  { id: 'awaiting_confirmation', label: '待确认' },
  { id: 'published', label: '已发布' },
  { id: 'draft_saved', label: '已保存到平台草稿箱' },
  { id: 'needs_attention', label: '失败或异常' },
]
const platformNames: Record<string, string> = {
  douyin: '抖音', xiaohongshu: '小红书', kuaishou: '快手', bilibili: '哔哩哔哩',
  wechat_channels: '微信视频号', baijiahao: '百家号', weibo: '微博',
}
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const pageNumbers = computed(() => {
  const visibleCount = 5
  const start = Math.max(1, Math.min(page.value - 2, pageCount.value - visibleCount + 1))
  return Array.from({ length: Math.min(visibleCount, pageCount.value) }, (_, index) => start + index)
})
const rangeStart = computed(() => total.value ? (page.value - 1) * pageSize.value + 1 : 0)
const rangeEnd = computed(() => Math.min(page.value * pageSize.value, total.value))

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '视频发布记录读取失败，请稍后重试。'
}

function deleteErrorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '删除视频发布任务失败，请稍后重试。'
}

function statusLabel(value: string): string {
  return ({
    queued: '排队中', running: '发布中', pending_review: '已提交，待平台确认', published: '发布成功', draft_saved: '草稿已保存',
    partial: '部分完成', failed: '失败', unknown: '结果未知', cancelled: '已取消',
  } as Record<string, string>)[value] || value
}

function targetStatusLabel(target: VideoPublishTarget): string {
  if (!target.options.save_as_draft) return statusLabel(target.status)
  return ({
    pending_review: '草稿待确认', unknown: '草稿结果未知', draft_saved: '草稿已保存',
  } as Record<string, string>)[target.status] || statusLabel(target.status)
}

function batchProgressLabel(batch: VideoPublishBatch): string {
  const waiting = batch.summary.pending_review + batch.summary.unknown
  const completed = batch.summary.published + batch.summary.draft_saved
  const parts = [
    batch.summary.published ? `已发布 ${batch.summary.published}` : '',
    batch.summary.draft_saved ? `草稿 ${batch.summary.draft_saved}` : '',
    waiting ? `待确认 ${waiting}` : '',
  ].filter(Boolean)
  return parts.join(' · ') || `已处理 ${completed} / ${batch.summary.total}`
}

function statusClass(value: string): string {
  return `video-state-${value}`
}

function platformName(platformId: string): string {
  return platformNames[platformId] || platformId
}

function platformStatusLabel(value: string | null): string {
  return ({ private: '作品存在 · 私密', present: '作品存在 · 状态待确认', reviewing: '审核中', published: '已发布', rejected: '审核未通过', restricted: '作品受限' } as Record<string, string>)[value || ''] || '尚未确认'
}

function batchPortraitCoverUrl(batch: VideoPublishBatch): string | undefined {
  if (!batch.media_available || failedBatchCoverIds.value.has(batch.id)) return undefined
  const filename = batch.targets.find((target) => target.options.cover_portrait_filename)?.options.cover_portrait_filename
  return filename ? api.videoPublishBatchCoverUrl(batch.id, filename) : undefined
}

function failBatchCover(batchId: string): void {
  failedBatchCoverIds.value = new Set(failedBatchCoverIds.value).add(batchId)
}

function failBatchMedia(batchId: string): void {
  failedBatchMediaIds.value = new Set(failedBatchMediaIds.value).add(batchId)
}

function failDraftMedia(draftId: string): void {
  failedDraftMediaIds.value = new Set(failedDraftMediaIds.value).add(draftId)
}

function seekRepresentativeFrame(event: Event): void {
  const video = event.currentTarget as HTMLVideoElement
  if (!Number.isFinite(video.duration) || video.duration <= 0) return
  const timestamp = video.duration * 0.5
  if (timestamp > 0.1 && Math.abs(video.currentTime - timestamp) > 0.05) video.currentTime = timestamp
}

async function syncPlatform(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  busyTarget.value = target.id
  syncNotice.value = ''
  error.value = ''
  try {
    const result = await api.syncVideoPublishTarget(target.id)
    syncNotice.value = result.queued ? '已开始查询抖音状态，结果将在本页自动更新。' : '该账号任务正在查询，请稍候。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function formatSize(value: number): string {
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

async function load(silent = false, timerDriven = false): Promise<void> {
  if (timerDriven && activeLoadCount > 0) return
  const requestId = ++loadRequestId
  const requestedQuery = query.value
  const requestedStatus = status.value
  const requestedPageSize = pageSize.value
  let requestedPage = page.value
  activeLoadCount += 1
  if (!silent) loading.value = true
  error.value = ''
  try {
    let result = await api.listVideoPublishBatches({ q: requestedQuery, status: requestedStatus, limit: requestedPageSize, offset: (requestedPage - 1) * requestedPageSize })
    if (requestId !== loadRequestId) return
    if (requestedPage > 1 && result.offset >= result.total) {
      requestedPage = Math.max(1, Math.ceil(result.total / requestedPageSize))
      page.value = requestedPage
      result = await api.listVideoPublishBatches({ q: requestedQuery, status: requestedStatus, limit: requestedPageSize, offset: (requestedPage - 1) * requestedPageSize })
      if (requestId !== loadRequestId) return
    }
    items.value = result.items
    total.value = result.total
    statusCounts.value = result.status_counts || { all: result.total }
  } catch (cause) {
    if (requestId !== loadRequestId) return
    error.value = errorMessage(cause)
  } finally {
    activeLoadCount -= 1
    if (requestId === loadRequestId) loading.value = false
  }
}

function submitSearch(): void {
  query.value = searchDraft.value.trim()
  page.value = 1
  void load()
}

function resetSearch(): void {
  searchDraft.value = ''
  query.value = ''
  page.value = 1
  void load()
}

function selectStatus(value: string): void {
  status.value = value
  page.value = 1
  void load()
}

function changePageSize(event: Event): void {
  pageSize.value = Number((event.target as HTMLSelectElement).value)
  page.value = 1
  void load()
}

function goToPage(value: number): void {
  if (value < 1 || value > pageCount.value || value === page.value) return
  page.value = value
  void load()
}

function openNewTask(): void {
  void router.push({ name: 'video-publish-create' })
}

async function loadDrafts(): Promise<void> {
  if (draftsLoading.value) return
  draftsLoading.value = true
  draftLoadError.value = ''
  try {
    const result = await api.listVideoPublishDrafts()
    drafts.value = result.items
  } catch (cause) {
    draftLoadError.value = cause instanceof ApiError ? cause.message : '本地草稿读取失败，请稍后重试。'
  } finally {
    draftsLoading.value = false
  }
}

function selectView(value: 'tasks' | 'drafts'): void {
  if (viewMode.value === value) return
  viewMode.value = value
  if (value === 'tasks') void load()
  else void loadDrafts()
}

function continueDraft(draft: VideoPublishDraft): void {
  void router.push({ name: 'video-publish-create', query: { draft_id: draft.id } })
}

async function deleteDraft(draft: VideoPublishDraft): Promise<void> {
  if (deletingDraftId.value || draftsLoading.value) return
  const scopeText = draft.linked_media
    ? '封面和已填写的发布设置会从本机移除；原视频文件不受影响，会保留在原位置。'
    : '视频副本、封面和已填写的发布设置都会从本机移除。'
  try {
    await ElMessageBox.confirm(
      `确定删除「${draft.title}」的本地草稿吗？${scopeText}`,
      '删除本地草稿',
      { confirmButtonText: '删除草稿', cancelButtonText: '保留草稿', confirmButtonType: 'danger', type: 'warning' },
    )
  } catch {
    return
  }
  deletingDraftId.value = draft.id
  draftLoadError.value = ''
  try {
    const result = await api.deleteVideoPublishDraft(draft.id)
    if (!result.deleted) throw new Error('本地草稿未能删除，请刷新后重试。')
    drafts.value = drafts.value.filter((item) => item.id !== draft.id)
    await loadDrafts()
    ElMessage.success(`本地草稿已删除，释放 ${formatSize(result.bytes_freed)}。`)
  } catch (cause) {
    draftLoadError.value = cause instanceof ApiError ? cause.message : (cause instanceof Error ? cause.message : '删除本地草稿失败，请稍后重试。')
  } finally {
    deletingDraftId.value = ''
  }
}

function openDetails(item: VideoPublishBatch): void {
  void router.push({ name: 'video-publish-detail', params: { batchId: item.id } })
}

function canCleanBatchMedia(batch: VideoPublishBatch): boolean {
  return batch.media_available && canDeleteBatch(batch)
}

function canDeleteBatch(batch: VideoPublishBatch): boolean {
  return batch.targets.length > 0 && batch.targets.every((target) => ['published', 'draft_saved', 'failed', 'cancelled'].includes(target.status))
}

function deleteBlockedReason(batch: VideoPublishBatch): string {
  if (batch.targets.some((target) => target.status === 'queued' || target.status === 'running')) {
    return '请先停止正在排队或发布中的平台任务。'
  }
  if (batch.targets.some((target) => target.status === 'pending_review' || target.status === 'unknown')) {
    return '请先核实并确认所有平台结果。'
  }
  return '没有可删除的平台发布目标。'
}

function deleteBlockedLabel(batch: VideoPublishBatch): string {
  if (batch.targets.some((target) => target.status === 'queued' || target.status === 'running')) return '需先停止任务'
  if (batch.targets.some((target) => target.status === 'pending_review' || target.status === 'unknown')) return '需先核实结果'
  return '暂无可删除目标'
}

async function deleteBatch(batch: VideoPublishBatch): Promise<void> {
  if (deletingBatchId.value || cleaningBatchId.value || busyTarget.value || loading.value || !canDeleteBatch(batch)) return
  const confirmed = window.confirm(
    `确定删除「${batch.title}」吗？\n\n本机发布记录、各平台结果、发布尝试历史，以及该任务专属的视频和封面副本都会删除。\n\n平台上的作品、发布账号和素材库源文件不会删除。`,
  )
  if (!confirmed) return
  deletingBatchId.value = batch.id
  error.value = ''
  deleteError.value = ''
  try {
    await api.deleteVideoPublishBatch(batch.id)
    if (expandedId.value === batch.id) expandedId.value = ''
    await load(true)
  } catch (cause) {
    deleteError.value = deleteErrorMessage(cause)
  } finally {
    deletingBatchId.value = ''
  }
}

async function cleanBatchMedia(batch: VideoPublishBatch): Promise<void> {
  if (cleaningBatchId.value || deletingBatchId.value || busyTarget.value || !batch.media_available || !canCleanBatchMedia(batch)) return
  const confirmed = window.confirm(`清理「${batch.title}」在本机保存的视频和封面副本？发布历史会保留；之后不能从这条任务直接重试，需要重新选择原视频创建新任务。原始视频及素材库文件不会被删除。`)
  if (!confirmed) return
  cleaningBatchId.value = batch.id
  error.value = ''
  try {
    await api.cleanVideoPublishBatchMedia(batch.id)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    cleaningBatchId.value = ''
  }
}

async function confirmPublished(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  if (!window.confirm(`请先在「${platformName(target.platform_id)}」后台确认作品已发布，再标记为成功。`)) return
  busyTarget.value = target.id
  try {
    await api.confirmVideoPublishTarget(target.id, 'published', true)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function confirmNotPublished(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  const checkText = target.options.save_as_draft ? '草稿箱中没有这条视频草稿' : '没有这条作品'
  if (!window.confirm(`请先在「${platformName(target.platform_id)}」后台确认${checkText}。确认后，该目标会转为失败状态，你可以再单独重试。`)) return
  busyTarget.value = target.id
  try {
    await api.confirmVideoPublishTarget(target.id, 'not_published', true)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function confirmDraftSaved(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  if (!window.confirm('请先在微信视频号草稿箱确认这条视频已保存。')) return
  busyTarget.value = target.id
  try {
    await api.confirmVideoPublishTarget(target.id, 'draft_saved', true)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function retryTarget(target: VideoPublishTarget, batch: VideoPublishBatch, withSchedule = false): Promise<void> {
  if (busyTarget.value) return
  const effectiveSchedule = Object.prototype.hasOwnProperty.call(target.options, 'schedule_at_override') ? target.options.schedule_at_override : batch.schedule_at
  if (effectiveSchedule && !withSchedule) {
    retryEditorId.value = target.id
    retryMode.value = 'scheduled'
    retryScheduleAt.value = effectiveSchedule.replace(' ', 'T').slice(0, 16)
    error.value = ''
    return
  }
  if (withSchedule && retryMode.value === 'scheduled') {
    const planned = new Date(retryScheduleAt.value).getTime()
    const remaining = planned - Date.now()
    if (!Number.isFinite(planned) || remaining <= 0) { error.value = '请选择晚于当前时间的发布时间。'; return }
    if (['douyin', 'kuaishou', 'xiaohongshu', 'wechat_channels'].includes(target.platform_id) && remaining <= 2 * 60 * 60 * 1000) { error.value = '定时发布时间必须距现在超过 2 小时，请重新选择。'; return }
    if (target.platform_id === 'douyin' && remaining > 14 * 24 * 60 * 60 * 1000) { error.value = '抖音定时发布时间不能超过 14 天。'; return }
  }
  if (!window.confirm(`重新发送到「${platformName(target.platform_id)} · ${target.account_name || target.account_alias}」？`)) return
  busyTarget.value = target.id
  try {
    await api.retryVideoPublishTarget(target.id, true, withSchedule, withSchedule && retryMode.value === 'scheduled' ? retryScheduleAt.value : null)
    retryEditorId.value = ''
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function cancelTarget(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || !window.confirm('停止这个平台的发布进程？如果视频已经提交到平台，结果仍需人工核实。')) return
  busyTarget.value = target.id
  try {
    await api.cancelVideoPublishTarget(target.id)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

onActivated(() => {
  const requestedBatchId = typeof route.query.batch === 'string' ? route.query.batch : ''
  if (requestedBatchId) {
    viewMode.value = 'tasks'
    status.value = 'all'
    query.value = ''
    searchDraft.value = ''
    page.value = 1
    expandedId.value = requestedBatchId
    void router.replace({ name: 'video-publish-history' })
  }
  if (viewMode.value === 'drafts') void loadDrafts()
  else {
    void load()
    void loadDrafts()
  }
  if (!refreshTimer) refreshTimer = window.setInterval(() => {
    if (viewMode.value === 'tasks') void load(true, true)
  }, 5000)
})
function stopRefresh(): void {
  if (refreshTimer) window.clearInterval(refreshTimer)
  refreshTimer = undefined
}
onDeactivated(stopRefresh)
onBeforeUnmount(stopRefresh)
</script>

<template>
  <section class="video-history-page" aria-label="视频发布记录">
    <form v-if="viewMode === 'tasks'" class="toolbar list-toolbar video-query-panel" aria-label="发布记录检索" @submit.prevent="submitSearch">
      <div class="video-query-entry list-toolbar-filters">
        <label class="tb-search video-search">
          <span class="tb-label">关键词</span>
          <span class="tb-search-control">
            <svg class="tb-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
            <input v-model="searchDraft" class="tb-input" type="search" aria-label="搜索发布任务" placeholder="搜索标题、文件名或账号" />
          </span>
        </label>
        <button class="tb-btn" type="submit" :disabled="loading">搜索</button>
        <button class="tb-btn tb-btn-sec" type="button" :disabled="loading || (!searchDraft && !query)" @click="resetSearch">重置</button>
      </div>
      <div class="video-query-actions list-toolbar-actions">
        <button class="tb-btn tb-btn-quiet" type="button" :disabled="loading || !!deletingBatchId" @click="load()">{{ loading ? '刷新中…' : '↻ 刷新记录' }}</button>
        <button class="tb-btn" type="button" @click="openNewTask">＋ 新建发布任务</button>
      </div>
    </form>
    <div v-else class="toolbar list-toolbar video-query-panel video-draft-toolbar" aria-label="本地草稿操作">
      <p>草稿仅保存在本机，不会进入平台发布队列。</p>
      <div class="video-query-actions list-toolbar-actions">
        <button class="tb-btn tb-btn-quiet" type="button" :disabled="draftsLoading || !!deletingDraftId" @click="loadDrafts()">{{ draftsLoading ? '刷新中…' : '↻ 刷新草稿' }}</button>
        <button class="tb-btn" type="button" @click="openNewTask">＋ 新建发布任务</button>
      </div>
    </div>

    <section class="panel video-history-panel" aria-label="发布历史列表">
      <div class="video-history-tabs video-record-mode-tabs" role="tablist" aria-label="记录类型">
        <button class="video-history-tab" :class="{ 'is-active': viewMode === 'tasks' }" type="button" role="tab" :aria-selected="viewMode === 'tasks'" @click="selectView('tasks')">发布任务</button>
        <button class="video-history-tab" :class="{ 'is-active': viewMode === 'drafts' }" type="button" role="tab" :aria-selected="viewMode === 'drafts'" @click="selectView('drafts')">本地草稿 <span class="video-history-tab-count">{{ drafts.length }}</span></button>
      </div>

      <template v-if="viewMode === 'tasks'">
      <div class="video-history-tabs" role="tablist" aria-label="按发布状态筛选">
        <button v-for="tab in statusTabs" :key="tab.id" class="video-history-tab" :class="{ 'is-active': status === tab.id }" type="button" role="tab" :aria-selected="status === tab.id" @click="selectStatus(tab.id)">
          {{ tab.label }} <span class="video-history-tab-count">{{ statusCounts[tab.id] ?? 0 }}</span>
        </button>
        <span class="video-history-total">共 {{ total }} 条</span>
      </div>

      <div v-if="error" class="info-box form-error" role="alert">{{ error }}</div>
      <div v-if="deleteError" class="info-box form-error" role="alert">{{ deleteError }}</div>
      <div v-if="syncNotice" class="info-box" role="status">{{ syncNotice }}</div>
      <div v-if="loading && !items.length" class="video-empty-state" role="status">正在读取发布历史…</div>
      <div v-else-if="!items.length" class="video-empty-state">
        <span class="video-empty-mark" aria-hidden="true">▶</span>
        <h2>还没有视频发布记录</h2>
        <p>选择一个本地视频和已登录的平台账号，创建第一条多平台发布任务。</p>
        <button class="button" type="button" @click="openNewTask">新建发布任务</button>
      </div>

      <div v-else class="video-table-scroll">
        <div class="video-history-table-head" aria-hidden="true">
          <span>视频内容</span><span>发布账号</span><span>任务状态</span><span>确认进度</span><span>创建时间</span><span>操作</span>
        </div>
        <div class="video-batch-list">
          <article v-for="item in items" :key="item.id" class="video-batch-card">
            <div class="video-batch-row">
              <div class="video-media-cell">
                <div class="video-media-mark" aria-hidden="true">
                  <img v-if="batchPortraitCoverUrl(item)" :src="batchPortraitCoverUrl(item)" alt="" loading="lazy" decoding="async" @error="failBatchCover(item.id)" />
                  <video
                    v-else-if="item.media_available && !failedBatchMediaIds.has(item.id)"
                    :src="api.videoPublishBatchMediaUrl(item.id)"
                    preload="metadata"
                    muted
                    playsinline
                    tabindex="-1"
                    @loadedmetadata="seekRepresentativeFrame"
                    @error="failBatchMedia(item.id)"
                  />
                  <span v-else class="video-media-placeholder">▶</span>
                </div>
                <div class="video-media-copy">
                  <h2 class="video-batch-title">{{ item.title }}</h2>
                  <p v-if="item.media_available" class="video-batch-file">{{ item.media_name }} <span>·</span> {{ formatSize(item.media_size) }}</p>
                  <p v-else class="video-batch-file video-batch-file-cleaned">本机视频副本已清理 · 原记录保留</p>
                </div>
              </div>
              <div class="video-target-cell">
                <span>{{ item.targets[0] ? `${platformName(item.targets[0].platform_id)} · ${item.targets[0].account_name || item.targets[0].account_alias}` : '未指定账号' }}</span>
                <small>{{ item.targets.length }} 个发布目标<span v-if="item.targets.length > 1">，另有 {{ item.targets.length - 1 }} 个账号</span></small>
              </div>
              <span class="video-state" :class="statusClass(item.status)">{{ statusLabel(item.status) }}</span>
              <div class="video-batch-progress">
                <strong>{{ item.summary.published + item.summary.draft_saved }} / {{ item.summary.total }}</strong>
                <small>{{ batchProgressLabel(item) }}</small>
              </div>
              <time class="video-batch-date" :datetime="item.created_at">{{ formatDate(item.created_at) }}</time>
              <div class="video-row-actions">
                <button class="text-button video-detail-toggle" type="button" @click="openDetails(item)">
                  详情 <span aria-hidden="true">›</span>
                </button>
                <span v-if="!canDeleteBatch(item)" :id="`video-delete-reason-${item.id}`" class="sr-only">{{ deleteBlockedReason(item) }}</span>
                <button
                  class="text-button video-delete-toggle"
                  type="button"
                  :disabled="!canDeleteBatch(item) || !!busyTarget || !!cleaningBatchId || !!deletingBatchId || loading"
                  :title="canDeleteBatch(item) ? '删除本机发布记录和该任务副本' : deleteBlockedReason(item)"
                  :aria-label="`删除发布任务：${item.title}`"
                  :aria-describedby="!canDeleteBatch(item) ? `video-delete-reason-${item.id}` : undefined"
                  @click="deleteBatch(item)"
                >{{ deletingBatchId === item.id ? '删除中…' : '删除' }}</button>
                <span v-if="!canDeleteBatch(item)" class="video-delete-help">{{ deleteBlockedLabel(item) }}</span>
              </div>
            </div>

            <div v-if="expandedId === item.id" class="video-target-details">
              <div class="video-batch-media-actions">
                <p v-if="!item.media_available" role="status">本机视频和封面副本已清理；历史记录保留。若要重试，请重新选择原视频创建任务。</p>
                <button v-else-if="canCleanBatchMedia(item)" class="button button-secondary button-small" type="button" :disabled="!!busyTarget || !!cleaningBatchId || !!deletingBatchId" @click="cleanBatchMedia(item)">{{ cleaningBatchId === item.id ? '正在清理…' : '清理本机视频及封面副本' }}</button>
              </div>
              <div v-for="target in item.targets" :key="target.id" class="video-target-detail-row">
                <div class="video-target-detail-name">
                  <strong>{{ platformName(target.platform_id) }}</strong>
                  <span>{{ target.account_name || target.account_alias }}</span>
                </div>
                <span class="video-state" :class="statusClass(target.status)">{{ targetStatusLabel(target) }}</span>
                <div class="video-target-messages">
                  <p>{{ videoPublishUserMessage(target.message) || '任务排队中，等待开始处理。' }}</p>
                  <p>发布时间：{{ (target.options.schedule_at_override !== undefined ? target.options.schedule_at_override : item.schedule_at)?.replace('T', ' ') || '立即发布' }}</p>
                  <p v-if="target.platform_id === 'bilibili' && target.options.cover_4_3_filename && target.options.cover_16_9_filename">任务已归档 4:3 和 16:9 两张封面；当前发布仅使用 4:3 封面。</p>
                  <p v-if="target.platform_id === 'douyin'">抖音状态：{{ platformStatusLabel(target.platform_status) }}<template v-if="target.platform_observed_at">（最近确认 {{ formatDate(target.platform_observed_at) }}）</template><template v-if="target.platform_message">；{{ target.platform_message }}</template><template v-if="target.platform_checked_at">（最近查询 {{ formatDate(target.platform_checked_at) }}）</template></p>
                </div>
                <div class="video-target-actions">
                  <button v-if="target.platform_id === 'douyin' && !['queued', 'running', 'cancelled'].includes(target.status)" class="button button-secondary button-small" type="button" :disabled="!!busyTarget" @click="syncPlatform(target)">查询抖音状态</button>
                  <a v-if="target.remote_url && (target.platform_id !== 'douyin' || target.platform_observed_at)" class="text-button" :href="target.remote_url" target="_blank" rel="noopener noreferrer">打开抖音作品</a>
                  <button v-if="(target.status === 'pending_review' || target.status === 'unknown') && target.options.save_as_draft" class="button button-secondary button-small" type="button" :disabled="busyTarget === target.id" @click="confirmDraftSaved(target)">已确认保存草稿</button>
                  <button v-if="(target.status === 'pending_review' || target.status === 'unknown') && !target.options.save_as_draft" class="button button-secondary button-small" type="button" :disabled="busyTarget === target.id" @click="confirmPublished(target)">已确认发布</button>
                  <button v-if="target.status === 'pending_review' || target.status === 'unknown'" class="text-button video-cancel-action" type="button" :disabled="busyTarget === target.id" @click="confirmNotPublished(target)">{{ target.options.save_as_draft ? '确认未保存' : '确认未发布' }}</button>
                  <button v-if="target.status === 'failed'" class="button button-secondary button-small" type="button" :disabled="busyTarget === target.id || !item.media_available" :title="item.media_available ? undefined : '本机视频副本已清理，请重新选择原视频创建任务'" @click="retryTarget(target, item)">{{ busyTarget === target.id ? '提交中…' : '重试' }}</button>
                  <button v-if="target.status === 'queued' || target.status === 'running'" class="text-button video-cancel-action" type="button" :disabled="busyTarget === target.id" @click="cancelTarget(target)">停止任务</button>
                </div>
                <div v-if="retryEditorId === target.id" class="video-retry-schedule">
                  <strong>重新设置本账号的发布时间</strong>
                  <label><input v-model="retryMode" type="radio" value="immediate" /> 立即发布</label>
                  <label><input v-model="retryMode" type="radio" value="scheduled" /> 重新定时</label>
                  <input v-if="retryMode === 'scheduled'" v-model="retryScheduleAt" class="input" type="datetime-local" aria-label="重新选择发布时间" />
                  <small>{{ target.platform_id === 'baijiahao' ? '百家号定时发布时间需在 1 小时后至 7 天内。' : '抖音定时发布须在 2 小时后、14 天内。' }}</small>
                  <button class="button button-small" type="button" :disabled="!!busyTarget" @click="retryTarget(target, item, true)">确认重试</button>
                  <button class="button button-secondary button-small" type="button" @click="retryEditorId = ''">取消</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>

      <footer v-if="total > 0" class="video-pagination" aria-label="分页控制">
        <div class="video-pagination-left">
          <span>显示 {{ rangeStart }}–{{ rangeEnd }} 条，共 {{ total }} 条</span>
          <label>每页
            <select class="video-page-size" :value="pageSize" aria-label="每页条数" @change="changePageSize">
              <option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option>
            </select>
          </label>
        </div>
        <div class="video-pagination-right">
          <span>{{ page }} / {{ pageCount }} 页</span>
          <nav class="video-page-buttons" aria-label="分页">
            <button class="video-page-button" type="button" aria-label="上一页" :disabled="page <= 1 || loading" @click="goToPage(page - 1)">‹</button>
            <button v-for="pageNumber in pageNumbers" :key="pageNumber" class="video-page-button" :class="{ 'is-active': pageNumber === page }" type="button" :aria-label="`第 ${pageNumber} 页`" :aria-current="pageNumber === page ? 'page' : undefined" :disabled="loading" @click="goToPage(pageNumber)">{{ pageNumber }}</button>
            <button class="video-page-button" type="button" aria-label="下一页" :disabled="page >= pageCount || loading" @click="goToPage(page + 1)">›</button>
          </nav>
        </div>
      </footer>
      </template>

      <template v-else>
        <div class="video-draft-list-heading">
          <strong>本地草稿</strong>
          <span>{{ drafts.length }} 条</span>
        </div>
        <div v-if="draftLoadError" class="info-box form-error video-draft-error" role="alert">{{ draftLoadError }}</div>
        <div v-if="draftsLoading && !drafts.length" class="video-empty-state" role="status">正在读取本地草稿…</div>
        <div v-else-if="!draftLoadError && !drafts.length" class="video-empty-state">
          <span class="video-empty-mark" aria-hidden="true">▤</span>
          <h2>还没有本地草稿</h2>
          <p>在发布视频页面上传视频并填写内容后，可以先保存到本机，稍后再继续编辑。</p>
          <button class="button" type="button" @click="openNewTask">新建发布任务</button>
        </div>
        <div v-else-if="drafts.length" class="video-draft-list">
          <article v-for="draft in drafts" :key="draft.id" class="video-draft-card">
            <div class="video-draft-media-mark" aria-hidden="true">
              <video
                v-if="!failedDraftMediaIds.has(draft.id)"
                :src="api.videoPublishDraftMediaUrl(draft.id)"
                preload="metadata"
                muted
                playsinline
                tabindex="-1"
                @loadedmetadata="seekRepresentativeFrame"
                @error="failDraftMedia(draft.id)"
              />
              <span v-else>▶</span>
            </div>
            <div class="video-draft-main">
              <h2>{{ draft.title }}</h2>
              <p>{{ draft.media_name }} <span>·</span> {{ formatSize(draft.media_size) }}</p>
              <div class="video-draft-summary">
                <span>{{ draft.platforms.length ? draft.platforms.map(platformName).join('、') : '尚未选择平台' }}</span>
                <span v-if="draft.account_names.length">{{ draft.account_names.join('、') }}</span>
              </div>
            </div>
            <time class="video-draft-updated" :datetime="draft.updated_at">更新于 {{ formatDate(draft.updated_at) }}</time>
            <div class="video-draft-actions">
              <button class="button button-small" type="button" :disabled="!!deletingDraftId" @click="continueDraft(draft)">继续编辑</button>
              <button class="text-button video-delete-toggle" type="button" :disabled="!!deletingDraftId" @click="deleteDraft(draft)">{{ deletingDraftId === draft.id ? '删除中…' : '删除' }}</button>
            </div>
          </article>
        </div>
      </template>
    </section>
  </section>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本页的覆盖(全量落地时上移 styles.css)—— */
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }

.video-history-page { display: grid; gap: 10px; min-width: 0; }
.video-query-panel { min-height: 54px; padding: 9px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--paper); }
.video-query-entry, .video-query-actions { display: flex; align-items: flex-end; gap: 8px; }
.video-query-entry { min-width: 0; }
.video-query-actions { flex: 0 0 auto; }
.video-draft-toolbar { align-items: center; justify-content: space-between; }
.video-draft-toolbar p { margin: 0; color: var(--text-secondary); font-size: var(--fs-body-sm); }
.video-search { width: auto; }
.video-history-panel { overflow: hidden; padding: 0; border-radius: 6px; }
.video-history-tabs { display: flex; align-items: stretch; gap: 18px; min-height: 48px; padding: 0 13px; overflow-x: auto; border-bottom: 1px solid var(--line); }
.video-record-mode-tabs { gap: 22px; background: #fafbfd; }
.video-history-tab { display: inline-flex; align-items: center; gap: 6px; height: 47px; flex: 0 0 auto; padding: 0 2px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--muted); font-size: var(--fs-body); white-space: nowrap; cursor: pointer; }
.video-history-tab.is-active { border-bottom-color: var(--pine); color: var(--pine); font-weight: var(--fw-semibold); }
.video-history-tab-count { color: #929aa4; font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.video-history-tab.is-active .video-history-tab-count { color: var(--pine); }
.video-history-total { margin-left: auto; align-self: center; color: var(--text-tertiary); font-size: var(--fs-body-sm); white-space: nowrap; font-variant-numeric: tabular-nums; }
.video-table-scroll { overflow-x: auto; }
.video-history-table-head, .video-batch-row { display: grid; grid-template-columns: minmax(235px, 2.05fr) minmax(190px, 1.55fr) minmax(108px, .82fr) minmax(100px, .72fr) minmax(112px, .84fr) 104px; align-items: center; column-gap: 14px; min-width: 1010px; }
.video-history-table-head { min-height: 36px; padding: 0 15px; border-bottom: 1px solid var(--line); background: #f8f9fb; color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-batch-list { display: grid; }
.video-batch-card { border-bottom: 1px solid #e9ecf0; }
.video-batch-card:last-child { border-bottom: 0; }
.video-batch-row { min-height: 64px; padding: 9px 15px; transition: background .14s ease; }
.video-batch-card:hover .video-batch-row { background: #fafbfd; }
.video-media-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.video-media-mark { position: relative; display: grid; width: 39px; height: 52px; flex: 0 0 auto; place-items: center; overflow: hidden; border: 1px solid #e0e4e9; border-radius: 4px; background: #edf0f3; color: #536171; }
.video-media-mark img, .video-media-mark video { display: block; width: 100%; height: 100%; object-fit: cover; }
.video-media-mark .video-media-placeholder { display: grid; width: 18px; height: 18px; place-items: center; padding-left: 1px; border: 1px solid currentColor; border-radius: 50%; font-size: 12px; }
.video-media-copy { min-width: 0; }
.video-batch-title { overflow: hidden; margin: 0; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-batch-file { overflow: hidden; margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.video-batch-file-cleaned { color: #9b6b25; }
.video-batch-file span { padding: 0 3px; color: #a2aab3; }
.video-target-cell { display: grid; gap: 4px; min-width: 0; }
.video-target-cell > span { overflow: hidden; color: var(--text-secondary); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.video-target-cell small, .video-batch-progress small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.video-state { display: inline-flex; align-items: center; gap: 6px; color: #58616d; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.video-state::before { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: #9aa2ad; content: ''; }
.video-state-queued, .video-state-running { color: #486e81; }
.video-state-queued::before, .video-state-running::before { background: #477589; }
.video-state-pending_review, .video-state-unknown { color: #856126; }
.video-state-pending_review::before, .video-state-unknown::before { background: #9b6b25; }
.video-state-published, .video-state-draft_saved { color: #477257; }
.video-state-published::before, .video-state-draft_saved::before { background: #47765a; }
.video-state-failed, .video-state-partial { color: #a0524b; }
.video-state-failed::before, .video-state-partial::before { background: #ad514b; }
.video-state-cancelled { color: #777f89; }
.video-batch-progress { display: grid; gap: 3px; }
.video-batch-progress strong { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
.video-batch-date { color: var(--text-secondary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; white-space: nowrap; }
.video-row-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 3px 8px; min-width: 0; }
.video-detail-toggle { display: inline-flex; align-items: center; gap: 4px; padding: 5px 0 5px 6px; color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); white-space: nowrap; }
.video-detail-toggle span { font-size: var(--fs-body); }
.video-delete-toggle { color: var(--danger); font-size: var(--fs-body-sm); }
.video-row-actions .video-delete-toggle:disabled { color: var(--text-tertiary); cursor: not-allowed; text-decoration: none; }
.video-delete-help { flex-basis: 100%; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.4; text-align: right; }
.video-target-details { display: grid; padding: 0 15px 10px 64px; background: #fbfcfd; }
.video-batch-media-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid #eaedf1; }
.video-batch-media-actions p { margin: 0; color: #856126; font-size: var(--fs-caption); line-height: 1.5; }
.video-target-detail-row { display: grid; grid-template-columns: minmax(130px, 1fr) auto minmax(160px, 1.5fr) auto; align-items: center; gap: 12px; padding: 9px 0; border-top: 1px solid #eaedf1; }
.video-target-detail-name { display: grid; gap: 2px; min-width: 0; }
.video-target-messages { display: grid; gap: 4px; min-width: 0; }
.video-target-detail-name strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-target-detail-name span, .video-target-detail-row p { overflow: hidden; margin: 0; color: var(--text-tertiary); font-size: var(--fs-caption); text-overflow: ellipsis; }
.video-target-actions { display: flex; align-items: center; justify-content: flex-end; gap: 9px; }
.video-retry-schedule { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px; }
.video-retry-schedule small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.video-cancel-action { color: #9a5d51; }
.video-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 50px; padding: 0 14px; border-top: 1px solid var(--line); color: var(--text-secondary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.video-pagination-left, .video-pagination-right { display: flex; align-items: center; gap: 11px; }
.video-pagination-left label { display: flex; align-items: center; gap: 5px; }
.video-page-size { height: 30px; padding: 0 24px 0 8px; border: 1px solid var(--line); border-radius: 4px; background: white; color: var(--text-secondary); font-size: var(--fs-body-sm); }
.video-page-buttons { display: flex; align-items: center; gap: 3px; }
.video-page-button { display: grid; width: 30px; height: 30px; place-items: center; border: 1px solid transparent; border-radius: 4px; background: transparent; color: #57616d; font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; cursor: pointer; }
.video-page-button:hover:not(:disabled) { border-color: var(--line); background: #f8f9fb; }
.video-page-button.is-active { border-color: var(--pine); background: var(--pine); color: #fff; font-weight: var(--fw-semibold); }
.video-page-button:disabled { color: #c4c9cf; cursor: default; }
.video-empty-state { display: grid; justify-items: center; gap: 8px; padding: 58px 20px; color: var(--text-tertiary); text-align: center; }
.video-empty-state h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.video-empty-state p { max-width: 430px; margin: 0 0 7px; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.video-empty-mark { color: var(--pine-2); font-size: 29px; }
.video-draft-list-heading { display: flex; align-items: center; justify-content: space-between; min-height: 42px; padding: 0 15px; border-bottom: 1px solid var(--line); color: var(--text-secondary); font-size: var(--fs-body-sm); }
.video-draft-list-heading strong { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.video-draft-list-heading span { color: var(--text-tertiary); }
.video-draft-error { margin: 12px 14px 0; }
.video-draft-list { display: grid; }
.video-draft-card { display: grid; grid-template-columns: 39px minmax(200px, 1fr) minmax(130px, auto) auto; align-items: center; gap: 13px; min-height: 88px; padding: 12px 15px; border-bottom: 1px solid #e9ecf0; }
.video-draft-card:last-child { border-bottom: 0; }
.video-draft-card:hover { background: #fafbfd; }
.video-draft-media-mark { position: relative; display: grid; width: 39px; height: 52px; place-items: center; overflow: hidden; border: 1px solid #e0e4e9; border-radius: 4px; background: #edf0f3; color: #536171; }
.video-draft-media-mark video { display: block; width: 100%; height: 100%; object-fit: cover; }
.video-draft-media-mark span { display: grid; width: 21px; height: 21px; place-items: center; padding-left: 1px; border: 1px solid currentColor; border-radius: 50%; font-size: 12px; }
.video-draft-main { min-width: 0; }
.video-draft-main h2 { overflow: hidden; margin: 0; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-draft-main > p { overflow: hidden; margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.video-draft-main > p span { padding: 0 3px; }
.video-draft-summary { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 5px; color: var(--text-secondary); font-size: var(--fs-caption); }
.video-draft-summary span { overflow: hidden; max-width: 380px; text-overflow: ellipsis; white-space: nowrap; }
.video-draft-updated { color: var(--text-tertiary); font-size: var(--fs-caption); white-space: nowrap; }
.video-draft-actions { display: flex; align-items: center; gap: 10px; }
.video-draft-actions .video-delete-toggle:disabled { color: var(--text-tertiary); cursor: not-allowed; }
@media (max-width: 760px) {
  .video-query-panel { align-items: stretch; flex-direction: column; gap: 8px; }
  .video-query-entry { width: 100%; }
  .video-query-entry .video-search { width: auto; flex: 1; }
  .video-query-actions { justify-content: flex-end; }
  .video-draft-toolbar { align-items: flex-start; flex-direction: column; }
  .video-history-tabs { gap: 13px; }
  .video-history-total { display: none; }
  .video-table-scroll { overflow: visible; }
  .video-history-table-head { display: none; }
  .video-batch-row { grid-template-columns: minmax(0, 1fr) auto; gap: 7px 10px; min-width: 0; padding: 12px; }
  .video-media-cell { grid-column: 1; }
  .video-batch-row > .video-state { grid-column: 2; grid-row: 1; align-self: start; }
  .video-target-cell { grid-column: 1 / -1; padding-left: 49px; }
  .video-batch-progress { grid-column: 2; grid-row: 2; justify-items: end; }
  .video-batch-date { grid-column: 1; grid-row: 3; padding-left: 49px; }
  .video-row-actions { grid-column: 2; grid-row: 3; }
  .video-target-details { padding-left: 16px; }
  .video-target-detail-row { grid-template-columns: 1fr auto; }
  .video-target-messages { grid-column: 1 / -1; }
  .video-target-actions { grid-column: 1 / -1; justify-content: flex-start; }
  .video-pagination { align-items: flex-start; flex-direction: column; padding: 10px 12px; }
  .video-pagination-left, .video-pagination-right { width: 100%; justify-content: space-between; }
  .video-draft-card { grid-template-columns: 39px minmax(0, 1fr); gap: 9px 11px; padding: 12px; }
  .video-draft-media-mark { grid-column: 1; grid-row: 1 / span 2; width: 39px; height: 52px; }
  .video-draft-main { grid-column: 2; }
  .video-draft-updated { grid-column: 2; }
  .video-draft-actions { grid-column: 2; justify-content: flex-start; }
}
</style>
