<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { TaskArtifact, TaskStep } from '../api/types'
import ArtifactRow from './ArtifactRow.vue'
import MarkdownPreview from './MarkdownPreview.vue'

const props = defineProps<{ artifacts: TaskArtifact[]; steps: TaskStep[] }>()
const emit = defineEmits<{ (event: 'edit', artifact: TaskArtifact): void }>()
const openingId = ref('')
const openMessage = ref('')
const openError = ref('')
const expanded = ref(new Set<string>())
const previewDialog = ref<HTMLDialogElement | null>(null)
const infoDialog = ref<HTMLDialogElement | null>(null)
const previewArtifact = ref<TaskArtifact | null>(null)
const infoArtifact = ref<TaskArtifact | null>(null)
const previewText = ref('')
const previewError = ref('')
const previewLoading = ref(false)
const previewFailed = ref(false)
let previewSequence = 0
let returnFocus: HTMLElement | null = null

const editableKinds = new Set(['transcript', 'transcript_draft', 'transcript_revision', 'ocr_result', 'ocr_revision', 'script', 'script_revision', 'final_script', 'knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved'])
const scriptKinds = new Set(['script', 'script_revision', 'final_script'])
const noteKinds = new Set(['note', 'video_notes', 'comment_analysis', 'analysis'])
const mediaKinds = new Set(['video', 'media', 'audio', 'cover'])
function isInternal(a: TaskArtifact): boolean {
  return a.kind === 'metadata' || a.name === 'metadata.md' || ['asr_audio_chunk', 'ocr_frame'].includes(a.kind) || a.kind.endsWith('_raw') || a.kind.endsWith('_json')
}
function groupKey(a: TaskArtifact): string {
  if (a.kind === 'note') return a.metadata.scope === 'comments' ? 'comments' : a.metadata.scope === 'video_notes' ? 'video_notes' : `note:${a.id}`
  if (a.kind === 'video_notes') return 'video_notes'
  if (a.kind === 'ocr_revision') return 'ocr_result'
  if (a.kind === 'transcript_revision') return a.metadata.review_copy === true ? 'transcript_draft' : 'transcript_revision'
  if (['knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved'].includes(a.kind)) return `card:${String(a.metadata.title || a.id)}`
  if (['video', 'media', 'audio', 'cover', 'transcript', 'transcript_draft', 'ocr_result', 'comment_analysis', 'analysis'].includes(a.kind)) return a.kind
  return `other:${a.id}`
}
function label(a: TaskArtifact): string {
  if (a.kind === 'video') return '原始视频'
  if (a.kind === 'cover') return a.metadata.cover_source === 'video_frame' ? '视频首帧预览' : '平台封面'
  if (a.kind === 'media' || a.kind === 'audio') return a.mime_type.startsWith('audio/') || a.metadata.media_type === 'audio' ? '本地音频' : '本地媒体'
  if (a.kind === 'note') return a.metadata.scope === 'comments' ? '评论资料' : '视频资料'
  if (a.kind === 'video_notes') return '视频资料'
  if (a.kind === 'transcript') return '语音转写'
  if (a.kind === 'transcript_draft') return '转写校对草稿'
  if (a.kind === 'transcript_revision') return a.metadata.review_copy === true ? '人工校对草稿' : '语音转写 · 人工修订'
  if (a.kind === 'ocr_result' || a.kind === 'ocr_revision') return '画面文字 OCR'
  if (a.kind === 'comment_analysis') return '评论精选与摘要'
  if (a.kind === 'analysis') return '内容结构分析'
  if (scriptKinds.has(a.kind)) return typeof a.metadata.style === 'string' ? a.metadata.style : '口播稿'
  if (a.kind.startsWith('knowledge_card')) return typeof a.metadata.title === 'string' ? `知识卡 · ${a.metadata.title}` : '知识卡片'
  return '其他文件'
}
function details(a: TaskArtifact): string {
  const ext = a.name.split('.').pop()?.toUpperCase() || '文件'
  if (a.kind === 'transcript') return `${ext} · 原稿`
  if (a.kind === 'transcript_draft') return `${ext} · 待校对`
  if (a.kind === 'ocr_result') return `${ext} · 待核对`
  if (a.kind === 'knowledge_card_approved' || a.kind === 'final_script') return `${ext} · 已审核`
  if (a.kind === 'knowledge_card') return `${ext} · 待审核`
  if (a.metadata.human_edited === true) return `${ext} · 人工修订`
  if (scriptKinds.has(a.kind)) return `${ext} · 草稿`
  return ext
}
function versionDetails(a: TaskArtifact): string {
  const date = new Date(a.created_at)
  const time = Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleString('zh-CN')
  return `${details(a)} · v${a.version} · ${time}`
}
function icon(a: TaskArtifact): string {
  if (a.kind === 'video' || a.mime_type.startsWith('video/')) return '▶'
  if (a.kind === 'cover' || a.mime_type.startsWith('image/')) return '图'
  if (a.kind === 'media' || a.kind === 'audio') return '音'
  if (a.kind.startsWith('ocr')) return '字'
  if (a.kind.startsWith('transcript')) return a.kind === 'transcript' ? '语' : '校'
  if (scriptKinds.has(a.kind)) return '稿'
  if (a.kind.startsWith('knowledge_card')) return '卡'
  return a.metadata.scope === 'comments' ? '评' : '文'
}
function tone(a: TaskArtifact): string {
  if (a.kind === 'video' || a.mime_type.startsWith('video/')) return 'video'
  if (a.kind === 'cover' || a.kind.startsWith('ocr') || a.mime_type.startsWith('image/')) return 'image'
  if (scriptKinds.has(a.kind)) return 'script'
  return 'document'
}
function previewLabel(a: TaskArtifact): string {
  if (a.mime_type.startsWith('video/') || a.mime_type.startsWith('audio/')) return '播放'
  if (a.mime_type.startsWith('image/')) return '预览'
  if (a.kind.startsWith('knowledge_card')) return '查看 / 审核'
  if (scriptKinds.has(a.kind)) return '预览 / 编辑'
  if (editableKinds.has(a.kind)) return '预览 / 校对'
  if (noteKinds.has(a.kind)) return '预览'
  return ''
}
type Group = { key: string; items: TaskArtifact[] }
type EntryOrder = { key: string; ordinal: number; createdAt: string; tieId: string }
type UnnumberedDocumentEntry =
  | (EntryOrder & { kind: 'document'; group: Group })
  | (EntryOrder & { kind: 'scripts'; items: TaskArtifact[] })
type DocumentEntry =
  | (EntryOrder & { kind: 'document'; group: Group; sequence: number })
  | (EntryOrder & { kind: 'scripts'; items: TaskArtifact[]; sequences: number[]; sequenceStart: number; sequenceEnd: number })
function groupArtifacts(items: TaskArtifact[]): Group[] {
  const grouped = new Map<string, TaskArtifact[]>()
  for (const item of items) {
    const key = groupKey(item)
    grouped.set(key, [...(grouped.get(key) ?? []), item])
  }
  return [...grouped.entries()].map(([key, values]) => ({ key, items: values.sort((a, b) => b.created_at.localeCompare(a.created_at) || b.version - a.version || b.id.localeCompare(a.id)) }))
}
const visible = computed(() => props.artifacts.filter(a => !isInternal(a)))
const mediaGroups = computed(() => groupArtifacts(visible.value.filter(a => mediaKinds.has(a.kind) || a.mime_type.startsWith('video/') || a.mime_type.startsWith('audio/') || a.mime_type.startsWith('image/'))))
const documentGroups = computed(() => groupArtifacts(visible.value.filter(a => !scriptKinds.has(a.kind) && !mediaKinds.has(a.kind) && !/^(video|audio|image)\//.test(a.mime_type))))
const scripts = computed(() => visible.value.filter(a => scriptKinds.has(a.kind)).sort((a,b) => b.created_at.localeCompare(a.created_at) || b.version - a.version || b.id.localeCompare(a.id)))
const stepOrdinals = computed(() => new Map(props.steps.map(step => [step.id, step.ordinal])))
const scriptGroups = computed(() => {
  const grouped = new Map<string, TaskArtifact[]>()
  for (const item of scripts.value) grouped.set(item.step_id, [...(grouped.get(item.step_id) ?? []), item])
  return [...grouped.entries()].map(([stepId, items]) => {
    const orderedItems = items.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.version - b.version || a.id.localeCompare(b.id))
    return {
      key: `scripts:${stepId}`,
      items: orderedItems,
      ordinal: stepOrdinals.value.get(stepId) ?? Number.MAX_SAFE_INTEGER,
      createdAt: orderedItems[0]?.created_at ?? '',
      tieId: stepId,
    }
  })
})
const documentEntries = computed<DocumentEntry[]>(() => {
  const entries: UnnumberedDocumentEntry[] = [
    ...documentGroups.value.map(group => {
      const primary = group.items[0]
      return {
        kind: 'document' as const,
        key: group.key,
        group,
        ordinal: stepOrdinals.value.get(primary.step_id) ?? Number.MAX_SAFE_INTEGER,
        createdAt: primary.created_at,
        tieId: primary.id,
      }
    }),
    ...scriptGroups.value.map(group => ({
      kind: 'scripts' as const,
      key: group.key,
      items: group.items,
      ordinal: group.ordinal,
      createdAt: group.createdAt,
      tieId: group.tieId,
    })),
  ]
  entries.sort((a, b) => a.ordinal - b.ordinal || a.createdAt.localeCompare(b.createdAt) || a.tieId.localeCompare(b.tieId))

  let nextSequence = 1
  return entries.map(entry => {
    if (entry.kind === 'document') return { ...entry, sequence: nextSequence++ }
    const sequences = entry.items.map(() => nextSequence++)
    return {
      ...entry,
      sequences,
      sequenceStart: sequences[0] ?? nextSequence,
      sequenceEnd: sequences[sequences.length - 1] ?? nextSequence - 1,
    }
  })
})
function sequenceLabel(sequence: number): string { return String(sequence).padStart(2, '0') }
function sequenceRange(entry: Extract<DocumentEntry, { kind: 'scripts' }>): string {
  const start = sequenceLabel(entry.sequenceStart)
  return entry.sequenceStart === entry.sequenceEnd ? start : `${start}–${sequenceLabel(entry.sequenceEnd)}`
}
const latestComments = computed(() => visible.value.filter(a => a.kind === 'note' && a.metadata.scope === 'comments').sort((a,b) => b.created_at.localeCompare(a.created_at))[0])
const staleAnalysis = computed(() => Boolean(latestComments.value && visible.value.some(a => a.kind === 'comment_analysis' && Array.isArray(a.metadata.source_artifact_ids) && !a.metadata.source_artifact_ids.includes(latestComments.value!.id)) && !visible.value.some(a => a.kind === 'comment_analysis' && Array.isArray(a.metadata.source_artifact_ids) && a.metadata.source_artifact_ids.includes(latestComments.value!.id))))
function toggleHistory(key: string): void {
  const next = new Set(expanded.value); if (next.has(key)) next.delete(key); else next.add(key); expanded.value = next
}
async function openArtifact(a: TaskArtifact, reveal: boolean): Promise<void> {
  if (openingId.value) return
  openingId.value = a.id; openError.value = ''; openMessage.value = ''
  try { await api.openArtifact(a.id, reveal); openMessage.value = reveal ? `已打开 ${a.name} 所在目录。` : `已打开 ${a.name}。` }
  catch (cause) { openError.value = cause instanceof ApiError ? cause.message : '无法打开本机文件。' }
  finally { openingId.value = '' }
}
async function readNotePreview(a: TaskArtifact): Promise<string> {
  try {
    return (await api.getTextArtifact(a.id)).content
  } catch (cause) {
    // Older local services allow downloading notes but do not expose them through /text.
    if (!(cause instanceof ApiError) || cause.status !== 422 || !['note', 'video_notes', 'comment_analysis'].includes(a.kind)) throw cause
    const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(a.id)}/file`, { credentials: 'same-origin', mode: 'same-origin', cache: 'no-store' })
    if (!response.ok || !response.body) throw new ApiError(response.status, '无法读取文档，请检查文件状态并重启工作台。')
    const limit = 5 * 1024 * 1024
    if (Number(response.headers.get('Content-Length')) > limit) throw new ApiError(413, '文档过大，请打开文件查看。')
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let total = 0
    let content = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        total += value.byteLength
        if (total > limit) { await reader.cancel(); throw new ApiError(413, '文档过大，请打开文件查看。') }
        content += decoder.decode(value, { stream: true })
      }
      return content + decoder.decode()
    } catch (error) {
      if (error instanceof ApiError) throw error
      throw new ApiError(422, '文档编码无法识别，请打开文件查看。')
    }
  }
}
async function preview(a: TaskArtifact): Promise<void> {
  if (a.file_available === false) return
  if (editableKinds.has(a.kind)) { emit('edit', a); return }
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  previewArtifact.value = a; previewText.value = ''; previewError.value = ''; previewFailed.value = false
  const sequence = ++previewSequence
  await nextTick(); previewDialog.value?.showModal()
  if (noteKinds.has(a.kind)) {
    previewLoading.value = true
    try { const content = await readNotePreview(a); if (sequence === previewSequence) previewText.value = content }
    catch (cause) { if (sequence === previewSequence) previewError.value = cause instanceof ApiError ? cause.message : '无法预览文件。' }
    finally { if (sequence === previewSequence) previewLoading.value = false }
  }
}
async function showInfo(a: TaskArtifact): Promise<void> {
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  infoArtifact.value = a; await nextTick(); infoDialog.value?.showModal()
}
function closeDialog(dialog: HTMLDialogElement | null): void { dialog?.close(); previewSequence++; previewLoading.value = false; returnFocus?.focus() }
function closePreviewDialog(restoreFocus = true): void {
  const media = previewDialog.value?.querySelector<HTMLMediaElement>('video, audio')
  if (media) {
    media.pause()
    media.removeAttribute('src')
    media.load()
  }
  previewDialog.value?.close()
  previewSequence++
  previewLoading.value = false
  previewText.value = ''
  previewArtifact.value = null
  if (restoreFocus) returnFocus?.focus()
  returnFocus = null
}
watch(() => props.artifacts[0]?.task_id, () => { expanded.value = new Set(); closePreviewDialog(false); infoDialog.value?.close() })
onBeforeUnmount(() => { closePreviewDialog(false); infoDialog.value?.close() })
</script>

<template>
  <p v-if="staleAnalysis" class="helper" role="status">评论已更新，旧评论分析基于之前的资料；可重新运行评论分析。</p>
  <div v-if="visible.length" class="artifact-list" aria-label="任务成果清单">
    <template v-if="mediaGroups.length"><div class="artifact-group-heading" data-artifact-group="media">媒体 <span>视频、音频与封面</span></div>
      <template v-for="group in mediaGroups" :key="group.key"><ArtifactRow :artifact="group.items[0]" :label="label(group.items[0])" :icon="icon(group.items[0])" :tone="tone(group.items[0])" :description="details(group.items[0])" :preview-label="previewLabel(group.items[0])" :busy="Boolean(openingId)" :versions="group.items.length" :history-open="expanded.has(group.key)" @preview="preview" @open="openArtifact" @info="showInfo" @history="toggleHistory(group.key)" /><template v-if="expanded.has(group.key)"><ArtifactRow v-for="item in group.items.slice(1)" :key="item.id" :artifact="item" :label="label(item)" :icon="icon(item)" :tone="tone(item)" :description="versionDetails(item)" :preview-label="previewLabel(item)" :busy="Boolean(openingId)" historical @preview="preview" @open="openArtifact" @info="showInfo" /></template></template>
    </template>
    <template v-if="documentEntries.length"><div class="artifact-group-heading" data-artifact-group="documents">文档与整理 <span>预览、校对与审核</span></div>
      <template v-for="entry in documentEntries" :key="entry.key">
        <template v-if="entry.kind === 'document'"><ArtifactRow :artifact="entry.group.items[0]" :label="label(entry.group.items[0])" :icon="icon(entry.group.items[0])" :tone="tone(entry.group.items[0])" :description="details(entry.group.items[0])" :preview-label="previewLabel(entry.group.items[0])" :busy="Boolean(openingId)" :sequence="entry.sequence" :versions="entry.group.items.length" :history-open="expanded.has(entry.group.key)" @preview="preview" @open="openArtifact" @info="showInfo" @history="toggleHistory(entry.group.key)" /><template v-if="expanded.has(entry.group.key)"><ArtifactRow v-for="item in entry.group.items.slice(1)" :key="item.id" :artifact="item" :label="label(item)" :icon="icon(item)" :tone="tone(item)" :description="versionDetails(item)" :preview-label="previewLabel(item)" :busy="Boolean(openingId)" historical @preview="preview" @open="openArtifact" @info="showInfo" /></template></template>
        <details v-else class="artifact-script-group"><summary><span class="artifact-type-icon artifact-type-icon--script" aria-hidden="true">稿</span><span><strong>改写口播稿</strong><small>{{ sequenceRange(entry) }} · {{ entry.items.length }} 份稿件与版本 · 点击展开</small></span><span class="artifact-script-chevron" aria-hidden="true">⌄</span></summary><ArtifactRow v-for="(item, index) in entry.items" :key="item.id" :artifact="item" :label="label(item)" :icon="icon(item)" :tone="tone(item)" :description="versionDetails(item)" :preview-label="previewLabel(item)" :busy="Boolean(openingId)" :sequence="entry.sequences[index]" historical @preview="preview" @open="openArtifact" @info="showInfo" /></details>
      </template>
    </template>
  </div>
  <div v-else class="empty-state empty-state-compact" role="status"><h3>暂无处理产物</h3><p>完成的媒体与文档会显示在此处。</p></div>
  <p v-if="openMessage" class="helper" role="status">{{ openMessage }}</p><p v-if="openError" class="info-box form-error" role="alert">{{ openError }}</p>
  <dialog ref="previewDialog" class="artifact-dialog" aria-labelledby="artifact-preview-title" @cancel.prevent="closePreviewDialog()"><div class="artifact-dialog-head"><h2 id="artifact-preview-title">{{ previewArtifact ? label(previewArtifact) : '文件预览' }}</h2><button type="button" class="text-button" aria-label="关闭预览" @click="closePreviewDialog()">关闭</button></div><div v-if="previewArtifact" class="artifact-dialog-body"><video v-if="previewArtifact.mime_type.startsWith('video/')" controls autoplay playsinline preload="metadata" :src="`${previewArtifact.file_url}?inline=1`" @error="previewFailed = true"></video><audio v-else-if="previewArtifact.mime_type.startsWith('audio/')" controls autoplay preload="metadata" :src="`${previewArtifact.file_url}?inline=1`" @error="previewFailed = true"></audio><img v-else-if="previewArtifact.mime_type.startsWith('image/')" :src="`${previewArtifact.file_url}?inline=1`" :alt="label(previewArtifact)" @error="previewFailed = true" /><template v-else><p v-if="previewLoading" class="helper" role="status">正在读取文档…</p><p v-else-if="previewError" class="info-box form-error" role="alert">{{ previewError }}</p><MarkdownPreview v-else :content="previewText || '文件内容为空。'" /></template><p v-if="previewFailed" class="info-box form-error" role="alert">文件无法预览，可检查文件状态后重试。</p></div><div class="artifact-dialog-foot"><button class="button button-secondary button-small" type="button" @click="closePreviewDialog()">关闭</button><button v-if="previewArtifact" class="button button-secondary button-small" type="button" :disabled="previewArtifact.file_available === false || Boolean(openingId)" @click="openArtifact(previewArtifact, false)">打开文件</button></div></dialog>
  <dialog ref="infoDialog" class="artifact-dialog" aria-labelledby="artifact-info-title" @cancel.prevent="closeDialog(infoDialog)"><div class="artifact-dialog-head"><h2 id="artifact-info-title">文件详情</h2><button type="button" class="text-button" aria-label="关闭详情" @click="closeDialog(infoDialog)">关闭</button></div><div v-if="infoArtifact" class="artifact-dialog-body artifact-info"><p><strong>文件：</strong>{{ infoArtifact.name }}</p><p><strong>归档位置：</strong>{{ infoArtifact.relative_path || '路径不可用' }}</p><p><strong>状态：</strong>{{ infoArtifact.file_available === false ? '文件不可用' : '已登记' }}</p><p><strong>版本：</strong>v{{ infoArtifact.version }} · {{ new Date(infoArtifact.created_at).toLocaleString('zh-CN') }}</p><p><strong>SHA-256：</strong><code>{{ infoArtifact.sha256 }}</code></p></div><div class="artifact-dialog-foot"><a v-if="infoArtifact && infoArtifact.file_available !== false" class="text-button" :href="infoArtifact.file_url" :download="infoArtifact.name">下载副本</a><button class="button button-secondary button-small" type="button" @click="closeDialog(infoDialog)">关闭</button></div></dialog>
</template>
