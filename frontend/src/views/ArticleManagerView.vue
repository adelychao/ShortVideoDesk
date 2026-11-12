<script setup lang="ts">
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElAlert, ElButton, ElConfigProvider, ElDialog, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTabPane, ElTabs, ElTag } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/pagination/style/css'
import 'element-plus/es/components/table/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tag/style/css'
import { ApiError, api } from '../api/client'
import type { ArticleListItem, ArticleSourceScript } from '../api/types'

const router = useRouter()
const items = ref<ArticleListItem[]>([])
const total = ref(0)
const pageSize = ref(10)
const currentPage = ref(1)
const query = ref('')
const platform = ref('')
const status = ref('all')
const loading = ref(false)
const creating = ref(false)
const deletingArticleId = ref('')
const error = ref('')
const cleanupWarning = ref('')
const sourceDialogOpen = ref(false)
const sourceScripts = ref<ArticleSourceScript[]>([])
const sourceQuery = ref('')
const sourceLoading = ref(false)
const sourceBusyId = ref('')
const sourceError = ref('')
const deleteDialogOpen = ref(false)
const articleToDelete = ref<ArticleListItem | null>(null)
const authorizationDialogOpen = ref(false)
const scriptToGenerate = ref<ArticleSourceScript | null>(null)

const statusTabs = [
  { id: 'all', label: '全部文章' },
  { id: 'draft', label: '草稿' },
  { id: 'finalized', label: '定稿' },
  { id: 'published', label: '已发布' },
]
const platforms = [
  ['wechat', '微信公众号'], ['csdn', 'CSDN'], ['zhihu', '知乎'], ['xiaohongshu', '小红书'],
  ['toutiao', '今日头条'], ['baijiahao', '百家号'], ['weibo', '微博'],
] as const
const resultSummary = computed(() => `共 ${total.value} 篇`)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const pageStart = computed(() => (items.value.length ? (currentPage.value - 1) * pageSize.value + 1 : 0))
const pageEnd = computed(() => Math.min((currentPage.value - 1) * pageSize.value + items.value.length, total.value))
const paginationBusy = computed(() => loading.value || !!deletingArticleId.value)
const filters = computed(() => ({ query: query.value, status: status.value, platform: platform.value || undefined }))
const filterKey = computed(() => JSON.stringify(filters.value))
let appliedFilterKey: string | undefined

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '文章管理请求失败，请稍后重试。'
}

let requestSequence = 0

async function load(page = currentPage.value, requestedPageSize = pageSize.value): Promise<void> {
  const requestedFilters = filters.value
  const requestedFilterKey = filterKey.value
  const requestedPage = requestedFilterKey === appliedFilterKey ? Math.max(1, Math.floor(page)) : 1
  const sequence = ++requestSequence
  loading.value = true
  error.value = ''
  try {
    const result = await api.listArticles({
      ...requestedFilters,
      limit: requestedPageSize,
      offset: (requestedPage - 1) * requestedPageSize,
    })
    if (sequence !== requestSequence) return
    const lastPage = Math.max(1, Math.ceil(result.total / requestedPageSize))
    if (requestedPage > lastPage) {
      await load(lastPage, requestedPageSize)
      return
    }
    items.value = result.items
    total.value = result.total
    currentPage.value = requestedPage
    pageSize.value = requestedPageSize
    appliedFilterKey = requestedFilterKey
  } catch (cause) {
    if (sequence === requestSequence) error.value = errorMessage(cause)
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

async function createArticle(): Promise<void> {
  if (loading.value || creating.value) return
  creating.value = true
  error.value = ''
  try {
    await router.push({ name: 'article-new' })
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    creating.value = false
  }
}

async function openSourceDialog(): Promise<void> {
  sourceDialogOpen.value = true
  sourceError.value = ''
  await loadSourceScripts()
}

async function loadSourceScripts(): Promise<void> {
  sourceLoading.value = true
  sourceError.value = ''
  try {
    sourceScripts.value = (await api.listArticleSourceScripts(sourceQuery.value)).items
  } catch (cause) {
    sourceError.value = errorMessage(cause)
  } finally {
    sourceLoading.value = false
  }
}

function requestGenerateFromScript(script: ArticleSourceScript): void {
  if (sourceBusyId.value) return
  scriptToGenerate.value = script
  authorizationDialogOpen.value = true
}

async function generateFromScript(): Promise<void> {
  const script = scriptToGenerate.value
  if (!script || sourceBusyId.value) return
  authorizationDialogOpen.value = false
  sourceBusyId.value = script.artifact_id
  sourceError.value = ''
  try {
    const article = await api.createArticleFromScript(script.artifact_id, true)
    sourceDialogOpen.value = false
    await router.push({ name: 'article-edit', params: { articleId: article.id } })
  } catch (cause) {
    sourceError.value = errorMessage(cause)
  } finally {
    sourceBusyId.value = ''
  }
}

function openEditor(articleId: string): void {
  void router.push({ name: 'article-edit', params: { articleId } })
}

function openPublisher(articleId: string): void {
  void router.push({ name: 'article-publish', params: { articleId } })
}

function openPublicationProgress(articleId: string): void {
  void router.push({ name: 'article-publish-task', params: { articleId } })
}

function requestDeleteArticle(item: ArticleListItem): void {
  if (deletingArticleId.value) return
  articleToDelete.value = item
  deleteDialogOpen.value = true
}

async function deleteArticle(): Promise<void> {
  const item = articleToDelete.value
  if (!item || deletingArticleId.value) return
  deleteDialogOpen.value = false
  deletingArticleId.value = item.id
  error.value = ''
  cleanupWarning.value = ''
  try {
    const result = await api.deleteArticle(item.id)
    cleanupWarning.value = result.cleanup_warning ?? ''
    if (items.value.some((row) => row.id === item.id)) {
      items.value = items.value.filter((row) => row.id !== item.id)
      total.value = Math.max(0, total.value - 1)
      currentPage.value = Math.min(currentPage.value, pageCount.value)
    }
    const refreshPage = loading.value ? 1 : currentPage.value
    if (queryTimer) clearTimeout(queryTimer)
    queryTimer = undefined
    await load(refreshPage)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    deletingArticleId.value = ''
  }
}

function dateLabel(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function stateLabel(item: ArticleListItem): string {
  if (Object.values(item.publication_statuses).includes('partial')) return '部分发布'
  if (item.published_platforms.length) return '已发布'
  return item.status === 'finalized' ? '已定稿' : '草稿'
}

function stateClass(item: ArticleListItem): string {
  if (Object.values(item.publication_statuses).includes('partial')) return 'article-status-partial'
  if (item.published_platforms.length) return 'article-status-published'
  return item.status === 'finalized' ? 'article-status-finalized' : 'article-status-draft'
}

function platformState(statusValue: string | undefined): string {
  return ({ draft_saved: '草稿箱', published: '已发布', publishing: '发布中', unknown: '待核实', failed: '失败', partial: '部分成功', manual_required: '待手动发布', manual_published: '已发布' } as Record<string, string>)[statusValue || ''] || ''
}

// 列表工具栏：输入防抖搜索，回车或点击搜索立即提交。
let queryTimer: ReturnType<typeof setTimeout> | undefined
function scheduleFilterLoad(delay = 0): void {
  if (queryTimer) clearTimeout(queryTimer)
  requestSequence += 1
  loading.value = true
  queryTimer = setTimeout(() => { queryTimer = undefined; void load(1) }, delay)
}

function searchNow(): void {
  if (queryTimer) clearTimeout(queryTimer)
  queryTimer = undefined
  void load(1)
}

function changePageSize(requestedPageSize: number): void {
  if (requestedPageSize !== pageSize.value) void load(1, requestedPageSize)
}

function resetFilters(): void {
  query.value = ''
  platform.value = ''
  status.value = 'all'
  scheduleFilterLoad()
}

watch(query, () => {
  scheduleFilterLoad(250)
})
watch([status, platform], () => scheduleFilterLoad())
onActivated(() => { void load(currentPage.value) })
</script>

<template>
  <el-config-provider :locale="zhCn">
  <section class="article-manager-page" aria-label="文章管理">
    <section class="panel article-list-panel" aria-label="文章历史列表">
      <form class="toolbar list-toolbar article-toolbar" aria-label="文章检索与操作" @submit.prevent="searchNow">
        <div class="list-toolbar-filters">
          <div class="tb-search">
            <label class="tb-label" for="article-keyword">关键词</label>
            <el-input id="article-keyword" v-model="query" class="article-keyword" type="search" aria-label="搜索文章标题" placeholder="搜索文章标题">
              <template #prefix><svg class="article-search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.5"/><path d="m16 16 4.5 4.5"/></svg></template>
            </el-input>
          </div>
          <div class="tb-field">
            <label class="tb-label" for="article-platform">平台</label>
            <el-select id="article-platform" v-model="platform" class="article-platform-select" aria-label="按平台筛选" placeholder="全部平台" popper-class="article-platform-dropdown">
              <el-option label="全部平台" value="" />
              <el-option v-for="[id, label] in platforms" :key="id" :label="label" :value="id" />
            </el-select>
          </div>
          <el-button type="primary" native-type="submit" :disabled="creating">搜索</el-button>
          <el-button native-type="button" :disabled="creating || (!query && !platform && status === 'all' && currentPage === 1)" @click="resetFilters">重置</el-button>
        </div>
        <div class="list-toolbar-actions">
          <el-button :disabled="loading || creating" @click="openSourceDialog">从口播稿生成</el-button>
          <el-button type="primary" :disabled="loading || creating" @click="createArticle">＋ 新建文章</el-button>
        </div>
      </form>

      <div class="article-list-tabbar">
        <el-tabs v-model="status" class="article-status-tabs" aria-label="文章状态筛选">
          <el-tab-pane v-for="tab in statusTabs" :key="tab.id" :label="tab.label" :name="tab.id" />
        </el-tabs>
        <span class="article-list-count">{{ resultSummary }}</span>
      </div>

      <el-alert v-if="error" class="article-feedback" type="error" :closable="false" show-icon :title="error" />
      <el-alert v-if="cleanupWarning" class="article-feedback" type="warning" :closable="false" show-icon :title="cleanupWarning" />
      <div v-if="loading && !items.length" class="article-empty-state" role="status">正在读取文章列表…</div>
      <el-empty v-else-if="!items.length && !error" :description="query || platform || status !== 'all' ? '没有符合条件的文章，请调整筛选条件后重试。' : '还没有文章，从已定稿口播稿生成或新建空白文章开始编辑。'">
        <el-button @click="openSourceDialog">从口播稿生成</el-button>
      </el-empty>
      <div v-else-if="items.length" class="article-table-scroll">
        <el-table :data="items" row-key="id" class="article-table" aria-label="文章历史列表">
          <el-table-column label="文章标题" min-width="210">
            <template #default="{ row }">
              <el-button class="article-title-link" text :disabled="deletingArticleId === row.id" @click="openEditor(row.id)">{{ row.title || '未命名文章' }}</el-button>
              <span class="article-version">v{{ row.version }}</span>
            </template>
          </el-table-column>
          <el-table-column label="来源口播稿" min-width="180">
            <template #default="{ row }"><span class="article-source-title">{{ row.source_title || '手动新建' }}</span><span class="article-summary">{{ row.summary || '暂无摘要' }}</span></template>
          </el-table-column>
          <el-table-column label="平台状态" min-width="150">
            <template #default="{ row }">
              <div v-if="Object.keys(row.publication_statuses).length" class="article-platform-badges">
                <el-tag v-for="[platformId, state] in Object.entries(row.publication_statuses)" :key="platformId" class="article-platform-state" effect="plain" type="info">{{ platforms.find(([id]) => id === platformId)?.[1] || platformId }} · {{ platformState(state as string) }}</el-tag>
              </div>
              <span v-else class="muted-cell">尚未发布</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" min-width="95">
            <template #default="{ row }"><el-tag class="article-status" :class="stateClass(row as ArticleListItem)" effect="light">{{ stateLabel(row as ArticleListItem) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="最近修改" min-width="130">
            <template #default="{ row }"><span class="article-date-cell">{{ dateLabel(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" min-width="300">
            <template #default="{ row }">
              <div class="article-row-actions">
                <el-button size="small" :disabled="deletingArticleId === row.id" @click="openEditor(row.id)">编辑</el-button>
                <el-button v-if="Object.keys(row.publication_statuses).length" size="small" :disabled="deletingArticleId === row.id" :aria-label="`查看${row.title || '未命名文章'}的发布进度`" @click="openPublicationProgress(row.id)">发布进度</el-button>
                <el-button size="small" type="primary" :disabled="deletingArticleId === row.id" @click="openPublisher(row.id)">去发布</el-button>
                <el-button size="small" type="danger" text :disabled="loading || !!deletingArticleId" @click="requestDeleteArticle(row as ArticleListItem)">{{ deletingArticleId === row.id ? '删除中…' : '删除' }}</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div v-if="total > 0" class="article-pagination" aria-label="文章列表分页">
        <span class="article-pagination-summary"><template v-if="items.length">显示 {{ pageStart }}–{{ pageEnd }} 篇，</template>共 {{ total }} 篇</span>
        <div class="article-pagination-controls">
          <label class="article-page-size" for="article-page-size"><span>每页</span></label>
          <el-select id="article-page-size" :model-value="pageSize" class="article-page-size-select" aria-label="每页文章数" :disabled="paginationBusy" popper-class="article-platform-dropdown" @change="changePageSize">
            <el-option :value="10" label="10 篇" /><el-option :value="20" label="20 篇" /><el-option :value="50" label="50 篇" />
          </el-select>
          <el-pagination :current-page="currentPage" :page-size="pageSize" :total="total" :pager-count="5" :disabled="paginationBusy" layout="prev, pager, next" prev-text="上一页" next-text="下一页" @current-change="load" />
        </div>
      </div>
      <p class="article-list-footnote"><span aria-hidden="true">ⓘ</span> 保存会生成新版本，原有文章版本会保留。</p>
    </section>

    <el-dialog v-model="sourceDialogOpen" class="article-source-dialog" title="选择口播稿定稿" :teleported="false">
      <p class="source-dialog-description">只显示已审核并设为定稿的版本。</p>
      <form class="source-script-search" @submit.prevent="loadSourceScripts">
        <label class="tb-label" for="source-script-query">搜索口播稿</label>
        <el-input id="source-script-query" v-model="sourceQuery" type="search" placeholder="搜索视频或稿件标题" />
        <el-button native-type="submit" :disabled="sourceLoading">搜索</el-button>
      </form>
      <el-alert v-if="sourceError" class="article-feedback" type="error" :closable="false" show-icon :title="sourceError" />
      <div v-if="sourceLoading" class="article-empty-state" role="status">正在读取定稿…</div>
      <el-empty v-else-if="!sourceScripts.length" description="没有找到可用于文章生成的定稿口播稿。" />
      <ul v-else class="source-script-list">
        <li v-for="script in sourceScripts" :key="script.artifact_id">
          <div><strong>{{ script.title }}</strong><span>{{ script.source_title || '本地任务' }} · v{{ script.version }} · {{ dateLabel(script.created_at) }}</span></div>
          <el-button type="primary" size="small" :disabled="!!sourceBusyId" @click="requestGenerateFromScript(script)">{{ sourceBusyId === script.artifact_id ? '正在生成…' : '生成文章' }}</el-button>
        </li>
      </ul>
      <p class="helper source-dialog-help">本次会把所选定稿发送给当前任务使用的文本模型；模型费用未知。发送前会再次确认。</p>
    </el-dialog>
    <el-dialog v-model="deleteDialogOpen" class="article-confirm-dialog" title="删除文章" :teleported="false">
      <p class="article-confirm-text">确定删除「{{ articleToDelete?.title || '未命名文章' }}」吗？本机文章、版本、发布记录和本地图片将被删除；平台上已发布的内容会保留。</p>
      <template #footer>
        <el-button @click="deleteDialogOpen = false">取消</el-button>
        <el-button type="danger" @click="deleteArticle">确认删除</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="authorizationDialogOpen" class="article-confirm-dialog" title="确认模型调用" :teleported="false">
      <p class="article-confirm-text">将「{{ scriptToGenerate?.source_title || '任务' }} · {{ scriptToGenerate?.title }} v{{ scriptToGenerate?.version }}」发送给本机当前配置的文本模型，生成可编辑文章。费用未知。确认授权本次调用？</p>
      <template #footer>
        <el-button @click="authorizationDialogOpen = false">取消</el-button>
        <el-button type="primary" @click="generateFromScript">确认授权并生成</el-button>
      </template>
    </el-dialog>
  </section>
  </el-config-provider>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类覆盖(全量落地时上移 styles.css)—— */
.panel-head h2 { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }

/* 排版系统 v2：将组件库语义变量映射到工作台令牌。 */
.article-manager-page {
  display: grid; gap: 10px;
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
.article-toolbar :deep(.el-button) { height: 36px; margin-left: 0; font-size: var(--fs-body); font-weight: var(--fw-medium); }
.article-toolbar :deep(.el-input__wrapper), .article-toolbar :deep(.el-select__wrapper) { min-height: 36px; }
.article-keyword { width: min(300px, 28vw); min-width: 190px; }
.article-platform-select { width: 150px; }
.article-search-icon { width: 16px; height: 16px; fill: none; stroke: var(--text-tertiary); stroke-width: 1.8; }
.article-row-actions :deep(.el-button) { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.article-row-actions :deep(.el-button + .el-button) { margin-left: 0; }
.article-manager-page :deep(.article-source-dialog) { width: min(650px, calc(100vw - 32px)); max-height: min(720px, 90vh); overflow: auto; border-radius: 14px; }
.article-manager-page :deep(.el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.article-feedback { margin-top: 12px; }
.article-manager-page :deep(.article-confirm-dialog) { width: min(520px, calc(100vw - 32px)); border-radius: 10px; }
.article-confirm-text { margin: 0; color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); overflow-wrap: anywhere; }
.source-dialog-description { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.source-dialog-help { margin-top: 15px; }
.source-script-search { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.source-script-search .el-input { flex: 1 1 220px; min-width: 0; }
.article-row-actions { display: flex; flex-wrap: nowrap; gap: 8px; }
.article-list-panel { padding: 18px; overflow: hidden; }
.article-toolbar { margin: 0 0 2px; }
.article-list-tabbar { display: flex; align-items: center; gap: 16px; margin: 20px 0 0; min-width: 0; }
.article-status-tabs { flex: 1; min-width: 0; }
.article-status-tabs :deep(.el-tabs__header) { margin: 0; }
.article-status-tabs :deep(.el-tabs__item) { font-size: var(--fs-body); }
.article-list-count { margin-left: auto; color: var(--text-tertiary); font-size: var(--fs-body-sm); white-space: nowrap; font-variant-numeric: tabular-nums; }
.article-table-scroll { min-width: 0; overflow: auto; }
.article-table { min-width: 985px; --el-table-header-bg-color: var(--paper-2); --el-table-border-color: var(--line); --el-table-text-color: var(--text-secondary); --el-table-header-text-color: var(--text-secondary); }
.article-table :deep(th) { font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.article-table :deep(td) { font-size: var(--fs-body); }
.article-title-link { max-width: 100%; height: auto; padding: 0; color: var(--text-primary); text-align: left; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); white-space: normal; }
.article-title-link:hover { color: var(--pine); }
.article-version { display: block; margin: 4px 0 0 2px; color: var(--text-tertiary); font-size: var(--fs-caption); font-weight: var(--fw-regular); font-variant-numeric: tabular-nums; }
.article-source-title, .article-summary { display: block; max-width: 230px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.article-source-title { color: var(--text-primary); }
.article-summary, .muted-cell { margin-top: 4px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.article-platform-badges { display: grid; gap: 4px; }
.article-platform-state { width: fit-content; max-width: 100%; color: var(--text-secondary); font-size: var(--fs-caption); white-space: nowrap; }
.article-status { min-height: 24px; padding: 0 10px; border: 0; border-radius: 999px; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.article-status-draft { color: var(--text-secondary); background: var(--paper-2); }
.article-status-finalized { color: var(--pine); background: var(--pine-soft); }
.article-status-published { color: var(--success); background: var(--success-soft); }
.article-status-partial { color: var(--warning); background: var(--warning-soft); }
.article-date-cell { color: var(--text-tertiary); white-space: nowrap; font-variant-numeric: tabular-nums; }
.article-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 2px 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.article-pagination-controls { display: flex; align-items: center; gap: 6px; }
.article-page-size { white-space: nowrap; }
.article-page-size-select { width: 90px; }
.article-pagination-controls :deep(.el-pagination) { --el-pagination-font-size: var(--fs-body); }
.article-list-footnote { display: flex; align-items: center; gap: 7px; margin: 15px 4px 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.article-empty-state { display: grid; justify-items: center; gap: 8px; padding: 48px 20px; color: var(--text-tertiary); text-align: center; }
.source-script-search { margin: 12px 0; }
.source-script-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.source-script-list li { display: flex; justify-content: space-between; align-items: center; gap: 15px; padding: 12px; border: 1px solid var(--line); border-radius: 9px; }
.source-script-list li div { display: grid; gap: 4px; min-width: 0; }
.source-script-list li strong { overflow: hidden; color: var(--ink); text-overflow: ellipsis; white-space: nowrap; }
.source-script-list li span { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
@media (max-width: 720px) { .article-toolbar .tb-search { flex: 1 1 100%; } .article-keyword { flex: 1; width: 100%; min-width: 0; } .article-toolbar :deep(.el-button) { flex: 1 0 auto; } .article-list-panel { padding: 12px; } .article-list-tabbar { margin-top: 16px; } }
@media (max-width: 680px) { .article-pagination { align-items: flex-start; flex-direction: column; } .article-pagination-controls { width: 100%; flex-wrap: wrap; } }
</style>

<style>
/* 排版系统 v2：下拉菜单挂载到 body，单独继承工作台主题令牌。 */
.article-platform-dropdown {
  --el-color-primary: var(--pine);
  --el-color-primary-light-9: var(--pine-soft);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
}
</style>
