<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { CommentInsights } from '../api/types'

const props = defineProps<{ taskId: string; transcriptArtifactId: string }>()
const emit = defineEmits<{
  (event: 'update:selectedCommentIds', value: string[]): void
  (event: 'merged'): void
}>()
const result = ref<CommentInsights | null>(null)
const selected = ref<string[]>([])
const category = ref('全部')
const loading = ref(false)
const merging = ref(false)
const error = ref('')
const mergeMessage = ref('')

const visibleItems = computed(() => {
  const items = result.value?.selected ?? []
  return category.value === '全部' ? items : items.filter((item) => item.category === category.value)
})

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '暂时无法读取评论分析结果。'
}

function publish(): void {
  emit('update:selectedCommentIds', [...selected.value])
}

function commentFeedbackMarkdown(): string {
  const selectedIds = new Set(selected.value)
  const comments = (result.value?.selected ?? []).filter((item) => selectedIds.has(item.comment_id))
  const lines = [
    '## 评论反馈参考',
    '',
    '以下内容来自用户勾选的公开评论，仅供后续稿件改写参考；不属于转写正文，不代表创作者经历，也不能作为事实依据。',
    '',
    '| 序号 | 点赞数 | 评论内容 |',
    '| ---: | ---: | :--- |',
  ]
  comments.forEach((item, index) => {
    const text = item.text.replace(/\r\n?/g, '\n').split('\n').map((line) => line.trim()).filter(Boolean).join('；')
    const tableText = text.replace(/\|/g, '\\|')
    lines.push(`| ${index + 1} | ${item.like_count ?? '未提供'} | ${tableText} |`)
  })
  return lines.join('\n').trim()
}

async function mergeIntoTranscript(): Promise<void> {
  if (!props.transcriptArtifactId || !selected.value.length || merging.value) return
  merging.value = true
  error.value = ''
  mergeMessage.value = ''
  try {
    const source = await api.getTextArtifact(props.transcriptArtifactId)
    const lines = source.content.replace(/\r\n?/g, '\n').split('\n')
    const existingAppendix = lines.findIndex((line) => line.trim() === '## 评论反馈参考')
    const manuscript = (existingAppendix >= 0 ? lines.slice(0, existingAppendix) : lines).join('\n').trimEnd()
    const merged = `${manuscript}\n\n${commentFeedbackMarkdown()}\n`
    await api.saveTextArtifactRevision(props.transcriptArtifactId, source.sha256, merged)
    selected.value = []
    publish()
    mergeMessage.value = '已生成合并版转写稿；后续稿件改写会优先使用它。'
    emit('merged')
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '合并评论到转写稿失败，请稍后重试。'
  } finally {
    merging.value = false
  }
}

function toggle(commentId: string, event: Event): void {
  const checked = (event.target as HTMLInputElement).checked
  selected.value = checked
    ? [...new Set([...selected.value, commentId])]
    : selected.value.filter((item) => item !== commentId)
  publish()
}

async function load(): Promise<void> {
  if (!props.taskId) return
  loading.value = true
  error.value = ''
  try {
    result.value = await api.getCommentInsights(props.taskId)
    const validIds = new Set(result.value.selected.map((item) => item.comment_id))
    selected.value = selected.value.filter((item) => validIds.has(item))
    publish()
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    loading.value = false
  }
}

watch(() => props.taskId, () => {
  result.value = null
  selected.value = []
  category.value = '全部'
  void load()
}, { immediate: true })
</script>

<template>
  <section class="panel panel-pad insight-panel" aria-labelledby="comment-insights-title">
    <div class="panel-head">
      <div>
        <h2 id="comment-insights-title">评论精选与摘要</h2>
        <p v-if="result">已分析 {{ result.processed_comment_ids.length }} 条本次采集评论；精选项按真实点赞数排序。</p>
        <p v-else>只展示本次采集结果，不代表平台全部评论。</p>
      </div>
      <label class="sort-control">
        <span>分类</span>
        <select v-model="category">
          <option>全部</option><option>有用</option><option>高赞</option><option>有趣</option>
        </select>
      </label>
    </div>
    <p v-if="loading" class="helper" role="status">正在读取评论分析…</p>
    <div v-if="error" class="info-box form-error" role="alert">{{ error }}</div>
    <template v-if="result">
      <p class="helper">勾选的评论可作为下一次稿件生成的受众反馈来源；不会把评论者经历写成创作者经历。</p>
      <ol v-if="visibleItems.length" class="comment-list">
        <li v-for="item in visibleItems" :key="item.comment_id" class="comment-item">
          <label class="insight-select">
            <input type="checkbox" :checked="selected.includes(item.comment_id)" @change="toggle(item.comment_id, $event)" />
            <span class="insight-copy">
              <span class="comment-head"><strong>{{ item.category }}</strong><span>点赞 {{ item.like_count ?? '未提供' }}</span></span>
              <span class="comment-text">{{ item.text }}</span>
              <span class="helper">精选理由：{{ item.reason }}</span>
            </span>
          </label>
        </li>
      </ol>
      <p v-else class="empty-state">当前分类没有精选评论。</p>
      <div class="insight-sections">
        <section><h3>主题</h3><ul><li v-for="item in result.themes" :key="item">{{ item }}</li><li v-if="!result.themes.length">暂无</li></ul></section>
        <section><h3>受众问题</h3><ul><li v-for="item in result.audience_questions" :key="item">{{ item }}</li><li v-if="!result.audience_questions.length">暂无</li></ul></section>
        <section><h3>分歧</h3><ul><li v-for="item in result.disagreements" :key="item">{{ item }}</li><li v-if="!result.disagreements.length">暂无</li></ul></section>
        <section><h3>待核实事实</h3><ul><li v-for="item in result.claims_to_verify" :key="item">{{ item }}</li><li v-if="!result.claims_to_verify.length">暂无</li></ul></section>
      </div>
      <p class="helper">当前选择 {{ selected.length }} 条评论。模型输出只作为待审核整理结果。</p>
      <div class="comment-merge-action">
        <button class="button button-secondary button-small" type="button" :disabled="merging || !selected.length || !transcriptArtifactId" @click="mergeIntoTranscript">
          {{ merging ? '正在合并…' : '合并到转写稿' }}
        </button>
        <span v-if="!transcriptArtifactId" class="helper">暂无可用转写稿，完成转写或校对后即可合并。</span>
      </div>
      <p v-if="mergeMessage" class="helper" role="status">{{ mergeMessage }}</p>
    </template>
  </section>
</template>

<style scoped>
.insight-panel { display: grid; gap: 1rem; }
.insight-select { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .85rem; align-items: start; }
.insight-select input { margin-top: .25rem; }
.insight-copy { display: grid; gap: .35rem; }
.comment-head { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; color: var(--text-secondary); font-size: .8rem; }
.comment-head strong { color: var(--text-primary); }
.comment-text { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text-primary); }
.insight-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
.insight-sections h3 { margin: 0 0 .5rem; font-size: .9rem; }
.insight-sections ul { margin: 0; padding-left: 1.1rem; color: var(--text-secondary); }
.comment-merge-action { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: .75rem; }
@media (max-width: 700px) { .insight-sections { grid-template-columns: 1fr; } }
</style>
