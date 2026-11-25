<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/message-box/style/css'
import { ApiError, api } from '../api/client'
import type { ArticleGenerationJob, ModelProfile } from '../api/types'
import MarkdownRichTextEditor from '../components/MarkdownRichTextEditor.vue'

const route = useRoute()
const router = useRouter()
const sourceMarkdown = ref('')
const defaultModel = ref<ModelProfile | null>(null)
const modelLoading = ref(true)
const modelError = ref('')
const generationError = ref('')
const submitting = ref(false)
const cancelling = ref(false)
const submitted = ref(false)
const idempotencyKey = ref(crypto.randomUUID())
const job = ref<ArticleGenerationJob | null>(null)
const jobLoading = ref(false)
const now = ref(Date.now())
const jobId = computed(() => typeof route.query.job === 'string' ? route.query.job : '')
const characterCount = computed(() => sourceMarkdown.value.replace(/\s/g, '').length)
const canGenerate = computed(() => characterCount.value > 0 && characterCount.value <= 1_000_000 && !modelLoading.value && !modelError.value && !submitting.value && !!defaultModel.value)
const elapsed = computed(() => {
  if (!job.value) return '00:00'
  const seconds = Math.max(0, Math.floor(((job.value.finished_at ? Date.parse(job.value.finished_at) : now.value) - Date.parse(job.value.created_at)) / 1000))
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
})
let active = false
let modelRequestSequence = 0
let pollTimer: ReturnType<typeof setInterval> | undefined

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '文章生成请求失败，请稍后重试。'
}

async function loadDefaultModel(): Promise<void> {
  const sequence = ++modelRequestSequence
  modelLoading.value = true
  modelError.value = ''
  try {
    const result = await api.getModelProfiles()
    if (sequence !== modelRequestSequence || !active) return
    defaultModel.value = result.items.find((profile) => profile.is_default && profile.capabilities.includes('text_generation')) ?? null
    if (!defaultModel.value || !defaultModel.value.credential.available || !defaultModel.value.credential.configured) {
      defaultModel.value = null
      modelError.value = '请先在知识库与设置中配置默认文本模型及 API Key。'
    }
  } catch (cause) {
    if (sequence === modelRequestSequence) modelError.value = errorMessage(cause)
  } finally {
    if (sequence === modelRequestSequence) modelLoading.value = false
  }
}

watch(sourceMarkdown, () => {
  if (submitting.value) return
  idempotencyKey.value = crypto.randomUUID()
  generationError.value = ''
})

async function guardUnsavedSource(): Promise<boolean> {
  if (jobId.value || submitted.value || submitting.value || !sourceMarkdown.value.trim()) return true
  try {
    await ElMessageBox.confirm('离开后会丢失当前输入的口播稿原文。', '放弃当前文案？', {
      confirmButtonText: '放弃并离开', cancelButtonText: '继续编辑', distinguishCancelAndClose: true,
      closeOnClickModal: false, closeOnPressEscape: false, type: 'warning', customClass: 'article-source-leave-confirm',
    })
    return true
  } catch { return false }
}
onBeforeRouteLeave(guardUnsavedSource)
onBeforeRouteUpdate(guardUnsavedSource)

function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (jobId.value || submitted.value || !sourceMarkdown.value.trim()) return
  event.preventDefault()
  event.returnValue = ''
}

async function refreshJob(): Promise<void> {
  const id = jobId.value
  if (!id) return
  try {
    const result = await api.articleGenerationJob(id)
    if (active && jobId.value === id) { job.value = result; generationError.value = '' }
  } catch (cause) {
    if (active && jobId.value === id) generationError.value = errorMessage(cause)
  } finally { jobLoading.value = false }
}

onActivated(() => {
  active = true
  window.addEventListener('beforeunload', warnBeforeUnload)
  if (jobId.value) { jobLoading.value = true; void refreshJob() }
  else {
    submitted.value = false
    const saved = sessionStorage.getItem('article-generation-retry-source')
    if (saved) { sourceMarkdown.value = saved; sessionStorage.removeItem('article-generation-retry-source') }
    void loadDefaultModel()
  }
  pollTimer = setInterval(() => { now.value = Date.now(); if (jobId.value && (!job.value || job.value.status === 'running')) void refreshJob() }, 2000)
})
onDeactivated(() => {
  active = false
  modelRequestSequence += 1
  if (pollTimer) clearInterval(pollTimer)
  window.removeEventListener('beforeunload', warnBeforeUnload)
})

async function generateArticle(): Promise<void> {
  if (!canGenerate.value) return
  submitting.value = true
  generationError.value = ''
  try {
    const result = await api.startArticleGenerationJob({ content: sourceMarkdown.value.trim(), idempotency_key: idempotencyKey.value, authorized: true })
    submitted.value = true
    window.dispatchEvent(new Event('article-generation-updated'))
    if (active && route.name === 'article-from-text' && !jobId.value) {
      await router.replace({ name: 'article-from-text', query: { job: result.id } })
    }
  } catch (cause) { generationError.value = errorMessage(cause) }
  finally { submitting.value = false }
}

async function cancelGeneration(): Promise<void> {
  if (!job.value || cancelling.value) return
  try {
    await ElMessageBox.confirm('确定取消生成吗？原文会保留。已发出的请求可能仍由模型服务商处理并产生费用。', '取消生成？', {
      confirmButtonText: '确定取消', cancelButtonText: '继续生成', type: 'warning', distinguishCancelAndClose: true,
    })
  } catch { return }
  cancelling.value = true
  try {
    job.value = await api.cancelArticleGenerationJob(job.value.id)
    window.dispatchEvent(new Event('article-generation-updated'))
  } catch (cause) { generationError.value = errorMessage(cause) }
  finally { cancelling.value = false }
}

async function returnToSource(): Promise<void> {
  if (job.value) sessionStorage.setItem('article-generation-retry-source', job.value.content)
  await router.push({ name: 'article-from-text' })
}
</script>

<template>
  <section class="article-from-text-page" aria-labelledby="article-from-text-title">
    <template v-if="jobId">
      <header class="page-heading"><div><h1 id="article-from-text-title">文章生成任务</h1><p class="lead">可切换到其他功能，生成完成后随时回来查看。</p></div></header>
      <div class="panel article-job-panel" aria-live="polite">
        <template v-if="jobLoading && !job"><p>正在读取任务…</p></template>
        <template v-else-if="job">
          <span v-if="job.status === 'running'" class="article-job-spinner" aria-hidden="true"></span>
          <h2>{{ job.status === 'running' ? '文章正在生成' : job.status === 'completed' ? '文章生成完成' : job.status === 'cancelled' ? '已取消生成' : '文章生成失败' }}</h2>
          <p class="article-job-description" v-if="job.status === 'running'">正在根据口播稿整理文章，请耐心等待。<br>离开此页面后，任务会继续在后台运行。</p>
          <p class="article-job-description" v-else-if="job.status === 'completed'">文章已保存为草稿，可继续编辑。</p>
          <p class="article-job-description" v-else-if="job.status === 'cancelled'">原文已保留，可以修改后重新生成。</p>
          <p class="article-job-description" v-else>{{ job.error_message || '生成失败，请稍后重试。' }}</p>
          <div class="article-job-elapsed">{{ job.status === 'running' ? '已等待' : '用时' }} {{ elapsed }}</div>
          <div class="article-job-actions">
            <button v-if="job.status === 'running'" class="button button-secondary" type="button" :disabled="cancelling" @click="cancelGeneration">{{ cancelling ? '正在取消…' : '取消生成' }}</button>
            <RouterLink v-if="job.status === 'completed' && job.article_id" class="button" :to="{ name: 'article-edit', params: { articleId: job.article_id }, query: { source: 'generated-from-text' } }">打开文章</RouterLink>
            <button v-if="job.status === 'failed' || job.status === 'cancelled'" class="button button-secondary" type="button" @click="returnToSource">返回原文</button>
          </div>
          <p class="article-job-hint">生成完成后，文章会保存为草稿。<br>从顶部“文章生成任务”入口可回到此页面。</p>
        </template>
        <ElAlert v-if="generationError" class="article-source-alert" type="error" :closable="false" show-icon :title="generationError" />
      </div>
    </template>
    <template v-else>
      <header class="page-heading article-source-heading"><div><h1 id="article-from-text-title">文案生成文章</h1><p class="lead">粘贴口播稿原文，生成后可在文章编辑器里继续修改。</p></div><RouterLink class="button button-secondary" :to="{ name: 'article-new' }">返回新建文章</RouterLink></header>
      <div class="panel article-source-panel" :aria-busy="submitting">
        <div class="article-source-editor-head"><label id="article-source-editor-label" class="form-label" for="article-source-editor">口播稿原文</label><span class="article-source-count" :class="{ 'is-over-limit': characterCount > 1_000_000 }">{{ characterCount.toLocaleString('zh-CN') }} 字</span></div>
        <MarkdownRichTextEditor id="article-source-editor" v-model="sourceMarkdown" labelled-by="article-source-editor-label" placeholder="在这里粘贴口播稿原文…" :disabled="submitting" class="article-source-rich-editor" />
        <p v-if="characterCount > 1_000_000" class="article-source-field-error" role="alert">文案过长，请分段精简后生成。</p>
        <ElAlert v-if="generationError" class="article-source-alert" type="error" :closable="false" show-icon :title="generationError" />
        <ElAlert v-if="modelError" class="article-source-alert" type="warning" :closable="false" show-icon :title="modelError" />
        <div class="article-source-footer"><div class="article-source-model"><p class="article-source-model-name">{{ modelLoading ? '正在读取文本模型…' : `文本模型 · ${defaultModel?.name || '未配置'}` }}</p><p class="article-source-model-help">{{ modelError ? '到「知识库与设置」完成配置后即可生成。' : '点击生成即将原文发送给该模型，生成的博文会保存为草稿。' }}</p></div><button class="button article-source-generate" type="button" :disabled="!canGenerate" @click="generateArticle">{{ submitting ? '正在提交…' : '一键生成博文' }}</button></div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.article-from-text-page { display: grid; gap: 20px; }
.page-heading { margin-bottom: 0; }
.article-source-heading { align-items: center; }
.article-source-heading .button { min-height: 40px; }
.article-source-heading .button[aria-disabled="true"] { pointer-events: none; opacity: .65; }
.article-source-panel { min-width: 0; padding: 20px 22px; }
.article-source-editor-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 14px; margin-bottom: 8px; }
.article-source-editor-head .form-label { margin: 0; font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.article-source-count { color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.article-source-count.is-over-limit, .article-source-field-error { color: var(--danger); }
.article-source-rich-editor { min-height: min(54vh, 560px); }
.article-source-rich-editor :deep(.rich-text-editor), .article-source-rich-editor :deep(.rich-text-editor__mount), .article-source-rich-editor :deep(.milkdown .ProseMirror), .article-source-rich-editor :deep(.rich-text-editor__loading) { min-height: min(54vh, 560px); }
.article-source-field-error { margin: 8px 0 0; font-size: var(--fs-body-sm); }
.article-source-alert { margin-top: 12px; }
.article-source-alert :deep(.el-alert__title) { overflow-wrap: anywhere; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.article-source-open-generated { display: inline-flex; margin-top: 10px; color: var(--accent); font-size: var(--fs-body-sm); text-decoration: underline; text-underline-offset: 3px; }
.article-source-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px 20px; margin-top: 18px; }
.article-source-model { min-width: 0; }
.article-source-model-name { margin: 0; font-size: var(--fs-body); font-weight: var(--fw-semibold); overflow-wrap: anywhere; }
.article-source-model-help { margin: 4px 0 0; color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); overflow-wrap: anywhere; }
.article-source-generate { min-width: 146px; }
.article-source-generate:disabled { cursor: default; }
.article-source-spinner { width: 16px; height: 16px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: article-source-spin .8s linear infinite; }
@keyframes article-source-spin { to { transform: rotate(360deg); } }
@media (max-width: 560px) {
  .article-source-panel { padding: 16px; }
  .article-source-heading .button, .article-source-generate { width: 100%; min-height: 44px; }
  .article-source-rich-editor :deep(.rich-text-editor), .article-source-rich-editor :deep(.rich-text-editor__mount), .article-source-rich-editor :deep(.milkdown .ProseMirror), .article-source-rich-editor :deep(.rich-text-editor__loading) { min-height: min(45vh, 420px); }
}
@media (prefers-reduced-motion: reduce) { .article-source-spinner { animation: none; } }
.article-job-panel { text-align: center; padding: 64px 24px 36px; }
.article-job-panel h2 { margin: 20px 0 12px; font-size: var(--fs-title-md); }
.article-job-spinner { display: inline-block; width: 56px; height: 56px; border: 5px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: article-source-spin .8s linear infinite; }
.article-job-description, .article-job-hint { color: var(--text-secondary); line-height: var(--lh-base); }
.article-job-elapsed { display: inline-block; margin: 14px 0 22px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.article-job-actions { display: flex; justify-content: center; gap: 12px; }
.article-job-hint { border-top: 1px solid var(--border); margin-top: 30px; padding-top: 22px; }
@media (prefers-reduced-motion: reduce) { .article-job-spinner { animation: none; } }
</style>
