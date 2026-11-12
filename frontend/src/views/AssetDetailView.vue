<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '../api/client'
import type { CommentCollectionState, TaskArtifact, TaskDetail, TaskStep, TranscriptComparisonData, VaultPreview } from '../api/types'
import ArtifactViewer from '../components/ArtifactViewer.vue'
import TaskSteps from '../components/TaskSteps.vue'
import CommentList from '../components/CommentList.vue'
import CommentInsights from '../components/CommentInsights.vue'
import ScriptEditor from '../components/ScriptEditor.vue'
import KnowledgeCardReview from '../components/KnowledgeCardReview.vue'
import StructureAnalysis from '../components/StructureAnalysis.vue'
import MarkdownTextEditor from '../components/MarkdownTextEditor.vue'
import MarkdownPreview from '../components/MarkdownPreview.vue'

const route = useRoute()
const router = useRouter()
type DetailTab = 'files' | 'proof' | 'comments' | 'scripts' | 'knowledge'
const detailTabs: Array<{ id: DetailTab; label: string }> = [
  { id: 'files', label: '产物' },
  { id: 'proof', label: '转写与校对' },
  { id: 'comments', label: '评论' },
  { id: 'scripts', label: '稿件' },
  { id: 'knowledge', label: '知识卡' },
]
const taskId = computed(() => typeof route.params.taskId === 'string' ? route.params.taskId : '')
const detail = ref<TaskDetail | null>(null)
const taskDurationNow = ref(Date.now())
const stepDetailsExpanded = ref(false)
const titleExpanded = ref(false)
const descriptionExpanded = ref(false)
const activeDetailTab = ref<DetailTab>('files')
const commentState = ref<CommentCollectionState | null>(null)
const loading = ref(false)
const actionBusy = ref(false)
const error = ref('')
const actionError = ref('')
const articleGenerationBusyId = ref('')
const articleGenerationError = ref('')
const editableArtifactId = ref('')
const loadedTextArtifactId = ref('')
const editedText = ref('')
const editExpectedSha256 = ref('')
const reviewCopySuffix = ref('')
const textPreviewBusy = ref(false)
const textSaveBusy = ref(false)
const textSaveMessage = ref('')
const textEditSnapshot = ref('')
const textCopyMessage = ref('')
const transcriptReconcileBusy = ref(false)
const transcriptReconcileMessage = ref('')
const transcriptComparison = ref<TranscriptComparisonData | null>(null)
const transcriptComparisonBusy = ref(false)
const transcriptComparisonOpen = ref(false)
const transcriptComparisonMessage = ref('')
const transcriptUndoMessage = ref('')
const fullOcrEvidenceBusy = ref(false)
const fullOcrEvidenceContent = ref('')
const fullOcrEvidenceMessage = ref('')
const taskFolderBusy = ref(false)
const taskFolderMessage = ref('')
const selectedReviewedTranscriptId = ref('')
const selectedCommentIds = ref<string[]>([])
const videoNotesBusy = ref(false)
const videoNotesMessage = ref('')
const selectedVaultArtifact = ref('')
const vaultTargetPath = ref('')
const vaultPreview = ref<VaultPreview | null>(null)
const vaultBusy = ref(false)
const vaultMessage = ref('')
const scriptEditor = ref<InstanceType<typeof ScriptEditor> | null>(null)
const scriptRegenerationTarget = ref<HTMLElement | null>(null)
const knowledgeEditor = ref<InstanceType<typeof KnowledgeCardReview> | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | undefined
let taskDurationTimer: ReturnType<typeof setInterval> | undefined
let requestSequence = 0
let textPreviewSequence = 0
let transcriptComparisonSequence = 0
let tabActive = true
let autoOpenedQueryArtifactId = ''

const taskActive = computed(() => ['queued', 'running', 'awaiting_authorization'].includes(detail.value?.task.status ?? ''))
const canCancel = computed(() => ['queued', 'running', 'interrupted', 'awaiting_authorization'].includes(detail.value?.task.status ?? ''))
const displayTitle = computed(() => detail.value?.asset.title || detail.value?.asset.source_id || '未命名素材')
const displayDescription = computed(() => detail.value?.asset.description?.trim() || '')
const titleCanExpand = computed(() => displayTitle.value.length > 80)
const descriptionCanExpand = computed(() => displayDescription.value.length > 180)
const videoMetadata = computed(() => {
  const artifacts = detail.value?.artifacts ?? []
  for (let index = artifacts.length - 1; index >= 0; index -= 1) {
    if (artifacts[index].kind === 'video') return artifacts[index].metadata
  }
  return {}
})
const videoAuthor = computed(() => detail.value?.asset.author?.trim() || videoMetadataText('author') || '未获取')
const videoPublishedAt = computed(() => formatDate(detail.value?.asset.published_at || videoMetadataText('published_at')))
const videoDuration = computed(() => formatDuration(videoMetadata.value.duration_seconds))
const videoResolution = computed(() => {
  const { width, height } = videoMetadata.value
  return typeof width === 'number' && width > 0 && typeof height === 'number' && height > 0
    ? `${width} × ${height}`
    : '未获取'
})
const stepLabels: Record<string, string> = {
  download: '下载视频与封面', import: '导入本地媒体', transcribe: '语音转文字', ocr: '字幕 OCR',
  comments: '采集公开评论', analyze_comments: '筛选评论', analyze_structure: '拆解内容结构',
  knowledge_cards: '生成知识卡', generate_scripts: '生成口播稿', index: '建立本地索引',
  write_obsidian: '写入知识库',
}
const activeSteps = computed(() => detail.value?.task.steps.filter((step) => step.status === 'running') ?? [])
const finalizedScriptArtifacts = computed(() => (detail.value?.artifacts ?? []).filter((artifact) => artifact.kind === 'final_script' && artifact.metadata.finalized === true && artifact.metadata.human_reviewed === true))
const activeStep = computed(() => activeSteps.value[0] ?? null)
const activeOcrStep = computed(() => activeSteps.value.find((step) => step.kind === 'ocr') ?? null)
function elapsedLabel(startedAt: string | null | undefined): string {
  if (!startedAt) return '未知'
  const timestamp = Date.parse(startedAt)
  if (Number.isNaN(timestamp)) return '未知'
  const elapsed = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  const minutes = Math.floor(elapsed / 60)
  const seconds = elapsed % 60
  return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`
}
function formatTaskDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours) return `${hours} 小时 ${minutes} 分 ${seconds} 秒`
  if (minutes) return `${minutes} 分 ${seconds} 秒`
  return `${seconds} 秒`
}
const taskDurationLabel = computed(() => {
  const task = detail.value?.task
  if (!task || !['running', 'completed'].includes(task.status)) return ''
  const stepStartTimes = task.steps
    .map((step) => Date.parse(step.started_at ?? ''))
    .filter((timestamp) => Number.isFinite(timestamp))
  const startedAt = stepStartTimes.length ? Math.min(...stepStartTimes) : Date.parse(task.created_at)
  const finishedAt = task.status === 'completed' ? Date.parse(task.updated_at) : taskDurationNow.value
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt)) return ''
  return formatTaskDuration(Math.max(0, Math.floor((finishedAt - startedAt) / 1000)))
})
const taskProgressHelp = computed(() => {
  const message = activeOcrStep.value?.message ?? ''
  if (message.startsWith('本地 OCR 已处理') || message.startsWith('本地 OCR 已恢复')) return '本地 OCR 会按已完成画面的平均速度动态估算总用时和剩余时间。'
  if (message.startsWith('本地 OCR')) return '本地 OCR 正在准备；处理首张画面后会显示耗时估算。'
  if (message.startsWith('云端 OCR 正在批量识别')) return `云端 OCR 已耗时 ${elapsedLabel(activeOcrStep.value?.started_at)}；服务商不返回逐张进度，无法可靠估算剩余时间。`
  if (message.startsWith('云端 OCR')) return '云端 OCR 正在准备关键帧；开始批量请求后会显示已耗时。'
  return '页面每 2 秒同步一次状态；素材大小和平台响应会影响处理时长。'
})
const failedStep = computed(() => detail.value?.task.steps.find((step) => ['failed', 'blocked'].includes(step.status)) ?? null)
const completedStepCount = computed(() => detail.value?.task.steps.filter((step) => ['completed', 'partial'].includes(step.status)).length ?? 0)
const skippedStepCount = computed(() => detail.value?.task.steps.filter((step) => step.status === 'skipped').length ?? 0)
const taskProgressPercent = computed(() => {
  const steps = detail.value?.task.steps ?? []
  if (!steps.length) return 0
  if (detail.value?.task.status === 'completed') return 100
  const currentFraction = activeSteps.value.reduce((total, step) => total + (typeof step.progress === 'number' ? Math.min(100, Math.max(0, step.progress)) / 100 : 0), 0)
  return Math.round(((completedStepCount.value + skippedStepCount.value + currentFraction) / steps.length) * 100)
})
function progressSegmentPercent(step: TaskStep): number {
  if (['completed', 'partial', 'skipped'].includes(step.status)) return 100
  if (step.status === 'running' && typeof step.progress === 'number') return Math.min(100, Math.max(0, step.progress))
  return 0
}
function progressSegmentClass(step: TaskStep): Record<string, boolean> {
  return {
    'is-running': step.status === 'running',
    'is-indeterminate': step.status === 'running' && step.progress == null,
    'is-attention': ['failed', 'blocked'].includes(step.status),
    'is-skipped': step.status === 'skipped',
  }
}
const progressIndeterminate = computed(() => {
  const status = detail.value?.task.status
  return status === 'queued' || (status === 'running' && (!activeSteps.value.length || activeSteps.value.some((step) => step.progress == null)))
})
const liveProgressMessage = computed(() => {
  const task = detail.value?.task
  if (!task) return ''
  if (task.status === 'queued') return '任务已进入本机队列，等待开始。'
  if (task.status === 'awaiting_authorization') return '此前暂停的 OCR 步骤可以直接继续识别。'
  if (task.status === 'running') {
    if (activeSteps.value.length > 1) return `正在并行处理：${activeSteps.value.map((step) => stepLabels[step.kind] ?? step.kind).join('、')}。`
    const step = activeStep.value
    if (!step) return '任务正在执行。'
    const label = stepLabels[step.kind] ?? step.kind
    return step.message ? `正在${label}：${step.message}` : `正在${label}。`
  }
  if (task.status === 'failed' || task.status === 'blocked') {
    const failed = failedStep.value
    const label = failed ? stepLabels[failed.kind] ?? failed.kind : '处理步骤'
    const reason = failed?.message ? `：${failed.message}` : '，请查看步骤说明。'
    return `任务${task.status === 'failed' ? '失败' : '受阻'}：${label}${reason}`
  }
  if (task.status === 'interrupted') return '任务已中断，尚未完成的步骤没有继续执行。'
  if (task.status === 'cancelled') return '任务已取消，尚未完成的步骤没有继续执行。'
  if (task.status === 'partial') return '任务部分完成；请检查步骤状态与停止原因。'
  if (task.status === 'completed') return skippedStepCount.value
    ? `任务已完成，跳过了 ${skippedStepCount.value} 个尚未开始的步骤。`
    : '本次选择的处理步骤已全部完成。'
  return '任务状态已更新。'
})
const taskNeedsAttention = computed(() => ['failed', 'blocked', 'interrupted', 'cancelled', 'partial'].includes(detail.value?.task.status ?? ''))
const vaultArtifacts = computed(() => (detail.value?.artifacts ?? []).filter((artifact) => artifact.kind === 'final_script'))
const pendingOcrStep = computed(() => detail.value?.task.steps.find((step) => step.kind === 'ocr' && step.status === 'awaiting_authorization') ?? null)
const currentStep = computed(() => activeStep.value
  ?? pendingOcrStep.value
  ?? failedStep.value
  ?? detail.value?.task.steps.find((step) => ['queued', 'pending'].includes(step.status))
  ?? null)
const currentStepLabel = computed(() => activeSteps.value.length > 1
  ? `${activeSteps.value.map((step) => stepLabels[step.kind] ?? step.kind).join('、')} · 并行处理中`
  : currentStep.value
  ? `${stepLabels[currentStep.value.kind] ?? currentStep.value.kind} · ${currentStep.value.status === 'running' ? '处理中' : currentStep.value.status === 'awaiting_authorization' ? '等待继续' : currentStep.value.status === 'failed' || currentStep.value.status === 'blocked' ? '需要处理' : '等待中'}`
  : detail.value?.task.status === 'completed' ? '所有步骤已完成' : '暂无运行步骤')
const canOpenTaskFolder = computed(() => Boolean(detail.value?.asset.archive_relative_path
  || detail.value?.artifacts.some((artifact) => artifact.relative_path)))
const editableTextArtifacts = computed(() => (detail.value?.artifacts ?? [])
  .filter((artifact) => ['transcript', 'transcript_draft', 'transcript_revision', 'ocr_result', 'ocr_revision'].includes(artifact.kind))
  .sort((left, right) => left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id)))
const proofingTextOptions = computed(() => {
  const artifacts = editableTextArtifacts.value
  const newest = (items: TaskArtifact[]) => [...items]
    .sort((left, right) => right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id))[0]
  const curated = [
    newest(artifacts.filter((artifact) => artifact.kind === 'transcript_revision' && artifact.metadata.human_edited === true)),
    newest(artifacts.filter((artifact) => artifact.kind === 'transcript_draft')),
    newest(artifacts.filter((artifact) => artifact.kind === 'transcript')),
  ].filter((artifact): artifact is TaskArtifact => Boolean(artifact))
  return curated.length ? curated : [newest(artifacts.filter((artifact) => ['ocr_result', 'ocr_revision'].includes(artifact.kind)))].filter((artifact): artifact is TaskArtifact => Boolean(artifact))
})
const scriptTranscriptSource = computed(() => editableTextArtifacts.value
  .filter((artifact) => ['transcript_revision', 'transcript_draft', 'transcript'].includes(artifact.kind))
  .sort((left, right) => {
    const leftReviewed = left.metadata.review_copy === true && left.metadata.human_edited === true
    const rightReviewed = right.metadata.review_copy === true && right.metadata.human_edited === true
    return Number(rightReviewed) - Number(leftReviewed) || right.created_at.localeCompare(left.created_at)
  })[0] ?? null)
const canRegenerateScripts = computed(() => {
  const task = detail.value?.task
  return Boolean(task
    && ['completed', 'partial'].includes(task.status)
    && task.steps.length > 0
    && task.steps.every((step) => ['completed', 'partial'].includes(step.status)))
})
const scriptRegenerationUnavailableReason = computed(() => {
  const task = detail.value?.task
  if (!task) return '正在读取任务状态。'
  if (taskActive.value) return '任务执行中；完成后可选择风格模板并重新生成。'
  if (!['completed', 'partial'].includes(task.status)) return '任务尚未结束，暂不能重新生成。'
  if (task.steps.some((step) => !['completed', 'partial'].includes(step.status))) return '仍有步骤未完成；请先处理完当前任务。'
  return ''
})
const hasTranscriptReconcileSources = computed(() => {
  const artifacts = detail.value?.artifacts ?? []
  const mediaArtifacts = artifacts
    .filter((artifact) => ['video', 'media'].includes(artifact.kind))
    .sort((left, right) => left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id))
  const media = mediaArtifacts[mediaArtifacts.length - 1]
  if (!media) return false
  return ['transcript_raw', 'ocr_raw'].every((kind) => artifacts.some((artifact) =>
    artifact.kind === kind && artifact.metadata.source_artifact_id === media.id))
})
const canReconcileTranscript = computed(() => Boolean(
  detail.value
  && hasTranscriptReconcileSources.value
  && !['queued', 'running'].includes(detail.value.task.status)
  && !detail.value.task.steps.some((step) => ['queued', 'running'].includes(step.status)),
))
const transcriptReconcileUnavailableReason = computed(() => {
  if (!detail.value) return '正在载入任务资料。'
  if (!hasTranscriptReconcileSources.value) return '需先完成本任务的语音转写和 OCR 识别。'
  if (!canReconcileTranscript.value) return '请等待当前任务步骤结束后再重新校对。'
  return ''
})
const reviewedTranscriptArtifacts = computed(() => editableTextArtifacts.value
  .filter((artifact) => artifact.kind === 'transcript_revision' && artifact.metadata.review_copy === true && artifact.metadata.human_edited === true))
const selectedEditableTextArtifact = computed(() => editableTextArtifacts.value.find((artifact) => artifact.id === editableArtifactId.value) ?? null)
const reviewCopyEditorActive = computed(() => selectedEditableTextArtifact.value?.kind === 'transcript_draft'
  || selectedEditableTextArtifact.value?.metadata.review_copy === true)
const mergedCommentFeedback = computed(() => {
  const heading = '## 评论反馈参考'
  const start = reviewCopySuffix.value.indexOf(heading)
  return start < 0 ? '' : reviewCopySuffix.value.slice(start).trim()
})
const textHasUnsavedChanges = computed(() => editedText.value !== textEditSnapshot.value)
const transcriptComparisonChanges = computed(() => buildTranscriptChanges(
  transcriptComparison.value?.source_segments ?? [],
  editedText.value,
))
const selectedTranscriptionArtifact = computed(() => {
  const selected = selectedEditableTextArtifact.value
  if (!selected) return null
  const allArtifacts = detail.value?.artifacts ?? []
  const visited = new Set<string>()
  function findTranscript(artifact: TaskDetail['artifacts'][number]): TaskDetail['artifacts'][number] | null {
    if (visited.has(artifact.id)) return null
    visited.add(artifact.id)
    if (artifact.kind === 'transcript') return artifact
    const ids = [artifact.metadata.source_artifact_id, ...(Array.isArray(artifact.metadata.source_artifact_ids) ? artifact.metadata.source_artifact_ids : [])]
    for (const id of ids) {
      if (typeof id !== 'string') continue
      const source = allArtifacts.find((item) => item.id === id)
      if (source) {
        const found = findTranscript(source)
        if (found) return found
      }
    }
    return null
  }
  return findTranscript(selected) ?? selected
})

interface TranscriptDiffUnit {
  text: string
  startSeconds: number | null
  endSeconds: number | null
  startOffset: number | null
  endOffset: number | null
}

interface TranscriptDiffChange {
  id: number
  originalText: string
  currentText: string
  startSeconds: number | null
  endSeconds: number | null
  commonPrefix: string
  removedText: string
  insertedText: string
  commonSuffix: string
  currentStartOffset: number | null
  currentEndOffset: number | null
  currentEditorText: string
}

function splitTranscriptUnits(text: string, segments?: TranscriptComparisonData['source_segments']): TranscriptDiffUnit[] {
  const result: TranscriptDiffUnit[] = []
  const addText = (value: string, startSeconds: number | null, endSeconds: number | null, baseOffset: number | null): void => {
    const expression = /[^,，。！？!?；;\n]+[,，。！？!?；;]?/gu
    for (const match of value.matchAll(expression)) {
      const unit = match[0].trim()
      if (!unit) continue
      const leadingWhitespace = match[0].length - match[0].trimStart().length
      const startOffset = baseOffset === null || match.index === undefined ? null : baseOffset + match.index + leadingWhitespace
      result.push({
        text: unit,
        startSeconds,
        endSeconds,
        startOffset,
        endOffset: startOffset === null ? null : startOffset + unit.length,
      })
    }
  }
  if (segments?.length) {
    for (const segment of segments) {
      addText(segment.text, typeof segment.start_seconds === 'number' ? segment.start_seconds : null,
        typeof segment.end_seconds === 'number' ? segment.end_seconds : null, null)
    }
  } else {
    addText(text, null, null, 0)
  }
  return result
}

function makeTranscriptDiffChange(
  oldUnits: TranscriptDiffUnit[],
  newUnits: TranscriptDiffUnit[],
  id: number,
  currentBody: string,
  insertionOffset: number | null,
): TranscriptDiffChange | null {
  const originalText = oldUnits.map((unit) => unit.text).join('\n')
  const currentText = newUnits.map((unit) => unit.text).join('\n')
  if (originalText === currentText) return null
  const timed = oldUnits.filter((unit) => unit.startSeconds !== null && unit.endSeconds !== null)
  const prefixLimit = Math.min(originalText.length, currentText.length)
  let prefixLength = 0
  while (prefixLength < prefixLimit && originalText[prefixLength] === currentText[prefixLength]) prefixLength++
  let suffixLength = 0
  while (suffixLength < originalText.length - prefixLength
    && suffixLength < currentText.length - prefixLength
    && originalText[originalText.length - suffixLength - 1] === currentText[currentText.length - suffixLength - 1]) suffixLength++
  const currentStartOffset = newUnits.length ? newUnits[0].startOffset : insertionOffset
  const currentEndOffset = newUnits.length ? newUnits[newUnits.length - 1].endOffset : insertionOffset
  return {
    id,
    originalText,
    currentText,
    startSeconds: timed.length ? Math.min(...timed.map((unit) => unit.startSeconds as number)) : null,
    endSeconds: timed.length ? Math.max(...timed.map((unit) => unit.endSeconds as number)) : null,
    commonPrefix: originalText.slice(0, prefixLength),
    removedText: originalText.slice(prefixLength, originalText.length - suffixLength),
    insertedText: currentText.slice(prefixLength, currentText.length - suffixLength),
    commonSuffix: suffixLength ? originalText.slice(originalText.length - suffixLength) : '',
    currentStartOffset,
    currentEndOffset,
    currentEditorText: currentStartOffset !== null && currentEndOffset !== null
      ? currentBody.slice(currentStartOffset, currentEndOffset)
      : '',
  }
}

function buildTranscriptChanges(
  sourceSegments: TranscriptComparisonData['source_segments'],
  currentText: string,
): TranscriptDiffChange[] {
  const original = splitTranscriptUnits('', sourceSegments)
  const revised = splitTranscriptUnits(currentText)
  if (!original.length && !revised.length) return []
  const changes: TranscriptDiffChange[] = []
  const flushPair = (oldUnits: TranscriptDiffUnit[], newUnits: TranscriptDiffUnit[], insertionOffset: number | null): void => {
    if (oldUnits.length > 0 && oldUnits.length === newUnits.length) {
      for (let index = 0; index < oldUnits.length; index++) {
        const change = makeTranscriptDiffChange([oldUnits[index]], [newUnits[index]], changes.length, currentText, insertionOffset)
        if (change) changes.push(change)
      }
    } else {
      const change = makeTranscriptDiffChange(oldUnits, newUnits, changes.length, currentText, insertionOffset)
      if (change) changes.push(change)
    }
    oldUnits.length = 0
    newUnits.length = 0
  }

  if (original.length * revised.length > 1_500_000) {
    const oldUnits: TranscriptDiffUnit[] = []
    const newUnits: TranscriptDiffUnit[] = []
    for (let index = 0; index < Math.max(original.length, revised.length); index++) {
      if (original[index]?.text === revised[index]?.text) {
        flushPair(oldUnits, newUnits, revised[index]?.startOffset ?? currentText.length)
      } else {
        if (original[index]) oldUnits.push(original[index])
        if (revised[index]) newUnits.push(revised[index])
      }
    }
    flushPair(oldUnits, newUnits, currentText.length)
    return changes
  }

  const rows = Array.from({ length: original.length + 1 }, () => new Uint32Array(revised.length + 1))
  for (let oldIndex = original.length - 1; oldIndex >= 0; oldIndex--) {
    for (let newIndex = revised.length - 1; newIndex >= 0; newIndex--) {
      rows[oldIndex][newIndex] = original[oldIndex].text === revised[newIndex].text
        ? rows[oldIndex + 1][newIndex + 1] + 1
        : Math.max(rows[oldIndex + 1][newIndex], rows[oldIndex][newIndex + 1])
    }
  }

  const oldUnits: TranscriptDiffUnit[] = []
  const newUnits: TranscriptDiffUnit[] = []
  let oldIndex = 0
  let newIndex = 0
  while (oldIndex < original.length || newIndex < revised.length) {
    if (oldIndex < original.length && newIndex < revised.length
      && original[oldIndex].text === revised[newIndex].text) {
      flushPair(oldUnits, newUnits, revised[newIndex].startOffset ?? currentText.length)
      oldIndex++
      newIndex++
    } else if (newIndex < revised.length
      && (oldIndex >= original.length || rows[oldIndex][newIndex + 1] >= rows[oldIndex + 1][newIndex])) {
      newUnits.push(revised[newIndex++])
    } else {
      oldUnits.push(original[oldIndex++])
    }
  }
  flushPair(oldUnits, newUnits, currentText.length)
  return changes
}

function ocrCorrectionsForChange(change: TranscriptDiffChange): TranscriptComparisonData['ocr_corrections'] {
  return (transcriptComparison.value?.ocr_corrections ?? []).filter((correction) => {
    const correctionStart = correction.start_seconds
    const correctionEnd = correction.end_seconds
    if (typeof correctionStart === 'number' && typeof correctionEnd === 'number'
      && change.startSeconds !== null && change.endSeconds !== null
      && (correctionEnd < change.startSeconds || correctionStart > change.endSeconds)) return false
    const asr = correction.asr_text?.trim()
    const corrected = correction.corrected_text?.trim()
    if (!asr && !corrected) return false
    return Boolean((asr && (change.originalText.includes(asr) || asr.includes(change.originalText)))
      || (corrected && (change.currentText.includes(corrected) || corrected.includes(change.currentText))))
  })
}

async function restoreTranscriptChange(change: TranscriptDiffChange): Promise<void> {
  const start = change.currentStartOffset
  const end = change.currentEndOffset
  if (start === null || end === null || start < 0 || end < start || end > editedText.value.length) {
    transcriptUndoMessage.value = '无法定位这处正文差异，请重新打开差异面板后再试。'
    return
  }
  if (editedText.value.slice(start, end) !== change.currentEditorText) {
    transcriptUndoMessage.value = '正文已发生变化，请根据当前差异重新确认后再撤回。'
    return
  }
  editedText.value = `${editedText.value.slice(0, start)}${change.originalText}${editedText.value.slice(end)}`
  await nextTick()
  transcriptUndoMessage.value = '已将这处内容恢复为原始 ASR 文本；点击“保存”后才会生成新版本。'
}

function transcriptTimeLabel(start: number | null, end: number | null): string {
  if (start === null || end === null) return '时间未记录'
  return `约 ${start.toFixed(1)}–${end.toFixed(1)} 秒`
}

function selectDetailTab(tab: DetailTab): void {
  activeDetailTab.value = tab
}

async function editArtifact(artifact: TaskArtifact): Promise<void> {
  if (['transcript', 'transcript_draft', 'transcript_revision', 'ocr_result', 'ocr_revision'].includes(artifact.kind)) {
    if (textHasUnsavedChanges.value && !window.confirm('当前文案有未保存的修改，切换版本会放弃这些修改。继续切换吗？')) return
    selectDetailTab('proof')
    await loadTextPreview(artifact.id)
  } else if (['script', 'script_revision', 'final_script'].includes(artifact.kind)) {
    selectDetailTab('scripts')
    await nextTick()
    await scriptEditor.value?.openArtifact(artifact.id)
  } else if (artifact.kind.startsWith('knowledge_card')) {
    selectDetailTab('knowledge')
    await nextTick()
    await knowledgeEditor.value?.openArtifact(artifact.id)
    document.querySelector('#knowledge-card-title')?.scrollIntoView({ block: 'start' })
  }
}

async function openQueryArtifactIfRequested(): Promise<void> {
  const artifactId = route.query.artifact
  if (typeof artifactId !== 'string' || !artifactId || artifactId === autoOpenedQueryArtifactId) return
  const artifact = detail.value?.artifacts.find((item) => item.id === artifactId)
  if (!artifact || !['transcript', 'transcript_draft', 'transcript_revision'].includes(artifact.kind)) return
  autoOpenedQueryArtifactId = artifactId
  await editArtifact(artifact)
}

function handleDetailTabKeydown(event: KeyboardEvent, current: DetailTab): void {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const index = detailTabs.findIndex((tab) => tab.id === current)
  const nextIndex = event.key === 'Home' ? 0
    : event.key === 'End' ? detailTabs.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : detailTabs.length - 1)) % detailTabs.length
  const next = detailTabs[nextIndex]
  activeDetailTab.value = next.id
  void nextTick(() => document.getElementById(`detail-tab-${next.id}`)?.focus())
}

function safeError(value: unknown): string {
  return value instanceof ApiError ? value.message : '无法读取任务详情，请稍后重试。'
}

async function createArticleFromScriptArtifact(artifact: TaskArtifact): Promise<void> {
  if (articleGenerationBusyId.value) return
  const title = typeof artifact.metadata.title === 'string' ? artifact.metadata.title : artifact.name
  if (!window.confirm(`将已审核定稿「${title}」发送给本机当前配置的文本模型，生成可编辑博文。模型费用未知。确认本次调用？`)) return
  articleGenerationBusyId.value = artifact.id
  articleGenerationError.value = ''
  try {
    const article = await api.createArticleFromScript(artifact.id, true)
    await router.push({ name: 'article-edit', params: { articleId: article.id } })
  } catch (cause) {
    articleGenerationError.value = safeError(cause)
  } finally {
    articleGenerationBusyId.value = ''
  }
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    queued: '排队中', running: '处理中', interrupted: '已中断', completed: '已完成',
    partial: '部分完成', failed: '失败', blocked: '受阻', cancelled: '已取消', awaiting_authorization: '等待继续',
  }
  return labels[value] ?? '未知状态'
}

function statusClass(value: string): string {
  if (['failed', 'blocked'].includes(value)) return 'failed'
  if (['queued', 'running', 'interrupted', 'partial', 'cancelled', 'awaiting_authorization'].includes(value)) return 'warn'
  return ''
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '未获取'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '日期未知' : parsed.toLocaleString('zh-CN')
}

function videoMetadataText(key: string): string | null {
  const value = videoMetadata.value[key]
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function formatDuration(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '未获取'
  const totalSeconds = Math.round(value)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours) return `${hours} 小时 ${minutes} 分 ${seconds} 秒`
  if (minutes) return `${minutes} 分 ${seconds} 秒`
  return `${seconds} 秒`
}

function platformLabel(value: string | null): string {
  if (value === 'douyin') return '抖音'
  if (value === 'bilibili') return 'B 站'
  return value || '未获取'
}

function archivePathLabel(value: string | null): string {
  if (value) return value
  return detail.value?.asset.archived_at ? '旧归档路径位于当前目录之外，未在页面展示。' : '尚未生成归档文件。'
}

function artifactMetadataText(artifact: TaskDetail['artifacts'][number], key: string, fallback = '未记录'): string {
  const value = artifact.metadata[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

function transcriptionMethod(artifact: TaskDetail['artifacts'][number]): string {
  const provider = artifactMetadataText(artifact, 'provider_id', '')
  return provider === 'local-faster-whisper' ? '本机离线转写' : provider || '未记录'
}

function textArtifactLabel(artifact: TaskDetail['artifacts'][number]): string {
  if (artifact.kind === 'transcript_revision') return artifact.metadata.review_copy === true ? '人工校对稿（推荐）' : '人工修订稿'
  if (artifact.kind === 'transcript_draft') return artifact.metadata.ocr_is_separate_evidence === true ? 'AI 校对稿（含画面参考）' : 'AI 校对稿（语音）'
  if (artifact.kind === 'transcript') return '原始语音转写'
  return '画面文字识别（OCR）'
}

function modelUsage(run: TaskDetail['model_runs'][number]): string {
  const tokens = [run.usage.input_tokens, run.usage.output_tokens]
    .map((value) => typeof value === 'number' ? value : null)
  if (tokens[0] === null && tokens[1] === null) return '服务商未返回用量'
  return `输入 ${tokens[0] ?? '未知'} token · 输出 ${tokens[1] ?? '未知'} token`
}

function clientRequestId(run: TaskDetail['model_runs'][number]): string | null {
  const detail = run.usage.input_detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  const id = (detail as Record<string, unknown>).client_request_id
  return typeof id === 'string' && id ? id : null
}

function schedulePoll(): void {
  if (pollTimer) clearTimeout(pollTimer)
  syncTaskDurationTimer()
  if (!tabActive || !taskActive.value || !taskId.value) return
  pollTimer = setTimeout(() => void loadTask(false), 2000)
}

function syncTaskDurationTimer(): void {
  if (tabActive && detail.value?.task.status === 'running') {
    if (!taskDurationTimer) {
      taskDurationNow.value = Date.now()
      taskDurationTimer = setInterval(() => { taskDurationNow.value = Date.now() }, 1000)
    }
    return
  }
  if (taskDurationTimer) clearInterval(taskDurationTimer)
  taskDurationTimer = undefined
}

function pauseTab(): void {
  if (!tabActive) return
  tabActive = false
  requestSequence++
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = undefined
  syncTaskDurationTimer()
}

function resumeTab(): void {
  if (tabActive) return
  tabActive = true
  void loadTask(false)
}

async function loadTask(initial = true): Promise<void> {
  const id = taskId.value
  if (!id) {
    detail.value = null
    error.value = '缺少任务标识。'
    return
  }
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  if (initial) loading.value = true
  error.value = ''
  try {
    const result = await api.getTask(id)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    void openQueryArtifactIfRequested()
    schedulePoll()
  } catch (cause) {
    if (sequence === requestSequence) {
      error.value = safeError(cause)
      if (!initial) schedulePoll()
    }
  } finally {
    if (sequence === requestSequence) {
      loading.value = false
    }
  }
}

async function cancelTask(): Promise<void> {
  if (!detail.value || actionBusy.value) return
  const id = detail.value.task.id
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  actionBusy.value = true
  actionError.value = ''
  try {
    const result = await api.cancelTask(id)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    schedulePoll()
  } catch (cause) {
    if (sequence === requestSequence && id === taskId.value) actionError.value = safeError(cause)
  } finally {
    if (sequence === requestSequence && id === taskId.value) actionBusy.value = false
  }
}

async function skipStep(stepId: string): Promise<void> {
  if (!detail.value || actionBusy.value) return
  const id = detail.value.task.id
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  actionBusy.value = true
  actionError.value = ''
  try {
    const result = await api.skipTaskStep(id, stepId)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    schedulePoll()
  } catch (cause) {
    if (sequence === requestSequence && id === taskId.value) actionError.value = safeError(cause)
  } finally {
    if (sequence === requestSequence && id === taskId.value) actionBusy.value = false
  }
}

async function openTaskFolder(): Promise<void> {
  if (!detail.value || taskFolderBusy.value) return
  taskFolderBusy.value = true
  taskFolderMessage.value = ''
  try {
    await api.openTaskFolder(detail.value.task.id)
    taskFolderMessage.value = '已在文件管理器中打开任务目录。'
  } catch (cause) {
    taskFolderMessage.value = safeError(cause)
  } finally {
    taskFolderBusy.value = false
  }
}

async function retryStep(
  stepId: string,
  ocrAction?: 'continue' | 'restart',
  transcriptionAuthorized = false,
): Promise<void> {
  if (!detail.value || actionBusy.value) return
  const id = detail.value.task.id
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  actionBusy.value = true
  actionError.value = ''
  try {
    const result = await api.retryStep(id, stepId, ocrAction, transcriptionAuthorized)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    schedulePoll()
  } catch (cause) {
    if (sequence === requestSequence && id === taskId.value) actionError.value = safeError(cause)
  } finally {
    if (sequence === requestSequence && id === taskId.value) actionBusy.value = false
  }
}


async function authorizeCommentAnalysis(stepId: string, artifactId: string): Promise<void> {
  if (!detail.value || actionBusy.value) return
  const id = detail.value.task.id
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  actionBusy.value = true
  actionError.value = ''
  try {
    await api.authorizeCommentAnalysis(id, stepId, artifactId)
    if (sequence !== requestSequence || id !== taskId.value) return
    const result = await api.getTask(id)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    schedulePoll()
  } catch (cause) {
    if (sequence === requestSequence && id === taskId.value) actionError.value = safeError(cause)
  } finally {
    if (sequence === requestSequence && id === taskId.value) actionBusy.value = false
  }
}

async function regenerateTranscriptDraft(): Promise<void> {
  if (!detail.value || transcriptReconcileBusy.value || !canReconcileTranscript.value) return
  if (textHasUnsavedChanges.value && !window.confirm('当前文案有未保存的修改。重新校对后会切换到新生成的草稿，未保存的内容将丢失。继续吗？')) return

  const id = detail.value.task.id
  const sequence = ++requestSequence
  if (pollTimer) clearTimeout(pollTimer)
  transcriptReconcileBusy.value = true
  transcriptReconcileMessage.value = ''
  try {
    const result = await api.reconcileTranscript(id)
    if (sequence !== requestSequence || id !== taskId.value) return
    detail.value = result
    const drafts = result.artifacts
      .filter((artifact) => artifact.kind === 'transcript_draft')
      .sort((left, right) => left.created_at.localeCompare(right.created_at) || left.version - right.version)
    const newestDraft = drafts[drafts.length - 1]
    if (newestDraft) {
      await loadTextPreview(newestDraft.id)
      if (sequence !== requestSequence || id !== taskId.value) return
      transcriptReconcileMessage.value = '已生成并打开最新校对草稿；原始 ASR 与 OCR 成果保持不变。'
    } else {
      transcriptReconcileMessage.value = '重新校对已完成，但没有找到新生成的校对版本；请刷新任务资料。'
    }
  } catch (cause) {
    if (sequence === requestSequence && id === taskId.value) {
      transcriptReconcileMessage.value = safeError(cause)
    }
  } finally {
    if (sequence === requestSequence && id === taskId.value) transcriptReconcileBusy.value = false
  }
}

function extractReviewCopyBody(markdown: string): string {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n')
  const heading = lines.findIndex((line) => line.trim() === '## 校对文案')
  if (heading < 0) return markdown.trim()
  const evidenceHeadings = new Set([
    '## OCR 证据支持的自动校正（待人工复核）',
    '## 画面文字证据（OCR）',
    '## 人工核对提示',
    '## 评论反馈参考',
  ])
  let end = lines.findIndex((line, index) => index > heading && evidenceHeadings.has(line.trim()))
  if (end < 0) end = lines.length
  return lines.slice(heading + 1, end)
    .filter((line) => !line.trim().startsWith('字数：'))
    .join('\n')
    .trim()
}

function extractReviewCopySuffix(markdown: string): string {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n')
  const heading = lines.findIndex((line) => line.trim() === '## 校对文案')
  if (heading < 0) return ''
  const evidenceHeadings = new Set([
    '## OCR 证据支持的自动校正（待人工复核）',
    '## 画面文字证据（OCR）',
    '## 人工核对提示',
    '## 评论反馈参考',
  ])
  const start = lines.findIndex((line, index) => index > heading && evidenceHeadings.has(line.trim()))
  return start < 0 ? '' : lines.slice(start).join('\n').trim()
}

function reviewCopyMarkdown(body: string, suffix: string): string {
  const appendix = suffix.trim()
  return `# 视频资料 · 转写校对稿\n\n## 校对文案\n\n${body.trim()}\n${appendix ? `\n${appendix}\n` : ''}`
}

async function loadTranscriptComparison(artifactId: string): Promise<void> {
  const sequence = ++transcriptComparisonSequence
  const taskIdValue = detail.value?.task.id
  if (!taskIdValue) return
  transcriptComparisonBusy.value = true
  transcriptComparisonMessage.value = ''
  try {
    const result = await api.getTranscriptComparison(taskIdValue, artifactId)
    if (sequence !== transcriptComparisonSequence || taskIdValue !== taskId.value) return
    transcriptComparison.value = result
  } catch (cause) {
    if (sequence === transcriptComparisonSequence && taskIdValue === taskId.value) {
      transcriptComparison.value = null
      transcriptComparisonMessage.value = safeError(cause)
    }
  } finally {
    if (sequence === transcriptComparisonSequence && taskIdValue === taskId.value) transcriptComparisonBusy.value = false
  }
}

async function toggleTranscriptComparison(): Promise<void> {
  if (!reviewCopyEditorActive.value || !editableArtifactId.value || transcriptComparisonBusy.value) return
  if (transcriptComparisonOpen.value) {
    transcriptComparisonOpen.value = false
    return
  }
  transcriptComparisonOpen.value = true
  if (transcriptComparison.value?.artifact_id !== editableArtifactId.value) {
    await loadTranscriptComparison(editableArtifactId.value)
  }
}

async function loadFullOcrEvidence(event: Event): Promise<void> {
  const details = event.currentTarget as HTMLDetailsElement
  if (!details.open || fullOcrEvidenceBusy.value) return
  const artifactId = transcriptComparison.value?.ocr_result_artifact_id
  if (!artifactId) {
    fullOcrEvidenceMessage.value = '当前校对版本没有关联的 OCR 结果文件。'
    return
  }
  if (fullOcrEvidenceContent.value) return
  fullOcrEvidenceBusy.value = true
  fullOcrEvidenceMessage.value = ''
  try {
    const result = await api.getTextArtifact(artifactId)
    if (transcriptComparison.value?.ocr_result_artifact_id !== artifactId) return
    fullOcrEvidenceContent.value = result.content
  } catch (cause) {
    fullOcrEvidenceMessage.value = safeError(cause)
  } finally {
    fullOcrEvidenceBusy.value = false
  }
}

async function loadTextPreview(artifactId: string): Promise<void> {
  if (!artifactId || textSaveBusy.value) return
  const sequence = ++textPreviewSequence
  textPreviewBusy.value = true
  textSaveMessage.value = ''
  textCopyMessage.value = ''
  try {
    const result = await api.getTextArtifact(artifactId)
    if (sequence !== textPreviewSequence) return
    const artifact = detail.value?.artifacts.find((item) => item.id === artifactId)
    const reviewCopy = artifact?.kind === 'transcript_draft' || artifact?.metadata.review_copy === true
    const editorContent = reviewCopy ? extractReviewCopyBody(result.content) : result.content
    reviewCopySuffix.value = reviewCopy ? extractReviewCopySuffix(result.content) : ''
    editableArtifactId.value = artifactId
    editedText.value = editorContent
    editExpectedSha256.value = result.sha256
    textEditSnapshot.value = editorContent
    loadedTextArtifactId.value = artifactId
  } catch (cause) {
    if (sequence === textPreviewSequence) {
      editableArtifactId.value = loadedTextArtifactId.value
      textSaveMessage.value = safeError(cause)
    }
  } finally {
    if (sequence === textPreviewSequence) textPreviewBusy.value = false
  }
}

async function copySelectedText(): Promise<void> {
  const content = editedText.value
  if (!content.trim()) {
    textCopyMessage.value = '当前版本没有可复制的文本。'
    return
  }
  try {
    let copied = false
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(content)
        copied = true
      } catch {
        // Clipboard API may be unavailable in a local browser context; try the legacy copy command.
      }
    }
    if (!copied) {
      const textarea = document.createElement('textarea')
      textarea.value = content
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      textarea.style.pointerEvents = 'none'
      document.body.appendChild(textarea)
      try {
        textarea.select()
        copied = document.execCommand('copy')
      } finally {
        textarea.remove()
      }
    }
    if (!copied) throw new Error('Clipboard copy command failed')
    textCopyMessage.value = '已复制当前版本的完整文本。'
  } catch {
    textCopyMessage.value = '复制失败，请检查浏览器剪贴板权限后重试。'
  }
}

function discardTextChanges(): void {
  editedText.value = textEditSnapshot.value
  textSaveMessage.value = '未保存修改已撤销。'
}

function handleTextArtifactChange(event: Event): void {
  const nextArtifactId = (event.target as HTMLSelectElement).value
  if (!nextArtifactId || nextArtifactId === loadedTextArtifactId.value) return
  if (textHasUnsavedChanges.value && !window.confirm('当前文案有未保存的修改，切换版本会放弃这些修改。继续切换吗？')) {
    editableArtifactId.value = loadedTextArtifactId.value
    return
  }
  void loadTextPreview(nextArtifactId)
}

async function saveTextRevision(): Promise<void> {
  if (!editableArtifactId.value || !editExpectedSha256.value || textSaveBusy.value) return
  textPreviewSequence++
  textSaveBusy.value = true
  textSaveMessage.value = ''
  const content = reviewCopyEditorActive.value ? reviewCopyMarkdown(editedText.value, reviewCopySuffix.value) : editedText.value
  try {
    const revision = await api.saveTextArtifactRevision(
      editableArtifactId.value,
      editExpectedSha256.value,
      content,
    )
    const updated = await api.getTask(taskId.value)
    detail.value = updated
    editableArtifactId.value = revision.id
    loadedTextArtifactId.value = revision.id
    editExpectedSha256.value = revision.sha256
    textEditSnapshot.value = editedText.value
    textSaveMessage.value = revision.metadata.conflict_copy === true
      ? '检测到原文件被外部修改；你的内容已单独另存为冲突版本，没有覆盖原文件。'
      : '人工校对版本已另存；原始 ASR/OCR 产物保持不变。'
  } catch (cause) {
    textSaveMessage.value = safeError(cause)
  } finally {
    textSaveBusy.value = false
  }
}

async function createVideoNotes(): Promise<void> {
  if (!detail.value || !selectedReviewedTranscriptId.value || videoNotesBusy.value) return
  videoNotesBusy.value = true
  videoNotesMessage.value = ''
  try {
    const artifact = await api.createVideoNotes(detail.value.task.id, selectedReviewedTranscriptId.value)
    detail.value = await api.getTask(detail.value.task.id)
    videoNotesMessage.value = `已另存 ${artifact.relative_path ?? artifact.name}；已有文件未覆盖。`
  } catch (cause) {
    videoNotesMessage.value = safeError(cause)
  } finally {
    videoNotesBusy.value = false
  }
}

async function previewVaultArtifact(): Promise<void> {
  if (!selectedVaultArtifact.value || !vaultTargetPath.value.trim() || vaultBusy.value) return
  vaultBusy.value = true
  vaultMessage.value = ''
  vaultPreview.value = null
  try {
    vaultPreview.value = await api.previewVaultWrite(selectedVaultArtifact.value, vaultTargetPath.value.trim())
    if (vaultPreview.value.conflict && vaultPreview.value.suggested_path) {
      vaultMessage.value = '目标文件已存在且内容不同；工作台不会覆盖。可以使用建议路径另存。'
    } else if (vaultPreview.value.exists) {
      vaultMessage.value = '目标内容已与所选产物一致；确认写入后不会改动文件。'
    } else {
      vaultMessage.value = '目标路径为空；确认预览内容后才会新建 Markdown。'
    }
  } catch (cause) {
    vaultMessage.value = safeError(cause)
  } finally {
    vaultBusy.value = false
  }
}

async function saveVaultArtifact(): Promise<void> {
  const preview = vaultPreview.value
  if (!preview || preview.conflict || vaultBusy.value) return
  if (!window.confirm(`确认将「${preview.title}」写入知识库的 ${preview.target_path}？`)) return
  vaultBusy.value = true
  vaultMessage.value = ''
  try {
    const result = await api.writeVaultNote(
      selectedVaultArtifact.value,
      preview.target_path,
      preview.exists ? preview.current_sha256 : null,
    )
    if (result.written) {
      vaultMessage.value = `已写入：${result.path}`
      vaultPreview.value = null
    } else {
      vaultMessage.value = `${result.reason ?? '未写入。'} 建议另存为：${result.conflict_path ?? '请更换目标路径'}`
      if (result.conflict_path) vaultTargetPath.value = result.conflict_path
      vaultPreview.value = null
    }
  } catch (cause) {
    vaultMessage.value = safeError(cause)
  } finally {
    vaultBusy.value = false
  }
}

function useSuggestedVaultPath(): void {
  const suggested = vaultPreview.value?.suggested_path
  if (!suggested) return
  vaultTargetPath.value = suggested
  vaultPreview.value = null
  void previewVaultArtifact()
}

watch(taskId, () => {
  autoOpenedQueryArtifactId = ''
  stepDetailsExpanded.value = false
  titleExpanded.value = false
  descriptionExpanded.value = false
  textPreviewSequence++
  transcriptComparisonSequence++
  textPreviewBusy.value = false
  transcriptReconcileBusy.value = false
  transcriptReconcileMessage.value = ''
  transcriptComparison.value = null
  transcriptComparisonBusy.value = false
  transcriptComparisonOpen.value = false
  transcriptComparisonMessage.value = ''
  transcriptUndoMessage.value = ''
  fullOcrEvidenceBusy.value = false
  fullOcrEvidenceContent.value = ''
  fullOcrEvidenceMessage.value = ''
  loadedTextArtifactId.value = ''
  editableArtifactId.value = ''
  editedText.value = ''
  reviewCopySuffix.value = ''
  textEditSnapshot.value = ''
  actionBusy.value = false
  actionError.value = ''
  activeDetailTab.value = 'files'
  taskFolderMessage.value = ''
  void loadTask()
}, { immediate: true })
watch(() => route.query.artifact, () => {
  autoOpenedQueryArtifactId = ''
  if (detail.value?.task.id === taskId.value) void openQueryArtifactIfRequested()
})
watch(() => selectedEditableTextArtifact.value?.id, (artifactId) => {
  transcriptComparisonSequence++
  transcriptComparison.value = null
  transcriptComparisonOpen.value = false
  transcriptComparisonMessage.value = ''
  transcriptUndoMessage.value = ''
  fullOcrEvidenceContent.value = ''
  fullOcrEvidenceMessage.value = ''
  if (artifactId && reviewCopyEditorActive.value) void loadTranscriptComparison(artifactId)
})
watch(editedText, () => {
  textCopyMessage.value = ''
  transcriptUndoMessage.value = ''
})
watch(() => editableTextArtifacts.value.map((artifact) => artifact.id).join(','), () => {
  if (!proofingTextOptions.value.some((artifact) => artifact.id === editableArtifactId.value)) {
    editableArtifactId.value = proofingTextOptions.value[0]?.id ?? ''
    if (editableArtifactId.value) void loadTextPreview(editableArtifactId.value)
  }
})
watch(() => reviewedTranscriptArtifacts.value.map((artifact) => artifact.id).join(','), () => {
  if (!reviewedTranscriptArtifacts.value.some((artifact) => artifact.id === selectedReviewedTranscriptId.value)) {
    selectedReviewedTranscriptId.value = reviewedTranscriptArtifacts.value[reviewedTranscriptArtifacts.value.length - 1]?.id ?? ''
  }
})
watch(vaultArtifacts, (items) => {
  if (!items.some((artifact) => artifact.id === selectedVaultArtifact.value)) {
    selectedVaultArtifact.value = items[0]?.id ?? ''
    vaultPreview.value = null
  }
  if (!vaultTargetPath.value && items[0]) {
    const base = (detail.value?.asset.title || items[0].name.replace(/\.md$/i, '') || '知识卡')
      .replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 100)
    vaultTargetPath.value = `${base}.md`
  }
}, { immediate: true })
onActivated(resumeTab)
onDeactivated(pauseTab)
onBeforeUnmount(pauseTab)
</script>

<template>
  <div class="asset-detail-page">
    <div class="page-heading detail-page-heading">
      <div class="detail-heading-copy">
        <div class="detail-title-row">
          <h1 v-if="detail" id="asset-detail-title" class="asset-detail-title" :class="{ 'is-collapsed': titleCanExpand && !titleExpanded }">{{ displayTitle }}</h1>
          <button v-if="detail && titleCanExpand" class="text-button detail-title-toggle" type="button" aria-controls="asset-detail-title" :aria-expanded="titleExpanded" @click="titleExpanded = !titleExpanded">
            {{ titleExpanded ? '收起标题' : '展开标题' }}
          </button>
        </div>
        <div v-if="detail && displayDescription" class="detail-description-row">
          <p id="asset-detail-description" class="detail-description" :class="{ 'is-collapsed': descriptionCanExpand && !descriptionExpanded }">{{ displayDescription }}</p>
          <button v-if="descriptionCanExpand" class="text-button detail-description-toggle" type="button" aria-controls="asset-detail-description" :aria-expanded="descriptionExpanded" @click="descriptionExpanded = !descriptionExpanded">
            {{ descriptionExpanded ? '收起简介' : '展开简介' }}
          </button>
        </div>
        <p v-if="detail" class="lead">
          <span class="status-badge" :class="statusClass(detail.task.status)">{{ statusLabel(detail.task.status) }}</span>
          <span>{{ detail.asset.source_kind === 'file' ? '本地媒体' : platformLabel(detail.asset.platform) }}</span>
          <span v-if="detail.asset.source_id">作品 ID：{{ detail.asset.source_id }}</span>
          <span v-if="detail.asset.source_kind === 'url' && detail.task.platform_access?.mode === 'account'">使用账号：{{ detail.task.platform_access.account_name || '历史采集登录态' }}</span>
          <span v-else-if="detail.asset.source_kind === 'url' && detail.task.platform_access">访问方式：匿名</span>
          <span>创建于 {{ formatDate(detail.task.created_at) }}</span>
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="button button-secondary" to="/history">返回素材档案</RouterLink>
        <button v-if="detail" class="button" type="button" :disabled="!canOpenTaskFolder || taskFolderBusy" @click="openTaskFolder">
          {{ taskFolderBusy ? '正在打开…' : '打开任务目录' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="panel panel-pad" role="status">正在读取本地任务详情…</div>
    <div v-else-if="error" class="info-box form-error" role="alert">
      <span>{{ error }}</span>
      <button class="text-button" type="button" @click="loadTask()">重新读取</button>
    </div>

    <template v-else-if="detail">
      <div class="detail-grid">
        <div class="detail-main">
      <section class="task-detail-progress" :class="{ 'has-attention': taskNeedsAttention }" aria-labelledby="detail-progress-title">
        <div class="task-progress-metric">
          <span id="detail-progress-title">任务进度</span>
          <strong>{{ taskProgressPercent }}%</strong>
        </div>
        <div class="task-progress-main">
          <div class="task-progress-topline">
            <strong class="task-progress-step-count">{{ completedStepCount }} / {{ detail.task.steps.length }} 步完成<span v-if="skippedStepCount"> · {{ skippedStepCount }} 步跳过</span></strong>
            <div class="task-progress-state" :class="{ 'is-attention': taskNeedsAttention }">
              <span class="task-progress-state-mark" aria-hidden="true">{{ taskNeedsAttention ? '!' : detail.task.status === 'completed' ? '✓' : '·' }}</span>
              <span class="task-progress-state-copy">
                <span class="task-progress-state-heading">
                  <strong>{{ detail.task.status === 'completed' ? '全部步骤已完成' : currentStepLabel }}</strong>
                  <span v-if="taskDurationLabel" class="task-progress-state-duration">{{ detail.task.status === 'completed' ? '用时：' : '执行时长：' }}{{ taskDurationLabel }}</span>
                </span>
                <span v-if="detail.task.status === 'completed'">暂无运行步骤</span>
                <span v-else-if="taskNeedsAttention">请检查步骤与停止原因</span>
                <span v-else-if="taskActive">状态同步中</span>
                <span v-else>暂无运行步骤</span>
              </span>
            </div>
            <div class="task-progress-actions">
              <button class="task-progress-record-button" type="button" aria-controls="task-steps" :aria-expanded="stepDetailsExpanded" @click="stepDetailsExpanded = !stepDetailsExpanded">
                {{ stepDetailsExpanded ? '收起步骤记录' : `查看步骤与运行记录 · ${detail.task.steps.length} 步` }}
              </button>
              <button v-if="canCancel" class="button button-secondary button-small" type="button" :disabled="actionBusy" @click="cancelTask">
                {{ actionBusy ? '正在提交…' : '取消任务' }}
              </button>
            </div>
          </div>
          <div class="task-progress-segments" :class="{ 'is-indeterminate': progressIndeterminate }" role="progressbar" :aria-valuenow="progressIndeterminate ? undefined : taskProgressPercent" aria-valuemin="0" aria-valuemax="100" :aria-label="`任务进度 ${taskProgressPercent}%`" :aria-valuetext="`${completedStepCount} / ${detail.task.steps.length} 步完成，${taskProgressPercent}%`">
            <span v-for="step in detail.task.steps" :key="step.id" class="task-progress-segment" :class="progressSegmentClass(step)" aria-hidden="true">
              <span :style="{ width: `${progressSegmentPercent(step)}%` }"></span>
            </span>
            <span v-if="!detail.task.steps.length" class="task-progress-segment" aria-hidden="true"></span>
            <span v-if="progressIndeterminate" class="task-progress-indeterminate" aria-hidden="true"></span>
          </div>
          <p v-if="taskActive || taskNeedsAttention" class="task-progress-message">{{ liveProgressMessage }}</p>
          <p v-if="taskActive" class="task-progress-help">{{ taskProgressHelp }}</p>
        </div>
        <div id="task-steps" class="task-step-disclosure" :hidden="!stepDetailsExpanded">
          <TaskSteps :task="detail.task" :artifacts="detail.artifacts" :consents="detail.consents" :model-runs="detail.model_runs" :transcription-config="detail.transcription_config" :comment-state="commentState" :busy="actionBusy" @retry="retryStep" @skip="skipStep" @reauthorize-comments="authorizeCommentAnalysis" />
          <details v-if="detail.model_runs.length" class="model-run-disclosure">
            <summary id="model-runs-title">云端模型调用记录 · {{ detail.model_runs.length }}</summary>
            <ul class="model-run-list">
              <li v-for="run in detail.model_runs" :key="run.id" class="model-run-item">
                <div class="panel-foot">
                  <strong>{{ run.provider_id }} · {{ run.model_id }}</strong>
                  <span class="status-badge" :class="statusClass(run.status)">{{ run.error_code === 'provider_timeout' ? '超时（结果未知）' : run.status === 'completed' ? '已返回' : run.status === 'running' ? '处理中' : run.status === 'failed' ? '失败' : run.status }}</span>
                </div>
                <p class="helper">材料产物 ID：<code>{{ run.artifact_ids.join('、') || '无' }}</code></p>
                <p class="helper">{{ modelUsage(run) }} · 费用未知</p>
                <p class="helper">请求时间：{{ formatDate(run.created_at) }}<template v-if="run.finished_at"> · 记录结束：{{ formatDate(run.finished_at) }}</template></p>
                <p v-if="clientRequestId(run)" class="helper">客户端请求 ID：<code>{{ clientRequestId(run) }}</code>（仅为本地生成，不能证明服务端已收到或完成）</p>
                <p v-if="run.request_id" class="helper">服务商请求 ID：<code>{{ run.request_id }}</code></p>
                <p v-else-if="run.error_code === 'provider_timeout'" class="helper">未取得服务商请求 ID；无法从本地记录确认服务商是否处理或计费。</p>
                <p v-if="run.error_code" class="helper">错误分类：<code>{{ run.error_code }}</code></p>
              </li>
            </ul>
          </details>
        </div>
      </section>

      <div v-if="actionError" class="info-box form-error" role="alert">{{ actionError }}</div>
      <p v-if="taskFolderMessage" class="helper" role="status">{{ taskFolderMessage }}</p>
          <div class="detail-tabs" role="tablist" aria-label="任务内容分类">
            <button
              v-for="tab in detailTabs"
              :id="`detail-tab-${tab.id}`"
              :key="tab.id"
              role="tab"
              type="button"
              :aria-selected="activeDetailTab === tab.id"
              :aria-controls="`detail-panel-${tab.id}`"
              :tabindex="activeDetailTab === tab.id ? 0 : -1"
              @click="selectDetailTab(tab.id)"
              @keydown="handleDetailTabKeydown($event, tab.id)"
            >{{ tab.label }}</button>
          </div>

          <section id="detail-panel-files" v-show="activeDetailTab === 'files'" class="panel panel-pad detail-tab-panel" role="tabpanel" aria-labelledby="detail-tab-files" tabindex="0">
            <div class="panel-head">
              <div><h2>任务产物</h2><p>仅显示本次任务已登记的文件，可直接打开或定位到所在目录。</p></div>
            </div>
            <ArtifactViewer :artifacts="detail.artifacts" :steps="detail.task.steps" @edit="editArtifact" />
            <StructureAnalysis :task-id="detail.task.id" :artifacts="detail.artifacts" :task-status="detail.task.status" @updated="loadTask(false)" />
          </section>

          <div id="detail-panel-proof" v-show="activeDetailTab === 'proof'" class="detail-tab-stack" role="tabpanel" aria-labelledby="detail-tab-proof" tabindex="0">
            <section class="panel panel-pad" aria-labelledby="transcript-edit-title">
              <div class="panel-head">
                <div><h2 id="transcript-edit-title">转写与画面校对</h2><p>原稿与校对稿分开保存。</p></div>
                <button class="button button-secondary button-small" type="button"
                  :disabled="!canReconcileTranscript || transcriptReconcileBusy || textPreviewBusy || textSaveBusy"
                  :title="transcriptReconcileUnavailableReason || '使用本任务已有的 ASR 与 OCR 结果生成新的校对草稿，不会重新识别。'"
                  @click="regenerateTranscriptDraft">
                  {{ transcriptReconcileBusy ? '正在重新校对…' : '重新校对' }}
                </button>
              </div>
              <p v-if="transcriptReconcileUnavailableReason" class="helper" role="status">{{ transcriptReconcileUnavailableReason }}</p>
              <p v-if="transcriptReconcileMessage" class="helper" role="status">{{ transcriptReconcileMessage }}</p>
              <template v-if="editableTextArtifacts.length">
                <div v-if="selectedEditableTextArtifact && selectedEditableTextArtifact.kind.includes('transcript') && selectedTranscriptionArtifact" class="detail-meta-inline transcript-method-summary">
                  <span>{{ transcriptionMethod(selectedTranscriptionArtifact) }} · {{ artifactMetadataText(selectedTranscriptionArtifact, 'model_id') }} · {{ artifactMetadataText(selectedTranscriptionArtifact, 'language', '语言未记录') }}<template v-if="artifactMetadataText(selectedTranscriptionArtifact, 'device', '')"> · {{ artifactMetadataText(selectedTranscriptionArtifact, 'device') }}</template></span>
                </div>
                <label class="form-label" for="editable-text-artifact">选择稿件</label>
                <select id="editable-text-artifact" v-model="editableArtifactId" class="select-control" :disabled="textPreviewBusy || textSaveBusy" @change="handleTextArtifactChange">
                  <option v-for="artifact in proofingTextOptions" :key="artifact.id" :value="artifact.id">
                    {{ textArtifactLabel(artifact) }}
                  </option>
                </select>
                <p class="helper">只列出人工校对稿、最新 AI 校对稿和原始转写；旧版本可在“产物”页的历史版本中查看。</p>
                <div class="button-row text-copy-row">
                  <button class="button button-secondary button-small" type="button" :disabled="textPreviewBusy || !editedText.trim()" @click="copySelectedText">{{ textPreviewBusy ? '正在载入…' : '复制文本' }}</button>
                  <button v-if="reviewCopyEditorActive" class="button button-secondary button-small" type="button"
                    :disabled="transcriptComparisonBusy || textPreviewBusy"
                    :aria-expanded="transcriptComparisonOpen"
                    @click="toggleTranscriptComparison">
                    {{ transcriptComparisonBusy ? '正在比对…' : `${transcriptComparisonOpen ? '收起差异' : '查看差异'}${transcriptComparisonOpen && transcriptComparison ? ` · ${transcriptComparisonChanges.length} 处` : ''}` }}
                  </button>
                  <p v-if="textCopyMessage" class="helper" role="status">{{ textCopyMessage }}</p>
                </div>
                <div v-if="transcriptComparisonMessage" class="info-box form-error" role="alert">{{ transcriptComparisonMessage }}</div>
                <section v-if="reviewCopyEditorActive && transcriptComparisonOpen" class="transcript-comparison-panel" aria-label="ASR 原稿与校对稿差异">
                  <div v-if="transcriptComparisonBusy" class="helper" role="status">正在载入 ASR 原稿和 OCR 依据…</div>
                  <template v-else-if="transcriptComparison">
                    <div v-if="!transcriptComparisonChanges.length" class="transcript-comparison-empty">ASR 原稿与当前校对稿正文一致，没有发现文字变化。</div>
                    <article v-for="change in transcriptComparisonChanges" :key="change.id" class="transcript-change-item">
                      <div class="transcript-change-time">{{ transcriptTimeLabel(change.startSeconds, change.endSeconds) }}</div>
                      <div class="transcript-diff-columns">
                        <div class="transcript-diff-side transcript-diff-original">
                          <span class="transcript-diff-label">原始 ASR</span>
                          <p><span>{{ change.commonPrefix }}</span><del v-if="change.removedText">{{ change.removedText }}</del><span v-else class="transcript-diff-empty">（无）</span><span>{{ change.commonSuffix }}</span></p>
                        </div>
                        <div class="transcript-diff-side transcript-diff-current">
                          <span class="transcript-diff-label">当前校对稿</span>
                          <p><span>{{ change.commonPrefix }}</span><ins v-if="change.insertedText">{{ change.insertedText }}</ins><span v-else class="transcript-diff-empty">（删除）</span><span>{{ change.commonSuffix }}</span></p>
                        </div>
                      </div>
                      <details v-for="(correction, correctionIndex) in ocrCorrectionsForChange(change)" :key="`${change.id}-${correctionIndex}`" class="transcript-change-evidence">
                        <summary>查看 OCR 依据<span v-if="correction.frame_ids?.length"> · {{ correction.frame_ids.length }} 帧</span></summary>
                        <p v-if="correction.ocr_text">OCR 摘录：<b>{{ correction.ocr_text }}</b></p>
                        <p v-if="correction.frame_ids?.length">画面帧：{{ correction.frame_ids.join('、') }}</p>
                        <p v-if="correction.frame_times_seconds?.length">抽帧时间：{{ correction.frame_times_seconds.map((time) => `${time.toFixed(1)} 秒`).join('、') }}</p>
                      </details>
                      <div class="transcript-change-actions">
                        <span>恢复为原始 ASR 文本，不会自动保存</span>
                        <button class="button button-secondary button-small" type="button"
                          :disabled="textSaveBusy || change.currentStartOffset === null || change.currentEndOffset === null"
                          title="将这处校对恢复为原始 ASR 文本；不会自动保存"
                          @click="restoreTranscriptChange(change)">撤回此处校对</button>
                      </div>
                    </article>
                    <p v-if="transcriptUndoMessage" class="helper transcript-undo-message" role="status">{{ transcriptUndoMessage }}</p>
                    <details v-if="transcriptComparison.ocr_result_artifact_id" class="transcript-full-ocr" @toggle="loadFullOcrEvidence">
                      <summary>查看完整 OCR 结果</summary>
                      <p v-if="fullOcrEvidenceBusy" class="helper" role="status">正在载入 OCR 结果…</p>
                      <p v-if="fullOcrEvidenceMessage" class="helper" role="status">{{ fullOcrEvidenceMessage }}</p>
                      <pre v-if="fullOcrEvidenceContent" class="transcript-full-ocr-content">{{ fullOcrEvidenceContent }}</pre>
                    </details>
                  </template>
                </section>
                <div v-if="selectedEditableTextArtifact && textPreviewBusy" class="info-box" role="status">正在读取所选版本…</div>
                <div v-if="selectedEditableTextArtifact && !textPreviewBusy && reviewCopyEditorActive" class="review-copy-editor-heading">
                  <h3>校对正文</h3>
                </div>
                <MarkdownTextEditor v-if="selectedEditableTextArtifact && !textPreviewBusy" v-model="editedText" :disabled="textSaveBusy" />
                <section v-if="mergedCommentFeedback && !textPreviewBusy" class="info-box merged-comment-feedback" aria-label="已合并的评论反馈">
                  <div class="merged-comment-feedback-heading">
                    <h3>已合并的评论反馈</h3>
                    <p>保存在当前转写稿末尾，后续稿件改写会读取这部分参考。</p>
                  </div>
                  <MarkdownPreview class="merged-comment-feedback-preview" :content="mergedCommentFeedback" />
                </section>
                <div v-if="selectedEditableTextArtifact && !textPreviewBusy" class="text-editor-save-bar">
                  <div class="text-editor-save-copy">
                    <button class="button button-secondary button-small" type="button" :disabled="!textHasUnsavedChanges || textSaveBusy || textPreviewBusy" @click="discardTextChanges">撤销修改</button>
                    <p class="helper">保存会生成新的校对版本，不会覆盖原始转写或 OCR 文件。</p>
                    <p v-if="textSaveMessage" class="helper" role="status">{{ textSaveMessage }}</p>
                  </div>
                  <button class="button button-small" type="button" :disabled="!textHasUnsavedChanges || textSaveBusy || textPreviewBusy" @click="saveTextRevision">
                    {{ textSaveBusy ? '正在保存…' : '保存' }}
                  </button>
                </div>
              </template>
              <div v-else class="empty-state empty-state-compact" role="status"><h3>暂无转写或 OCR 文本</h3><p>任务生成相关产物后，可在这里预览和校对。</p></div>
              <button class="text-button" type="button" @click="selectDetailTab('files')">按需分析内容结构 →</button>
              <div v-if="reviewedTranscriptArtifacts.length" class="video-notes-action">
                <label class="form-label" for="reviewed-transcript-artifact">生成资料所用的人工校对版本</label>
                <select id="reviewed-transcript-artifact" v-model="selectedReviewedTranscriptId" class="select-control">
                  <option v-for="artifact in reviewedTranscriptArtifacts" :key="artifact.id" :value="artifact.id">校对版本 · v{{ artifact.version }} · {{ formatDate(artifact.created_at) }}</option>
                </select>
                <p class="helper">在本机合并来源信息、人工校对文案、OCR 证据和已归档评论；生成新版本，不覆盖已有资料。</p>
                <button class="button button-secondary button-small" type="button" :disabled="videoNotesBusy || !selectedReviewedTranscriptId" @click="createVideoNotes">
                  {{ videoNotesBusy ? '正在生成本地资料…' : '生成视频资料.md' }}
                </button>
                <p v-if="videoNotesMessage" class="helper" role="status">{{ videoNotesMessage }}</p>
              </div>
            </section>

            <section v-if="pendingOcrStep" class="panel panel-pad" aria-labelledby="ocr-resume-title">
              <div class="panel-head"><div><h2 id="ocr-resume-title">继续 OCR 识别</h2><p>已生成的关键帧将发送给当前配置的云端图像模型，可能产生费用。</p></div></div>
              <button class="button" type="button" :disabled="actionBusy" @click="retryStep(pendingOcrStep.id)">
                {{ actionBusy ? '正在提交…' : '继续识别' }}
              </button>
            </section>
          </div>

          <div id="detail-panel-comments" v-show="activeDetailTab === 'comments'" class="detail-tab-stack" role="tabpanel" aria-labelledby="detail-tab-comments" tabindex="0">
            <CommentList v-if="detail.task.steps.some((step) => step.kind === 'comments')" :task-id="detail.task.id" :active="taskActive" @update:state="commentState = $event" />
            <CommentInsights v-if="detail.artifacts.some((artifact) => artifact.kind === 'comment_analysis_json')" :task-id="detail.task.id" :transcript-artifact-id="scriptTranscriptSource?.id ?? ''" @update:selected-comment-ids="selectedCommentIds = $event" @merged="loadTask(false)" />
            <section v-if="!detail.task.steps.some((step) => step.kind === 'comments') && !detail.artifacts.some((artifact) => artifact.kind === 'comment_analysis_json')" class="panel panel-pad" role="status">
              <h2>本次任务没有采集评论</h2><p class="helper">创建任务时选择评论采集后，评论和精选结果会显示在这里。</p>
            </section>
          </div>

          <div id="detail-panel-scripts" v-show="activeDetailTab === 'scripts'" class="detail-tab-stack" role="tabpanel" aria-labelledby="detail-tab-scripts" tabindex="0">
            <ScriptEditor ref="scriptEditor" :task-id="detail.task.id" :artifacts="detail.artifacts" :selected-comment-ids="selectedCommentIds" :can-regenerate="canRegenerateScripts" :regeneration-unavailable-reason="scriptRegenerationUnavailableReason" :regeneration-target="scriptRegenerationTarget" @updated="loadTask(false)" />
            <section v-if="!detail.artifacts.some((artifact) => ['script', 'script_revision', 'final_script'].includes(artifact.kind)) && !detail.task.steps.some((step) => step.kind === 'generate_scripts')" class="panel panel-pad" role="status">
              <h2>暂无稿件</h2><p class="helper">口播稿生成后会在这里预览和编辑；有可用转写稿时，可从右侧重新生成。</p>
            </section>
          </div>

          <div id="detail-panel-knowledge" v-show="activeDetailTab === 'knowledge'" class="detail-tab-stack" role="tabpanel" aria-labelledby="detail-tab-knowledge" tabindex="0">
            <KnowledgeCardReview ref="knowledgeEditor" :artifacts="detail.artifacts" @updated="loadTask(false)" />
            <section v-if="vaultArtifacts.length" class="panel panel-pad vault-write-panel" aria-labelledby="vault-write-title">
              <div class="panel-head"><div><h2 id="vault-write-title">写入知识库</h2><p>从定稿口播稿中选择；先预览内容，再确认写入。</p></div></div>
              <div class="field-row">
                <label class="settings-field"><span class="form-label">选择要写入的产物</span><select v-model="selectedVaultArtifact" class="select-control" @change="vaultPreview = null"><option v-for="artifact in vaultArtifacts" :key="artifact.id" :value="artifact.id">{{ artifact.name }} · 定稿</option></select></label>
                <label class="settings-field"><span class="form-label">输出目录内的相对路径</span><input v-model="vaultTargetPath" class="input" type="text" maxlength="1024" placeholder="定稿/标题.md" @input="vaultPreview = null" /></label>
              </div>
              <div class="panel-foot"><span class="helper">仅接受 `.md`；已有不同内容的文件不会被覆盖。</span><button class="button button-secondary button-small" type="button" :disabled="vaultBusy || !selectedVaultArtifact || !vaultTargetPath.trim()" @click="previewVaultArtifact">{{ vaultBusy ? '正在准备…' : '预览写入内容' }}</button></div>
              <div v-if="vaultMessage" class="info-box" role="status">{{ vaultMessage }}</div>
              <div v-if="vaultPreview" class="vault-preview">
                <div class="scope-tags"><span>{{ vaultPreview.exists ? '目标已存在' : '将新建文件' }}</span><span>{{ vaultPreview.target_path }}</span><span>SHA-256 {{ vaultPreview.proposed_sha256.slice(0, 12) }}</span></div>
                <pre>{{ vaultPreview.content }}</pre>
                <div class="panel-foot"><button v-if="vaultPreview.conflict && vaultPreview.suggested_path" class="button button-secondary button-small" type="button" :disabled="vaultBusy" @click="useSuggestedVaultPath">另存为 {{ vaultPreview.suggested_path }}</button><button v-else class="button button-small" type="button" :disabled="vaultBusy || vaultPreview.conflict" @click="saveVaultArtifact">{{ vaultPreview.exists ? '确认（无需修改）' : '确认新建到知识库' }}</button></div>
              </div>
            </section>
            <section v-if="!detail.artifacts.some((artifact) => ['knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved'].includes(artifact.kind))" class="panel panel-pad" role="status">
              <h2>{{ detail.task.steps.some((step) => step.kind === 'knowledge_cards') ? '知识卡尚未生成' : '暂无知识卡' }}</h2><p class="helper">知识卡生成后会在这里单独审核和编辑；如步骤正在运行或遇到问题，可查看上方的运行记录。</p>
            </section>
          </div>
        </div>

        <aside class="detail-aside" aria-label="稿件操作、作品信息与归档">
          <section v-show="activeDetailTab === 'scripts'" class="panel aside-card continue-creation-card" aria-labelledby="continue-creation-title">
            <div class="aside-head"><div><h2 id="continue-creation-title">继续创作</h2><p>基于已审核定稿继续处理。</p></div></div>
            <section v-if="finalizedScriptArtifacts.length" class="article-generation-panel" aria-labelledby="article-generation-title">
              <div class="article-generation-heading"><div><h3 id="article-generation-title">生成博文</h3><p>从定稿创建文章草稿，之后可继续编辑。</p></div><RouterLink class="text-button" to="/articles">文章管理 →</RouterLink></div>
              <p v-if="articleGenerationError" class="info-box form-error" role="alert">{{ articleGenerationError }}</p>
              <ul class="article-generation-list">
                <li v-for="artifact in finalizedScriptArtifacts" :key="artifact.id">
                  <div><strong>{{ typeof artifact.metadata.title === 'string' ? artifact.metadata.title : artifact.name }}</strong><span>人工审核定稿 · v{{ artifact.version }}</span></div>
                  <button class="button button-small" type="button" :disabled="!!articleGenerationBusyId" @click="createArticleFromScriptArtifact(artifact)">{{ articleGenerationBusyId === artifact.id ? '正在生成…' : '生成博文' }}</button>
                </li>
              </ul>
            </section>
            <p v-else class="helper">审核稿件并设为定稿后，可从这里生成博文。</p>
            <div ref="scriptRegenerationTarget" class="script-regeneration-dock"></div>
            <div class="aside-next-step"><span>写入知识库</span><button class="text-button" type="button" @click="activeDetailTab = 'knowledge'">去知识卡页签预览 →</button></div>
            <p class="continue-creation-cost">模型费用未知。生成前会再次确认本次使用的材料。</p>
          </section>
          <section class="panel aside-card">
            <div class="aside-head"><div><h2>作品信息与归档</h2><p>来源信息和本机保存位置。</p></div></div>
            <dl class="meta-list">
              <div class="meta-row"><dt>来源</dt><dd>{{ detail.asset.source_kind === 'file' ? '本地媒体' : platformLabel(detail.asset.platform) }}</dd></div>
              <div class="meta-row"><dt>作者</dt><dd>{{ videoAuthor }}</dd></div>
              <div class="meta-row"><dt>发布时间</dt><dd>{{ videoPublishedAt }}</dd></div>
              <div class="meta-row"><dt>时长</dt><dd>{{ videoDuration }}</dd></div>
              <div class="meta-row"><dt>分辨率</dt><dd>{{ videoResolution }}</dd></div>
              <div class="meta-row"><dt>作品 ID</dt><dd><code>{{ detail.asset.source_id || '—' }}</code></dd></div>
              <div class="meta-row"><dt>归档位置</dt><dd>{{ archivePathLabel(detail.asset.archive_relative_path) }}</dd></div>
              <div class="meta-row"><dt>归档时间</dt><dd>{{ formatDate(detail.asset.archived_at) }}</dd></div>
            </dl>
            <button class="button button-secondary button-small detail-folder-button" type="button" :disabled="!canOpenTaskFolder || taskFolderBusy" @click="openTaskFolder">{{ taskFolderBusy ? '正在打开…' : '打开任务目录' }}</button>
            <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'folders' } }">查看目录设置 →</RouterLink>
            <div class="detail-pending-group">
              <div class="aside-head"><div><h2>待处理事项</h2><p>需要授权或检查时在这里提示。</p></div></div>
              <div v-if="pendingOcrStep" class="detail-pending-item">
                <strong>OCR 等待继续</strong><span>继续后将使用当前云端图像模型识别已生成的关键帧。</span>
                <button class="text-button" type="button" :disabled="actionBusy" @click="retryStep(pendingOcrStep.id)">继续识别 →</button>
              </div>
              <div v-else-if="failedStep" class="detail-pending-item">
                <strong>{{ stepLabels[failedStep.kind] ?? failedStep.kind }}需要处理</strong><span>{{ failedStep.message || '查看步骤记录了解停止原因。' }}</span>
              </div>
              <div v-else-if="taskActive" class="detail-empty">任务运行中，页面会自动同步步骤状态。</div>
              <div v-else class="detail-empty">暂无待处理事项</div>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本页(含子组件)的覆盖(全量落地时上移)—— */
.page-heading h1 { font-size: var(--fs-title-2xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); letter-spacing: -.01em; }
.detail-page-heading > .detail-heading-copy { min-width: 0; flex: 1 1 auto; }
.detail-title-row { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }
.detail-page-heading .asset-detail-title { min-width: 0; flex: 1 1 auto; margin: 0; color: var(--text-primary); overflow-wrap: anywhere; }
.detail-page-heading .asset-detail-title.is-collapsed { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.detail-title-toggle, .detail-description-toggle { flex: 0 0 auto; font-size: var(--fs-body-sm); }
.detail-description-row { display: flex; align-items: flex-end; gap: 8px; min-width: 0; margin-top: 5px; }
.detail-page-heading .detail-description { min-width: 0; flex: 1 1 auto; margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); overflow-wrap: anywhere; }
.detail-page-heading .detail-description.is-collapsed { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.task-progress-state-heading { display: flex; min-width: 0; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; }
.task-progress-state-duration { color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.page-heading .lead { color: var(--text-tertiary); font-size: var(--fs-body); }
.panel-head h2 { font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.aside-card h2 { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.review-copy-editor-heading h3 { margin: 0 0 3px; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.merged-comment-feedback { display: grid; min-width: 0; gap: .75rem; margin-top: 1rem; }
.merged-comment-feedback-heading h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.merged-comment-feedback-heading p { margin: .25rem 0 0; color: var(--text-secondary); font-size: var(--fs-body-sm); }
.merged-comment-feedback-preview { min-width: 0; max-height: 24rem; overflow: auto; }
.merged-comment-feedback-preview :deep(table) { width: 100%; table-layout: fixed; border-collapse: collapse; }
.merged-comment-feedback-preview :deep(th), .merged-comment-feedback-preview :deep(td) { padding: .5rem; border: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
.merged-comment-feedback-preview :deep(th:first-child), .merged-comment-feedback-preview :deep(td:first-child) { width: 3.5rem; }
.merged-comment-feedback-preview :deep(th:nth-child(2)), .merged-comment-feedback-preview :deep(td:nth-child(2)) { width: 5rem; }
.asset-detail-page :deep(.form-label) { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.asset-detail-page :deep(.helper) { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.asset-detail-page :deep(.info-box) { font-size: var(--fs-body); line-height: var(--lh-base); }
.asset-detail-page :deep(.status-badge) { min-height: 24px; padding: 0 10px; font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.asset-detail-page :deep(.empty-state h3) { color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.asset-detail-page :deep(.empty-state p) { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.scope-tags { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: var(--text-tertiary); font-size: var(--fs-caption); }
.detail-empty { color: var(--text-tertiary); font-size: var(--fs-body-sm); }

.continue-creation-card .aside-head { margin-bottom: 0; }
.article-generation-panel { display: grid; gap: 9px; }
.article-generation-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.article-generation-heading h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.article-generation-heading p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.article-generation-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.article-generation-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 9px; border: 1px solid var(--line); border-radius: 6px; }
.article-generation-list li > div { display: grid; flex: 1 1 auto; gap: 4px; min-width: 0; }
.article-generation-list strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); overflow-wrap: anywhere; }
.article-generation-list span { color: var(--text-tertiary); font-size: var(--fs-caption); }
.script-regeneration-dock { min-width: 0; }
.aside-next-step { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-top: 10px; border-top: 1px solid var(--line); }
.aside-next-step > span { color: var(--text-secondary); font-size: var(--fs-body-sm); }
.aside-next-step .text-button { min-height: 30px; font-size: var(--fs-body-sm); }
.continue-creation-cost { margin: 0; padding: 8px 9px; border: 1px solid var(--warning-soft); border-radius: 6px; color: var(--warning); background: var(--warning-soft); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.detail-pending-group { display: grid; gap: 9px; margin-top: 1px; padding-top: 12px; border-top: 1px solid var(--line); }
.detail-pending-group .aside-head { margin: 0; }
.detail-pending-group .aside-head h2 { font-size: var(--fs-title-md); }
.detail-pending-group .aside-head p { margin-top: 3px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.detail-pending-group .detail-pending-item { border-radius: 6px; }
@media (max-width: 600px) {
  .article-generation-heading { flex-direction: column; }
  .article-generation-list li { align-items: flex-start; flex-direction: column; }
}
</style>
