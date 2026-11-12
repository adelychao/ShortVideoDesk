<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ElAlert, ElButton, ElCheckbox, ElCheckboxGroup, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/tag/style/css'
import { ApiError, api } from '../api/client'
import type { ScriptStyleTemplate, TaskArtifact } from '../api/types'
import MarkdownPreview from './MarkdownPreview.vue'
import MarkdownRichTextEditor from './MarkdownRichTextEditor.vue'

const props = defineProps<{
  taskId: string
  artifacts: TaskArtifact[]
  selectedCommentIds?: string[]
  canRegenerate: boolean
  regenerationUnavailableReason: string
  regenerationTarget: HTMLElement | null
}>()
const emit = defineEmits<{ (event: 'updated'): void }>()
const selectedId = ref('')
const content = ref('')
const expectedHash = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
const editing = ref(false)
const editSnapshot = ref('')
const scriptStyleTemplates = ref<ScriptStyleTemplate[]>([])
const scriptStyleTemplatesLoading = ref(false)
const scriptStyleTemplatesLoaded = ref(false)
const scriptStyleTemplatesError = ref('')
const selectedScriptStyleIds = ref<string[]>([])
const styleSelectionTouched = ref(false)
const maxSelectedScriptStyles = 20
let loadSequence = 0
let styleLoadSequence = 0

const scripts = computed(() => props.artifacts
  .filter((artifact) => ['script', 'script_revision', 'final_script'].includes(artifact.kind))
  .sort((left, right) => right.created_at.localeCompare(left.created_at)))
const transcriptSources = computed(() => props.artifacts
  .filter((artifact) => ['transcript_revision', 'transcript_draft', 'transcript'].includes(artifact.kind))
  .sort((left, right) => {
    const leftReviewed = left.metadata.review_copy === true && left.metadata.human_edited === true
    const rightReviewed = right.metadata.review_copy === true && right.metadata.human_edited === true
    return Number(rightReviewed) - Number(leftReviewed) || right.created_at.localeCompare(left.created_at)
  }))
const selectedArtifact = computed(() => scripts.value.find((item) => item.id === selectedId.value) ?? null)
const canFinalize = computed(() => Boolean(selectedArtifact.value && selectedArtifact.value.kind !== 'final_script'))
const enabledScriptStyleTemplates = computed(() => scriptStyleTemplates.value.filter((item) => item.enabled))
const selectedScriptStyles = computed(() => enabledScriptStyleTemplates.value.filter((item) => selectedScriptStyleIds.value.includes(item.id)))
const selectedScriptStyleModel = computed({
  get: () => selectedScriptStyleIds.value,
  set: (value: string[]) => {
    selectedScriptStyleIds.value = value
    styleSelectionTouched.value = true
  },
})
const latestGeneratedStyleIds = computed(() => {
  const latest = scripts.value.find((item) => typeof item.metadata.style_id === 'string')
  if (!latest) return []
  return [...new Set(scripts.value
    .filter((item) => item.step_id === latest.step_id)
    .map((item) => item.metadata.style_id)
    .filter((value): value is string => typeof value === 'string'))]
})
const scriptStyleSelectionRequired = computed(() => enabledScriptStyleTemplates.value.length > 0 && selectedScriptStyles.value.length === 0)

function label(artifact: TaskArtifact): string {
  const style = typeof artifact.metadata.style === 'string' ? artifact.metadata.style : '口播稿'
  const status = artifact.kind === 'final_script' ? '定稿' : artifact.metadata.human_edited ? '人工修订' : 'AI 草稿'
  return `${style} · ${status} · v${artifact.version}`
}

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '稿件操作失败，请稍后重试。'
}

function applyLatestStyleSelection(): void {
  if (styleSelectionTouched.value) return
  const enabledIds = new Set(enabledScriptStyleTemplates.value.map((item) => item.id))
  selectedScriptStyleIds.value = latestGeneratedStyleIds.value.filter((id) => enabledIds.has(id))
}

async function loadScriptStyleTemplates(): Promise<void> {
  if (scriptStyleTemplatesLoading.value) return
  const sequence = ++styleLoadSequence
  scriptStyleTemplatesLoading.value = true
  scriptStyleTemplatesError.value = ''
  try {
    const result = await api.getConfig()
    if (sequence !== styleLoadSequence) return
    scriptStyleTemplates.value = result.settings.script_style_templates
    applyLatestStyleSelection()
  } catch (cause) {
    if (sequence === styleLoadSequence) scriptStyleTemplatesError.value = safeError(cause)
  } finally {
    if (sequence === styleLoadSequence) {
      scriptStyleTemplatesLoading.value = false
      scriptStyleTemplatesLoaded.value = true
    }
  }
}

function transcriptSourceDescription(source: TaskArtifact): string {
  if (source.metadata.human_edited === true) return '人工校对后的最终文案'
  if (source.metadata.ocr_is_separate_evidence === true) return '语音转写与 OCR 合并后的识别文案'
  return source.kind === 'transcript_draft' ? '最终语音识别文案' : '语音转写文案'
}

async function load(artifactId = selectedId.value): Promise<void> {
  if (!artifactId) return
  const sequence = ++loadSequence
  busy.value = true
  editing.value = false
  error.value = ''
  message.value = ''
  try {
    const result = await api.getTextArtifact(artifactId)
    if (sequence !== loadSequence) return
    selectedId.value = artifactId
    content.value = result.content
    expectedHash.value = result.sha256
  } catch (cause) {
    if (sequence === loadSequence) error.value = safeError(cause)
  } finally {
    if (sequence === loadSequence) busy.value = false
  }
}

async function openArtifact(id: string): Promise<void> {
  if (!scripts.value.some(item => item.id === id)) return
  if (editing.value && content.value !== editSnapshot.value && !window.confirm('当前稿件有未保存的修改，切换版本会放弃这些修改。继续切换吗？')) return
  if (id !== selectedId.value || editing.value) await load(id)
}
defineExpose({ openArtifact })

async function saveRevision(): Promise<void> {
  if (!selectedArtifact.value || !expectedHash.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const revision = await api.saveTextArtifactRevision(selectedArtifact.value.id, expectedHash.value, content.value)
    emit('updated')
    await load(revision.id)
    editing.value = false
    message.value = revision.metadata.conflict_copy === true
      ? '检测到原文件被外部修改；稿件已单独另存为冲突版本，没有覆盖原文件。'
      : '已保存为新的人工修订版本；原稿保持不变。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

function beginEdit(): void {
  editSnapshot.value = content.value
  editing.value = true
  error.value = ''
  message.value = ''
}

function cancelEdit(): void {
  content.value = editSnapshot.value
  editing.value = false
  message.value = ''
}

async function finalize(): Promise<void> {
  if (!selectedArtifact.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.reviewGeneratedArtifact(selectedArtifact.value.id, expectedHash.value)
    emit('updated')
    await load(result.id)
    message.value = '已另存为定稿。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

async function regenerateFromSelection(): Promise<void> {
  const commentIds = props.selectedCommentIds ?? []
  const source = transcriptSources.value[0]
  const styleIds = selectedScriptStyles.value.map((item) => item.id)
  if (!source || !props.canRegenerate || !styleIds.length || busy.value) return
  const styleNames = selectedScriptStyles.value.map((item) => item.name)
  const referencePaths = [...new Set(selectedScriptStyles.value.flatMap((item) => item.reference_paths))]
  const materials = [
    `最终文案（${transcriptSourceDescription(source)}）`,
    `风格模板：${styleNames.join('、')}`,
    referencePaths.length ? `关联参考 Markdown：${referencePaths.join('、')}` : '所选模板未关联参考 Markdown',
    ...(commentIds.length ? [`单独选择的精选评论：${commentIds.length} 条`] : []),
  ]
  const details = `本次会将以下材料发送给本机当前配置的文本模型：\n${materials.map((item) => `• ${item}`).join('\n')}\n\n模板和参考文档仅定义表达风格；新稿事实以最终转写文案为准。提交时会保存本次提示词和参考 Markdown 快照。模型费用未知。确认授权？`
  if (!window.confirm(details)) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    await api.generateScriptsFromSelection(props.taskId, source.id, commentIds, styleIds)
    emit('updated')
    message.value = `已创建新稿生成步骤，将按 ${styleNames.join('、')} 分别生成新稿；已有稿件不会被覆盖。`
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

watch(scripts, (items) => {
  if (!items.some((item) => item.id === selectedId.value)) {
    const next = items[0]?.id ?? ''
    selectedId.value = next
    if (next) void load(next)
  }
}, { immediate: true })
watch(() => props.taskId, () => {
  content.value = ''
  expectedHash.value = ''
  editing.value = false
  message.value = ''
  error.value = ''
  scriptStyleTemplates.value = []
  scriptStyleTemplatesLoaded.value = false
  scriptStyleTemplatesError.value = ''
  selectedScriptStyleIds.value = []
  styleSelectionTouched.value = false
  if (props.canRegenerate) void loadScriptStyleTemplates()
  const next = scripts.value[0]
  selectedId.value = next?.id ?? ''
  if (next) void load(next.id)
})
watch(() => props.canRegenerate, (available) => {
  if (available && !scriptStyleTemplatesLoaded.value) void loadScriptStyleTemplates()
}, { immediate: true })
watch(scripts, () => applyLatestStyleSelection())
</script>

<template>
  <Teleport v-if="regenerationTarget && transcriptSources.length" :to="regenerationTarget">
    <details class="regeneration-panel">
      <summary class="regeneration-heading">
        <div>
          <h3 id="script-regeneration-title">重新生成稿件</h3>
          <p>选择一种或多种风格，生成新的稿件版本。</p>
        </div>
        <span class="regeneration-chevron" aria-hidden="true"></span>
      </summary>
      <div class="regeneration-content">
        <el-alert v-if="!props.canRegenerate" type="info" :closable="false" show-icon role="status">
          {{ props.regenerationUnavailableReason || '任务完成后可选择风格模板并重新生成。' }}
        </el-alert>
        <template v-else>
          <div class="regeneration-source">
            <span class="regeneration-source-label">本次输入文案</span>
            <strong>{{ transcriptSourceDescription(transcriptSources[0]) }}</strong>
            <span>{{ transcriptSources[0].name }}</span>
          </div>
          <div class="regeneration-style-field">
            <div class="regeneration-style-head">
              <div><strong>选择改写风格 <span class="required-marker" aria-hidden="true">*</span></strong><p>每个选中模板生成一篇新稿。提交时保存本次提示词和参考 Markdown 快照。</p></div>
              <el-tag type="info" effect="plain">已选 {{ selectedScriptStyles.length }} / {{ maxSelectedScriptStyles }} 个</el-tag>
            </div>
            <p v-if="scriptStyleTemplatesLoading" class="helper" role="status">正在读取已启用的风格模板…</p>
            <el-alert v-else-if="scriptStyleTemplatesError" type="error" :closable="false" show-icon role="alert">{{ scriptStyleTemplatesError }}</el-alert>
            <el-alert v-else-if="enabledScriptStyleTemplates.length === 0" type="warning" :closable="false" show-icon role="alert">
              没有已启用的风格模板。请先到 <RouterLink :to="{ name: 'settings', query: { tab: 'script-styles' } }">口播稿风格设置</RouterLink> 添加或启用模板。
            </el-alert>
            <el-checkbox-group v-else v-model="selectedScriptStyleModel" class="regeneration-style-options" aria-label="选择重新生成稿件的风格模板">
              <el-checkbox v-for="template in enabledScriptStyleTemplates" :key="template.id" :label="template.id" :disabled="busy || (!selectedScriptStyleIds.includes(template.id) && selectedScriptStyleIds.length >= maxSelectedScriptStyles)">
                {{ template.name }}
              </el-checkbox>
            </el-checkbox-group>
            <p v-if="selectedScriptStyleIds.length >= maxSelectedScriptStyles" class="helper" role="status">每次最多选择 {{ maxSelectedScriptStyles }} 个风格模板。</p>
            <el-alert v-if="scriptStyleSelectionRequired && !scriptStyleTemplatesLoading && !scriptStyleTemplatesError" class="regeneration-required" type="error" :closable="false" show-icon role="alert">请至少选择一个风格模板。</el-alert>
          </div>
          <div v-if="selectedCommentIds?.length" class="info-box" role="status">另有 {{ selectedCommentIds.length }} 条评论作为单独标注的受众反馈。</div>
          <p class="regeneration-cost-note">模型费用未知。提交前会再次确认本次使用的材料。</p>
          <div class="regeneration-actions">
            <el-button type="primary" :disabled="busy || scriptStyleTemplatesLoading || !!scriptStyleTemplatesError || !selectedScriptStyles.length" @click="regenerateFromSelection">
              {{ busy ? '正在创建…' : `生成新稿${selectedScriptStyles.length ? `（${selectedScriptStyles.length} 种风格）` : ''}` }}
            </el-button>
          </div>
        </template>
      </div>
    </details>
  </Teleport>
  <section v-if="scripts.length || transcriptSources.length" class="panel panel-pad editor-panel" aria-labelledby="script-editor-title">
    <div class="script-editor-heading">
      <div><h2 id="script-editor-title">当前稿件</h2><p>集中阅读和编辑当前稿件；人工修订会另存版本。</p></div>
      <span v-if="selectedArtifact" class="script-status" :class="{ 'is-final': selectedArtifact.kind === 'final_script' }">
        {{ selectedArtifact.kind === 'final_script' ? '人工审核定稿' : selectedArtifact.metadata.human_edited === true ? '人工修订' : 'AI 草稿' }}
      </span>
    </div>
    <div v-if="scripts.length" class="script-editor-toolbar">
      <div class="script-version-field">
        <label class="form-label" for="script-artifact-select">稿件版本</label>
        <select id="script-artifact-select" v-model="selectedId" class="select-control" :disabled="busy || editing" @change="load()">
          <option v-for="artifact in scripts" :key="artifact.id" :value="artifact.id">{{ label(artifact) }}</option>
        </select>
      </div>
      <div v-if="selectedArtifact" class="button-row script-action-row">
        <button v-if="!editing" class="button button-secondary" type="button" :disabled="busy" @click="beginEdit">编辑正文</button>
        <template v-else>
          <button class="button" type="button" :disabled="busy" @click="saveRevision">{{ busy ? '正在保存…' : '保存为新稿件版本' }}</button>
          <button class="button button-secondary" type="button" :disabled="busy" @click="cancelEdit">取消</button>
        </template>
        <button v-if="canFinalize && !editing" class="button" type="button" :disabled="busy || !selectedArtifact" @click="finalize">设为定稿</button>
      </div>
    </div>
    <div v-if="selectedCommentIds?.length" class="info-box" role="status">已在评论面板选择 {{ selectedCommentIds.length }} 条受众反馈，可在后续稿件生成时选用。</div>
    <MarkdownPreview v-if="selectedArtifact && !editing" class="script-preview" aria-label="口播稿预览" :content="content || '这份稿件目前没有正文。'" />
    <MarkdownRichTextEditor
      v-if="selectedArtifact && editing"
      id="script-content-editor"
      v-model="content"
      labelled-by="script-editor-title"
      placeholder="编辑口播稿正文…"
      :disabled="busy"
    />
    <p v-if="error" class="info-box form-error" role="alert">{{ error }}</p>
    <p v-if="message" class="helper" role="status">{{ message }}</p>
  </section>
</template>

<style scoped>
.editor-panel { display: grid; gap: 14px; }
.script-editor-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.script-editor-heading h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.script-editor-heading p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.script-status { display: inline-flex; flex: 0 0 auto; align-items: center; min-height: 24px; padding: 0 9px; border-radius: 4px; color: var(--text-secondary); background: var(--paper-2); font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.script-status.is-final { color: var(--success); background: var(--success-soft); }
.script-editor-toolbar { display: flex; align-items: flex-end; gap: 12px; }
.script-version-field { display: grid; gap: 6px; width: min(460px, 55%); min-width: 0; }
.script-version-field .form-label { margin: 0; }
.script-action-row { flex-wrap: wrap; align-items: center; margin: 0 0 0 auto; }
.script-preview { min-height: 28rem; max-height: 42rem; overflow: auto; padding: 1.5rem 2rem; border: 1px solid var(--line); border-radius: 6px; background: var(--paper-2); color: var(--text-secondary); font-size: var(--fs-body-lg); line-height: var(--lh-loose); white-space: normal; overflow-wrap: anywhere; }
.regeneration-panel { display: block; margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.regeneration-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 54px; padding: 8px 0; border-top: 1px solid var(--line); cursor: pointer; list-style: none; }
.regeneration-heading::-webkit-details-marker { display: none; }
.regeneration-heading h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.regeneration-heading p { margin: 3px 0 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: var(--lh-base); }
.regeneration-chevron { display: grid; flex: 0 0 24px; width: 24px; height: 24px; place-items: center; border-radius: 4px; color: var(--text-tertiary); }
.regeneration-chevron::before { content: '＋'; font-size: 16px; line-height: 1; }
.regeneration-panel[open] .regeneration-chevron::before { content: '−'; }
.regeneration-content { display: grid; gap: 12px; padding: 2px 0 8px; }
.regeneration-source { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; gap: 3px; padding: 9px 11px; border-radius: 6px; background: var(--paper-2); }
.regeneration-source-label { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.regeneration-source strong { color: var(--text-primary); font-size: var(--fs-body-sm); }
.regeneration-source > span:last-child { color: var(--text-secondary); font-size: var(--fs-body-sm); overflow-wrap: anywhere; }
.regeneration-style-field { display: grid; gap: 8px; }
.regeneration-style-head { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.regeneration-style-head strong { color: var(--text-primary); font-size: var(--fs-body); }
.regeneration-style-head p { margin: 3px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.regeneration-style-options { display: flex; flex-wrap: wrap; gap: 4px 10px; }
.regeneration-style-options :deep(.el-checkbox) { min-height: 30px; margin: 0; }
.regeneration-style-options :deep(.el-checkbox__label) { color: var(--text-primary); font-size: var(--fs-body-sm); }
.regeneration-required { margin-top: 0; }
.regeneration-cost-note { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.regeneration-actions { display: flex; justify-content: flex-end; }
.regeneration-actions :deep(.el-button) { height: 36px; margin-left: 0; font-size: var(--fs-body-sm); }
@media (max-width: 600px) {
  .script-editor-toolbar { align-items: stretch; flex-direction: column; }
  .script-version-field { width: 100%; }
  .script-action-row { margin-left: 0; }
  .script-preview { min-height: 20rem; padding: 1rem; }
  .regeneration-actions { justify-content: stretch; }
  .regeneration-actions :deep(.el-button) { width: 100%; }
}
</style>
