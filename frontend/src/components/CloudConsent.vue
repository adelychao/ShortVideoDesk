<script setup lang="ts">
import { computed } from 'vue'
import type { StepKind } from '../api/types'

const props = withDefaults(defineProps<{
  steps: StepKind[]
  modelConfigured: boolean
  providerName?: string | null
  modelName?: string | null
  costEstimate?: string | null
  authorized: boolean
}>(), {
  providerName: null,
  modelName: null,
  costEstimate: null,
})

const emit = defineEmits<{
  (event: 'update:authorized', value: boolean): void
}>()

const materialByStep: Partial<Record<StepKind, string>> = {
  transcribe: '本次选择的视频或音频中的语音片段',
  ocr: '本次视频按时长均匀抽取的关键帧图像（最多 8 张）及其中可见文字',
  analyze_comments: '本次选择的评论文本与必要的上下文',
  analyze_structure: '本次选择的转写文本和视频元数据',
  knowledge_cards: '本次选择的转写、评论精选或结构分析',
  generate_scripts: '本任务最终转写稿、本次选中的风格模板指令及其明确关联的参考 Markdown',
}

const selectedMaterials = computed(() => props.steps
  .map((step) => materialByStep[step])
  .filter((material): material is string => Boolean(material)))
const selectedModels = computed(() => props.modelName ? [props.modelName] : [...new Set(props.steps.flatMap((step) => {
  if (step === 'transcribe') return ['音频转写模型']
  if (step === 'ocr' || step === 'analyze_comments' || step === 'analyze_structure' || step === 'knowledge_cards' || step === 'generate_scripts') return ['文本与图像模型']
  return []
}))])
const canAuthorize = computed(() => selectedMaterials.value.length > 0 && props.modelConfigured)
const costLabel = computed(() => props.costEstimate ?? '无法估算')

function updateAuthorization(event: Event): void {
  const checkbox = event.target as HTMLInputElement
  emit('update:authorized', canAuthorize.value && checkbox.checked)
}
</script>

<template>
  <section class="cloud-consent" aria-labelledby="cloud-consent-title">
    <div>
      <strong id="cloud-consent-title">云端模型授权</strong>
      <span>只对本次勾选的模型步骤生效；具体输入产物会在调用前写入本地授权记录。</span>
    </div>
    <ul v-if="selectedMaterials.length" aria-label="可能发送给模型的材料">
      <li v-for="material in selectedMaterials" :key="material">{{ material }}</li>
    </ul>
    <p v-else class="provider-note">当前所选步骤不向云端发送材料。</p>
    <dl class="scope-list" aria-label="模型与费用信息">
      <div><dt>服务商</dt><dd>{{ providerName || '所选服务商' }}</dd></div>
      <div><dt>模型</dt><dd>{{ modelName || selectedModels.join('、') || '未配置' }}</dd></div>
      <div><dt>费用估算</dt><dd>{{ costLabel }}</dd></div>
    </dl>
    <label class="consent-check">
      <input
        type="checkbox"
        :checked="authorized"
        :disabled="!canAuthorize"
        aria-describedby="cloud-consent-status"
        @change="updateAuthorization"
      />
      <span>我已了解本次材料范围和费用未知，并授权所选云端步骤。</span>
    </label>
    <p id="cloud-consent-status" class="provider-note" role="status">
      {{ !modelConfigured
        ? '模型尚未配置；当前不能授权或调用云端服务。'
        : selectedMaterials.length
          ? (authorized ? '已授权；模型调用前会绑定本次任务的具体产物 ID。估算费用未知。' : '选择的云端步骤需要单独授权后才可提交。')
          : '当前所选步骤不向云端发送材料。' }}
    </p>
  </section>
</template>
