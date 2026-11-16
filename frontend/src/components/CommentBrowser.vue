<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { CommentCollectionState, CommentItem, CommentPageResponse } from '../api/types'

const props = withDefaults(defineProps<{
  taskId: string
  active?: boolean
  variant?: 'tab' | 'preview'
}>(), { active: true, variant: 'tab' })
const emit = defineEmits<{ (event: 'update:state', state: CommentCollectionState | null): void }>()

let nextBrowserId = 0
const searchId = `comment-search-${++nextBrowserId}`
const result = ref<CommentPageResponse | null>(null)
const sort = ref<CommentPageResponse['sort']>('likes_desc')
const pageSize = ref(props.variant === 'preview' ? 50 : 100)
const offset = ref(0)
const searchDraft = ref('')
const activeQuery = ref('')
const jumpPage = ref('1')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const expanded = ref<Record<string, string>>({})
const expandLoading = ref<Record<string, boolean>>({})
const expandErrors = ref<Record<string, string>>({})
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0
let pageRequest: AbortController | null = null
const commentRequests = new Map<string, AbortController>()

const matchedTotal = computed(() => result.value?.matched_total ?? result.value?.total ?? 0)
const pageCount = computed(() => Math.max(1, Math.ceil(matchedTotal.value / pageSize.value)))
const currentPage = computed(() => Math.floor(offset.value / pageSize.value) + 1)
const pageLabel = computed(() => {
  const total = matchedTotal.value
  if (!total) return '0 条匹配'
  return `${offset.value + 1}–${Math.min(offset.value + (result.value?.items.length ?? pageSize.value), total)} / ${total}`
})
const showingPage = computed(() => pageCount.value > 1 || Boolean(activeQuery.value))

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '暂时无法读取评论记录。'
}

function clearTimer(): void {
  if (timer) clearTimeout(timer)
  timer = undefined
}

function schedule(): void {
  clearTimer()
  if (props.active && result.value?.state?.status === 'running') {
    timer = setTimeout(() => void load(), 2500)
  }
}

async function load(skipRevision = false): Promise<void> {
  if (!props.active || !props.taskId) return
  const current = ++sequence
  pageRequest?.abort()
  pageRequest = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const response = await api.getComments(props.taskId, {
      sort: sort.value,
      limit: pageSize.value,
      offset: offset.value,
      q: activeQuery.value,
      revision: !skipRevision && offset.value > 0 ? result.value?.revision : undefined,
      signal: pageRequest.signal,
    })
    if (current !== sequence) return
    result.value = response
    jumpPage.value = String(currentPage.value)
    emit('update:state', response.state)
    schedule()
  } catch (cause) {
    if (current !== sequence) return
    if (cause instanceof Error && cause.name === 'AbortError') return
    if (cause instanceof ApiError && cause.status === 409 && !skipRevision) {
      offset.value = 0
      jumpPage.value = '1'
      notice.value = '评论采集有更新，已刷新到第一页。'
      void load(true)
      return
    }
    error.value = safeError(cause)
    schedule()
  } finally {
    if (current === sequence) loading.value = false
  }
}

function submitSearch(): void {
  activeQuery.value = searchDraft.value.trim()
  offset.value = 0
  jumpPage.value = '1'
  notice.value = ''
  expanded.value = {}
  void load()
}

function resetSearch(): void {
  searchDraft.value = ''
  activeQuery.value = ''
  sort.value = 'likes_desc'
  pageSize.value = props.variant === 'preview' ? 50 : 100
  offset.value = 0
  jumpPage.value = '1'
  notice.value = ''
  expanded.value = {}
  void load()
}

function updateSort(): void {
  offset.value = 0
  jumpPage.value = '1'
  notice.value = ''
  void load()
}

function updatePageSize(): void {
  offset.value = 0
  jumpPage.value = '1'
  notice.value = ''
  expanded.value = {}
  void load()
}

function pageBack(): void {
  if (offset.value <= 0 || loading.value) return
  offset.value = Math.max(0, offset.value - pageSize.value)
  jumpPage.value = String(currentPage.value)
  void load()
}

function pageForward(): void {
  if (!result.value || loading.value || offset.value + pageSize.value >= matchedTotal.value) return
  offset.value += pageSize.value
  jumpPage.value = String(currentPage.value)
  void load()
}

function goToPage(): void {
  const target = Number(jumpPage.value)
  if (!Number.isInteger(target) || target < 1 || target > pageCount.value) {
    error.value = `请输入 1 到 ${pageCount.value} 之间的页码。`
    return
  }
  error.value = ''
  offset.value = (target - 1) * pageSize.value
  void load()
}

async function expandComment(item: CommentItem): Promise<void> {
  const id = item.platform_comment_id
  if (expanded.value[id] !== undefined || expandLoading.value[id]) return
  const controller = new AbortController()
  commentRequests.set(id, controller)
  expandLoading.value = { ...expandLoading.value, [id]: true }
  expandErrors.value = { ...expandErrors.value, [id]: '' }
  try {
    const full = await api.getComment(props.taskId, id, controller.signal)
    expanded.value = { ...expanded.value, [id]: full.text }
  } catch (cause) {
    if (cause instanceof Error && cause.name === 'AbortError') return
    expandErrors.value = { ...expandErrors.value, [id]: safeError(cause) }
  } finally {
    commentRequests.delete(id)
    expandLoading.value = { ...expandLoading.value, [id]: false }
  }
}

function formatTime(value: string | null): string {
  if (!value) return '未提供时间'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '未提供时间' : parsed.toLocaleString('zh-CN')
}

watch(() => [props.taskId, props.active], ([taskId, active], previous) => {
  clearTimer()
  pageRequest?.abort()
  pageRequest = null
  for (const controller of commentRequests.values()) controller.abort()
  commentRequests.clear()
  sequence++
  if (taskId !== previous?.[0]) {
    result.value = null
    offset.value = 0
    activeQuery.value = ''
    searchDraft.value = ''
    expanded.value = {}
    emit('update:state', null)
  }
  if (active) void load()
}, { immediate: true })

onBeforeUnmount(() => {
  clearTimer()
  pageRequest?.abort()
  for (const controller of commentRequests.values()) controller.abort()
  commentRequests.clear()
  sequence++
})
</script>

<template>
  <section class="comment-browser" :class="`comment-browser--${variant}`" :aria-label="variant === 'preview' ? '评论分页预览' : '评论列表'">
    <div v-if="variant === 'preview'" class="comment-browser-intro">
      <strong>本次任务的当前评论</strong>
      <span>来自任务已保存的公开评论；分页读取，不加载整份归档文档。</span>
    </div>

    <div class="comment-browser-toolbar">
      <form class="comment-search" role="search" @submit.prevent="submitSearch">
        <label :for="searchId">关键词</label>
        <div class="comment-search-field">
          <svg class="comment-search-icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5" /><path d="m12.5 12.5 4 4" /></svg>
          <input :id="searchId" v-model="searchDraft" class="input" type="search" maxlength="120" placeholder="搜索评论内容" />
        </div>
        <button class="button button-small" type="submit" :disabled="loading">搜索</button>
        <button class="button button-secondary button-small" type="button" :disabled="loading && !result" @click="resetSearch">重置</button>
      </form>
      <div class="comment-view-options">
        <label><span>排序</span><select v-model="sort" class="select-control" @change="updateSort"><option value="likes_desc">点赞从高到低</option><option value="time_desc">时间从新到旧</option><option value="time_asc">时间从旧到新</option></select></label>
        <label><span>每页</span><select v-model.number="pageSize" class="select-control" @change="updatePageSize"><option :value="25">25 条</option><option :value="50">50 条</option><option :value="100">100 条</option></select></label>
      </div>
    </div>

    <div v-if="result" class="comment-result-summary">
      <span>采集 {{ result.total }} 条<template v-if="activeQuery"> · 匹配 {{ matchedTotal }} 条</template><template v-else> · 当前匹配 {{ matchedTotal }} 条</template></span>
      <span v-if="result.state">{{ result.state.status === 'running' ? '采集中' : result.state.stop_reason || '采集状态已保存' }}</span>
      <span v-if="loading" role="status">正在更新…</span>
    </div>
    <p v-else-if="loading" class="helper" role="status">正在读取评论…</p>
    <p v-if="notice" class="helper" role="status">{{ notice }}</p>
    <div v-if="error" class="info-box form-error" role="alert"><span>{{ error }}</span><button class="text-button" type="button" @click="load()">重试</button></div>
    <div v-else-if="result && !result.items.length" class="comment-browser-empty" role="status">
      {{ activeQuery ? '没有找到匹配的评论。' : result.state?.status === 'running' ? '正在逐页采集，当前尚未返回评论。' : '本次没有取得可展示的评论记录。' }}
    </div>

    <ol v-else-if="result" class="comment-browser-list">
      <li v-for="(item, index) in result.items" :key="item.platform_comment_id" class="comment-browser-row">
        <div class="comment-browser-row-head">
          <span class="comment-browser-number">{{ offset + index + 1 }}</span>
          <span>点赞 {{ item.like_count ?? '未提供' }}</span>
          <template v-if="variant === 'tab'"><strong>{{ item.author || '未提供作者' }}</strong><time>{{ formatTime(item.created_at) }}</time></template>
        </div>
        <p class="comment-browser-text">{{ expanded[item.platform_comment_id] ?? item.text }}</p>
        <button v-if="item.has_more_text && expanded[item.platform_comment_id] === undefined" class="text-button comment-expand" type="button" :disabled="expandLoading[item.platform_comment_id]" @click="expandComment(item)">
          {{ expandLoading[item.platform_comment_id] ? '正在读取全文…' : '展开全文' }}
        </button>
        <div v-if="expandErrors[item.platform_comment_id]" class="comment-expand-error" role="alert">{{ expandErrors[item.platform_comment_id] }} <button class="text-button" type="button" @click="expandComment(item)">重试</button></div>
        <div v-if="expanded[item.platform_comment_id] !== undefined" class="comment-expanded-meta">{{ item.author || '未提供作者' }} · {{ formatTime(item.created_at) }}</div>
      </li>
    </ol>

    <div v-if="result && showingPage" class="comment-browser-pagination">
      <button class="button button-secondary button-small" type="button" :disabled="offset === 0 || loading" @click="pageBack">上一页</button>
      <span class="helper">{{ pageLabel }}</span>
      <form class="comment-page-jump" @submit.prevent="goToPage">
        <label class="sr-only" :for="`${searchId}-page`">跳转到页码</label>
        <input :id="`${searchId}-page`" v-model="jumpPage" class="input" type="number" min="1" :max="pageCount" inputmode="numeric" />
        <span> / {{ pageCount }} 页</span>
        <button class="button button-secondary button-small" type="submit" :disabled="loading">跳转</button>
      </form>
      <button class="button button-secondary button-small" type="button" :disabled="offset + pageSize >= matchedTotal || loading" @click="pageForward">下一页</button>
    </div>
  </section>
</template>

<style scoped>
.comment-browser { display: grid; gap: .85rem; min-width: 0; }
.comment-browser-intro { display: grid; gap: .2rem; padding-bottom: .7rem; border-bottom: 1px solid var(--line); color: var(--text-secondary); font-size: var(--fs-body-sm); }
.comment-browser-intro strong { color: var(--text-primary); font-size: var(--fs-body); }
.comment-browser-toolbar { display: flex; align-items: end; justify-content: space-between; flex-wrap: wrap; gap: .65rem; }
.comment-search, .comment-view-options, .comment-view-options label { display: flex; align-items: center; flex-wrap: wrap; gap: .45rem; }
.comment-search label, .comment-view-options label { color: var(--text-secondary); font-size: var(--fs-body-sm); }
.comment-search-field { position: relative; width: min(18rem, 48vw); min-height: 36px; }
.comment-search-field .input { width: 100%; min-height: 36px; padding: 4px 10px 4px 32px; border-radius: 6px; font-size: var(--fs-body); }
.comment-search-icon { position: absolute; left: .6rem; top: 50%; z-index: 1; width: 16px; height: 16px; transform: translateY(-50%); fill: none; stroke: var(--text-tertiary); stroke-width: 1.7; stroke-linecap: round; }
.comment-search .button { min-height: 36px; border-radius: 6px; font-size: var(--fs-body); }
.comment-view-options .select-control { min-height: 36px; width: auto; max-width: 11rem; padding: 4px 30px 4px 8px; border-radius: 6px; font-size: var(--fs-body); }
.comment-result-summary { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .5rem; color: var(--text-secondary); font-size: var(--fs-caption); }
.comment-browser-list { list-style: none; margin: 0; padding: 0; display: grid; }
.comment-browser-row { min-width: 0; padding: .75rem .1rem; border-top: 1px solid var(--line); }
.comment-browser-row-head { display: flex; align-items: center; flex-wrap: wrap; gap: .7rem; color: var(--text-secondary); font-size: var(--fs-caption); }
.comment-browser-row-head strong { color: var(--text-primary); font-weight: 500; }
.comment-browser-row-head time { margin-left: auto; }
.comment-browser-number { display: inline-grid; place-items: center; width: 1.65rem; min-width: 1.65rem; height: 1.45rem; border-radius: 4px; background: var(--paper-2); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.comment-browser-text { margin: .45rem 0 0; max-width: 100%; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.55; }
.comment-expand { margin-top: .3rem; font-size: var(--fs-caption); }
.comment-expanded-meta { margin-top: .35rem; color: var(--text-secondary); font-size: var(--fs-caption); }
.comment-expand-error { color: var(--danger); font-size: var(--fs-caption); }
.comment-browser-pagination { position: sticky; bottom: 0; z-index: 1; display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: .5rem; padding: .65rem 0; background: var(--paper); border-top: 1px solid var(--line); }
.comment-browser-pagination .helper { margin: 0 .35rem; font-size: var(--fs-caption); }
.comment-page-jump { display: flex; align-items: center; gap: .35rem; color: var(--text-secondary); font-size: var(--fs-caption); }
.comment-page-jump .input { width: 4.3rem; min-height: 36px; padding: .25rem .4rem; border-radius: 6px; font-size: var(--fs-body); }
.comment-browser-empty { padding: 1rem 0; color: var(--text-secondary); }
.comment-browser--preview { min-height: 18rem; }
.comment-browser--tab { padding: .25rem 0; }
.comment-browser--tab .comment-browser-pagination { background: var(--paper); }
@media (max-width: 680px) {
  .comment-browser-toolbar { align-items: stretch; }
  .comment-search { width: 100%; }
  .comment-search-field { flex: 1; min-width: 6rem; width: auto; }
  .comment-view-options { justify-content: space-between; }
  .comment-browser-pagination { gap: .4rem; }
  .comment-browser-pagination .helper { flex-basis: 100%; text-align: center; }
}
</style>
