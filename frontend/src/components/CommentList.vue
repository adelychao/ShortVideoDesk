<script setup lang="ts">
import CommentBrowser from './CommentBrowser.vue'
import type { CommentCollectionState } from '../api/types'

withDefaults(defineProps<{ taskId: string; active?: boolean }>(), { active: true })
const emit = defineEmits<{ (event: 'update:state', state: CommentCollectionState | null): void }>()
</script>

<template>
  <section class="panel panel-pad comment-panel" aria-labelledby="comments-heading">
    <div class="panel-head">
      <div>
        <h2 id="comments-heading">本次采集到的评论</h2>
        <p>只显示公开页面实际返回并保存的评论；支持关键词筛选、排序和分页浏览。</p>
      </div>
    </div>
    <CommentBrowser :task-id="taskId" :active="active" variant="tab" @update:state="emit('update:state', $event)" />
  </section>
</template>

<style scoped>
.comment-panel { display: grid; gap: 1rem; }
</style>
