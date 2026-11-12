<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElAvatar, ElButton, ElCard, ElCheckbox, ElIcon, ElInput, ElLink, ElMessageBox, ElOption, ElSelect, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/avatar/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/card/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/icon/style/css'
import 'element-plus/es/components/link/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/tag/style/css'
import { CircleCheck, Loading } from '@element-plus/icons-vue'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import MarkdownPreview from '../components/MarkdownPreview.vue'
import VideoCoverCropEditor from '../components/VideoCoverCropEditor.vue'
import type { ArticlePlatformAccount, ArticlePlatformStatus, ArticlePublication, ArticlePublishTarget, ArticleRecord, CsdnPublishOptions, CsdnTagCatalog, ToutiaoAccountPublishOptions, ToutiaoPublishOptions, VideoPublishAccount } from '../api/types'

type PublishTargetOption = ArticlePublishTarget & { key: string; account_name: string | null; platform_name: string }

const route = useRoute()
const router = useRouter()
// 2026-11-09 01:22:14 zhw：该视图按 fullPath 缓存；固定文章 ID，避免切出时 useRoute.params 变空并破坏路由链接。
const articleId = String(route.params.articleId || '')
const article = ref<ArticleRecord | null>(null)
const platforms = ref<ArticlePlatformStatus[]>([])
const selected = ref<string[]>([])
const activePlatformId = ref('wechat')
const notifyFans = ref(false)
const loading = ref(true)
const publishing = ref(false)
const busyManual = ref('')
const refreshingPublicationId = ref('')
const error = ref('')
const message = ref('')
type PublishCover = { reference: string; preview_url: string }
const commonCovers = reactive<Record<string, PublishCover>>({})
const platformCovers = reactive<Record<string, PublishCover>>({})
const platformCoverRatios = reactive<Record<string, string>>({})
const coverRatio = ref('wide')
const coverRatios: Record<string, number> = { wide: 16 / 9, landscape: 4 / 3, portrait: 3 / 4, square: 1 }
const coverRatioNames: Record<string, string> = { wide: '16:9', landscape: '4:3', portrait: '3:4', square: '1:1' }
const coverTargetKeys = new Set(['wechat', 'csdn', 'zhihu', 'xiaohongshu', 'toutiao', 'baijiahao', 'weibo', 'toutiao-slot-1', 'toutiao-slot-2', 'toutiao-slot-3'])
const cropTarget = ref('')
const cropOpen = ref(false)
const cropUploading = ref(false)
const cropImageSrc = ref('')
const cropRatio = ref(16 / 9)
const csdnTags = ref<string[]>([])
const csdnTagInput = ref('')
const csdnTagCatalog = ref<CsdnTagCatalog | null>(null)
const csdnTagLoading = ref(false)
const csdnTagError = ref('')
const csdnDeclarationError = ref('')
const csdnTagCategory = ref('推荐')
const csdnTagSearchResults = ref<string[] | null>(null)
const visibleCsdnTags = computed(() => csdnTagSearchResults.value ?? csdnTagCatalog.value?.categories.find((item) => item.name === csdnTagCategory.value)?.tags ?? [])
const csdnDeclarationOptions = computed(() => csdnTagCatalog.value?.declarations ?? [])
const csdnDeclarationSelectionInvalid = computed(() => Boolean(csdnTagCatalog.value && csdnDeclarationOptions.value.length && !csdnDeclarationOptions.value.includes(csdnSettings.declaration)))
const csdnSettingsLocked = computed(() => currentPublication('csdn')?.status === 'manual_required')
const csdnSettings = reactive<CsdnPublishOptions>({ article_type: 'original', source_url: '', summary: '', visibility: 'public', template: 'simple', declaration: '', category: '', activity: '', topic: '', backup_gitcode: false, cross_publish: false })
const toutiaoSettings = reactive<ToutiaoPublishOptions>({ cover_mode: 'single', cover_image_paths: [], advertising: false, original_first: false, allow_tips: false, sync_micro: false, declarations: [], location: '', collection: '' })
const toutiaoAccountOptions = ref<ToutiaoAccountPublishOptions | null>(null)
const toutiaoOptionsLoading = ref(false)
const toutiaoOptionsError = ref('')
const toutiaoDeclarations = computed(() => toutiaoAccountOptions.value?.declarations || [])
const toutiaoOptionsVerified = computed(() => Boolean(toutiaoAccountOptions.value && !toutiaoOptionsError.value && !toutiaoOptionsLoading.value))
function toggleToutiaoDeclaration(declaration: string, checked: string | number | boolean) {
  const selected = toutiaoSettings.declarations.includes(declaration)
  const shouldSelect = checked === true || checked === declaration
  if (shouldSelect && !selected) toutiaoSettings.declarations.push(declaration)
  else if (!shouldSelect && selected) toutiaoSettings.declarations = toutiaoSettings.declarations.filter((item) => item !== declaration)
}
const toutiaoCollections = computed(() => toutiaoAccountOptions.value?.collections || [])
const toutiaoCoverModeNames = { single: '单图', triple: '三图', none: '无封面' } as const

function effectiveCover(platformId: string): PublishCover | null {
  const own = platformCovers[platformId]
  if (own) return own
  const common = commonCovers[platformCoverRatios[platformId] || '']
  if (common) return common
  const reference = article.value?.document.cover_image_path || ''
  return reference ? { reference, preview_url: api.articleCoverUrl(articleId) + '?v=' + article.value?.version } : null
}

function coverStorageKey(): string {
  return `article-publish-covers:${articleId}:${article.value?.version || 0}`
}

function saveCoverChoices(): void {
  try { sessionStorage.setItem(coverStorageKey(), JSON.stringify({ common: commonCovers, platforms: platformCovers, ratios: platformCoverRatios })) }
  catch { message.value = '浏览器无法保存临时封面选择，刷新后需要重新选择。' }
}

function restoreCoverChoices(): void {
  for (const key of Object.keys(commonCovers)) delete commonCovers[key]
  for (const key of Object.keys(platformCovers)) delete platformCovers[key]
  for (const key of Object.keys(platformCoverRatios)) delete platformCoverRatios[key]
  try {
    const saved = JSON.parse(sessionStorage.getItem(coverStorageKey()) || '{}') as Record<string, Record<string, unknown>>
    const prefix = `data/article-assets/${articleId}/`
    const previewPrefix = `/api/v1/articles/${articleId}/publish-covers/`
    for (const [kind, target] of [['common', commonCovers], ['platforms', platformCovers]] as const) {
      for (const [key, raw] of Object.entries(saved[kind] || {})) {
        if (kind === 'common' ? !Object.prototype.hasOwnProperty.call(coverRatios, key) : !coverTargetKeys.has(key)) continue
        const value = raw as Partial<PublishCover>
        const filename = typeof value?.reference === 'string' && value.reference.startsWith(prefix) ? value.reference.slice(prefix.length) : ''
        const match = /^([0-9a-f]{32})\.(?:jpe?g|png|webp)$/.exec(filename)
        if (match && value.preview_url === previewPrefix + match[1]) target[key] = { reference: value.reference!, preview_url: value.preview_url }
      }
    }
    for (const [key, value] of Object.entries(saved.ratios || {})) if (coverTargetKeys.has(key) && typeof value === 'string' && Object.prototype.hasOwnProperty.call(coverRatios, value)) platformCoverRatios[key] = value
  } catch { message.value = '保存的临时封面选择无法读取，请重新选择。' }
}

function openPublishCoverEditor(target: string): void {
  cropTarget.value = target
  const ratio = target === 'common' ? coverRatio.value : (platformCoverRatios[target] || coverRatio.value)
  cropRatio.value = coverRatios[ratio] || 16 / 9
  const emptyExtraToutiaoSlot = /^toutiao-slot-[23]$/.test(target) && !platformCovers[target]
  cropImageSrc.value = target === 'common' ? (commonCovers[ratio]?.preview_url || effectiveCover('')?.preview_url || '') : emptyExtraToutiaoSlot ? '' : (platformCovers[target]?.preview_url || (target === 'toutiao-slot-1' ? effectiveCover('toutiao')?.preview_url : effectiveCover(target)?.preview_url) || '')
  cropOpen.value = true
}

function effectiveToutiaoCover(slot: number): PublishCover | null {
  const own = platformCovers[`toutiao-slot-${slot}`]
  if (own) return own
  if (slot === 1) return commonCovers[platformCoverRatios.toutiao || coverRatio.value] || effectiveCover('toutiao')
  return null
}

function toutiaoCoverCount(): number {
  return toutiaoSettings.cover_mode === 'triple' ? 3 : toutiaoSettings.cover_mode === 'single' ? 1 : 0
}

function useToutiaoCommonCover(slot: number): void {
  delete platformCovers[`toutiao-slot-${slot}`]
  saveCoverChoices()
}

function toutiaoCoverPaths(): string[] {
  return Array.from({ length: toutiaoCoverCount() }, (_, index) => effectiveToutiaoCover(index + 1)?.reference || '')
}

async function savePublishCover(file: File): Promise<void> {
  if (cropUploading.value) return
  cropUploading.value = true
  error.value = ''
  try {
    const uploaded = await api.uploadArticlePublishCover(articleId, file)
    const cover = { reference: uploaded.reference, preview_url: uploaded.preview_url }
    if (cropTarget.value === 'common') commonCovers[coverRatio.value] = cover
    else platformCovers[cropTarget.value] = cover
    saveCoverChoices()
    cropOpen.value = false
  } catch (cause) { error.value = errorMessage(cause) }
  finally { cropUploading.value = false }
}

function useCommonCover(platformId: string): void {
  delete platformCovers[platformId]
  saveCoverChoices()
}

function chooseCsdnTag(rawTag: string, custom = false): void {
  const tag = rawTag.trim()
  if (!tag) return
  if (!csdnTagCatalog.value) { csdnTagError.value = '请先读取 CSDN 标签和数量限制。'; return }
  if (custom && csdnTagCatalog.value.custom_allowed === false) { csdnTagError.value = '当前 CSDN 账号暂不支持自定义标签，请选择平台标签。'; return }
  if (custom && (tag.length > 15 || /[~^$@%&!*()<>:;'"{}【】]/.test(tag))) { csdnTagError.value = '自定义标签不能超过 15 个字符，也不能包含平台不支持的特殊字符。'; return }
  if (csdnTags.value.some((item) => item.toLocaleLowerCase() === tag.toLocaleLowerCase())) { csdnTagError.value = '该 CSDN 标签已添加。'; return }
  if (csdnTags.value.length >= csdnTagCatalog.value.max_tags) { csdnTagError.value = `CSDN 当前最多选择 ${csdnTagCatalog.value.max_tags} 个标签。`; return }
  csdnTags.value.push(tag)
  if (custom) csdnTagInput.value = ''
  csdnTagError.value = ''
}

function toggleCsdnTag(tag: string): void {
  const existing = csdnTags.value.find((item) => item.toLocaleLowerCase() === tag.toLocaleLowerCase())
  if (existing) {
    csdnTags.value = csdnTags.value.filter((item) => item !== existing)
    csdnTagError.value = ''
  } else chooseCsdnTag(tag)
}

async function loadCsdnTags(keyword = ''): Promise<void> {
  if (csdnTagLoading.value || !article.value) return
  csdnTagLoading.value = true
  csdnTagError.value = ''
  try {
    const catalog = await api.getCsdnArticleTags(articleId, keyword.trim())
    csdnTagCatalog.value = catalog
    if (!catalog.categories.some((item) => item.name === csdnTagCategory.value)) csdnTagCategory.value = catalog.categories[0]?.name || ''
    csdnTagSearchResults.value = keyword.trim() ? catalog.results : null
    const declarations = Array.isArray(catalog.declarations) ? catalog.declarations : []
    csdnDeclarationError.value = declarations.length ? '' : 'CSDN 当前未返回创作声明选项，请重新获取后再发表。'
    if (!csdnSettings.declaration && declarations.includes('无声明')) csdnSettings.declaration = '无声明'
  } catch (cause) {
    const reason = cause instanceof ApiError ? cause.message : '无法读取 CSDN 标签和创作声明，请检查登录状态后重试。'
    csdnTagError.value = reason
    if (!csdnDeclarationOptions.value.length) csdnDeclarationError.value = reason
  } finally {
    csdnTagLoading.value = false
  }
}

async function loadToutiaoOptions(force = false): Promise<void> {
  if (toutiaoOptionsLoading.value || !activePlatform.value?.connected || (!force && toutiaoAccountOptions.value)) return
  if (force) toutiaoAccountOptions.value = null
  toutiaoOptionsLoading.value = true
  toutiaoOptionsError.value = ''
  try {
    toutiaoAccountOptions.value = await api.getToutiaoPublishOptions()
  } catch (cause) {
    toutiaoOptionsError.value = errorMessage(cause)
  } finally {
    toutiaoOptionsLoading.value = false
  }
}

function selectCsdnTagCategory(name: string): void {
  csdnTagCategory.value = name
  csdnTagSearchResults.value = null
  csdnTagInput.value = ''
  csdnTagError.value = ''
}

watch(() => [activePlatformId.value, platformSelectionCount('csdn'), Boolean(article.value)] as const, ([platform, count, hasArticle]) => {
  if (platform === 'csdn' && count > 0 && hasArticle && !csdnTagCatalog.value && !csdnTagLoading.value && !csdnTagError.value) void loadCsdnTags()
})
const publishOptions = reactive<Record<string, { title: string; summary: string; author: string; note: string }>>({
  wechat: { title: '', summary: '', author: '', note: '' },
  zhihu: { title: '', summary: '', author: '', note: '' },
  xiaohongshu: { title: '', summary: '', author: '', note: '' },
  toutiao: { title: '', summary: '', author: '', note: '' },
})
let publicationPollTimer: number | undefined
let refreshingPublications = false
let pageRequestId = 0
let platformStatusRequestId = 0
let pageActive = false
let loadedArticleId = ''
const pendingAccountPreflights = reactive(new Set<string>())
const accountCheckCacheTtlMs = 5 * 60 * 1000
let accountCheckPollTimer: number | undefined
let refreshingAccountStatuses = false
let videoAccountStatusRevision = 0
const platformNames: Record<string, string> = {
  wechat: '微信公众号', csdn: 'CSDN', zhihu: '知乎', xiaohongshu: '小红书',
  toutiao: '今日头条', baijiahao: '百家号', weibo: '微博',
}
const latestPublications = computed(() => {
  const result: Record<string, ArticlePublication> = {}
  for (const publication of article.value?.publications ?? []) {
    const key = targetKey(publication.platform_id, publication.account_id)
    if (!result[key]) result[key] = publication
  }
  return result
})
const currentTitle = computed(() => article.value?.document.title || article.value?.title || '未命名文章')
const currentSummary = computed(() => article.value?.document.summary || '暂无文章摘要。')
const hasCoverImage = computed(() => Boolean(article.value?.document.cover_image_path))
const bodyImages = computed(() => (article.value?.document.blocks ?? []).filter((block) => typeof block.image_path === 'string' && block.image_path).map((block, index) => ({
  id: String(block.id || index),
  path: String(block.image_path),
  name: typeof block.image_name === 'string' ? block.image_name : `正文配图 ${index + 1}`,
  prompt: typeof block.image_prompt === 'string' ? block.image_prompt : '',
})))
const csdnBrowserEnabled = computed(() => platforms.value.find((platform) => platform.id === 'csdn')?.capabilities?.article?.publish === 'browser')
const csdnManualReason = computed(() => {
  if (csdnSettings.article_type !== 'original' || csdnSettings.source_url.trim()) return '转载和翻译来源设置需手动发布。'
  if (csdnSettings.category.trim() || csdnSettings.activity.trim() || csdnSettings.topic.trim()) return '分类专栏、活动或话题需手动发布。'
  if (csdnSettings.backup_gitcode || csdnSettings.cross_publish) return 'GitCode 备份或 CSDN 多平台发布需手动处理。'
  const body = article.value?.document.platform_overrides.csdn?.body_markdown || article.value?.document.body_markdown || ''
  if (bodyImages.value.length || /!\[[^\]]*\]\(\s*<?(?:data[:/]|\/api\/|file:|https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?\/)/i.test(body) || /<img\b[^>]*\bsrc\s*=\s*['"]?(?:data[:/]|\/api\/|file:|https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?\/)/i.test(body)) return '正文配图需在 CSDN 手动上传并发表。'
  return ''
})
const toutiaoManualReason = computed(() => {
  if (!activePlatform.value?.connected) return '今日头条账号当前未连接，系统将创建手动发布任务。'
  if (toutiaoOptionsError.value || !toutiaoAccountOptions.value) return '尚未读取当前头条号发表选项，自动发表暂不可用。'
  if (toutiaoSettings.declarations.some((value) => !toutiaoDeclarations.value.includes(value))) return '当前作品声明选项已变化，请刷新选项后重新核对。'
  if (toutiaoSettings.location.trim()) return '头条号地点搜索尚未接入，当前将转为手动发布。'
  if (toutiaoSettings.collection.trim()) return '头条号合集写入尚未接入，当前将转为手动发布。'
  const body = article.value?.document.platform_overrides.toutiao?.body_markdown || article.value?.document.body_markdown || ''
  if (bodyImages.value.length || /!\[[^\]]*\]\(/.test(body) || /<img\b/i.test(body)) return '头条号正文配图需在平台补充，当前将转为手动发布。'
  if (/^\s*\|.*\|\s*$/m.test(body)) return '头条号表格排版尚未核验，当前将转为手动发布。'
  return ''
})
const toutiaoCoverSelectionError = computed(() => {
  const paths = toutiaoCoverPaths()
  if (paths.some((path) => !path)) return toutiaoSettings.cover_mode === 'triple' ? '三图封面需要分别设置 3 张图片。' : '单图封面需要设置一张图片；也可以选择“无封面”。'
  if (new Set(paths).size !== paths.length) return '三图封面需要选择 3 张不同的图片。'
  return ''
})
const xhsNoteImages = computed(() => {
  const images: Array<{ key: string; name: string; url: string }> = []
  const seen = new Set<string>()
  const selectedCover = effectiveCover('xiaohongshu')
  const coverPath = selectedCover?.reference || ''
  if (coverPath) {
    seen.add(coverPath)
    images.push({ key: coverPath, name: article.value?.document.cover_image_name || '文章封面', url: selectedCover?.preview_url || '' })
  }
  for (const image of bodyImages.value) {
    if (seen.has(image.path)) continue
    seen.add(image.path)
    images.push({ key: image.path, name: image.name, url: api.articleImageUrl(articleId, image.path.split('/').pop() || '') })
  }
  return images
})
const targetOptions = computed<PublishTargetOption[]>(() => platforms.value.flatMap<PublishTargetOption>((platform): PublishTargetOption[] => {
  if (platform.id === 'wechat' || platform.id === 'zhihu') {
    return platform.accounts.filter((account) => account.configured).map((account) => ({
      key: targetKey(platform.id, account.id), platform_id: platform.id, account_id: account.id,
      account_name: account.name, platform_name: platform.name,
    }))
  }
  if (platform.id === 'xiaohongshu') {
    if (platform.capabilities?.image_note?.publish !== 'manual') return []
    return platform.accounts.filter((account) => account.configured).map((account) => ({
      key: targetKey(platform.id, account.id), platform_id: platform.id, account_id: account.id,
      account_name: account.name, platform_name: platform.name,
    }))
  }
  if (platform.id === 'weibo') {
    return platform.publish_ready ? [{
      key: targetKey(platform.id, null), platform_id: platform.id, account_id: null,
      account_name: platform.name || '微博账号', platform_name: platform.name,
    }] : []
  }
  if (platform.mode === 'manual') {
    return [{ key: targetKey(platform.id, null), platform_id: platform.id, account_id: null, account_name: null, platform_name: platform.name }]
  }
  return []
}))
const activePlatform = computed(() => platforms.value.find((platform) => platform.id === activePlatformId.value) || platforms.value[0] || null)
const activePlatformAccount = computed(() => activePlatform.value?.accounts.find((account) => account.configured) || activePlatform.value?.accounts[0] || null)
const activePlatformRequiresManual = computed(() => {
  if (!activePlatform.value) return false
  if (activePlatform.value.id === 'csdn') return !(csdnBrowserEnabled.value && !csdnManualReason.value)
  if (activePlatform.value.id === 'toutiao') return !(activePlatform.value.capabilities?.article?.publish === 'browser' && !toutiaoManualReason.value)
  return activePlatform.value.mode === 'manual'
})
watch(() => [activePlatformId.value, activePlatform.value?.connected] as const, ([platform, connected]) => {
  if (platform === 'toutiao' && connected) void loadToutiaoOptions()
})
const selectedWechatCount = computed(() => selected.value.filter((key) => key.startsWith('wechat:')).length)
const actionableSelection = computed(() => selected.value
  .map((key) => targetOptions.value.find((target) => target.key === key))
  .filter((target): target is PublishTargetOption => Boolean(target && canPublish(target.platform_id, target.account_id))))
const allSelected = computed(() => targetOptions.value.length > 0 && targetOptions.value.every((target) => selected.value.includes(target.key)))

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '发布页请求失败，请稍后重试。'
}

function publicationLabel(status: string | undefined): string {
  return ({ draft_saved: '已存入草稿箱', published: '已发布', publishing: '平台处理中', failed: '发布失败', unknown: '结果待核实', manual_required: '待手动发布', manual_published: '已发布' } as Record<string, string>)[status || ''] || '未发布'
}

function fanNotificationLabel(status: string | undefined): string {
  return ({
    not_requested: '未通知', waiting_publication: '等待发表成功', pending: '待提交群发',
    sending: '群发处理中', sent: '已通知粉丝', failed: '通知失败', unknown: '通知结果待核实',
  } as Record<string, string>)[status || ''] || '未通知'
}

function fanNotificationClass(status: string | undefined): string {
  if (status === 'sent') return 'is-published'
  if (status === 'failed') return 'is-failed'
  if (status === 'unknown') return 'is-unknown'
  return status && status !== 'not_requested' ? 'is-pending' : 'is-idle'
}

function fanNotificationTagType(status: string | undefined): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'sent') return 'success'
  if (status === 'failed') return 'danger'
  if (status && status !== 'not_requested') return 'warning'
  return 'info'
}

function platformSelectionCount(platformId: string): number {
  return selected.value.filter((key) => key.startsWith(platformId + ':')).length
}

function platformStatusText(platform: ArticlePlatformStatus): string {
  const selectedCount = platformSelectionCount(platform.id)
  if (selectedCount) return `已选 ${selectedCount}`
  if (platform.id === 'wechat' || platform.id === 'zhihu') {
    return targetOptions.value.some((target) => target.platform_id === platform.id) ? '可发布' : '未配置'
  }
  if (platform.id === 'csdn') {
    if (platform.connection_checking) return '检查中'
    if (platform.login_in_progress) return '登录窗口已打开'
    if (platform.connected) return csdnBrowserEnabled.value ? '已登录 · 浏览器发表' : '已登录 · 手动发布'
    return platform.connection_checked === false ? '登录状态异常' : '需登录 CSDN'
  }
  if (platform.id === 'xiaohongshu') {
    if (platform.capabilities?.image_note?.publish !== 'manual') return '需重启后端'
    return platform.accounts.some((account) => account.configured) ? '账号已连接' : '需连接视频账号'
  }
  if (platform.id === 'weibo') {
    if (platform.publish_ready) return '可发布'
    if (!platform.cli_installed) return '需安装 CLI'
    if (platform.connection_checking) return '检查中'
    if (!platform.connection_checked) return '连接状态待确认'
    if (!platform.connected) return '未登录'
    return platform.readiness_checked ? '已连接 · 需完成发布准备' : '发布准备状态待确认'
  }
  return platform.mode === 'manual' || platform.configured ? '可发布' : '未连接'
}

function platformReady(platform: ArticlePlatformStatus): boolean {
  if (platform.id === 'xiaohongshu') return platform.capabilities?.image_note?.publish === 'manual' && platform.accounts.some((account) => account.configured)
  if (platform.id === 'weibo') return platform.publish_ready === true
  return platform.configured || platform.mode === 'manual'
}

function hasPollablePublication(publication: ArticlePublication): boolean {
  return publication.status === 'publishing'
    || publication.fan_notification_status === 'pending'
    || publication.fan_notification_status === 'sending'
    || (publication.fan_notification_status === 'unknown' && Boolean(publication.fan_notification_remote_id))
}

function targetKey(platformId: string, accountId: string | null): string {
  return `${platformId}:${accountId || 'default'}`
}

function currentPublication(platformId: string, accountId: string | null = null): ArticlePublication | undefined {
  const publication = latestPublications.value[targetKey(platformId, accountId)]
  return publication?.article_version === article.value?.version ? publication : undefined
}

function canPublish(platformId: string, accountId: string | null = null): boolean {
  const status = currentPublication(platformId, accountId)?.status
  if (platformId === 'toutiao' && status === 'manual_required') return !toutiaoManualReason.value
  return !status || status === 'failed'
}

function publicationClass(status: string | undefined): string {
  if (status === 'published' || status === 'manual_published') return 'is-published'
  if (status === 'failed') return 'is-failed'
  if (status === 'manual_required' || status === 'draft_saved' || status === 'publishing' || status === 'unknown') return 'is-pending'
  return 'is-idle'
}

function publicationTagType(status: string | undefined): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'published' || status === 'manual_published') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'manual_required' || status === 'draft_saved' || status === 'publishing' || status === 'unknown') return 'warning'
  return 'info'
}

function accountLabel(platform: ArticlePlatformStatus): string {
  if (platform.id === 'csdn' && csdnBrowserEnabled.value) return platform.connected ? '已登录 · 浏览器发表' : '待登录 CSDN'
  if (platform.id === 'toutiao' && platform.capabilities?.article?.publish === 'browser') return platform.connected ? '已登录 · 浏览器发表' : '待登录今日头条'
  if (platform.mode === 'manual') return '手动发布'
  if (platform.id === 'wechat' && platform.connected) return '调用凭据已验证'
  if (platform.id === 'zhihu' && platform.configured) return '凭据已保存 · 权限待确认'
  if (platform.id === 'weibo') {
    if (platform.publish_ready) return `微博账号：${platform.name || '已连接'}`
    if (!platform.cli_installed) return '未安装官方 CLI'
    if (!platform.connection_checked) return '连接状态待确认'
    if (!platform.connected) return '待登录'
    return platform.readiness_checked ? '已连接 · 需完成发布准备' : '发布准备状态待确认'
  }
  return '未连接'
}

function accountClass(platform: ArticlePlatformStatus): string {
  return platform.connected ? 'is-connected' : platform.configured ? 'is-configured' : 'is-disconnected'
}

function usesVideoAccountSession(platformId: string, account: ArticlePlatformAccount): boolean {
  return account.auth_type === 'browser' || (platformId === 'xiaohongshu' && account.status !== undefined)
}

function articleAccountStatusState(platformId: string, account: ArticlePlatformAccount): 'checking' | 'connected' | 'verified' | 'saved' | 'error' | 'idle' {
  if (usesVideoAccountSession(platformId, account)) {
    if (pendingAccountPreflights.has(targetKey(platformId, account.id)) || account.status === 'checking' || account.status === 'login_in_progress') return 'checking'
    if (account.status === 'connected' || account.configured) return 'connected'
    if (account.status === 'expired' || account.status === 'error') return 'error'
    return 'idle'
  }
  if (account.credential_verified) return 'verified'
  return account.configured ? 'saved' : 'idle'
}

function articleAccountStatusLabel(platformId: string, account: ArticlePlatformAccount): string {
  const state = articleAccountStatusState(platformId, account)
  if (state === 'checking') return '正在检查'
  if (state === 'connected') return '已连接'
  if (state === 'verified') return '凭据已验证'
  if (state === 'saved') return '凭据已保存'
  if (state === 'error') return account.status === 'expired' ? '登录已失效' : '登录状态异常'
  return '未连接'
}

function isArticleAccountCheckPending(platformId: string, account: ArticlePlatformAccount): boolean {
  return usesVideoAccountSession(platformId, account)
    && (pendingAccountPreflights.has(targetKey(platformId, account.id)) || account.status === 'checking' || account.status === 'login_in_progress')
}

function hasRecentSuccessfulAccountCheck(account: ArticlePlatformAccount): boolean {
  if (account.status !== 'connected' || !account.credential_verified_at) return false
  if (/本次检查未完成|无法确认登录状态|检查账号登录状态超时/.test(account.message || '')) return false
  const checkedAt = Date.parse(account.credential_verified_at.replace(/(\.\d{3})\d+/, '$1'))
  const ageMs = Date.now() - checkedAt
  return Number.isFinite(checkedAt) && ageMs >= 0 && ageMs <= accountCheckCacheTtlMs
}

function videoAccountId(platformId: string, articleAccountId: string): string {
  return platformId === 'zhihu' && articleAccountId.startsWith('browser:')
    ? articleAccountId.slice('browser:'.length)
    : articleAccountId
}

function updateArticleAccountFromVideoStatus(platformId: string, articleAccountId: string, updated: VideoPublishAccount): void {
  videoAccountStatusRevision += 1
  platforms.value = platforms.value.map((platform) => {
    if (platform.id !== platformId) return platform
    const accounts = platform.accounts.map((account) => account.id === articleAccountId
      ? {
          ...account,
          name: updated.profile_name || updated.display_name || updated.account_alias || account.name,
          public_value: updated.account_alias || account.public_value,
          avatar_url: updated.avatar_url || account.avatar_url,
          credential_verified_at: updated.checked_at,
          credential_verified: updated.status === 'connected',
          configured: updated.status === 'connected',
          status: updated.status,
          message: updated.message,
          auth_type: 'browser' as const,
        }
      : account)
    const configured = accounts.some((account) => account.configured)
    const connected = accounts.some((account) => account.configured && (account.credential_verified || account.status === 'connected'))
    return { ...platform, accounts, configured, connected }
  })
}

function mergeVideoAccountStatuses(incoming: ArticlePlatformStatus[]): ArticlePlatformStatus[] {
  return incoming.map((platform) => {
    if (platform.id !== 'zhihu' && platform.id !== 'xiaohongshu') return platform
    const current = platforms.value.find((item) => item.id === platform.id)
    if (!current) return platform
    const currentAccounts = new Map(current.accounts.map((account) => [account.id, account] as const))
    const accounts = platform.accounts.map((account) => {
      if (!usesVideoAccountSession(platform.id, account)) return account
      return currentAccounts.get(account.id) || account
    })
    const configured = accounts.some((account) => account.configured)
    const connected = accounts.some((account) => account.configured && (account.credential_verified || account.status === 'connected'))
    return { ...platform, accounts, configured, connected }
  })
}

function settleAccountPreflights(): void {
  for (const key of [...pendingAccountPreflights]) {
    const separator = key.indexOf(':')
    const platformId = key.slice(0, separator)
    const accountId = key.slice(separator + 1)
    const platform = platforms.value.find((item) => item.id === platformId)
    const account = platform?.accounts.find((item) => item.id === accountId)
    if (!account) {
      pendingAccountPreflights.delete(key)
      continue
    }
    if (account.status === 'checking' || account.status === 'login_in_progress') continue

    pendingAccountPreflights.delete(key)
    if (account.status !== 'connected' || !account.configured) continue
    if (!selected.value.includes(key) && canPublish(platformId, accountId)) {
      selected.value = [...selected.value, key]
      message.value = `${account.name}登录状态有效，已加入所选账号。`
      error.value = ''
    }
  }
}

function reconcileVideoAccountSelections(): void {
  selected.value = selected.value.filter((key) => {
    const separator = key.indexOf(':')
    const platformId = key.slice(0, separator)
    const accountId = key.slice(separator + 1)
    const platform = platforms.value.find((item) => item.id === platformId)
    const account = platform?.accounts.find((item) => item.id === accountId)
    if (!account || !usesVideoAccountSession(platformId, account)) return true
    return account.status === 'connected' || account.status === 'checking' || account.status === 'login_in_progress'
  })
}

function accountStatusNeedsPolling(): boolean {
  return pendingAccountPreflights.size > 0 || platforms.value.some((platform) =>
    (platform.id === 'zhihu' || platform.id === 'xiaohongshu')
    && platform.accounts.some((account) => usesVideoAccountSession(platform.id, account) && (account.status === 'checking' || account.status === 'login_in_progress')))
}

function startAccountCheckPolling(): void {
  if (!pageActive || accountCheckPollTimer !== undefined) return
  accountCheckPollTimer = window.setInterval(() => { void refreshArticleAccountStatuses() }, 3000)
}

function stopAccountCheckPolling(): void {
  if (accountCheckPollTimer !== undefined) window.clearInterval(accountCheckPollTimer)
  accountCheckPollTimer = undefined
}

function syncAccountCheckPolling(): void {
  if (!pageActive) return
  if (accountStatusNeedsPolling()) startAccountCheckPolling()
  else stopAccountCheckPolling()
}

async function refreshArticleAccountStatuses(): Promise<void> {
  if (refreshingAccountStatuses || !pageActive) return
  const requestId = pageRequestId
  const accountRevisionAtRequest = videoAccountStatusRevision
  refreshingAccountStatuses = true
  try {
    const result = await api.getArticlePlatforms(false)
    if (!pageActive || requestId !== pageRequestId) return
    videoAccountStatusRevision += 1
    const latest = accountRevisionAtRequest === videoAccountStatusRevision - 1
      ? result.platforms
      : mergeVideoAccountStatuses(result.platforms)
    const incomingById = new Map(latest.map((platform) => [platform.id, platform] as const))
    platforms.value = platforms.value.map((platform) =>
      platform.id === 'zhihu' || platform.id === 'xiaohongshu' ? incomingById.get(platform.id) || platform : platform)
    settleAccountPreflights()
    reconcileVideoAccountSelections()
  } catch (cause) {
    if (pageActive && requestId === pageRequestId) error.value = errorMessage(cause)
  } finally {
    refreshingAccountStatuses = false
    syncAccountCheckPolling()
  }
}

async function handleArticleAccountTargetChange(platformId: string, account: ArticlePlatformAccount, checked: boolean): Promise<void> {
  const key = targetKey(platformId, account.id)
  if (selected.value.includes(key)) {
    toggleTarget(key)
    return
  }
  if (!checked || publishing.value || !canPublish(platformId, account.id)) return
  if (!usesVideoAccountSession(platformId, account)) {
    selected.value = [...selected.value, key]
    return
  }
  if (account.status !== 'connected' || !account.configured || pendingAccountPreflights.has(key)) return
  if (hasRecentSuccessfulAccountCheck(account)) {
    selected.value = [...selected.value, key]
    message.value = `${account.name}的登录状态最近已确认正常，已直接选中。`
    error.value = ''
    return
  }

  pendingAccountPreflights.add(key)
  error.value = ''
  message.value = ''
  try {
    const updated = await api.checkVideoPublishAccount(videoAccountId(platformId, account.id))
    updateArticleAccountFromVideoStatus(platformId, account.id, updated)
    settleAccountPreflights()
    syncAccountCheckPolling()
  } catch (cause) {
    pendingAccountPreflights.delete(key)
    error.value = errorMessage(cause)
    await refreshArticleAccountStatuses()
  }
}

function toggleTarget(key: string): void {
  selected.value = selected.value.includes(key)
    ? selected.value.filter((item) => item !== key)
    : [...selected.value, key]
}

function toggleAll(): void {
  if (allSelected.value) {
    selected.value = []
    pendingAccountPreflights.clear()
    syncAccountCheckPolling()
    return
  }
  const nextSelection: string[] = []
  for (const target of targetOptions.value) {
    if (!canPublish(target.platform_id, target.account_id)) continue
    const account = target.account_id
      ? platforms.value.find((platform) => platform.id === target.platform_id)?.accounts.find((item) => item.id === target.account_id)
      : undefined
    if (account && usesVideoAccountSession(target.platform_id, account)) {
      void handleArticleAccountTargetChange(target.platform_id, account, true)
    } else nextSelection.push(target.key)
  }
  selected.value = [...new Set([...selected.value, ...nextSelection])]
}

async function loadPage(): Promise<void> {
  pageActive = true
  const requestId = ++pageRequestId
  const requestedArticleId = articleId
  const focusPlatformId = typeof route.query.focusPlatform === 'string' ? route.query.focusPlatform : ''
  loading.value = !article.value || loadedArticleId !== requestedArticleId
  error.value = ''
  notifyFans.value = false
  const [articleResult, platformResult] = await Promise.allSettled([
    api.getArticle(requestedArticleId),
    api.getArticlePlatforms(false),
  ])
  if (!pageActive || requestId !== pageRequestId) return
  if (articleResult.status === 'fulfilled') {
    article.value = articleResult.value
    loadedArticleId = requestedArticleId
    restoreCoverChoices()
    for (const publication of article.value.publications) {
      if (publication.article_version !== article.value.version || publication.status !== 'manual_required') continue
      const reference = publication.options.cover_image_path
      if (typeof reference !== 'string') continue
      if (reference === article.value.document.cover_image_path) {
        delete platformCovers[publication.platform_id]
        delete platformCoverRatios[publication.platform_id]
        continue
      }
      const prefix = `data/article-assets/${articleId}/`
      const match = reference.startsWith(prefix) ? /^([0-9a-f]{32})\.(?:jpe?g|png|webp)$/.exec(reference.slice(prefix.length)) : null
      if (match) platformCovers[publication.platform_id] = { reference, preview_url: `/api/v1/articles/${articleId}/publish-covers/${match[1]}` }
    }
    csdnSettings.summary = (article.value.document.platform_overrides.csdn?.summary || article.value.document.summary || '').slice(0, 256)
    const savedCsdn = article.value.publications.find((publication) => publication.platform_id === 'csdn'
      && publication.article_version === article.value?.version && publication.status === 'manual_required')
    if (Array.isArray(savedCsdn?.options?.tags)) csdnTags.value = savedCsdn.options.tags.filter((tag): tag is string => typeof tag === 'string')
    const storedCsdn = savedCsdn?.options?.csdn
    if (storedCsdn && typeof storedCsdn === 'object' && !Array.isArray(storedCsdn)) Object.assign(csdnSettings, storedCsdn)
    for (const platformId of ['wechat', 'zhihu']) {
      const override = article.value.document.platform_overrides[platformId]
      publishOptions[platformId] = {
        title: override?.title || article.value.document.title,
        summary: override?.summary || article.value.document.summary || '',
        author: platformId === 'wechat' ? article.value.document.author || '' : '',
        note: '',
      }
    }
    const toutiaoOverride = article.value.document.platform_overrides.toutiao
    publishOptions.toutiao.title = toutiaoOverride?.title || article.value.document.title
    const xhsOverride = article.value.document.platform_overrides.xiaohongshu
    publishOptions.xiaohongshu.title = (xhsOverride?.title || article.value.document.title).slice(0, 20)
    publishOptions.xiaohongshu.note = flattenXhsNote(
      [xhsOverride?.body_markdown || article.value.document.body_markdown, article.value.document.outro].filter(Boolean).join('\n\n'),
    ).slice(0, 1000)
    const focusedAccountId = typeof route.query.focusAccount === 'string' ? route.query.focusAccount : ''
    const manualXhs = article.value.publications.find((publication) => publication.platform_id === 'xiaohongshu'
      && publication.account_id === focusedAccountId && publication.article_version === article.value?.version
      && publication.status === 'manual_required')
    if (typeof manualXhs?.options?.title === 'string') publishOptions.xiaohongshu.title = manualXhs.options.title
    if (typeof manualXhs?.options?.note === 'string') publishOptions.xiaohongshu.note = manualXhs.options.note
  }
  else error.value = errorMessage(articleResult.reason)
  if (platformResult.status === 'fulfilled') {
    platforms.value = platformResult.value.platforms
    if (focusPlatformId && platforms.value.some((platform) => platform.id === focusPlatformId)) {
      activePlatformId.value = focusPlatformId
    } else if (!platforms.value.some((platform) => platform.id === activePlatformId.value)) {
      activePlatformId.value = platforms.value[0]?.id || ''
    }
    const defaultPlatforms = new Set<string>()
    selected.value = targetOptions.value.filter((target) => {
      if (!['wechat', 'zhihu'].includes(target.platform_id) || defaultPlatforms.has(target.platform_id) || !canPublish(target.platform_id, target.account_id)) return false
      const account = target.account_id
        ? platforms.value.find((platform) => platform.id === target.platform_id)?.accounts.find((item) => item.id === target.account_id)
        : undefined
      if (account && usesVideoAccountSession(target.platform_id, account)) return false
      defaultPlatforms.add(target.platform_id)
      return true
    }).map((target) => target.key)

    const retryPublicationId = typeof route.query.retryPublication === 'string' ? route.query.retryPublication : ''
    if (retryPublicationId) selected.value = []
    const retryPublication = article.value?.publications.find((publication) => (
      publication.id === retryPublicationId
      && publication.status === 'failed'
      && publication.article_version === article.value?.version
    ))
    if (retryPublication) {
      const retryTarget = targetOptions.value.find((target) => (
        target.platform_id === retryPublication.platform_id
        && target.account_id === retryPublication.account_id
      ))
      if (retryTarget && currentPublication(retryTarget.platform_id, retryTarget.account_id)?.id === retryPublication.id) {
        const account = retryTarget.account_id
          ? platforms.value.find((platform) => platform.id === retryTarget.platform_id)?.accounts.find((item) => item.id === retryTarget.account_id)
          : undefined
        if (account && usesVideoAccountSession(retryTarget.platform_id, account)) {
          void handleArticleAccountTargetChange(retryTarget.platform_id, account, true)
        } else selected.value = [retryTarget.key]
      }
    }
  } else if (!error.value) error.value = errorMessage(platformResult.reason)
  loading.value = false
  const focusAccountId = typeof route.query.focusAccount === 'string' ? route.query.focusAccount : ''
  if (focusPlatformId && focusAccountId) {
    const accountElementId = 'article-account-' + focusPlatformId + '-' + focusAccountId
    window.requestAnimationFrame(() => {
      if (pageActive) document.getElementById(accountElementId)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  }
  startPublicationPolling()
  settleAccountPreflights()
  syncAccountCheckPolling()
  if (platformResult.status === 'fulfilled') void refreshLivePlatformStatus(requestId)
}

async function refreshLivePlatformStatus(requestId: number): Promise<void> {
  const statusRequestId = ++platformStatusRequestId
  const accountRevisionAtRequest = videoAccountStatusRevision
  try {
    const result = await api.getArticlePlatforms()
    if (!pageActive || requestId !== pageRequestId || statusRequestId !== platformStatusRequestId) return
    platforms.value = accountRevisionAtRequest === videoAccountStatusRevision
      ? result.platforms
      : mergeVideoAccountStatuses(result.platforms)
    if (!platforms.value.some((platform) => platform.id === activePlatformId.value)) {
      activePlatformId.value = platforms.value[0]?.id || ''
    }
    settleAccountPreflights()
    reconcileVideoAccountSelections()
    syncAccountCheckPolling()
  } catch (cause) {
    if (pageActive && requestId === pageRequestId && statusRequestId === platformStatusRequestId) {
      error.value = errorMessage(cause)
    }
  }
}

async function confirmAction(prompt: string, title: string, confirmButtonText: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(prompt, title, {
      confirmButtonText,
      cancelButtonText: '取消',
      type: 'warning',
      customClass: 'article-publish-confirm',
      customStyle: { width: 'min(540px, calc(100vw - 32px))' },
      closeOnClickModal: false,
    })
    return true
  } catch {
    return false
  }
}

async function publishSelected(): Promise<void> {
  const targets = actionableSelection.value
  if (!article.value || !targets.length || publishing.value) {
    if (selected.value.length && !targets.length) message.value = '所选平台的当前文章版本已处理，没有重复发送。'
    return
  }
  if (article.value.status !== 'finalized') {
    error.value = '请先在编辑页保存并设为定稿，再进行发布。'
    return
  }
  if (targets.some((target) => !(publishOptions[target.platform_id]?.title ?? platformDocument(target.platform_id).title).trim())) {
    error.value = '请为每个平台填写发布标题。'
    return
  }
  if (targets.some((target) => target.platform_id === 'csdn') && !csdnTags.value.length) {
    activePlatformId.value = 'csdn'
    error.value = '请选择至少一个 CSDN 文章标签。'
    return
  }
  if (targets.some((target) => target.platform_id === 'csdn') && (!csdnTagCatalog.value || csdnTags.value.length > csdnTagCatalog.value.max_tags)) {
    activePlatformId.value = 'csdn'
    error.value = '请先读取 CSDN 标签，并按平台当前数量上限选择。'
    return
  }
  if (targets.some((target) => target.platform_id === 'toutiao')) {
    const title = publishOptions.toutiao.title.trim()
    if (title.length < 2 || title.length > 30) {
      activePlatformId.value = 'toutiao'
      error.value = '今日头条文章标题须为 2 到 30 个字符。'
      return
    }
    if (toutiaoCoverSelectionError.value) {
      activePlatformId.value = 'toutiao'
      error.value = toutiaoCoverSelectionError.value
      return
    }
  }
  if (targets.some((target) => target.platform_id === 'csdn') && (!csdnDeclarationOptions.value.length || !csdnDeclarationOptions.value.includes(csdnSettings.declaration))) {
    activePlatformId.value = 'csdn'
    error.value = '请重新读取 CSDN 创作声明，并选择平台当前提供的选项。'
    return
  }
  if (targets.some((target) => target.platform_id === 'xiaohongshu' && !publishOptions.xiaohongshu.note.trim())) {
    error.value = '请填写小红书笔记正文。'
    return
  }
  if (targets.some((target) => target.platform_id === 'xiaohongshu' && xhsNoteImages.value.length === 0)) {
    error.value = '小红书图文至少需要一张图片，请先在文章编辑页上传封面或正文配图。'
    return
  }
  if (targets.some((target) => target.platform_id === 'xiaohongshu' && xhsNoteImages.value.length > 18)) {
    error.value = '小红书图文最多使用 18 张图片，请先减少封面或正文配图。'
    return
  }
  const selectedNames = targets.map((target) => `${target.platform_name}${target.account_name ? ` · ${target.account_name}` : ''}`).join('、')
  const targetPlatformIds = new Set(targets.map((target) => target.platform_id))
  const hasWeibo = targetPlatformIds.has('weibo')
  const hasWechat = targetPlatformIds.has('wechat')
  const hasZhihu = targetPlatformIds.has('zhihu')
  const hasXiaohongshu = targetPlatformIds.has('xiaohongshu')
  const hasCsdn = targetPlatformIds.has('csdn')
  const hasToutiao = targetPlatformIds.has('toutiao')
  const previousToutiaoManual = hasToutiao ? currentPublication('toutiao') : undefined
  const hasGeneratedImages = bodyImages.value.length > 0
  const hasAnyImage = hasGeneratedImages || hasCoverImage.value
  const fanNotificationTargetCount = hasWechat && notifyFans.value
    ? targets.filter((target) => target.platform_id === 'wechat').length
    : 0
  const csdnNotice = hasCsdn ? (csdnBrowserEnabled.value && !csdnManualReason.value ? '\nCSDN 将按所选可见范围通过浏览器提交发表。' : `\nCSDN 将创建手动发布任务。${csdnManualReason.value || ''}`) : ''
  const toutiaoRetryNotice = previousToutiaoManual?.status === 'manual_required' && !toutiaoManualReason.value
    ? '\n这篇文章此前没有通过工作台提交到头条号。请先确认头条号内容管理中没有这篇文章；如果已手动发布，请取消。本次确认会新建一次自动提交。'
    : ''
  const toutiaoNotice = hasToutiao ? (toutiaoManualReason.value ? `\n今日头条将创建手动发布任务。${toutiaoManualReason.value}` : `\n今日头条将使用当前已连接账号最终发表；广告、首发、赞赏、微头条和作品声明按本页设置逐项提交。${toutiaoRetryNotice}`) : ''
  const prompt = `确认将「${currentTitle.value}」发送到：${selectedNames}？${csdnNotice}${toutiaoNotice}${hasWeibo ? '\n微博接口可能消耗积分，并会标注 AI 生成内容。' : ''}${hasWechat && hasAnyImage ? '\n公众号会自动上传封面和正文配图，并提交平台发表；审核期间会显示“平台处理中”。' : ''}${fanNotificationTargetCount ? `\n同时将向所选的 ${fanNotificationTargetCount} 个公众号的全体粉丝群发文章；这会真实发送消息，并可能需要管理员在微信后台确认。` : ''}${hasZhihu && hasAnyImage ? '\n知乎会自动上传正文配图；封面图会作为正文首图嵌入（知乎文章接口不提供独立封面或分类字段）。' : ''}${hasXiaohongshu ? `\n小红书仅创建手动发布任务；请自行使用 ${xhsNoteImages.value.length} 张图片和笔记文案在创作中心发布。` : ''}${hasWeibo && hasAnyImage ? '\n当前工作台无法通过微博官方 CLI 自动上传配图；发布后需在微博端手动补充。图片可在本页下载。' : ''}\n若平台结果待核实，请先到后台检查后再处理，避免重复发布。`
  if (!(await confirmAction(prompt, '确认发表文章', '确认发表'))) return
  publishing.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api.publishArticle(articleId, targets.map(({ platform_id, account_id }) => ({
      platform_id,
      account_id,
      options: {
        title: (publishOptions[platform_id]?.title ?? platformDocument(platform_id).title).trim(),
        ...(platform_id !== 'toutiao' && effectiveCover(platform_id) ? { cover_image_path: effectiveCover(platform_id)?.reference } : {}),
        ...(platform_id === 'csdn' ? { tags: [...csdnTags.value], csdn: { ...csdnSettings } } : {}),
        ...(platform_id === 'xiaohongshu' ? { note: publishOptions.xiaohongshu.note.trim() } : {}),
        ...(platform_id === 'wechat' ? {
          summary: publishOptions[platform_id].summary.trim(),
          author: publishOptions[platform_id].author.trim(),
          notify_fans: notifyFans.value,
        } : {}),
        ...(platform_id === 'toutiao' ? {
          toutiao: { ...toutiaoSettings, cover_image_paths: toutiaoCoverPaths(), account_options_verified: toutiaoOptionsVerified.value },
          ...(previousToutiaoManual?.status === 'manual_required' && !toutiaoManualReason.value ? { retry_publication_id: previousToutiaoManual.id } : {}),
        } : {}),
      },
    })), true)
    article.value = result.article
    notifyFans.value = false
    startPublicationPolling()
    await router.push({
      name: 'article-publish-task',
      params: { articleId },
      query: { publications: result.results.map((publication) => publication.id).join(',') },
    })
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    publishing.value = false
  }
}

function flattenXhsNote(markdown: string): string {
  return markdown
    .replace(/```[^\n]*\n([\s\S]*?)```/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*(?:[-*+]\s+|\d+[.)]\s+)/gm, '')
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/gm, '')
    .replace(/^\s*\|/gm, '')
    .replace(/\|\s*$/gm, '')
    .replace(/\|/g, '  ')
    .replace(/[*_~`]+/g, '')
    .replace(/<[^>]+>/g, '')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function platformDocument(platformId: string): { title: string; summary: string; body_markdown: string } {
  const document = article.value?.document
  const outro = document?.outro?.trim() || ''
  const body = document?.body_markdown || ''
  const override = document?.platform_overrides[platformId]
  const selectedBody = override?.body_markdown || body
  const finalBody = outro && !selectedBody.trimEnd().endsWith(outro) ? `${selectedBody.trimEnd()}\n\n${outro}` : selectedBody
  return {
    title: override?.title || currentTitle.value,
    summary: override?.summary || document?.summary || '',
    body_markdown: finalBody,
  }
}

async function copyManual(platformId: string): Promise<void> {
  const content = platformDocument(platformId)
  const plain = [content.title, content.summary, content.body_markdown].filter(Boolean).join('\n\n')
  try {
    await navigator.clipboard.writeText(plain)
    message.value = `已复制 ${platformNames[platformId]} 格式的文章。`
  } catch {
    error.value = '复制失败；请检查浏览器剪贴板权限。'
  }
}

async function copyXhsText(value: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    message.value = `已复制小红书${label}。`
  } catch {
    error.value = '复制失败；请检查浏览器剪贴板权限。'
  }
}

async function copyCsdnSettings(): Promise<void> {
  const articleTypes = { original: '原创', reprint: '转载', translation: '翻译' }
  const visibilities = { public: '全部可见', private: '仅我可见', followers: '粉丝可见', vip: 'VIP 可见' }
  const templates = { default: '默认模板', simple: '简洁模板', ink: '墨衍模板' }
  const content = [
    `标签：${csdnTags.value.join('、') || '未选择'}`,
    `摘要：${csdnSettings.summary || '未填写'}`,
    `文章类型：${articleTypes[csdnSettings.article_type]}`,
    ...(csdnSettings.source_url ? [`来源：${csdnSettings.source_url}`] : []),
    `可见范围：${visibilities[csdnSettings.visibility]}`,
    `文章模板：${templates[csdnSettings.template]}`,
    `创作声明：${csdnSettings.declaration || '未选择'}`,
    `分类专栏：${csdnSettings.category || '未选择'}`,
    `活动：${csdnSettings.activity || '未选择'}`,
    `话题：${csdnSettings.topic || '未选择'}`,
    `备份到 GitCode：${csdnSettings.backup_gitcode ? '是' : '否'}`,
    `CSDN 多平台发布：${csdnSettings.cross_publish ? '是' : '否'}`,
  ].join('\n')
  try { await navigator.clipboard.writeText(content); message.value = '已复制 CSDN 发表设置，请在平台表单中核对。' }
  catch { error.value = '复制失败；请检查浏览器剪贴板权限。' }
}

async function openPlatform(platform: ArticlePlatformStatus): Promise<void> {
  if (platform.id === 'csdn' || platform.id === 'toutiao') {
    if (busyManual.value) return
    const action = platform.id === 'csdn' ? 'open-csdn-editor' : 'open-toutiao-editor'
    busyManual.value = action
    error.value = ''
    message.value = ''
    try {
      const status = platform.id === 'csdn' ? await api.openCsdnEditor() : await api.openToutiaoEditor()
      message.value = status.message
      platforms.value = (await api.getArticlePlatforms()).platforms
    } catch (cause) {
      error.value = errorMessage(cause)
    } finally {
      busyManual.value = ''
    }
    return
  }
  if (!platform.manual_url) return
  window.open(platform.manual_url, '_blank', 'noopener,noreferrer')
}

function openPlatformLabel(platform: ArticlePlatformStatus): string {
  if (platform.id === 'toutiao') {
    if (busyManual.value === 'open-toutiao-editor') return '正在打开…'
    return platform.connected ? '打开头条号创作页' : '登录并打开头条号创作页'
  }
  if (platform.id !== 'csdn') return '打开创作平台 ↗'
  if (busyManual.value === 'open-csdn-editor') return '正在打开…'
  return platform.connected ? '打开 CSDN 编辑器' : '登录并打开 CSDN 编辑器'
}

async function retryFanNotification(publication: ArticlePublication): Promise<void> {
  if (!article.value || publishing.value || publication.fan_notification_remote_id) return
  const accountName = publication.account_name || '当前公众号'
  if (!(await confirmAction(`确认重新向「${accountName}」的全体粉丝发送《${currentTitle.value}》？微信之前明确拒绝且未返回群发消息 ID；本次会重新提交一次群发任务。`, '重新发送粉丝通知', '重新发送'))) return
  publishing.value = true
  error.value = ''
  try {
    const updated = await api.retryArticleFanNotification(articleId, publication.id, true)
    article.value = {
      ...article.value,
      publications: article.value.publications.map((item) => item.id === updated.id ? updated : item),
    }
    message.value = '已重新提交粉丝通知，正在查询微信发送结果。'
    startPublicationPolling()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    publishing.value = false
  }
}

async function markManualPublished(platformId: string): Promise<void> {
  if (busyManual.value) return
  if (!(await confirmAction(`确认「${currentTitle.value}」已经在${platformNames[platformId]}发布成功？`, '确认手动发布结果', '确认已发布'))) return
  busyManual.value = platformId
  error.value = ''
  message.value = ''
  try {
    const result = await api.confirmManualArticlePublication(articleId, platformId, true)
    article.value = { ...article.value!, publications: [result, ...article.value!.publications] }
    message.value = `${platformNames[platformId]}已记录为手动发布。`
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busyManual.value = ''
  }
}

function stopPublicationPolling(): void {
  if (publicationPollTimer !== undefined) window.clearInterval(publicationPollTimer)
  publicationPollTimer = undefined
}

async function refreshPendingPublications(): Promise<void> {
  if (refreshingPublications || !article.value) return
  const pending = article.value.publications.filter(hasPollablePublication)
  if (!pending.length) {
    stopPublicationPolling()
    return
  }
  refreshingPublications = true
  try {
    for (const publication of pending) {
      const updated = await api.refreshArticlePublication(articleId, publication.id)
      if (!article.value) return
      article.value = {
        ...article.value,
        publications: article.value.publications.map((item) => item.id === updated.id ? updated : item),
      }
      if (updated.status === 'published' && updated.fan_notification_status === 'sent') {
        message.value = `${updated.account_name || '公众号'}已发表并通知粉丝。`
      } else if (updated.status === 'published') {
        message.value = `${updated.account_name || '公众号'}已确认发表成功。`
      }
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    refreshingPublications = false
    if (!article.value?.publications.some(hasPollablePublication)) stopPublicationPolling()
  }
}

async function refreshUnknownPublication(publication: ArticlePublication): Promise<void> {
  if (refreshingPublicationId.value || !article.value) return
  refreshingPublicationId.value = publication.id
  error.value = ''
  try {
    const updated = await api.refreshArticlePublication(articleId, publication.id)
    if (!article.value) return
    article.value = {
      ...article.value,
      publications: article.value.publications.map((item) => item.id === updated.id ? updated : item),
    }
    message.value = updated.fan_notification_status === 'sent'
      ? (updated.account_name || '公众号') + '已发表并通知粉丝。'
      : updated.status === 'published'
        ? (updated.account_name || '公众号') + '已确认发表成功。'
        : '已重新查询公众号任务状态。'
    if (hasPollablePublication(updated)) startPublicationPolling()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    refreshingPublicationId.value = ''
  }
}

function refreshAccountPublication(platformId: string, accountId: string): void {
  const publication = currentPublication(platformId, accountId)
  if (publication) void refreshUnknownPublication(publication)
}

function retryAccountNotification(platformId: string, accountId: string): void {
  const publication = currentPublication(platformId, accountId)
  if (publication) void retryFanNotification(publication)
}

async function confirmXhsAccountPublication(accountId: string, outcome: 'published' | 'not_published'): Promise<void> {
  if (!article.value || refreshingPublicationId.value) return
  const publication = currentPublication('xiaohongshu', accountId)
  if (!publication || !['unknown', 'manual_required'].includes(publication.status)) return
  const prompt = outcome === 'published'
    ? '请先确认已在小红书创作中心看到这条笔记。确认后工作台会记录为已发布。'
    : '请先确认小红书创作中心没有这条笔记。确认后工作台会允许重新提交。'
  if (!(await confirmAction(prompt, '确认小红书发布结果', outcome === 'published' ? '确认已发布' : '确认未发布'))) return
  refreshingPublicationId.value = publication.id
  error.value = ''
  try {
    const updated = await api.confirmXiaohongshuArticlePublication(articleId, publication.id, outcome)
    article.value = {
      ...article.value,
      publications: article.value.publications.map((item) => item.id === updated.id ? updated : item),
    }
    message.value = updated.message
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    refreshingPublicationId.value = ''
  }
}

async function confirmCsdnPublication(outcome: 'published' | 'not_published'): Promise<void> {
  if (!article.value || refreshingPublicationId.value) return
  const publication = currentPublication('csdn')
  if (!publication || publication.status !== 'unknown') return
  const prompt = outcome === 'published'
    ? '请先在 CSDN 内容管理的已发布列表确认这篇文章。确认后工作台会记录为已发布。'
    : '请先在 CSDN 内容管理中确认这篇文章未发布。确认后工作台才会允许重新提交。'
  if (!(await confirmAction(prompt, '确认 CSDN 发布结果', outcome === 'published' ? '确认已发布' : '确认未发布'))) return
  refreshingPublicationId.value = publication.id
  error.value = ''
  try {
    const updated = await api.confirmCsdnArticlePublication(articleId, publication.id, outcome)
    article.value = { ...article.value, publications: article.value.publications.map((item) => item.id === updated.id ? updated : item) }
    message.value = updated.message
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    refreshingPublicationId.value = ''
  }
}

async function confirmToutiaoPublication(outcome: 'published' | 'not_published'): Promise<void> {
  if (!article.value || refreshingPublicationId.value) return
  const publication = currentPublication('toutiao')
  if (!publication || publication.status !== 'unknown') return
  const prompt = outcome === 'published'
    ? '请先在今日头条内容管理中确认这篇文章已提交发表。确认后工作台会记录结果。'
    : '请先在今日头条内容管理中确认这篇文章没有发表。确认后工作台才会允许重新提交。'
  if (!(await confirmAction(prompt, '确认今日头条发布结果', outcome === 'published' ? '确认已发表' : '确认未发表'))) return
  refreshingPublicationId.value = publication.id
  error.value = ''
  try {
    const updated = await api.confirmToutiaoArticlePublication(articleId, publication.id, outcome)
    article.value = { ...article.value, publications: article.value.publications.map((item) => item.id === updated.id ? updated : item) }
    message.value = updated.message
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    refreshingPublicationId.value = ''
  }
}

function startPublicationPolling(): void {
  const hasPending = article.value?.publications.some(hasPollablePublication)
  if (!hasPending) {
    stopPublicationPolling()
    return
  }
  if (publicationPollTimer === undefined) publicationPollTimer = window.setInterval(() => { void refreshPendingPublications() }, 5000)
}

onActivated(() => { void loadPage() })
onDeactivated(() => {
  pageActive = false
  pageRequestId += 1
  platformStatusRequestId += 1
  stopPublicationPolling()
  stopAccountCheckPolling()
})
onBeforeUnmount(() => {
  pageActive = false
  pageRequestId += 1
  platformStatusRequestId += 1
  stopPublicationPolling()
  stopAccountCheckPolling()
})
</script>

<template>
  <section class="article-publish-page" aria-label="发布文章">
    <div class="article-publish-toolbar">
      <div><RouterLink class="article-back-link" to="/articles">← 返回文章管理</RouterLink><h1>发布文章</h1></div>
      <RouterLink v-if="article" v-slot="{ navigate }" :to="{ name: 'article-edit', params: { articleId } }">
        <ElButton class="button button-secondary" native-type="button" @click="navigate">返回编辑</ElButton>
      </RouterLink>
    </div>

    <ElAlert v-if="error" class="info-box form-error" type="error" :closable="false" :show-icon="false" role="alert" :title="error" />
    <ElAlert v-if="message" class="info-box task-success" type="success" :closable="false" :show-icon="false" role="status" :title="message" />
    <ElAlert v-if="loading" class="panel article-publish-loading" type="info" :closable="false" :show-icon="false" role="status" title="正在读取文章内容…" />
    <template v-else-if="article">
      <ElAlert v-if="article.status !== 'finalized'" class="info-box article-finalize-reminder" type="warning" :closable="false" :show-icon="false" role="status">
        <span>发布前请先确认最终内容，并将文章设为定稿。</span>
        <RouterLink v-slot="{ navigate }" :to="{ name: 'article-edit', params: { articleId } }">
          <ElButton class="button button-small" native-type="button" @click="navigate">去编辑并定稿</ElButton>
        </RouterLink>
      </ElAlert>

      <div class="article-publish-workspace">
        <section class="article-publish-article" aria-labelledby="publish-article-heading">
          <header class="workspace-panel-heading">
            <div><span class="article-summary-kicker">{{ article.status === 'finalized' ? '定稿文章' : '尚未定稿' }} · v{{ article.version }}</span><h2 id="publish-article-heading">文章内容</h2></div>
            <ElTag class="readonly-tag" type="info" effect="light" size="small">只读预览</ElTag>
          </header>
          <div class="article-preview-scroll">
            <div class="article-preview-heading">
              <h3>{{ currentTitle }}</h3>
              <p>{{ currentSummary }}</p>
              <img v-if="hasCoverImage" class="article-preview-cover" :src="api.articleCoverUrl(articleId) + '?v=' + article.version" alt="文章封面" />
            </div>
            <section class="settings-section article-common-cover" aria-label="通用封面设置">
              <div class="settings-section-heading"><h3>通用封面裁剪</h3><span>用于各平台继承</span></div>
              <label class="form-label" for="article-common-ratio">裁剪比例</label>
              <ElSelect id="article-common-ratio" v-model="coverRatio" class="input" aria-label="通用封面裁剪比例">
                <ElOption v-for="(label, value) in coverRatioNames" :key="value" :label="label" :value="value" />
              </ElSelect>
              <img v-if="commonCovers[coverRatio]" class="article-preview-cover" :src="commonCovers[coverRatio].preview_url" :alt="`${coverRatioNames[coverRatio]} 通用封面`" />
              <p class="platform-help-note">{{ commonCovers[coverRatio] ? `已设置 ${coverRatioNames[coverRatio]} 通用封面。` : '可从当前文章封面裁剪，或选择新图片。' }}</p>
              <ElButton class="button button-secondary button-small" native-type="button" @click="openPublishCoverEditor('common')">{{ commonCovers[coverRatio] ? '重新裁剪' : '设置此比例' }}</ElButton>
            </section>
            <MarkdownPreview :content="article.document.body_markdown || '文章正文为空。'" />
            <div v-if="bodyImages.length" class="article-preview-images">
              <div class="preview-subheading">正文配图 · {{ bodyImages.length }} 张</div>
              <div class="article-preview-image-grid">
                <figure v-for="(image, index) in bodyImages" :key="image.id">
                  <img :src="api.articleImageUrl(articleId, image.path.split('/').pop() || '')" :alt="image.name" />
                  <figcaption><span>配图 {{ index + 1 }} · {{ image.name }}</span><ElLink :href="api.articleImageUrl(articleId, image.path.split('/').pop() || '')" :download="image.name" :underline="false">下载</ElLink></figcaption>
                </figure>
              </div>
              <p class="preview-image-note">公众号和知乎会自动上传正文配图；其他平台可下载后手动添加。</p>
            </div>
          </div>
          <footer class="article-preview-footer"><RouterLink :to="{ name: 'article-edit', params: { articleId } }">返回编辑文章 →</RouterLink><span>发布使用当前定稿版本</span></footer>
        </section>

        <nav class="article-platform-rail" aria-label="发布平台">
          <div class="platform-rail-heading"><h2>平台</h2><ElButton text class="text-button" native-type="button" @click="toggleAll">{{ allSelected ? '取消全选' : '全选' }}</ElButton></div>
          <div class="platform-rail-list">
            <ElButton
              v-for="platform in platforms"
              :key="platform.id"
              text bg class="platform-rail-item"
              :class="{ 'is-active': activePlatformId === platform.id, 'is-selected': platformSelectionCount(platform.id) > 0 }"
              native-type="button"
              :aria-pressed="activePlatformId === platform.id"
              @click="activePlatformId = platform.id"
            >
              <PlatformLogo class="platform-brand-mark" :platform-id="platform.id" :name="platform.name" />
              <span class="platform-rail-copy"><strong>{{ platform.name }}</strong><small>{{ platformStatusText(platform) }}</small></span>
              <ElTag v-if="platformSelectionCount(platform.id)" class="platform-rail-count" type="primary" effect="light" size="small" round>{{ platformSelectionCount(platform.id) }}</ElTag>
              <span v-else class="platform-rail-dot" :class="platformReady(platform) ? 'is-ready' : ''"></span>
            </ElButton>
          </div>
          <div class="platform-rail-hint">点击平台查看账号和专属发表设置</div>
        </nav>

        <section v-if="activePlatform" class="article-platform-settings" aria-labelledby="platform-settings-heading">
          <header class="platform-settings-header">
            <div class="platform-settings-title">
              <PlatformLogo class="platform-brand-mark platform-brand-mark-large" :platform-id="activePlatform.id" :name="activePlatform.name" />
              <div><h2 id="platform-settings-heading">{{ activePlatform.name }}</h2><ElTag class="article-platform-mode" :class="activePlatform.mode === 'official' || (activePlatform.id === 'csdn' && csdnBrowserEnabled) || (activePlatform.id === 'toutiao' && activePlatform.capabilities?.article?.publish === 'browser') ? 'mode-official' : 'mode-manual'" :type="activePlatform.mode === 'official' || (activePlatform.id === 'csdn' && csdnBrowserEnabled) || (activePlatform.id === 'toutiao' && activePlatform.capabilities?.article?.publish === 'browser') ? 'success' : 'info'" effect="light" size="small">{{ (activePlatform.id === 'csdn' && csdnBrowserEnabled) || (activePlatform.id === 'toutiao' && activePlatform.capabilities?.article?.publish === 'browser') ? '浏览器发表' : activePlatform.mode === 'official' ? '官方接口' : '手动发布' }}</ElTag></div>
            </div>
            <RouterLink v-if="activePlatform.id === 'wechat' || activePlatform.id === 'zhihu'" class="text-button" :to="{ name: 'accounts', query: { tab: 'article' } }">账号管理 ↗</RouterLink>
            <RouterLink v-else-if="activePlatform.id === 'xiaohongshu'" class="text-button" :to="{ name: 'accounts', query: { tab: 'video', platform: 'xiaohongshu' } }">小红书账号 ↗</RouterLink>
          </header>
          <p class="platform-settings-description">{{ activePlatform.message }}</p>
          <div class="article-platform-accounts-scroll">
            <template v-if="activePlatform.id === 'wechat' || activePlatform.id === 'zhihu'">
              <section class="settings-section" aria-labelledby="publish-account-list-heading" aria-label="发布目标账号">
                <div class="settings-section-heading"><h3 id="publish-account-list-heading">发布账号</h3><span>{{ platformSelectionCount(activePlatform.id) }} 个已选</span></div>
                <div v-if="activePlatform.accounts.length" class="article-account-target-list">
                  <ElCard v-for="account in activePlatform.accounts" :id="'article-account-' + activePlatform.id + '-' + account.id" :key="account.id" class="article-account-target" :class="{ 'is-picked': selected.includes(targetKey(activePlatform.id, account.id)), 'is-checking': isArticleAccountCheckPending(activePlatform.id, account) }" shadow="never">
                    <ElCheckbox class="article-account-target-choice" :model-value="selected.includes(targetKey(activePlatform.id, account.id))" :aria-label="`选择${activePlatform.name}账号 ${account.name}`" :disabled="publishing || !account.configured || isArticleAccountCheckPending(activePlatform.id, account) || !canPublish(activePlatform.id, account.id)" @change="handleArticleAccountTargetChange(activePlatform.id, account, $event === true)">
                      <ElAvatar v-if="account.avatar_url" class="article-account-avatar" :src="account.avatar_url" :size="34" fit="cover" :alt="`${account.name}头像`" />
                      <PlatformLogo v-else class="article-account-avatar" :platform-id="activePlatform.id" :name="activePlatform.name" />
                      <span class="article-account-copy"><strong>{{ account.name }}</strong><small :class="`account-status-${articleAccountStatusState(activePlatform.id, account)}`"><ElIcon v-if="articleAccountStatusState(activePlatform.id, account) === 'checking'" class="is-loading"><Loading /></ElIcon><ElIcon v-else-if="['connected', 'verified'].includes(articleAccountStatusState(activePlatform.id, account))"><CircleCheck /></ElIcon>{{ articleAccountStatusLabel(activePlatform.id, account) }}</small></span>
                      <ElTag class="article-publish-state" :class="publicationClass(currentPublication(activePlatform.id, account.id)?.status)" :type="publicationTagType(currentPublication(activePlatform.id, account.id)?.status)" effect="light" size="small">{{ publicationLabel(currentPublication(activePlatform.id, account.id)?.status) }}</ElTag>
                    </ElCheckbox>
                    <div v-if="currentPublication(activePlatform.id, account.id)" class="article-account-result">
                      <p v-if="currentPublication(activePlatform.id, account.id)?.message">{{ currentPublication(activePlatform.id, account.id)?.message }}</p>
                      <div v-if="activePlatform.id === 'wechat' && currentPublication(activePlatform.id, account.id)?.fan_notification_status !== 'not_requested'" class="fan-notification-result">
                        <ElTag class="article-publish-state" :class="fanNotificationClass(currentPublication(activePlatform.id, account.id)?.fan_notification_status)" :type="fanNotificationTagType(currentPublication(activePlatform.id, account.id)?.fan_notification_status)" effect="light" size="small">{{ fanNotificationLabel(currentPublication(activePlatform.id, account.id)?.fan_notification_status) }}</ElTag>
                        <span>{{ currentPublication(activePlatform.id, account.id)?.fan_notification_message }}</span>
                      </div>
                      <ElLink v-if="currentPublication(activePlatform.id, account.id)?.remote_url" class="article-remote-link" :href="currentPublication(activePlatform.id, account.id)?.remote_url || undefined" target="_blank" rel="noopener noreferrer" :underline="false">查看已发表文章 ↗</ElLink>
                      <ElButton
                        v-if="activePlatform.id === 'wechat' && (currentPublication(activePlatform.id, account.id)?.status === 'unknown' || currentPublication(activePlatform.id, account.id)?.fan_notification_status === 'unknown')"
                        text class="text-button result-refresh-button"
                        native-type="button"
                        :disabled="Boolean(refreshingPublicationId)"
                        @click="refreshAccountPublication(activePlatform.id, account.id)"
                      >
                        {{ refreshingPublicationId === currentPublication(activePlatform.id, account.id)?.id ? '查询中…' : '重新查询微信结果' }}
                      </ElButton>
                      <ElButton
                        v-if="activePlatform.id === 'wechat' && currentPublication(activePlatform.id, account.id)?.fan_notification_status === 'failed' && !currentPublication(activePlatform.id, account.id)?.fan_notification_remote_id"
                        text class="text-button result-refresh-button"
                        native-type="button"
                        :disabled="publishing"
                        @click="retryAccountNotification(activePlatform.id, account.id)"
                      >
                        重试粉丝通知
                      </ElButton>
                    </div>
                  </ElCard>
                </div>
                <div v-else class="article-no-account"><span>还没有添加可用账号。</span><RouterLink :to="{ name: 'accounts', query: { tab: 'article' } }">去管理账号 →</RouterLink></div>
              </section>
            </template>
            <template v-else-if="activePlatform.id === 'xiaohongshu'">
              <section class="settings-section" aria-labelledby="publish-account-list-heading" aria-label="小红书发布目标账号">
                <div class="settings-section-heading"><h3 id="publish-account-list-heading">发布账号</h3><span>{{ platformSelectionCount(activePlatform.id) }} 个已选</span></div>
                <div v-if="activePlatform.accounts.length" class="article-account-target-list">
                  <ElCard v-for="account in activePlatform.accounts" :id="'article-account-' + activePlatform.id + '-' + account.id" :key="account.id" class="article-account-target" :class="{ 'is-picked': selected.includes(targetKey(activePlatform.id, account.id)), 'is-checking': isArticleAccountCheckPending(activePlatform.id, account) }" shadow="never">
                    <ElCheckbox class="article-account-target-choice" :model-value="selected.includes(targetKey(activePlatform.id, account.id))" :aria-label="`选择小红书账号 ${account.name}`" :disabled="publishing || activePlatform.capabilities?.image_note?.publish !== 'manual' || !account.configured || isArticleAccountCheckPending(activePlatform.id, account) || xhsNoteImages.length === 0 || xhsNoteImages.length > 18 || !canPublish(activePlatform.id, account.id)" @change="handleArticleAccountTargetChange(activePlatform.id, account, $event === true)">
                      <ElAvatar v-if="account.avatar_url" class="article-account-avatar" :src="account.avatar_url" :size="34" fit="cover" :alt="`${account.name}头像`" />
                      <PlatformLogo v-else class="article-account-avatar" :platform-id="activePlatform.id" :name="activePlatform.name" />
                      <span class="article-account-copy"><strong>{{ account.name }}</strong><small :class="`account-status-${articleAccountStatusState(activePlatform.id, account)}`"><ElIcon v-if="articleAccountStatusState(activePlatform.id, account) === 'checking'" class="is-loading"><Loading /></ElIcon><ElIcon v-else-if="['connected', 'verified'].includes(articleAccountStatusState(activePlatform.id, account))"><CircleCheck /></ElIcon>{{ articleAccountStatusLabel(activePlatform.id, account) }}</small></span>
                      <ElTag class="article-publish-state" :class="publicationClass(currentPublication(activePlatform.id, account.id)?.status)" :type="publicationTagType(currentPublication(activePlatform.id, account.id)?.status)" effect="light" size="small">{{ publicationLabel(currentPublication(activePlatform.id, account.id)?.status) }}</ElTag>
                    </ElCheckbox>
                    <div v-if="currentPublication(activePlatform.id, account.id)" class="article-account-result">
                      <p v-if="currentPublication(activePlatform.id, account.id)?.message">{{ currentPublication(activePlatform.id, account.id)?.message }}</p>
                      <div v-if="['unknown', 'manual_required'].includes(currentPublication(activePlatform.id, account.id)?.status || '')" class="article-manual-actions">
                        <ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmXhsAccountPublication(account.id, 'published')">{{ refreshingPublicationId === currentPublication(activePlatform.id, account.id)?.id ? '正在记录…' : '已核实发布成功' }}</ElButton>
                        <ElButton text class="text-button" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmXhsAccountPublication(account.id, 'not_published')">确认未发布，可重试</ElButton>
                      </div>
                    </div>
                  </ElCard>
                </div>
                <div v-else class="article-no-account"><span>还没有小红书视频发布账号。</span><RouterLink :to="{ name: 'accounts', query: { tab: 'video', platform: 'xiaohongshu' } }">去连接小红书账号 →</RouterLink></div>
                <p v-if="!xhsNoteImages.length" class="platform-help-note">小红书图文至少需要 1 张图片，请返回文章编辑页上传封面或正文配图。</p>
                <p v-else-if="xhsNoteImages.length > 18" class="platform-help-note">当前有 {{ xhsNoteImages.length }} 张可用图片；小红书图文最多支持 18 张，请先减少配图。</p>
                <p v-if="activePlatform.capabilities?.image_note?.publish !== 'manual'" class="platform-help-note">请先重启工作台后端，启用小红书手动发布保护。</p>
              </section>
            </template>
            <template v-else>
              <section class="settings-section" :aria-label="`${activePlatform.name}发布目标`">
                <div class="article-account-target-list manual-account-target-list">
                  <ElCard class="article-account-target manual-target-card" :class="{ 'is-picked': selected.includes(targetKey(activePlatform.id, null)) }" shadow="never">
                    <ElCheckbox class="article-account-target-choice" :model-value="selected.includes(targetKey(activePlatform.id, null))" :aria-label="`选择${activePlatform.name}账号 ${activePlatformAccount?.name || ''}`" :disabled="publishing || !targetOptions.some((target) => target.platform_id === activePlatform.id) || !canPublish(activePlatform.id)" @change="toggleTarget(targetKey(activePlatform.id, null))">
                      <ElAvatar v-if="activePlatformAccount?.avatar_url" class="article-account-avatar" :src="activePlatformAccount.avatar_url" :size="34" fit="cover" :alt="`${activePlatformAccount.name}头像`" />
                      <PlatformLogo v-else class="article-account-avatar manual-avatar" :platform-id="activePlatform.id" :name="activePlatform.name" />
                      <span class="article-account-copy"><strong>{{ activePlatformAccount?.name || `${activePlatform.name}发布目标` }}</strong><small>{{ activePlatform.id === 'csdn' && csdnBrowserEnabled && !csdnManualReason ? '使用已登录的 CSDN 账号发表' : activePlatform.id === 'toutiao' && activePlatform.capabilities?.article?.publish === 'browser' && !toutiaoManualReason ? '使用当前已连接的头条号账号最终发表' : activePlatformAccount ? articleAccountStatusLabel(activePlatform.id, activePlatformAccount) : '复制排版内容后在平台网页完成发表' }}</small></span>
                      <ElTag class="article-publish-state" :class="publicationClass(currentPublication(activePlatform.id)?.status)" :type="publicationTagType(currentPublication(activePlatform.id)?.status)" effect="light" size="small">{{ publicationLabel(currentPublication(activePlatform.id)?.status) }}</ElTag>
                    </ElCheckbox>
                    <p v-if="currentPublication(activePlatform.id)?.message" class="article-publish-result-message">{{ currentPublication(activePlatform.id)?.message }}</p>
                    <ElLink v-if="currentPublication(activePlatform.id)?.remote_url" class="article-remote-link" :href="currentPublication(activePlatform.id)?.remote_url || undefined" target="_blank" rel="noopener noreferrer" :underline="false">查看平台内容 ↗</ElLink>
                  </ElCard>
                  <div v-if="activePlatform.mode === 'manual' && activePlatformRequiresManual" class="article-manual-actions manual-account-target-actions">
                      <ElButton class="button button-secondary button-small" native-type="button" @click="copyManual(activePlatform.id)">复制平台格式正文</ElButton>
                      <ElButton v-if="activePlatform.manual_url" class="button button-secondary" native-type="button" :disabled="Boolean(busyManual)" @click="openPlatform(activePlatform)">{{ openPlatformLabel(activePlatform) }}</ElButton>
                      <ElButton v-if="currentPublication(activePlatform.id)?.status === 'manual_required'" class="button" native-type="button" :disabled="!!busyManual" @click="markManualPublished(activePlatform.id)">{{ busyManual === activePlatform.id ? '正在记录…' : '确认已发布' }}</ElButton>
                      <template v-if="activePlatform.id === 'csdn' && currentPublication('csdn')?.status === 'unknown'">
                        <ElButton class="button button-secondary" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmCsdnPublication('published')">已核实发布成功</ElButton>
                        <ElButton text class="text-button" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmCsdnPublication('not_published')">确认未发布，可重试</ElButton>
                      </template>
                      <template v-if="activePlatform.id === 'toutiao' && currentPublication('toutiao')?.status === 'unknown'">
                        <ElButton class="button button-secondary" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmToutiaoPublication('published')">已核实发布成功</ElButton>
                        <ElButton text class="text-button" native-type="button" :disabled="Boolean(refreshingPublicationId)" @click="confirmToutiaoPublication('not_published')">确认未发布，可重试</ElButton>
                      </template>
                  </div>
                </div>
              </section>
            </template>
          </div>
          <div class="platform-settings-scroll">
            <section v-if="activePlatform.id !== 'toutiao' && (platformSelectionCount(activePlatform.id) > 0 || (activePlatform.id === 'csdn' && csdnSettingsLocked))" class="settings-section article-platform-cover" aria-label="平台封面设置">
              <div class="settings-section-heading"><h3>平台封面</h3><span>{{ platformCovers[activePlatform.id] ? '平台专用' : '继承通用封面' }}</span></div>
              <img v-if="effectiveCover(activePlatform.id)" class="article-preview-cover" :src="effectiveCover(activePlatform.id)?.preview_url" :alt="`${activePlatform.name} 封面预览`" />
              <p class="platform-help-note">比例由你选择；平台实际封面要求以其发布页面为准。{{ activePlatform.mode === 'manual' && !(activePlatform.id === 'csdn' && csdnBrowserEnabled && !csdnManualReason) ? '此平台需下载封面后手动上传。' : '' }}</p>
              <label class="form-label" :for="`article-cover-ratio-${activePlatform.id}`">封面比例</label>
              <ElSelect :id="`article-cover-ratio-${activePlatform.id}`" v-model="platformCoverRatios[activePlatform.id]" class="input" placeholder="使用文章原图" :disabled="activePlatform.id === 'csdn' && csdnSettingsLocked" clearable @change="saveCoverChoices">
                <ElOption v-for="(label, value) in coverRatioNames" :key="value" :label="label" :value="value" />
              </ElSelect>
              <div class="article-manual-actions">
                <ElButton class="button button-secondary button-small" native-type="button" :disabled="activePlatform.id === 'csdn' && csdnSettingsLocked" @click="openPublishCoverEditor(activePlatform.id)">单独裁剪或上传</ElButton>
                <ElButton v-if="platformCovers[activePlatform.id] && !(activePlatform.id === 'csdn' && csdnSettingsLocked)" text class="text-button" native-type="button" @click="useCommonCover(activePlatform.id)">改用通用封面</ElButton>
                <ElLink v-if="activePlatform.mode === 'manual' && effectiveCover(activePlatform.id)" :href="effectiveCover(activePlatform.id)?.preview_url" download="文章封面.jpg" :underline="false">下载封面</ElLink>
              </div>
            </section>
            <template v-if="activePlatform.id === 'wechat' || activePlatform.id === 'zhihu'">


              <section v-if="activePlatform.configured" class="settings-section platform-fields-section" aria-labelledby="publish-fields-heading">
                <div class="settings-section-heading"><h3 id="publish-fields-heading">{{ activePlatform.name }}发表设置</h3><span>仅作用于当前平台</span></div>
                <label class="form-label" :for="`publish-${activePlatform.id}-title`">发表标题 <span class="required-mark">*</span></label>
                <ElInput :id="`publish-${activePlatform.id}-title`" v-model="publishOptions[activePlatform.id].title" class="input" maxlength="200" />
                <template v-if="activePlatform.id === 'wechat'">
                  <label class="form-label" for="publish-wechat-summary">摘要 <span class="optional-label">最多 120 字</span></label>
                  <ElInput id="publish-wechat-summary" v-model="publishOptions.wechat.summary" class="textarea" type="textarea" :rows="3" maxlength="120" placeholder="留空表示不填写公众号摘要" />
                  <label class="form-label" for="publish-wechat-author">作者 <span class="optional-label">选填</span></label>
                  <ElInput id="publish-wechat-author" v-model="publishOptions.wechat.author" class="input" maxlength="200" />
                  <div class="article-publish-cover-setting">
                    <span class="cover-setting-icon">▧</span><span><strong>文章封面</strong><small>{{ hasCoverImage ? '使用左侧预览中的当前封面' : '公众号发表需要封面图' }}</small></span>
                    <RouterLink :to="{ name: 'article-edit', params: { articleId } }">{{ hasCoverImage ? '更换' : '去上传' }} →</RouterLink>
                  </div>
                  <div class="fan-notification-setting" :class="{ 'is-enabled': notifyFans }">
                    <div class="fan-notification-toggle"><ElCheckbox v-model="notifyFans" class="fan-notification-checkbox" :disabled="publishing || selectedWechatCount === 0"><span class="fan-notification-copy"><strong>发表后通知粉丝</strong><small>单独调用微信群发接口，向所选公众号的全体粉丝发送</small></span></ElCheckbox><ElTag class="default-off-tag" type="info" effect="light" size="small">默认关闭</ElTag></div>
                    <p>勾选后，系统会先等待文章发表成功，再提交群发并查询结果。群发可能受账号资质、接口权限、每日额度和后台安全确认影响。开启此项会另建一份群发素材。</p>
                    <p v-if="selectedWechatCount === 0" class="notification-selection-hint">请先勾选上方至少一个公众号账号。</p>
                  </div>
                  <p class="article-platform-caution">微信可能要求公众号管理员在后台确认 API 群发。AppID 权限、账号认证状态和请求出口 IP 白名单由公众号后台控制。</p>
                </template>
                <p v-else class="article-platform-field-note">知乎发布接口提交标题与正文；摘要、独立封面和分类字段不受当前 OpenAPI 支持。</p>
              </section>
            </template>

            <template v-else-if="activePlatform.id === 'xiaohongshu'">


              <section class="settings-section platform-fields-section" aria-labelledby="publish-fields-heading">
                <div class="settings-section-heading"><h3 id="publish-fields-heading">小红书图文笔记预览</h3><span>手动发布</span></div>
                <label class="form-label" for="publish-xiaohongshu-title">笔记标题 <span class="required-mark">最多 20 字</span></label>
                <ElInput id="publish-xiaohongshu-title" v-model="publishOptions.xiaohongshu.title" class="input" maxlength="20" />
                <label class="form-label" for="publish-xiaohongshu-note">笔记正文 <span class="optional-label">{{ publishOptions.xiaohongshu.note.length }} / 1000</span></label>
                <ElInput id="publish-xiaohongshu-note" v-model="publishOptions.xiaohongshu.note" class="textarea" type="textarea" :rows="8" maxlength="1000" placeholder="文章正文会预填为笔记文案，可在此调整。" />
                <div class="article-manual-actions">
                  <ElButton class="button button-secondary button-small" native-type="button" @click="copyXhsText(publishOptions.xiaohongshu.title, '标题')">复制标题</ElButton>
                  <ElButton class="button button-secondary button-small" native-type="button" @click="copyXhsText(publishOptions.xiaohongshu.note, '正文')">复制正文</ElButton>
                  <ElLink href="https://creator.xiaohongshu.com/publish/publish" target="_blank" rel="noopener noreferrer" :underline="false">自行打开创作中心 ↗</ElLink>
                </div>
                <div class="xhs-note-image-preview">
                  <strong>待手动上传的图片 · {{ xhsNoteImages.length }} 张</strong>
                  <div v-if="xhsNoteImages.length" class="article-preview-image-grid">
                    <figure v-for="(image, index) in xhsNoteImages" :key="image.key">
                      <img :src="image.url" :alt="image.name" />
                      <figcaption><span>{{ index === 0 && article?.document.cover_image_path ? '封面' : `配图 ${index + 1}` }} · {{ image.name }}</span><ElLink :href="image.url" :download="image.name" :underline="false">下载</ElLink></figcaption>
                    </figure>
                  </div>
                  <p>顺序为文章封面，其后按正文配图顺序；同一张图片只上传一次。最多 18 张。</p>
                </div>
                <p class="platform-help-note">文章格式已整理为纯文本；请自行复制标题、正文并下载图片，在创作中心手动发布后回来确认。</p>
              </section>
            </template>

            <template v-else>

              <section v-if="activePlatform.id === 'csdn' && (platformSelectionCount('csdn') > 0 || csdnSettingsLocked)" class="settings-section platform-fields-section" aria-label="CSDN 发表设置">
                <div class="settings-section-heading"><h3>CSDN 发表设置</h3><span>本次任务保存</span></div>
                <p class="platform-help-note">{{ csdnBrowserEnabled ? (csdnManualReason || '支持原创文章的标题、正文、标签、摘要、封面、创作声明、可见范围和模板；其他设置将转为手动发布。') : 'CSDN 目前仍需手动发布；以下设置会记录在任务中，请在平台发布表单中核对填写。' }}</p>
                <label class="form-label" for="csdn-tags">文章标签 <span class="required-mark">*</span></label>
                <div class="csdn-tag-count" aria-live="polite">已选 {{ csdnTags.length }}<template v-if="csdnTagCatalog"> / {{ csdnTagCatalog.max_tags }}</template></div>
                <div v-if="csdnTags.length" class="csdn-selected-tags"><ElTag v-for="tag in csdnTags" :key="tag" :closable="!csdnSettingsLocked" @close="csdnTags = csdnTags.filter((item) => item !== tag)">{{ tag }}</ElTag></div>
                <div class="csdn-tag-picker">
                  <div class="csdn-tag-input"><ElInput id="csdn-tags" v-model="csdnTagInput" class="input" maxlength="100" placeholder="搜索标签，或输入自定义标签" :disabled="csdnSettingsLocked || csdnTagLoading" @keydown.enter.prevent="chooseCsdnTag(csdnTagInput, true)" /><ElButton class="button button-secondary button-small" native-type="button" :disabled="csdnTagLoading || csdnSettingsLocked" @click="loadCsdnTags(csdnTagInput)">搜索</ElButton></div>
                  <p class="platform-help-note">平台标签 · 分类及选项来自当前 CSDN 账号</p>
                  <p v-if="csdnTagLoading" class="platform-help-note" role="status">正在读取 CSDN 标签…</p>
                  <div v-else-if="csdnTagCatalog" class="csdn-tag-catalog">
                    <nav class="csdn-tag-categories" aria-label="CSDN 标签分类"><button v-for="category in csdnTagCatalog.categories" :key="category.name" type="button" class="csdn-tag-category" :aria-pressed="csdnTagSearchResults === null && csdnTagCategory === category.name" @click="selectCsdnTagCategory(category.name)">{{ category.name }}</button></nav>
                    <div class="csdn-tag-options"><strong>{{ csdnTagSearchResults === null ? csdnTagCategory : '搜索结果' }}</strong><div v-if="visibleCsdnTags.length" class="csdn-tag-options-list"><button v-for="tag in visibleCsdnTags" :key="tag" type="button" class="csdn-tag-option" :aria-pressed="csdnTags.some((item) => item.toLocaleLowerCase() === tag.toLocaleLowerCase())" :disabled="csdnSettingsLocked" @click="toggleCsdnTag(tag)">{{ tag }}</button></div><p v-else class="platform-help-note">没有相关标签，可换个关键词或尝试自定义。</p></div>
                  </div>
                  <ElButton class="button button-secondary button-small" native-type="button" :disabled="csdnSettingsLocked || csdnTagLoading || !csdnTagCatalog || csdnTagCatalog.custom_allowed === false" @click="chooseCsdnTag(csdnTagInput, true)">添加自定义标签</ElButton>
                  <p v-if="csdnTagCatalog?.custom_allowed === false" class="platform-help-note">当前账号暂不支持自定义标签，请选择平台标签。</p>
                  <div v-if="csdnTagError && csdnTagCatalog" class="csdn-tag-error" role="alert">{{ csdnTagError }} <ElButton class="button button-secondary button-small" native-type="button" :disabled="csdnTagLoading" @click="loadCsdnTags()">重新获取</ElButton></div>
                </div>
                <p class="platform-help-note">推荐使用当前文章标题和正文；自定义标签按 CSDN 账号和平台规则核验。</p>
                <label class="form-label" for="csdn-summary">文章摘要 <span class="optional-label">最多 256 字</span></label>
                <ElInput id="csdn-summary" v-model="csdnSettings.summary" class="textarea" type="textarea" :rows="3" maxlength="256" :disabled="csdnSettingsLocked" show-word-limit />
                <label class="form-label" for="csdn-type">文章类型</label>
                <ElSelect id="csdn-type" v-model="csdnSettings.article_type" class="input" :disabled="csdnSettingsLocked"><ElOption label="原创" value="original" /><ElOption label="转载" value="reprint" /><ElOption label="翻译" value="translation" /></ElSelect>
                <template v-if="csdnSettings.article_type !== 'original'"><label class="form-label" for="csdn-source">来源链接</label><ElInput id="csdn-source" v-model="csdnSettings.source_url" class="input" placeholder="填写原文链接" :disabled="csdnSettingsLocked" /></template>
                <label class="form-label" for="csdn-visibility">可见范围</label>
                <ElSelect id="csdn-visibility" v-model="csdnSettings.visibility" class="input" :disabled="csdnSettingsLocked"><ElOption label="全部可见" value="public" /><ElOption label="仅我可见" value="private" /><ElOption label="粉丝可见" value="followers" /><ElOption label="VIP 可见" value="vip" /></ElSelect>
                <label class="form-label" for="csdn-template">文章模板</label>
                <ElSelect id="csdn-template" v-model="csdnSettings.template" class="input" :disabled="csdnSettingsLocked"><ElOption label="默认模板" value="default" /><ElOption label="简洁模板" value="simple" /><ElOption label="墨衍模板" value="ink" /></ElSelect>
                <ElCheckbox v-model="csdnSettings.backup_gitcode" :disabled="csdnSettingsLocked">同时备份到 GitCode</ElCheckbox>
                <ElCheckbox v-model="csdnSettings.cross_publish" :disabled="csdnSettingsLocked">启用 CSDN 多平台发布</ElCheckbox>
                <label class="form-label" for="csdn-category">分类专栏</label><ElInput id="csdn-category" v-model="csdnSettings.category" class="input" placeholder="填写平台中可用的分类专栏名称" :disabled="csdnSettingsLocked" />
                <label class="form-label" for="csdn-declaration">创作声明</label>
                <ElSelect id="csdn-declaration" v-model="csdnSettings.declaration" class="input" :disabled="csdnSettingsLocked || csdnTagLoading || !csdnDeclarationOptions.length">
                  <ElOption v-for="declaration in csdnDeclarationOptions" :key="declaration" :label="declaration" :value="declaration" />
                </ElSelect>
                <div v-if="csdnTagLoading && !csdnDeclarationOptions.length" class="platform-help-note" role="status">正在读取 CSDN 创作声明选项…</div>
                <div v-else-if="csdnDeclarationSelectionInvalid" class="csdn-tag-error" role="alert">当前选择不在 CSDN 最新声明列表中，请重新选择。</div>
                <div v-else-if="csdnDeclarationError" class="csdn-tag-error" role="alert">{{ csdnDeclarationError }} <ElButton class="button button-secondary button-small" native-type="button" :disabled="csdnTagLoading" @click="loadCsdnTags()">重新获取</ElButton></div>
                <label class="form-label" for="csdn-activity">参与活动</label><ElInput id="csdn-activity" v-model="csdnSettings.activity" class="input" placeholder="填写平台中可用的活动；可留空" :disabled="csdnSettingsLocked" />
                <label class="form-label" for="csdn-topic">话题</label><ElInput id="csdn-topic" v-model="csdnSettings.topic" class="input" placeholder="填写平台中可用的话题；可留空" :disabled="csdnSettingsLocked" />
                <ElButton class="button button-secondary button-small" native-type="button" @click="copyCsdnSettings">复制发表设置</ElButton>
              </section>
              <section v-if="activePlatform.id === 'toutiao'" class="settings-section platform-fields-section" aria-label="今日头条发表设置">
                <div class="settings-section-heading"><h3>头条号发表设置</h3><span>本次任务保存</span></div>
                <p class="platform-help-note">单图默认继承通用封面；三图需要分别设置 3 张不同图片。封面比例以头条号上传校验为准。</p>
                <label class="form-label" for="publish-toutiao-title">头条文章标题 <span class="required-mark">*</span></label>
                <ElInput id="publish-toutiao-title" v-model="publishOptions.toutiao.title" class="input" maxlength="30" show-word-limit />
                <span class="platform-help-note">头条号标题限制 2–30 字；平台编辑器会再次校验。</span>

                <span class="form-label">展示封面</span>
                <div class="article-manual-actions toutiao-cover-mode" role="group" aria-label="头条号封面模式">
                  <ElButton v-for="(label, mode) in toutiaoCoverModeNames" :key="mode" class="button button-secondary button-small" :type="toutiaoSettings.cover_mode === mode ? 'primary' : 'default'" native-type="button" :aria-pressed="toutiaoSettings.cover_mode === mode" @click="toutiaoSettings.cover_mode = mode">{{ label }}</ElButton>
                </div>
                <div v-if="toutiaoCoverCount()" class="toutiao-cover-slots">
                  <div v-for="slot in toutiaoCoverCount()" :key="`toutiao-slot-${slot}`" class="toutiao-cover-slot">
                    <img v-if="effectiveToutiaoCover(slot)" :src="effectiveToutiaoCover(slot)?.preview_url" :alt="`头条号封面 ${slot}`" />
                    <div v-else class="toutiao-cover-empty">封面 {{ slot }}<br />尚未设置</div>
                    <div class="toutiao-cover-slot-copy"><strong>封面 {{ slot }}</strong><small>{{ effectiveToutiaoCover(slot) ? (slot === 1 && !platformCovers['toutiao-slot-1'] ? '继承通用封面' : '平台专用封面') : '请选择不同的图片' }}</small>
                      <div class="article-manual-actions"><ElButton class="button button-secondary button-small" native-type="button" @click="openPublishCoverEditor(`toutiao-slot-${slot}`)">{{ effectiveToutiaoCover(slot) ? '裁剪或更换' : '选择图片' }}</ElButton><ElButton v-if="platformCovers[`toutiao-slot-${slot}`] && slot === 1" text class="text-button" native-type="button" @click="useToutiaoCommonCover(slot)">恢复通用封面</ElButton><ElButton v-if="platformCovers[`toutiao-slot-${slot}`] && slot > 1" text class="text-button" native-type="button" @click="useToutiaoCommonCover(slot)">清除</ElButton></div>
                    </div>
                  </div>
                </div>
                <p v-if="toutiaoCoverSelectionError" class="platform-validation-note" role="alert">{{ toutiaoCoverSelectionError }}</p>
                <p v-else-if="toutiaoSettings.cover_mode === 'none'" class="platform-help-note">本次发表不上传封面。</p>
                <p v-else-if="toutiaoSettings.cover_mode === 'triple'" class="platform-help-note">封面 1 可继承通用封面；封面 2、3 需要单独选择，不能重复同一张图片。</p>

                <div class="settings-section toutiao-options-grid">
                  <div><span class="form-label">投放广告</span><div class="toutiao-option-row"><ElCheckbox v-model="toutiaoSettings.advertising">投放广告赚收益</ElCheckbox><span>默认关闭</span></div></div>
                  <div><span class="form-label">头条首发</span><div class="toutiao-option-row"><ElCheckbox v-model="toutiaoSettings.original_first">声明头条首发</ElCheckbox><span>默认关闭</span></div></div>
                  <div><span class="form-label">发文特权</span><div class="toutiao-option-row"><ElCheckbox v-model="toutiaoSettings.allow_tips">允许赞赏</ElCheckbox><span>默认关闭</span></div></div>
                  <div><span class="form-label">同步微头条</span><div class="toutiao-option-row"><ElCheckbox v-model="toutiaoSettings.sync_micro">同时发布微头条</ElCheckbox><span>默认关闭</span></div></div>
                </div>
                <span class="form-label">作品声明</span>
                <p v-if="toutiaoOptionsLoading" class="platform-help-note" role="status">正在读取当前账号的作品声明和合集选项…</p>
                <div v-else-if="toutiaoDeclarations.length" class="toutiao-declarations"><ElCheckbox v-for="declaration in toutiaoDeclarations" :key="declaration" :model-value="toutiaoSettings.declarations.includes(declaration)" :label="declaration" @change="toggleToutiaoDeclaration(declaration, $event)">{{ declaration }}</ElCheckbox></div>
                <p v-else class="platform-help-note">请连接头条号后读取当前账号的作品声明选项。</p>
                <div v-if="toutiaoOptionsError" class="platform-validation-note" role="alert">{{ toutiaoOptionsError }} <ElButton class="button button-secondary button-small" native-type="button" :disabled="toutiaoOptionsLoading" @click="loadToutiaoOptions(true)">重新读取选项</ElButton></div>
                <p v-else-if="toutiaoAccountOptions" class="platform-help-note">声明选项已从当前登录账号的头条号编辑器读取；请按文章实际情况勾选，系统不会替你默认声明。</p>
                <div class="settings-section toutiao-account-options">
                  <div><strong>发布位置</strong><span>未添加（默认）</span></div>
                  <div><strong>合集</strong><ElSelect v-model="toutiaoSettings.collection" class="input" placeholder="不添加合集" :disabled="!toutiaoCollections.length"><ElOption v-for="collection in toutiaoCollections" :key="collection" :label="collection" :value="collection" /></ElSelect></div>
                </div>
                <p class="platform-help-note">发布位置暂未接入地点搜索；合集按当前账号实际读取，选择合集时将转为手动发布，由你在头条号网页确认。</p>
                <p v-if="toutiaoManualReason" class="platform-validation-note" role="status">{{ toutiaoManualReason }}</p>
                <p v-else class="platform-help-note">本页会在最终发表前逐项复核标题、正文、封面和发表选项。平台审核及公开展示时间由头条号决定。</p>
              </section>
              <div v-if="activePlatform.id === 'weibo'" class="platform-help-note">微博发布可能消耗已开通服务的积分，并会按平台规则标记 AI 生成内容；配图需在微博端手动添加。</div>
              <div v-if="activePlatform.id === 'zhihu'" class="platform-help-note">知乎封面会作为正文首图嵌入。当前 OpenAPI 不提供独立封面、摘要或分类字段，实际发布权限以知乎授权为准。</div>
              <div v-if="activePlatform.mode !== 'manual' && activePlatform.id === 'weibo' && (bodyImages.length || hasCoverImage)" class="article-manual-actions">
                <ElLink class="text-button" href="https://weibo.com/" target="_blank" rel="noopener noreferrer" :underline="false">打开微博补充配图 ↗</ElLink>
              </div>
            </template>
          </div>
        </section>
      </div>

      <div class="article-publish-footer">
        <div><strong>已选择 {{ actionableSelection.length }} 个发布目标</strong><p>平台和账号分别执行、分别记录结果；只读取当前文章定稿版本。</p></div>
        <ElButton class="button" native-type="button" :disabled="publishing || !actionableSelection.length || article.status !== 'finalized'" @click="publishSelected">{{ publishing ? '正在处理所选平台…' : `确认发表（${actionableSelection.length}）` }}</ElButton>
      </div>
    </template>
    <VideoCoverCropEditor :open="cropOpen" :title="cropTarget === 'common' ? '设置通用封面' : '设置平台专用封面'" :aspect-ratio="cropRatio" video-src="" :initial-image-src="cropImageSrc" :disabled="cropUploading" @save="savePublishCover" @close="cropOpen = false" />
  </section>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类覆盖(全量落地时上移 styles.css)—— */
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.form-label { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }

.article-publish-page { display: flex; flex-direction: column; gap: 12px; min-width: 0; padding-bottom: 12px; }
.article-publish-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; min-height: 44px; }
.article-publish-toolbar h1 { margin: 5px 0 0; color: var(--ink); font-size: var(--fs-title-2xl); font-weight: var(--fw-semibold); }
.article-back-link { color: var(--text-tertiary); font-size: var(--fs-body-sm); text-decoration: none; }
.article-back-link:hover { color: var(--pine); }
.article-finalize-reminder { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.article-publish-workspace { display: grid; grid-template-columns: minmax(320px, .95fr) 190px minmax(380px, 1.1fr); height: min(720px, calc(100vh - 255px)); min-height: 570px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--paper); box-shadow: 0 3px 16px rgb(24 39 75 / 5%); }
.article-publish-article, .article-platform-rail, .article-platform-settings { min-width: 0; min-height: 0; }
.article-publish-article { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; border-right: 1px solid var(--line); background: #fff; }
.workspace-panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 17px 18px 13px; border-bottom: 1px solid var(--line); }
.workspace-panel-heading h2, .platform-rail-heading h2, .platform-settings-header h2 { margin: 4px 0 0; color: var(--ink); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-summary-kicker { color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.readonly-tag, .default-off-tag { padding: 3px 8px; border-radius: 999px; color: #526273; background: #f1f4f7; font-size: var(--fs-caption); white-space: nowrap; }
.readonly-tag.el-tag, .default-off-tag.el-tag { height: auto; border-color: transparent; font-size: var(--fs-caption); line-height: 1.5; }
.article-preview-scroll { min-height: 0; overflow: auto; padding: 20px 22px; }
.article-preview-heading { margin-bottom: 18px; padding-bottom: 17px; border-bottom: 1px solid var(--line); }
.article-preview-heading h3 { margin: 0; color: var(--ink); font-size: var(--fs-title-2xl); line-height: 1.45; overflow-wrap: anywhere; }
.article-preview-heading p { margin: 10px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.7; }
.article-preview-cover { display: block; width: 100%; max-height: 230px; margin-top: 16px; border-radius: 8px; object-fit: cover; }
.article-preview-scroll :deep(.markdown-preview) { color: var(--ink); font-size: var(--fs-body-lg); line-height: 1.8; overflow-wrap: anywhere; }
.article-preview-scroll :deep(.markdown-preview h1), .article-preview-scroll :deep(.markdown-preview h2), .article-preview-scroll :deep(.markdown-preview h3) { margin: 1.35em 0 .55em; font-size: 16px; line-height: 1.5; }
.article-preview-scroll :deep(.markdown-preview p) { margin: .8em 0; }
.article-preview-scroll :deep(.markdown-preview blockquote) { margin: 1em 0; padding: 2px 12px; border-left: 3px solid #cbd8e5; color: var(--muted); }
.article-preview-scroll :deep(.markdown-preview a) { color: var(--pine); }
.article-preview-images { margin-top: 22px; padding-top: 17px; border-top: 1px solid var(--line); }
.xhs-note-image-preview { display: grid; gap: 8px; margin-top: 18px; padding-top: 15px; border-top: 1px solid var(--line); }
.xhs-note-image-preview > strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.xhs-note-image-preview > p { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.preview-subheading { margin-bottom: 10px; color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.article-preview-image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 9px; }
.article-preview-image-grid figure { min-width: 0; margin: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.article-preview-image-grid img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; }
.article-preview-image-grid figcaption { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 7px; color: var(--text-tertiary); font-size: var(--fs-caption); }
.article-preview-image-grid figcaption span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.article-preview-image-grid a { flex: 0 0 auto; color: var(--pine); text-decoration: none; }
.preview-image-note { margin: 8px 0 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.6; }
.article-preview-footer { display: flex; justify-content: space-between; gap: 8px; padding: 11px 18px; border-top: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-caption); }
.article-preview-footer a { color: var(--pine); text-decoration: none; }
.article-platform-rail { display: flex; flex-direction: column; overflow: auto; border-right: 1px solid var(--line); background: #f8fafc; }
.platform-rail-heading { display: flex; align-items: center; justify-content: space-between; gap: 5px; padding: 16px 10px 11px; }
.platform-rail-heading h2 { margin: 0; font-size: var(--fs-body-sm); }
.platform-rail-heading .text-button { padding: 4px 2px; font-size: var(--fs-caption); }
.platform-rail-list { display: grid; gap: 4px; padding: 0 7px; }
.platform-rail-item.el-button { position: relative; display: flex; align-items: center; width: 100%; height: auto; min-height: 53px; margin: 0; padding: 7px 8px; border: 1px solid transparent; border-radius: 8px; color: var(--ink); background: transparent; text-align: left; cursor: pointer; }
.platform-rail-item.el-button :deep(> span) { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 8px; width: 100%; }
.platform-rail-item.el-button:hover { border-color: transparent; color: var(--ink); background: #edf3f7; }
.platform-rail-item.el-button.is-active { border-color: #d8e6fb; background: #edf4ff; }
.platform-rail-item.el-button.is-active::before { position: absolute; top: 9px; bottom: 9px; left: -1px; width: 3px; border-radius: 3px; background: #3977eb; content: ''; }
.platform-brand-mark { display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto; overflow: hidden; border: 1px solid #e7ebf0; border-radius: 8px; background: #fff; }
.platform-brand-mark-large { width: 36px; height: 36px; border-radius: 10px; }
.platform-rail-copy { display: grid; gap: 4px; min-width: 0; }
.platform-rail-copy strong { overflow: hidden; font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.platform-rail-copy small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.platform-rail-count { min-width: 20px; padding: 3px 6px; border-radius: 999px; color: #2462c9; background: #dfeaff; font-size: var(--fs-caption); font-variant-numeric: tabular-nums; text-align: center; }
.platform-rail-count.el-tag { height: auto; border-color: transparent; line-height: 1.5; }
.platform-rail-dot { width: 7px; height: 7px; border-radius: 50%; background: #bcc5d0; }
.platform-rail-dot.is-ready { background: #31a874; }
.platform-rail-hint { margin: auto 11px 12px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.55; }
.article-platform-settings { display: grid; grid-template-rows: auto auto auto minmax(0, 1fr); background: #fff; }
.platform-settings-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px 10px; }
.platform-settings-title { display: flex; align-items: center; gap: 10px; }
.platform-settings-title h2 { margin: 0 0 6px; font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.platform-settings-mark { width: 36px; height: 36px; border-radius: 10px; background: #1da76b; }
.article-platform-mode { display: inline-flex; margin-left: 0; padding: 3px 8px; border-radius: 999px; font-size: var(--fs-caption); font-weight: var(--fw-semibold); white-space: nowrap; }
.article-platform-mode.el-tag { height: auto; border-color: transparent; line-height: 1.5; }
.mode-official { color: #176b4b; background: #eaf6ef; }
.mode-manual { color: #647184; background: #f0f2f5; }
.text-button { color: var(--pine); }
.platform-settings-description { margin: 0; padding: 0 18px 12px; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.55; }
.article-platform-accounts-scroll { min-height: 0; max-height: min(480px, 50vh); overflow: auto; padding: 0 18px; scrollbar-gutter: stable; }
.platform-settings-scroll { min-height: 0; overflow: auto; padding: 0 18px 18px; }
.settings-section { display: grid; gap: 11px; padding: 15px 0; border-top: 1px solid var(--line); }
.settings-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.settings-section-heading h3 { margin: 0; color: var(--ink); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.settings-section-heading > span { color: var(--text-tertiary); font-size: var(--fs-caption); }
.article-account-target-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 10px; }
.csdn-tag-input { display: flex; align-items: center; gap: 8px; min-width: 0; }
.csdn-tag-input .input { flex: 1 1 auto; min-width: 0; }
.csdn-selected-tags { display: flex; flex-wrap: wrap; gap: 8px; }
.csdn-tag-count { color: var(--text-secondary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.csdn-tag-picker { display: grid; gap: 10px; min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }
.csdn-tag-picker > .button { justify-self: start; }
.csdn-tag-catalog { display: grid; grid-template-columns: minmax(90px, 130px) minmax(0, 1fr); gap: 12px; min-width: 0; }
.csdn-tag-categories { display: flex; flex-direction: column; align-items: stretch; max-height: 220px; overflow: auto; }
.csdn-tag-category, .csdn-tag-option { border: 0; border-radius: 5px; color: var(--text-primary); background: transparent; font-size: var(--fs-body-sm); cursor: pointer; }
.csdn-tag-category { min-height: 34px; padding: 6px 8px; text-align: left; overflow-wrap: anywhere; }
.csdn-tag-category[aria-pressed='true'], .csdn-tag-option[aria-pressed='true'] { color: var(--pine); background: var(--pine-soft); font-weight: var(--fw-semibold); }
.csdn-tag-category:focus-visible, .csdn-tag-option:focus-visible { outline: 2px solid var(--pine); outline-offset: 2px; }
.csdn-tag-options { min-width: 0; max-height: 220px; overflow: auto; padding-left: 12px; border-left: 1px solid var(--line); }
.csdn-tag-options > strong { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.csdn-tag-options-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.csdn-tag-option { min-height: 34px; padding: 5px 10px; border: 1px solid var(--line); overflow-wrap: anywhere; }
.csdn-tag-option:disabled { cursor: not-allowed; }
.csdn-tag-error { color: var(--danger); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.manual-target-card { grid-column: 1 / -1; }
.manual-account-target-actions { grid-column: 1 / -1; }
.article-account-target.el-card { min-width: 0; min-height: 92px; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); box-shadow: 0 1px 3px rgb(24 39 75 / 4%); }
.article-account-target.el-card :deep(.el-card__body) { display: grid; align-content: start; gap: 9px; min-width: 0; padding: 11px; }
.article-account-target.el-card.is-picked { border-color: var(--pine); background: var(--pine-soft); }
.article-account-target.el-card.is-checking { border-color: #94b5f1; }
.article-account-target-choice { display: flex; align-items: center; gap: 9px; width: 100%; min-width: 0; min-height: 44px; height: auto; margin: 0; cursor: pointer; }
.article-account-target-choice :deep(.el-checkbox__input) { flex: 0 0 auto; }
.article-account-target-choice :deep(.el-checkbox__label) { display: flex; align-items: center; flex: 1 1 auto; gap: 9px; min-width: 0; padding-left: 9px; color: var(--text-primary); line-height: var(--lh-base); white-space: normal; }
.article-account-avatar { display: grid; flex: 0 0 34px; place-items: center; width: 34px; height: 34px; border-radius: 50%; color: #14875a; background: #e7f6ee; font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.article-account-avatar.el-avatar { display: flex; overflow: hidden; }
.article-account-avatar.platform-logo { width: 34px; height: 34px; border-radius: 50%; }
.article-account-copy { display: grid; flex: 1 1 auto; gap: 4px; min-width: 0; }
.article-account-copy strong { overflow: hidden; color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.article-account-copy small { display: inline-flex; align-items: center; gap: 4px; color: var(--text-tertiary); font-size: var(--fs-caption); }
.article-account-copy small :deep(.el-icon) { flex: 0 0 auto; width: 14px; height: 14px; font-size: 14px; }
.article-account-copy small.account-status-checking { color: var(--pine); font-weight: 550; }
.article-account-copy small.account-status-connected, .article-account-copy small.account-status-verified { color: var(--success); font-weight: 550; }
.article-account-copy small.account-status-error { color: var(--danger); font-weight: 550; }
.article-publish-state { flex: 0 0 auto; min-height: 22px; padding: 0 8px; border-radius: 999px; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.article-publish-state.el-tag { height: auto; border-color: transparent; line-height: 1.5; }
.is-published { color: #176b4b; background: #eaf6ef; }
.is-idle { color: #697586; background: #f0f2f5; }
.is-pending { color: #815d16; background: #fff4dc; }
.is-failed { color: #a33c33; background: #fff0ee; }
.is-unknown { color: #845c13; background: #fff1cd; }
.article-account-result { display: grid; gap: 7px; margin: 0; padding-top: 8px; border-top: 1px solid var(--line); }
.article-account-result > p, .fan-notification-result > span:last-child { margin: 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.55; overflow-wrap: anywhere; }
.fan-notification-result { display: flex; align-items: flex-start; gap: 8px; }
.article-publish-result-message, .article-platform-caution { margin: 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.55; }
.article-platform-caution { color: #86641f; }
.article-remote-link { color: var(--pine); font-size: var(--fs-caption); text-decoration: none; }
.result-refresh-button { justify-self: start; padding: 0; font-size: var(--fs-caption); }
.article-no-account { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px; border: 1px dashed var(--line); border-radius: 8px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.article-no-account a { color: var(--pine); text-decoration: none; }
.platform-fields-section .form-label { margin: 2px 0 -5px; }
.toutiao-cover-mode { margin-top: -3px; }
.toutiao-cover-slots { display: grid; gap: 10px; }
.toutiao-cover-slot { display: grid; grid-template-columns: minmax(108px, 150px) minmax(0, 1fr); align-items: center; gap: 12px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; }
.toutiao-cover-slot img, .toutiao-cover-empty { width: 100%; aspect-ratio: 16 / 9; border-radius: 6px; object-fit: cover; }
.toutiao-cover-empty { display: grid; place-content: center; color: var(--text-tertiary); background: #f1f3f6; font-size: var(--fs-caption); line-height: 1.6; text-align: center; }
.toutiao-cover-slot-copy { display: grid; gap: 6px; min-width: 0; }
.toutiao-cover-slot-copy strong { color: var(--ink); font-size: var(--fs-body-sm); }
.toutiao-cover-slot-copy small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.toutiao-cover-slot-copy .article-manual-actions { align-items: center; margin-top: 0; }
.toutiao-options-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.toutiao-option-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.toutiao-option-row > span { color: var(--text-tertiary); font-size: var(--fs-caption); white-space: nowrap; }
.toutiao-declarations { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; }
.toutiao-declarations :deep(.el-checkbox) { height: auto; margin-right: 0; white-space: normal; }
.toutiao-account-options { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.toutiao-account-options > div { display: grid; gap: 4px; }
.toutiao-account-options strong { font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.toutiao-account-options span { color: var(--text-tertiary); font-size: var(--fs-caption); }
.platform-validation-note { margin: 0; padding: 10px 12px; border-radius: 7px; color: var(--danger); background: #fff1ef; font-size: var(--fs-caption); line-height: 1.55; }
.required-mark { color: #c34d43; }
.optional-label { margin-left: 4px; color: var(--text-tertiary); font-size: var(--fs-caption); font-weight: var(--fw-regular); }
.article-publish-cover-setting { display: flex; align-items: center; gap: 10px; padding: 11px; border: 1px solid var(--line); border-radius: 8px; background: #fafbfd; }
.cover-setting-icon { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; color: #386ecc; background: #e8f0ff; }
.article-publish-cover-setting > span:nth-child(2) { display: grid; flex: 1; gap: 4px; }
.article-publish-cover-setting strong { font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.article-publish-cover-setting small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.article-publish-cover-setting a { color: var(--pine); font-size: var(--fs-caption); text-decoration: none; }
.fan-notification-setting { padding: 12px; border: 1px solid var(--line); border-radius: 9px; background: #fbfcfe; }
.fan-notification-setting.is-enabled { border-color: #b7d4c5; background: #f5fbf7; }
.fan-notification-toggle { display: flex; align-items: center; gap: 10px; }
.fan-notification-checkbox { flex: 1 1 auto; min-width: 0; margin: 0; }
.fan-notification-checkbox :deep(.el-checkbox__label) { min-width: 0; padding-left: 9px; white-space: normal; }
.fan-notification-copy { display: grid; gap: 4px; }
.fan-notification-toggle strong { color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.fan-notification-toggle small { color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.5; }
.default-off-tag { font-size: var(--fs-caption); }
.fan-notification-setting > p { margin: 9px 0 0 27px; color: #697586; font-size: var(--fs-caption); line-height: 1.6; }
.fan-notification-setting > p.notification-selection-hint { color: #946717; }
.article-platform-field-note, .platform-help-note { margin: 0; padding: 11px 12px; border-radius: 8px; color: #677387; background: #f5f7fa; font-size: var(--fs-caption); line-height: 1.65; }
.manual-avatar { color: #315fba; background: #eaf1ff; }
.article-manual-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 14px 0; border-top: 1px solid var(--line); }
.article-publish-footer { position: sticky; bottom: 8px; z-index: 5; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 13px 17px; border: 1px solid var(--line); border-radius: 11px; background: rgb(255 255 255 / 96%); box-shadow: 0 7px 25px rgb(24 39 75 / 12%); backdrop-filter: blur(12px); }
.article-publish-footer strong { color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.article-publish-footer p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.5; }
.article-publish-loading { padding: 30px; color: var(--muted); }
.article-publish-page :deep(.el-alert__title) { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); line-height: var(--lh-base); }
.article-publish-page :deep(.el-alert__description) { color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.article-publish-page :deep(.el-input.input), .article-publish-page :deep(.el-textarea.textarea) { min-height: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.article-publish-page :deep(.el-input.input .el-input__wrapper) { min-height: 40px; padding: 1px 13px; border-radius: 8px; background: var(--paper); box-shadow: 0 0 0 1px var(--line); }
.article-publish-page :deep(.el-input.input .el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--pine), 0 0 0 3px var(--pine-soft); }
.article-publish-page :deep(.el-input.input .el-input__inner), .article-publish-page :deep(.el-textarea.textarea .el-textarea__inner) { color: var(--text-primary); font-size: var(--fs-body-sm); }
.article-publish-page :deep(.el-textarea.textarea .el-textarea__inner) { min-height: 94px; padding: 12px 13px; border-radius: 8px; background: var(--paper); box-shadow: 0 0 0 1px var(--line); line-height: var(--lh-base); }
.article-publish-page :deep(.el-textarea.textarea .el-textarea__inner:focus) { box-shadow: 0 0 0 1px var(--pine), 0 0 0 3px var(--pine-soft); }
.article-publish-page :deep(.el-checkbox__label) { color: var(--text-primary); font-size: var(--fs-body-sm); }
.article-publish-page :deep(.el-link) { color: var(--pine); font-size: var(--fs-caption); }
.article-publish-page :deep(.el-link:hover) { color: var(--text-primary); }
.article-publish-page :deep(.text-button.el-link) { min-height: 36px; font-size: var(--fs-body-sm); }
.article-publish-page :deep(.article-finalize-reminder .el-alert__content) { display: flex; align-items: center; justify-content: space-between; flex: 1; gap: 14px; }
.article-publish-page :deep(.article-finalize-reminder .el-alert__title) { margin: 0; }
.article-publish-page :deep(.el-button.button) { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.article-publish-page :deep(.el-button.button-small) { min-height: 36px; font-size: var(--fs-body-sm); }
:global(.article-publish-confirm .el-message-box__message) { white-space: pre-line; }
@media (max-width: 1180px) { .article-publish-workspace { grid-template-columns: minmax(290px, .9fr) 170px minmax(330px, 1.1fr); } .article-platform-accounts-scroll, .platform-settings-scroll { padding-right: 13px; padding-left: 13px; } }
@media (max-width: 980px) { .article-publish-workspace { grid-template-columns: 1fr; height: auto; min-height: 0; } .article-publish-article { min-height: 430px; max-height: 560px; border-right: 0; border-bottom: 1px solid var(--line); } .article-platform-rail { display: block; overflow-x: auto; overflow-y: hidden; border-right: 0; border-bottom: 1px solid var(--line); } .platform-rail-heading { padding: 10px 12px 7px; } .platform-rail-list { display: flex; width: max-content; padding: 0 8px 9px; } .platform-rail-item { width: 158px; min-height: 48px; } .platform-rail-hint { display: none; } .article-platform-settings { min-height: 420px; } .article-platform-accounts-scroll { max-height: none; overflow: visible; } .platform-settings-scroll { overflow: visible; } }
@media (max-width: 640px) { .article-publish-toolbar { align-items: flex-start; } .article-publish-toolbar h1 { font-size: 20px; } .article-preview-scroll { padding: 16px; } .article-preview-heading h3 { font-size: 18px; } .platform-settings-header { align-items: flex-start; padding: 14px; } .platform-settings-description { padding-right: 14px; padding-left: 14px; } .article-platform-accounts-scroll, .platform-settings-scroll { padding-right: 14px; padding-left: 14px; } .article-account-target-choice { flex-wrap: wrap; } .article-account-copy { min-width: 120px; } .article-publish-footer { align-items: stretch; flex-direction: column; } .article-publish-footer .button { width: 100%; } .article-finalize-reminder { align-items: flex-start; flex-direction: column; } }
@media (max-width: 640px) { .toutiao-options-grid, .toutiao-declarations, .toutiao-account-options { grid-template-columns: 1fr; } .toutiao-cover-slot { grid-template-columns: 104px minmax(0, 1fr); gap: 9px; padding: 8px; } }
@media (max-width: 640px) { .article-account-target-list { grid-template-columns: minmax(0, 1fr); } }
</style>
