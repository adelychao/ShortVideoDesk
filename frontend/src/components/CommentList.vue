<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { CommentCollectionState, CommentPageResponse } from '../api/types'

const props = defineProps<{ taskId: string; active?: boolean }>()
const emit = defineEmits<{ (event: 'update:state', state: CommentCollectionState | null): void }>()
const result = ref<CommentPageResponse | null>(null)
const sort = ref<CommentPageResponse['sort']>('likes_desc')
const offset = ref(0)
const loading = ref(false)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0
let tabActive = true

const pageLabel = computed(() => {
  const total = result.value?.total ?? 0
  if (!total) return '暂无评论记录'
  return `${offset.value + 1}–${Math.min(offset.value + 100, total)} / ${total}`
})

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '暂时无法读取评论记录。'
}

function schedule(): void {
  if (timer) clearTimeout(timer)
  if (tabActive && (result.value?.state?.status === 'running' || props.active)) {
    timer = setTimeout(() => void load(), 2000)
  }
}

function pauseTab(): void {
  tabActive = false
  sequence++
  if (timer) clearTimeout(timer)
  timer = undefined
}

function resumeTab(): void {
  if (tabActive) return
  tabActive = true
  void load()
}

async function load(): Promise<void> {
  const current = ++sequence
  loading.value = true
  error.value = ''
  try {
    const response = await api.getComments(props.taskId, sort.value, offset.value)
    if (current !== sequence) return
    result.value = response
    emit('update:state', response.state)
    schedule()
  } catch (cause) {
    if (current === sequence) error.value = safeError(cause)
  } finally {
    if (current === sequence) loading.value = false
  }
}

function changeSort(): void {
  offset.value = 0
  void load()
}

function pageBack(): void {
  offset.value = Math.max(0, offset.value - 100)
  void load()
}

function pageForward(): void {
  if (!result.value || offset.value + 100 >= result.value.total) return
  offset.value += 100
  void load()
}

function formatTime(value: string | null): string {
  if (!value) return '未提供时间'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '未提供时间' : parsed.toLocaleString('zh-CN')
}

watch(() => [props.taskId, props.active], () => {
  if (timer) clearTimeout(timer)
  result.value = null
  emit('update:state', null)
  offset.value = 0
  void load()
}, { immediate: true })
onActivated(resumeTab)
onDeactivated(pauseTab)
onBeforeUnmount(pauseTab)
</script>

<template>
  <section class="panel panel-pad comment-panel" aria-labelledby="comments-heading">
    <div class="panel-head">
      <div>
        <h2 id="comments-heading">本次采集到的评论</h2>
        <p>{{ result?.scope || '仅显示公开页面实际返回并保存的一级评论；楼中楼回复默认不采集。' }}</p>
      </div>
      <label class="sort-control">
        <span>排序</span>
        <select v-model="sort" @change="changeSort">
          <option value="likes_desc">点赞从高到低</option>
          <option value="time_desc">时间从新到旧</option>
          <option value="time_asc">时间从旧到新</option>
        </select>
      </label>
    </div>

    <div v-if="result?.state" class="info-box comment-summary" role="status">
      <span>
        已增量采集 {{ result.state.collected_count }} 条，{{ result.state.page_count }} 页。
        <template v-if="result.state.stop_reason">采集说明：{{ result.state.stop_reason }}</template>
      </span>
      <span v-if="loading" class="helper">正在同步…</span>
    </div>
    <div v-else-if="loading" class="helper" role="status">等待评论采集步骤开始…</div>
    <div v-if="error" class="info-box form-error" role="alert">{{ error }}</div>
    <div v-else-if="result && !result.items.length" class="empty-state">
      {{ result.state?.status === 'running' ? '正在逐页采集，当前尚未返回评论。' : '本次没有取得可展示的评论记录。' }}
    </div>
    <ol v-else-if="result" class="comment-list">
      <li v-for="item in result.items" :key="item.platform_comment_id" class="comment-item">
        <div class="comment-head">
          <strong>{{ item.author || '未提供作者' }}</strong>
          <span>点赞 {{ item.like_count ?? '未提供' }}</span>
          <time>{{ formatTime(item.created_at) }}</time>
        </div>
        <p class="comment-text">{{ item.text }}</p>
      </li>
    </ol>

    <div v-if="result && result.total > 100" class="comment-pagination">
      <button class="button button-secondary button-small" type="button" :disabled="offset === 0 || loading" @click="pageBack">上一页</button>
      <span class="helper">{{ pageLabel }}</span>
      <button class="button button-secondary button-small" type="button" :disabled="offset + 100 >= result.total || loading" @click="pageForward">下一页</button>
    </div>
  </section>
</template>

<style scoped>
.comment-panel { display: grid; gap: 1rem; }
.sort-control { display: flex; align-items: center; gap: .5rem; color: var(--text-secondary); font-size: .82rem; }
.sort-control select { max-width: 12rem; }
.comment-summary { display: flex; justify-content: space-between; gap: .75rem; }
.comment-list { list-style: none; margin: 0; padding: 0; display: grid; gap: .75rem; }
.comment-item { padding: .9rem 0; border-top: 1px solid var(--border-subtle); }
.comment-head { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; color: var(--text-secondary); font-size: .78rem; }
.comment-head strong { color: var(--text-primary); }
.comment-head time { margin-left: auto; }
.comment-text { margin: .7rem 0; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text-primary); }
.comment-pagination { display: flex; align-items: center; justify-content: center; gap: 1rem; }
.empty-state { color: var(--text-secondary); padding: .5rem 0; }
</style>
