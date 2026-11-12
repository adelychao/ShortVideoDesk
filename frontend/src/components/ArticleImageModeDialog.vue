<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElButton, ElDialog } from 'element-plus'
import { RouterLink } from 'vue-router'
import type { ArticleImageSettings } from '../api/types'

type ImageMode = 'provider' | 'vector'

const props = defineProps<{
  open: boolean
  target: 'cover' | 'paragraph'
  promptAvailable: boolean
  imageSettings: ArticleImageSettings | null
  replacing: boolean
}>()
const emit = defineEmits<{
  'update:open': [open: boolean]
  generate: [mode: ImageMode]
}>()

const mode = ref<ImageMode>('provider')
const recommended = computed<ImageMode>(() => props.target === 'cover' ? 'provider' : 'vector')
const targetLabel = computed(() => props.target === 'cover' ? '封面' : '正文配图')
const imageModelIssue = computed(() => {
  const settings = props.imageSettings
  if (!settings?.configured) return '请先在「模型与转写」设置配图模型。'
  if (settings.provider_id === 'zhipu' && ['glm-5.3-flash', 'glm-5.3-flashx', 'glm-5.3-flashx-legal'].includes(settings.model_id.toLowerCase())) {
    return '当前智谱模型只输出文本。可在配图模型配置中填写 glm-image。'
  }
  return ''
})
const issue = computed(() => mode.value === 'vector' ? '' : imageModelIssue.value || (
  props.promptAvailable ? '' : '请先在配图区填写 AI 生图提示词。'
))

watch(() => props.open, (open) => {
  if (open) mode.value = recommended.value
})
</script>

<template>
  <el-dialog
    :model-value="open"
    class="article-image-mode-dialog"
    :title="replacing ? '重新生成配图' : '生成配图'"
    :teleported="false"
    :close-on-click-modal="false"
    @update:model-value="emit('update:open', $event)"
  >
    <div class="image-mode-body">
      <p class="image-mode-intro">为{{ targetLabel }}选择一种生成方式。</p>
      <fieldset class="image-mode-options">
        <legend class="sr-only">配图生成方式</legend>
        <label class="image-mode-option" :class="{ 'is-selected': mode === 'provider' }">
          <input v-model="mode" class="sr-only" type="radio" name="article-image-mode" value="provider">
          <span class="image-mode-art image-mode-art--ai" aria-hidden="true">
            <svg viewBox="0 0 144 72" fill="none">
              <rect x="21" y="11" width="102" height="50" rx="8" stroke="currentColor" stroke-width="1.5"/>
              <path d="M22 52 52 31l21 15 16-11 33 20v5H22Z" fill="currentColor" opacity=".17"/>
              <path d="m25 52 27-21 21 15 16-11 30 20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <circle cx="93" cy="26" r="6" fill="currentColor" opacity=".5"/>
              <path d="M111 11v14m-7-7h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </span>
          <span class="image-mode-title">AI 生图 <span v-if="recommended === 'provider'" class="image-mode-badge">推荐</span></span>
          <span class="image-mode-description">使用现有提示词生成插画、场景或人物图。</span>
          <span class="image-mode-radio" aria-hidden="true" />
        </label>
        <label class="image-mode-option" :class="{ 'is-selected': mode === 'vector' }">
          <input v-model="mode" class="sr-only" type="radio" name="article-image-mode" value="vector">
          <span class="image-mode-art image-mode-art--diagram" aria-hidden="true">
            <svg viewBox="0 0 144 72" fill="none">
              <path d="M72 28v12m-39 8v-8h78v8m-39-8v8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              <rect x="52" y="10" width="40" height="18" rx="6" fill="currentColor" opacity=".85"/>
              <rect x="17" y="48" width="32" height="14" rx="5" fill="currentColor" opacity=".3"/>
              <rect x="56" y="48" width="32" height="14" rx="5" fill="currentColor" opacity=".45"/>
              <rect x="95" y="48" width="32" height="14" rx="5" fill="currentColor" opacity=".3"/>
            </svg>
          </span>
          <span class="image-mode-title">知识图解 <span v-if="recommended === 'vector'" class="image-mode-badge">推荐</span></span>
          <span class="image-mode-description">从文章提炼要点，按内容选择关系图、流程图等。</span>
          <span class="image-mode-radio" aria-hidden="true" />
        </label>
      </fieldset>
      <div class="image-mode-details" aria-live="polite">
        <p v-if="issue" class="image-mode-issue">
          {{ issue }}
          <RouterLink v-if="imageModelIssue" to="/settings" @click="emit('update:open', false)">打开设置</RouterLink>
        </p>
        <p v-else-if="mode === 'provider'">提示词将发送给 {{ imageSettings?.model_id }}，费用按图片模型服务商计收。</p>
        <p v-else>文章标题、摘要和相关正文将发送给默认文本模型。有提示词时也会一并参考。图解在本机绘制，文本模型调用可能产生费用。</p>
        <p v-if="replacing" class="image-mode-replacement">生成成功后，新图会替换当前配图。</p>
      </div>
    </div>
    <template #footer>
      <el-button @click="emit('update:open', false)">取消</el-button>
      <el-button type="primary" :disabled="!!issue" @click="emit('generate', mode)">确认生成</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.image-mode-body { display: grid; gap: 14px; min-width: 0; }
.image-mode-intro { margin: 0; color: var(--text-secondary); font-size: var(--fs-body); line-height: var(--lh-base); }
.image-mode-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; min-width: 0; margin: 0; padding: 0; border: 0; }
.image-mode-option { position: relative; display: grid; grid-template-rows: auto auto 1fr; gap: 7px; min-width: 0; padding: 10px 12px 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--paper); cursor: pointer; }
.image-mode-option:hover { border-color: var(--pine-2); }
.image-mode-option.is-selected { border-color: var(--pine); box-shadow: inset 0 0 0 1px var(--pine); }
.image-mode-option:focus-within { outline: 2px solid var(--pine); outline-offset: 2px; }
.image-mode-art { display: grid; place-items: center; min-height: 70px; border-radius: 8px; }
.image-mode-art svg { width: min(144px, 100%); height: 70px; }
.image-mode-art--ai { color: var(--gold); background: var(--gold-soft); }
.image-mode-art--diagram { color: var(--pine); background: var(--pine-soft); }
.image-mode-title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding-right: 22px; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.image-mode-description { color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.image-mode-badge { padding: 1px 6px; border-radius: 6px; background: var(--pine-soft); color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.image-mode-radio { position: absolute; top: 90px; right: 12px; width: 16px; height: 16px; border: 1px solid var(--text-tertiary); border-radius: 50%; background: var(--paper); }
.image-mode-option.is-selected .image-mode-radio { border: 5px solid var(--pine); }
.image-mode-details { display: grid; gap: 5px; padding: 11px 13px; border-radius: 8px; background: var(--paper-2); color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); overflow-wrap: anywhere; }
.image-mode-details p { margin: 0; }
.image-mode-issue { color: var(--warning); }
.image-mode-details a { color: var(--pine); font-weight: var(--fw-medium); text-decoration: underline; }
.image-mode-replacement { color: var(--text-tertiary); }
@media (max-width: 480px) {
  .image-mode-options { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .image-mode-option { grid-template: auto 1fr / 72px minmax(0, 1fr); column-gap: 10px; }
  .image-mode-art { grid-row: span 2; min-height: 54px; }
  .image-mode-art svg { height: 54px; }
  .image-mode-radio { top: 12px; }
}
</style>
