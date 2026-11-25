<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElButton, ElDialog } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/dialog/style/css'
import type { ObsidianOutputDirectory } from '../api/types'

const props = withDefaults(defineProps<{
  open: boolean
  libraries: ObsidianOutputDirectory[]
  title?: string
  hint?: string
  confirmLabel?: string
}>(), {
  title: '选择要写入的知识库',
  hint: '写入后将记录关联信息，可随时在工作台更新。',
  confirmLabel: '确认',
})

const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [libraryName: string]
  cancel: []
}>()

const choice = ref('')

watch(() => props.open, (isOpen) => {
  if (isOpen) choice.value = props.libraries[0]?.name ?? ''
})

function confirm(): void {
  if (!choice.value) return
  emit('confirm', choice.value)
}

function cancel(): void {
  emit('update:open', false)
  emit('cancel')
}
</script>

<template>
  <el-dialog
    :model-value="open"
    class="card-library-dialog"
    :title="title"
    width="520"
    :teleported="false"
    :close-on-click-modal="false"
    @close="cancel()"
  >
    <p class="library-dialog-hint">{{ hint }}</p>
    <fieldset class="library-choice-list">
      <legend class="library-choice-legend">知识库目录</legend>
      <label v-for="library in libraries" :key="library.name" class="library-choice-item">
        <input v-model="choice" type="radio" name="vault-library-choice" :value="library.name" />
        <span class="library-choice-name">{{ library.name }}</span>
        <span class="library-choice-directory">{{ library.directory }}</span>
      </label>
    </fieldset>
    <template #footer>
      <el-button @click="cancel()">取消</el-button>
      <el-button type="primary" :disabled="!choice" @click="confirm">{{ confirmLabel }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* —— 选择知识库目录的弹窗（知识卡审核与转写稿一键导入共用） —— */
.card-library-dialog :deep(.el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.card-library-dialog :deep(.el-dialog__body) { padding-top: 8px; color: var(--text-secondary); }
.library-dialog-hint { margin: 0 0 10px; font-size: var(--fs-body); line-height: var(--lh-base); color: var(--text-secondary); }
.library-choice-list { display: grid; gap: 8px; margin: 0; padding: 0; border: 0; }
.library-choice-legend { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.library-choice-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-areas: "radio name" "radio directory";
  align-items: center;
  column-gap: 10px;
  row-gap: 2px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  cursor: pointer;
}
.library-choice-item:hover { border-color: var(--pine); }
.library-choice-item:has(input:checked) { border-color: var(--pine); background: var(--pine-soft); }
.library-choice-item input { grid-area: radio; width: 16px; height: 16px; accent-color: var(--pine); }
.library-choice-name { grid-area: name; font-size: var(--fs-body); font-weight: var(--fw-semibold); color: var(--text-primary); overflow-wrap: anywhere; }
.library-choice-directory { grid-area: directory; font-size: var(--fs-caption); color: var(--text-tertiary); overflow-wrap: anywhere; }
</style>
