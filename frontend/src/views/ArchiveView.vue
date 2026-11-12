<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElAlert, ElButton, ElConfigProvider, ElDatePicker, ElDialog, ElEmpty, ElInput, ElMessage, ElMessageBox, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTag, ElTooltip } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/date-picker/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/pagination/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/table/style/css'
import 'element-plus/es/components/tag/style/css'
import 'element-plus/es/components/tooltip/style/css'
import { RouterLink } from 'vue-router'
import { ApiError, api } from '../api/client'
import type { TaskDeletionItem, TaskDeletionPreview, TaskStatus, TaskSummary } from '../api/types'

const items = ref<TaskSummary[]>([])
const coverFailed = ref(new Set<string>())
const titleOverflow = ref(new Set<string>())
const total = ref(0)
const pageSize = ref(10)
const currentPage = ref(1)
const loading = ref(false)
const error = ref('')
const showRemoved = ref(false)
const actionMessage = ref('')
const actionBusy = ref(false)
const folderBusyId = ref<string | null>(null)
const deleteDialogOpen = ref(false)
const deletePreview = ref<TaskDeletionPreview | null>(null)
const deletePreviewLoading = ref(false)
const deleteError = ref('')
const stopMessage = ref('')
const stopRequested = ref(false)
const deleting = ref(false)
const deleteResultMessage = ref('')
const dialogTaskId = ref('')
const searchQuery = ref('')
const status = ref<TaskStatus | ''>('')
const platform = ref('')
const dateRange = ref<string[] | null>(null)
const sort = ref<'newest' | 'oldest' | 'title'>('newest')
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let requestSequence = 0

const dateFrom = computed(() => dateRange.value?.[0] ?? '')
const dateTo = computed(() => dateRange.value?.[1] ?? '')
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const visiblePages = computed(() => {
  const start = Math.max(1, Math.min(currentPage.value - 2, pageCount.value - 4))
  const end = Math.min(pageCount.value, start + 4)
  return Array.from({ length: end - start + 1 }, (_, index) => start + index)
})
const pageStart = computed(() => (total.value ? (currentPage.value - 1) * pageSize.value + 1 : 0))
const pageEnd = computed(() => Math.min(currentPage.value * pageSize.value, total.value))
const hasArchiveFilters = computed(() => Boolean(searchQuery.value || status.value || platform.value || dateFrom.value || dateTo.value || sort.value !== 'newest'))

const statusOptions: Array<{ value: TaskStatus | ''; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '处理中' },
  { value: 'interrupted', label: '已中断' },
  { value: 'awaiting_authorization', label: '等待图片授权' },
  { value: 'completed', label: '已完成' },
  { value: 'partial', label: '部分完成' },
  { value: 'failed', label: '失败' },
  { value: 'blocked', label: '受阻' },
  { value: 'cancelled', label: '已取消' },
]

const platformOptions = [
  { value: '', label: '全部平台' },
  { value: 'douyin', label: '抖音' },
  { value: 'bilibili', label: 'B 站' },
  { value: 'local', label: '本地导入' },
]

function statusLabel(value: string): string {
  return statusOptions.find((option) => option.value === value)?.label ?? '未知状态'
}

function statusType(value: string): 'success' | 'warning' | 'danger' | 'info' {
  if (['failed', 'blocked'].includes(value)) return 'danger'
  if (['queued', 'running', 'interrupted', 'partial', 'cancelled', 'awaiting_authorization'].includes(value)) return 'warning'
  if (value === 'completed') return 'success'
  return 'info'
}

function platformLabel(value: string | null, sourceKind: string): string {
  if (sourceKind === 'file') return '本地导入'
  if (value === 'douyin') return '抖音'
  if (value === 'bilibili') return 'B 站'
  return value || '未知平台'
}

function formatDate(value: string | null): string {
  if (!value) return '尚未归档'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '日期未知'
  return parsed.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

function coverFailedFor(taskId: string): boolean {
  return coverFailed.value.has(taskId)
}

function titleOf(item: TaskSummary): string {
  return item.asset.title || item.asset.source_id || '未命名素材'
}

function checkTitleOverflow(item: TaskSummary, event: MouseEvent): void {
  const el = event.currentTarget as HTMLElement | null
  if (!el) return
  const next = new Set(titleOverflow.value)
  if (el.scrollWidth > el.clientWidth) next.add(item.task.id)
  else next.delete(item.task.id)
  titleOverflow.value = next
}

function markCoverFailed(taskId: string): void {
  const next = new Set(coverFailed.value)
  next.add(taskId)
  coverFailed.value = next
}

function completedLabel(summary: TaskSummary): string {
  if (!summary.completed_step_kinds.length) return '尚无完成步骤'
  const labels: Record<string, string> = {
    download: '下载', import: '导入', transcribe: '转写', ocr: 'OCR', comments: '评论',
    analyze_comments: '评论分析', analyze_structure: '结构分析', knowledge_cards: '知识卡',
    generate_scripts: '稿件', index: '索引', write_obsidian: '知识库',
  }
  return summary.completed_step_kinds.map((kind) => labels[kind] ?? kind).join(' · ')
}

function errorMessage(value: unknown): string {
  return value instanceof ApiError ? value.message : '暂时无法读取素材档案。'
}

function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let amount = value / 1024
  let unitIndex = 0
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024
    unitIndex += 1
  }
  return `${amount.toFixed(amount >= 100 ? 0 : 1)} ${units[unitIndex]}`
}

function switchRemovedView(): void {
  showRemoved.value = !showRemoved.value
  actionMessage.value = ''
  currentPage.value = 1
  void loadPage(1)
}

function resetDeleteDialog(): void {
  deletePreview.value = null
  deleteError.value = ''
  stopMessage.value = ''
  stopRequested.value = false
  deleteResultMessage.value = ''
}

function closeDeleteDialog(): void {
  if (actionBusy.value || deleting.value) return
  deleteDialogOpen.value = false
  resetDeleteDialog()
}

async function removeFromArchive(item: TaskSummary): Promise<void> {
  const title = item.asset.title || item.asset.source_id || '未命名素材'
  try {
    await ElMessageBox.confirm(
      `从档案移除「${title}」？视频、任务结果和其他产出都会保留，可在“已移除记录”中恢复。`,
      '移除档案记录',
      { confirmButtonText: '确认移除', cancelButtonText: '取消', confirmButtonType: 'danger', type: 'warning', customClass: 'archive-remove-confirm' },
    )
  } catch {
    return
  }
  actionBusy.value = true
  actionMessage.value = ''
  try {
    await api.removeTaskFromArchive(item.task.id)
    actionMessage.value = '已从档案移除；文件与任务产出仍保留。'
    await loadPage(Math.min(currentPage.value, pageCount.value))
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionBusy.value = false
  }
}

async function restoreToArchive(item: TaskSummary): Promise<void> {
  actionBusy.value = true
  actionMessage.value = ''
  try {
    await api.restoreTaskToArchive(item.task.id)
    actionMessage.value = '记录已恢复到素材档案。'
    await loadPage(Math.min(currentPage.value, pageCount.value))
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    actionBusy.value = false
  }
}

async function openArchiveFolder(item: TaskSummary): Promise<void> {
  if (folderBusyId.value) return
  folderBusyId.value = item.task.id
  try {
    await api.openTaskFolder(item.task.id)
    ElMessage.success({ message: '已在文件管理器中打开归档目录。', grouping: true, duration: 2500 })
  } catch (cause) {
    ElMessage.error({ message: errorMessage(cause), grouping: true, duration: 4000 })
  } finally {
    folderBusyId.value = null
  }
}

async function beginPermanentDelete(item: TaskSummary): Promise<void> {
  if (deletePreviewLoading.value) return
  dialogTaskId.value = item.task.id
  deletePreviewLoading.value = true
  deleteError.value = ''
  stopMessage.value = ''
  stopRequested.value = false
  deleteResultMessage.value = ''
  deletePreview.value = null
  deleteDialogOpen.value = true
  try {
    deletePreview.value = await api.getTaskDeletionPreview(item.task.id)
  } catch (cause) {
    deleteError.value = errorMessage(cause)
  } finally {
    deletePreviewLoading.value = false
  }
}

async function stopOrRefreshTaskBeforeDelete(): Promise<void> {
  if (!deletePreview.value?.can_stop || actionBusy.value || deleting.value) return
  actionBusy.value = true
  deleteError.value = ''
  stopMessage.value = ''
  try {
    if (!stopRequested.value) {
      await api.cancelTask(dialogTaskId.value)
      stopRequested.value = true
    }
    deletePreview.value = await api.getTaskDeletionPreview(dialogTaskId.value)
    stopMessage.value = deletePreview.value.blocked_reason
      ? '停止请求已发送。若当前步骤无法立即中断，完成后可再次刷新预览。'
      : '任务已停止，产出清单已更新；请核对后确认删除。'
  } catch (cause) {
    deleteError.value = errorMessage(cause)
  } finally {
    actionBusy.value = false
  }
}

async function openPreviewPath(item: TaskDeletionItem, reveal: boolean): Promise<void> {
  if ((!item.can_open && !item.can_reveal) || !dialogTaskId.value) return
  actionBusy.value = true
  deleteError.value = ''
  try {
    await api.openTaskOutput(dialogTaskId.value, item.relative_path, reveal)
  } catch (cause) {
    deleteError.value = errorMessage(cause)
  } finally {
    actionBusy.value = false
  }
}

async function confirmPermanentDelete(): Promise<void> {
  if (!deletePreview.value || deletePreview.value.blocked_reason || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    const result = await api.permanentlyDeleteTask(dialogTaskId.value, deletePreview.value.token)
    deleteResultMessage.value = `已删除任务记录、${result.deleted_files} 个文件和 ${result.deleted_comments} 条评论。`
    if (result.cleanup_pending.length) {
      deleteResultMessage.value += `有 ${result.cleanup_pending.length} 个文件待清理，请稍后检查归档目录。`
    }
    await loadPage(Math.min(currentPage.value, pageCount.value))
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 409) {
      try {
        deletePreview.value = await api.getTaskDeletionPreview(dialogTaskId.value)
        deleteError.value = '任务或文件引用刚刚发生变化，清单已更新。请重新核对后再删除。'
      } catch (refreshCause) {
        deleteError.value = errorMessage(refreshCause)
      }
    } else {
      deleteError.value = errorMessage(cause)
    }
  } finally {
    deleting.value = false
  }
}

async function loadPage(page = 1): Promise<void> {
  const requestedPage = Math.max(1, Math.floor(page))
  const sequence = ++requestSequence
  loading.value = true
  error.value = ''
  try {
    const result = await api.listTasks({
      limit: pageSize.value,
      offset: (requestedPage - 1) * pageSize.value,
      status: status.value,
      platform: platform.value,
      dateFrom: dateFrom.value,
      dateTo: dateTo.value,
      search: searchQuery.value,
      sort: sort.value,
      includeRemoved: showRemoved.value,
    })
    if (sequence !== requestSequence) return
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize.value))
    if (requestedPage > lastPage) {
      total.value = result.total
      currentPage.value = lastPage
      void loadPage(lastPage)
      return
    }
    items.value = result.items
    total.value = result.total
    currentPage.value = requestedPage
  } catch (cause) {
    if (sequence === requestSequence) error.value = errorMessage(cause)
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

function changePageSize(): void {
  currentPage.value = 1
  void loadPage(1)
}

function submitArchiveSearch(): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = undefined
  currentPage.value = 1
  void loadPage(1)
}

function resetArchiveFilters(): void {
  searchQuery.value = ''
  status.value = ''
  platform.value = ''
  dateRange.value = null
  sort.value = 'newest'
}

watch([searchQuery, status, platform, dateRange, sort], () => {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = undefined
  currentPage.value = 1
  refreshTimer = setTimeout(() => void loadPage(1), searchQuery.value ? 250 : 0)
})

onMounted(() => void loadPage(1))
onBeforeUnmount(() => {
  if (refreshTimer) clearTimeout(refreshTimer)
  requestSequence += 1
})
</script>

<template>
  <el-config-provider :locale="zhCn">
  <div class="archive-page">
    <section class="panel panel-pad" aria-labelledby="archive-list-title">
      <div class="panel-head archive-panel-head">
        <div>
          <h2 id="archive-list-title">{{ showRemoved ? '已移除记录' : '素材档案' }}</h2>
          <p>{{ showRemoved ? '这些记录只从档案列表隐藏，任务信息和本地文件仍然保留。' : '每条记录对应一次处理任务；同一素材的不同任务会分别保留。' }}</p>
        </div>
        <div class="archive-list-actions">
          <el-button :disabled="loading || actionBusy" @click="switchRemovedView">
            {{ showRemoved ? '返回素材档案' : '已移除记录' }}
          </el-button>
          <RouterLink class="tb-btn" to="/">开始整理素材</RouterLink>
          <span class="quiet-pill">{{ total }} 条记录</span>
        </div>
      </div>

      <form class="toolbar list-toolbar archive-toolbar" aria-label="素材档案检索" @submit.prevent="submitArchiveSearch">
        <div class="archive-filter-group">
          <label class="tb-search">
            <span class="tb-label">关键词</span>
            <el-input v-model="searchQuery" class="archive-keyword" type="search" :maxlength="200" aria-label="搜索标题、作品 ID 或任务 ID" placeholder="搜索标题、作品 ID 或任务 ID">
              <template #prefix><svg class="archive-search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4 4"/></svg></template>
            </el-input>
          </label>
          <label class="tb-field">
            <span class="tb-label">平台</span>
            <el-select v-model="platform" class="archive-filter-select" aria-label="按平台筛选" placeholder="全部平台" popper-class="archive-theme-popper">
              <el-option v-for="option in platformOptions" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </label>
          <label class="tb-field">
            <span class="tb-label">状态</span>
            <el-select v-model="status" class="archive-filter-select" aria-label="按状态筛选" placeholder="全部状态" popper-class="archive-theme-popper">
              <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </label>
          <label class="tb-field">
            <span class="tb-label">归档日期</span>
            <el-date-picker
              v-model="dateRange"
              type="daterange"
              class="archive-date-range-picker"
              value-format="YYYY-MM-DD"
              format="YYYY-MM-DD"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              unlink-panels
              aria-label="按归档日期范围筛选"
              popper-class="archive-theme-popper"
            />
          </label>
          <label class="tb-field">
            <span class="tb-label">排序</span>
            <el-select v-model="sort" class="archive-sort-select" aria-label="排序方式" popper-class="archive-theme-popper">
              <el-option label="日期：从新到旧" value="newest" />
              <el-option label="日期：从旧到新" value="oldest" />
              <el-option label="标题：A 到 Z" value="title" />
            </el-select>
          </label>
          <el-button type="primary" native-type="submit" :disabled="loading">
            {{ loading ? '搜索中…' : '搜索' }}
          </el-button>
          <el-button native-type="button" :disabled="loading" @click="resetArchiveFilters">重置</el-button>
        </div>
      </form>

      <el-alert v-if="error" class="archive-feedback" type="error" :closable="false" show-icon :title="error" />
      <el-alert v-if="actionMessage" class="archive-feedback archive-action-message" type="success" :closable="false" show-icon :title="actionMessage" />
      <p v-if="loading" class="helper" role="status">正在读取素材档案…</p>

      <template v-if="!loading && items.length">
        <div class="archive-table-wrap">
          <el-table :data="items" row-key="task.id" class="archive-table" aria-label="素材档案列表">
            <el-table-column label="素材" min-width="340">
              <template #default="{ row }">
                <RouterLink class="archive-title" :to="{ name: 'task-detail', params: { taskId: row.task.id } }"><img v-if="row.cover && !coverFailedFor(row.task.id)" class="archive-cover" :src="`${row.cover.file_url}?inline=1`" alt="" loading="lazy" @error="markCoverFailed(row.task.id)" /><span v-else class="archive-art" aria-hidden="true"></span><span><strong @mouseenter="checkTitleOverflow(row as TaskSummary, $event)"><el-tooltip :content="titleOf(row as TaskSummary)" placement="top" :show-after="400" :disabled="!titleOverflow.has(row.task.id)" popper-class="archive-theme-popper"><span>{{ titleOf(row as TaskSummary) }}</span></el-tooltip></strong><span>作者：{{ row.asset.author || '未获取' }}</span><span>作品 ID：{{ row.asset.source_id || '未获取' }}</span></span></RouterLink>
              </template>
            </el-table-column>
            <el-table-column label="平台" min-width="85"><template #default="{ row }">{{ platformLabel(row.asset.platform, row.asset.source_kind) }}</template></el-table-column>
            <el-table-column label="归档日期" min-width="150"><template #default="{ row }">{{ formatDate(row.asset.archived_at || row.asset.created_at) }}</template></el-table-column>
            <el-table-column label="已完成步骤" min-width="160"><template #default="{ row }">{{ completedLabel(row as TaskSummary) }}</template></el-table-column>
            <el-table-column label="任务状态" min-width="120"><template #default="{ row }"><el-tag :type="statusType(row.task.status)" effect="light">{{ statusLabel(row.task.status) }}</el-tag></template></el-table-column>
            <el-table-column label="操作" min-width="230" fixed="right">
              <template #default="{ row }">
                <div class="archive-row-actions">
                  <el-button text :disabled="folderBusyId !== null" @click="openArchiveFolder(row as TaskSummary)">{{ folderBusyId === row.task.id ? '正在打开…' : '归档目录' }}</el-button>
                  <el-button v-if="showRemoved" type="success" text :disabled="actionBusy" @click="restoreToArchive(row as TaskSummary)">恢复</el-button>
                  <el-button v-else text :disabled="actionBusy" @click="removeFromArchive(row as TaskSummary)">移除</el-button>
                  <el-button type="danger" text :disabled="actionBusy" @click="beginPermanentDelete(row as TaskSummary)">删除</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="mobile-records">
          <article v-for="item in items" :key="item.task.id" class="mobile-record">
            <div class="mobile-record-top">
              <RouterLink class="archive-title" :to="{ name: 'task-detail', params: { taskId: item.task.id } }">
                <img v-if="item.cover && !coverFailedFor(item.task.id)" class="archive-cover" :src="`${item.cover.file_url}?inline=1`" alt="" loading="lazy" @error="markCoverFailed(item.task.id)" />
                <span v-else class="archive-art" aria-hidden="true"></span>
                <span>
                  <strong @mouseenter="checkTitleOverflow(item, $event)"><el-tooltip :content="titleOf(item)" placement="top" :show-after="400" :disabled="!titleOverflow.has(item.task.id)" popper-class="archive-theme-popper"><span>{{ titleOf(item) }}</span></el-tooltip></strong>
                  <span>作者：{{ item.asset.author || '未获取' }}</span>
                  <span>作品 ID：{{ item.asset.source_id || '未获取' }}</span>
                </span>
              </RouterLink>
              <el-tag :type="statusType(item.task.status)" effect="light">{{ statusLabel(item.task.status) }}</el-tag>
            </div>
            <div class="mobile-record-meta">
              <span>{{ platformLabel(item.asset.platform, item.asset.source_kind) }}</span>
              <span>{{ formatDate(item.asset.archived_at || item.asset.created_at) }}</span>
              <span>{{ completedLabel(item) }}</span>
            </div>
            <div class="mobile-record-bottom">
              <span class="helper">任务 {{ item.task.id.slice(0, 8) }}</span>
              <div class="archive-row-actions">
                <el-button text :disabled="folderBusyId !== null" @click="openArchiveFolder(item)">{{ folderBusyId === item.task.id ? '正在打开…' : '归档目录' }}</el-button>
                <template v-if="showRemoved">
                  <el-button type="success" text :disabled="actionBusy" @click="restoreToArchive(item)">恢复</el-button>
                  <el-button type="danger" text :disabled="actionBusy" @click="beginPermanentDelete(item)">删除</el-button>
                </template>
                <template v-else>
                  <el-button text :disabled="actionBusy" @click="removeFromArchive(item)">移除</el-button>
                  <el-button type="danger" text :disabled="actionBusy" @click="beginPermanentDelete(item)">删除</el-button>
                </template>
              </div>
            </div>
          </article>
        </div>

        <div v-if="total > 0" class="archive-pagination" aria-label="素材档案分页">
          <span class="archive-pagination-summary">显示 {{ pageStart }}–{{ pageEnd }} 条，共 {{ total }} 条</span>
          <div class="archive-pagination-controls">
            <label class="archive-page-size" for="archive-page-size"><span>每页</span></label>
            <el-select id="archive-page-size" :model-value="pageSize" class="archive-page-size-select" aria-label="每页记录数" :disabled="loading" popper-class="archive-theme-popper" @change="changePageSize">
              <el-option :value="10" label="10 条" /><el-option :value="20" label="20 条" /><el-option :value="50" label="50 条" />
            </el-select>
            <el-pagination :current-page="currentPage" :page-size="pageSize" :total="total" :pager-count="5" :disabled="loading" layout="prev, pager, next" prev-text="上一页" next-text="下一页" @current-change="loadPage" />
          </div>
        </div>
      </template>

      <div v-else-if="!loading" class="archive-empty-state">
        <el-empty :description="hasArchiveFilters ? '调整关键词或筛选范围后再试。' : (showRemoved ? '从档案移除的任务会显示在这里，可随时恢复。' : '完成的下载和本地导入会按日期整理在这里。')">
          <template #image><span class="archive-empty-title">{{ hasArchiveFilters ? '没有符合筛选条件的记录' : (showRemoved ? '没有已移除记录' : '暂无可展示的档案') }}</span></template>
          <el-button v-if="hasArchiveFilters" @click="resetArchiveFilters">清除筛选</el-button>
          <el-button v-else-if="showRemoved" @click="switchRemovedView">返回素材档案</el-button>
          <RouterLink v-else class="tb-btn" to="/">返回素材采集 →</RouterLink>
        </el-empty>
      </div>
    </section>

    <el-dialog
      v-model="deleteDialogOpen"
      class="archive-delete-dialog"
      title="删除记录及产出物"
      aria-label="删除记录及产出物"
      width="min(1120px, calc(100vw - 32px))"
      :close-on-click-modal="!actionBusy && !deleting"
      :close-on-press-escape="!actionBusy && !deleting"
      :show-close="!actionBusy && !deleting"
      :teleported="false"
      @closed="resetDeleteDialog"
    >
      <template #header>
        <div class="archive-delete-header">
          <div>
            <h2>删除记录及产出物</h2>
            <p v-if="deletePreview">{{ deletePreview.title }} · 任务 {{ deletePreview.task_id.slice(0, 8) }}</p>
          </div>
        </div>
      </template>
      <div class="archive-delete-content">
        <p v-if="deletePreviewLoading" class="helper" role="status">正在核对任务产出和文件引用…</p>
        <el-alert v-if="deleteError && !deletePreview && !deletePreviewLoading" class="archive-feedback" type="error" :closable="false" show-icon :title="deleteError" />
        <template v-else-if="deletePreview && !deleteResultMessage">
          <el-alert
            class="archive-delete-summary"
            type="warning"
            :closable="false"
            :title="`将删除 ${deletePreview.delete_file_count} 个文件，预计释放 ${formatBytes(deletePreview.delete_bytes)}`"
            :description="`另有 ${deletePreview.keep_file_count} 个文件会保留；列表也包含数据库记录、评论和外部引用。`"
          />
          <div v-if="deletePreview.blocked_reason" class="archive-delete-blocked">
            <el-alert class="archive-feedback" type="error" :closable="false" show-icon :title="deletePreview.blocked_reason" />
            <el-button
              v-if="deletePreview.can_stop"
              class="archive-stop-task"
              :disabled="actionBusy || deleting"
              @click="stopOrRefreshTaskBeforeDelete"
            >{{ actionBusy ? (stopRequested ? '正在刷新…' : '正在请求停止…') : (stopRequested ? '刷新预览' : '停止任务') }}</el-button>
          </div>
          <el-alert v-if="stopMessage" class="archive-feedback archive-stop-status" type="info" :closable="false" :title="stopMessage" />
          <el-alert v-if="deleteError" class="archive-feedback" type="error" :closable="false" show-icon :title="deleteError" />
          <div class="archive-delete-table-wrap">
            <el-table :data="deletePreview.items" row-key="full_path" class="archive-delete-table" aria-label="将删除和保留的文件清单" max-height="420">
              <el-table-column label="序号" prop="index" width="72" />
              <el-table-column label="文件名 / 类型" min-width="180">
                <template #default="{ row }"><strong class="delete-output-name">{{ row.name }}</strong><span class="delete-output-kind">{{ row.kind_label || row.kind }}</span></template>
              </el-table-column>
              <el-table-column label="文件大小" min-width="110"><template #default="{ row }"><span class="delete-output-size">{{ formatBytes(row.size_bytes) }}</span></template></el-table-column>
              <el-table-column label="路径" min-width="200"><template #default="{ row }"><code class="delete-output-path" :title="row.full_path">{{ row.relative_path }}</code></template></el-table-column>
              <el-table-column label="处理结果" min-width="210">
                <template #default="{ row }"><el-tag :type="row.action === 'delete' || row.action === 'delete_data' ? 'danger' : 'warning'" effect="light">{{ row.status }}</el-tag><span class="delete-output-reason">{{ row.reason }}</span></template>
              </el-table-column>
              <el-table-column label="操作" min-width="125">
                <template #default="{ row }">
                  <div class="delete-output-actions">
                    <el-button v-if="row.can_open" text :disabled="actionBusy" @click="openPreviewPath(row as TaskDeletionItem, false)">查看</el-button>
                    <el-button v-if="row.can_reveal" text :disabled="actionBusy" @click="openPreviewPath(row as TaskDeletionItem, true)">打开目录</el-button>
                    <span v-if="!row.can_open && !row.can_reveal" class="helper">—</span>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>
          <p class="archive-delete-note">文章正文、发布历史、共享文件和外部导出副本会保留。无法确认归属的文件不会自动删除；确认时系统会再次核对引用关系。</p>
        </template>
        <el-alert v-else-if="deleteResultMessage" class="archive-delete-result" type="success" :closable="false" show-icon title="删除已完成" :description="deleteResultMessage" />
      </div>
      <template #footer>
        <div class="archive-delete-footer">
          <el-button :disabled="actionBusy || deleting" @click="closeDeleteDialog">{{ deleteResultMessage ? '完成' : '取消' }}</el-button>
          <el-button v-if="!deleteResultMessage" type="danger" :disabled="deletePreviewLoading || !deletePreview || Boolean(deletePreview.blocked_reason) || deleting || actionBusy" @click="confirmPermanentDelete">
            {{ deleting ? '正在删除…' : '确认删除' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
  </el-config-provider>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本页的覆盖(全量落地时上移 styles.css)—— */
.archive-page {
  --el-color-primary: var(--pine);
  --el-color-primary-light-3: var(--pine-2);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-color-warning: var(--warning);
  --el-color-success: var(--success);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-text-color-placeholder: var(--text-placeholder);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
  --el-border-radius-base: 6px;
}
.panel-head h2 { font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.panel-head h3 { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.quiet-pill { min-height: 24px; padding: 0 10px; color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-medium); font-variant-numeric: tabular-nums; }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }

.archive-list-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 9px; }
.archive-page .archive-toolbar :deep(.el-button) { height: 36px; margin-left: 0; font-size: var(--fs-body); font-weight: var(--fw-medium); }
.archive-page .archive-toolbar :deep(.el-input__wrapper), .archive-page .archive-toolbar :deep(.el-select__wrapper), .archive-page .archive-toolbar :deep(.el-date-editor) { min-height: 36px; height: 36px; }
.archive-keyword { width: min(300px, 28vw); min-width: 210px; }
.archive-filter-select { width: 150px; }
.archive-sort-select { width: 190px; }
.archive-date-range-picker { width: 300px; }
.archive-search-icon { width: 16px; height: 16px; fill: none; stroke: var(--text-tertiary); stroke-width: 1.8; }
.archive-feedback { margin-top: 10px; }
.archive-table-wrap { min-width: 0; overflow: auto; }
.archive-page .archive-table { min-width: 1120px; --el-table-header-bg-color: var(--paper-2); --el-table-border-color: var(--line); --el-table-text-color: var(--text-secondary); --el-table-header-text-color: var(--text-secondary); }
.archive-page .archive-table :deep(th) { font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.archive-page .archive-table :deep(td) { font-size: var(--fs-body); }
.archive-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 2px 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.archive-pagination-controls { display: flex; align-items: center; gap: 8px; min-width: 0; }
.archive-page-size { white-space: nowrap; }
.archive-page-size-select { width: 90px; }
.archive-pagination-controls :deep(.el-pagination) { --el-pagination-font-size: var(--fs-body); }
.archive-empty-title { color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.archive-page :deep(.el-empty__description p) { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.archive-row-actions { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.archive-row-actions :deep(.el-button) { min-height: 32px; margin-left: 0; padding: 0 4px; font-size: var(--fs-body-sm); }
.archive-row-actions :deep(.el-button + .el-button) { margin-left: 0; }
.archive-action-message { margin-bottom: 12px; }
.archive-page :deep(.archive-delete-dialog) { max-height: min(90dvh, 900px); padding: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: var(--paper); color: var(--text-primary); box-shadow: var(--shadow); }
.archive-page :deep(.archive-delete-dialog .el-dialog__header) { margin: 0; padding: 0; border-bottom: 1px solid var(--line); }
.archive-page :deep(.archive-delete-dialog .el-dialog__headerbtn) { top: 15px; right: 18px; width: 34px; height: 34px; }
.archive-page :deep(.archive-delete-dialog .el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.archive-delete-header { padding: 18px 22px; }
.archive-delete-header h2 { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.archive-delete-header p { margin: 5px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); overflow-wrap: anywhere; }
.archive-page :deep(.archive-delete-dialog .el-dialog__body) { max-height: calc(90dvh - 150px); padding: 0; overflow: auto; color: var(--text-primary); }
.archive-page :deep(.archive-delete-dialog .el-dialog__footer) { padding: 0; border-top: 1px solid var(--line); }
.archive-delete-content { min-height: 0; padding: 16px 22px 20px; }
.archive-delete-summary { margin-bottom: 12px; border-radius: 8px; }
.archive-page :deep(.archive-delete-summary .el-alert__title) { font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.archive-page :deep(.archive-delete-summary .el-alert__description) { color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.archive-delete-blocked { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.archive-delete-blocked .archive-feedback { flex: 1; margin: 0; }
.archive-stop-task { flex: 0 0 auto; white-space: nowrap; }
.archive-stop-status { margin: 0 0 10px; color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.archive-delete-table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 9px; }
.archive-page .archive-delete-table { min-width: 920px; --el-table-header-bg-color: var(--paper-2); --el-table-border-color: var(--line); --el-table-text-color: var(--text-secondary); --el-table-header-text-color: var(--text-secondary); }
.archive-page .archive-delete-table :deep(th) { font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.archive-page .archive-delete-table :deep(td) { font-size: var(--fs-body-sm); line-height: var(--lh-base); vertical-align: top; }
.delete-output-name { display: block; max-width: 240px; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-medium); overflow-wrap: anywhere; }
.delete-output-kind { display: block; margin-top: 2px; color: var(--text-tertiary); font-size: var(--fs-caption); }
.delete-output-size { white-space: nowrap; font-variant-numeric: tabular-nums; }
.delete-output-path { display: block; min-width: 150px; max-width: 310px; color: var(--text-secondary); font-size: var(--fs-body-sm); white-space: normal; overflow-wrap: anywhere; }
.delete-output-reason { display: block; min-width: 180px; max-width: 300px; margin-top: 2px; color: var(--text-tertiary); font-size: var(--fs-caption); overflow-wrap: anywhere; }
.delete-output-actions { display: flex; align-items: flex-start; gap: 8px; white-space: nowrap; }
.delete-output-actions :deep(.el-button) { min-height: 28px; padding: 0; font-size: var(--fs-body-sm); }
.archive-delete-note { margin: 12px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.archive-delete-result { margin: 16px 22px 20px; }
.archive-page :deep(.archive-delete-result .el-alert__title) { font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.archive-page :deep(.archive-delete-result .el-alert__description) { margin-top: 6px; color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); }
.archive-delete-footer { display: flex; justify-content: flex-end; gap: 8px; min-height: 64px; padding: 12px 22px; }

@media (max-width: 680px) {
  .archive-panel-head { flex-wrap: wrap; }
  .archive-list-actions { margin-top: 10px; }
  .archive-pagination { align-items: flex-start; flex-direction: column; }
  .archive-pagination-controls { width: 100%; flex-wrap: wrap; }
  .archive-row-actions { flex-wrap: wrap; justify-content: flex-end; }
  .archive-page :deep(.archive-delete-dialog) { max-height: 94dvh; }
  .archive-page :deep(.archive-delete-dialog .el-dialog__body) { max-height: calc(94dvh - 142px); }
  .archive-delete-header, .archive-delete-content, .archive-delete-footer { padding-left: 14px; padding-right: 14px; }
  .archive-delete-blocked { align-items: flex-start; flex-direction: column; }
}
</style>

<style>
.archive-theme-popper,
.archive-remove-confirm {
  --el-color-primary: var(--pine);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-color-warning: var(--warning);
  --el-color-success: var(--success);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-text-color-placeholder: var(--text-placeholder);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
}
.archive-remove-confirm .el-message-box__title { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.archive-remove-confirm .el-message-box__message { color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); }
</style>
