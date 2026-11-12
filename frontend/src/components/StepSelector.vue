<script setup lang="ts">
import { computed } from 'vue'
import { ElAlert, ElCheckbox, ElCheckboxGroup, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/tag/style/css'
import type { ScriptStyleTemplate, StepKind } from '../api/types'

interface StepOption {
  kind: StepKind
  title: string
  description: string
  available: boolean
  dependency?: string
}

const props = withDefaults(defineProps<{
  modelValue: StepKind[]
  sourceKind?: 'url' | 'file'
  modelConfigured?: boolean
  textModelConfigured?: boolean
  asrModelConfigured?: boolean
  transcriptionSummary?: string
  ocrSummary?: string
  imageModelConfigured?: boolean
  mediaType?: 'video' | 'audio' | null
  scriptStyleTemplates?: ScriptStyleTemplate[]
  scriptStyleIds?: string[]
  scriptStyleTemplatesLoading?: boolean
  scriptStyleTemplatesError?: string
}>(), {
  sourceKind: 'url',
  modelConfigured: false,
  textModelConfigured: undefined,
  asrModelConfigured: undefined,
  transcriptionSummary: '',
  ocrSummary: '',
  imageModelConfigured: undefined,
  mediaType: null,
  scriptStyleTemplates: () => [],
  scriptStyleIds: () => [],
  scriptStyleTemplatesLoading: false,
  scriptStyleTemplatesError: '',
})
const emit = defineEmits<{
  (event: 'update:modelValue', value: StepKind[]): void
  (event: 'update:scriptStyleIds', value: string[]): void
  (event: 'settings', value: 'download' | 'comments' | 'ocr' | 'transcribe'): void
}>()

const selectedScriptStyleIds = computed({
  get: () => props.scriptStyleIds,
  set: (value: string[]) => emit('update:scriptStyleIds', value),
})
const maxSelectedScriptStyles = 20
const enabledScriptStyles = computed(() => props.scriptStyleTemplates.filter((item) => item.enabled))
const scriptStyleSelectionRequired = computed(() => props.modelValue.includes('generate_scripts') && selectedScriptStyleIds.value.length === 0)

const options = computed<StepOption[]>(() => [
  { kind: 'download', title: '下载视频与封面', description: '保存经身份核对和完整解码校验的视频及封面。', available: props.sourceKind === 'url' },
  { kind: 'import', title: '导入本地媒体', description: '复制本机视频或音频到所选归档目录。', available: props.sourceKind === 'file' },
  { kind: 'transcribe', title: '语音转文字', description: '可选择本地或云端语音转写。', available: props.asrModelConfigured ?? props.modelConfigured },
  { kind: 'ocr', title: '字幕 OCR 与校对', description: '识别视频画面中的文字，可选择本地或云端方式。', available: (props.imageModelConfigured ?? props.textModelConfigured ?? props.modelConfigured) && props.mediaType !== 'audio', dependency: '需要视频画面' },
  { kind: 'comments', title: '采集公开评论', description: '逐页采集公开主评论；默认不获取楼中楼回复。', available: props.sourceKind === 'url', dependency: '同时需要下载素材' },
  { kind: 'analyze_comments', title: '筛选评论', description: '按真实点赞数排序全部已采集评论，再由模型分析主题、问题、分歧并精选。', available: (props.textModelConfigured ?? props.modelConfigured) && props.sourceKind === 'url', dependency: '需要评论采集' },
  { kind: 'knowledge_cards', title: '生成知识卡片', description: '提取观点、方法和案例；新卡片会等待人工审核，不自动写入 Vault。', available: props.textModelConfigured ?? props.modelConfigured, dependency: '需要转写' },
  { kind: 'generate_scripts', title: '生成多版口播稿', description: '根据原视频最终识别文案和所选风格模板生成可编辑草稿。', available: props.textModelConfigured ?? props.modelConfigured, dependency: '需要转写' },
  { kind: 'write_obsidian', title: '保存到知识库', description: '将选中的 Markdown 写入你指定的目录。', available: false, dependency: '需要生成 Markdown' },
])

function toggle(kind: StepKind, event: Event): void {
  const option = options.value.find((item) => item.kind === kind)
  const input = event.target as HTMLInputElement
  if (!option?.available) return
  const next = new Set(props.modelValue)
  const mediaStep = props.sourceKind === 'file' ? 'import' : 'download'
  if (input.checked) {
    if (kind === 'comments' || kind === 'analyze_comments') next.add('download')
    if (kind === 'analyze_comments') next.add('comments')
    if (kind === 'transcribe' || kind === 'ocr' || kind === 'analyze_structure' || kind === 'knowledge_cards' || kind === 'generate_scripts') next.add(mediaStep)
    if (kind === 'analyze_structure' || kind === 'knowledge_cards' || kind === 'generate_scripts') next.add('transcribe')
    next.add(kind)
  } else {
    next.delete(kind)
    if (kind === 'generate_scripts') emit('update:scriptStyleIds', [])
    if (kind === 'download') {
      next.delete('comments')
      next.delete('analyze_comments')
      next.delete('transcribe')
      next.delete('ocr')
      next.delete('analyze_structure')
      next.delete('knowledge_cards')
      next.delete('generate_scripts')
    }
    if (kind === 'import') {
      next.delete('transcribe')
      next.delete('ocr')
      next.delete('analyze_structure')
      next.delete('knowledge_cards')
      next.delete('generate_scripts')
    }
    if (kind === 'comments') next.delete('analyze_comments')
    if (kind === 'transcribe') {
      next.delete('analyze_structure')
      next.delete('knowledge_cards')
      next.delete('generate_scripts')
    }
  }
  emit('update:modelValue', [...next])
}
</script>

<template>
  <div>
    <div class="option-list compact-options" role="group" aria-label="任务处理步骤">
      <div v-for="option in options" :key="option.kind" class="option-card"
        :class="{
          'is-selected': props.modelValue.includes(option.kind),
          'is-disabled': !option.available,
          'has-style-options': option.kind === 'generate_scripts' && props.modelValue.includes('generate_scripts'),
        }">
        <input :id="`step-${option.kind}`" type="checkbox" :checked="props.modelValue.includes(option.kind)"
          :disabled="!option.available" :aria-describedby="`step-${option.kind}-help`" @change="toggle(option.kind, $event)" />
        <label class="option-main" :for="`step-${option.kind}`">
          <span class="option-title">{{ option.title }}</span>
          <span :id="`step-${option.kind}-help`" class="option-desc" :title="option.description">
            {{ option.kind === 'transcribe' && props.transcriptionSummary ? props.transcriptionSummary : option.kind === 'ocr' && props.ocrSummary && props.mediaType !== 'audio' ? props.ocrSummary : !option.available ? (option.kind === 'write_obsidian' ? '需审核后写入' : option.kind === 'import' ? '本地文件' : option.kind === 'download' || option.kind === 'comments' ? '链接任务' : option.kind === 'ocr' && props.mediaType === 'audio' ? '需要视频画面' : '需配置对应模型') : option.dependency || (option.kind === 'download' ? '无封面时生成首帧可选' : option.kind === 'transcribe' ? '可在设置中选择转写方式' : '本地处理') }}
          </span>
        </label>
        <button v-if="option.kind === 'transcribe' || option.kind === 'ocr' || (option.available && props.modelValue.includes(option.kind) && (option.kind === 'download' || option.kind === 'comments'))"
          class="text-button option-settings" type="button" :aria-label="`${option.title}设置`" @click="emit('settings', option.kind)">设置</button>
        <section v-if="option.kind === 'generate_scripts' && props.modelValue.includes('generate_scripts')" class="script-style-picker" aria-labelledby="script-style-picker-title">
          <div class="script-style-picker-head">
            <div>
              <h3 id="script-style-picker-title" class="script-style-picker-title">选择本次使用的风格模板 <span class="required-marker" aria-hidden="true">*</span></h3>
              <p class="script-style-picker-help">每个模板生成一版稿件。参考资料只用于表达风格，新稿事实以原视频最终识别文案为准。</p>
            </div>
            <el-tag type="info" effect="plain">已选择 {{ selectedScriptStyleIds.length }} / {{ maxSelectedScriptStyles }} 个</el-tag>
          </div>
          <p v-if="props.scriptStyleTemplatesLoading" class="helper" role="status">正在读取已启用的风格模板…</p>
          <el-alert v-else-if="props.scriptStyleTemplatesError" type="error" :closable="false" show-icon role="alert">{{ props.scriptStyleTemplatesError }}</el-alert>
          <el-alert v-else-if="enabledScriptStyles.length === 0" type="warning" :closable="false" show-icon role="alert">没有已启用的风格模板，请先到“知识库与设置 → 口播稿风格”添加并启用模板。</el-alert>
          <el-checkbox-group v-else v-model="selectedScriptStyleIds" class="script-style-options" aria-label="选择口播稿风格模板">
            <el-checkbox v-for="template in enabledScriptStyles" :key="template.id" :label="template.id" class="script-style-option" :disabled="!selectedScriptStyleIds.includes(template.id) && selectedScriptStyleIds.length >= maxSelectedScriptStyles">
              <span class="script-style-option-copy">
                <span class="script-style-option-title">{{ template.name }}</span>
              </span>
            </el-checkbox>
          </el-checkbox-group>
          <p v-if="selectedScriptStyleIds.length >= maxSelectedScriptStyles" class="helper" role="status">每次任务最多选择 {{ maxSelectedScriptStyles }} 个模板。</p>
          <el-alert v-if="scriptStyleSelectionRequired && !props.scriptStyleTemplatesLoading && !props.scriptStyleTemplatesError" class="script-style-required" type="error" :closable="false" show-icon role="alert">请至少选择一个风格模板，才能创建任务。</el-alert>
        </section>
      </div>
    </div>
    <p class="helper dependency-note">链接任务先下载；评论筛选需要评论，结构、知识卡和口播稿需要转写。归档资料会自动加入本地全文检索。</p>
    <p v-if="!(props.textModelConfigured ?? props.modelConfigured) || !(props.asrModelConfigured ?? props.modelConfigured)" class="helper dependency-note">云端步骤需先在设置中配置相应模型 ID 和服务商 API Key。</p>
  </div>
</template>

<style scoped>
.compact-options { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.compact-options .option-card { grid-template-columns: 16px minmax(0, 1fr) auto; gap: 6px; padding: 8px; min-height: 52px; }
.compact-options .option-card.has-style-options { grid-column: 1 / -1; grid-template-columns: 16px minmax(0, 1fr); cursor: default; }
.compact-options .option-card.has-style-options .script-style-picker { grid-column: 1 / -1; width: 100%; margin: 4px 0 0; padding: 12px; border-color: var(--pine); background: var(--paper); }
.compact-options .option-card input { width: 14px; height: 14px; margin-top: 1px; }
.compact-options .option-main { display: block; cursor: pointer; }
.compact-options .is-disabled .option-main { cursor: not-allowed; }
.compact-options .option-title { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); line-height: 1.35; }
.compact-options .option-desc { display: block; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.4; }
.option-settings { align-self: start; font-size: var(--fs-body-sm); white-space: nowrap; }
.dependency-note { margin: 7px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.script-style-picker { display: grid; gap: 10px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }
.script-style-picker-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.script-style-picker-title { margin: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); line-height: var(--lh-base); }
.script-style-picker-help { margin: 3px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.script-style-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 2px 8px; }
.script-style-options :deep(.el-checkbox) { min-width: 0; min-height: 32px; height: auto; margin: 0; padding: 3px 6px; align-items: center; border-color: var(--line); border-radius: 6px; white-space: normal; }
.script-style-options :deep(.el-checkbox.is-checked) { border-color: var(--pine); background: var(--pine-soft); }
.script-style-options :deep(.el-checkbox__input) { margin-top: 2px; }
.script-style-options :deep(.el-checkbox__label) { display: block; min-width: 0; padding-left: 8px; white-space: normal; }
.script-style-option-copy { display: grid; gap: 3px; min-width: 0; }
.script-style-option-title { color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.script-style-required { margin-top: 0; }
@media (max-width: 1050px) { .compact-options { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) { .compact-options { grid-template-columns: 1fr; } }
</style>
