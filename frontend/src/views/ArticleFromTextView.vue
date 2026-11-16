<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, ref, watch } from 'vue'
import { isNavigationFailure, onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/message-box/style/css'
import { ApiError, api } from '../api/client'
import type { ModelProfile } from '../api/types'
import MarkdownRichTextEditor from '../components/MarkdownRichTextEditor.vue'
import { closeArticleSourceTabAfterNavigation, getArticleSourceLeaveDecision } from '../articleSourceNavigation'
import { useWorkspaceTabs } from '../stores/workspaceTabs'

const route = useRoute()
const router = useRouter()
const { closeTab } = useWorkspaceTabs()
const sourceMarkdown = ref('')
const defaultModel = ref<ModelProfile | null>(null)
const modelLoading = ref(true)
const modelError = ref('')
const generationError = ref('')
const submitting = ref(false)
const idempotencyKey = ref(crypto.randomUUID())
const generatedArticleId = ref('')
const characterCount = computed(() => sourceMarkdown.value.replace(/\s/g, '').length)
const canGenerate = computed(() => characterCount.value > 0 && characterCount.value <= 1_000_000
  && !modelLoading.value && !modelError.value && !submitting.value && !!defaultModel.value)

let active = false
let modelRequestSequence = 0
let generationSucceeded = false

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '文章生成请求失败，请稍后重试。'
}

async function loadDefaultModel(): Promise<void> {
  const sequence = ++modelRequestSequence
  modelLoading.value = true
  modelError.value = ''
  defaultModel.value = null
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
  generationSucceeded = false
  generatedArticleId.value = ''
  idempotencyKey.value = crypto.randomUUID()
  generationError.value = ''
})

async function guardUnsavedSource(): Promise<boolean> {
  const decision = getArticleSourceLeaveDecision({
    hasContent: Boolean(sourceMarkdown.value.trim()),
    submitting: submitting.value,
    generationSucceeded,
  })
  if (decision === 'allow') return true
  if (decision === 'block') return false
  try {
    await ElMessageBox.confirm('离开后会丢失当前输入的口播稿原文。', '放弃当前文案？', {
      confirmButtonText: '放弃并离开',
      cancelButtonText: '继续编辑',
      distinguishCancelAndClose: true,
      closeOnClickModal: false,
      closeOnPressEscape: false,
      type: 'warning',
      customClass: 'article-source-leave-confirm',
    })
    return true
  } catch {
    return false
  }
}

onBeforeRouteLeave(guardUnsavedSource)
onBeforeRouteUpdate(guardUnsavedSource)

function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (!sourceMarkdown.value.trim() && !submitting.value) return
  event.preventDefault()
  event.returnValue = ''
}

onActivated(() => {
  active = true
  window.addEventListener('beforeunload', warnBeforeUnload)
  void loadDefaultModel()
})

onDeactivated(() => {
  active = false
  modelRequestSequence += 1
  window.removeEventListener('beforeunload', warnBeforeUnload)
})

async function generateArticle(): Promise<void> {
  if (!canGenerate.value) return
  const sourceTabKey = route.fullPath
  submitting.value = true
  generationError.value = ''
  generatedArticleId.value = ''
  generationSucceeded = false
  try {
    const article = await api.createArticleFromText({
      content: sourceMarkdown.value.trim(),
      idempotency_key: idempotencyKey.value,
      authorized: true,
    })
    generatedArticleId.value = article.id
    generationSucceeded = true
    const navigated = await closeArticleSourceTabAfterNavigation(
      router.replace({ name: 'article-edit', params: { articleId: article.id }, query: { source: 'generated-from-text' } }),
      isNavigationFailure,
      nextTick,
      () => closeTab(sourceTabKey),
    )
    if (!navigated) {
      generationError.value = '博文已生成，但暂时无法自动打开编辑页，可使用下方链接继续编辑。'
      return
    }
  } catch (cause) {
    generationError.value = generatedArticleId.value
      ? '博文已生成，但页面未能自动打开，可使用下方链接继续编辑。'
      : errorMessage(cause)
  } finally {
    submitting.value = false
  }
}

function preventLeaveWhileGenerating(event: MouseEvent): void {
  if (submitting.value) event.preventDefault()
}
</script>

<template>
  <section class="article-from-text-page" aria-labelledby="article-from-text-title">
    <header class="page-heading article-source-heading">
      <div>
        <h1 id="article-from-text-title">文案生成文章</h1>
        <p class="lead">粘贴口播稿原文，生成后可在文章编辑器里继续修改。</p>
      </div>
      <RouterLink class="button button-secondary" :to="{ name: 'article-new' }" :aria-disabled="submitting" @click="preventLeaveWhileGenerating">返回新建文章</RouterLink>
    </header>
    <div class="panel article-source-panel" :aria-busy="submitting">
      <div class="article-source-editor-head">
        <label id="article-source-editor-label" class="form-label" for="article-source-editor">口播稿原文</label>
        <span class="article-source-count" :class="{ 'is-over-limit': characterCount > 1_000_000 }" aria-live="polite">{{ characterCount.toLocaleString('zh-CN') }} 字</span>
      </div>
      <MarkdownRichTextEditor
        id="article-source-editor"
        v-model="sourceMarkdown"
        labelled-by="article-source-editor-label"
        placeholder="在这里粘贴口播稿原文…"
        :disabled="submitting"
        class="article-source-rich-editor"
      />
      <p v-if="characterCount > 1_000_000" class="article-source-field-error" role="alert">文案过长，请分段精简后生成。</p>
      <ElAlert v-if="generationError" class="article-source-alert" type="error" :closable="false" show-icon :title="generationError" role="alert" />
      <RouterLink v-if="generatedArticleId" class="article-source-open-generated" :to="{ name: 'article-edit', params: { articleId: generatedArticleId }, query: { source: 'generated-from-text' } }">打开已生成的文章</RouterLink>
      <ElAlert v-if="modelError" class="article-source-alert" type="warning" :closable="false" show-icon :title="modelError" />
      <div class="article-source-footer">
        <div class="article-source-model" aria-live="polite">
          <p class="article-source-model-name">{{ modelLoading ? '正在读取文本模型…' : `文本模型 · ${defaultModel?.name || '未配置'}` }}</p>
          <p class="article-source-model-help">{{ modelError ? '到「知识库与设置」完成配置后即可生成。' : '点击生成即将原文发送给该模型，生成的博文会保存为草稿。' }}</p>
        </div>
        <button class="button article-source-generate" type="button" :disabled="!canGenerate" @click="generateArticle">
          <span v-if="submitting" class="article-source-spinner" aria-hidden="true"></span>
          <svg v-else class="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 2.5c.7 5.4 3.1 7.8 8.5 8.5-5.4.7-7.8 3.1-8.5 8.5-.7-5.4-3.1-7.8-8.5-8.5 5.4-.7 7.8-3.1 8.5-8.5ZM19 16v6m-3-3h6"/></svg>
          {{ submitting ? '正在生成…' : '一键生成博文' }}
        </button>
      </div>
    </div>
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
</style>
