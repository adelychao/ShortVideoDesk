<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElAvatar, ElButton, ElCard, ElCheckbox, ElEmpty, ElIcon, ElImage, ElInput, ElMessage, ElMessageBox, ElOption, ElPopover, ElProgress, ElRadio, ElRadioGroup, ElSelect, ElTabPane, ElTabs, ElTag, ElUpload } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/icon/style/css'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/avatar/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/card/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/image/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/popover/style/css'
import 'element-plus/es/components/progress/style/css'
import 'element-plus/es/components/radio/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tag/style/css'
import 'element-plus/es/components/upload/style/css'
import { CircleCheck, Loading } from '@element-plus/icons-vue'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import LoginProgress from '../components/LoginProgress.vue'
import { videoAccountName } from '../videoAccountName'
import LocalMediaUpload from '../components/LocalMediaUpload.vue'
import VideoCoverCropEditor from '../components/VideoCoverCropEditor.vue'
import type { UploadReceipt, VideoPublishAccount, VideoPublishBatch, VideoPublishCoverUpload, VideoPublishDraftForm, VideoPublishMedia, VideoPublishPlatform, VideoPublishPoi, VideoPublishSauStatus, VideoPublishTarget, VideoPublishXhsPoi } from '../api/types'

type PlatformOverride = {
  enabled: boolean
  title: string
  description: string
  tags: string
  shortTitle: string
  collectionName: string
  saveAsDraft: boolean
  tid: string
  bilibiliStatement: string
  bilibiliSource: string
  bilibiliNoReprint: boolean
  bilibiliPrivate: boolean
  bilibiliDynamic: string
  declaration: string
  repostSource: '' | 'inside' | 'outside'
  visibility: '' | 'public' | 'friends' | 'private'
  allowDownload: boolean | null
  xhsOriginal: boolean
  xhsDeclaration: '' | 'fiction' | 'ai' | 'advertising' | 'self_shot' | 'repost'
  xhsRepostSource: string
  xhsVisibility: '' | 'public' | 'private' | 'friends'
}
type CoverVariant = 'portrait' | 'landscape'
type CoverRatioSlot = 'portrait' | 'landscape' | 'wide' | 'square' | 'tall' | 'original'
type CoverSlot = 'single' | CoverRatioSlot

const MAX_VIDEO_TAGS = 10
const ACCOUNT_CHECK_CACHE_TTL_MS = 5 * 60 * 1000
const router = useRouter()
const route = useRoute()
const sau = ref<VideoPublishSauStatus | null>(null)
const sauProjectPath = ref('')
const savingSau = ref(false)
const platforms = ref<VideoPublishPlatform[]>([])
const accounts = ref<VideoPublishAccount[]>([])
const bilibiliCategories = ref<{ id: number; name: string }[]>([])
const bilibiliStatements = ref<{ id: number; name: string }[]>([])
const bilibiliStatementsError = ref('')
const bilibiliStatementsLoading = ref(false)
let bilibiliStatementVersion = 0
const bilibiliCategoriesError = ref('')
const bilibiliCategoriesLoading = ref(false)
let bilibiliCategoryVersion = 0
const mediaItems = ref<VideoPublishMedia[]>([])
const editBatch = ref<VideoPublishBatch | null>(null)
const editTarget = ref<VideoPublishTarget | null>(null)
const editLoadError = ref('')
const editCoverFilenames = reactive<Record<string, string>>({})
const uploadReceipt = ref<UploadReceipt | null>(null)
const selectedMedia = ref<VideoPublishMedia | null>(null)
const title = ref('')
const description = ref('')
const tags = ref<string[]>([])
const tagDraft = ref('')
const tagInputError = ref('')
const scheduleAt = ref('')
const scheduleMode = ref<'immediate' | 'scheduled'>('immediate')
const platformSchedules = reactive<Record<string, string>>({})
const minimumScheduleAt = ref(nextScheduleMinute())
const selectedAccountIds = ref<string[]>([])
const pendingAccountPreflightIds = reactive(new Set<string>())
const accountCheckBusy = ref('')
const justConnectedIds = reactive(new Set<string>())
const justConnectedTimers = new Map<string, number>()
const activePlatformId = ref('')
const overrides = reactive<Record<string, PlatformOverride>>({})
const repostHistories = reactive<Record<string, { id: string; title: string }[]>>({})
const repostSelectedIds = reactive<Record<string, string>>({})
const repostHistoryBusy = ref('')
const poiQueries = reactive<Record<string, string>>({})
const poiResults = reactive<Record<string, VideoPublishPoi[]>>({})
const poiSelections = reactive<Record<string, VideoPublishPoi | null>>({})
const poiCities = reactive<Record<string, { code: string; name: string }[]>>({})
const poiCityCodes = reactive<Record<string, string>>({})
const poiBusy = reactive<Record<string, boolean>>({})
const poiErrors = reactive<Record<string, string>>({})
const poiSearched = reactive<Record<string, boolean>>({})
const poiVersions: Record<string, number> = {}
const poiTimers = new Map<string, number>()
const douyinTopicKeyword = ref('')
const douyinTopicResults = ref<import('../api/types').VideoPublishDouyinTopic[]>([])
const douyinTopicBusy = ref(false)
const douyinTopicError = ref('')
const douyinTopicSearched = ref(false)
const douyinTopicInput = ref<HTMLInputElement | null>(null)
const douyinTopicPopoverDismissed = ref(false)
let douyinTopicVersion = 0
let douyinTopicTimer: number | undefined
const xhsPoiQueries = reactive<Record<string, string>>({})
const xhsPoiResults = reactive<Record<string, VideoPublishXhsPoi[]>>({})
const xhsPoiSelections = reactive<Record<string, VideoPublishXhsPoi | null>>({})
const xhsPoiBusy = reactive<Record<string, boolean>>({})
const xhsPoiErrors = reactive<Record<string, string>>({})
const xhsPoiSearched = reactive<Record<string, boolean>>({})
const xhsPoiVersions: Record<string, number> = {}
const xhsPoiTimers = new Map<string, number>()
const coverUploads = reactive<Record<string, VideoPublishCoverUpload | null>>({})
const coverPreviews = reactive<Record<string, string>>({})
const coverDimensions = reactive<Record<string, { width: number; height: number } | null>>({})
const coverUploadRefs = reactive<Record<string, { clearFiles?: () => void } | null>>({})
const coverUploading = ref('')
const coverEditorOpen = ref(false)
const coverEditorKey = ref('')
const coverEditorRatio = ref(4 / 3)
const coverEditorImageSrc = ref('')
const coverEditorImageName = ref('')
const mediaDimensions = ref<{ width: number; height: number } | null>(null)
const mediaDimensionsError = ref(false)
const uploadedVideoUrl = ref('')
const uploadedVideoFirstFrame = ref('')
const uploadedVideoFirstFrameState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const xiaohongshuCoverRatio = ref<'portrait' | 'landscape'>('portrait')
const kuaishouCoverRatio = ref<'original' | 'landscape' | 'portrait' | 'square' | 'tall'>('original')
const accountBusy = ref('')
const accountCancelBusy = ref('')
const submitting = ref(false)
const savingDraft = ref(false)
const loading = ref(true)
const error = ref('')
const draftAccountWarning = ref('')
const activeDraftId = ref('')
const footerErrorAnimationKey = ref(0)
const qrRefreshKey = ref(Date.now())
const qrImageReady = reactive<Record<string, boolean>>({})
let accountPollTimer: number | undefined
let scheduleClockTimer: number | undefined
let videoDimensionRequest = 0
let uploadedVideoFirstFrameObjectUrl = ''
let initializedEditKey = ''
let initializedDraftId = ''

const editBatchId = computed(() => typeof route.query.edit_batch_id === 'string' ? route.query.edit_batch_id : '')
const editTargetId = computed(() => typeof route.query.edit_target_id === 'string' ? route.query.edit_target_id : '')
const draftIdFromRoute = computed(() => typeof route.query.draft_id === 'string' ? route.query.draft_id : '')
const isEditMode = computed(() => Boolean(editBatchId.value && editTargetId.value))
const selectedAccounts = computed(() => accounts.value.filter((account) => selectedAccountIds.value.includes(account.id) && account.status === 'connected'))
const selectedPlatforms = computed(() => [...new Set(selectedAccounts.value.map((account) => account.platform_id))])
const showDouyinTopicPopover = computed(() => !douyinTopicPopoverDismissed.value && selectedAccounts.value.some(account => account.platform_id === 'douyin') && Boolean(douyinTopicKeyword.value) && (douyinTopicBusy.value || Boolean(douyinTopicError.value) || douyinTopicSearched.value))
const activePlatform = computed(() => visiblePlatforms.value.find((platform) => platform.id === activePlatformId.value) || visiblePlatforms.value[0] || null)
const allowDownloadRadioValue = computed<string | boolean>(() => {
  const value = activePlatform.value ? overrideFor(activePlatform.value.id).allowDownload : null
  return value === null ? 'default' : value
})
const editTargetReady = computed(() => Boolean(
  isEditMode.value
  && editBatch.value?.media_available
  && editTarget.value
  && ['failed', 'cancelled'].includes(editTarget.value.status)
  && selectedAccounts.value.some((account) => account.id === editTarget.value?.account_id),
))
const visiblePlatforms = computed(() => isEditMode.value && editTarget.value
  ? platforms.value.filter((platform) => platform.id === editTarget.value?.platform_id)
  : platforms.value)

async function loadBilibiliCategories(accountId: string): Promise<void> {
  const version = ++bilibiliCategoryVersion
  bilibiliCategories.value = []
  bilibiliCategoriesError.value = ''
  if (!accountId) { bilibiliCategoriesLoading.value = false; return }
  bilibiliCategoriesLoading.value = true
  try {
    const result = await api.getBilibiliCategories(accountId)
    if (version === bilibiliCategoryVersion) {
      bilibiliCategories.value = result.items
      const remembered = localStorage.getItem(`bilibili-category:${accountId}`) || ''
      const savedCategory = editTarget.value?.platform_id === 'bilibili' ? editTarget.value.options.bilibili_category ?? editTarget.value.options.tid : undefined
      const savedCategoryName = editTarget.value?.options.bilibili_category_name || '已保存的分区'
      if (isEditMode.value && savedCategory) {
        const savedId = String(savedCategory)
        if (!bilibiliCategories.value.some(item => String(item.id) === savedId)) bilibiliCategories.value.push({ id: savedCategory, name: savedCategoryName })
        overrideFor('bilibili').tid = savedId
      } else if (activeDraftId.value && overrideFor('bilibili').tid) {
        const savedId = overrideFor('bilibili').tid
        if (!bilibiliCategories.value.some(item => String(item.id) === savedId)) bilibiliCategories.value.push({ id: Number(savedId), name: '草稿中保存的分区' })
      } else {
        overrideFor('bilibili').tid = result.items.some(item => String(item.id) === remembered) ? remembered : ''
      }
    }
  } catch (cause) {
    if (version === bilibiliCategoryVersion) bilibiliCategoriesError.value = safeError(cause)
  } finally {
    if (version === bilibiliCategoryVersion) bilibiliCategoriesLoading.value = false
  }
}

async function loadBilibiliStatements(accountId: string): Promise<void> {
  const version = ++bilibiliStatementVersion
  bilibiliStatements.value = []
  bilibiliStatementsError.value = ''
  if (!isEditMode.value && !activeDraftId.value) overrideFor('bilibili').bilibiliStatement = ''
  if (!accountId) { bilibiliStatementsLoading.value = false; return }
  bilibiliStatementsLoading.value = true
  try {
    const result = await api.getBilibiliStatements(accountId)
    if (version === bilibiliStatementVersion) {
      bilibiliStatements.value = result.items
      const savedStatement = editTarget.value?.platform_id === 'bilibili' ? editTarget.value.options.bilibili_statement : undefined
      if (isEditMode.value && savedStatement !== undefined) {
        const savedId = String(savedStatement)
        const savedName = editTarget.value?.options.bilibili_statement_name || '已保存的创作声明'
        if (!bilibiliStatements.value.some(item => String(item.id) === savedId)) bilibiliStatements.value.push({ id: savedStatement, name: savedName })
        overrideFor('bilibili').bilibiliStatement = savedId
      } else if (activeDraftId.value && overrideFor('bilibili').bilibiliStatement) {
        const savedId = overrideFor('bilibili').bilibiliStatement
        if (!bilibiliStatements.value.some(item => String(item.id) === savedId)) bilibiliStatements.value.push({ id: Number(savedId), name: '草稿中保存的创作声明' })
      }
    }
  } catch (cause) {
    if (version === bilibiliStatementVersion) bilibiliStatementsError.value = safeError(cause)
  } finally {
    if (version === bilibiliStatementVersion) bilibiliStatementsLoading.value = false
  }
}

watch(() => selectedAccounts.value.find(account => account.platform_id === 'bilibili')?.id || '', id => { void loadBilibiliCategories(id); void loadBilibiliStatements(id) })
watch(() => selectedAccounts.value.some(account => account.platform_id === 'douyin'), enabled => {
  if (enabled) queueDouyinTopicSearch()
  else clearDouyinTopicSearch()
})
watch(() => overrideFor('bilibili').tid, value => {
  const account = selectedAccounts.value.find(item => item.platform_id === 'bilibili')
  if (account && bilibiliCategories.value.some(item => String(item.id) === value)) localStorage.setItem(`bilibili-category:${account.id}`, value)
})
const activeMediaName = computed(() => uploadReceipt.value?.filename || selectedMedia.value?.name || (isEditMode.value ? editBatch.value?.media_name : '') || '')
const activeMediaSize = computed(() => uploadReceipt.value?.sizeBytes || selectedMedia.value?.size_bytes || (isEditMode.value ? editBatch.value?.media_size : 0) || 0)
const activeMediaIsVideo = computed(() => isEditMode.value ? editTargetReady.value : uploadReceipt.value ? uploadReceipt.value.detectedMediaType === 'video' : Boolean(selectedMedia.value))
const hasPublishMedia = computed(() => isEditMode.value ? editTargetReady.value : uploadReceipt.value?.detectedMediaType === 'video' || Boolean(selectedMedia.value))
const activeMediaVideoSource = computed(() => {
  if (uploadReceipt.value?.detectedMediaType === 'video') return uploadedVideoUrl.value
  if (selectedMedia.value?.artifact_id) return `/api/v1/artifacts/${encodeURIComponent(selectedMedia.value.artifact_id)}/file?inline=1`
  return ''
})
const activeVideoRatio = computed(() => mediaDimensions.value ? mediaDimensions.value.width / mediaDimensions.value.height : null)
const activeVideoRatioLabel = computed(() => mediaDimensions.value ? coverAspectLabel(mediaDimensions.value) : !activeMediaVideoSource.value ? '尚未选择视频' : mediaDimensionsError.value ? '无法读取视频比例' : '读取视频比例中…')
const commonCoverSlots = computed<CoverRatioSlot[]>(() => [
  'portrait', 'landscape', 'wide', 'square', 'tall', ...(mediaDimensions.value ? ['original' as const] : []),
])

function clearUploadedVideoFirstFrame(): void {
  if (uploadedVideoFirstFrameObjectUrl) URL.revokeObjectURL(uploadedVideoFirstFrameObjectUrl)
  uploadedVideoFirstFrameObjectUrl = ''
  uploadedVideoFirstFrame.value = ''
  uploadedVideoFirstFrameState.value = 'idle'
}

function captureUploadedVideoFirstFrame(video: HTMLVideoElement, requestId: number): void {
  const maxDimension = 480
  const scale = Math.min(1, maxDimension / Math.max(video.videoWidth, video.videoHeight))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
  canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
  const context = canvas.getContext('2d')
  if (!context) {
    uploadedVideoFirstFrameState.value = 'error'
    return
  }
  try {
    context.drawImage(video, 0, 0, canvas.width, canvas.height)
    canvas.toBlob((blob) => {
      if (requestId !== videoDimensionRequest || uploadReceipt.value?.detectedMediaType !== 'video') return
      if (!blob) {
        uploadedVideoFirstFrameState.value = 'error'
        return
      }
      const nextUrl = URL.createObjectURL(blob)
      if (uploadedVideoFirstFrameObjectUrl) URL.revokeObjectURL(uploadedVideoFirstFrameObjectUrl)
      uploadedVideoFirstFrameObjectUrl = nextUrl
      uploadedVideoFirstFrame.value = nextUrl
      uploadedVideoFirstFrameState.value = 'ready'
    }, 'image/jpeg', 0.84)
  } catch {
    if (requestId === videoDimensionRequest) {
      uploadedVideoFirstFrameState.value = 'error'
    }
  }
}

watch(activeMediaVideoSource, (source, previousSource, onCleanup) => {
  if (previousSource && previousSource !== source) {
    void removeCover(commonCoverKey('original'))
    void removeCover(coverKey('kuaishou', 'original'))
  }
  if (previousSource !== source) clearUploadedVideoFirstFrame()
  const requestId = ++videoDimensionRequest
  mediaDimensions.value = null
  mediaDimensionsError.value = false
  if (!source) return
  const uploadedVideo = uploadReceipt.value?.detectedMediaType === 'video'
  if (uploadedVideo) uploadedVideoFirstFrameState.value = 'loading'
  const probe = document.createElement('video')
  probe.preload = 'metadata'
  probe.muted = true
  probe.playsInline = true
  let frameHandled = false
  let probeReleased = false
  let frameCaptureScheduled = false
  let frameCallbackId: number | null = null
  let frameFallbackTimer: number | undefined
  const releaseProbe = () => {
    if (probeReleased) return
    probeReleased = true
    if (frameFallbackTimer !== undefined) window.clearTimeout(frameFallbackTimer)
    if (frameCallbackId !== null) probe.cancelVideoFrameCallback(frameCallbackId)
    probe.onloadedmetadata = null
    probe.onloadeddata = null
    probe.onerror = null
    probe.pause()
    probe.removeAttribute('src')
    probe.load()
  }
  onCleanup(releaseProbe)
  const capturePresentedFrame = () => {
    if (frameCaptureScheduled || probeReleased) return
    frameCaptureScheduled = true
    if (frameFallbackTimer !== undefined) window.clearTimeout(frameFallbackTimer)
    if (frameCallbackId !== null) probe.cancelVideoFrameCallback(frameCallbackId)
    frameCallbackId = null
    if (requestId !== videoDimensionRequest) {
      releaseProbe()
      return
    }
    if (!probe.videoWidth || !probe.videoHeight) {
      if (uploadedVideo) uploadedVideoFirstFrameState.value = 'error'
      releaseProbe()
      return
    }
    if (uploadedVideo) captureUploadedVideoFirstFrame(probe, requestId)
    releaseProbe()
  }
  const captureAfterAnimationFrames = () => {
    if (frameCaptureScheduled || probeReleased) return
    if (frameFallbackTimer !== undefined) window.clearTimeout(frameFallbackTimer)
    if (frameCallbackId !== null) probe.cancelVideoFrameCallback(frameCallbackId)
    frameCallbackId = null
    requestAnimationFrame(() => requestAnimationFrame(capturePresentedFrame))
  }
  const handleLoadedData = () => {
    if (frameHandled) return
    frameHandled = true
    if (requestId !== videoDimensionRequest) {
      releaseProbe()
      return
    }
    if (!probe.videoWidth || !probe.videoHeight) {
      if (uploadedVideo) uploadedVideoFirstFrameState.value = 'error'
      releaseProbe()
      return
    }
    if (!uploadedVideo) {
      releaseProbe()
      return
    }
    if (typeof probe.requestVideoFrameCallback === 'function') {
      frameCallbackId = probe.requestVideoFrameCallback(() => {
        frameCallbackId = null
        capturePresentedFrame()
      })
      frameFallbackTimer = window.setTimeout(captureAfterAnimationFrames, 1200)
      void probe.play().catch(captureAfterAnimationFrames)
      return
    }
    captureAfterAnimationFrames()
  }
  probe.onloadedmetadata = () => {
    if (requestId === videoDimensionRequest && probe.videoWidth > 0 && probe.videoHeight > 0) {
      mediaDimensions.value = { width: probe.videoWidth, height: probe.videoHeight }
    }
    if (probe.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) handleLoadedData()
  }
  probe.onloadeddata = handleLoadedData
  probe.onerror = () => {
    if (requestId === videoDimensionRequest) {
      mediaDimensions.value = null
      mediaDimensionsError.value = true
      if (uploadedVideo) uploadedVideoFirstFrameState.value = 'error'
    }
    releaseProbe()
  }
  probe.src = source
  probe.load()
}, { immediate: true })
const unsupportedSchedules = computed(() => selectedPlatforms.value.filter((id) => !platforms.value.find((item) => item.id === id)?.schedule))
const tagLimitHint = computed(() => {
  const selected = selectedPlatforms.value
    .map(id => platforms.value.find(platform => platform.id === id))
    .filter((platform): platform is VideoPublishPlatform => Boolean(platform))
  if (!selected.length) return '提交时会按所选平台的标签上限，依录入顺序保留前面的标签。'
  const limits = selected.map(platform => `${platform.name}前 ${platform.max_tags} 个`).join('、')
  return `将自动按平台截取：${limits}；超出部分不会发送。`
})
const missingBilibiliTid = computed(() => {
  const value = overrideFor('bilibili').tid.trim()
  const tid = Number(value)
  return selectedPlatforms.value.includes('bilibili') && (!value || !Number.isInteger(tid) || !bilibiliCategories.value.some(item => item.id === tid))
})
const readyAccountCount = computed(() => selectedAccounts.value.length)
const accountStatusLabel: Record<string, string> = {
  not_connected: '未登录', login_in_progress: '等待网页登录', checking: '正在检查', connected: '已连接', expired: '登录失效', error: '检查未完成',
}
function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '视频发布页面请求失败，请稍后重试。'
}

function showStatusMessage(text: string, type: 'success' | 'info' | 'warning' = 'success'): void {
  ElMessage({ message: text, type, duration: 3000, grouping: true })
}

async function confirmWithElementPlus(message: string, title: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      type: 'warning',
      distinguishCancelAndClose: true,
    })
    return true
  } catch {
    return false
  }
}

function platformName(id: string): string {
  return platforms.value.find((item) => item.id === id)?.name || id
}

function platformAccounts(platformId: string): VideoPublishAccount[] {
  return accounts.value.filter((account) => account.platform_id === platformId)
}

function hasRecentSuccessfulAccountCheck(account: VideoPublishAccount): boolean {
  if (account.status !== 'connected' || !account.checked_at) return false
  if (/本次检查未完成|无法确认登录状态|检查账号登录状态超时/.test(account.message)) return false
  const checkedAt = Date.parse(account.checked_at.replace(/(\.\d{3})\d+/, '$1'))
  const ageMs = Date.now() - checkedAt
  return Number.isFinite(checkedAt) && ageMs >= 0 && ageMs <= ACCOUNT_CHECK_CACHE_TTL_MS
}

function platformSelectionCount(platformId: string): number {
  return selectedAccounts.value.filter((account) => account.platform_id === platformId).length
}

function platformAccountSummary(platformId: string): string {
  const items = platformAccounts(platformId)
  const connectedCount = items.filter((account) => account.status === 'connected').length
  const selectedCount = platformSelectionCount(platformId)
  if (selectedCount) return `${selectedCount} 个已选`
  if (connectedCount) return `${connectedCount} 个已连接`
  return items.length ? '账号待登录' : '未添加账号'
}

function overrideFor(platformId: string): PlatformOverride {
  if (!overrides[platformId]) {
    overrides[platformId] = {
      enabled: false,
      title: '',
      description: '',
      tags: '',
      shortTitle: '',
      collectionName: '',
      saveAsDraft: false,
      tid: '',
      bilibiliStatement: '',
      bilibiliSource: '',
      bilibiliNoReprint: false,
      bilibiliPrivate: false,
      bilibiliDynamic: '',
      declaration: '无需添加自主声明',
      repostSource: '',
      visibility: '',
      allowDownload: null,
      xhsOriginal: false,
      xhsDeclaration: '',
      xhsRepostSource: '',
      xhsVisibility: '',
    }
  }
  return overrides[platformId]
}

function hasPlatformSchedule(platformId: string): boolean {
  return Object.prototype.hasOwnProperty.call(platformSchedules, platformId)
}

function platformSchedule(platformId: string): string {
  return hasPlatformSchedule(platformId) ? platformSchedules[platformId] : scheduleAt.value
}

function setPlatformSchedule(platformId: string, event: Event): void {
  platformSchedules[platformId] = (event.target as HTMLInputElement).value
}

function nextScheduleMinute(): string {
  const next = new Date(Date.now() + 60_000)
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}T${pad(next.getHours())}:${pad(next.getMinutes())}`
}

function refreshScheduleMinimum(event?: FocusEvent | MouseEvent): void {
  minimumScheduleAt.value = nextScheduleMinute()
  if (event?.currentTarget instanceof HTMLInputElement) event.currentTarget.min = minimumScheduleAt.value
}

function showDateTimePicker(event: MouseEvent): void {
  const input = event.currentTarget as HTMLInputElement
  refreshScheduleMinimum(event)
  if (typeof input.showPicker === 'function') {
    try { input.showPicker() } catch { input.focus() }
  }
}

function readCoverDimensionsFromUrl(url: string): Promise<{ width: number; height: number } | null> {
  return new Promise((resolve) => {
    const image = new Image()
    image.onload = () => {
      const dimensions = image.naturalWidth > 0 && image.naturalHeight > 0
        ? { width: image.naturalWidth, height: image.naturalHeight }
        : null
      resolve(dimensions)
    }
    image.onerror = () => {
      resolve(null)
    }
    image.src = url
  })
}

async function readCoverDimensions(file: File): Promise<{ width: number; height: number } | null> {
  const url = URL.createObjectURL(file)
  try {
    return await readCoverDimensionsFromUrl(url)
  } finally {
    URL.revokeObjectURL(url)
  }
}

function aspectMatches(dimensions: { width: number; height: number }, expected: number, tolerance = 0.08): boolean {
  const actual = dimensions.width / dimensions.height
  return Number.isFinite(actual) && Math.abs(actual - expected) / expected <= tolerance
}

function coverAspectLabel(dimensions: { width: number; height: number } | null): string {
  if (!dimensions) return '无法读取图片比例'
  const known: Array<[number, string]> = [[3 / 4, '3:4'], [4 / 3, '4:3'], [16 / 9, '16:9'], [1, '1:1'], [9 / 16, '9:16']]
  const expected = known.find(([ratio]) => aspectMatches(dimensions, ratio, 0.025))
  return `${dimensions.width} × ${dimensions.height} · ${expected?.[1] || `${(dimensions.width / dimensions.height).toFixed(2)}:1`}`
}

function coverKey(platformId: string, slot: CoverSlot): string {
  return `${platformId}:${slot}`
}

function commonCoverKey(slot: CoverRatioSlot): string {
  return `common:${slot}`
}

function setCoverUploadRef(key: string, element: unknown): void {
  coverUploadRefs[key] = element as { clearFiles?: () => void } | null
}

function onCoverUploadChange(key: string, uploadFile: { raw?: File }): void {
  const file = uploadFile.raw
  coverUploadRefs[key]?.clearFiles?.()
  if (file) void uploadCoverTo(key, file)
}

function slotAspectRatio(slot: CoverSlot, platformId = ''): number {
  if (slot === 'portrait') return 3 / 4
  if (slot === 'landscape') return 4 / 3
  if (slot === 'wide') return 16 / 9
  if (slot === 'square') return 1
  if (slot === 'tall') return 9 / 16
  if (slot === 'original') return activeVideoRatio.value || 16 / 9
  if (platformId === 'weibo') return 4 / 3
  return 16 / 9
}

function slotRatioLabel(slot: CoverSlot): string {
  if (slot === 'original') return mediaDimensions.value ? `原始 ${activeVideoRatioLabel.value}` : '原始比例'
  if (slot === 'portrait') return '3:4'
  if (slot === 'landscape') return '4:3'
  if (slot === 'wide') return '16:9'
  if (slot === 'square') return '1:1'
  if (slot === 'tall') return '9:16'
  return '封面'
}

function commonCoverTitle(slot: CoverRatioSlot): string {
  if (slot === 'portrait') return '竖版封面'
  if (slot === 'landscape') return '横版封面'
  if (slot === 'wide') return '宽屏封面'
  if (slot === 'square') return '方形封面'
  if (slot === 'tall') return '竖屏封面'
  return '视频原始比例'
}

function coverSlots(platformId: string): CoverSlot[] {
  if (platformId === 'baijiahao') return ['portrait', 'wide']
  if (platformId === 'douyin') return ['portrait', 'landscape']
  if (platformId === 'bilibili') return ['landscape', 'wide']
  if (platformId === 'kuaishou') return [kuaishouCoverRatio.value]
  if (platformId === 'xiaohongshu') return [xiaohongshuCoverRatio.value]
  if (platformId === 'wechat_channels') {
    return mediaDimensions.value && mediaDimensions.value.height > mediaDimensions.value.width
      ? ['portrait']
      : ['portrait', 'landscape']
  }
  return ['single']
}

function inheritedCommonCoverSlot(platformId: string, slot: CoverSlot): CoverRatioSlot | null {
  if (slot === 'single') {
    if (platformId === 'baijiahao') {
      return (['landscape', 'wide', 'original'] as const).find((candidate) => {
        const dimensions = coverDimensions[commonCoverKey(candidate)]
        return Boolean(coverUploads[commonCoverKey(candidate)] && dimensions && dimensions.width > dimensions.height)
      }) || null
    }
    if (platformId === 'weibo') {
      return (['portrait', 'wide', 'landscape'] as const).find((candidate) => {
        const dimensions = coverDimensions[commonCoverKey(candidate)]
        return Boolean(coverUploads[commonCoverKey(candidate)] && dimensions && [3 / 4, 16 / 9, 4 / 3].some((ratio) => aspectMatches(dimensions, ratio, 0.015)))
      }) || null
    }
    return coverUploads[commonCoverKey('original')] ? 'original' : null
  }
  const expected = slotAspectRatio(slot, platformId)
  const candidates: CoverRatioSlot[] = [slot, ...commonCoverSlots.value.filter((candidate) => candidate !== slot)]
  return candidates.find((candidate) => {
    const key = commonCoverKey(candidate)
    const dimensions = coverDimensions[key]
    return Boolean(coverUploads[key] && dimensions && aspectMatches(dimensions, expected, 0.015))
  }) || null
}

function coverUploadFor(platformId: string, slot: CoverSlot): VideoPublishCoverUpload | null {
  const own = ownCoverUploadFor(platformId, slot)
  if (own) return own
  const commonSlot = inheritedCommonCoverSlot(platformId, slot)
  return commonSlot ? coverUploads[commonCoverKey(commonSlot)] || null : null
}

function ownCoverUploadFor(platformId: string, slot: CoverSlot): VideoPublishCoverUpload | null {
  return coverUploads[coverKey(platformId, slot)] || null
}

function coverPreviewFor(platformId: string, slot: CoverSlot): string {
  const key = coverKey(platformId, slot)
  if (ownCoverUploadFor(platformId, slot)) return coverPreviews[key] || ''
  const commonSlot = inheritedCommonCoverSlot(platformId, slot)
  if (commonSlot) {
    const commonPreview = coverPreviews[commonCoverKey(commonSlot)]
    if (commonPreview) return commonPreview
  }
  const savedCover = editCoverFilenames[key]
  return isEditMode.value && savedCover && editBatch.value
    ? api.videoPublishBatchCoverUrl(editBatch.value.id, savedCover)
    : ''
}

function coverSlotTitle(platformId: string, slot: CoverSlot): string {
  if (slot === 'portrait') return '竖版封面 · 3:4'
  if (slot === 'landscape') return platformId === 'bilibili' ? '首页推荐封面 · 4:3' : '横版封面 · 4:3'
  if (slot === 'wide') return platformId === 'bilibili' ? '个人空间封面 · 16:9' : '横版封面 · 16:9'
  if (slot === 'square') return '方形封面 · 1:1'
  if (slot === 'tall') return '竖屏封面 · 9:16'
  if (slot === 'original') return '视频原始比例封面'
  return platformId === 'baijiahao' ? '横版视频封面' : '视频封面'
}

function coverEditorTitle(): string {
  const [platformId, slotName = 'single'] = coverEditorKey.value.split(':')
  const slot = slotName as CoverSlot
  return platformId === 'common'
    ? `通用封面 · ${slotRatioLabel(slot)}`
    : `${platformName(platformId)} · ${coverSlotTitle(platformId, slot)}`
}

function coverSlotRatio(slot: CoverSlot): string {
  return slotRatioLabel(slot)
}

function coverUploadLabel(platformId: string, slot: CoverSlot): string {
  const ownCover = ownCoverUploadFor(platformId, slot)
  if (ownCover) return `${ownCover.filename} · ${formatSize(ownCover.size_bytes)}`
  const inherited = coverUploadFor(platformId, slot)
  if (inherited) {
    const commonSlot = inheritedCommonCoverSlot(platformId, slot)
    if (!commonSlot) return '尚未设置；可从图片或视频帧裁剪'
    return `继承左侧 ${slotRatioLabel(commonSlot)}：${inherited.filename}`
  }
  const savedCover = editCoverFilenames[coverKey(platformId, slot)]
  if (savedCover) return ''
  return '未设置；可从图片或视频帧裁剪'
}

function hasEffectiveCover(platformId: string, slot: CoverSlot): boolean {
  return Boolean(coverUploadFor(platformId, slot) || (isEditMode.value && editCoverFilenames[coverKey(platformId, slot)]))
}

function coverHelp(platformId: string): string {
  const help: Record<string, string> = {
    douyin: '抖音需要 3:4 竖封面和 4:3 横封面。左侧相同比例的封面会自动继承，也可以单独裁剪覆盖。',
    xiaohongshu: '可在 4:3 和 3:4 中选择一个比例；平台侧未按视频方向自动锁定比例，因此这里由你手动选择。',
    kuaishou: '从原始比例、4:3、3:4、1:1、9:16 中任选一种。左侧有相同比例封面时自动继承。',
    bilibili: '需要同时设置 4:3 与 16:9 两张封面。工作台会保存两张；当前 SAU 发布仍只接收 4:3 封面。',
    wechat_channels: mediaDimensions.value && mediaDimensions.value.height > mediaDimensions.value.width
      ? '竖版视频只设置 3:4 封面。'
      : '横版视频需要同时设置 3:4 和 4:3 两张封面。左侧同尺寸封面会自动继承。',
    baijiahao: '需要同时设置 3:4 和 16:9 两张封面；左侧相同比例的通用封面会自动继承。',
    weibo: '微博竖版封面为 3:4，横版可用 16:9 或 4:3；封面不超过 5 MB，仅继承比例匹配的左侧封面。',
  }
  return help[platformId] || '可使用左侧通用封面，也可单独上传当前平台封面。'
}

function hasCommonCover(): boolean {
  return commonCoverSlots.value.some((slot) => Boolean(coverUploads[commonCoverKey(slot)]))
}

async function uploadCoverTo(key: string, file?: File, variant?: CoverVariant): Promise<void> {
  if (!file) return
  error.value = ''
  if (!/\.(jpe?g|png|webp)$/i.test(file.name)) {
    error.value = '封面仅支持 JPG、PNG 或 WEBP 图片。'
    return
  }
  const platformId = key.split(':', 1)[0]
  const maxBytes = platformId === 'weibo' ? 5 * 1024 * 1024 : 20 * 1024 * 1024
  if (file.size > maxBytes) {
    error.value = platformId === 'weibo' ? '微博视频封面不能超过 5 MB。' : '封面图片不能超过 20 MB。'
    return
  }
  const dimensions = await readCoverDimensions(file)
  if (!dimensions) {
    error.value = '无法读取封面图片尺寸，请换一张有效图片。'
    return
  }
  if (variant && !aspectMatches(dimensions, slotAspectRatio(variant))) {
    error.value = `请上传接近 ${slotRatioLabel(variant)} 的封面。`
    return
  }
  if (platformId === 'baijiahao' && dimensions.width <= dimensions.height) {
    error.value = '百家号封面需要使用横版图片。'
    return
  }
  if (platformId === 'weibo' && ![3 / 4, 16 / 9, 4 / 3].some(ratio => aspectMatches(dimensions, ratio))) {
    error.value = '微博封面请使用 3:4、16:9 或 4:3 比例。'
    return
  }
  coverUploading.value = key
  try {
    const uploaded = await api.uploadVideoPublishCover(file)
    const previous = coverUploads[key]
    const previousPreview = coverPreviews[key]
    coverUploads[key] = uploaded
    coverPreviews[key] = URL.createObjectURL(file)
    coverDimensions[key] = dimensions
    if (previousPreview) URL.revokeObjectURL(previousPreview)
    if (previous) await api.deleteVideoPublishCover(previous.upload_id).catch(() => undefined)
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    coverUploading.value = ''
  }
}

async function storeCroppedCover(key: string, file: File): Promise<boolean> {
  error.value = ''
  const dimensions = await readCoverDimensions(file)
  if (!dimensions) {
    error.value = '无法读取裁剪后的封面尺寸，请重新裁剪。'
    return false
  }
  const platformId = key.split(':', 1)[0]
  const slotName = key.split(':')[1] as CoverSlot | undefined
  if (slotName && slotName !== 'single' && !aspectMatches(dimensions, slotAspectRatio(slotName, platformId), 0.015)) {
    error.value = `裁剪结果应为 ${slotRatioLabel(slotName)}，请重新调整封面。`
    return false
  }
  const maxBytes = platformId === 'weibo' ? 5 * 1024 * 1024 : 20 * 1024 * 1024
  if (file.size > maxBytes) {
    error.value = platformId === 'weibo' ? '微博视频封面不能超过 5 MB。' : '封面图片不能超过 20 MB。'
    return false
  }
  coverUploading.value = key
  try {
    const uploaded = await api.uploadVideoPublishCover(file)
    const previous = coverUploads[key]
    const previousPreview = coverPreviews[key]
    coverUploads[key] = uploaded
    coverPreviews[key] = URL.createObjectURL(file)
    coverDimensions[key] = dimensions
    if (previousPreview) URL.revokeObjectURL(previousPreview)
    if (previous) await api.deleteVideoPublishCover(previous.upload_id).catch(() => undefined)
    return true
  } catch (cause) {
    error.value = safeError(cause)
    return false
  } finally {
    coverUploading.value = ''
  }
}

function openCoverEditor(key: string, slot: CoverSlot, platformId = 'common'): void {
  coverEditorKey.value = key
  coverEditorRatio.value = slotAspectRatio(slot, platformId)
  const [keyPlatformId, keySlot = 'single'] = key.split(':')
  const ownUpload = coverUploads[key]
  let imageSrc = ownUpload ? coverPreviews[key] || '' : ''
  let imageName = ownUpload?.filename || ''
  if (!imageSrc && keyPlatformId !== 'common') {
    const savedCover = editCoverFilenames[key]
    if (isEditMode.value && savedCover && editBatch.value) {
      imageSrc = api.videoPublishBatchCoverUrl(editBatch.value.id, savedCover)
      imageName = savedCover
    } else {
      const inheritedSlot = inheritedCommonCoverSlot(keyPlatformId, keySlot as CoverSlot)
      if (inheritedSlot) {
        const commonKey = commonCoverKey(inheritedSlot)
        imageSrc = coverPreviews[commonKey] || ''
        imageName = coverUploads[commonKey]?.filename || ''
      }
    }
  }
  coverEditorImageSrc.value = imageSrc
  coverEditorImageName.value = imageName
  coverEditorOpen.value = true
}

async function saveEditedCover(file: File): Promise<void> {
  const saved = await storeCroppedCover(coverEditorKey.value, file)
  if (saved) coverEditorOpen.value = false
}

async function removeCover(key: string): Promise<void> {
  const previous = coverUploads[key]
  const preview = coverPreviews[key]
  coverUploads[key] = null
  coverPreviews[key] = ''
  coverDimensions[key] = null
  if (preview) URL.revokeObjectURL(preview)
  if (previous) await api.deleteVideoPublishCover(previous.upload_id).catch(() => undefined)
}

function splitTags(value: string): string[] {
  return value.split(/[，,\n]/).map((item) => item.trim().replace(/^#+/, '')).filter(Boolean).slice(0, MAX_VIDEO_TAGS)
}

function limitTagInput(value: string): string {
  let tagCount = 0
  const retained: string[] = []
  for (const part of value.split(/([，,\n])/)) {
    if (/^[，,\n]$/.test(part)) {
      retained.push(part)
      continue
    }
    if (part.trim() && ++tagCount > MAX_VIDEO_TAGS) break
    retained.push(part)
  }
  return retained.join('')
}

function addCommonTags(values: string[]): void {
  const existing = new Set(tags.value.map(value => value.toLocaleLowerCase('zh-CN')))
  const additions: string[] = []
  let exceeded = false
  for (const rawValue of values) {
    const value = rawValue.trim().replace(/^#+/, '').trim()
    if (!value) continue
    const key = value.toLocaleLowerCase('zh-CN')
    if (existing.has(key)) continue
    if (tags.value.length + additions.length >= MAX_VIDEO_TAGS) {
      exceeded = true
      break
    }
    existing.add(key)
    additions.push(value)
  }
  if (additions.length) tags.value = [...tags.value, ...additions]
  tagInputError.value = exceeded ? `最多填写 ${MAX_VIDEO_TAGS} 个标签。` : ''
}

function commitTagDraft(): void {
  addCommonTags(tagDraft.value.split(/[，,\n]/))
  tagDraft.value = ''
  clearDouyinTopicSearch()
}

function handleTagInputKeydown(event: Event): void {
  const keyboardEvent = event as KeyboardEvent
  if (keyboardEvent.key === 'Enter' && !keyboardEvent.isComposing && keyboardEvent.keyCode !== 229) {
    keyboardEvent.preventDefault()
    commitTagDraft()
    return
  }
  if (keyboardEvent.key === 'Escape') dismissDouyinTopicPopover()
  if (keyboardEvent.key === 'Backspace' && !tagDraft.value && tags.value.length) tags.value = tags.value.slice(0, -1)
}

function handleCommonTagsInput(value: string): void {
  tagDraft.value = value
  tagInputError.value = ''
  queueDouyinTopicSearch()
}

function reopenDouyinTopicPopover(): void {
  douyinTopicPopoverDismissed.value = false
}

function dismissDouyinTopicPopover(): void {
  douyinTopicPopoverDismissed.value = true
}

function handleDouyinTopicOutsidePointerDown(event: PointerEvent): void {
  if (!showDouyinTopicPopover.value || !(event.target instanceof Element)) return
  if (event.target.closest('.video-tag-input, .douyin-topic-popper')) return
  dismissDouyinTopicPopover()
}

function handleDouyinTopicGlobalKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && showDouyinTopicPopover.value) dismissDouyinTopicPopover()
}

function setDouyinTopicInput(element: unknown): void {
  const root = (element as { $el?: HTMLElement } | null)?.$el
  douyinTopicInput.value = root?.querySelector('input') ?? null
}

function removeCommonTag(index: number): void {
  tags.value = tags.value.filter((_, tagIndex) => tagIndex !== index)
  tagInputError.value = ''
}

function handlePlatformTagsInput(platformId: string): void {
  const override = overrideFor(platformId)
  override.tags = limitTagInput(override.tags)
}

function currentDouyinTopicKeyword(): string {
  const parts = tagDraft.value.split(/[，,\n]/)
  return parts[parts.length - 1]?.trim().replace(/^#+/, '').trim() || ''
}

function clearDouyinTopicSearch(): void {
  douyinTopicVersion += 1
  if (douyinTopicTimer !== undefined) window.clearTimeout(douyinTopicTimer)
  douyinTopicTimer = undefined
  douyinTopicKeyword.value = ''
  douyinTopicResults.value = []
  douyinTopicError.value = ''
  douyinTopicSearched.value = false
}

function queueDouyinTopicSearch(): void {
  douyinTopicVersion += 1
  const version = douyinTopicVersion
  douyinTopicPopoverDismissed.value = false
  if (douyinTopicTimer !== undefined) window.clearTimeout(douyinTopicTimer)
  douyinTopicKeyword.value = currentDouyinTopicKeyword()
  douyinTopicResults.value = []
  douyinTopicError.value = ''
  douyinTopicSearched.value = false
  if (!selectedAccounts.value.some(account => account.platform_id === 'douyin') || !douyinTopicKeyword.value) return
  douyinTopicTimer = window.setTimeout(() => { void runDouyinTopicSearch(version, douyinTopicKeyword.value) }, 500)
}

async function runDouyinTopicSearch(version: number, keyword: string): Promise<void> {
  if (douyinTopicBusy.value || version !== douyinTopicVersion || !keyword) return
  douyinTopicBusy.value = true
  douyinTopicError.value = ''
  try {
    const result = await api.searchDouyinTopics(keyword)
    if (version !== douyinTopicVersion) return
    douyinTopicResults.value = result.items
    douyinTopicSearched.value = true
  } catch (cause) {
    if (version === douyinTopicVersion) douyinTopicError.value = safeError(cause)
  } finally {
    douyinTopicBusy.value = false
    if (version !== douyinTopicVersion && douyinTopicKeyword.value && selectedAccounts.value.some(account => account.platform_id === 'douyin')) {
      douyinTopicTimer = window.setTimeout(() => { void runDouyinTopicSearch(douyinTopicVersion, douyinTopicKeyword.value) }, 80)
    }
  }
}

function chooseDouyinTopic(topic: import('../api/types').VideoPublishDouyinTopic): void {
  const parts = tagDraft.value.split(/[，,\n]/)
  parts[parts.length - 1] = topic.name
  addCommonTags(parts)
  tagDraft.value = ''
  clearDouyinTopicSearch()
}

function formatSize(bytes: number): string {
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

async function clearTemporaryDraftUploads(): Promise<void> {
  const mediaUploadId = uploadReceipt.value?.sourceType === 'local' ? undefined : uploadReceipt.value?.uploadId
  const coverUploadIds = [...new Set(Object.values(coverUploads).flatMap((cover) => cover ? [cover.upload_id] : []))]
  uploadReceipt.value = null
  if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
  uploadedVideoUrl.value = ''
  clearUploadedVideoFirstFrame()
  for (const key of Object.keys(coverUploads)) delete coverUploads[key]
  for (const key of Object.keys(coverPreviews)) {
    if (coverPreviews[key]) URL.revokeObjectURL(coverPreviews[key])
    delete coverPreviews[key]
  }
  for (const key of Object.keys(coverDimensions)) delete coverDimensions[key]
  const cleanup = [
    ...(mediaUploadId ? [api.deleteUpload(mediaUploadId).catch(() => undefined)] : []),
    ...coverUploadIds.map((id) => api.deleteVideoPublishCover(id).catch(() => undefined)),
  ]
  await Promise.all(cleanup)
}

function clearDraftFormState(): void {
  title.value = ''
  description.value = ''
  tags.value = []
  tagDraft.value = ''
  tagInputError.value = ''
  scheduleMode.value = 'immediate'
  scheduleAt.value = ''
  selectedAccountIds.value = []
  activePlatformId.value = platforms.value[0]?.id || ''
  draftAccountWarning.value = ''
  for (const key of Object.keys(platformSchedules)) delete platformSchedules[key]
  for (const key of Object.keys(overrides)) delete overrides[key]
  for (const key of Object.keys(poiSelections)) delete poiSelections[key]
  for (const key of Object.keys(poiQueries)) delete poiQueries[key]
  for (const key of Object.keys(poiCityCodes)) delete poiCityCodes[key]
  for (const key of Object.keys(xhsPoiSelections)) delete xhsPoiSelections[key]
  for (const key of Object.keys(xhsPoiQueries)) delete xhsPoiQueries[key]
  for (const key of Object.keys(repostHistories)) delete repostHistories[key]
  for (const key of Object.keys(repostSelectedIds)) delete repostSelectedIds[key]
  kuaishouCoverRatio.value = 'original'
  xiaohongshuCoverRatio.value = 'portrait'
  selectedMedia.value = null
  mediaDimensions.value = null
}

async function initializeDraftForm(draftId: string): Promise<void> {
  const restored = await api.restoreVideoPublishDraft(draftId)
  const form = restored.draft.form
  activeDraftId.value = draftId
  draftAccountWarning.value = ''
  const availableAccountIds = new Set(accounts.value.filter((account) => account.status === 'connected').map((account) => account.id))
  const savedAccountIds = Array.isArray(form.selected_account_ids) ? form.selected_account_ids : []
  selectedAccountIds.value = savedAccountIds.filter((id) => availableAccountIds.has(id))
  const unavailableCount = savedAccountIds.length - selectedAccountIds.value.length
  if (unavailableCount > 0) {
    draftAccountWarning.value = `草稿中有 ${unavailableCount} 个账号目前未连接或已移除，已暂不勾选；重新连接后可再选择。`
  }

  title.value = form.title || ''
  description.value = form.description || ''
  tags.value = Array.isArray(form.tags) ? [...form.tags] : []
  tagDraft.value = form.tag_draft || ''
  tagInputError.value = ''
  scheduleMode.value = form.schedule_mode === 'scheduled' ? 'scheduled' : 'immediate'
  scheduleAt.value = form.schedule_at || ''
  for (const key of Object.keys(platformSchedules)) delete platformSchedules[key]
  Object.assign(platformSchedules, form.platform_schedules || {})
  const requestedActivePlatform = form.active_platform_id || ''
  activePlatformId.value = platforms.value.some((platform) => platform.id === requestedActivePlatform)
    ? requestedActivePlatform
    : platforms.value[0]?.id || ''

  for (const key of Object.keys(overrides)) delete overrides[key]
  for (const [platformId, savedOverride] of Object.entries(form.overrides || {})) {
    if (!platforms.value.some((platform) => platform.id === platformId) || !savedOverride || typeof savedOverride !== 'object') continue
    Object.assign(overrideFor(platformId), savedOverride)
  }
  for (const record of [poiSelections, poiQueries, poiCityCodes, xhsPoiSelections, xhsPoiQueries, repostHistories, repostSelectedIds]) {
    for (const key of Object.keys(record)) delete record[key]
  }
  Object.assign(poiSelections, form.poi_selections || {})
  Object.assign(poiQueries, form.poi_queries || {})
  Object.assign(poiCityCodes, form.poi_city_codes || {})
  Object.assign(xhsPoiSelections, form.xhs_poi_selections || {})
  Object.assign(xhsPoiQueries, form.xhs_poi_queries || {})
  for (const [accountId, work] of Object.entries(form.repost_selections || {})) {
    if (!work?.id || !work.title) continue
    repostHistories[accountId] = [{ id: work.id, title: work.title }]
    repostSelectedIds[accountId] = work.id
  }
  const coverPreferences = form.cover_preferences || { kuaishou: 'original', xiaohongshu: 'portrait' }
  if (['original', 'landscape', 'portrait', 'square', 'tall'].includes(coverPreferences.kuaishou)) {
    kuaishouCoverRatio.value = coverPreferences.kuaishou as typeof kuaishouCoverRatio.value
  }
  if (coverPreferences.xiaohongshu === 'landscape' || coverPreferences.xiaohongshu === 'portrait') {
    xiaohongshuCoverRatio.value = coverPreferences.xiaohongshu
  }

  uploadedVideoUrl.value = restored.media_upload ? (restored.media_upload.previewUrl || api.videoPublishDraftMediaUrl(draftId)) : ''
  uploadReceipt.value = restored.media_upload
  if (!restored.media_upload) error.value = '原视频已移动、删除或发生变化，请重新选择视频后保存草稿。'
  selectedMedia.value = null
  const covers = Object.entries(restored.cover_uploads || {})
  await Promise.all(covers.map(async ([slot, receipt]) => {
    coverUploads[slot] = receipt
    coverPreviews[slot] = receipt.preview_url || api.videoPublishDraftCoverUrl(draftId, slot)
    coverDimensions[slot] = await readCoverDimensionsFromUrl(coverPreviews[slot])
  }))
}

async function loadPage(): Promise<void> {
  loading.value = true
  error.value = ''
  // 编辑历史素材任务时仍读取素材库元数据；新建任务暂时只走本地上传。
  const mediaRequest = isEditMode.value
    ? api.listVideoPublishMedia()
    : Promise.resolve({ items: [] as VideoPublishMedia[] })
  const [statusResult, platformResult, accountResult, mediaResult] = await Promise.allSettled([
    api.getVideoPublishStatus(), api.getVideoPublishPlatforms(), api.getVideoPublishAccounts(), mediaRequest,
  ])
  if (statusResult.status === 'fulfilled') {
    sau.value = statusResult.value.sau
    sauProjectPath.value = statusResult.value.sau.project_path || ''
  } else error.value = safeError(statusResult.reason)
  if (platformResult.status === 'fulfilled') {
    platforms.value = platformResult.value.platforms
    if (!platforms.value.some((platform) => platform.id === activePlatformId.value)) {
      activePlatformId.value = platforms.value[0]?.id || ''
    }
  }
  else if (!error.value) error.value = safeError(platformResult.reason)
  if (accountResult.status === 'fulfilled') accounts.value = accountResult.value.items
  else if (!error.value) error.value = safeError(accountResult.reason)
  if (mediaResult.status === 'fulfilled') mediaItems.value = mediaResult.value.items
  else if (!error.value) error.value = safeError(mediaResult.reason)

  /* 素材库新建入口暂时停用，保留原逻辑供后续恢复。作者：zhw，时间：2026-11-08 22:53:04（Asia/Shanghai）
  const requestedAssetId = String(router.currentRoute.value.query.asset_id || '')
  if (requestedAssetId && !selectedMedia.value && !isEditMode.value) {
    selectedMedia.value = mediaItems.value.find((item) => item.asset_id === requestedAssetId) || null
  }
  */
  if (isEditMode.value) {
    const editKey = `${editBatchId.value}:${editTargetId.value}`
    if (initializedEditKey !== editKey) {
      editLoadError.value = ''
      editBatch.value = null
      editTarget.value = null
      try {
        const result = await api.getVideoPublishBatch(editBatchId.value)
        const target = result.targets.find((item) => item.id === editTargetId.value)
        if (!target) throw new Error('该账号不属于原发布任务。')
        editBatch.value = result
        editTarget.value = target
        if (!['failed', 'cancelled'].includes(target.status)) {
          throw new Error('该账号当前状态不支持编辑，请先停止任务或核实平台结果。')
        }
        if (!result.media_available) throw new Error('任务素材已清理或原视频不可用，请重新选择视频创建新任务。')
        if (!accounts.value.some((account) => account.id === target.account_id && account.status === 'connected')) {
          throw new Error('原发布账号当前未连接，请先检查登录状态后再编辑。')
        }
        initializeEditForm(result, target)
        initializedEditKey = editKey
      } catch (cause) {
        editLoadError.value = cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '无法读取原任务设置。'
        error.value = editLoadError.value
      }
    }
  } else {
    editBatch.value = null
    editTarget.value = null
    editLoadError.value = ''
    initializedEditKey = ''
    if (draftIdFromRoute.value) {
      const requestedDraftId = draftIdFromRoute.value
      if (initializedDraftId !== requestedDraftId) {
        error.value = ''
        try {
          await clearTemporaryDraftUploads()
          clearDraftFormState()
          activeDraftId.value = ''
          await initializeDraftForm(requestedDraftId)
          initializedDraftId = requestedDraftId
        } catch (cause) {
          await clearTemporaryDraftUploads()
          clearDraftFormState()
          activeDraftId.value = ''
          initializedDraftId = ''
          error.value = safeError(cause)
        }
      }
    } else if (initializedDraftId) {
      await clearTemporaryDraftUploads()
      clearDraftFormState()
      activeDraftId.value = ''
      initializedDraftId = ''
    }
  }
  loading.value = false
}

function initializeEditForm(batch: VideoPublishBatch, target: VideoPublishTarget): void {
  const options = target.options
  const accountId = target.account_id
  const platformId = target.platform_id
  selectedAccountIds.value = [accountId]
  activePlatformId.value = platformId
  const libraryMedia = batch.asset_id ? mediaItems.value.find((item) => item.asset_id === batch.asset_id) : undefined
  selectedMedia.value = libraryMedia || null
  title.value = options.title || batch.title
  description.value = options.description ?? batch.description
  const savedTags = Object.prototype.hasOwnProperty.call(options, 'tags') ? options.tags ?? [] : batch.tags
  tags.value = savedTags.slice(0, MAX_VIDEO_TAGS).map(tag => tag.trim().replace(/^#+/, '')).filter(Boolean)
  tagDraft.value = ''
  tagInputError.value = ''
  clearDouyinTopicSearch()
  const schedule = Object.prototype.hasOwnProperty.call(options, 'schedule_at_override')
    ? options.schedule_at_override
    : batch.schedule_at
  scheduleMode.value = schedule ? 'scheduled' : 'immediate'
  scheduleAt.value = schedule ? schedule.replace(' ', 'T').slice(0, 16) : ''
  for (const key of Object.keys(platformSchedules)) delete platformSchedules[key]

  const override = overrideFor(platformId)
  override.enabled = false
  override.title = ''
  override.description = ''
  override.tags = ''
  if (platformId === 'douyin') {
    override.declaration = options.declaration || '无需添加自主声明'
    override.repostSource = options.repost_source || ''
    override.visibility = options.visibility || ''
    override.allowDownload = typeof options.allow_download === 'boolean' ? options.allow_download : null
    if (options.location && options.location_id && options.location_address && options.location_city_code) {
      poiSelections[accountId] = {
        id: options.location_id,
        name: options.location,
        address: options.location_address,
        city_code: options.location_city_code,
        city_name: options.location_city_name || '',
      }
      poiQueries[accountId] = options.location
      poiCityCodes[accountId] = options.location_city_code
    } else {
      poiSelections[accountId] = null
      poiQueries[accountId] = ''
    }
    if (options.repost_id && options.repost_title) {
      repostHistories[accountId] = [{ id: options.repost_id, title: options.repost_title }]
      repostSelectedIds[accountId] = options.repost_id
    } else {
      repostHistories[accountId] = []
      repostSelectedIds[accountId] = ''
    }
  } else if (platformId === 'baijiahao') {
    override.declaration = options.baijiahao_declaration || ''
  } else if (platformId === 'xiaohongshu') {
    override.xhsOriginal = Boolean(options.xhs_original)
    override.xhsDeclaration = options.xhs_declaration || ''
    override.xhsRepostSource = options.xhs_repost_source || ''
    override.xhsVisibility = options.xhs_visibility || ''
    if (options.xhs_location && options.xhs_location_id && options.xhs_location_address) {
      xhsPoiSelections[accountId] = { id: options.xhs_location_id, name: options.xhs_location, address: options.xhs_location_address }
      xhsPoiQueries[accountId] = options.xhs_location
    } else {
      xhsPoiSelections[accountId] = null
      xhsPoiQueries[accountId] = ''
    }
    if (options.cover_ratio === 'landscape' || options.cover_ratio === 'portrait') xiaohongshuCoverRatio.value = options.cover_ratio
  } else if (platformId === 'kuaishou') {
    if (options.cover_ratio) kuaishouCoverRatio.value = options.cover_ratio
    override.collectionName = options.collection_name || ''
  } else if (platformId === 'wechat_channels') {
    override.shortTitle = options.short_title || ''
    override.saveAsDraft = Boolean(options.save_as_draft)
    override.collectionName = options.collection_name || ''
  } else if (platformId === 'bilibili') {
    override.tid = String(options.bilibili_category ?? options.tid ?? '')
    override.bilibiliStatement = options.bilibili_statement === undefined ? '' : String(options.bilibili_statement)
    override.bilibiliSource = options.bilibili_source || ''
    override.bilibiliNoReprint = Boolean(options.bilibili_no_reprint)
    override.bilibiliPrivate = Boolean(options.bilibili_private)
    override.bilibiliDynamic = options.bilibili_dynamic || ''
  }

  for (const key of Object.keys(editCoverFilenames)) delete editCoverFilenames[key]
  if (platformId === 'douyin') {
    if (options.cover_portrait_filename) editCoverFilenames[coverKey(platformId, 'portrait')] = options.cover_portrait_filename
    if (options.cover_landscape_filename) editCoverFilenames[coverKey(platformId, 'landscape')] = options.cover_landscape_filename
  } else if (platformId === 'bilibili') {
    if (options.cover_4_3_filename || options.cover_filename) editCoverFilenames[coverKey(platformId, 'landscape')] = options.cover_4_3_filename || options.cover_filename || ''
    if (options.cover_16_9_filename) editCoverFilenames[coverKey(platformId, 'wide')] = options.cover_16_9_filename
  } else if (platformId === 'wechat_channels') {
    if (options.cover_portrait_filename) editCoverFilenames[coverKey(platformId, 'portrait')] = options.cover_portrait_filename
    if (options.cover_landscape_filename) editCoverFilenames[coverKey(platformId, 'landscape')] = options.cover_landscape_filename
  } else if (platformId === 'baijiahao') {
    if (options.cover_portrait_filename) editCoverFilenames[coverKey(platformId, 'portrait')] = options.cover_portrait_filename
    if (options.cover_16_9_filename || options.cover_filename) {
      editCoverFilenames[coverKey(platformId, 'wide')] = options.cover_16_9_filename || options.cover_filename || ''
    }
  } else if (options.cover_filename) {
    const slot = ['kuaishou', 'xiaohongshu'].includes(platformId) ? coverSlots(platformId)[0] : 'single'
    editCoverFilenames[coverKey(platformId, slot)] = options.cover_filename
  }
}

async function refreshAccounts(): Promise<void> {
  try {
    const result = await api.getVideoPublishAccounts()
    const pendingIds = new Set(accounts.value.filter((item) => item.status === 'login_in_progress').map((item) => item.id))
    const notice = result.notices?.find((item) => pendingIds.has(item.account_id))
    accounts.value = result.items
    const accountById = new Map(result.items.map((account) => [account.id, account] as const))
    if (accountCancelBusy.value && accountById.get(accountCancelBusy.value)?.status !== 'login_in_progress') {
      accountCancelBusy.value = ''
    }
    selectedAccountIds.value = selectedAccountIds.value.filter((id) => {
      const status = accountById.get(id)?.status
      return status === 'connected' || status === 'checking'
    })
    settleAccountPreflights(result.items)
    if (notice) error.value = notice.message
  } catch (cause) {
    error.value = safeError(cause)
  }
}

function isAccountChecking(account: VideoPublishAccount): boolean {
  return pendingAccountPreflightIds.has(account.id) || accountCheckBusy.value === account.id || account.status === 'checking'
}

function markJustConnected(accountId: string): void {
  justConnectedIds.add(accountId)
  const existing = justConnectedTimers.get(accountId)
  if (existing !== undefined) window.clearTimeout(existing)
  justConnectedTimers.set(accountId, window.setTimeout(() => {
    justConnectedIds.delete(accountId)
    justConnectedTimers.delete(accountId)
  }, 1600))
}

function settleAccountPreflights(nextAccounts: VideoPublishAccount[]): void {
  const accountById = new Map(nextAccounts.map((account) => [account.id, account] as const))
  for (const accountId of [...pendingAccountPreflightIds]) {
    const account = accountById.get(accountId)
    if (!account) {
      pendingAccountPreflightIds.delete(accountId)
      continue
    }
    if (account.status === 'checking' || account.status === 'login_in_progress') continue

    pendingAccountPreflightIds.delete(accountId)
    if (account.status !== 'connected' || selectedAccountIds.value.includes(accountId)) continue
    selectedAccountIds.value = [...selectedAccountIds.value, accountId]
    markJustConnected(accountId)
    showStatusMessage(`${videoAccountName(account)}登录状态有效，已加入所选账号。`)
    error.value = ''
  }
}

function startAccountPolling(): void {
  if (accountPollTimer) return
  accountPollTimer = window.setInterval(() => {
    if (accounts.value.some((account) => account.status === 'login_in_progress' || account.status === 'checking')) {
      qrRefreshKey.value = Date.now()
      void refreshAccounts()
    } else {
      stopAccountPolling()
    }
  }, 3000)
}

function stopAccountPolling(): void {
  if (accountPollTimer) window.clearInterval(accountPollTimer)
  accountPollTimer = undefined
}

async function saveSauSettings(): Promise<void> {
  savingSau.value = true
  error.value = ''
  try {
    const result = await api.saveVideoPublishSettings(sauProjectPath.value.trim() || null)
    sau.value = result.sau
    showStatusMessage(result.sau.configured ? 'SAU 已就绪。' : result.sau.message, result.sau.configured ? 'success' : 'warning')
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    savingSau.value = false
  }
}

async function addAccount(platformId: string): Promise<void> {
  if (accountBusy.value || !platforms.value.find((item) => item.id === platformId)?.login_available) return
  accountBusy.value = 'create'
  error.value = ''
  try {
    const account = await api.addVideoPublishAccount({ platform_id: platformId })
    accounts.value = [...accounts.value, account]
    if (sau.value?.configured) {
      await startLogin(account)
    } else {
      showStatusMessage('账号已添加。配置 SAU 后可打开登录窗口。', 'info')
    }
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    accountBusy.value = ''
  }
}

async function startLogin(account: VideoPublishAccount): Promise<void> {
  if (!platforms.value.find((item) => item.id === account.platform_id)?.login_available) return
  if (account.platform_id === 'wechat_channels') { qrImageReady[account.id] = false; qrRefreshKey.value = Date.now() }
  accountBusy.value = account.id
  error.value = ''
  try {
    const updated = await api.loginVideoPublishAccount(account.id)
    accounts.value = accounts.value.map((item) => item.id === account.id ? updated : item)
    showStatusMessage(['bilibili', 'wechat_channels'].includes(account.platform_id) ? '正在准备登录二维码，请稍候。' : `正在打开 ${platformName(account.platform_id)} 登录窗口，请留意弹出的页面。`, 'info')
    startAccountPolling()
  } catch (cause) {
    error.value = safeError(cause)
    await refreshAccounts()
  } finally {
    accountBusy.value = ''
  }
}

async function cancelAccountLogin(account: VideoPublishAccount): Promise<void> {
  if (accountCancelBusy.value) return
  accountCancelBusy.value = account.id
  error.value = ''
  try {
    await api.cancelVideoPublishAccountLogin(account.id)
    showStatusMessage('正在取消登录，请稍候。', 'info')
    startAccountPolling()
    await refreshAccounts()
  } catch (cause) {
    error.value = safeError(cause)
    await refreshAccounts()
    accountCancelBusy.value = ''
  }
}

async function checkAccount(account: VideoPublishAccount): Promise<void> {
  accountBusy.value = account.id
  accountCheckBusy.value = account.id
  error.value = ''
  try {
    const updated = await api.checkVideoPublishAccount(account.id)
    accounts.value = accounts.value.map((item) => item.id === account.id ? updated : item)
    if (updated.status === 'connected') markJustConnected(account.id)
    startAccountPolling()
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    accountBusy.value = ''
    accountCheckBusy.value = ''
  }
}

async function logoutAccount(account: VideoPublishAccount): Promise<void> {
  if (accountBusy.value) return
  if (!await confirmWithElementPlus(`退出并移除「${videoAccountName(account)}」？本机登录状态将被清除，平台账号及发布历史会保留。`, '确认退出并移除账号')) return
  accountBusy.value = account.id
  try {
    await api.logoutVideoPublishAccount(account.id)
    accounts.value = accounts.value.filter((item) => item.id !== account.id)
    selectedAccountIds.value = selectedAccountIds.value.filter((id) => id !== account.id)
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    accountBusy.value = ''
  }
}

async function toggleAccount(account: VideoPublishAccount, checked: boolean): Promise<void> {
  const wasSelected = selectedAccountIds.value.includes(account.id)
  if (wasSelected) {
    selectedAccountIds.value = selectedAccountIds.value.filter((id) => id !== account.id)
    return
  }
  if (!checked) return
  if (account.status !== 'connected' || pendingAccountPreflightIds.has(account.id)) return

  if (hasRecentSuccessfulAccountCheck(account)) {
    selectedAccountIds.value = [...selectedAccountIds.value, account.id]
    showStatusMessage(`${videoAccountName(account)}的登录状态最近已确认正常，已直接选中。`)
    error.value = ''
    return
  }

  pendingAccountPreflightIds.add(account.id)
  error.value = ''
  try {
    const updated = await api.checkVideoPublishAccount(account.id)
    accounts.value = accounts.value.map((item) => item.id === account.id ? updated : item)
    if (updated.status === 'checking') startAccountPolling()
    else settleAccountPreflights(accounts.value)
  } catch (cause) {
    pendingAccountPreflightIds.delete(account.id)
    error.value = safeError(cause)
    await refreshAccounts()
  }
}

function onUploaded(receipt: UploadReceipt | null, file?: File): void {
  if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
  uploadedVideoUrl.value = receipt?.detectedMediaType === 'video' ? (file ? URL.createObjectURL(file) : receipt.previewUrl || '') : ''
  uploadReceipt.value = receipt
  selectedMedia.value = null
  if (receipt && receipt.detectedMediaType !== 'video') error.value = '所选文件是音频，视频发布任务需要视频文件。'
  else error.value = ''
}

/* 素材库选择入口暂时关闭，保留原实现供后续恢复。作者：zhw，时间：2026-11-08 22:53:04（Asia/Shanghai）
async function selectLibraryMedia(media: VideoPublishMedia): Promise<void> {
  if (uploadReceipt.value) {
    await api.deleteUpload(uploadReceipt.value.uploadId).catch(() => undefined)
    uploadReceipt.value = null
  }
  if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
  uploadedVideoUrl.value = ''
  sourceMode.value = 'library'
  sourceTabMode.value = 'library'
  selectedMedia.value = media
  libraryOpen.value = false
  error.value = ''
}

async function switchToUpload(): Promise<void> {
  sourceMode.value = 'upload'
  sourceTabMode.value = 'upload'
  selectedMedia.value = null
  if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
  uploadedVideoUrl.value = ''
  if (uploadReceipt.value) {
    await api.deleteUpload(uploadReceipt.value.uploadId).catch(() => undefined)
    uploadReceipt.value = null
  }
}

function handleSourceTabChange(value: string | number): void {
  if (value === 'library') {
    sourceTabMode.value = 'library'
    libraryOpen.value = true
  } else if (value === 'upload') {
    sourceTabMode.value = 'upload'
    void switchToUpload()
  }
}

function handleLibraryDialogClose(): void {
  sourceTabMode.value = sourceMode.value
}
*/

function setAllowDownloadRadioValue(value: string | number | boolean | undefined): void {
  if (!activePlatform.value) return
  overrideFor(activePlatform.value.id).allowDownload = value === 'default' || value === undefined ? null : value === true
}

const baijiahaoDeclarations = ['无需声明', '含AI生成内容', '内容为转载', '含虚构演绎内容', '内容含营销信息', '个人观点，仅供参考']

function validateDraft(): string | null {
  if (!sau.value?.configured) return '请先配置可用的 social-auto-upload 项目目录。'
  if (!activeMediaName.value || !activeMediaIsVideo.value) return '请先选择一个有效视频文件。'
  if (!title.value.trim()) return '请填写视频标题。'
  if (description.value.length > 1000) return '视频简介不能超过 1000 个字符。'
  if (selectedAccounts.value.length === 0) return '请至少选择一个已经连接的平台账号。'
  if (selectedAccounts.value.length > 20) return '一次最多选择 20 个发布账号。'
  if (selectedPlatforms.value.includes('baijiahao') && !baijiahaoDeclarations.includes(overrideFor('baijiahao').declaration)) return '请为百家号选择符合实际的创作声明。'
  if (scheduleMode.value === 'scheduled' && !scheduleAt.value) return '请选择通用计划发布时间。'
  const locationMissing = selectedAccounts.value.find(account => account.platform_id === 'douyin' && (poiQueries[account.id] || '').trim() && !poiSelections[account.id])
  if (locationMissing) return `请为抖音账号「${videoAccountName(locationMissing)}」选择搜索结果，或清空地点输入。`
  if (selectedPlatforms.value.includes('douyin') && overrideFor('douyin').declaration === '内容为转载信息') {
    if (!overrideFor('douyin').repostSource) return '抖音转载声明请选择取材站内或取材站外。'
    if (overrideFor('douyin').repostSource === 'inside') {
      const missing = selectedAccounts.value.find((account) => account.platform_id === 'douyin' && !repostHistories[account.id]?.some(work => work.id === repostSelectedIds[account.id]))
      if (missing) return `请为抖音账号「${videoAccountName(missing)}」选择站内原作品。`
    }
  }
  if (scheduleMode.value === 'scheduled') {
    if (unsupportedSchedules.value.length) return `${unsupportedSchedules.value.map(platformName).join('、')}暂不支持定时发布，请移除相关账号或选择立即发布。`
    const commonTimestamp = new Date(scheduleAt.value).getTime()
    if (!Number.isFinite(commonTimestamp) || commonTimestamp <= Date.now()) return '通用计划发布时间必须晚于当前时间。'
    for (const id of selectedPlatforms.value) {
      const value = platformSchedule(id)
      const timestamp = new Date(value).getTime()
      const remaining = timestamp - Date.now()
      if (!value || !Number.isFinite(timestamp) || remaining <= 0) return `${platformName(id)}的定时发布时间必须晚于当前时间。`
      if (id === 'bilibili' && remaining < 5 * 60 * 1000) return '哔哩哔哩定时发布时间需至少距现在 5 分钟。'
      if (id === 'bilibili' && remaining > 15 * 24 * 60 * 60 * 1000) return '哔哩哔哩定时发布时间不能超过 15 天。'
      if (id === 'baijiahao' && (remaining < 60 * 60 * 1000 || remaining > 7 * 24 * 60 * 60 * 1000)) return '百家号定时发布时间需在 1 小时后至 7 天内。'
      if (['douyin', 'kuaishou', 'xiaohongshu', 'wechat_channels'].includes(id) && remaining <= 2 * 60 * 60 * 1000) return `${platformName(id)}的定时发布时间必须距现在超过 2 小时，请重新选择。`
      if (id === 'douyin' && remaining > 14 * 24 * 60 * 60 * 1000) return '抖音定时发布时间不能超过 14 天，请重新选择。'
    }
  }
  if (missingBilibiliTid.value) return '发布到哔哩哔哩时请选择有效的投稿分区。'
  if (selectedPlatforms.value.includes('bilibili')) {
    const statement = overrideFor('bilibili').bilibiliStatement
    if (!statement || !bilibiliStatements.value.some(item => String(item.id) === statement) || !['-2', '-1', '1', '2', '3', '4'].includes(statement)) return '发布到哔哩哔哩时请选择有效的创作声明。'
    if (statement === '-2' && !overrideFor('bilibili').bilibiliSource.trim()) return '转载视频需要填写转载来源。'
  }
  if (selectedPlatforms.value.includes('bilibili') && !description.value.trim() && !overrideFor('bilibili').description.trim()) return '哔哩哔哩要求填写视频简介。'
  if (selectedPlatforms.value.includes('bilibili')) {
    if (!hasEffectiveCover('bilibili', 'landscape')) return '哔哩哔哩需要设置 4:3 封面，可使用左侧通用封面或单独裁剪。'
    if (!hasEffectiveCover('bilibili', 'wide')) return '哔哩哔哩需要设置 16:9 封面，可使用左侧通用封面或单独裁剪。'
  }
  if (selectedPlatforms.value.includes('douyin')) {
    const hasCustomDouyinCover = hasEffectiveCover('douyin', 'portrait') || hasEffectiveCover('douyin', 'landscape')
    if (hasCustomDouyinCover && (!hasEffectiveCover('douyin', 'portrait') || !hasEffectiveCover('douyin', 'landscape'))) return '抖音自定义封面需要同时设置 3:4 和 4:3 两种比例；也可以移除自定义封面，使用视频首帧。'
  }
  if (selectedPlatforms.value.includes('wechat_channels')) {
    if (!isEditMode.value && !mediaDimensions.value) return mediaDimensionsError.value ? '当前视频无法在浏览器中读取尺寸，请更换视频格式后再设置视频号封面。' : '正在读取视频尺寸，暂时无法判断视频号所需封面比例，请稍后再提交。'
    const missingSlot = coverSlots('wechat_channels').find((slot) => !hasEffectiveCover('wechat_channels', slot))
    if (missingSlot) return `微信视频号需要设置 ${slotRatioLabel(missingSlot)} 封面，可使用左侧通用封面或单独裁剪。`
  }
  if (selectedPlatforms.value.includes('wechat_channels') && overrideFor('wechat_channels').saveAsDraft && scheduleMode.value === 'scheduled') return '微信视频号保存草稿不能同时设置定时发布时间，请选择立即发布或关闭草稿模式。'
  if (selectedPlatforms.value.includes('weibo')) {
    const weiboOverride = overrideFor('weibo')
    const weiboTitle = weiboOverride.enabled && weiboOverride.title.trim() ? weiboOverride.title.trim() : title.value.trim()
    if (weiboTitle.length > 30) return '微博视频标题最多 30 个字符，请在平台专属设置中单独填写短标题。'
  }
  if (selectedPlatforms.value.includes('xiaohongshu')) {
    const xhsOverride = overrideFor('xiaohongshu')
    if (xhsOverride.xhsDeclaration === 'self_shot') return '小红书自主拍摄声明需要拍摄地点和拍摄日期，当前工作台暂不支持填写；请选择符合实际的其他声明或不添加声明。'
    if (xhsOverride.xhsDeclaration === 'repost' && !xhsOverride.xhsRepostSource.trim()) return '小红书来源转载需要填写媒体名称。'
    const missingLocation = selectedAccounts.value.find(account => account.platform_id === 'xiaohongshu' && (xhsPoiQueries[account.id] || '').trim() && !xhsPoiSelections[account.id])
    if (missingLocation) return `请从搜索结果中选择账号「${videoAccountName(missingLocation)}」的地点，或清空地点关键词。`
  }
  if (selectedPlatforms.value.includes('baijiahao')) {
    const missingSlot = coverSlots('baijiahao').find((slot) => !hasEffectiveCover('baijiahao', slot))
    if (missingSlot) return `百家号需要设置 ${coverSlotRatio(missingSlot)} 封面，可使用左侧同尺寸通用封面或单独裁剪。`
  }
  if (selectedPlatforms.value.includes('weibo')) {
    const cover = coverUploadFor('weibo', 'single')
    if (!hasEffectiveCover('weibo', 'single')) return hasCommonCover()
      ? '左侧通用封面比例不符合微博要求，请为微博单独上传 3:4、16:9 或 4:3 封面。'
      : '发布到微博时必须上传 3:4、16:9 或 4:3 封面。'
    if (cover && cover.size_bytes > 5 * 1024 * 1024) return '微博视频封面不能超过 5 MB，请压缩图片或单独上传微博封面。'
  }
  return null
}

async function loadRepostHistory(account: VideoPublishAccount): Promise<void> {
  if (repostHistoryBusy.value) return
  repostHistoryBusy.value = account.id
  error.value = ''
  try {
    const result = await api.getDouyinRepostHistory(account.id)
    repostHistories[account.id] = result.items
    if (!result.items.some(work => work.id === repostSelectedIds[account.id])) repostSelectedIds[account.id] = ''
    if (!result.items.length) error.value = '抖音没有返回可选择的观看历史，请先在该账号观看原作品后重试。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    repostHistoryBusy.value = ''
  }
}

function queuePoiSearch(account: VideoPublishAccount): void {
  const id = account.id
  poiVersions[id] = (poiVersions[id] || 0) + 1
  poiSelections[id] = null
  poiResults[id] = []
  poiErrors[id] = ''
  poiSearched[id] = false
  const previous = poiTimers.get(id)
  if (previous !== undefined) window.clearTimeout(previous)
  if (!(poiQueries[id] || '').trim()) return
  poiTimers.set(id, window.setTimeout(() => { void runPoiSearch(account) }, 600))
}

async function runPoiSearch(account: VideoPublishAccount): Promise<void> {
  const id = account.id
  if (poiBusy[id] || !(poiQueries[id] || '').trim() || poiSelections[id]) return
  const version = poiVersions[id] || 0
  const keyword = poiQueries[id].trim()
  poiBusy[id] = true
  poiErrors[id] = ''
  try {
    const result = await api.searchDouyinPoi(id, keyword, poiCityCodes[id] || null)
    if (version !== poiVersions[id]) return
    poiResults[id] = result.items
    poiCities[id] = result.cities
    poiCityCodes[id] = result.selected_city_code
    poiSearched[id] = true
  } catch (cause) {
    if (version === poiVersions[id]) poiErrors[id] = safeError(cause)
  } finally {
    poiBusy[id] = false
    if (version !== poiVersions[id] && (poiQueries[id] || '').trim() && !poiSelections[id]) {
      const previous = poiTimers.get(id)
      if (previous !== undefined) window.clearTimeout(previous)
      poiTimers.set(id, window.setTimeout(() => { void runPoiSearch(account) }, 100))
    }
  }
}

function choosePoi(account: VideoPublishAccount, poi: VideoPublishPoi): void {
  const id = account.id
  poiVersions[id] = (poiVersions[id] || 0) + 1
  const previous = poiTimers.get(id)
  if (previous !== undefined) window.clearTimeout(previous)
  poiSelections[id] = poi
  poiQueries[id] = poi.name
  poiResults[id] = []
  poiErrors[id] = ''
}

function clearPoi(account: VideoPublishAccount): void {
  poiQueries[account.id] = ''
  queuePoiSearch(account)
}

function queueXhsPoiSearch(account: VideoPublishAccount): void {
  const id = account.id
  xhsPoiVersions[id] = (xhsPoiVersions[id] || 0) + 1
  xhsPoiSelections[id] = null
  xhsPoiResults[id] = []
  xhsPoiErrors[id] = ''
  xhsPoiSearched[id] = false
  const previous = xhsPoiTimers.get(id)
  if (previous !== undefined) window.clearTimeout(previous)
  if ((xhsPoiQueries[id] || '').trim()) xhsPoiTimers.set(id, window.setTimeout(() => { void runXhsPoiSearch(account) }, 600))
}

async function runXhsPoiSearch(account: VideoPublishAccount): Promise<void> {
  const id = account.id
  if (xhsPoiBusy[id] || !(xhsPoiQueries[id] || '').trim() || xhsPoiSelections[id]) return
  const version = xhsPoiVersions[id] || 0
  xhsPoiBusy[id] = true
  xhsPoiErrors[id] = ''
  try {
    const result = await api.searchXhsPoi(id, xhsPoiQueries[id].trim())
    if (version !== xhsPoiVersions[id]) return
    xhsPoiResults[id] = result.items
    xhsPoiSearched[id] = true
  } catch (cause) {
    if (version === xhsPoiVersions[id]) xhsPoiErrors[id] = safeError(cause)
  } finally {
    xhsPoiBusy[id] = false
    if (version !== xhsPoiVersions[id] && (xhsPoiQueries[id] || '').trim() && !xhsPoiSelections[id]) {
      const previous = xhsPoiTimers.get(id)
      if (previous !== undefined) window.clearTimeout(previous)
      xhsPoiTimers.set(id, window.setTimeout(() => { void runXhsPoiSearch(account) }, 100))
    }
  }
}

function chooseXhsPoi(account: VideoPublishAccount, poi: VideoPublishXhsPoi): void {
  const id = account.id
  xhsPoiVersions[id] = (xhsPoiVersions[id] || 0) + 1
  const previous = xhsPoiTimers.get(id)
  if (previous !== undefined) window.clearTimeout(previous)
  xhsPoiSelections[id] = poi
  xhsPoiQueries[id] = poi.name
  xhsPoiResults[id] = []
  xhsPoiErrors[id] = ''
}

function optionsFor(platformId: string, accountId: string): {
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
} | undefined {
  const override = overrideFor(platformId)
  const options: {
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
  } = {}
  if (isEditMode.value) {
    options.schedule_at_override = scheduleMode.value === 'scheduled' ? scheduleAt.value : null
  } else if (scheduleMode.value === 'scheduled' && hasPlatformSchedule(platformId)) {
    options.schedule_at_override = platformSchedules[platformId]
  }
  if (override.enabled) {
    if (override.title.trim()) options.title = override.title.trim()
    if (override.description.trim()) options.description = override.description.trim()
    const platformTags = splitTags(override.tags)
    if (platformTags.length) options.tags = platformTags
  }
  if (platformId === 'bilibili' && override.tid.trim()) {
    options.bilibili_category = Number(override.tid)
    const category = bilibiliCategories.value.find(item => item.id === Number(override.tid))
    if (category) options.bilibili_category_name = category.name
    options.bilibili_statement = Number(override.bilibiliStatement)
    const statement = bilibiliStatements.value.find(item => item.id === Number(override.bilibiliStatement))
    if (statement) options.bilibili_statement_name = statement.name
    if (override.bilibiliStatement === '-2') options.bilibili_source = override.bilibiliSource.trim()
    else options.bilibili_no_reprint = override.bilibiliNoReprint
    options.bilibili_private = override.bilibiliPrivate
    if (override.bilibiliDynamic.trim()) options.bilibili_dynamic = override.bilibiliDynamic.trim()
  }
  if (platformId === 'kuaishou' || platformId === 'wechat_channels') {
    if (override.collectionName.trim()) options.collection_name = override.collectionName.trim()
  }
  if (platformId === 'wechat_channels' && override.shortTitle.trim()) options.short_title = override.shortTitle.trim()
  if (platformId === 'wechat_channels' && override.saveAsDraft) options.save_as_draft = true
  if (platformId === 'baijiahao') options.baijiahao_declaration = override.declaration
  if (platformId === 'xiaohongshu') {
    if (override.xhsOriginal) options.xhs_original = true
    if (override.xhsDeclaration) options.xhs_declaration = override.xhsDeclaration
    if (override.xhsDeclaration === 'repost') options.xhs_repost_source = override.xhsRepostSource.trim()
    if (override.xhsVisibility) options.xhs_visibility = override.xhsVisibility
    const poi = xhsPoiSelections[accountId]
    if (poi) { options.xhs_location = poi.name; options.xhs_location_id = poi.id; options.xhs_location_address = poi.address }
  }
  if (platformId === 'bilibili') {
    const cover43 = coverUploadFor(platformId, 'landscape')
    const cover169 = coverUploadFor(platformId, 'wide')
    if (cover43) options.cover_4_3_upload_id = cover43.upload_id
    if (cover169) options.cover_16_9_upload_id = cover169.upload_id
  } else if (platformId === 'baijiahao') {
    const cover34 = coverUploadFor(platformId, 'portrait')
    const cover169 = coverUploadFor(platformId, 'wide')
    if (cover34) options.cover_portrait_upload_id = cover34.upload_id
    if (cover169) options.cover_16_9_upload_id = cover169.upload_id
  } else if (platformId === 'douyin' || platformId === 'wechat_channels') {
    const portrait = coverUploadFor(platformId, 'portrait')
    const landscape = coverSlots(platformId).includes('landscape') ? coverUploadFor(platformId, 'landscape') : null
    if (portrait) options.cover_portrait_upload_id = portrait.upload_id
    if (landscape) options.cover_landscape_upload_id = landscape.upload_id
  } else {
    const ratioSlot = ['kuaishou', 'xiaohongshu'].includes(platformId) ? coverSlots(platformId)[0] : 'single'
    const cover = coverUploadFor(platformId, ratioSlot)
    if (cover) {
      options.cover_upload_id = cover.upload_id
      if (platformId === 'kuaishou') options.cover_ratio = kuaishouCoverRatio.value
      if (platformId === 'xiaohongshu') options.cover_ratio = xiaohongshuCoverRatio.value
    }
  }
  if (isEditMode.value && platformId === 'kuaishou') options.cover_ratio = kuaishouCoverRatio.value
  if (isEditMode.value && platformId === 'xiaohongshu') options.cover_ratio = xiaohongshuCoverRatio.value
  if (platformId === 'douyin') {
    if (override.declaration) options.declaration = override.declaration
    if (override.declaration === '内容为转载信息' && override.repostSource) {
      options.repost_source = override.repostSource
      if (override.repostSource === 'inside') {
        const work = repostHistories[accountId]?.find(item => item.id === repostSelectedIds[accountId])
        if (work) { options.repost_title = work.title; options.repost_id = work.id }
      }
    }
    const poi = poiSelections[accountId]
    if (poi) {
      options.location = poi.name
      options.location_id = poi.id
      options.location_address = poi.address
      options.location_city_code = poi.city_code
      if (poi.city_name) options.location_city_name = poi.city_name
    }
    if (override.visibility) options.visibility = override.visibility
    if (override.allowDownload !== null) options.allow_download = override.allowDownload
  }
  return Object.keys(options).length ? options : undefined
}

function buildVideoPublishDraftForm(): VideoPublishDraftForm {
  const repostSelections = Object.fromEntries(Object.entries(repostSelectedIds).flatMap(([accountId, workId]) => {
    const work = repostHistories[accountId]?.find((item) => item.id === workId)
    return work ? [[accountId, { id: work.id, title: work.title }]] : []
  }))
  return JSON.parse(JSON.stringify({
    title: title.value,
    description: description.value,
    tags: [...tags.value],
    tag_draft: tagDraft.value,
    schedule_mode: scheduleMode.value,
    schedule_at: scheduleAt.value,
    platform_schedules: { ...platformSchedules },
    selected_account_ids: [...selectedAccountIds.value],
    active_platform_id: activePlatformId.value,
    overrides,
    poi_selections: poiSelections,
    poi_queries: poiQueries,
    poi_city_codes: poiCityCodes,
    xhs_poi_selections: xhsPoiSelections,
    xhs_poi_queries: xhsPoiQueries,
    repost_selections: repostSelections,
    cover_preferences: {
      kuaishou: kuaishouCoverRatio.value,
      xiaohongshu: xiaohongshuCoverRatio.value,
    },
  })) as VideoPublishDraftForm
}

async function saveDraft(): Promise<void> {
  const media = uploadReceipt.value
  if (!media || media.detectedMediaType !== 'video') {
    error.value = '请先上传有效视频，再保存草稿。'
    return
  }
  savingDraft.value = true
  error.value = ''
  try {
    const coverUploadIds = Object.fromEntries(Object.entries(coverUploads).flatMap(([slot, cover]) => cover ? [[slot, cover.upload_id]] : []))
    const input = {
      ...(media.sourceType === 'local' ? { local_source_id: media.uploadId } : { upload_id: media.uploadId }),
      cover_upload_ids: coverUploadIds,
      form: buildVideoPublishDraftForm(),
    }
    const saved = activeDraftId.value
      ? await api.updateVideoPublishDraft(activeDraftId.value, input)
      : await api.saveVideoPublishDraft(input)
    activeDraftId.value = saved.id
    initializedDraftId = saved.id
    await router.replace({ name: 'video-publish-create', query: { draft_id: saved.id } })
    ElMessage.success('草稿已保存在本机。')
  } catch (cause) {
    footerErrorAnimationKey.value += 1
    error.value = safeError(cause)
  } finally {
    savingDraft.value = false
  }
}

async function submitBatch(): Promise<void> {
  const validation = validateDraft()
  if (validation) {
    footerErrorAnimationKey.value += 1
    error.value = validation
    return
  }
  const destinationText = selectedAccounts.value.map((account) => `${platformName(account.platform_id)} · ${videoAccountName(account)}`).join('、')
  const when = scheduleMode.value === 'scheduled' ? `\n计划发布时间：\n${selectedPlatforms.value.map(id => `${platformName(id)}：${platformSchedule(id).replace('T', ' ')}`).join('\n')}` : '\n发布时间：立即发布'
  const commonRatios = commonCoverSlots.value.filter((slot) => Boolean(coverUploads[commonCoverKey(slot)])).map(slot => slotRatioLabel(slot)).join('、')
  const coverNote = commonRatios
    ? `\n左侧通用封面已设置：${commonRatios}；相同比例的平台封面位会自动继承。`
    : selectedPlatforms.value.includes('douyin')
      ? '\n抖音未上传自定义封面，将自动生成视频首帧封面。'
      : ''
  const draftNote = overrideFor('wechat_channels').saveAsDraft && selectedPlatforms.value.includes('wechat_channels')
    ? '\n微信视频号将保存到草稿箱，不会立即发布。'
    : ''
  const confirmText = isEditMode.value
    ? `确认修改并重新发送到「${destinationText}」？\n原任务：${editBatch.value?.title || ''}\n视频：${activeMediaName.value}${when}\n原处理记录会保留，只重新排队这个账号。`
    : `确认创建任务「${title.value.trim()}」？\n视频：${activeMediaName.value}\n目标：${destinationText}${when}${coverNote}${draftNote}\n\n部分平台执行完后需要你在后台人工核实状态。`
  if (!await confirmWithElementPlus(confirmText, isEditMode.value ? '确认修改并重新发送' : '确认创建视频发布任务')) return
  submitting.value = true
  error.value = ''
  try {
    if (isEditMode.value) {
      if (!editTarget.value || !editBatch.value) throw new Error('原任务信息已失效，请返回详情页重新打开编辑。')
      const account = selectedAccounts.value.find((item) => item.id === editTarget.value?.account_id)
      if (!account) throw new Error('原发布账号当前不可用，请检查账号登录状态。')
      const options = optionsFor(account.platform_id, account.id) || {}
      options.title = title.value.trim()
      options.description = description.value
      options.tags = tags.value.slice(0, MAX_VIDEO_TAGS)
      const updated = await api.editVideoPublishTarget(editTarget.value.id, { options, confirmed: true })
      for (const key of Object.keys(coverUploads)) {
        if (coverPreviews[key]) URL.revokeObjectURL(coverPreviews[key])
        coverPreviews[key] = ''
        coverUploads[key] = null
        coverDimensions[key] = null
      }
      initializedEditKey = ''
      await router.push({ name: 'video-publish-detail', params: { batchId: updated.id } })
      return
    }
    const draftId = activeDraftId.value
    const request = {
      idempotency_key: draftId || crypto.randomUUID().replace(/-/g, ''),
      ...(uploadReceipt.value?.sourceType === 'local'
        ? { local_source_id: uploadReceipt.value.uploadId }
        : { upload_id: uploadReceipt.value?.uploadId }),
      title: title.value.trim(),
      description: description.value.trim(),
      tags: tags.value.slice(0, MAX_VIDEO_TAGS),
      schedule_at: scheduleMode.value === 'scheduled' ? scheduleAt.value : null,
      targets: selectedAccounts.value.map((account) => ({ account_id: account.id, options: optionsFor(account.platform_id, account.id) })),
      confirmed: true,
    }
    const batch = await api.createVideoPublishBatch(request)
    if (draftId) {
      try {
        await api.deleteVideoPublishDraft(draftId)
        activeDraftId.value = ''
        initializedDraftId = ''
      } catch {
        ElMessage.warning('发布任务已创建，但本地草稿未能清理；可在发布记录的“本地草稿”中删除。')
      }
    }
    uploadReceipt.value = null
    if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
    uploadedVideoUrl.value = ''
    for (const key of Object.keys(coverUploads)) {
      if (coverPreviews[key]) URL.revokeObjectURL(coverPreviews[key])
      coverPreviews[key] = ''
      coverUploads[key] = null
      coverDimensions[key] = null
    }
    await router.push({ name: 'video-publish-detail', params: { batchId: batch.id } })
  } catch (cause) {
    footerErrorAnimationKey.value += 1
    error.value = safeError(cause)
  } finally {
    submitting.value = false
  }
}

async function cancel(): Promise<void> {
  if (isEditMode.value && editBatch.value) {
    for (const [key, cover] of Object.entries(coverUploads)) {
      if (cover) await api.deleteVideoPublishCover(cover.upload_id).catch(() => undefined)
      if (coverPreviews[key]) URL.revokeObjectURL(coverPreviews[key])
      coverUploads[key] = null
      coverPreviews[key] = ''
    }
    initializedEditKey = ''
    await router.push({ name: 'video-publish-detail', params: { batchId: editBatch.value.id } })
    return
  }
  await router.push({ name: 'video-publish-history' })
}

let coverKeepaliveTimer: number | undefined

function keepEditingCovers(): void {
  const ids = [...new Set(Object.values(coverUploads).flatMap(cover => cover ? [cover.upload_id] : []))]
  if (ids.length) void api.keepVideoPublishCovers(ids).catch(() => undefined)
}

onActivated(() => {
  void loadPage()
  keepEditingCovers()
  coverKeepaliveTimer ??= window.setInterval(keepEditingCovers, 5 * 60_000)
  startAccountPolling()
  document.addEventListener('pointerdown', handleDouyinTopicOutsidePointerDown, true)
  document.addEventListener('keydown', handleDouyinTopicGlobalKeydown, true)
  refreshScheduleMinimum()
  if (scheduleClockTimer !== undefined) window.clearInterval(scheduleClockTimer)
  scheduleClockTimer = window.setInterval(() => refreshScheduleMinimum(), 30_000)
})
onDeactivated(() => {
  stopAccountPolling()
  document.removeEventListener('pointerdown', handleDouyinTopicOutsidePointerDown, true)
  document.removeEventListener('keydown', handleDouyinTopicGlobalKeydown, true)
  if (scheduleClockTimer !== undefined) window.clearInterval(scheduleClockTimer)
  scheduleClockTimer = undefined
})
onBeforeUnmount(() => {
  if (coverKeepaliveTimer !== undefined) window.clearInterval(coverKeepaliveTimer)
  stopAccountPolling()
  document.removeEventListener('pointerdown', handleDouyinTopicOutsidePointerDown, true)
  document.removeEventListener('keydown', handleDouyinTopicGlobalKeydown, true)
  if (scheduleClockTimer !== undefined) window.clearInterval(scheduleClockTimer)
  if (douyinTopicTimer !== undefined) window.clearTimeout(douyinTopicTimer)
  for (const timer of poiTimers.values()) window.clearTimeout(timer)
  for (const timer of xhsPoiTimers.values()) window.clearTimeout(timer)
  for (const timer of justConnectedTimers.values()) window.clearTimeout(timer)
  justConnectedTimers.clear()
  if (uploadReceipt.value && uploadReceipt.value.sourceType !== 'local') void api.deleteUpload(uploadReceipt.value.uploadId).catch(() => undefined)
  videoDimensionRequest += 1
  clearUploadedVideoFirstFrame()
  if (uploadedVideoUrl.value) URL.revokeObjectURL(uploadedVideoUrl.value)
  for (const [key, cover] of Object.entries(coverUploads)) {
    if (cover) void api.deleteVideoPublishCover(cover.upload_id).catch(() => undefined)
    if (coverPreviews[key]) URL.revokeObjectURL(coverPreviews[key])
  }
})
</script>

<template>
  <section class="video-create-page" :class="{ 'is-upload-entry': !hasPublishMedia }" :aria-label="isEditMode ? '重新编辑发布任务' : '新建视频发布任务'">
    <section v-if="!loading && !sau?.configured" class="video-setup-card" aria-label="配置 social-auto-upload">
      <div class="video-setup-icon" aria-hidden="true">⚙</div>
      <div class="video-setup-copy">
        <strong>连接 social-auto-upload</strong>
        <p>{{ sau?.message || '配置项目目录后，工作台会通过隔离的 SAU CLI 管理账号登录和视频发布。' }}</p>
      </div>
      <div class="video-setup-input-row">
        <label>
          <span class="sr-only">SAU 项目根目录</span>
          <ElInput v-model="sauProjectPath" class="input" type="text" placeholder="例如 D:\tools\social-auto-upload" />
        </label>
        <ElButton class="button button-secondary button-small" native-type="button" :disabled="savingSau" @click="saveSauSettings">{{ savingSau ? '保存中…' : '保存目录' }}</ElButton>
      </div>
      <small>目录需要包含 pyproject.toml。请先在该目录准备 SAU 与 Chromium；浏览器登录档案由 SAU 单独管理。</small>
    </section>
    <ElAlert v-if="error && (!hasPublishMedia || loading)" class="info-box form-error" type="error" :closable="false" :title="error" />
    <ElAlert v-if="draftAccountWarning" class="info-box" type="warning" :closable="false" :title="draftAccountWarning" />
    <div v-if="isEditMode && editLoadError" class="video-edit-return">
      <RouterLink class="button button-secondary button-small" :to="{ name: 'video-publish-detail', params: { batchId: editBatchId } }">返回原任务详情</RouterLink>
    </div>
    <div class="video-create-workspace" :class="{ 'is-upload-entry': !hasPublishMedia }">
      <div class="video-compose-column">
        <section class="panel video-compose-card" aria-labelledby="video-media-heading">
          <div class="video-section-heading">
          <div><span class="video-section-index">01</span><div><h2 id="video-media-heading">{{ isEditMode ? '原任务视频' : '选择原创视频' }}</h2><p>{{ isEditMode ? '视频素材固定沿用原任务，仅修改当前账号的发布内容和平台设置。' : '直接关联本机原创视频；草稿和发布任务会使用原文件，封面保存在设置的目录中。' }}</p></div></div>
            <!-- 素材来源切换暂时关闭，仅保留本地上传；作者：zhw，时间：2026-11-08 22:53:04（Asia/Shanghai）
            <ElTabs v-if="!isEditMode" :model-value="sourceTabMode" class="video-source-toggle" aria-label="素材来源" @tab-change="handleSourceTabChange">
              <ElTabPane name="upload" label="本地上传" />
              <ElTabPane name="library" label="素材库" />
            </ElTabs>
            -->
          </div>
          <LocalMediaUpload
            v-if="!isEditMode"
            :disabled="submitting || savingDraft"
            :large="!hasPublishMedia"
            :compact="hasPublishMedia && uploadReceipt?.detectedMediaType === 'video'"
            :publish-target="true"
            :restored-receipt="uploadReceipt"
            :preview="uploadedVideoFirstFrame"
            :preview-state="uploadedVideoFirstFrameState"
            :details="mediaDimensions ? activeVideoRatioLabel : ''"
            @uploaded="onUploaded"
          />
          <div v-else-if="isEditMode" class="video-selected-media is-readonly">
            <div class="video-selected-media-mark" aria-hidden="true">▶</div>
            <div><strong>{{ activeMediaName }}</strong><span>{{ formatSize(activeMediaSize) }} · 原任务视频</span></div>
            <span class="video-edit-media-lock">固定素材</span>
          </div>
          <!-- 素材库选择卡片暂时停用，保留原实现供后续恢复。作者：zhw，时间：2026-11-08 22:53:04（Asia/Shanghai）
          <div v-else class="video-selected-media">
            <div class="video-selected-media-mark" aria-hidden="true">▶</div>
            <div><strong>{{ selectedMedia?.name || '尚未选择素材' }}</strong><span>{{ selectedMedia ? `${formatSize(selectedMedia.size_bytes)} · ${selectedMedia.title}` : '从已归档视频中选择' }}</span></div>
            <ElButton class="button button-secondary button-small" native-type="button" @click="libraryOpen = true">{{ selectedMedia ? '更换' : '选择素材' }}</ElButton>
          </div>
          -->
          <div v-if="!isEditMode && activeMediaName && uploadReceipt?.detectedMediaType !== 'video'" class="video-media-summary">
            <span class="video-media-check">✓</span>
            <div class="video-media-summary-copy">
              <strong>{{ activeMediaName }}</strong>
              <span>{{ formatSize(activeMediaSize) }} · 不是视频文件</span>
            </div>
          </div>
          <section v-if="hasPublishMedia && !isEditMode" class="video-common-cover-settings" aria-labelledby="video-common-cover-heading">
            <div class="video-settings-section-heading"><h3 id="video-common-cover-heading">通用封面裁剪</h3><span>视频比例：{{ activeVideoRatioLabel }}</span></div>
            <p class="video-cover-help">每个比例可从图片上传或所选视频截帧后裁剪。右侧平台有相同比例时自动继承；需要不同构图时可在平台区域单独裁剪覆盖。百家号、微博封面要求保持原有规则，微博封面最大 5 MB。</p>
            <div class="video-cover-grid video-common-cover-grid">
              <ElCard v-for="slot in commonCoverSlots" :key="slot" class="video-cover-upload-card" shadow="never">
                <ElImage v-if="coverPreviews[commonCoverKey(slot)]" :src="coverPreviews[commonCoverKey(slot)]" :alt="`${slotRatioLabel(slot)}通用封面预览`" fit="cover" class="video-cover-preview" :class="{ 'is-portrait': slot === 'portrait', 'is-tall': slot === 'tall', 'is-landscape': slot === 'landscape', 'is-wide': slot === 'wide', 'is-common': slot === 'square' || slot === 'original' }" />
                <div v-else class="video-cover-placeholder" :class="{ 'is-portrait': slot === 'portrait', 'is-tall': slot === 'tall', 'is-landscape': slot === 'landscape', 'is-wide': slot === 'wide', 'is-common': slot === 'square' || slot === 'original' }"><span>{{ slotRatioLabel(slot) }}</span><small>{{ commonCoverTitle(slot) }}</small></div>
                <div class="video-cover-upload-copy"><strong>{{ commonCoverTitle(slot) }} · {{ slotRatioLabel(slot) }}</strong><small>{{ coverUploads[commonCoverKey(slot)] ? `${coverUploads[commonCoverKey(slot)]?.filename} · ${formatSize(coverUploads[commonCoverKey(slot)]?.size_bytes || 0)}` : slot === 'original' && !mediaDimensions ? '选择视频后读取原始比例' : '尚未设置' }}</small></div>
                <div class="video-cover-actions">
                  <ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(coverUploading) || submitting || (slot === 'original' && !mediaDimensions)" @click="openCoverEditor(commonCoverKey(slot), slot)">{{ coverUploading === commonCoverKey(slot) ? '上传中…' : coverUploads[commonCoverKey(slot)] ? '重新裁剪' : '设置此比例' }}</ElButton>
                  <ElButton v-if="coverUploads[commonCoverKey(slot)]" class="text-button" native-type="button" :disabled="submitting" @click="removeCover(commonCoverKey(slot))">移除</ElButton>
                </div>
              </ElCard>
            </div>
          </section>
        </section>

        <section v-if="hasPublishMedia" class="panel video-compose-card" aria-labelledby="video-content-heading">
          <div class="video-section-heading">
            <div><span class="video-section-index">02</span><div><h2 id="video-content-heading">编辑发布内容</h2><p>{{ isEditMode ? '这些内容仅应用于当前账号，不会修改同任务的其他发布结果。' : '通用内容会应用到所有目标平台，可在右侧按平台设置覆盖项。' }}</p></div></div>
          </div>
          <label class="video-form-field">
            <span>视频标题 <b>*</b></span>
            <div class="video-counted-input"><ElInput v-model="title" class="input" maxlength="200" placeholder="写一个清晰、准确的视频标题" /><small>{{ title.length }} / 200</small></div>
          </label>
          <div class="video-form-field">
            <label for="video-topic-tags">话题标签</label>
            <div class="video-tag-input notranslate" :class="{ 'is-disabled': tags.length >= MAX_VIDEO_TAGS }" translate="no">
              <ElTag v-for="(tag, index) in tags" :key="`${tag}-${index}`" class="video-tag-chip notranslate" type="primary" effect="light" closable :aria-label="`删除标签 ${tag}`" @close="removeCommonTag(index)">{{ tag }}</ElTag>
              <ElInput id="video-topic-tags" :ref="setDouyinTopicInput" v-model="tagDraft" class="video-tag-entry notranslate" type="text" autocomplete="off" translate="no" :disabled="tags.length >= MAX_VIDEO_TAGS" :placeholder="tags.length >= MAX_VIDEO_TAGS ? '已达到 10 个标签上限' : '输入标签后按 Enter 创建'" @input="handleCommonTagsInput" @focus="reopenDouyinTopicPopover" @keydown="handleTagInputKeydown" />
            </div>
            <small>最多填写 10 个标签（<span class="video-tag-count">{{ tags.length }} / 10</span>）。{{ tagLimitHint }}</small>
            <small v-if="tagInputError" class="video-poi-error" role="alert">{{ tagInputError }}</small>
            <small v-if="!selectedAccounts.some(account => account.platform_id === 'douyin')" class="douyin-topic-hint">选择抖音发布账号后，输入标签可查看话题建议和浏览量。</small>
          </div>
          <label class="video-form-field">
            <span>视频简介</span>
            <ElInput v-model="description" class="input" type="textarea" :rows="5" maxlength="1000" placeholder="补充视频说明、背景或行动提示" />
            <small>{{ description.length }} / 1000</small>
          </label>
          <ElPopover :visible="showDouyinTopicPopover" :virtual-ref="douyinTopicInput || undefined" virtual-triggering placement="bottom-start" :fallback-placements="['top-start', 'bottom-end', 'top-end']" :width="360" :show-arrow="false" popper-class="douyin-topic-popper" :popper-style="{ maxHeight: '360px' }">
            <div class="douyin-poi-popover douyin-topic-popover" aria-live="polite">
              <div class="douyin-topic-heading"><strong>抖音话题建议</strong><span>浏览量</span></div>
              <small v-if="douyinTopicBusy" class="douyin-poi-message" role="status">正在查询抖音话题…</small>
              <small v-if="douyinTopicError" class="douyin-poi-message video-poi-error" role="alert">{{ douyinTopicError }}</small>
              <div v-if="douyinTopicResults.length" class="video-poi-results" aria-label="抖音话题搜索结果">
                <ElButton v-for="topic in douyinTopicResults" :key="topic.id" native-type="button" class="video-poi-option douyin-topic-option" @click="chooseDouyinTopic(topic)"><strong># {{ topic.name }}</strong><small>{{ topic.view_count_text }}</small></ElButton>
              </div>
              <small v-else-if="douyinTopicSearched && !douyinTopicBusy && !douyinTopicError" class="douyin-poi-message">没有找到匹配话题，仍可手动填写。</small>
            </div>
          </ElPopover>
          <div class="video-form-field video-schedule-field">
            <span>{{ isEditMode ? '本账号发布时间' : '发布时间' }}</span>
            <div class="video-schedule-row">
              <div class="douyin-choice-group" role="radiogroup" :aria-label="isEditMode ? '本账号发布时间' : '通用发布时间'">
                <ElRadioGroup v-model="scheduleMode" class="video-radio-group">
                  <ElRadio class="douyin-choice" value="immediate">立即发布</ElRadio>
                  <ElRadio class="douyin-choice" value="scheduled">定时发布</ElRadio>
                </ElRadioGroup>
              </div>
              <label v-if="scheduleMode === 'scheduled'" class="video-schedule-control">
                <span class="sr-only">计划发布时间</span>
                <input v-model="scheduleAt" class="input video-datetime-input" type="datetime-local" :min="minimumScheduleAt" aria-label="通用计划发布时间" @focus="refreshScheduleMinimum" @click="showDateTimePicker" />
                <small v-if="scheduleAt && scheduleAt < minimumScheduleAt" class="video-poi-error" role="alert">请选择晚于当前系统时间的发布时间。</small>
              </label>
            </div>
            <small v-if="scheduleMode === 'scheduled'">{{ isEditMode ? '立即发布会覆盖原任务的计划时间；定时发布使用此账号单独设置的时间。' : '默认适用于所有选中平台，可在右侧为各平台单独调整。抖音须选择 2 小时后、14 天内的时间。' }}</small>
            <small v-if="scheduleMode === 'scheduled' && unsupportedSchedules.length" class="video-poi-error">{{ unsupportedSchedules.map(platformName).join('、') }}暂不支持定时发布，请移除账号或选择立即发布。</small>
          </div>
        </section>
      </div>

      <div v-if="hasPublishMedia && loading" class="panel video-loading video-platform-loading" role="status">正在读取平台账号和素材…</div>

      <nav v-if="hasPublishMedia && !loading" class="video-platform-rail" aria-label="发布平台">
        <div class="video-platform-rail-heading"><h2>平台</h2><span>{{ selectedPlatforms.length }} 个平台已选</span></div>
        <ElTabs :model-value="activePlatform?.id || ''" class="video-platform-tabs" tab-position="left" aria-label="发布平台" @tab-change="activePlatformId = String($event)">
          <ElTabPane v-for="platform in visiblePlatforms" :key="platform.id" :name="platform.id">
            <template #label>
              <div class="video-platform-tab" :class="{ 'is-active': activePlatform?.id === platform.id, 'is-selected': platformSelectionCount(platform.id) > 0 }">
                <PlatformLogo class="video-platform-mark" :platform-id="platform.id" :name="platform.name" />
                <span class="video-platform-tab-copy"><strong>{{ platform.name }}</strong><small>{{ platformAccountSummary(platform.id) }}</small></span>
                <ElTag v-if="platformSelectionCount(platform.id)" class="video-platform-count" type="primary" effect="light" size="small">{{ platformSelectionCount(platform.id) }}</ElTag>
                <span v-else class="video-platform-dot" :class="{ 'is-ready': platformAccounts(platform.id).some((account) => account.status === 'connected') }"></span>
              </div>
            </template>
          </ElTabPane>
        </ElTabs>
        <p class="video-platform-rail-hint">{{ isEditMode ? '仅修改当前任务中的这个平台账号。' : '切换平台，分别选择账号并填写平台专属内容。' }}</p>
      </nav>

      <section v-if="hasPublishMedia && !loading && activePlatform" id="video-platform-panel" class="panel video-platform-settings" role="tabpanel" aria-labelledby="video-platform-panel-heading">
        <header class="video-platform-settings-header">
          <div class="video-platform-settings-title">
            <PlatformLogo class="video-platform-mark video-platform-mark-large" :platform-id="activePlatform.id" :name="activePlatform.name" />
            <div><h2 id="video-platform-panel-heading">{{ activePlatform.name }}</h2><span>{{ platformSelectionCount(activePlatform.id) }} 个发布账号已选</span></div>
          </div>
          <div v-if="!isEditMode" class="video-platform-settings-actions">
            <RouterLink class="text-button" :to="{ name: 'accounts', query: { tab: 'video', platform: activePlatform.id } }">管理账号</RouterLink>
            <ElButton text class="text-button" native-type="button" :disabled="!sau?.configured" @click="refreshAccounts">刷新账号</ElButton>
            <ElButton class="button button-secondary button-small" native-type="button" :disabled="!sau?.configured || !!accountBusy || !activePlatform.login_available" @click="addAccount(activePlatform.id)">＋ 添加账号</ElButton>
          </div>
        </header>
        <div class="video-platform-meta">
          <p v-if="activePlatform.id === 'douyin' && platformAccounts(activePlatform.id).some(account => account.status === 'connected') && !selectedAccounts.some(account => account.platform_id === activePlatform.id)" class="video-platform-description">勾选下方已连接的抖音账号后，在左侧输入话题时会显示抖音话题建议及浏览量。</p>
          <p v-else-if="activePlatform.id !== 'douyin' && platformAccounts(activePlatform.id).some(account => account.status === 'connected')" class="video-platform-description">勾选账号时，系统会核对其登录状态；发布结果还可能受平台规则和内容审核影响，请以平台反馈为准。</p>
          <p v-if="!activePlatform.login_available && !platformAccounts(activePlatform.id).some(account => account.status === 'connected') && platformAccounts(activePlatform.id).length > 0" class="video-platform-description" role="status">当前版本暂不支持添加或重新登录该平台账号。</p>
        </div>

        <div class="video-platform-accounts-scroll">
          <section class="video-settings-section" aria-labelledby="video-platform-accounts-heading">
            <div class="video-settings-section-heading"><h3 id="video-platform-accounts-heading">发布账号</h3><span>{{ platformSelectionCount(activePlatform.id) }} / {{ platformAccounts(activePlatform.id).length }} 个已选</span></div>
            <div v-if="isEditMode && editTarget" class="video-edit-account-lock">
              <PlatformLogo class="video-platform-mark" :platform-id="editTarget.platform_id" :name="platformName(editTarget.platform_id)" />
              <span><strong>{{ editTarget.account_name || editTarget.account_alias || '当前发布账号' }}</strong><small>仅修改该账号，其他平台账号及发布记录不受影响</small></span>
              <span class="video-edit-media-lock">账号固定</span>
            </div>
            <div v-else-if="platformAccounts(activePlatform.id).length" class="video-account-list">
              <ElCard v-for="account in platformAccounts(activePlatform.id)" :key="account.id" class="video-account-entry" :class="{ 'is-picked': selectedAccountIds.includes(account.id), 'is-checking': isAccountChecking(account) }" shadow="never">
                <div class="video-account-option">
                  <div class="video-account-choice">
                    <ElCheckbox class="video-account-checkbox" :model-value="selectedAccountIds.includes(account.id)" :aria-label="`选择账号 ${videoAccountName(account)}`" :disabled="account.status !== 'connected' || pendingAccountPreflightIds.has(account.id) || !sau?.configured" @change="toggleAccount(account, $event === true)" />
                    <ElAvatar v-if="account.avatar_url" class="video-account-avatar" :src="account.avatar_url" :size="36" fit="cover" />
                    <span class="video-account-label"><strong>{{ videoAccountName(account) }}</strong><small><template v-if="isAccountChecking(account)"><el-icon class="is-loading"><Loading /></el-icon><span class="account-status-checking">正在检查</span></template><template v-else-if="justConnectedIds.has(account.id)"><el-icon class="account-status-ok"><CircleCheck /></el-icon><span>已连接</span></template><template v-else>{{ accountStatusLabel[account.status] || account.status }}</template></small></span>
                  </div>
                  <div class="video-account-actions">
                    <ElButton v-if="account.status === 'connected' || account.status === 'error'" class="text-button" native-type="button" :disabled="accountBusy === account.id || pendingAccountPreflightIds.has(account.id)" @click="checkAccount(account)"><template v-if="accountCheckBusy === account.id"><el-icon class="is-loading"><Loading /></el-icon>检查中…</template><template v-else>{{ account.status === 'error' ? '重新检查' : '检查' }}</template></ElButton>
                    <ElButton v-if="account.status !== 'connected' && account.status !== 'login_in_progress'" class="text-button" native-type="button" :disabled="accountBusy === account.id || account.status === 'checking' || pendingAccountPreflightIds.has(account.id) || !sau?.configured || !activePlatform.login_available" @click="startLogin(account)"><template v-if="account.status === 'checking'"><el-icon class="is-loading"><Loading /></el-icon>正在检查</template><template v-else>{{ account.status === 'expired' || account.status === 'error' ? '重新登录' : '登录' }}</template></ElButton>
                    <ElButton v-if="account.status === 'login_in_progress'" class="text-button" native-type="button" :disabled="!!accountCancelBusy" @click="cancelAccountLogin(account)">{{ accountCancelBusy === account.id ? '正在取消…' : '取消登录' }}</ElButton>
                    <ElButton class="video-account-remove" native-type="button" :disabled="!!accountBusy || account.status === 'checking' || account.status === 'login_in_progress'" @click="logoutAccount(account)">{{ account.status === 'connected' ? '退出登录' : '移除账号' }}</ElButton>
                  </div>
                </div>
                <p v-if="account.duplicate_notice || (['error', 'expired'].includes(account.status) && account.message) || (account.status === 'not_connected' && account.message === '登录已取消。')" class="video-account-message" role="status">{{ account.duplicate_notice || account.message }}</p>
                <div v-if="account.status === 'login_in_progress'" class="video-login-qr" aria-label="账号登录二维码">
                  <div class="video-login-qr-image">
                    <img
                      :key="`${account.id}-${qrRefreshKey}`"
                      :src="api.videoPublishAccountLoginQrUrl(account.id, qrRefreshKey)"
                      alt="账号登录二维码"
                      :class="{ 'is-ready': qrImageReady[account.id] }"
                      @load="qrImageReady[account.id] = true"
                      @error="qrImageReady[account.id] = false"
                    />
                    <span v-if="!qrImageReady[account.id]">正在刷新二维码</span>
                  </div>
                  <LoginProgress :qr-ready="!!qrImageReady[account.id]" :in-page-only="['bilibili', 'wechat_channels'].includes(account.platform_id)" :waiting-hint="account.platform_id === 'wechat_channels' ? '正在连接官方登录页并获取二维码；遇到可恢复的加载异常时系统会尝试恢复一次。如仍未显示，可在登录窗口手动重试。' : undefined" />
                </div>
              </ElCard>
            </div>
            <ElEmpty v-else-if="!isEditMode" class="video-no-account" :description="activePlatform.login_available ? `暂无${activePlatform.name}账号，请先添加并完成登录。` : `当前版本暂不支持添加或登录${activePlatform.name}账号。`">
              <ElButton class="button button-secondary button-small" native-type="button" :disabled="!sau?.configured || !!accountBusy || !activePlatform.login_available" @click="addAccount(activePlatform.id)">添加账号并登录</ElButton>
            </ElEmpty>
          </section>
        </div>

        <div :key="`${activePlatform.id}-${platformSelectionCount(activePlatform.id) > 0}`" class="video-platform-settings-scroll">
          <section v-if="platformSelectionCount(activePlatform.id) > 0" class="video-settings-section video-platform-tuning" aria-labelledby="video-platform-fields-heading">
            <div class="video-settings-section-heading"><h3 id="video-platform-fields-heading">{{ activePlatform.name }}发布设置</h3><span>仅作用于当前平台</span></div>
            <section class="video-platform-cover-settings" :aria-labelledby="`video-${activePlatform.id}-cover-heading`">
              <div class="video-settings-section-heading"><h4 :id="`video-${activePlatform.id}-cover-heading`">视频封面</h4><span>{{ coverSlots(activePlatform.id).length > 1 ? '按平台比例设置' : '使用通用封面或单独裁剪' }}</span></div>
              <p class="video-cover-help">{{ coverHelp(activePlatform.id) }}</p>
              <label v-if="activePlatform.id === 'xiaohongshu'" class="video-cover-ratio-select"><span>封面比例</span><ElSelect v-model="xiaohongshuCoverRatio" class="input" aria-label="小红书封面比例"><ElOption label="竖版 · 3:4" value="portrait" /><ElOption label="横版 · 4:3" value="landscape" /></ElSelect></label>
              <label v-if="activePlatform.id === 'kuaishou'" class="video-cover-ratio-select"><span>封面比例</span><ElSelect v-model="kuaishouCoverRatio" class="input" aria-label="快手封面比例"><ElOption :label="`原始比例 · ${activeVideoRatioLabel}`" value="original" /><ElOption label="4:3" value="landscape" /><ElOption label="3:4" value="portrait" /><ElOption label="1:1" value="square" /><ElOption label="9:16" value="tall" /></ElSelect></label>
              <p v-if="activePlatform.id === 'wechat_channels' && !mediaDimensions" class="video-cover-direction-note">正在读取视频尺寸，以确定视频号封面位数量。</p>
              <div class="video-cover-grid" :class="{ 'is-single': coverSlots(activePlatform.id).length === 1 }">
                <ElCard v-for="slot in coverSlots(activePlatform.id)" :key="slot" class="video-cover-upload-card" shadow="never">
                  <ElImage v-if="coverPreviewFor(activePlatform.id, slot)" :src="coverPreviewFor(activePlatform.id, slot)" :alt="`${activePlatform.name} ${coverSlotRatio(slot)} 封面预览`" fit="cover" class="video-cover-preview" :class="{ 'is-portrait': slot === 'portrait', 'is-tall': slot === 'tall', 'is-landscape': slot === 'landscape', 'is-wide': slot === 'wide', 'is-common': slot === 'single' || slot === 'square' || slot === 'original' }" />
                  <div v-else class="video-cover-placeholder" :class="{ 'is-portrait': slot === 'portrait', 'is-tall': slot === 'tall', 'is-landscape': slot === 'landscape', 'is-wide': slot === 'wide', 'is-common': slot === 'single' || slot === 'square' || slot === 'original' }"><span>{{ coverSlotRatio(slot) }}</span><small>{{ coverSlotTitle(activePlatform.id, slot) }}</small></div>
                  <div class="video-cover-upload-copy"><strong>{{ coverSlotTitle(activePlatform.id, slot) }}</strong><small v-if="coverUploadLabel(activePlatform.id, slot)">{{ coverUploadLabel(activePlatform.id, slot) }}</small></div>
                  <div class="video-cover-actions">
                    <template v-if="activePlatform.id !== 'weibo'">
                      <ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(coverUploading) || submitting || (slot === 'original' && !mediaDimensions)" @click="openCoverEditor(coverKey(activePlatform.id, slot), slot, activePlatform.id)">{{ ownCoverUploadFor(activePlatform.id, slot) || editCoverFilenames[coverKey(activePlatform.id, slot)] ? '重新裁剪' : coverUploadFor(activePlatform.id, slot) ? '单独裁剪覆盖' : '选择图片或视频帧' }}</ElButton>
                    </template>
                    <template v-else>
                      <ElUpload :ref="(element) => setCoverUploadRef(coverKey(activePlatform.id, slot), element)" class="video-cover-upload-picker" :auto-upload="false" :show-file-list="false" :limit="1" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" :disabled="Boolean(coverUploading) || submitting" :on-change="(uploadFile) => onCoverUploadChange(coverKey(activePlatform.id, slot), uploadFile)">
                        <template #trigger><ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(coverUploading) || submitting">{{ coverUploading === coverKey(activePlatform.id, slot) ? '上传中…' : ownCoverUploadFor(activePlatform.id, slot) || editCoverFilenames[coverKey(activePlatform.id, slot)] ? '更换封面' : '上传封面' }}</ElButton></template>
                      </ElUpload>
                    </template>
                    <ElButton v-if="ownCoverUploadFor(activePlatform.id, slot)" class="text-button" native-type="button" :disabled="submitting" @click="removeCover(coverKey(activePlatform.id, slot))">改用通用封面</ElButton>
                  </div>
                </ElCard>
              </div>
            </section>
            <section v-if="activePlatform.id === 'xiaohongshu' && !isEditMode" class="douyin-section" aria-labelledby="xiaohongshu-publish-heading">
              <h4 id="xiaohongshu-publish-heading">发布时间</h4>
              <div class="douyin-form-row"><span>发布方式</span><div class="douyin-row-content">
                <ElRadioGroup v-model="scheduleMode" class="douyin-choice-group" aria-label="小红书发布方式">
                  <ElRadio value="immediate">立即发布</ElRadio>
                  <ElRadio value="scheduled">定时发布</ElRadio>
                </ElRadioGroup>
                <label v-if="scheduleMode === 'scheduled'" class="video-schedule-control">
                  <span>通用计划发布时间</span>
                  <input v-model="scheduleAt" class="input video-datetime-input" type="datetime-local" :min="minimumScheduleAt" aria-label="小红书设置中的通用计划发布时间" @focus="refreshScheduleMinimum" @click="showDateTimePicker" />
                  <small v-if="scheduleAt && scheduleAt < minimumScheduleAt" class="video-poi-error" role="alert">请选择晚于当前系统时间的发布时间。</small>
                </label>
                <small class="douyin-hint">发布方式与左侧通用设置同步，会影响所有已选平台。选择定时后，可在下方为小红书单独调整时间。</small>
              </div></div>
            </section>
            <template v-if="activePlatform.id === 'xiaohongshu'">
              <section class="douyin-section" aria-labelledby="xhs-content-settings-heading">
                <h4 id="xhs-content-settings-heading">内容设置</h4>
                <ElCheckbox v-model="overrideFor('xiaohongshu').xhsOriginal" class="video-override-checkbox">原创声明</ElCheckbox>
                <label class="video-form-field"><span>内容类型声明</span>
                  <ElSelect v-model="overrideFor('xiaohongshu').xhsDeclaration" class="input">
                    <ElOption value="" label="不添加" /><ElOption value="fiction" label="虚构演绎，仅供娱乐" /><ElOption value="ai" label="笔记含AI合成内容" /><ElOption value="advertising" label="内容包含营销广告" /><ElOption value="self_shot" label="内容来源声明：自主拍摄" /><ElOption value="repost" label="内容来源声明：来源转载" />
                  </ElSelect>
                  <small v-if="overrideFor('xiaohongshu').xhsDeclaration === 'self_shot'" class="video-poi-error" role="alert">小红书要求填写拍摄地点和拍摄日期，工作台暂不支持；请选择符合实际的其他声明或不添加。</small>
                  <small v-else>目前支持选择一项内容类型声明。</small>
                </label>
                <label v-if="overrideFor('xiaohongshu').xhsDeclaration === 'repost'" class="video-form-field"><span>转载媒体名称 <b>*</b></span><ElInput v-model="overrideFor('xiaohongshu').xhsRepostSource" class="input" maxlength="100" placeholder="填写来源媒体名称" /></label>
                <div class="video-form-field"><span>添加地点</span>
                  <div v-for="account in selectedAccounts.filter(item => item.platform_id === 'xiaohongshu')" :key="account.id" class="video-poi-account">
                    <small v-if="selectedAccounts.filter(item => item.platform_id === 'xiaohongshu').length > 1" class="douyin-account-name">{{ videoAccountName(account) }}</small>
                    <div class="video-poi-search-row"><ElInput v-model="xhsPoiQueries[account.id]" class="input douyin-input" type="search" maxlength="100" :aria-label="`${videoAccountName(account)} 的小红书地点关键词`" placeholder="输入城市或地点名称搜索" @input="queueXhsPoiSearch(account)" /><ElButton class="douyin-search-button" native-type="button" :disabled="!!xhsPoiBusy[account.id] || !(xhsPoiQueries[account.id] || '').trim() || !!xhsPoiSelections[account.id]" @click="runXhsPoiSearch(account)">搜索</ElButton></div>
                    <div v-if="!xhsPoiSelections[account.id] && (xhsPoiResults[account.id]?.length || xhsPoiBusy[account.id] || xhsPoiErrors[account.id] || xhsPoiSearched[account.id])" class="douyin-poi-popover">
                      <small v-if="xhsPoiBusy[account.id]" class="douyin-poi-message" role="status">正在查询小红书地点…</small>
                      <small v-if="xhsPoiErrors[account.id]" class="douyin-poi-message video-poi-error" role="alert">{{ xhsPoiErrors[account.id] }}</small>
                      <div v-if="xhsPoiResults[account.id]?.length" class="video-poi-results" aria-label="小红书地点搜索结果"><ElButton v-for="poi in xhsPoiResults[account.id]" :key="poi.id" native-type="button" class="video-poi-option" @click="chooseXhsPoi(account, poi)"><strong>{{ poi.name }}</strong><small>{{ poi.address }}</small></ElButton></div>
                      <small v-else-if="xhsPoiSearched[account.id] && !xhsPoiBusy[account.id] && !xhsPoiErrors[account.id]" class="douyin-poi-message">未找到地点，请换关键词重试。</small>
                    </div>
                    <div v-if="xhsPoiSelections[account.id]" class="video-poi-selected"><small>已选择：{{ xhsPoiSelections[account.id]?.name }} · {{ xhsPoiSelections[account.id]?.address }}</small><ElButton text class="text-button" native-type="button" @click="xhsPoiQueries[account.id] = ''; queueXhsPoiSearch(account)">清除</ElButton></div>
                  </div>
                  <small>地点来自所选小红书账号；输入城市加地点名称可缩小范围。</small>
                </div>
              </section>
              <section class="douyin-section" aria-labelledby="xhs-visibility-heading">
                <h4 id="xhs-visibility-heading">可见范围</h4>
                <label class="video-form-field"><span>谁可以看</span><ElSelect v-model="overrideFor('xiaohongshu').xhsVisibility" class="input"><ElOption value="" label="平台默认" /><ElOption value="public" label="公开可见" /><ElOption value="private" label="仅自己可见" /><ElOption value="friends" label="仅互关好友可见" /></ElSelect><small>“只给谁看”和“不给谁看”需在小红书选择指定用户，暂不支持。</small></label>
                <p class="video-platform-inherit-note">设置无法在平台页面确认时，任务会停止，不会按默认值继续发布。</p>
              </section>
            </template>
            <template v-if="activePlatform.id === 'douyin'">
              <label class="douyin-form-row">
                <span>自主声明</span>
                <ElSelect v-model="overrideFor(activePlatform.id).declaration" class="input douyin-input">
                  <ElOption value="内容由AI生成" label="内容由AI生成" /><ElOption value="内容为个人观点或见解" label="内容为个人观点或见解" /><ElOption value="内容为转载信息" label="内容为转载信息" /><ElOption value="内容含营销推广信息" label="内容含营销推广信息" /><ElOption value="虚构演绎，仅供娱乐" label="虚构演绎，仅供娱乐" /><ElOption value="无需添加自主声明" label="无需添加自主声明" />
                </ElSelect>
              </label>
              <template v-if="overrideFor('douyin').declaration === '内容为转载信息'">
                <label class="video-form-field">
                  <span>转载来源</span>
                  <ElSelect v-model="overrideFor('douyin').repostSource" class="input">
                    <ElOption value="" label="请选择取材来源" /><ElOption value="inside" label="取材站内" /><ElOption value="outside" label="取材站外" />
                  </ElSelect>
                  <small>取材站外无需填写其他内容；取材站内需从该账号的抖音观看历史选择原作品。</small>
                </label>
                <div v-if="overrideFor('douyin').repostSource === 'inside'" class="video-form-field">
                  <span>站内原作品</span>
                  <small v-if="!selectedAccounts.some(account => account.platform_id === 'douyin')">请先选择抖音发布账号。</small>
                  <div v-for="account in selectedAccounts.filter(item => item.platform_id === 'douyin')" :key="account.id" class="video-repost-account">
                    <strong>{{ videoAccountName(account) }}</strong>
                    <ElButton class="button button-secondary button-small" native-type="button" :disabled="!!repostHistoryBusy" @click="loadRepostHistory(account)">{{ repostHistoryBusy === account.id ? '读取中…' : '读取观看历史' }}</ElButton>
                    <ElSelect v-if="repostHistories[account.id]" v-model="repostSelectedIds[account.id]" class="input" :aria-label="`${videoAccountName(account)} 的原作品`">
                      <ElOption value="" label="请选择原作品" /><ElOption v-for="work in repostHistories[account.id]" :key="work.id" :value="work.id" :label="work.title" />
                    </ElSelect>
                  </div>
                </div>
              </template>
              <section class="douyin-section" aria-labelledby="douyin-extended-heading">
                <h4 id="douyin-extended-heading">扩展信息</h4>
                <div class="douyin-form-row">
                  <span>添加标签</span>
                  <div class="douyin-row-content">
                    <small v-if="!selectedAccounts.some(account => account.platform_id === 'douyin')" class="douyin-hint">请先选择抖音发布账号，再搜索位置。</small>
                    <div v-for="account in selectedAccounts.filter(item => item.platform_id === 'douyin')" :key="account.id" class="video-poi-account">
                      <small v-if="selectedAccounts.filter(item => item.platform_id === 'douyin').length > 1" class="douyin-account-name">{{ videoAccountName(account) }}</small>
                      <div class="video-poi-search-row">
                        <span class="douyin-location-type">位置</span>
                        <ElInput v-model="poiQueries[account.id]" class="input douyin-input" type="search" maxlength="100" :aria-label="`${videoAccountName(account)} 的地点关键词`" placeholder="输入地点名称，选择搜索结果" @input="queuePoiSearch(account)" />
                        <ElButton class="douyin-search-button" native-type="button" :disabled="!!poiBusy[account.id] || !(poiQueries[account.id] || '').trim() || !!poiSelections[account.id]" @click="runPoiSearch(account)">搜索</ElButton>
                      </div>
                      <div v-if="!poiSelections[account.id] && (poiCities[account.id]?.length || poiResults[account.id]?.length || poiBusy[account.id] || poiErrors[account.id] || poiSearched[account.id])" class="douyin-poi-popover">
                        <div v-if="poiCities[account.id]?.length" class="douyin-city-tabs" aria-label="搜索城市">
                          <ElButton v-for="city in poiCities[account.id]" :key="city.code" native-type="button" :class="{ 'is-active': poiCityCodes[account.id] === city.code }" :aria-pressed="poiCityCodes[account.id] === city.code" @click="poiCityCodes[account.id] = city.code; queuePoiSearch(account)">{{ city.name }}</ElButton>
                        </div>
                        <small v-if="poiBusy[account.id]" class="douyin-poi-message" role="status">正在查询抖音地点，请稍候…</small>
                        <small v-if="poiErrors[account.id]" class="douyin-poi-message video-poi-error" role="alert">{{ poiErrors[account.id] }}</small>
                        <div v-if="poiResults[account.id]?.length" class="video-poi-results" aria-label="地点搜索结果">
                          <ElButton v-for="poi in poiResults[account.id]" :key="poi.id" native-type="button" class="video-poi-option" @click="choosePoi(account, poi)"><strong>{{ poi.name }}</strong><small>{{ poi.address }}</small></ElButton>
                        </div>
                        <small v-else-if="poiSearched[account.id] && !poiBusy[account.id] && !poiErrors[account.id]" class="douyin-poi-message">未找到可确认的地点，请换关键词或城市。</small>
                      </div>
                      <div v-if="poiSelections[account.id]" class="video-poi-selected">
                        <span><small>已选择：{{ poiSelections[account.id]?.address || poiSelections[account.id]?.name }}</small></span>
                        <ElButton text class="text-button" native-type="button" @click="clearPoi(account)">清除</ElButton>
                      </div>
                    </div>
                    <small class="douyin-hint">地点来自抖音创作者中心，请从搜索结果中选择。</small>
                  </div>
                </div>
              </section>
              <section class="douyin-section" aria-labelledby="douyin-publish-heading">
                <h4 id="douyin-publish-heading">发布设置</h4>
                <div class="douyin-form-row"><span>谁可以看</span><ElRadioGroup v-model="overrideFor(activePlatform.id).visibility" class="douyin-choice-group" aria-label="谁可以看">
                  <ElRadio value="public">公开</ElRadio><ElRadio value="friends">好友可见</ElRadio><ElRadio value="private">仅自己可见</ElRadio><ElRadio value="">平台默认</ElRadio>
                </ElRadioGroup></div>
                <div class="douyin-form-row"><span>保存权限</span><ElRadioGroup :model-value="allowDownloadRadioValue" class="douyin-choice-group" aria-label="保存权限" @change="setAllowDownloadRadioValue">
                  <ElRadio :value="true">允许</ElRadio><ElRadio :value="false">不允许</ElRadio><ElRadio value="default">平台默认</ElRadio>
                </ElRadioGroup></div>
              </section>
              <p class="video-platform-inherit-note">关联热点目前没有稳定的 SAU 接口，暂不提供自动设置；话题标签仍可在左侧单独填写。</p>
            </template>
            <section v-if="scheduleMode === 'scheduled' && !isEditMode" class="douyin-section platform-schedule-section" aria-label="平台发布时间">
              <h4>平台发布时间</h4>
              <div v-if="activePlatform.schedule" class="douyin-form-row"><span>计划发布时间</span><div class="douyin-row-content">
                <input :value="platformSchedule(activePlatform.id)" class="input douyin-input video-datetime-input" type="datetime-local" :min="minimumScheduleAt" :aria-label="`${activePlatform.name}计划发布时间`" @input="setPlatformSchedule(activePlatform.id, $event)" @focus="refreshScheduleMinimum" @click="showDateTimePicker" />
                <small v-if="platformSchedule(activePlatform.id) && platformSchedule(activePlatform.id) < minimumScheduleAt" class="video-poi-error" role="alert">请选择晚于当前系统时间的发布时间。</small>
                <div class="platform-schedule-hint"><small class="douyin-hint">{{ hasPlatformSchedule(activePlatform.id) ? '已为该平台单独设置，适用于该平台所有选中账号。' : '跟随左侧通用时间，修改后仅影响当前平台。' }}</small><ElButton v-if="hasPlatformSchedule(activePlatform.id)" class="text-button" native-type="button" @click="delete platformSchedules[activePlatform.id]">恢复通用时间</ElButton></div>
              </div></div>
              <small v-else class="video-poi-error">{{ activePlatform.name }}暂不支持定时发布，请移除相关账号或在左侧选择立即发布。</small>
            </section>
            <template v-if="activePlatform.id === 'wechat_channels'">
              <label class="video-form-field">
                <span>视频号短标题</span>
                <ElInput v-model="overrideFor(activePlatform.id).shortTitle" class="input" maxlength="15" placeholder="留空时根据通用标题自动整理" />
                <small>发布器会按视频号要求整理到 7–15 个字符。</small>
              </label>
              <ElCheckbox v-model="overrideFor(activePlatform.id).saveAsDraft" class="video-override-checkbox">保存为草稿</ElCheckbox>
              <p v-if="overrideFor(activePlatform.id).saveAsDraft" class="video-platform-inherit-note">该账号的作品将保存至视频号草稿箱，不会立即发布；草稿模式不能与定时发布同时使用。</p>
            </template>
            <label v-if="['kuaishou', 'wechat_channels'].includes(activePlatform.id)" class="video-form-field">
              <span>添加到合集</span>
              <ElInput v-model="overrideFor(activePlatform.id).collectionName" class="input" maxlength="100" placeholder="可选；填写该账号中已存在的合集名称" />
              <small>合集不存在或账号暂不支持时，发布器会跳过归集并继续发布。</small>
            </label>
            <label v-if="activePlatform.id === 'baijiahao'" class="video-form-field">
              <span>创作声明 <b>*</b></span>
              <ElSelect v-model="overrideFor('baijiahao').declaration" class="input" required>
                <ElOption value="" label="请选择符合视频内容的声明" />
                <ElOption v-for="statement in baijiahaoDeclarations" :key="statement" :value="statement" :label="statement" />
              </ElSelect>
            </label>
            <template v-if="activePlatform.id === 'bilibili'">
              <label class="video-form-field"><span>投稿分区 <b>*</b></span>
                <ElSelect v-model="overrideFor('bilibili').tid" class="input" :disabled="bilibiliCategoriesLoading || !bilibiliCategories.length">
                  <ElOption value="" :label="bilibiliCategoriesLoading ? '正在获取B站分区…' : '请选择投稿分区'" />
                  <ElOption v-for="item in bilibiliCategories" :key="item.id" :value="String(item.id)" :label="item.name" />
                </ElSelect>
              </label>
              <small class="video-platform-inherit-note">分区按所选 B 站账号记住；多账号发布时使用相同分区，提交前逐个核验。</small>
              <p v-if="bilibiliCategoriesError" class="video-poi-error" role="alert">{{ bilibiliCategoriesError }} <ElButton text class="text-button" native-type="button" @click="loadBilibiliCategories(selectedAccounts.find(a => a.platform_id === 'bilibili')?.id || '')">重试</ElButton></p>
              <label class="video-form-field"><span>创作声明 <b>*</b></span>
                <ElSelect v-model="overrideFor('bilibili').bilibiliStatement" class="input" :disabled="bilibiliStatementsLoading || !bilibiliStatements.length">
                  <ElOption value="" :label="bilibiliStatementsLoading ? '正在获取B站创作声明…' : '请选择符合视频内容的创作声明'" />
                  <ElOption v-for="item in bilibiliStatements" :key="item.id" :value="String(item.id)" :disabled="![-2, -1, 1, 2, 3, 4].includes(item.id)" :label="`${item.name}${![-2, -1, 1, 2, 3, 4].includes(item.id) ? '（暂不支持）' : ''}`" />
                </ElSelect>
              </label>
              <p v-if="bilibiliStatementsError" class="video-poi-error" role="alert">{{ bilibiliStatementsError }} <ElButton text class="text-button" native-type="button" @click="loadBilibiliStatements(selectedAccounts.find(a => a.platform_id === 'bilibili')?.id || '')">重试</ElButton></p>
              <label v-if="overrideFor('bilibili').bilibiliStatement === '-2'" class="video-form-field"><span>转载来源 <b>*</b></span><ElInput v-model="overrideFor('bilibili').bilibiliSource" class="input" maxlength="500" placeholder="填写原作者及原视频链接" /></label>
              <ElCheckbox v-else v-model="overrideFor('bilibili').bilibiliNoReprint" class="video-override-checkbox">内容为自制：未经作者允许，禁止转载（可选）</ElCheckbox>
              <ElCheckbox v-model="overrideFor('bilibili').bilibiliPrivate" class="video-override-checkbox">仅自己可见</ElCheckbox>
              <label class="video-form-field"><span>粉丝动态</span><ElInput v-model="overrideFor('bilibili').bilibiliDynamic" class="input" type="textarea" :rows="2" maxlength="233" placeholder="可选，最多 233 字" /></label>
              <p class="video-platform-inherit-note">商业推广、章节、字幕等请在 B 站创作中心设置。</p>
            </template>
            <template v-if="!isEditMode">
              <ElCheckbox v-model="overrideFor(activePlatform.id).enabled" class="video-override-checkbox">为该平台单独设置标题、简介或标签</ElCheckbox>
              <p v-if="!overrideFor(activePlatform.id).enabled" class="video-platform-inherit-note">当前使用左侧填写的通用标题、简介和标签。</p>
            </template>
            <template v-if="!isEditMode && overrideFor(activePlatform.id).enabled">
              <label class="video-form-field"><span>平台标题</span><ElInput v-model="overrideFor(activePlatform.id).title" class="input" maxlength="200" placeholder="留空则使用通用标题" /></label>
              <label class="video-form-field"><span>平台简介</span><ElInput v-model="overrideFor(activePlatform.id).description" class="input" type="textarea" :rows="3" maxlength="5000" placeholder="留空则使用通用简介" /></label>
              <label class="video-form-field"><span>平台标签</span><ElInput v-model="overrideFor(activePlatform.id).tags" class="input" placeholder="留空则使用通用标签" @input="handlePlatformTagsInput(activePlatform.id)" /><small>最多填写 10 个标签。</small></label>
            </template>
          </section>
        </div>
      </section>
    </div>

    <footer v-if="hasPublishMedia && !loading" class="video-create-footer">
      <div><strong>{{ isEditMode ? '仅重试当前账号' : `${readyAccountCount} 个账号目标` }}</strong><span>{{ activeMediaName || '尚未选择视频素材' }} · {{ scheduleMode === 'scheduled' ? (scheduleAt ? `定时 ${scheduleAt.replace('T', ' ')}${!isEditMode && selectedPlatforms.some(id => hasPlatformSchedule(id)) ? '（部分平台单独设置）' : ''}` : '待选择定时时间') : '立即发布' }}</span></div>
      <div class="video-create-footer-actions">
        <div v-if="error" :key="footerErrorAnimationKey" class="video-create-footer-error" role="alert">{{ error }}</div>
        <div class="video-create-footer-buttons"><ElButton class="button button-secondary" native-type="button" :disabled="submitting || savingDraft" @click="cancel">{{ isEditMode ? '返回任务详情' : '取消' }}</ElButton><ElButton v-if="!isEditMode" class="button button-secondary" native-type="button" :disabled="submitting || savingDraft || !!coverUploading" @click="saveDraft">{{ savingDraft ? '正在保存…' : activeDraftId ? '更新草稿' : '保存草稿' }}</ElButton><ElButton class="button" native-type="button" :disabled="submitting || savingDraft || !sau?.configured" @click="submitBatch">{{ submitting ? '正在创建…' : isEditMode ? '保存修改并重试' : '创建发布任务' }}</ElButton></div>
      </div>
    </footer>

    <!-- 素材库选择弹窗暂时停用，保留原实现供后续恢复。作者：zhw，时间：2026-11-08 22:53:04（Asia/Shanghai）
    <ElDialog v-model="libraryOpen" class="video-library-dialog" width="min(620px, calc(100% - 36px))" :show-close="false" :append-to-body="true" @close="handleLibraryDialogClose">
      <template #header>
        <header class="video-library-dialog-header"><div><h2 id="video-library-title">从素材库选择视频</h2><p>只显示已归档且仍可读取的视频文件。</p></div><ElButton text class="video-close-button" native-type="button" aria-label="关闭" @click="libraryOpen = false">×</ElButton></header>
      </template>
      <ElEmpty v-if="!mediaItems.length" class="video-library-empty" description="素材库中还没有可用视频。你可以从本地上传视频。" />
      <ElButton v-for="media in mediaItems" :key="media.asset_id" class="video-library-item" native-type="button" @click="selectLibraryMedia(media)">
        <span class="video-selected-media-mark" aria-hidden="true">▶</span><span><strong>{{ media.name }}</strong><small>{{ media.title }} · {{ formatSize(media.size_bytes) }}</small></span><em>选择</em>
      </ElButton>
    </ElDialog>
    -->

    <VideoCoverCropEditor
      :open="coverEditorOpen"
      :title="coverEditorTitle()"
      :aspect-ratio="coverEditorRatio"
      :video-src="activeMediaVideoSource"
      :initial-image-src="coverEditorImageSrc"
      :initial-image-name="coverEditorImageName"
      :disabled="submitting"
      @close="coverEditorOpen = false"
      @save="saveEditedCover"
    />

  </section>
</template>

<style scoped>
.video-create-page { display: grid; gap: 14px; min-width: 0; padding-bottom: 82px; }
.video-setup-card { display: grid; grid-template-columns: auto minmax(200px, 1fr) minmax(300px, .9fr); align-items: center; gap: 12px; padding: 14px; border: 1px solid #d9e4d9; border-radius: 13px; background: #f6faf5; }
.video-setup-icon { display: grid; width: 36px; height: 36px; place-items: center; border-radius: 10px; background: #e4eee3; color: var(--pine); font-size: 17px; }
.video-setup-copy strong { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.video-setup-copy p { margin: 3px 0 0; color: var(--muted); font-size: var(--fs-body-sm); }
.video-setup-input-row { display: flex; gap: 7px; }
.video-setup-input-row label { flex: 1; min-width: 0; }
.video-setup-input-row .input { width: 100%; min-height: 38px; font-size: var(--fs-body); }
.video-setup-card > small { grid-column: 2 / -1; color: var(--muted); font-size: var(--fs-caption); }
.video-loading { padding: 24px; color: var(--muted); }
.video-create-workspace { display: grid; grid-template-columns: minmax(320px, .95fr) 190px minmax(380px, 1.1fr); height: min(820px, calc(100vh - 185px)); min-height: 560px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--paper); box-shadow: 0 3px 16px rgb(24 39 75 / 5%); }
.video-create-page.is-upload-entry { padding-bottom: 0; }
.video-create-workspace.is-upload-entry { grid-template-columns: minmax(0, 1fr); height: min(820px, calc(100vh - 185px)); min-height: 440px; overflow: visible; border: 0; border-radius: 0; background: transparent; box-shadow: none; }
.video-create-workspace.is-upload-entry .video-compose-column { display: block; height: 100%; overflow: visible; padding: 0; border: 0; background: transparent; }
.video-create-workspace.is-upload-entry .video-compose-card { min-height: 100%; padding: 20px; border: 1px solid var(--line); border-radius: 12px; background: var(--paper); box-shadow: 0 3px 16px rgb(24 39 75 / 5%); }
.video-create-workspace.is-upload-entry .video-section-heading { margin-bottom: 18px; }
.video-create-workspace.is-upload-entry .video-selected-media { min-height: min(54vh, 420px); flex-direction: column; justify-content: center; border-style: dashed; background: #fbfcfd; text-align: center; }
.video-create-workspace.is-upload-entry .video-selected-media > div:nth-child(2) { flex: 0 1 auto; }
.video-create-workspace.is-upload-entry .video-selected-media > button { width: min(280px, 100%); margin-left: 0; }
.video-platform-loading { grid-column: 2 / -1; display: grid; min-height: 220px; place-items: center; }
.video-compose-column, .video-platform-rail, .video-platform-settings { min-width: 0; min-height: 0; }
.video-compose-column { display: grid; align-content: start; gap: 12px; height: 100%; overflow: auto; padding: 14px; border-right: 1px solid var(--line); background: #fff; }
.video-compose-card { min-width: 0; padding: 3px 2px 14px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: transparent; box-shadow: none; }
.video-compose-card:last-child { border-bottom: 0; }
.video-section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.video-section-heading > div { display: flex; align-items: flex-start; gap: 10px; }
.video-section-index { display: grid; width: 25px; height: 25px; flex: 0 0 auto; place-items: center; border-radius: 8px; background: var(--pine-soft); color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.video-section-heading h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.video-section-heading p { margin: 3px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.video-source-toggle { display: flex; gap: 3px; padding: 3px; border-radius: 8px; background: #f1f1eb; }
.video-source-toggle button { min-height: 27px; padding: 0 9px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: var(--fs-caption); cursor: pointer; }
.video-source-toggle button.active { background: #fff; color: var(--pine); box-shadow: 0 1px 4px #20352d14; font-weight: var(--fw-bold); }
.video-selected-media { display: flex; align-items: center; gap: 10px; min-height: 67px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: #fbfaf6; }
.video-selected-media.is-readonly { background: #f7f9fb; }
.video-edit-media-lock { flex: 0 0 auto; padding: 4px 7px; border: 1px solid #dce5ee; border-radius: 4px; background: #fff; color: #65798f; font-size: var(--fs-caption); }
.video-edit-return { display: flex; justify-content: flex-end; }
.video-selected-media > div:nth-child(2) { display: grid; flex: 1; min-width: 0; gap: 3px; }
.video-selected-media strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-selected-media span:not(.video-selected-media-mark) { color: var(--muted); font-size: var(--fs-caption); }
.video-selected-media-mark { display: grid; width: 36px; height: 36px; flex: 0 0 auto; place-items: center; border-radius: 9px; background: #e9eee6; color: var(--pine); font-size: var(--fs-body-sm); }
.video-media-summary { display: flex; align-items: center; gap: 9px; margin-top: 11px; padding: 9px 11px; border-radius: 9px; background: #eef5ee; }
.video-media-first-frame { display: grid; width: 58px; height: 74px; flex: 0 0 auto; place-items: center; overflow: hidden; margin: 0; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
.video-media-first-frame img { max-width: 100%; max-height: 100%; object-fit: contain; }
.video-media-first-frame span { padding: 4px; color: var(--muted); font-size: var(--fs-caption); text-align: center; }
.video-media-check { display: grid; width: 21px; height: 21px; place-items: center; border-radius: 50%; background: #dcebdd; color: #3e754b; font-size: var(--fs-body-sm); }
.video-media-summary-copy { display: grid; gap: 3px; min-width: 0; }
.video-media-summary strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-media-summary-copy span { overflow: hidden; color: var(--muted); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.video-form-field { display: grid; gap: 6px; margin: 12px 0; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-form-field:first-of-type { margin-top: 0; }
.video-form-field b { color: #a64e42; }
.video-form-field .input { width: 100%; min-width: 0; }
.video-form-field textarea { resize: vertical; }
.video-form-field small { color: var(--muted); font-size: var(--fs-caption); font-weight: 400; }
.video-tag-input { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 46px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 10px; background: var(--paper); transition: border-color .18s ease, box-shadow .18s ease; }
.video-tag-input:focus-within:not(.is-disabled) { border-color: var(--pine); box-shadow: 0 0 0 3px var(--pine-soft); }
.video-tag-input.is-disabled { background: var(--paper-2); }
.video-tag-chip { display: inline-flex; max-width: 100%; align-items: center; gap: 6px; min-height: 32px; padding: 0 9px; border: 0; border-radius: 6px; background: var(--pine-soft); color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); cursor: pointer; overflow-wrap: anywhere; }
.video-tag-chip span { flex: 0 0 auto; color: var(--text-tertiary); font-size: var(--fs-body); line-height: 1; }
.video-tag-entry { flex: 1 1 160px; min-width: min(160px, 100%); min-height: 32px; padding: 4px 2px; border: 0; outline: 0; background: transparent; color: var(--text-primary); font-size: var(--fs-body); }
.video-tag-entry:focus-visible { outline: 0; }
.video-tag-entry::placeholder { color: var(--text-placeholder); }
.video-tag-entry:disabled { cursor: not-allowed; }
.video-tag-count { font-variant-numeric: tabular-nums; }
.video-repost-account { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; padding: 8px; border: 1px solid var(--line); border-radius: 6px; }
.video-repost-account select { grid-column: 1 / -1; }
.video-poi-account { display: grid; gap: 7px; padding: 8px; border: 1px solid var(--line); border-radius: 6px; }
.video-poi-search-row { display: flex; gap: 6px; min-width: 0; }
.video-poi-search-row .input { flex: 1; }
.video-poi-city { display: flex; gap: 7px; align-items: center; }
.video-poi-city .input { flex: 1; }
.video-poi-results { display: grid; max-height: 190px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px; }
.video-poi-option { display: grid; gap: 3px; padding: 8px; text-align: left; background: transparent; border: 0; border-bottom: 1px solid var(--line); cursor: pointer; }
.video-poi-option:hover, .video-poi-option:focus-visible { background: var(--paper-2, #f4f7ff); }
.video-poi-selected { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.video-poi-selected span { display: grid; gap: 3px; }
.video-poi-error { color: #a64e42 !important; }
.video-counted-input { position: relative; }
.video-counted-input .input { width: 100%; padding-right: 54px; }
.video-counted-input small { position: absolute; top: 50%; right: 10px; transform: translateY(-50%); color: var(--muted); font-size: var(--fs-caption); font-weight: 400; }
.video-schedule-field { max-width: 100%; }
.video-schedule-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.video-schedule-control { display: grid; flex: 1 1 180px; max-width: 320px; min-width: min(180px, 100%); gap: 6px; }
.video-schedule-control .input { width: 100%; min-width: 0; }
.video-datetime-input { cursor: pointer; }
.platform-schedule-hint { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.platform-schedule-hint .text-button { font-size: var(--fs-body-sm); }
.video-platform-rail { display: flex; flex-direction: column; overflow: auto; border-right: 1px solid var(--line); background: #f8fafc; }
.video-platform-rail-heading { display: flex; align-items: center; justify-content: space-between; gap: 5px; padding: 16px 10px 11px; }
.video-platform-rail-heading h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.video-platform-rail-heading > span { color: var(--muted); font-size: var(--fs-caption); white-space: nowrap; }
.video-platform-list { display: grid; gap: 4px; padding: 0 7px; }
.video-platform-tab { position: relative; display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 8px; width: 100%; min-height: 53px; padding: 7px 8px; border: 1px solid transparent; border-radius: 8px; color: var(--ink); background: transparent; text-align: left; cursor: pointer; }
.video-platform-tab:hover { background: #edf3f7; }
.video-platform-tab.is-active { border-color: #d8e6fb; background: #edf4ff; }
.video-platform-tab.is-active::before { position: absolute; top: 9px; bottom: 9px; left: -1px; width: 3px; border-radius: 3px; background: #3977eb; content: ''; }
.video-platform-mark { display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto; overflow: hidden; border: 1px solid #e7ebf0; border-radius: 8px; background: #fff; }
.video-platform-mark-large { width: 36px; height: 36px; border-radius: 10px; }
.video-platform-tab-copy { display: grid; gap: 4px; min-width: 0; }
.video-platform-tab-copy strong { overflow: hidden; font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-platform-tab-copy small { overflow: hidden; color: var(--muted); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.video-platform-count { min-width: 19px; padding: 3px 5px; border-radius: 999px; color: #2462c9; background: #dfeaff; font-size: var(--fs-caption); text-align: center; }
.video-platform-dot { width: 7px; height: 7px; border-radius: 50%; background: #bcc5d0; }
.video-platform-dot.is-ready { background: #31a874; }
.video-platform-rail-hint { margin: auto 11px 12px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: var(--fs-caption); line-height: 1.55; }
.video-platform-settings { display: grid; grid-template-rows: auto auto auto minmax(0, 1fr); background: #fff; }
.video-platform-settings-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px 10px; }
.video-platform-settings-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.video-platform-settings-title > div { display: grid; gap: 5px; min-width: 0; }
.video-platform-settings-title h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.video-platform-settings-title > div > span { color: var(--muted); font-size: var(--fs-caption); }
.video-platform-settings-actions { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.video-platform-settings-actions .text-button { font-size: var(--fs-caption); }
.video-platform-meta { min-width: 0; }
.video-platform-description { margin: 0; padding: 0 18px 12px; color: var(--muted); font-size: var(--fs-body-sm); line-height: 1.55; }
.video-platform-accounts-scroll { min-height: 0; max-height: min(480px, 50vh); overflow: auto; padding: 0 18px; scrollbar-gutter: stable; }
.video-platform-settings-scroll { min-height: 0; overflow: auto; padding: 0 18px 18px; }
.video-settings-section { display: grid; gap: 10px; padding: 15px 0; border-top: 1px solid var(--line); }
.video-settings-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.video-settings-section-heading h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.video-settings-section-heading h4 { margin: 0; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-settings-section-heading > span { color: var(--muted); font-size: var(--fs-caption); }
.video-common-cover-settings, .video-platform-cover-settings { display: grid; gap: 9px; padding: 11px; border: 1px solid var(--line); border-radius: 10px; background: #fbfcfa; }
.video-cover-help { margin: 0; color: var(--muted); font-size: var(--fs-caption); line-height: 1.55; }
.video-cover-ratio-select { display: grid; grid-template-columns: 90px minmax(0, 260px); align-items: center; gap: 8px; color: var(--ink); font-size: var(--fs-body-sm); }
.video-cover-ratio-select .input { min-height: 34px; font-size: var(--fs-body-sm); }
.video-cover-direction-note { margin: 0; color: var(--muted); font-size: var(--fs-caption); }
.video-cover-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.video-common-cover-grid { align-items: stretch; }
.video-cover-grid.is-single { grid-template-columns: minmax(0, 1fr); }
.video-cover-upload-card { display: grid; grid-template-columns: 54px minmax(0, 1fr); align-items: center; gap: 7px; min-width: 0; padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.video-cover-preview, .video-cover-placeholder { display: grid; width: 54px; height: 70px; grid-row: span 2; place-items: center; overflow: hidden; border-radius: 6px; background: #f0f2ee; object-fit: cover; }
.video-cover-preview.is-landscape, .video-cover-placeholder.is-landscape { width: 54px; height: 41px; justify-self: center; }
.video-cover-preview.is-wide, .video-cover-placeholder.is-wide { width: 54px; height: 30px; justify-self: center; }
.video-cover-preview.is-tall, .video-cover-placeholder.is-tall { width: 40px; height: 70px; justify-self: center; }
.video-cover-preview.is-common, .video-cover-placeholder.is-common { width: 54px; height: 54px; }
.video-cover-placeholder { align-content: center; gap: 3px; color: var(--muted); }
.video-cover-placeholder span { color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-bold); }
.video-cover-placeholder small { font-size: var(--fs-caption); }
.video-cover-upload-copy { display: grid; min-width: 0; gap: 4px; }
.video-cover-upload-copy strong { color: var(--ink); font-size: var(--fs-caption); }
.video-cover-upload-copy small { overflow: hidden; color: var(--muted); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.video-cover-actions { display: flex; grid-column: 2; align-items: center; gap: 8px; min-width: 0; }
.video-cover-actions .button { min-height: 27px; padding: 4px 8px; font-size: var(--fs-caption); }
.video-cover-actions .text-button { font-size: var(--fs-caption); }
.video-cover-input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }
.video-account-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 10px; }
.video-edit-account-lock { display: flex; align-items: center; gap: 10px; min-height: 58px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: #f7f9fb; }
.video-edit-account-lock > span:nth-child(2) { display: grid; flex: 1; min-width: 0; gap: 3px; }
.video-edit-account-lock strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-edit-account-lock small { color: var(--muted); font-size: var(--fs-caption); }
.video-account-entry { display: grid; align-content: start; gap: 9px; min-width: 0; min-height: 92px; padding: 11px; border: 1px solid var(--line); border-radius: 10px; background: #fff; box-shadow: 0 1px 3px rgb(24 39 75 / 4%); }
.video-account-entry.is-picked { border-color: #94b5f1; background: #f8fbff; }
.video-account-option { display: grid; grid-template-columns: minmax(0, 1fr); align-items: stretch; gap: 9px; min-height: 0; }
.video-account-choice { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; cursor: pointer; }
.video-account-choice > input { flex: 0 0 auto; width: 16px; height: 16px; accent-color: #3274e8; }
.video-account-choice > input:disabled { cursor: not-allowed; }
.video-account-avatar { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; }
.video-account-label { display: grid; min-width: 0; gap: 3px; }
.video-account-label strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-account-label small { color: var(--muted); font-size: var(--fs-caption); display: inline-flex; align-items: center; gap: 4px; }
.video-account-label small .el-icon { flex: 0 0 auto; width: 14px; height: 14px; font-size: 14px; }
.account-status-checking { color: var(--pine); font-weight: 550; }
.account-status-ok { color: var(--success); font-weight: 550; }
.video-account-entry.is-checking { border-color: #94b5f1; }
@media (prefers-reduced-motion: no-preference) {
  .video-account-entry.is-checking { animation: account-check-breath 1.8s ease-out infinite; }
  .is-checking .video-account-avatar { animation: avatar-check-breath 1.8s ease-out infinite; }
  .account-status-ok .el-icon { animation: status-pop-in .32s cubic-bezier(.34, 1.56, .64, 1); }
}
@keyframes account-check-breath { 0% { box-shadow: 0 0 0 0 rgb(40 99 232 / 20%), 0 1px 3px rgb(24 39 75 / 4%); } 70%, 100% { box-shadow: 0 0 0 6px rgb(40 99 232 / 0%), 0 1px 3px rgb(24 39 75 / 4%); } }
@keyframes avatar-check-breath { 0% { box-shadow: 0 0 0 0 rgb(40 99 232 / 35%); } 70%, 100% { box-shadow: 0 0 0 5px rgb(40 99 232 / 0%); } }
@keyframes status-pop-in { from { transform: scale(.4); opacity: 0; } to { transform: scale(1); opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .video-account-entry .el-icon.is-loading { animation: none; } }
.video-account-actions { display: flex; align-items: center; justify-content: flex-end; gap: 7px; width: 100%; padding-top: 7px; border-top: 1px solid var(--line); }
.video-account-actions .text-button { font-size: var(--fs-caption); display: inline-flex; align-items: center; gap: 4px; }
.video-account-actions .text-button .el-icon { width: 13px; height: 13px; font-size: 13px; }
.video-no-account { display: grid; justify-items: start; gap: 7px; padding: 14px; border: 1px dashed var(--line); border-radius: 9px; background: #fafbfd; }
.video-no-account strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-no-account span { color: var(--muted); font-size: var(--fs-caption); }
.video-login-qr { display: flex; align-items: center; gap: 12px; margin: 5px 0 8px; padding: 10px; border: 1px solid #d8e2d7; border-radius: 9px; background: #f7faf5; }
.video-login-qr-image { position: relative; display: grid; width: 144px; min-height: 144px; flex: 0 0 144px; place-items: center; overflow: hidden; border-radius: 7px; background: white; }
.video-login-qr-image img { display: none; width: 132px; height: 132px; object-fit: contain; }
.video-login-qr-image img.is-ready { display: block; }
.video-login-qr-image span { padding: 12px; color: var(--muted); font-size: var(--fs-caption); text-align: center; }
.video-account-remove { flex: 0 0 auto; border: 0; background: transparent; color: #9b8279; font-size: var(--fs-body-sm); cursor: pointer; }
.video-account-message { margin: 3px 0 8px; color: var(--danger); font-size: var(--fs-body-sm); line-height: 1.5; }
.video-account-remove:hover { background: #f4e9e5; color: #964f43; }
.video-platform-tuning { gap: 10px; }
.video-platform-tuning .video-form-field { margin: 0; }
.douyin-section { display: grid; gap: 20px; margin: 4px -8px 0; padding: 20px 16px; border-radius: 8px; background: #f9f9fa; }
.douyin-section h4 { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.douyin-form-row { display: grid; grid-template-columns: 76px minmax(0, 1fr); align-items: start; gap: 10px; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.douyin-form-row > span { padding-top: 10px; }
.douyin-row-content { display: grid; min-width: 0; gap: 8px; }
.douyin-form-row .douyin-input { width: 100%; min-width: 0; min-height: 36px; border: 1px solid transparent; border-radius: 4px; background: #f0f1f3; color: #23252b; font-size: var(--fs-body); font-weight: var(--fw-regular); }
.douyin-form-row .douyin-input:focus { border-color: #315cff; outline: 2px solid rgba(49, 92, 255, .12); }
.douyin-form-row > .douyin-input { background: #f0f1f3; }
.douyin-hint, .douyin-account-name { color: #858991; font-size: var(--fs-body-sm); font-weight: 400; line-height: 1.5; }
.video-poi-account { gap: 4px; padding: 0; border: 0; border-radius: 0; }
.video-poi-search-row { align-items: stretch; gap: 6px; }
.douyin-location-type { display: flex; flex: 0 0 72px; align-items: center; padding: 0 12px; border-radius: 4px; background: #f0f1f3; color: #30343b; font-weight: 400; }
.douyin-search-button { padding: 0 10px; border: 0; border-radius: 4px; background: #f0f1f3; color: #40444b; cursor: pointer; }
.douyin-search-button:disabled { color: #a4a6aa; cursor: default; }
.douyin-poi-popover { min-width: 0; padding: 4px 0; border: 1px solid #eaebed; border-radius: 4px; background: #fff; box-shadow: 0 5px 18px rgba(18, 22, 33, .1); }
.douyin-topic-popover { box-sizing: border-box; width: 100%; max-height: 360px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; }
.douyin-topic-heading { display: flex; justify-content: space-between; gap: 12px; padding: 8px 12px; border-bottom: 1px solid var(--line); color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.douyin-topic-heading span { color: var(--text-tertiary); font-weight: var(--fw-regular); }
.douyin-topic-popover .video-poi-results { max-height: none; overflow: visible; }
.douyin-topic-heading { position: sticky; top: -4px; z-index: 1; background: var(--paper); }
.douyin-topic-option { display: flex; min-height: 44px; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; }
.douyin-topic-popover .douyin-topic-option + .douyin-topic-option { margin-left: 0; }
.douyin-topic-option strong { min-width: 0; overflow-wrap: anywhere; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.douyin-topic-option small { flex: 0 0 auto; color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.douyin-topic-hint { color: var(--text-tertiary) !important; font-size: var(--fs-body-sm) !important; font-weight: var(--fw-regular) !important; }
.douyin-city-tabs { display: flex; gap: 14px; overflow-x: auto; padding: 8px 12px 0; border-bottom: 1px solid #f0f0f1; }
.douyin-city-tabs button { flex: none; padding: 0 0 8px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: #858991; cursor: pointer; font-size: var(--fs-body-sm); }
.douyin-city-tabs button.is-active { border-bottom-color: #20232a; color: #20232a; }
.douyin-poi-message { display: block; padding: 10px 12px; color: #858991; font-size: var(--fs-body-sm); font-weight: 400; }
.video-poi-results { max-height: 190px; border: 0; border-radius: 0; }
.video-poi-option { padding: 10px 12px; border-bottom: 0; color: #30343b; font-size: var(--fs-body-sm); }
.video-poi-option small, .video-poi-selected small { color: #999da5; font-size: var(--fs-body-sm); font-weight: 400; }
.video-poi-selected { padding: 2px 0; font-size: var(--fs-body-sm); }
.douyin-choice-group { display: flex; flex-wrap: wrap; gap: 7px; }
.douyin-choice { display: inline-flex; min-width: 94px; min-height: 34px; align-items: center; gap: 8px; padding: 0 10px; border-radius: 4px; background: #f0f1f3; color: #30343b; cursor: pointer; font-size: var(--fs-body-sm); font-weight: 400; }
.douyin-choice input { width: 15px; height: 15px; margin: 0; accent-color: #16171a; }
.douyin-schedule-input { max-width: 260px; }
@media (max-width: 600px) { .douyin-form-row { grid-template-columns: 68px minmax(0, 1fr); gap: 6px; } .douyin-section { padding: 16px 10px; } .douyin-choice { min-width: 85px; } .douyin-location-type { flex-basis: 55px; padding: 0 8px; } }
.video-override-switch { display: flex; align-items: center; gap: 6px; margin: 9px 0; color: var(--muted); font-size: var(--fs-caption); }
.video-override-switch input { accent-color: var(--pine); }
.video-platform-inherit-note { margin: -5px 0 0 20px; color: var(--muted); font-size: var(--fs-caption); }
.video-selected-summary { display: grid; gap: 3px; padding: 11px; border-radius: 9px; background: #f1f6ef; }
.video-selected-summary strong { color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-selected-summary span { color: var(--muted); font-size: var(--fs-caption); }
.video-create-footer { position: fixed; right: 22px; bottom: 12px; left: calc(var(--sidebar-width) + 22px); z-index: 10; display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 11px 14px; border: 1px solid #dcded6; border-radius: 12px; background: rgb(255 254 250 / 96%); box-shadow: 0 8px 26px rgb(32 43 38 / 12%); backdrop-filter: blur(12px); }
.video-create-footer > div:first-child { display: grid; min-width: 0; gap: 2px; }
.video-create-footer strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.video-create-footer span { overflow: hidden; color: var(--muted); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.video-create-footer-actions { display: flex; flex: 1 1 auto; align-items: center; justify-content: flex-end; min-width: 0; gap: 12px; }
.video-create-footer-error { flex: 0 1 auto; max-width: min(52vw, 620px); min-width: 0; margin-left: auto; color: var(--danger); font-size: var(--fs-body-sm); line-height: var(--lh-base); text-align: right; overflow-wrap: anywhere; animation: video-footer-error-shake 260ms ease-in-out; }
.video-create-footer-buttons { display: flex; flex: 0 0 auto; gap: 8px; }
@keyframes video-footer-error-shake { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 50% { transform: translateX(3px); } 75% { transform: translateX(-1px); } }
@media (max-width: 600px) { .video-create-footer-error { width: 100%; max-width: none; margin-left: 0; } }
@media (prefers-reduced-motion: reduce) { .video-create-footer-error { animation: none; } }
.video-dialog-backdrop { position: fixed; inset: 0; z-index: 80; display: grid; place-items: center; padding: 18px; background: rgb(20 33 27 / 45%); }
.video-dialog { width: min(620px, 100%); max-height: min(720px, 88vh); overflow: auto; padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: var(--paper); box-shadow: 0 20px 60px #18241b30; }
.video-dialog > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.video-dialog h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.video-dialog header p { margin: 4px 0 0; color: var(--muted); font-size: var(--fs-body-sm); }
.video-close-button { width: 30px; height: 30px; border: 1px solid var(--line); border-radius: 8px; background: white; color: var(--muted); font-size: 20px; cursor: pointer; }
.video-library-empty { padding: 22px; color: var(--muted); font-size: var(--fs-body-sm); text-align: center; }
.video-library-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; cursor: pointer; }
.video-library-item:hover { background: #f7f8f2; }
.video-library-item > span:nth-child(2) { display: grid; flex: 1; min-width: 0; gap: 3px; }
.video-library-item strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.video-library-item small { color: var(--muted); font-size: var(--fs-caption); }
.video-library-item em { color: var(--pine); font-size: var(--fs-caption); font-style: normal; }
.video-account-dialog { width: min(460px, 100%); }
.video-account-security-note { padding: 9px 10px; border-radius: 8px; background: #f5f5ef; color: var(--muted); font-size: var(--fs-caption); }
.video-account-dialog > footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
@media (max-width: 1200px) { .video-create-workspace { grid-template-columns: minmax(260px, .9fr) 155px minmax(310px, 1.1fr); } .video-platform-settings-scroll { padding-right: 13px; padding-left: 13px; } }
@media (max-width: 980px) { .video-create-workspace { grid-template-columns: 1fr; height: auto; min-height: 0; overflow: visible; } .video-compose-column { height: auto; overflow: visible; border-right: 0; border-bottom: 1px solid var(--line); } .video-platform-rail { display: block; overflow-x: auto; overflow-y: hidden; border-right: 0; border-bottom: 1px solid var(--line); } .video-platform-rail-heading { padding: 10px 12px 7px; } .video-platform-list { display: flex; width: max-content; padding: 0 8px 9px; } .video-platform-tab { width: 158px; min-height: 48px; } .video-platform-rail-hint { display: none; } .video-platform-settings { min-height: 420px; } .video-platform-accounts-scroll { max-height: none; overflow: visible; padding: 0 13px; } .video-platform-settings-scroll { overflow: visible; } .video-setup-card { grid-template-columns: auto 1fr; } .video-setup-input-row { grid-column: 1 / -1; } .video-setup-card > small { grid-column: 1 / -1; } }
@media (max-width: 980px) { .video-platform-loading { grid-column: 1; min-height: 160px; } }
@media (max-width: 600px) { .video-cover-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .video-section-heading { flex-direction: column; } .video-platform-settings-header { align-items: flex-start; flex-direction: column; padding: 14px; } .video-platform-settings-actions { width: 100%; justify-content: space-between; } .video-platform-description { padding-right: 14px; padding-left: 14px; } .video-platform-settings-scroll { padding-right: 14px; padding-left: 14px; } .video-account-option { align-items: flex-start; flex-direction: column; padding: 7px 0; } .video-account-actions { justify-content: flex-end; align-self: flex-end; } .video-login-qr { align-items: flex-start; flex-direction: column; } .video-create-footer { right: 8px; bottom: 8px; left: 8px; align-items: stretch; flex-direction: column; } .video-create-footer-actions { align-items: stretch; flex-direction: column; } .video-create-footer-buttons { justify-content: flex-end; } .video-create-footer .button { flex: 1; } .video-create-page { padding-bottom: 120px; } }
@media (max-width: 600px) { .video-account-list { grid-template-columns: minmax(0, 1fr); } .video-account-option { padding: 0; } .video-account-actions { align-self: stretch; } }
/* —— 排版系统 v2 试点:共享类在本页的覆盖(全量落地时上移 styles.css)—— */
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }

/* Element Plus controls inherit the existing workbench tokens and field density. */
.video-create-page :deep(.el-button) { --el-button-border-color: var(--line); --el-button-bg-color: var(--paper); --el-button-text-color: var(--text-secondary); --el-button-hover-border-color: var(--pine); --el-button-hover-bg-color: var(--pine-soft); --el-button-hover-text-color: var(--pine); font-size: var(--fs-body-sm); }
.video-create-page :deep(.el-button.button) { min-height: 36px; border-color: var(--pine); border-radius: 6px; background: var(--pine); color: #fff; font-size: var(--fs-body-sm); font-weight: var(--fw-medium); box-shadow: none; }
.video-create-page :deep(.el-button.button:hover) { border-color: var(--pine-2); background: var(--pine-2); color: #fff; }
.video-create-page :deep(.el-button.button-secondary) { border-color: var(--line); background: var(--paper); color: var(--text-secondary); }
.video-create-page :deep(.el-button.button-secondary:hover) { border-color: var(--pine); background: var(--pine-soft); color: var(--pine); }
.video-create-page :deep(.el-button.button-small) { min-height: 32px; padding: 0 11px; font-size: var(--fs-caption); }
.video-create-page :deep(.el-button.text-button) { min-height: 30px; padding: 0 2px; border: 0; background: transparent; color: var(--pine); font-size: var(--fs-body-sm); box-shadow: none; }
.video-create-page :deep(.el-button.text-button:hover) { background: transparent; color: var(--pine-2); text-decoration: underline; }
.video-create-page :deep(.el-input.input), .video-create-page :deep(.el-textarea.input), .video-create-page :deep(.el-select.input) { min-height: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.video-create-page :deep(.el-input__wrapper), .video-create-page :deep(.el-select__wrapper) { min-height: 36px; padding: 1px 11px; border-radius: 6px; background: var(--paper); box-shadow: 0 0 0 1px var(--line); }
.video-create-page :deep(.el-input__wrapper.is-focus), .video-create-page :deep(.el-select__wrapper.is-focused) { box-shadow: 0 0 0 1px var(--pine), 0 0 0 3px var(--pine-soft); }
.video-create-page :deep(.el-input__inner), .video-create-page :deep(.el-textarea__inner), .video-create-page :deep(.el-select__placeholder), .video-create-page :deep(.el-select__selected-item) { color: var(--text-primary); font-size: var(--fs-body-sm); }
.video-create-page :deep(.el-textarea__inner) { min-height: 88px; padding: 9px 11px; border: 0; border-radius: 6px; box-shadow: 0 0 0 1px var(--line); }
.video-create-page :deep(.el-input.input.el-input--textarea) { width: 100%; }
.video-create-page :deep(.video-counted-input .el-input__wrapper) { padding-right: 54px; }
.video-create-page :deep(.video-tag-entry.el-input) { flex: 1 1 160px; min-width: min(160px, 100%); width: auto; }
.video-create-page :deep(.video-tag-entry .el-input__wrapper) { min-height: 30px; padding: 0 2px; border: 0; background: transparent; box-shadow: none; }
.video-create-page :deep(.video-tag-entry .el-input__inner) { min-height: 30px; font-size: var(--fs-body-sm); }
.video-create-page :deep(.el-tag.video-tag-chip) { min-height: 28px; height: auto; padding: 4px 8px; border: 0; border-radius: 6px; background: var(--pine-soft); color: var(--pine); font-size: var(--fs-body-sm); white-space: normal; }
.video-create-page :deep(.video-tag-chip .el-tag__content) { white-space: normal; overflow-wrap: anywhere; }
.video-create-page :deep(.el-checkbox.video-account-checkbox) { flex: 0 0 auto; margin: 0; }
.video-create-page :deep(.el-checkbox.video-account-checkbox .el-checkbox__label) { display: none; }
.video-create-page :deep(.el-checkbox.video-override-checkbox) { display: flex; align-items: center; margin: 9px 0; color: var(--text-secondary); font-size: var(--fs-body-sm); }
.video-create-page :deep(.el-checkbox__input.is-checked .el-checkbox__inner), .video-create-page :deep(.el-radio__input.is-checked .el-radio__inner) { border-color: var(--pine); background: var(--pine); }
.video-create-page :deep(.video-account-entry.el-card) { min-width: 0; min-height: 92px; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); box-shadow: 0 1px 3px rgb(24 39 75 / 4%); }
.video-create-page :deep(.video-account-entry .el-card__body) { display: grid; align-content: start; gap: 9px; padding: 11px; }
.video-create-page :deep(.video-account-entry.is-picked) { border-color: var(--pine); background: var(--pine-soft); }
.video-create-page :deep(.video-cover-upload-card.el-card) { display: block; width: 100%; min-width: 0; padding: 0; border: 0; border-radius: 8px; background: transparent; box-shadow: none; }
.video-create-page :deep(.video-cover-upload-card .el-card__body) { display: grid; width: 100%; box-sizing: border-box; grid-template-columns: 54px minmax(0, 1fr); align-items: center; gap: 7px; min-height: 100%; padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }
.video-create-page :deep(.el-image.video-cover-preview) { display: block; }
.video-create-page :deep(.video-cover-preview .el-image__inner) { display: block; width: 100%; height: 100%; object-fit: cover; }
.video-create-page :deep(.video-platform-tabs) { display: flex; min-width: 0; flex: 1; flex-direction: column; }
.video-create-page :deep(.video-platform-tabs > .el-tabs__header) { width: 100%; margin: 0; border: 0; }
.video-create-page :deep(.video-platform-tabs.el-tabs--left > .el-tabs__header) { margin-right: 0; }
.video-create-page :deep(.video-platform-tabs .el-tabs__nav-wrap::after), .video-create-page :deep(.video-platform-tabs .el-tabs__active-bar), .video-create-page :deep(.video-platform-tabs .el-tabs__content) { display: none; }
.video-create-page :deep(.video-platform-tabs .el-tabs__nav) { display: grid; width: 100%; gap: 4px; border: 0; }
.video-create-page :deep(.video-platform-tabs .el-tabs__item) { height: auto; min-height: 53px; padding: 0 !important; line-height: normal; }
.video-create-page :deep(.video-platform-tabs .el-tabs__item.is-active) { color: inherit; }
.video-create-page :deep(.video-source-toggle) { flex: 0 0 auto; padding: 3px; border-radius: 8px; background: var(--paper-2); }
.video-create-page :deep(.video-source-toggle > .el-tabs__header), .video-create-page :deep(.cover-editor-tabs > .el-tabs__header) { margin: 0; border: 0; }
.video-create-page :deep(.video-source-toggle .el-tabs__nav-wrap::after), .video-create-page :deep(.video-source-toggle .el-tabs__active-bar), .video-create-page :deep(.video-source-toggle .el-tabs__content) { display: none; }
.video-create-page :deep(.video-source-toggle .el-tabs__nav), .video-create-page :deep(.cover-editor-tabs .el-tabs__nav) { border: 0; }
.video-create-page :deep(.video-source-toggle .el-tabs__item) { height: 30px; padding: 0 10px !important; border-radius: 6px; color: var(--text-secondary); font-size: var(--fs-caption); line-height: 30px; }
.video-create-page :deep(.video-source-toggle .el-tabs__item.is-active) { background: var(--paper); color: var(--pine); box-shadow: 0 1px 4px rgb(32 53 45 / 8%); }
.video-create-page :deep(.douyin-choice-group) { display: flex; flex-wrap: wrap; gap: 7px; }
.video-create-page :deep(.douyin-choice-group .el-radio) { min-height: 34px; margin: 0; padding: 0 10px; border-radius: 4px; background: var(--paper-2); color: var(--text-secondary); }
.video-create-page :deep(.douyin-choice-group .el-radio__label) { padding-left: 6px; color: inherit; font-size: var(--fs-body-sm); }
.video-create-page :deep(.video-no-account .el-empty) { padding: 12px 0; }
.video-create-page :deep(.video-no-account .el-empty__description p) { font-size: var(--fs-body-sm); }
.video-create-page :deep(.video-library-item.el-button) { display: flex; width: 100%; height: auto; min-height: 58px; justify-content: flex-start; padding: 10px 0; border: 0; border-top: 1px solid var(--line); border-radius: 0; background: transparent; color: var(--text-primary); text-align: left; white-space: normal; }
.video-create-page :deep(.video-library-item.el-button:hover) { background: var(--paper-2); color: var(--text-primary); }
.video-create-page :deep(.video-library-item .el-button__text) { display: flex; flex: 1; align-items: center; gap: 10px; min-width: 0; }
.video-create-page :deep(.video-library-item .el-button__text > span:nth-child(2)) { display: grid; flex: 1; min-width: 0; gap: 3px; }
.video-create-page :deep(.video-library-empty) { padding: 16px 0; }
:global(.el-dialog.video-library-dialog) { overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--paper); box-shadow: 0 20px 60px rgb(24 36 27 / 19%); }
:global(.el-dialog.video-library-dialog .el-dialog__header) { margin: 0; padding: 18px 18px 12px; border-bottom: 1px solid var(--line); }
:global(.el-dialog.video-library-dialog .el-dialog__body) { max-height: min(620px, 72vh); overflow: auto; padding: 0 18px 12px; }
:global(.video-library-dialog-header) { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
:global(.video-library-dialog-header h2) { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
:global(.video-library-dialog-header p) { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
:global(.video-library-dialog-header .el-button) { min-height: 30px; padding: 0 4px; border: 0; color: var(--text-secondary); background: transparent; font-size: 20px; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item) { display: flex; width: 100%; height: auto; min-height: 58px; justify-content: flex-start; padding: 10px 0; border: 0; border-top: 1px solid var(--line); border-radius: 0; background: transparent; color: var(--text-primary); text-align: left; white-space: normal; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item:hover) { background: var(--paper-2); color: var(--text-primary); }
:global(.el-dialog.video-library-dialog .video-library-item .el-button__text) { display: flex; flex: 1; align-items: center; gap: 10px; min-width: 0; }
:global(.el-dialog.video-library-dialog .video-library-item .el-button__text > span:nth-child(2)) { display: grid; flex: 1; min-width: 0; gap: 3px; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item) { min-width: 0; box-sizing: border-box; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item + .el-button.video-library-item) { margin-left: 0; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item > span) { display: flex; width: 100%; min-width: 0; align-items: center; gap: 10px; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item > span > span:nth-child(2)) { display: grid; flex: 1; min-width: 0; gap: 3px; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item strong), :global(.el-dialog.video-library-dialog .el-button.video-library-item small) { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:global(.el-dialog.video-library-dialog .el-button.video-library-item em) { flex: 0 0 auto; white-space: nowrap; }
@media (max-width: 980px) {
  .video-create-page :deep(.video-platform-tabs) { flex: none; }
  .video-create-page :deep(.video-platform-tabs.el-tabs--left) { flex-direction: column; }
  .video-create-page :deep(.video-platform-tabs.el-tabs--left > .el-tabs__header) { float: none; flex: none; border: 0; }
  .video-create-page :deep(.video-platform-tabs .el-tabs__nav) { display: flex; width: max-content; gap: 4px; }
  .video-create-page :deep(.video-platform-tabs .el-tabs__item) { width: 158px; min-height: 48px; }
}
</style>
