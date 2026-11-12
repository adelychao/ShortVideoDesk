<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, api } from '../api/client'
import type { TaskArtifact, ConfigResponse } from '../api/types'

const props = defineProps<{ taskId: string; artifacts: TaskArtifact[]; taskStatus: string }>()
const emit = defineEmits<{ (event: 'updated'): void }>()
const dialog = ref<HTMLDialogElement | null>(null)
const config = ref<ConfigResponse | null>(null)
const transcriptId = ref('')
const ocrId = ref('')
const includeOcr = ref(false)
const confirmed = ref(false)
const busy = ref(false)
const error = ref('')
const message = ref('')
const selectedId = ref('')
const sections = ref<Array<{ kind: string; present: boolean; source_excerpt: string | null; analysis: string; source_artifact_id: string; verified?: boolean }>>([])
const selectedEvidence = ref<number | null>(null)
const evidenceSource = ref('')
const sortNewest = (a: TaskArtifact, b: TaskArtifact) => b.created_at.localeCompare(a.created_at)
const transcripts = computed(() => props.artifacts.filter(a => ['transcript', 'transcript_draft', 'transcript_revision'].includes(a.kind) && a.file_available !== false).sort(sortNewest))
const ocr = computed(() => props.artifacts.filter(a => ['ocr_result', 'ocr_revision'].includes(a.kind) && a.file_available !== false).sort(sortNewest))
const history = computed(() => props.artifacts.filter(a => a.kind === 'structure_analysis_json').sort(sortNewest))
const canStart = computed(() => transcripts.value.length > 0 && !['queued', 'running', 'awaiting_authorization', 'cancelled'].includes(props.taskStatus))
const provider = computed(() => config.value?.settings.model_provider ?? '')
const model = computed(() => config.value?.settings.model_id ?? '')
const selectedResult = computed(() => history.value.find(a => a.id === selectedId.value))
const markdown = computed(() => props.artifacts.find(a => a.kind === 'analysis' && a.metadata.raw_artifact_id === selectedId.value))

function label(a: TaskArtifact): string {
  const type = a.kind === 'transcript_revision' ? '人工校对版' : a.kind === 'transcript_draft' ? '转写校对草稿（可能包含 OCR）' : a.kind === 'transcript' ? '语音转写' : '画面文字'
  return `${type} · v${a.version} · ${new Date(a.created_at).toLocaleString()}`
}
async function openSettings(): Promise<void> {
  error.value = ''; message.value = ''; confirmed.value = false; includeOcr.value = false
  transcriptId.value = transcripts.value.find(a => a.kind === 'transcript')?.id ?? transcripts.value[0]?.id ?? ''
  ocrId.value = ocr.value[0]?.id ?? ''
  try { config.value = await api.getConfig(); dialog.value?.showModal() }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '无法读取模型设置。' }
}
async function submit(): Promise<void> {
  if (!confirmed.value || !transcriptId.value || !provider.value || !model.value || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    await api.generateStructure(props.taskId, transcriptId.value, includeOcr.value ? ocrId.value : null, provider.value, model.value)
    dialog.value?.close(); message.value = '已创建按需分析步骤，请在任务进度中查看状态。'; emit('updated')
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '无法创建结构分析。' }
  finally { busy.value = false }
}
async function showResult(id: string): Promise<void> {
  error.value = ''; selectedEvidence.value = null; evidenceSource.value = ''
  try {
    const result = await api.getTextArtifact(id)
    const parsed: unknown = JSON.parse(result.content)
    if (!parsed || typeof parsed !== 'object' || !('sections' in parsed) || !Array.isArray(parsed.sections)) throw new Error('invalid')
    sections.value = parsed.sections; selectedId.value = id
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '分析文件无法读取或格式无效。' }
}
async function showEvidence(index: number): Promise<void> {
  if (selectedEvidence.value === index) { selectedEvidence.value = null; evidenceSource.value = ''; return }
  selectedEvidence.value = index; evidenceSource.value = ''; error.value = ''
  const resultId = selectedId.value
  try { const source = await api.getTextArtifact(sections.value[index].source_artifact_id); if (selectedId.value === resultId && selectedEvidence.value === index) evidenceSource.value = source.content }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '无法读取来源文本。' }
}
async function openFile(reveal = false): Promise<void> {
  if (!markdown.value) return
  try { await api.openArtifact(markdown.value.id, reveal) }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '无法打开分析文档。' }
}
watch(() => props.taskId, () => { selectedId.value = ''; sections.value = []; error.value = ''; message.value = '' })
</script>

<template>
  <section class="structure-panel" aria-labelledby="structure-title">
    <div class="panel-head"><div><h2 id="structure-title">内容结构分析</h2><p>按需分析开头、观点、论证与结尾，帮助学习表达方式。</p></div><button class="button button-secondary button-small" type="button" :disabled="!canStart" @click="openSettings">分析内容结构</button></div>
    <p v-if="!transcripts.length" class="helper">暂无可用转写；先完成语音转写。历史分析仍可查看。</p>
    <p v-else-if="!canStart" class="helper">任务正在执行或已取消，暂不能追加分析。</p>
    <p v-if="message" class="helper" role="status">{{ message }}</p>
    <p v-if="error && !dialog?.open" class="info-box form-error" role="alert">{{ error }}</p>
    <details v-if="history.length"><summary>历史分析记录 · {{ history.length }} 项</summary><div v-for="item in history" :key="item.id" class="structure-history"><span>{{ new Date(item.created_at).toLocaleString() }} · {{ item.metadata.model_id || '模型未记录' }}</span><button class="text-button" type="button" @click="showResult(item.id)">查看</button></div></details>
    <p v-else class="helper">暂无分析记录。</p>
    <div v-if="selectedResult" class="structure-result">
      <div class="panel-head"><div><h3>结构分析结果</h3><p>来源无法核对的模型引用需人工确认，不视为原文。</p></div><div class="button-row"><button class="text-button" type="button" :disabled="!markdown" @click="openFile()">打开文档</button><button class="text-button" type="button" :disabled="!markdown" @click="openFile(true)">打开目录</button></div></div>
      <div v-for="(section, index) in sections" :key="index" class="structure-row"><strong>{{ section.kind }}</strong><span>{{ section.present ? section.analysis : '未识别到' }}</span><span>{{ !section.present ? '' : section.verified === true ? '已匹配原文' : '待核对' }} <button v-if="section.present" class="text-button" type="button" @click="showEvidence(index)">{{ selectedEvidence === index ? '收起' : '查看来源' }}</button></span><div v-if="selectedEvidence === index" class="structure-evidence">来源产物 ID：{{ section.source_artifact_id }}<br>{{ section.verified === true ? '已匹配的片段' : '模型引用（未确认是原文）' }}：{{ section.source_excerpt || '未提供' }}<p>来源文本</p><pre>{{ evidenceSource || '暂无可显示的来源文本' }}</pre></div></div>
    </div>
    <dialog ref="dialog" class="structure-dialog" @cancel="error = ''">
      <div class="panel-head"><div><h2>分析内容结构</h2><p>选择本次发送的文本；不发送图片。</p></div><button class="text-button" type="button" aria-label="关闭" :disabled="busy" @click="dialog?.close()">✕</button></div>
      <label class="form-label" for="structure-transcript">转写来源</label><select id="structure-transcript" v-model="transcriptId" class="select-control"><option v-for="item in transcripts" :key="item.id" :value="item.id">{{ label(item) }}</option></select>
      <label class="consent-check"><input v-model="includeOcr" type="checkbox" :disabled="!ocr.length" /><span>附带画面 OCR 文本</span></label>
      <select v-if="includeOcr" v-model="ocrId" class="select-control" aria-label="选择 OCR 文本"><option v-for="item in ocr" :key="item.id" :value="item.id">{{ label(item) }}</option></select>
      <p class="helper">将完整发送所选文件的文本。校对草稿可能已经包含 OCR 内容；请根据实际内容选择。</p>
      <p class="helper">服务商：{{ provider || '未配置' }} · 模型：{{ model || '未配置' }} · 费用取决于服务商</p>
      <RouterLink :to="{ name: 'settings', query: { tab: 'models' } }" class="text-button">模型与服务设置 →</RouterLink>
      <label class="consent-check"><input v-model="confirmed" type="checkbox" /><span>我确认将所选文本发送至上述模型服务商。</span></label>
      <p v-if="error" class="info-box form-error" role="alert">{{ error }}</p>
      <div class="button-row"><button class="button" type="button" :disabled="busy || !confirmed || !provider || !model || !transcriptId || (includeOcr && !ocrId)" @click="submit">{{ busy ? '正在创建…' : '开始分析' }}</button><button class="button button-secondary" type="button" :disabled="busy" @click="dialog?.close()">取消</button></div>
    </dialog>
  </section>
</template>

<style scoped>
.structure-panel { border-top: 1px solid var(--line); margin-top: 1rem; padding-top: 1rem; }
.structure-history, .structure-row { display: grid; grid-template-columns: 1fr auto; gap: .5rem; padding: .6rem 0; border-bottom: 1px solid var(--line); }
.structure-result { margin-top: 1rem; }.structure-row { grid-template-columns: 6rem 1fr auto; }.structure-evidence { grid-column: 1 / -1; white-space: pre-wrap; overflow-wrap: anywhere; }.structure-evidence pre { max-height: 14rem; overflow: auto; white-space: pre-wrap; }
.structure-dialog { width: min(620px, calc(100vw - 2rem)); border: 0; border-radius: 10px; padding: 1.4rem; }.structure-dialog::backdrop { background: #1c293a88; }
@media (max-width: 650px) { .structure-row { grid-template-columns: 1fr; } }
</style>
