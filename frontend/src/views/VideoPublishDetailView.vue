<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import type { VideoPublishBatch, VideoPublishTarget, VideoPublishTargetStatus } from '../api/types'
import { videoPublishUserMessage } from '../utils/videoPublishMessages'

const route = useRoute()
const router = useRouter()
const batch = ref<VideoPublishBatch | null>(null)
const selectedTargetId = ref('')
const loading = ref(false)
const busyTarget = ref('')
const cleaning = ref(false)
const deleting = ref(false)
const error = ref('')
const deleteError = ref('')
const notice = ref('')
const retryEditorId = ref('')
const retryMode = ref<'immediate' | 'scheduled'>('scheduled')
const retryScheduleAt = ref('')
let refreshTimer: number | undefined
let requestInFlight = false

const platformNames: Record<string, string> = {
  douyin: '抖音', xiaohongshu: '小红书', kuaishou: '快手', bilibili: '哔哩哔哩',
  wechat_channels: '微信视频号', baijiahao: '百家号', weibo: '微博',
}
const currentBatchId = computed(() => String(route.params.batchId || ''))
const selectedTarget = computed(() => batch.value?.targets.find((target) => target.id === selectedTargetId.value) ?? batch.value?.targets[0] ?? null)
const confirmedCount = computed(() => batch.value ? batch.value.summary.published + batch.value.summary.draft_saved : 0)
const totalTargets = computed(() => batch.value?.summary.total ?? 0)
const overallPercent = computed(() => totalTargets.value ? Math.round(confirmedCount.value * 100 / totalTargets.value) : 0)
const canDeleteBatch = computed(() => !!batch.value && batch.value.targets.length > 0 && batch.value.targets.every((target) => ['published', 'draft_saved', 'failed', 'cancelled'].includes(target.status)))
const canCleanBatchMedia = computed(() => !!batch.value?.media_available && canDeleteBatch.value)
const deleteBlockedReason = computed(() => {
  if (!batch.value) return ''
  if (batch.value.targets.some((target) => target.status === 'queued' || target.status === 'running')) {
    return '请先停止正在排队或发布中的平台任务。'
  }
  if (batch.value.targets.some((target) => target.status === 'pending_review' || target.status === 'unknown')) {
    return '请先核实并确认所有平台结果。'
  }
  return '没有可删除的平台发布目标。'
})

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '发布任务详情读取失败，请稍后重试。'
}

function deleteErrorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '删除视频发布任务失败，请稍后重试。'
}

function readablePublishMessage(target: VideoPublishTarget, message: string | null | undefined): string {
  if (target.platform_id === 'xiaohongshu' && message?.includes('小红书设置定时发布时间失败') && target.options.xhs_declaration === 'self_shot') {
    return '小红书定时设置失败；复现发现“自主拍摄”声明的弹窗可能阻挡操作。该声明需要拍摄地点和拍摄日期，当前工作台暂不支持填写。请先到小红书后台核实作品状态，再选择“核实未发布并修改”，调整内容声明。'
  }
  return videoPublishUserMessage(message)
}

function platformName(platformId: string): string {
  return platformNames[platformId] || platformId
}

function effectiveTitle(target: VideoPublishTarget, task: VideoPublishBatch): string {
  const title = hasTargetOverride(target, 'title') ? target.options.title : task.title
  return title?.trim() || '未填写标题'
}

function effectiveDescription(target: VideoPublishTarget, task: VideoPublishBatch): string {
  const description = hasTargetOverride(target, 'description') ? target.options.description : task.description
  return description?.trim() || '未填写简介'
}

function effectiveTags(target: VideoPublishTarget, task: VideoPublishBatch): string[] {
  return Object.prototype.hasOwnProperty.call(target.options, 'tags') ? target.options.tags ?? [] : task.tags
}

function hasTargetOverride(target: VideoPublishTarget, key: 'title' | 'description' | 'tags'): boolean {
  return Object.prototype.hasOwnProperty.call(target.options, key)
}

function editTarget(target: VideoPublishTarget): void {
  if (!batch.value || !batch.value.media_available || !['failed', 'cancelled'].includes(target.status)) return
  void router.push({
    name: 'video-publish-create',
    query: { edit_batch_id: batch.value.id, edit_target_id: target.id },
  })
}

function canOpenXhsPublishWindow(target: VideoPublishTarget): boolean {
  return target.platform_id === 'xiaohongshu' && ['unknown', 'failed'].includes(target.status) && !!batch.value?.media_available
}

function effectiveSchedule(target: VideoPublishTarget, task: VideoPublishBatch): string | null {
  return Object.prototype.hasOwnProperty.call(target.options, 'schedule_at_override')
    ? target.options.schedule_at_override ?? null
    : task.schedule_at
}

function platformSettings(target: VideoPublishTarget, task: VideoPublishBatch): Array<{ label: string; value: string }> {
  const options = target.options
  const settings: Array<{ label: string; value: string }> = []
  const add = (label: string, value: string | number | boolean | null | undefined, fallback = '未设置'): void => {
    settings.push({ label, value: value === null || value === undefined || value === '' ? fallback : String(value) })
  }
  const visibility = (value: string | undefined): string => ({
    public: '公开', friends: '好友可见', private: '仅自己可见',
  } as Record<string, string>)[value || ''] || '平台默认'
  const hasScheduleOverride = Object.prototype.hasOwnProperty.call(options, 'schedule_at_override')
  const schedule = effectiveSchedule(target, task)
  add('计划发布时间', schedule ? formatDate(schedule) : '立即发布')
  add('发布时间来源', hasScheduleOverride ? '该平台单独设置' : '通用设置')

  const covers: Array<[string, string | undefined]> = target.platform_id === 'douyin'
    ? [['竖版封面 · 3:4', options.cover_portrait_filename], ['横版封面 · 4:3', options.cover_landscape_filename]]
    : target.platform_id === 'bilibili'
      ? [['投稿封面 · 4:3', options.cover_4_3_filename || options.cover_filename], ['空间封面 · 16:9', options.cover_16_9_filename]]
      : target.platform_id === 'wechat_channels'
        ? [['竖版封面', options.cover_portrait_filename], ['横版封面', options.cover_landscape_filename]]
        : [['视频封面', options.cover_filename]]
  const availableCovers = covers.filter(([, filename]) => filename)
  if (availableCovers.length) {
    for (const [label, filename] of availableCovers) add(label, filename)
  } else {
    add('封面', undefined, '未单独设置')
  }

  if (target.platform_id === 'douyin') {
    add('自主声明', options.declaration)
    if (options.repost_source) add('转载来源', options.repost_source === 'inside' ? '抖音站内' : '站外')
    if (options.repost_title) add('站内原作品', options.repost_title)
    if (options.repost_id) add('原作品 ID', options.repost_id)
    if (options.location) add('发布地点', [options.location, options.location_address, options.location_city_name].filter(Boolean).join(' · '))
    if (options.location_id) add('地点 ID', options.location_id)
    add('可见范围', visibility(options.visibility))
    add('允许下载', typeof options.allow_download === 'boolean' ? (options.allow_download ? '允许' : '不允许') : '平台默认')
  } else if (target.platform_id === 'xiaohongshu') {
    add('原创声明', options.xhs_original ? '开启' : '未开启')
    add('封面比例', ({ portrait: '竖版 · 3:4', landscape: '横版 · 4:3' } as Record<string, string>)[options.cover_ratio || ''] || '未记录')
    add('内容类型声明', ({ fiction: '虚构演绎，仅供娱乐', ai: '笔记含 AI 合成内容', advertising: '内容包含营销广告', self_shot: '自主拍摄', repost: '来源转载' } as Record<string, string>)[options.xhs_declaration || ''] || '未添加')
    if (options.xhs_repost_source) add('转载媒体名称', options.xhs_repost_source)
    add('可见范围', visibility(options.xhs_visibility))
    if (options.xhs_location) add('发布地点', [options.xhs_location, options.xhs_location_address].filter(Boolean).join(' · '))
    if (options.xhs_location_id) add('地点 ID', options.xhs_location_id)
  } else if (target.platform_id === 'kuaishou') {
    add('封面比例', ({ original: '原始比例', landscape: '4:3', portrait: '3:4', square: '1:1', tall: '9:16' } as Record<string, string>)[options.cover_ratio || ''] || '未记录')
    add('合集', options.collection_name, '未选择合集')
  } else if (target.platform_id === 'wechat_channels') {
    add('短标题', options.short_title, '按平台规则整理通用标题')
    add('发布方式', options.save_as_draft ? '保存到草稿箱' : '发布作品')
    add('合集', options.collection_name, '未选择合集')
  } else if (target.platform_id === 'baijiahao') {
    add('创作声明', options.baijiahao_declaration, '未记录')
  } else if (target.platform_id === 'bilibili') {
    const categoryId = options.bilibili_category ?? options.tid
    add('投稿分区', options.bilibili_category_name ? `${options.bilibili_category_name}${categoryId ? `（ID ${categoryId}）` : ''}` : categoryId ? `分区 ID ${categoryId}` : undefined, '未记录')
    add('创作声明', options.bilibili_statement_name ? `${options.bilibili_statement_name}${options.bilibili_statement !== undefined ? `（ID ${options.bilibili_statement}）` : ''}` : options.bilibili_statement !== undefined ? `声明 ID ${options.bilibili_statement}` : undefined, options.bilibili_ai ? 'AI 内容声明' : '未记录')
    if (options.bilibili_source) add('转载来源', options.bilibili_source)
    if (typeof options.bilibili_no_reprint === 'boolean') add('禁止转载', options.bilibili_no_reprint ? '开启' : '关闭')
    add('可见范围', options.bilibili_private ? '仅自己可见' : '公开')
    if (options.bilibili_dynamic) add('粉丝动态', options.bilibili_dynamic)
  } else {
    add('平台设置', undefined, '该平台没有单独设置项')
  }

  return settings
}

function statusLabel(status: string, target?: VideoPublishTarget): string {
  if (target?.options.save_as_draft) {
    if (status === 'pending_review') return '草稿待确认'
    if (status === 'unknown') return '草稿结果未知'
    if (status === 'draft_saved' || status === 'published') return '草稿已保存'
  }
  return ({
    queued: '排队中', running: '发布中', pending_review: '已提交，待平台确认', published: '发布成功',
    draft_saved: '草稿已保存', failed: '失败', unknown: '结果未知', cancelled: '已取消', partial: '部分完成',
  } as Record<string, string>)[status] || status
}

function statusTone(status: string): string {
  if (status === 'published' || status === 'draft_saved') return 'is-success'
  if (status === 'running') return 'is-running'
  if (status === 'pending_review' || status === 'unknown') return 'is-waiting'
  if (status === 'failed') return 'is-failed'
  if (status === 'cancelled') return 'is-cancelled'
  return 'is-queued'
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatSize(value: number): string {
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

function displayProgress(target: VideoPublishTarget): number | null {
  if (target.status !== 'running' || target.progress === null || !Number.isFinite(target.progress) || target.progress <= 0 || target.progress >= 100) return null
  return Math.min(100, Math.max(0, Math.round(target.progress)))
}

function platformStatusLabel(value: string | null): string {
  return ({
    private: '作品存在 · 私密', present: '作品存在 · 状态待确认', reviewing: '审核中', published: '已发布',
    rejected: '审核未通过', restricted: '作品受限',
  } as Record<string, string>)[value || ''] || '尚未确认'
}

function safeRemoteUrl(value: string | null): string {
  if (!value) return ''
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch {
    return ''
  }
}

async function load(silent = false): Promise<void> {
  if (deleting.value || !currentBatchId.value || requestInFlight) return
  requestInFlight = true
  if (!silent) loading.value = true
  error.value = ''
  try {
    const result = await api.getVideoPublishBatch(currentBatchId.value)
    batch.value = result
    if (!result.targets.some((target) => target.id === selectedTargetId.value)) {
      selectedTargetId.value = result.targets[0]?.id || ''
    }
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) {
      batch.value = null
      selectedTargetId.value = ''
    }
    error.value = errorMessage(cause)
  } finally {
    requestInFlight = false
    loading.value = false
  }
}

async function refresh(): Promise<void> {
  notice.value = ''
  await load()
}

async function syncPlatform(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  busyTarget.value = target.id
  error.value = ''
  notice.value = ''
  try {
    const result = await api.syncVideoPublishTarget(target.id)
    notice.value = result.queued ? '已开始查询平台作品状态，结果会自动更新。' : '该账号正在查询，请稍候。'
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function confirmPublished(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || !window.confirm(`请先在「${platformName(target.platform_id)}」后台确认作品已发布，再标记为成功。`)) return
  await confirmResult(target, 'published')
}

async function confirmDraftSaved(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || !window.confirm('请先在微信视频号草稿箱确认这条视频已保存。')) return
  await confirmResult(target, 'draft_saved')
}

async function confirmNotPublished(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value) return
  const checkText = target.options.save_as_draft ? '草稿箱中没有这条视频草稿' : '没有这条作品'
  if (!window.confirm(`请先在「${platformName(target.platform_id)}」后台确认${checkText}。确认后可以再单独重试。`)) return
  await confirmResult(target, 'not_published')
}

async function confirmNotPublishedAndEdit(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || !batch.value || !batch.value.media_available) return
  const resultName = target.options.save_as_draft ? '草稿箱中没有本次视频草稿' : '作品和定时列表中没有本次视频'
  if (!window.confirm(`请先在「${platformName(target.platform_id)}」后台检查并确认${resultName}，再修改设置，避免重复提交。现在确认吗？`)) return
  busyTarget.value = target.id
  error.value = ''
  try {
    await api.confirmVideoPublishTarget(target.id, 'not_published', true)
    await router.push({ name: 'video-publish-create', query: { edit_batch_id: batch.value.id, edit_target_id: target.id } })
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function confirmResult(target: VideoPublishTarget, state: 'published' | 'draft_saved' | 'not_published'): Promise<void> {
  busyTarget.value = target.id
  error.value = ''
  try {
    await api.confirmVideoPublishTarget(target.id, state, true)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function retryTarget(target: VideoPublishTarget, withSchedule = false): Promise<void> {
  if (busyTarget.value || !batch.value) return
  const effectiveSchedule = Object.prototype.hasOwnProperty.call(target.options, 'schedule_at_override')
    ? target.options.schedule_at_override
    : batch.value.schedule_at
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
  error.value = ''
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

async function openXhsPublishWindow(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || deleting.value || cleaning.value || !canOpenXhsPublishWindow(target)) return
  const check = target.status === 'unknown'
    ? '请先在小红书创作中心确认作品和定时列表里没有这条视频。\n\n'
    : ''
  const prompt = `${check}工作台将使用原任务的设置重新上传视频。请在小红书窗口核对内容，由你点击「发布」或「定时发布」。\n\n现在打开发布窗口吗？`
  if (!window.confirm(prompt)) return
  busyTarget.value = target.id
  error.value = ''
  notice.value = ''
  try {
    await api.retryVideoPublishTarget(target.id, true, false, null, true)
    retryEditorId.value = ''
    notice.value = '已加入发布队列。视频上传后，请在小红书窗口核对内容并手动提交。'
    await load(true)
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '无法打开小红书发布窗口，请稍后重试。'
  } finally {
    busyTarget.value = ''
  }
}

async function cancelTarget(target: VideoPublishTarget): Promise<void> {
  if (busyTarget.value || !window.confirm('停止这个平台的发布进程？如果视频已经提交到平台，结果仍需人工核实。')) return
  busyTarget.value = target.id
  error.value = ''
  try {
    await api.cancelVideoPublishTarget(target.id)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyTarget.value = ''
  }
}

async function deleteBatch(): Promise<void> {
  if (!batch.value || !canDeleteBatch.value || deleting.value || cleaning.value || busyTarget.value || loading.value || requestInFlight) return
  const current = batch.value
  const confirmed = window.confirm(
    `确定删除「${current.title}」吗？\n\n本机发布记录、各平台结果、发布尝试历史，以及该任务专属的视频和封面副本都会删除。\n\n平台上的作品、发布账号和素材库源文件不会删除。`,
  )
  if (!confirmed) return
  deleting.value = true
  error.value = ''
  deleteError.value = ''
  try {
    await api.deleteVideoPublishBatch(current.id)
    batch.value = null
    selectedTargetId.value = ''
    stopRefresh()
    await router.push({ name: 'video-publish-history' })
  } catch (cause) {
    deleteError.value = deleteErrorMessage(cause)
  } finally {
    deleting.value = false
  }
}

async function cleanBatchMedia(): Promise<void> {
  if (!batch.value || !canCleanBatchMedia.value || cleaning.value || deleting.value || busyTarget.value) return
  const confirmed = window.confirm(`清理「${batch.value.title}」的任务封面及旧任务视频副本？发布历史和原创视频会保留；之后需要重新选择视频和封面创建新任务。`)
  if (!confirmed) return
  cleaning.value = true
  error.value = ''
  try {
    await api.cleanVideoPublishBatchMedia(batch.value.id)
    await load(true)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    cleaning.value = false
  }
}

onActivated(() => {
  void load()
  if (!refreshTimer) refreshTimer = window.setInterval(() => { void load(true) }, 4000)
})
function stopRefresh(): void {
  if (refreshTimer) window.clearInterval(refreshTimer)
  refreshTimer = undefined
}
onDeactivated(stopRefresh)
onBeforeUnmount(stopRefresh)
</script>

<template>
  <section class="publish-detail" aria-label="视频发布结果">
    <div v-if="error" class="info-box form-error detail-message" role="alert">{{ error }}</div>
    <div v-if="deleteError" class="info-box form-error detail-message" role="alert">{{ deleteError }}</div>
    <div v-if="notice" class="info-box detail-message" role="status">{{ notice }}</div>
    <div v-if="loading && !batch" class="panel detail-loading" role="status">正在读取发布任务…</div>
    <div v-else-if="!batch && !error" class="panel detail-loading">没有找到这条视频发布任务。</div>

    <template v-if="batch">
      <section class="panel detail-overview" aria-label="发布任务总体进度">
        <div class="detail-media">
          <div class="detail-media-mark" aria-hidden="true">▶</div>
          <div class="detail-media-copy">
            <strong>{{ batch.title }}</strong>
            <span>{{ batch.media_name }} · {{ formatSize(batch.media_size) }}</span>
            <span>{{ batch.targets.length }} 个平台账号</span>
          </div>
        </div>
        <div class="detail-overall">
          <div class="detail-overall-head"><span>总体确认进度</span><strong>{{ confirmedCount }} <small>/ {{ totalTargets }} 个账号已确认发布</small></strong></div>
          <div class="detail-progress-track" role="progressbar" aria-label="已确认发布的账号占比" :aria-valuenow="overallPercent" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${overallPercent}%` }"></span></div>
          <p>按已确认发布账号数计算；上传进度按平台回报单独显示。</p>
          <div class="detail-counts">
            <span>已发布 <strong>{{ batch.summary.published + batch.summary.draft_saved }}</strong></span>
            <span>处理中 <strong>{{ batch.summary.running }}</strong></span>
            <span>待确认 <strong>{{ batch.summary.pending_review + batch.summary.unknown }}</strong></span>
            <span>排队中 <strong>{{ batch.summary.queued }}</strong></span>
            <span>失败 <strong>{{ batch.summary.failed }}</strong></span>
          </div>
        </div>
        <div class="detail-batch-state">
          <span>当前任务状态</span>
          <strong><i class="detail-state-dot" :class="statusTone(batch.status)"></i>{{ statusLabel(batch.status) }}</strong>
          <small>每个账号独立执行与记录；待确认结果请查看对应平台回执。</small>
        </div>
      </section>

      <section class="panel detail-common-settings" aria-label="通用发布设置">
        <header class="detail-section-head detail-common-head">
          <h2>通用发布内容</h2>
          <span>各账号默认使用此内容；平台单独设置见右侧账号详情</span>
        </header>
        <div class="detail-common-grid">
          <div class="detail-common-field">
            <span>通用标题</span>
            <strong>{{ batch.title || '未填写' }}</strong>
          </div>
          <div class="detail-common-field detail-common-description">
            <span>通用简介</span>
            <p>{{ batch.description || '未填写通用简介' }}</p>
          </div>
          <div class="detail-common-field detail-common-tags">
            <span>通用标签</span>
            <div v-if="batch.tags.length" class="detail-tag-list"><span v-for="tag in batch.tags" :key="tag">{{ tag }}</span></div>
            <p v-else>未添加通用标签</p>
          </div>
          <div class="detail-common-field">
            <span>通用发布时间</span>
            <strong>{{ batch.schedule_at ? formatDate(batch.schedule_at) : '立即发布' }}</strong>
          </div>
        </div>
      </section>

      <div v-if="!batch.media_available" class="info-box detail-message" role="status">任务素材已清理或原视频不可用，发布历史仍保留；无法从此任务重试。</div>

      <div class="detail-columns">
        <section class="panel detail-targets" aria-label="平台账号进度">
          <header class="detail-section-head">
            <h2>平台账号进度 <small>{{ batch.targets.length }} 个发布目标</small></h2>
            <div class="detail-head-actions">
              <button class="button button-secondary button-small detail-danger-action" type="button" :disabled="!canDeleteBatch || deleting || cleaning || !!busyTarget || loading" :title="canDeleteBatch ? '删除本机发布记录和该任务副本' : deleteBlockedReason" @click="deleteBatch">{{ deleting ? '正在删除…' : '删除任务' }}</button>
              <button v-if="canCleanBatchMedia" class="button button-secondary button-small" type="button" :disabled="cleaning || deleting || !!busyTarget" @click="cleanBatchMedia">{{ cleaning ? '正在清理…' : '清理任务素材' }}</button>
              <button class="button button-secondary button-small" type="button" :disabled="loading || deleting" @click="refresh">{{ loading ? '刷新中…' : '↻ 刷新进度' }}</button>
            </div>
          </header>
          <p v-if="!canDeleteBatch" class="detail-delete-help" role="status">{{ deleteBlockedReason }}</p>
          <div class="detail-table-head" aria-hidden="true"><span>平台 / 账号</span><span>当前状态</span><span>进度 / 最新信息</span></div>
          <button
            v-for="target in batch.targets"
            :key="target.id"
            class="detail-target-row"
            :class="{ 'is-selected': selectedTarget?.id === target.id }"
            type="button"
            :aria-pressed="selectedTarget?.id === target.id"
            @click="selectedTargetId = target.id"
          >
            <span class="detail-account">
              <PlatformLogo class="detail-platform-mark" :platform-id="target.platform_id" :name="platformName(target.platform_id)" />
              <span><strong>{{ platformName(target.platform_id) }}</strong><small>{{ target.account_name || target.account_alias }}</small></span>
            </span>
            <span class="detail-target-status" :class="statusTone(target.status)"><i class="detail-state-dot"></i>{{ statusLabel(target.status, target) }}</span>
            <span class="detail-target-info">
              <template v-if="displayProgress(target) !== null">
                <span>视频上传中 · {{ displayProgress(target) }}%</span>
                <span class="detail-progress-track is-target" role="progressbar" :aria-label="`${platformName(target.platform_id)} 上传进度`" :aria-valuenow="displayProgress(target) ?? 0" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${displayProgress(target)}%` }"></span></span>
              </template>
              <template v-else-if="target.status === 'running'">
                <span>{{ target.diagnosis?.message || readablePublishMessage(target, target.message) || '正在上传视频，平台暂未返回进度。' }}</span>
                <span class="detail-progress-track is-target is-indeterminate" role="progressbar" :aria-label="`${platformName(target.platform_id)} 正在处理中`" aria-valuetext="正在处理"><span></span></span>
              </template>
              <template v-else>{{ readablePublishMessage(target, target.message) || '任务排队中，等待开始处理。' }}</template>
            </span>
            <span class="detail-row-link">{{ selectedTarget?.id === target.id ? '当前查看' : '查看详情 ›' }}</span>
          </button>
          <p class="detail-target-foot">仅在平台提供进度时显示百分比；否则显示当前处理信息。</p>
        </section>

        <aside v-if="selectedTarget" class="panel detail-inspector" aria-label="账号发布详情">
          <header class="detail-inspector-head"><h2>账号详情</h2><p>处理过程与平台回执</p></header>
          <div class="detail-inspector-body">
            <div class="detail-account-title">
              <div><strong>{{ platformName(selectedTarget.platform_id) }}</strong><small>{{ selectedTarget.account_name || selectedTarget.account_alias }}</small></div>
              <span class="detail-state-pill" :class="statusTone(selectedTarget.status)"><i class="detail-state-dot"></i>{{ statusLabel(selectedTarget.status, selectedTarget) }}</span>
            </div>

            <div v-if="selectedTarget.status === 'unknown' || selectedTarget.status === 'failed' || selectedTarget.status === 'cancelled'" class="detail-primary-action">
              <button
                v-if="selectedTarget.status === 'unknown'"
                class="button button-small"
                type="button"
                :disabled="!!busyTarget || !batch.media_available"
                :title="batch.media_available ? '先在平台核实未发布，再进入编辑' : '任务素材已清理或原视频不可用，无法编辑'"
                @click="confirmNotPublishedAndEdit(selectedTarget)"
              >{{ selectedTarget.options.save_as_draft ? '核实未保存并修改' : '核实未发布并修改' }}</button>
              <button
                v-else
                class="button button-small"
                type="button"
                :disabled="!!busyTarget || !batch.media_available"
                :title="batch.media_available ? '修改当前账号的发布内容和平台设置' : '任务素材已清理或原视频不可用，无法编辑'"
                @click="editTarget(selectedTarget)"
              >重新编辑</button>
              <button
                v-if="canOpenXhsPublishWindow(selectedTarget)"
                class="button button-secondary button-small"
                type="button"
                :disabled="!!busyTarget || deleting || cleaning"
                @click="openXhsPublishWindow(selectedTarget)"
              >打开小红书发布窗口</button>
              <small v-if="selectedTarget.status === 'unknown'">先检查平台作品和定时列表；确认没有本次内容后再继续。</small>
              <small v-else>只修改当前账号，其他账号的发布结果与处理记录保持不变。</small>
            </div>

            <section class="detail-info-section detail-platform-content">
              <h3>本平台发布内容</h3>
              <div class="detail-setting-row">
              <span>标题 <small>{{ hasTargetOverride(selectedTarget, 'title') ? '平台单独设置' : '沿用通用设置' }}</small></span>
                <strong>{{ effectiveTitle(selectedTarget, batch) }}</strong>
              </div>
              <div class="detail-setting-row is-multiline">
                <span>简介 <small>{{ hasTargetOverride(selectedTarget, 'description') ? '平台单独设置' : '沿用通用设置' }}</small></span>
                <p>{{ effectiveDescription(selectedTarget, batch) }}</p>
              </div>
              <div class="detail-setting-row is-multiline">
                <span>标签 <small>{{ hasTargetOverride(selectedTarget, 'tags') ? '平台单独设置' : '沿用通用设置' }}</small></span>
                <div v-if="effectiveTags(selectedTarget, batch).length" class="detail-tag-list"><span v-for="tag in effectiveTags(selectedTarget, batch)" :key="tag">{{ tag }}</span></div>
                <p v-else>未添加标签</p>
              </div>
            </section>

            <section class="detail-info-section detail-platform-settings">
              <h3>平台专属设置</h3>
              <div v-for="setting in platformSettings(selectedTarget, batch)" :key="setting.label" class="detail-setting-row">
                <span>{{ setting.label }}</span>
                <strong>{{ setting.value }}</strong>
              </div>
            </section>

            <section class="detail-info-section">
              <h3>发布信息</h3>
              <div><span>发布方式</span><strong>{{ effectiveSchedule(selectedTarget, batch) ? '定时发布' : '立即发布' }}</strong></div>
              <div><span>开始时间</span><strong>{{ formatDate(selectedTarget.started_at || selectedTarget.created_at) }}</strong></div>
              <div><span>最近更新</span><strong>{{ formatDate(selectedTarget.updated_at) }}</strong></div>
              <div><span>执行次数</span><strong>{{ selectedTarget.attempt_count }}</strong></div>
              <template v-if="selectedTarget.status === 'running'">
                <div><span>进程心跳</span><strong>{{ selectedTarget.heartbeat_at === undefined ? '重启工作台后启用' : selectedTarget.heartbeat_at ? formatDate(selectedTarget.heartbeat_at) : '等待首个心跳' }}</strong></div>
                <div><span>最近上传器输出</span><strong>{{ selectedTarget.last_output_at === undefined ? '重启工作台后启用' : selectedTarget.last_output_at ? formatDate(selectedTarget.last_output_at) : '尚无输出' }}</strong></div>
                <div><span>上传失败重试</span><strong>{{ selectedTarget.upload_retry_count === undefined ? '重启后启用' : `${selectedTarget.upload_retry_count} 次` }}</strong></div>
              </template>
            </section>

            <section class="detail-info-section">
              <h3>处理过程</h3>
              <div v-if="!selectedTarget.attempts?.length" class="detail-empty-note">{{ readablePublishMessage(selectedTarget, selectedTarget.message) || '任务已排队，等待开始处理。' }}<time>{{ formatDate(selectedTarget.created_at) }}</time></div>
              <ol v-else class="detail-attempt-list">
                <li v-for="attempt in [...selectedTarget.attempts].reverse()" :key="`${selectedTarget.id}-${attempt.attempt_number}`">
                  <i class="detail-attempt-dot" :class="statusTone(attempt.status)"></i>
                  <div><strong>第 {{ attempt.attempt_number }} 次处理 · {{ statusLabel(attempt.status, selectedTarget) }}</strong><p>{{ readablePublishMessage(selectedTarget, attempt.message) || (attempt.finished_at ? '本次处理已结束。' : '正在处理。') }}</p><time>{{ formatDate(attempt.finished_at || attempt.started_at) }}</time></div>
                </li>
              </ol>
              <div v-if="selectedTarget.status === 'running'" class="detail-live-message">{{ readablePublishMessage(selectedTarget, selectedTarget.message) || '正在准备发布…' }}</div>
              <div
                v-if="selectedTarget.diagnosis"
                class="detail-diagnostic"
                :class="`is-${selectedTarget.diagnosis.severity}`"
                :role="selectedTarget.diagnosis.severity === 'danger' ? 'alert' : 'status'"
              >
                <strong>上传诊断</strong>
                <p>{{ selectedTarget.diagnosis.message }}</p>
              </div>
            </section>

            <section class="detail-info-section detail-receipt-section">
              <h3>平台回执</h3>
              <div class="detail-receipt">
                <strong>{{ selectedTarget.platform_status ? platformStatusLabel(selectedTarget.platform_status) : statusLabel(selectedTarget.status, selectedTarget) }}</strong>
                <p>{{ readablePublishMessage(selectedTarget, selectedTarget.platform_message || selectedTarget.message) || (selectedTarget.status === 'pending_review' || selectedTarget.status === 'unknown' ? '平台已接收或结果待核实，请在平台后台确认。' : '平台尚未返回可核验的作品信息。') }}</p>
                <p v-if="selectedTarget.remote_id">作品 ID：{{ selectedTarget.remote_id }}</p>
                <time v-if="selectedTarget.platform_checked_at">最近查询：{{ formatDate(selectedTarget.platform_checked_at) }}</time>
                <a v-if="safeRemoteUrl(selectedTarget.remote_url)" :href="safeRemoteUrl(selectedTarget.remote_url)" target="_blank" rel="noopener noreferrer">打开平台作品 ↗</a>
              </div>
            </section>

            <div v-if="['pending_review', 'unknown', 'failed', 'cancelled', 'queued', 'running'].includes(selectedTarget.status) || selectedTarget.platform_id === 'douyin'" class="detail-actions">
              <button v-if="selectedTarget.platform_id === 'douyin' && !['queued', 'running', 'cancelled'].includes(selectedTarget.status)" class="button button-secondary button-small" type="button" :disabled="!!busyTarget" @click="syncPlatform(selectedTarget)">{{ busyTarget === selectedTarget.id ? '查询中…' : '查询抖音状态' }}</button>
              <template v-if="selectedTarget.status === 'pending_review' || selectedTarget.status === 'unknown'">
                <button v-if="selectedTarget.options.save_as_draft" class="button button-secondary button-small" type="button" :disabled="!!busyTarget" @click="confirmDraftSaved(selectedTarget)">已确认保存草稿</button>
                <button v-else class="button button-secondary button-small" type="button" :disabled="!!busyTarget" @click="confirmPublished(selectedTarget)">已确认发布</button>
                <button class="text-button detail-danger-action" type="button" :disabled="!!busyTarget" @click="confirmNotPublished(selectedTarget)">{{ selectedTarget.options.save_as_draft ? '确认未保存' : '确认未发布' }}</button>
              </template>
              <button v-if="selectedTarget.status === 'failed'" class="button button-secondary button-small" type="button" :disabled="!!busyTarget || !batch.media_available" :title="batch.media_available ? undefined : '任务素材已清理或原视频不可用，请重新选择视频创建任务'" @click="retryTarget(selectedTarget)">{{ busyTarget === selectedTarget.id ? '提交中…' : '重试该账号' }}</button>
              <button v-if="selectedTarget.status === 'queued' || selectedTarget.status === 'running'" class="text-button detail-danger-action" type="button" :disabled="busyTarget === selectedTarget.id" @click="cancelTarget(selectedTarget)">停止此账号任务</button>
            </div>

            <div v-if="retryEditorId === selectedTarget.id" class="detail-retry-editor">
              <strong>重新设置发布时间</strong>
              <label><input v-model="retryMode" type="radio" value="immediate" /> 立即发布</label>
              <label><input v-model="retryMode" type="radio" value="scheduled" /> 重新定时</label>
              <input v-if="retryMode === 'scheduled'" v-model="retryScheduleAt" class="input" type="datetime-local" aria-label="重新选择发布时间" />
              <small>{{ selectedTarget.platform_id === 'baijiahao' ? '百家号定时发布时间需在 1 小时后至 7 天内。' : '抖音定时发布须在 2 小时后、14 天内。' }}</small>
              <button class="button button-small" type="button" :disabled="!!busyTarget" @click="retryTarget(selectedTarget, true)">确认重试</button>
              <button class="button button-secondary button-small" type="button" @click="retryEditorId = ''">取消</button>
            </div>
          </div>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
.publish-detail { display: grid; gap: 14px; }
.detail-message { margin: 0; }
.detail-loading { padding: 30px; color: var(--muted); }
.detail-overview { display: grid; grid-template-columns: minmax(260px, 1.2fr) minmax(320px, 1.4fr) minmax(250px, 1fr); overflow: hidden; }
.detail-common-settings { overflow: hidden; }
.detail-common-head > span { color: var(--text-tertiary); font-size: var(--fs-caption); }
.detail-common-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px 24px; padding: 15px 18px; }
.detail-common-field { display: grid; align-content: start; gap: 6px; min-width: 0; }
.detail-common-field > span { color: var(--text-tertiary); font-size: var(--fs-caption); }
.detail-common-field > strong { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-medium); line-height: 1.6; overflow-wrap: anywhere; white-space: pre-wrap; }
.detail-common-field > p { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.65; overflow-wrap: anywhere; white-space: pre-wrap; }
.detail-common-description { grid-column: 1 / -1; }
.detail-tag-list { display: flex; flex-wrap: wrap; gap: 5px; }
.detail-tag-list > span { padding: 3px 8px; border: 1px solid #e4e9ef; border-radius: 3px; background: #f8fafc; color: var(--text-secondary); font-size: var(--fs-caption); line-height: 1.4; }
.detail-overview > div { min-width: 0; padding: 17px 19px; }
.detail-overview > div + div { border-left: 1px solid var(--line); }
.detail-media { display: flex; align-items: center; gap: 12px; }
.detail-media-mark { display: grid; width: 84px; height: 62px; flex: 0 0 auto; place-items: center; border: 1px solid var(--line); border-radius: 4px; background: #edf0f3; color: #71869b; font-size: 22px; }
.detail-media-copy { display: grid; gap: 5px; min-width: 0; }
.detail-media-copy strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.detail-media-copy span { overflow: hidden; color: var(--text-tertiary); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.detail-overall-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-overall-head > strong { color: var(--text-primary); font-size: var(--fs-title-2xl); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; white-space: nowrap; }
.detail-overall-head small { color: var(--text-tertiary); font-size: var(--fs-body-sm); font-weight: var(--fw-regular); }
.detail-progress-track { display: block; height: 7px; overflow: hidden; margin: 10px 0 7px; border-radius: 5px; background: #e9edf2; }
.detail-progress-track > span { display: block; height: 100%; border-radius: inherit; background: #2864e8; transition: width .2s ease; }
.detail-progress-track.is-indeterminate > span { width: 34%; animation: detail-indeterminate 1.5s ease-in-out infinite; }
@keyframes detail-indeterminate { from { transform: translateX(-110%); } to { transform: translateX(310%); } }
.detail-overall > p { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-counts { display: flex; flex-wrap: wrap; gap: 7px 13px; margin-top: 9px; color: var(--text-secondary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.detail-counts strong { margin-left: 3px; color: var(--ink); }
.detail-batch-state { display: grid; align-content: center; gap: 9px; }
.detail-batch-state > span { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-batch-state > strong { display: flex; align-items: center; gap: 8px; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.detail-batch-state > small { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.6; }
.detail-state-dot { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: currentColor; }
.detail-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(310px, 370px); gap: 14px; align-items: start; }
.detail-targets, .detail-inspector { min-width: 0; overflow: hidden; }
.detail-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 54px; padding: 10px 17px; border-bottom: 1px solid var(--line); }
.detail-section-head h2, .detail-inspector-head h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.detail-section-head h2 small { margin-left: 8px; color: var(--text-tertiary); font-size: var(--fs-caption); font-weight: var(--fw-regular); }
.detail-head-actions { display: flex; gap: 7px; }
.detail-delete-help { margin: 0; padding: 8px 17px; border-bottom: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.detail-table-head, .detail-target-row { display: grid; grid-template-columns: minmax(145px, 1fr) minmax(105px, .72fr) minmax(140px, 1.2fr) 74px; align-items: center; gap: 12px; }
.detail-table-head { min-height: 36px; padding: 0 17px; background: #f8f9fb; color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.detail-target-row { width: 100%; min-height: 75px; padding: 12px 17px; border: 0; border-bottom: 1px solid #edf0f4; background: #fff; color: inherit; text-align: left; cursor: pointer; }
.detail-target-row:hover { background: #fafcff; }
.detail-target-row.is-selected { background: #f2f7ff; box-shadow: inset 3px 0 #2864e8; }
.detail-account { display: flex; align-items: center; gap: 10px; min-width: 0; }
.detail-account > span:last-child { display: grid; gap: 4px; min-width: 0; }
.detail-account strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.detail-account small { overflow: hidden; color: var(--text-tertiary); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.detail-platform-mark { width: 31px; height: 31px; flex: 0 0 auto; border-radius: 5px; }
.detail-target-status { display: flex; align-items: center; gap: 6px; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.is-success { color: #267849; }
.is-running { color: #2864e8; }
.is-waiting { color: #a16b13; }
.is-failed { color: #b44745; }
.is-cancelled, .is-queued { color: #718194; }
.detail-target-info { display: grid; gap: 6px; min-width: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; overflow-wrap: anywhere; }
.detail-progress-track.is-target { height: 4px; margin: 0; }
.detail-row-link { color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); text-align: right; white-space: nowrap; }
.detail-target-foot { margin: 0; padding: 9px 17px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-inspector-head { padding: 15px 17px 12px; border-bottom: 1px solid var(--line); }
.detail-inspector-head p { margin: 6px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-inspector-body { padding: 15px 17px; }
.detail-account-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.detail-primary-action { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin: 11px 0 3px; padding: 10px; border: 1px solid #dce6f4; border-radius: 6px; background: #f5f8fe; }
.detail-primary-action small { flex: 1 1 180px; color: #60748a; font-size: 10px; line-height: 1.55; }
.detail-account-title > div { display: grid; gap: 5px; }
.detail-account-title strong { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.detail-account-title small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.detail-state-pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 3px; background: #f0f2f5; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.detail-state-pill.is-success { background: #eaf7ef; }
.detail-state-pill.is-running { background: #edf4ff; }
.detail-state-pill.is-waiting { background: #fff6e5; }
.detail-state-pill.is-failed { background: #fff0ef; }
.detail-info-section { margin-top: 17px; padding-top: 14px; border-top: 1px solid #edf0f4; }
.detail-info-section h3 { margin: 0 0 10px; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.detail-info-section > div:not(.detail-receipt) { display: flex; justify-content: space-between; gap: 12px; margin: 7px 0; font-size: var(--fs-body-sm); line-height: 1.55; }
.detail-info-section > div > span { flex: 0 0 auto; color: var(--muted); }
.detail-info-section > div > strong { color: var(--text-primary); font-weight: var(--fw-medium); text-align: right; overflow-wrap: anywhere; }
.detail-info-section > .detail-setting-row > span { display: grid; gap: 3px; }
.detail-setting-row > span small { color: var(--text-tertiary); font-size: var(--fs-caption); font-weight: var(--fw-regular); }
.detail-info-section > .detail-setting-row.is-multiline { flex-wrap: wrap; }
.detail-info-section > .detail-setting-row.is-multiline > p,
.detail-info-section > .detail-setting-row.is-multiline > .detail-tag-list { flex-basis: 100%; }
.detail-info-section > .detail-setting-row.is-multiline > p { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.65; overflow-wrap: anywhere; white-space: pre-wrap; }
.detail-info-section > .detail-setting-row.is-multiline > .detail-tag-list { justify-content: flex-start; }
.detail-attempt-list { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.detail-attempt-list li { position: relative; display: grid; grid-template-columns: 8px minmax(0, 1fr); gap: 9px; padding: 0 0 13px; }
.detail-attempt-list li::before { position: absolute; top: 6px; bottom: -1px; left: 3px; width: 1px; background: #dce4ed; content: ''; }
.detail-attempt-list li:last-child::before { display: none; }
.detail-attempt-dot { position: relative; z-index: 1; width: 7px; height: 7px; margin-top: 4px; border-radius: 50%; background: #8da2b9; }
.detail-attempt-dot.is-success { background: #31845c; }
.detail-attempt-dot.is-running { background: #2864e8; }
.detail-attempt-dot.is-failed { background: #b44745; }
.detail-attempt-list li > div { min-width: 0; }
.detail-attempt-list strong { color: var(--text-primary); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.detail-attempt-list p { margin: 4px 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.6; overflow-wrap: anywhere; }
.detail-attempt-list time, .detail-empty-note time { display: block; margin-top: 3px; color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.detail-empty-note { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.6; }
.detail-live-message { margin-top: 8px; padding: 8px; border-radius: 4px; background: #f4f7fb; color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: 1.6; }
.detail-diagnostic { margin-top: 8px; padding: 9px; border: 1px solid var(--warning); border-radius: 4px; background: var(--warning-soft); color: var(--text-secondary); }
.detail-diagnostic.is-danger { border-color: var(--danger); background: var(--danger-soft); }
.detail-diagnostic strong { color: var(--warning); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.detail-diagnostic.is-danger strong { color: var(--danger); }
.detail-diagnostic p { margin: 4px 0 0; font-size: var(--fs-body-sm); line-height: 1.6; overflow-wrap: anywhere; }
.detail-receipt { padding: 10px; border: 1px solid #e6ebf1; border-radius: 4px; background: #f8fafc; }
.detail-receipt strong { color: var(--text-primary); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.detail-receipt p { margin: 6px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.6; overflow-wrap: anywhere; }
.detail-receipt time { display: block; margin-top: 6px; color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.detail-receipt a { display: inline-block; margin-top: 7px; color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.detail-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-top: 12px; }
.detail-danger-action { color: var(--danger); font-size: var(--fs-body-sm); }
.detail-retry-editor { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; padding: 10px; border: 1px solid var(--line); border-radius: 4px; font-size: var(--fs-body-sm); }
.detail-retry-editor strong, .detail-retry-editor small, .detail-retry-editor .input { flex-basis: 100%; }
.detail-retry-editor small { color: var(--muted); }
@media (max-width: 1050px) {
  .detail-overview { grid-template-columns: 1fr 1.2fr; }
  .detail-overview > div:last-child { grid-column: 1 / -1; border-top: 1px solid var(--line); border-left: 0; }
  .detail-columns { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .detail-overview { grid-template-columns: 1fr; }
  .detail-overview > div + div { border-top: 1px solid var(--line); border-left: 0; }
  .detail-overview > div:last-child { grid-column: auto; }
  .detail-common-grid { grid-template-columns: 1fr; }
  .detail-common-head { align-items: flex-start; flex-direction: column; }
  .detail-common-description { grid-column: auto; }
  .detail-section-head { align-items: flex-start; }
  .detail-head-actions { flex-wrap: wrap; justify-content: flex-end; }
  .detail-table-head { display: none; }
  .detail-target-row { grid-template-columns: minmax(0, 1fr) auto; gap: 8px; padding: 12px; }
  .detail-account { grid-column: 1; }
  .detail-target-status { grid-column: 2; grid-row: 1; }
  .detail-target-info { grid-column: 1 / -1; padding-left: 41px; }
  .detail-row-link { grid-column: 2; grid-row: 2; }
}
@media (prefers-reduced-motion: reduce) {
  .detail-progress-track > span { transition: none; }
  .detail-progress-track.is-indeterminate > span { width: 100%; animation: none; opacity: .45; }
}
/* —— 排版系统 v2 试点:共享类在本页的覆盖(全量落地时上移 styles.css)—— */
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
</style>
