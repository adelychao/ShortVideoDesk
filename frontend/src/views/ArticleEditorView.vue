<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onActivated, onBeforeUnmount, onDeactivated, ref, toRaw, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { isNavigationFailure, NavigationFailureType, onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from 'vue-router'
import type { RouteLocationNormalized } from 'vue-router'
import { ElAlert, ElButton, ElCard, ElConfigProvider, ElDatePicker, ElDialog, ElInput, ElMessageBox } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/card/style/css'
import 'element-plus/es/components/date-picker/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/message-box/style/css'
import { ApiError, api } from '../api/client'
import type { ArticleDocument, ArticleImageSettings, ArticleRecord } from '../api/types'
import ArticleImageModeDialog from '../components/ArticleImageModeDialog.vue'
import MarkdownPreview from '../components/MarkdownPreview.vue'
import { approveDraftSave, beginDraftLeave, isCurrentDraftLeave, redirectDraftLeave } from '../router'

const MarkdownRichTextEditor = defineAsyncComponent(() => import('../components/MarkdownRichTextEditor.vue'))

const route = useRoute()
const router = useRouter()
const article = ref<ArticleRecord | null>(null)
const articleId = computed(() => article.value?.id ?? String(route.params.articleId || ''))
const document = ref<ArticleDocument | null>(null)
const baseSnapshot = ref('')
const loading = ref(true)
const saving = ref(false)
const uploadingCover = ref(false)
type ArticleImageTarget = 'cover' | 'paragraph'
type ArticleImageMode = 'provider' | 'vector'
type ArticleImageJob = { articleId: string; target: ArticleImageTarget; blockId: string | null; mode: ArticleImageMode }
const imageSettings = ref<ArticleImageSettings | null>(null)
const imageModeDialogOpen = ref(false)
const imageRequest = ref<{ target: ArticleImageTarget; blockId: string | null }>({ target: 'cover', blockId: null })
const imageGenerationQueue = ref<ArticleImageJob[]>([])
const generatingImageTarget = ref<string | null>(null)
const generatingImageMode = ref<ArticleImageMode | null>(null)
const error = ref('')
const errorDialogMessage = ref('')
const imageGenerationError = ref(false)
const message = ref('')
const previewDialogOpen = ref(false)
const imagePreviewDialogOpen = ref(false)
const imagePreviewUrl = ref('')
const imagePreviewTitle = ref('')
const errorDialogOpen = ref(false)
const leaveDialogOpen = ref(false)
const editorActive = ref(false)
const coverInput = ref<HTMLInputElement | null>(null)
const bodyEditor = ref<{
  insertImageAtAnchor: (input: { src: string; alt: string; slotId: string; anchorText: string }) => 'replaced' | 'inserted-after-paragraph' | 'inserted-at-cursor' | 'unavailable'
  replaceManagedImageSource: (slotId: string, src: string) => boolean
} | null>(null)
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true })
markdown.disable('image')

type LeaveChoice = 'save' | 'discard' | 'stay'
let resolveLeaveChoice: ((choice: LeaveChoice) => void) | null = null
let latestLeaveTarget: RouteLocationNormalized | null = null
let saveLeaveTarget: RouteLocationNormalized | null = null
let discardLeaveTarget: RouteLocationNormalized | null = null
let imageGenerationWorkerRunning = false

watch(error, (value) => {
  if (!value) return
  finishLeaveChoice('stay')
  void nextTick(() => { errorDialogOpen.value = true })
})

const isNewArticleSession = computed(() => route.name === 'article-new' || route.query.source === 'new-article')
const isDirty = computed(() => Boolean(document.value && JSON.stringify(document.value) !== baseSnapshot.value))
const shouldProtectDraft = computed(() => editorActive.value && isNewArticleSession.value && isDirty.value)
const visibleTitle = computed({
  get: () => document.value?.title || '',
  set: (value: string) => {
    if (!document.value) return
    document.value.title = value
  },
})
const visibleSummary = computed({
  get: () => document.value?.summary || '',
  set: (value: string) => {
    if (!document.value) return
    document.value.summary = value
  },
})
const visibleBody = computed({
  get: () => document.value?.body_markdown || '',
  set: (value: string) => {
    if (!document.value) return
    document.value.body_markdown = value
  },
})
const coverUrl = computed(() => article.value?.document.cover_image_path ? `${api.articleCoverUrl(articleId.value)}?v=${article.value.version}` : '')
const paragraphImagePrompts = computed(() => (document.value?.blocks ?? []).filter((block) => block.type === 'paragraph' && (
  block.image_role === 'module' || (typeof block.image_prompt === 'string' && block.image_prompt.trim()) || block.image_path
)).map((block) => ({
  id: String(block.id || ''),
  text: String(block.text || ''),
  image_prompt: String(block.image_prompt || ''),
  image_path: typeof block.image_path === 'string' ? block.image_path : '',
  image_name: typeof block.image_name === 'string' ? block.image_name : '',
  image_model_id: typeof block.image_model_id === 'string' ? block.image_model_id : '',
})))
const selectedParagraphImage = computed(() => paragraphImagePrompts.value.find((item) => item.id === imageRequest.value.blockId))
const selectedImagePrompt = computed(() => imageRequest.value.target === 'cover'
  ? document.value?.cover_image_prompt ?? ''
  : selectedParagraphImage.value?.image_prompt ?? '')
const selectedImageExists = computed(() => imageRequest.value.target === 'cover'
  ? !!coverUrl.value
  : !!selectedParagraphImage.value?.image_path)

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '文章请求失败，请稍后重试。'
}

function reportError(value: string, fromImageGeneration = false): void {
  const displayMessage = value.trim() || (fromImageGeneration
    ? '配图生成失败，服务未返回具体原因。请检查模型设置后重试。'
    : '操作失败，服务未返回具体原因。请稍后重试。')
  imageGenerationError.value = fromImageGeneration
  errorDialogMessage.value = displayMessage
  error.value = displayMessage
}

function closeErrorDialog(): void {
  errorDialogOpen.value = false
  error.value = ''
  errorDialogMessage.value = ''
  imageGenerationError.value = false
}

async function confirmAction(message: string, title: string, confirmButtonText: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText,
      cancelButtonText: '取消',
      type: 'warning',
      customClass: 'article-editor-confirm',
      customStyle: { width: 'min(480px, calc(100vw - 32px))' },
      closeOnClickModal: false,
    })
    return true
  } catch {
    return false
  }
}

function createDefaultDocument(): ArticleDocument {
  const today = new Date()
  const month = String(today.getMonth() + 1).padStart(2, '0')
  const day = String(today.getDate()).padStart(2, '0')
  return {
    schema_version: 1,
    title: '',
    title_candidates: [],
    summary: '',
    mode: 'layout',
    analysis: { core_point: '', reason: '手动新建文章' },
    cover_image_prompt: '',
    cover_image_path: '',
    cover_image_name: '',
    cover_image_model_id: '',
    cover_image_generated_at: '',
    author: '老赵',
    published_at: `${today.getFullYear()}-${month}-${day}`,
    outro: '',
    blocks: [],
    body_markdown: '',
    platform_overrides: {},
  }
}

function initializeNewArticleDraft(): void {
  article.value = null
  document.value = createDefaultDocument()
  baseSnapshot.value = JSON.stringify(document.value)
  imageSettings.value = null
  error.value = ''
  message.value = ''
  loading.value = false
}

function acceptArticle(value: ArticleRecord): void {
  article.value = value
  document.value = structuredClone(toRaw(value.document))
  baseSnapshot.value = JSON.stringify(document.value)
}

async function loadArticle(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    acceptArticle(await api.getArticle(articleId.value))
    void loadImageSettings()
  } catch (cause) {
    reportError(errorMessage(cause))
  } finally {
    loading.value = false
  }
}

async function loadImageSettings(): Promise<void> {
  try {
    imageSettings.value = await api.getArticleImageSettings()
  } catch {
    imageSettings.value = null
  }
}

async function saveArticle(showMessage = true): Promise<boolean> {
  if (!document.value || saving.value) return false
  if (!isDirty.value) {
    if (showMessage) message.value = '内容没有变化。'
    return true
  }
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const savedArticle = article.value
      ? await api.saveArticle(article.value.id, article.value.version, document.value)
      : await api.createArticle(document.value.title, document.value)
    acceptArticle(savedArticle)
    if (showMessage) message.value = `已保存为 v${savedArticle.version}。`
    return true
  } catch (cause) {
    reportError(errorMessage(cause))
    return false
  } finally {
    saving.value = false
  }
}

function askLeaveChoice(): Promise<LeaveChoice> {
  if (!editorActive.value) return Promise.resolve('stay')
  leaveDialogOpen.value = true
  return new Promise((resolve) => {
    resolveLeaveChoice = resolve
  })
}

function finishLeaveChoice(choice: LeaveChoice): void {
  const resolve = resolveLeaveChoice
  if (!resolve) return
  resolveLeaveChoice = null
  leaveDialogOpen.value = false
  resolve(choice)
}

async function guardDraftNavigation(to: RouteLocationNormalized, from: RouteLocationNormalized): Promise<boolean> {
  latestLeaveTarget = to
  finishLeaveChoice('stay')
  if (to.fullPath === from.fullPath) return true
  if (to.redirectedFrom && saveLeaveTarget === to.redirectedFrom) {
    redirectDraftLeave(saveLeaveTarget, to)
    saveLeaveTarget = to
    return true
  }
  if (to.redirectedFrom && discardLeaveTarget === to.redirectedFrom) {
    redirectDraftLeave(discardLeaveTarget, to)
    discardLeaveTarget = to
    return true
  }
  if (saveLeaveTarget) return false
  if (!shouldProtectDraft.value) return true
  const coordinatesHistory = beginDraftLeave(to, (realigned) => {
    reportError(realigned
      ? '原浏览器历史条目无法恢复。地址已回到文章编辑页，编辑内容已保留，可重试离开。'
      : '无法恢复文章编辑页的地址。编辑内容已保留，可重试离开。')
  })
  if (saving.value || error.value) return false

  const choice = await askLeaveChoice()
  // zhw 2026-11-05 20:14:48 已被替代的导航交给路由器取消，避免各自回退浏览器历史。
  if (latestLeaveTarget !== to || (coordinatesHistory && !isCurrentDraftLeave(to))) return true
  if (choice === 'stay') return false
  if (choice === 'save') {
    saveLeaveTarget = to
    approveDraftSave(to)
    const saved = await saveArticle(false)
    return latestLeaveTarget !== to || saved
  }
  discardLeaveTarget = to
  return true
}

const stopLeaveAfterEach = router.afterEach((to, _from, failure) => {
  if (isNavigationFailure(failure, NavigationFailureType.duplicated) && to.fullPath === route.fullPath) {
    latestLeaveTarget = null
    finishLeaveChoice('stay')
  }
  if (latestLeaveTarget === to) latestLeaveTarget = null
  if (saveLeaveTarget === to) saveLeaveTarget = null
  if (discardLeaveTarget !== to) return
  discardLeaveTarget = null
  if (failure) return
  if (article.value) acceptArticle(article.value)
  else initializeNewArticleDraft()
})

const stopLeaveOnError = router.onError((_cause, to) => {
  if (latestLeaveTarget === to) latestLeaveTarget = null
  if (saveLeaveTarget === to) saveLeaveTarget = null
  if (discardLeaveTarget === to) discardLeaveTarget = null
})

function warnBeforeUnload(event: BeforeUnloadEvent): void {
  event.preventDefault()
  event.returnValue = ''
}

function deactivateEditor(): void {
  editorActive.value = false
  imageModeDialogOpen.value = false
  finishLeaveChoice('stay')
}

async function openSavedArticle(fromPath: string): Promise<void> {
  if (route.name !== 'article-new' || route.fullPath !== fromPath || !article.value) return
  try {
    await router.replace({ name: 'article-edit', params: { articleId: article.value.id }, query: { source: 'new-article' } })
  } catch {
    reportError('文章已保存，暂时无法打开编辑页，请重试。')
  }
}

async function saveFromToolbar(): Promise<void> {
  const fromPath = route.fullPath
  if (await saveArticle()) await openSavedArticle(fromPath)
}

async function finalize(): Promise<void> {
  if (!document.value || saving.value) return
  if (article.value === null && !document.value.body_markdown.trim()) {
    reportError('文章正文不能为空。')
    return
  }
  if (!await confirmAction('保存当前内容并设为定稿？你之后仍可继续编辑并生成新版本。', '设为定稿', '保存并设为定稿')) return
  const fromPath = route.fullPath
  if (!await saveArticle(false) || !article.value) return
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    acceptArticle(await api.finalizeArticle(articleId.value))
    message.value = '文章已设为定稿，可以进入发布页。'
  } catch (cause) {
    reportError(errorMessage(cause))
    return
  } finally {
    saving.value = false
  }
  await openSavedArticle(fromPath)
}

function updateParagraphPrompt(blockId: string, value: string): void {
  if (!document.value) return
  document.value.blocks = document.value.blocks.map((block) => String(block.id || '') === blockId
    ? { ...block, image_role: 'module', image_prompt: value }
    : block)
}

function articleImageUrl(path: string, slotId?: string): string {
  const filename = path.split('/').pop() || ''
  if (!filename) return ''
  const url = api.articleImageUrl(articleId.value, filename)
  return slotId ? `${url}?slot=${encodeURIComponent(slotId)}` : url
}

function isImageInserted(blockId: string): boolean {
  return !!blockId && visibleBody.value.includes(`?slot=${encodeURIComponent(blockId)}`)
}

function openImagePreview(url: string, title: string): void {
  if (!url) return
  imagePreviewUrl.value = url
  imagePreviewTitle.value = title
  imagePreviewDialogOpen.value = true
}

function insertParagraphImage(item: (typeof paragraphImagePrompts.value)[number]): void {
  if (!item.image_path) return
  const result = bodyEditor.value?.insertImageAtAnchor({
    src: articleImageUrl(item.image_path, item.id),
    alt: item.text ? `正文配图：${item.text.replace(/\]|\r|\n/g, ' ').slice(0, 120)}` : '正文配图',
    slotId: item.id,
    anchorText: item.text,
  })
  if (!result || result === 'unavailable') {
    message.value = '正文编辑器尚未就绪，请稍后再试。'
  } else if (result === 'replaced') {
    message.value = '已更新正文中的这张配图。'
  } else if (result === 'inserted-after-paragraph') {
    message.value = '配图已插入到对应段落后。'
  } else {
    message.value = '未找到唯一对应段落，配图已插入到编辑器光标所在段落后。'
  }
}

function imageTargetKey(target: ArticleImageTarget, blockId: string | null): string | null {
  return target === 'cover' ? 'cover' : blockId
}

function isImageGenerationActive(target: ArticleImageTarget, blockId: string | null): boolean {
  const key = imageTargetKey(target, blockId)
  return key !== null && generatingImageTarget.value === key
}

function imageGenerationQueuePosition(target: ArticleImageTarget, blockId: string | null): number {
  const articleId = article.value?.id
  if (!articleId) return 0
  const index = imageGenerationQueue.value.findIndex((job) => job.articleId === articleId && job.target === target && job.blockId === blockId)
  return index < 0 ? 0 : index + 1
}

function hasImageGenerationWork(): boolean {
  return !!generatingImageTarget.value || imageGenerationQueue.value.length > 0
}

function imageGenerationButtonText(target: ArticleImageTarget, blockId: string | null, idleText: string): string {
  if (isImageGenerationActive(target, blockId)) {
    return generatingImageMode.value === 'vector' ? '图解生成中' : 'AI 生图中'
  }
  const position = imageGenerationQueuePosition(target, blockId)
  return position ? `排队第${position}位` : idleText
}

function openImageModeDialog(target: ArticleImageTarget, blockId: string | null = null): void {
  if (!article.value || saving.value || uploadingCover.value || isImageGenerationActive(target, blockId) || imageGenerationQueuePosition(target, blockId)) return
  imageRequest.value = { target, blockId }
  imageModeDialogOpen.value = true
  void loadImageSettings()
}

async function copyImagePrompt(prompt: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(prompt)
    message.value = 'AI 生图提示词已复制。'
  } catch {
    reportError('无法复制提示词，可在输入框内选中文字后手动复制。')
  }
}

function acceptGeneratedImage(value: ArticleRecord, target: 'cover' | 'paragraph', blockId: string | null): void {
  const pending = isDirty.value && document.value ? structuredClone(toRaw(document.value)) : null
  const previousBlock = target === 'paragraph'
    ? document.value?.blocks.find((block) => String(block.id || '') === blockId)
    : undefined
  const savedBlock = target === 'paragraph'
    ? value.document.blocks.find((block) => String(block.id || '') === blockId)
    : undefined
  const previousPath = typeof previousBlock?.image_path === 'string' ? previousBlock.image_path : ''
  const nextPath = typeof savedBlock?.image_path === 'string' ? savedBlock.image_path : ''
  const sourceBody = pending?.body_markdown ?? document.value?.body_markdown ?? value.document.body_markdown
  let updatedBody = sourceBody
  if (blockId && previousPath && nextPath && previousPath !== nextPath) {
    const oldSource = articleImageUrl(previousPath, blockId)
    const newSource = articleImageUrl(nextPath, blockId)
    updatedBody = sourceBody.split(oldSource).join(newSource)
  }
  acceptArticle(value)
  if (!pending) {
    if (document.value && updatedBody !== document.value.body_markdown) document.value.body_markdown = updatedBody
    if (blockId && previousPath && nextPath && previousPath !== nextPath) {
      bodyEditor.value?.replaceManagedImageSource(blockId, articleImageUrl(nextPath, blockId))
    }
    return
  }
  const saved = value.document
  if (target === 'cover') {
    pending.cover_image_path = saved.cover_image_path
    pending.cover_image_name = saved.cover_image_name
    pending.cover_image_model_id = saved.cover_image_model_id
    pending.cover_image_generated_at = saved.cover_image_generated_at
  } else {
    const currentBlock = pending.blocks.find((block) => block.id === blockId)
    const persistedBlock = saved.blocks.find((block) => block.id === blockId)
    if (currentBlock && persistedBlock) {
      for (const key of ['image_path', 'image_name', 'image_model_id', 'image_generated_at']) currentBlock[key] = persistedBlock[key]
    }
    pending.body_markdown = updatedBody
  }
  document.value = pending
  if (blockId && previousPath && nextPath && previousPath !== nextPath) {
    bodyEditor.value?.replaceManagedImageSource(blockId, articleImageUrl(nextPath, blockId))
  }
}

function generateArticleImage(mode: ArticleImageMode): void {
  if (!imageModeDialogOpen.value || !article.value || saving.value || uploadingCover.value) return
  const { target, blockId } = imageRequest.value
  if (isImageGenerationActive(target, blockId) || imageGenerationQueuePosition(target, blockId)) return
  if (mode === 'provider' && (!imageSettings.value?.configured || !selectedImagePrompt.value.trim())) return
  imageModeDialogOpen.value = false
  imageGenerationQueue.value.push({ articleId: article.value.id, target, blockId, mode })
  error.value = ''
  message.value = ''
  void processImageGenerationQueue()
}

async function processImageGenerationQueue(): Promise<void> {
  if (imageGenerationWorkerRunning) return
  imageGenerationWorkerRunning = true
  try {
    while (imageGenerationQueue.value.length) {
      const job = imageGenerationQueue.value.shift()
      if (!job || article.value?.id !== job.articleId) continue
      generatingImageTarget.value = imageTargetKey(job.target, job.blockId)
      generatingImageMode.value = job.mode
      try {
        if (isDirty.value && !await saveArticle(false)) continue
        if (article.value?.id !== job.articleId) continue
        const result = await api.generateArticleImage(job.articleId, job.target, job.blockId, true, job.mode)
        if (article.value?.id !== job.articleId) continue
        acceptGeneratedImage(result.article, job.target, job.blockId)
        const label = job.target === 'cover' ? '封面' : '正文配图'
        message.value = label + (job.mode === 'vector' ? '知识图解' : '') + '已生成并保存到 v' + result.article.version + '。'
      } catch (cause) {
        if (article.value?.id === job.articleId) reportError(errorMessage(cause), true)
      } finally {
        generatingImageTarget.value = null
        generatingImageMode.value = null
      }
    }
  } finally {
    imageGenerationWorkerRunning = false
  }
}

async function copyCurrentArticle(): Promise<void> {
  const title = visibleTitle.value.trim()
  const summary = visibleSummary.value.trim()
  const body = visibleBody.value
  const outro = document.value?.outro.trim() || ''
  const text = [title, summary, body, outro].filter(Boolean).join('\n\n')
  const html = `${title ? `<h1>${escapeHtml(title)}</h1>` : ''}${summary ? `<p>${escapeHtml(summary)}</p>` : ''}${markdown.render(body)}${outro ? markdown.render(outro) : ''}`
  try {
    if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
      await navigator.clipboard.write([new ClipboardItem({ 'text/html': new Blob([html], { type: 'text/html' }), 'text/plain': new Blob([text], { type: 'text/plain' }) })])
    } else if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      throw new Error('clipboard unavailable')
    }
    message.value = '已复制通用长文。'
  } catch {
    reportError('复制失败；请检查浏览器剪贴板权限。')
  }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character] || character)
}

async function uploadCover(event: Event): Promise<void> {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file || !article.value || !document.value) return
  if (isDirty.value && !await saveArticle(false)) return
  uploadingCover.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api.uploadArticleCover(articleId.value, file)
    acceptArticle(result.article)
    message.value = '封面已上传。'
  } catch (cause) {
    reportError(errorMessage(cause))
  } finally {
    uploadingCover.value = false
    if (coverInput.value) coverInput.value.value = ''
  }
}

onActivated(() => {
  if (route.name === 'article-new') {
    if (!saving.value) initializeNewArticleDraft()
  } else {
    void loadArticle()
  }
  editorActive.value = true
})

onBeforeRouteLeave(guardDraftNavigation)
onBeforeRouteUpdate(guardDraftNavigation)
onDeactivated(deactivateEditor)
onBeforeUnmount(() => {
  deactivateEditor()
  stopLeaveAfterEach()
  stopLeaveOnError()
})

watch(shouldProtectDraft, (shouldProtect, _, onCleanup) => {
  if (!shouldProtect) return
  window.addEventListener('beforeunload', warnBeforeUnload)
  onCleanup(() => window.removeEventListener('beforeunload', warnBeforeUnload))
}, { flush: 'sync' })
</script>

<template>
  <el-config-provider :locale="zhCn">
  <section class="article-editor-page" aria-labelledby="article-editor-title">
    <h1 id="article-editor-title" class="sr-only">{{ route.name === 'article-new' ? '新建文章' : '编辑文章' }}</h1>
    <header class="article-editor-toolbar" aria-label="文章操作栏">
      <div class="article-editor-actions">
        <el-button size="large" :disabled="!document || saving" @click="copyCurrentArticle">复制文章</el-button>
        <el-button size="large" :disabled="!document || saving" @click="saveFromToolbar">{{ saving ? '正在保存…' : article ? '保存版本' : '保存草稿' }}</el-button>
        <el-button size="large" :disabled="!document" @click="previewDialogOpen = true">预览</el-button>
        <p v-if="isNewArticleSession && document" class="helper article-save-status" role="status">{{ article && !isDirty ? '草稿已保存' : '未保存' }}</p>
        <el-button type="primary" size="large" :disabled="!document || saving" @click="finalize">保存并设为定稿</el-button>
      </div>
    </header>

    <el-alert v-if="message" class="article-editor-feedback" type="success" :closable="false" show-icon :title="message" role="status" />
    <el-dialog v-model="leaveDialogOpen" class="article-leave-dialog" title="草稿尚未保存" :teleported="false" :show-close="false" :close-on-click-modal="false" :close-on-press-escape="false">
      <p class="article-dialog-copy">保存后将前往目标页面。放弃后会丢失本次未保存的修改。</p>
      <template #footer>
        <el-button @click="finishLeaveChoice('stay')">继续编辑</el-button>
        <el-button type="danger" plain @click="finishLeaveChoice('discard')">放弃并离开</el-button>
        <el-button type="primary" @click="finishLeaveChoice('save')">保存并离开</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="errorDialogOpen" class="article-error-dialog" :title="imageGenerationError ? '配图生成失败' : '操作失败'" :teleported="false" @close="closeErrorDialog">
      <p class="article-dialog-copy" role="alert">{{ errorDialogMessage }}</p>
      <template #footer>
        <el-button @click="closeErrorDialog">知道了</el-button>
        <RouterLink v-if="imageGenerationError" to="/settings" custom v-slot="{ navigate }"><el-button type="primary" @click="navigate">前往模型设置</el-button></RouterLink>
      </template>
    </el-dialog>
    <ArticleImageModeDialog
      v-model:open="imageModeDialogOpen"
      :target="imageRequest.target"
      :prompt-available="!!selectedImagePrompt.trim()"
      :image-settings="imageSettings"
      :replacing="selectedImageExists"
      @generate="generateArticleImage"
    />
    <el-dialog v-model="imagePreviewDialogOpen" class="article-image-preview-dialog" :title="imagePreviewTitle || '配图预览'" :teleported="false" :close-on-click-modal="true" :close-on-press-escape="true">
      <img v-if="imagePreviewUrl" class="article-image-preview-full" :src="imagePreviewUrl" :alt="imagePreviewTitle || '文章配图'" />
      <template #footer>
        <el-button size="large" @click="imagePreviewDialogOpen = false">关闭</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="previewDialogOpen" class="article-preview-dialog" title="文章预览" :teleported="false">
      <article v-if="document" class="article-full-preview">
        <img v-if="coverUrl" class="article-full-preview-cover" :src="coverUrl" alt="文章封面" />
        <header class="article-full-preview-header">
          <h1>{{ visibleTitle || '未命名文章' }}</h1>
          <p v-if="visibleSummary" class="article-full-preview-summary">{{ visibleSummary }}</p>
          <div class="article-full-preview-meta">
            <span v-if="document.author">{{ document.author }}</span>
            <time v-if="document.published_at" :datetime="document.published_at">{{ document.published_at }}</time>
          </div>
        </header>
        <MarkdownPreview :content="visibleBody || '正文为空。'" :managed-image-prefix="api.articleImageUrl(articleId, '')" />
        <section v-if="document.outro" class="article-full-preview-outro">
          <MarkdownPreview :content="document.outro" />
        </section>
      </article>
      <template #footer>
        <el-button size="large" @click="previewDialogOpen = false">关闭</el-button>
      </template>
    </el-dialog>
    <div v-if="loading" class="panel article-editor-loading" role="status">正在读取文章…</div>
    <div v-else-if="document" class="article-editor-workspace" :inert="isNewArticleSession && saving">
      <div class="article-editor-layout">
      <el-card class="article-editor-main" shadow="never">
        <div class="article-field-head"><label class="form-label article-label" for="article-title-input">文章标题</label><span class="article-character-count">{{ visibleTitle.length.toLocaleString('zh-CN') }} 字</span></div>
        <el-input id="article-title-input" v-model="visibleTitle" class="article-title-input" maxlength="200" placeholder="输入文章标题" />
        <div class="article-field-head"><label class="form-label article-label" for="article-summary-input">文章摘要</label><span class="article-character-count">{{ visibleSummary.length.toLocaleString('zh-CN') }} 字</span></div>
        <el-input id="article-summary-input" v-model="visibleSummary" class="article-summary-input" type="textarea" maxlength="2000" :rows="3" placeholder="用于文章列表和平台摘要" />
        <div class="article-editor-content-head"><label id="article-body-label" class="form-label">正文</label><span class="article-character-count">{{ visibleBody.length.toLocaleString('zh-CN') }} 字</span></div>
        <MarkdownRichTextEditor ref="bodyEditor" id="article-body-input" v-model="visibleBody" class="article-body-input" labelled-by="article-body-label" placeholder="从这里开始写正文。" />
        <label class="form-label article-label" for="article-outro-input">文章结尾与互动引导</label>
        <el-input id="article-outro-input" v-model="document.outro" class="article-summary-input" type="textarea" maxlength="4000" :rows="3" placeholder="回应开头问题，并设置一个具体互动问题。发布时会附加在正文末尾。" />
        <div class="article-editor-meta">
          <label class="article-inline-field"><span>作者</span><el-input v-model="document.author" maxlength="200" /></label>
          <label class="article-inline-field"><span>发布日期</span><el-date-picker v-model="document.published_at" type="date" format="YYYY-MM-DD" value-format="YYYY-MM-DD" placeholder="选择发布日期" /></label>
        </div>
      </el-card>

      <aside class="article-editor-aside">
        <el-card class="article-aside-card article-image-panel" shadow="never">
          <div class="panel-head"><div><h2>文章配图</h2><p>可连续提交配图任务，系统按确认顺序逐张生成。</p></div></div>
          <h3 class="article-image-section-title">封面</h3>
          <button v-if="coverUrl" type="button" class="article-image-preview-link" aria-label="预览文章封面" @click="openImagePreview(coverUrl, '文章封面')">
            <img class="article-cover-preview" :src="coverUrl" alt="文章封面预览" />
            <span>点击预览</span>
          </button>
          <div v-else class="article-cover-empty">
            <svg viewBox="0 0 48 38" fill="none" aria-hidden="true">
              <rect x="3" y="3" width="42" height="32" rx="6" stroke="currentColor" stroke-width="1.5"/>
              <path d="m4 29 13-10 8 6 6-5 13 11" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
              <circle cx="32" cy="13" r="3" fill="currentColor"/>
            </svg>
            <span>添加文章封面</span>
          </div>
          <input ref="coverInput" class="sr-only" type="file" accept="image/jpeg,image/png,image/webp" :disabled="!article" @change="uploadCover" />
          <div class="article-image-actions">
            <el-button type="primary" size="small" :disabled="!article || saving || uploadingCover || isImageGenerationActive('cover', null) || !!imageGenerationQueuePosition('cover', null)" :loading="isImageGenerationActive('cover', null)" @click="openImageModeDialog('cover')">{{ imageGenerationButtonText('cover', null, coverUrl ? '重新生成' : '生成配图') }}</el-button>
            <el-button size="small" :disabled="!article || uploadingCover || saving || hasImageGenerationWork()" @click="coverInput?.click()">{{ uploadingCover ? '上传中…' : coverUrl ? '上传新封面' : '上传封面' }}</el-button>
          </div>
          <p v-if="!article" class="helper">保存草稿后可使用图片功能</p>
          <p v-else class="helper">支持 JPEG、PNG、WebP，单张不超过 10 MB。</p>
          <div class="article-prompt-head">
            <label class="form-label" for="cover-prompt-input">AI 生图提示词</label>
            <el-button text size="small" :disabled="!document.cover_image_prompt.trim()" @click="copyImagePrompt(document.cover_image_prompt)">复制</el-button>
          </div>
          <el-input id="cover-prompt-input" v-model="document.cover_image_prompt" class="article-prompt-input" type="textarea" :rows="4" maxlength="4000" placeholder="描述画面主体、场景或风格。" />
          <p v-if="document.cover_image_model_id || document.cover_image_name" class="helper">{{ document.cover_image_model_id || document.cover_image_name }}</p>
          <div v-if="paragraphImagePrompts.length" class="article-image-prompt-list">
            <h3 class="article-image-section-title">正文配图</h3>
            <div v-for="(item, index) in paragraphImagePrompts" :key="item.id" class="article-paragraph-prompt">
              <p class="article-paragraph-image-title">配图 {{ index + 1 }}</p>
              <p class="article-paragraph-image-context" :title="item.text">{{ item.text }}</p>
              <button v-if="item.image_path" type="button" class="article-image-preview-link" :aria-label="'预览正文配图 ' + (index + 1)" @click="openImagePreview(articleImageUrl(item.image_path), '正文配图 ' + (index + 1))">
                <img class="article-inline-image-preview" :src="articleImageUrl(item.image_path)" :alt="'正文配图 ' + (index + 1)" />
                <span>点击预览</span>
              </button>
              <div class="article-prompt-head">
                <label class="form-label" :for="'paragraph-prompt-' + item.id">AI 生图提示词</label>
                <el-button text size="small" :disabled="!item.image_prompt.trim()" @click="copyImagePrompt(item.image_prompt)">复制</el-button>
              </div>
              <el-input :id="'paragraph-prompt-' + item.id" class="article-prompt-input" type="textarea" :rows="3" maxlength="4000" placeholder="描述这张配图的画面重点。" :model-value="item.image_prompt" @update:model-value="updateParagraphPrompt(item.id, $event)" />
              <p v-if="item.image_model_id" class="helper">{{ item.image_model_id }}</p>
              <div class="article-image-actions">
                <el-button type="primary" plain size="small" :disabled="!article || saving || uploadingCover || isImageGenerationActive('paragraph', item.id) || !!imageGenerationQueuePosition('paragraph', item.id)" :loading="isImageGenerationActive('paragraph', item.id)" @click="openImageModeDialog('paragraph', item.id)">{{ imageGenerationButtonText('paragraph', item.id, item.image_path ? '重新生成' : '生成配图') }}</el-button>
                <el-button size="small" :disabled="!item.image_path || saving" @click="insertParagraphImage(item)">{{ isImageInserted(item.id) ? '更新正文图片' : '插入正文' }}</el-button>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-if="article && (document.analysis.core_point || document.analysis.reason)" class="article-aside-card article-analysis-card" shadow="never">
          <div class="panel-head"><div><h2>文章提炼</h2><p>基于原始口播稿的结构分析。</p></div></div>
          <p v-if="document.analysis.core_point"><strong>核心观点</strong><br/>{{ document.analysis.core_point }}</p>
          <p v-if="document.analysis.reason"><strong>改写方式</strong><br/>{{ document.analysis.reason }}</p>
          <p v-if="document.outro"><strong>互动结尾</strong><br/>{{ document.outro }}</p>
        </el-card>

        <el-card v-if="article" class="article-aside-card article-version-card" shadow="never">
          <div class="panel-head"><div><h2>版本记录</h2><p>保存内容会保留历史版本。</p></div></div>
          <ul><li v-for="version in article.versions.slice(0, 5)" :key="version.version"><span>v{{ version.version }} · {{ version.note || '编辑' }}</span><time>{{ new Date(version.created_at).toLocaleDateString('zh-CN') }}</time></li></ul>
        </el-card>
      </aside>
      </div>
    </div>
  </section>
  </el-config-provider>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类覆盖(全量落地时上移 styles.css)—— */
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.form-label { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }

.article-editor-page {
  display: grid; gap: 9px; min-width: 0;
  padding-bottom: 112px;
  --el-color-primary: var(--pine);
  --el-color-primary-light-3: var(--pine-2);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-text-color-placeholder: var(--text-placeholder);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
  --el-border-radius-base: 6px;
}
.article-editor-toolbar {
  position: fixed; z-index: 45; right: 0; bottom: 0; left: var(--sidebar-width);
  display: flex; align-items: center; justify-content: center; gap: 12px;
  min-height: 82px; padding: 12px 22px calc(12px + env(safe-area-inset-bottom));
  border-top: 1px solid var(--line); background: color-mix(in srgb, var(--paper) 96%, transparent);
  box-shadow: 0 -8px 24px rgb(31 35 41 / 8%); backdrop-filter: blur(12px);
}
.article-editor-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 12px; width: auto; }
.article-editor-actions > .el-button { box-sizing: border-box; flex: 0 1 160px; width: 160px; height: 48px; margin: 0; padding: 0 16px; font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.article-image-actions > .el-button { min-width: 0; min-height: 34px; margin: 0; padding-inline: 8px; font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.article-save-status { margin: 0; padding-inline: 4px; font-size: var(--fs-body); }
.article-editor-feedback { margin: 0; }
.article-editor-page :deep(.el-alert__title) { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.article-editor-page :deep(.el-dialog) { width: min(520px, calc(100vw - 28px)); border-radius: 10px; }
.article-editor-page :deep(.el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-dialog-copy { margin: 0; color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); overflow-wrap: anywhere; }
.article-leave-dialog :deep(.el-dialog__footer) { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.article-leave-dialog :deep(.el-dialog__footer .el-button + .el-button) { margin-left: 0; }
.article-editor-page :deep(.el-dialog__footer .el-button) { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.article-editor-page :deep(.article-image-mode-dialog) { width: min(580px, calc(100vw - 28px)); }
.article-editor-page :deep(.article-image-mode-dialog .el-dialog__body) { max-height: min(65vh, 580px); overflow: auto; }
.article-editor-workspace { display: grid; gap: 8px; min-width: 0; }
.article-editor-layout { display: grid; grid-template-columns: minmax(0, 1fr) 300px; align-items: start; gap: 12px; min-width: 0; }
.article-editor-page :deep(.el-card__body) { padding: 14px; }
.article-editor-main { min-width: 0; }
.article-label { display: block; margin: 10px 0 6px; }
.article-field-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.article-character-count { color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; white-space: nowrap; }
.article-title-input :deep(.el-input__wrapper) { min-height: 40px; }
.article-title-input :deep(.el-input__inner) { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-summary-input :deep(.el-textarea__inner) { min-height: 60px; resize: vertical; font-size: var(--fs-body); line-height: var(--lh-base); }
.article-editor-content-head { display: flex; justify-content: space-between; align-items: baseline; margin-top: 10px; }
.article-editor-content-head .form-label { margin: 0 0 7px; }
.article-editor-page :deep(.article-preview-dialog) { width: min(900px, calc(100vw - 32px)); max-height: 88vh; }
.article-editor-page :deep(.article-preview-dialog .el-dialog__body) { max-height: calc(88vh - 130px); overflow: auto; padding: 8px 24px 24px; }
.article-editor-page :deep(.article-image-preview-dialog) { width: min(960px, calc(100vw - 28px)); max-height: 92vh; }
.article-editor-page :deep(.article-image-preview-dialog .el-dialog__body) { display: grid; place-items: center; max-height: calc(92vh - 130px); overflow: auto; padding: 12px; }
.article-image-preview-full { display: block; max-width: 100%; max-height: 76vh; border-radius: 8px; object-fit: contain; }
.article-full-preview { max-width: 760px; margin: 0 auto; color: var(--text-primary); font-size: var(--fs-body-lg); line-height: var(--lh-loose); overflow-wrap: anywhere; }
.article-full-preview-cover { display: block; width: 100%; max-height: 420px; margin: 0 auto 28px; border-radius: 8px; object-fit: contain; }
.article-full-preview-header { margin-bottom: 28px; }
.article-full-preview-header h1 { margin: 0 0 16px; color: var(--text-primary); font-size: var(--fs-title-2xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.article-full-preview-summary { margin: 0 0 16px; color: var(--text-secondary); font-size: var(--fs-body-lg); line-height: var(--lh-base); }
.article-full-preview-meta { display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.article-full-preview :deep(.markdown-preview) { color: var(--text-primary); font-size: var(--fs-body-lg); line-height: var(--lh-loose); }
.article-full-preview :deep(.markdown-preview img) { display: block; max-width: 100%; max-height: 640px; height: auto; margin: 20px auto; border-radius: 8px; object-fit: contain; }
.article-full-preview-outro { margin-top: 28px; padding-top: 20px; border-top: 1px solid var(--line); }
.article-editor-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.article-inline-field { display: grid; gap: 6px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.article-inline-field :deep(.el-input__wrapper), .article-inline-field :deep(.el-date-editor) { width: 100%; min-height: 40px; }
.article-inline-field :deep(.el-input__inner) { font-size: var(--fs-body-sm); }
.article-editor-aside { display: grid; gap: 12px; min-width: 0; }
.article-aside-card { min-width: 0; }
.article-aside-card .panel-head { margin-bottom: 12px; }
.article-aside-card .panel-head h2 { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-image-section-title { margin: 0 0 8px; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.article-cover-preview, .article-cover-empty { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: contain; }
.article-cover-empty { display: grid; place-content: center; justify-items: center; gap: 8px; border: 1px dashed var(--line); border-radius: 10px; color: var(--text-tertiary); background: var(--paper-2); font-size: var(--fs-body-sm); }
.article-cover-empty svg { width: 42px; height: 34px; color: var(--pine); }
.article-image-preview-link { display: grid; position: relative; width: 100%; padding: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 10px; background: var(--paper-2); color: inherit; text-align: left; cursor: zoom-in; }
.article-image-preview-link:focus-visible { outline: 2px solid var(--pine); outline-offset: 2px; }
.article-image-preview-link span { position: absolute; bottom: 6px; right: 6px; padding: 2px 7px; border: 1px solid var(--line); border-radius: 6px; background: var(--paper); color: var(--text-primary); font-size: var(--fs-caption); }
.article-image-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 9px; }
.article-image-actions--single { grid-template-columns: minmax(0, 1fr); margin-top: 2px; }
.article-aside-card .helper { margin: 8px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); overflow-wrap: anywhere; }
.article-prompt-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 6px 0 2px; }
.article-prompt-head .form-label { margin: 0; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.article-prompt-head .el-button { min-height: 32px; padding-inline: 7px; color: var(--pine); font-size: var(--fs-body-sm); }
.article-prompt-head .el-button.is-disabled { color: var(--text-tertiary); }
.article-prompt-input :deep(.el-textarea__inner) { min-height: 76px; resize: vertical; font-size: var(--fs-body); line-height: var(--lh-base); }
.article-image-prompt-list { display: grid; gap: 10px; margin-top: 17px; padding-top: 14px; border-top: 1px solid var(--line); min-width: 0; }
.article-image-prompt-list h3 { margin: 0; }
.article-paragraph-prompt { display: grid; gap: 7px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--paper-2); min-width: 0; }
.article-paragraph-image-title { margin: 0; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
.article-paragraph-image-context { min-width: 0; margin: 0; overflow: hidden; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); text-overflow: ellipsis; white-space: nowrap; }
.article-inline-image-preview { display: block; width: 100%; max-height: 180px; object-fit: contain; }
.article-analysis-card p { margin: 7px 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.65; }
.article-analysis-card strong { color: var(--ink); }
.article-version-card ul { display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }
.article-version-card li { display: flex; justify-content: space-between; gap: 9px; color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.article-version-card time { white-space: nowrap; }
.article-editor-loading { padding: 30px; color: var(--muted); }
@media (max-width: 1000px) { .article-editor-layout { grid-template-columns: minmax(0, 1fr) 260px; } }
@media (max-width: 780px) { .article-editor-layout { grid-template-columns: minmax(0, 1fr); } .article-editor-aside { grid-template-columns: repeat(2, minmax(0, 1fr)); } .article-editor-aside > :first-child { grid-row: span 2; } }
@media (max-width: 900px) { .article-editor-toolbar { left: 0; } }
@media (max-width: 560px) {
  .article-editor-toolbar { min-height: 82px; padding: 10px 12px calc(10px + env(safe-area-inset-bottom)); }
  .article-editor-page { padding-bottom: 140px; }
  .article-editor-actions { width: 100%; gap: 8px; }
  .article-editor-actions > .el-button { flex-basis: 160px; width: 160px; max-width: 100%; height: 48px; padding: 0 12px; font-size: var(--fs-body); }
  .article-editor-aside { grid-template-columns: 1fr; }
  .article-editor-aside > :first-child { grid-row: auto; }
  .article-editor-meta { grid-template-columns: 1fr; }
  .article-editor-page :deep(.article-preview-dialog .el-dialog__body) { max-height: calc(88vh - 120px); padding: 8px 16px 18px; }
  .article-editor-page :deep(.article-image-preview-dialog .el-dialog__body) { max-height: calc(92vh - 120px); padding: 8px; }
  .article-full-preview-header h1 { font-size: var(--fs-title-xl); }
}
</style>

<style>
/* 排版系统 v2：ElMessageBox 挂载到 body，单独映射工作台主题令牌。 */
.article-editor-confirm {
  --el-color-primary: var(--pine);
  --el-color-primary-light-3: var(--pine-2);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
  --el-border-radius-base: 6px;
}
.article-editor-confirm .el-message-box__title { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-editor-confirm .el-message-box__content { color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); }
.article-editor-confirm .el-button { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
</style>
