<script setup lang="ts">
import { ref, watch } from 'vue'
import type { CommentCollectionState, TaskArtifact, TaskConsentRecord, TaskDetail, TaskRecord, TaskStep, TaskTranscriptionConfig } from '../api/types'
import TranscriptionRetrySettings from './TranscriptionRetrySettings.vue'

const props = defineProps<{
  task: TaskRecord
  artifacts: TaskArtifact[]
  consents: TaskConsentRecord[]
  modelRuns: TaskDetail['model_runs']
  transcriptionConfig: TaskTranscriptionConfig
  commentState: CommentCollectionState | null
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'retry', stepId: string, ocrAction?: 'continue' | 'restart', transcriptionAuthorized?: boolean): void
  (event: 'skip', stepId: string): void
  (event: 'reauthorize-comments', stepId: string, artifactId: string): void
}>()
const authorizeUpdatedComments = ref(false)
const retryApprovedStepId = ref('')
watch(() => props.task.id, () => {
  retryApprovedStepId.value = ''
  authorizeUpdatedComments.value = false
})

const stepLabels: Record<string, string> = {
  download: '下载视频与封面', import: '导入本地媒体', transcribe: '语音转文字', ocr: '字幕 OCR',
  comments: '采集公开评论', analyze_comments: '筛选评论', analyze_structure: '拆解内容结构',
  knowledge_cards: '生成知识卡', generate_scripts: '生成口播稿', index: '建立本地索引',
  write_obsidian: '写入知识库',
}

const statusLabels: Record<string, string> = {
  queued: '排队中', running: '处理中', interrupted: '已中断', completed: '已完成', partial: '部分完成',
  failed: '失败', blocked: '受阻', cancelled: '已取消', skipped: '已跳过', pending: '等待中', awaiting_authorization: '等待图片授权',
}

function label(value: string): string {
  return statusLabels[value] ?? '未知状态'
}

function hasUnsuccessfulDependency(step: TaskStep, task: TaskRecord, visited = new Set<string>()): boolean {
  for (const dependencyId of step.depends_on) {
    if (visited.has(dependencyId)) continue
    visited.add(dependencyId)
    const dependency = task.steps.find((candidate) => candidate.id === dependencyId)
    if (!dependency) continue
    if (['failed', 'blocked', 'interrupted', 'cancelled'].includes(dependency.status)) return true
    if (hasUnsuccessfulDependency(dependency, task, visited)) return true
  }
  return false
}

function stepStatusLabel(step: TaskStep, task: TaskRecord): string {
  if (step.kind === 'ocr' && step.status === 'awaiting_authorization') return '等待继续'
  if (step.status === 'queued' && hasUnsuccessfulDependency(step, task)) return '未执行（前置步骤未完成）'
  return label(step.status)
}

function stepMessage(step: TaskStep): string | null {
  if (step.kind === 'ocr' && step.status === 'awaiting_authorization') return '关键帧已生成，继续后将发送给云端模型识别。'
  return step.message
}

function cssStatus(value: string): string {
  if (value === 'completed') return 'completed'
  if (['failed', 'blocked'].includes(value)) return 'failed'
  if (['queued', 'running', 'interrupted', 'partial', 'cancelled', 'awaiting_authorization'].includes(value)) return 'warn'
  return ''
}

function artifactNames(ids: string[], artifacts: TaskArtifact[]): string {
  if (!ids.length) return '无'
  let chunks = 0
  let internal = 0
  const names: string[] = []
  for (const id of ids) {
    const artifact = artifacts.find((item) => item.id === id)
    if (artifact?.kind === 'asr_audio_chunk') { chunks++; continue }
    if (artifact && (artifact.kind === 'metadata' || artifact.name === 'metadata.md' || artifact.kind.endsWith('_raw') || artifact.kind.endsWith('_json'))) { internal++; continue }
    names.push(artifact?.name ?? `产物 ${id.slice(0, 8)}`)
  }
  if (chunks) names.push(`语音处理分片（${chunks} 段）`)
  if (internal) names.push(`内部处理记录（${internal} 项）`)
  return names.join('、')
}

function outputArtifactNames(step: TaskStep, ids: string[], artifacts: TaskArtifact[]): string {
  if (step.kind !== 'knowledge_cards') return artifactNames(ids, artifacts)
  const cards = ids
    .map((id) => artifacts.find((artifact) => artifact.id === id))
    .filter((artifact): artifact is TaskArtifact => artifact?.kind === 'knowledge_card')
  return cards.length ? cards.map((artifact) => artifact.name).join('、') : '暂无知识卡'
}

function dependencyNames(ids: string[], task: TaskRecord): string {
  if (!ids.length) return '无'
  return ids.map((id) => {
    const dependency = task.steps.find((step) => step.id === id)
    return dependency ? stepLabels[dependency.kind] ?? dependency.kind : '未知前置步骤'
  }).join('、')
}

function canRetry(step: TaskStep, taskStatus: string): boolean {
  return ['download', 'import', 'comments', 'transcribe', 'ocr', 'index'].includes(step.kind)
    && (['failed', 'interrupted', 'blocked', 'cancelled', 'partial'].includes(step.status) || (step.kind === 'ocr' && step.status === 'awaiting_authorization'))
    && !['queued', 'running'].includes(taskStatus)
}

function canRetryModelStep(step: TaskStep, task: TaskRecord): boolean {
  return ['analyze_structure', 'knowledge_cards', 'generate_scripts'].includes(step.kind)
    && ['failed', 'interrupted', 'blocked', 'cancelled', 'partial'].includes(step.status)
    && !['queued', 'running'].includes(task.status)
    && !hasUnsuccessfulDependency(step, task)
}

function isLocalOcr(step: TaskStep): boolean {
  return step.kind === 'ocr' && !props.consents.some((consent) => consent.step_kind === 'ocr')
}

function canRetryAnalysis(step: TaskStep, taskStatus: string): boolean {
  return step.kind === 'analyze_comments'
    && step.error_code !== 'comments_analysis_stale'
    && ['failed', 'interrupted', 'blocked', 'cancelled'].includes(step.status)
    && !['queued', 'running'].includes(taskStatus)
}

function canSkip(step: TaskStep, taskStatus: string): boolean {
  return ['queued', 'running', 'awaiting_authorization'].includes(taskStatus)
    && (step.status === 'queued' || (step.kind === 'ocr' && step.status === 'running'))
}

function elapsedLabel(startedAt: string | null): string {
  if (!startedAt) return '未知'
  const timestamp = Date.parse(startedAt)
  if (Number.isNaN(timestamp)) return '未知'
  const elapsed = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  const minutes = Math.floor(elapsed / 60)
  const seconds = elapsed % 60
  return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`
}

function canContinueFirstAnalysis(step: TaskStep): boolean {
  const consent = [...props.consents].reverse().find((item) => item.step_kind === 'analyze_comments')
  return step.kind === 'analyze_comments' && step.error_code === 'comments_analysis_stale'
    && !['queued', 'running'].includes(props.task.status)
    && Boolean(consent?.authorized && consent.artifact_ids.length === 0)
    && !props.modelRuns.some((run) => run.step_id === step.id)
    && Boolean(latestCommentNote(props.artifacts))
}

function retryAnalysis(stepId: string): void {
  if (retryApprovedStepId.value !== stepId) return
  retryApprovedStepId.value = ''
  emit('retry', stepId)
}

function retryModelStep(stepId: string): void {
  if (retryApprovedStepId.value !== stepId) return
  retryApprovedStepId.value = ''
  emit('retry', stepId)
}

function analysisModel(consents: TaskConsentRecord[]): string {
  const consent = [...consents].reverse().find((item) => item.step_kind === 'analyze_comments' && item.authorized)
  return consent ? `${consent.provider_id} · ${consent.model_id}` : '原授权模型'
}

function latestCommentNote(artifacts: TaskArtifact[]): TaskArtifact | undefined {
  return artifacts.filter((item) => item.kind === 'note' && item.metadata.scope === 'comments')
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.version - a.version || b.id.localeCompare(a.id))[0]
}

function authorizeComments(stepId: string, artifactId: string): void {
  if (!authorizeUpdatedComments.value) return
  authorizeUpdatedComments.value = false
  emit('reauthorize-comments', stepId, artifactId)
}
</script>

<template>
  <ol class="task-step-list">
    <li v-for="step in task.steps" :key="step.id" class="task-step-card">
      <details class="task-step-details">
        <summary class="task-step-summary">
          <span class="task-step-index">{{ step.ordinal + 1 }}</span>
          <span class="task-step-summary-content">
            <span class="task-step-name">{{ stepLabels[step.kind] ?? step.kind }}</span>
            <span v-if="stepMessage(step)" class="task-step-summary-message" :title="stepMessage(step) ?? ''">{{ stepMessage(step) }}</span>
            <span v-else class="task-step-summary-message">{{ step.status === 'queued' ? '等待前置步骤或可用资源' : '点击查看详情' }}</span>
          </span>
          <span class="status-badge" :class="cssStatus(step.status)">{{ stepStatusLabel(step, task) }}</span>
          <span class="task-step-expand" aria-hidden="true">⌄</span>
        </summary>
        <div class="task-step-content">
          <div v-if="step.status === 'running' && step.progress !== null" class="task-progress" :aria-label="`处理进度 ${Math.round(step.progress)}%`">
            <div class="task-progress-track"><span :style="{ width: `${Math.min(100, Math.max(0, step.progress))}%` }"></span></div>
            <span>{{ Math.round(step.progress) }}%</span>
          </div>
          <div v-else-if="step.status === 'running'" class="task-progress" role="status" aria-label="步骤正在处理">
            <div class="task-progress-track is-indeterminate"><span></span></div>
            <span>处理中</span>
          </div>

          <p v-if="stepMessage(step)" class="task-step-message" :class="{ 'task-step-error': Boolean(step.error_code) }">{{ stepMessage(step) }}</p>
          <p v-if="step.kind === 'analyze_comments' && step.status === 'running'" class="helper">取消不能立即中断当前云端请求；单次连接或读取等待上限为 10 分钟，并非整个步骤的总耗时上限。</p>
          <p v-if="step.kind === 'comments' && commentState?.stop_reason && (step.status === 'partial' || commentState.stop_reason.includes('楼中楼'))" class="task-step-message">已保存 {{ commentState.collected_count }} 条，{{ commentState.page_count }} 页。{{ step.status === 'partial' ? '停止原因' : '采集提示' }}：{{ commentState.stop_reason }}</p>
          <dl class="task-step-artifacts">
            <div><dt>前置步骤</dt><dd>{{ dependencyNames(step.depends_on, task) }}</dd></div>
            <div><dt>输入产物</dt><dd>{{ artifactNames(step.input_artifact_ids, artifacts) }}</dd></div>
            <div><dt>输出产物</dt><dd>{{ outputArtifactNames(step, step.output_artifact_ids, artifacts) }}</dd></div>
            <div><dt>重试次数</dt><dd>{{ step.retry_count }}</dd></div>
          </dl>

          <div v-if="step.kind === 'ocr' && step.status === 'running'" class="task-step-actions">
            <p v-if="step.message?.startsWith('本地 OCR')">跳过会在当前画面处理完成后停止 OCR，其他步骤继续；顶部“取消任务”会停止整条任务。</p>
            <p v-else-if="step.message?.startsWith('云端 OCR 正在批量识别')">云端请求已耗时 {{ elapsedLabel(step.started_at) }}。请求发出后无法撤回；跳过会在当前请求返回后生效，服务商可能仍会计费。顶部“取消任务”会停止整条任务。</p>
            <p v-else>云端请求尚未发出；跳过会在本机关键帧准备结束后生效，后续步骤继续。顶部“取消任务”会停止整条任务。</p>
          </div>

          <div v-if="canSkip(step, task.status)" class="task-step-actions">
            <p v-if="step.status === 'queued'">跳过后，依赖此步骤且仍在排队的步骤也会一并跳过。</p>
            <button class="button button-secondary button-small" type="button" :disabled="busy || step.error_code === 'step_skip_requested'" @click="emit('skip', step.id)">
              {{ step.error_code === 'step_skip_requested' ? '正在停止…' : busy ? '正在提交…' : '跳过此步骤' }}
            </button>
          </div>

          <div v-if="isLocalOcr(step) && canRetry(step, task.status)" class="task-step-actions">
            <p>继续会复用已保存的识别画面；没有可用记录时从第一张开始。重新开始会清除已保存记录。</p>
            <div class="button-row">
              <button class="button button-small" type="button" :disabled="busy" @click="emit('retry', step.id, 'continue')">
                {{ busy ? '正在提交…' : '继续' }}
              </button>
              <button class="button button-secondary button-small" type="button" :disabled="busy" @click="emit('retry', step.id, 'restart')">
                重新开始
              </button>
            </div>
          </div>
          <div v-if="step.kind === 'transcribe' && canRetry(step, task.status)" class="task-step-actions">
            <TranscriptionRetrySettings
              :task-id="task.id"
              :step-id="step.id"
              :config="transcriptionConfig"
              :busy="busy"
              @retry="(stepId, transcriptionAuthorized) => emit('retry', stepId, undefined, transcriptionAuthorized)"
            />
          </div>
          <div v-else-if="canRetry(step, task.status) && !isLocalOcr(step)" class="task-step-actions">
            <button class="button button-secondary button-small" type="button" :disabled="busy" @click="emit('retry', step.id)">
              {{ busy ? '正在提交…' : step.kind === 'ocr' && step.status === 'awaiting_authorization' ? '继续识别' : step.kind === 'import' && step.status === 'interrupted' ? '继续本地导入' : '重试此步骤' }}
            </button>
          </div>
          <div v-if="canRetryModelStep(step, task)" class="task-step-actions">
            <p v-if="step.kind === 'knowledge_cards'">会先复用与当前口播稿、提示词和模型相符的已保存响应；没有可用响应时才请求模型，可能产生费用。中断前已保存的卡片会保留。</p>
            <p v-else-if="step.kind === 'analyze_structure'">将使用已保存的转写和画面文字重新调用当前文本模型，可能产生费用；已有分析产物会保留。</p>
            <p v-else>将按原任务选择的文案风格重新调用当前文本模型，可能产生费用；已有口播稿会保留。</p>
            <label class="consent-check"><input type="checkbox" :checked="retryApprovedStepId === step.id" :disabled="busy" @change="retryApprovedStepId = ($event.target as HTMLInputElement).checked ? step.id : ''" /> {{ step.kind === 'knowledge_cards' ? '我已了解没有可复用响应时会重新调用云端模型，并可能产生额外费用。' : '我已了解本次重试会重新调用云端模型，并可能产生额外费用。' }}</label>
            <button class="button button-secondary button-small" type="button" :disabled="busy || retryApprovedStepId !== step.id" @click="retryModelStep(step.id)">
              {{ busy ? '正在提交…' : step.kind === 'knowledge_cards' ? '确认并重新生成知识卡' : step.kind === 'analyze_structure' ? '确认并重新分析内容结构' : '确认并重新生成口播稿' }}
            </button>
          </div>
          <div v-if="canRetryAnalysis(step, task.status)" class="task-step-actions">
            <p v-if="step.error_code === 'provider_timeout'">本次请求结果未知，服务商可能已经处理或计费。请先查看调用记录；重新请求可能产生额外费用。</p>
            <a v-if="modelRuns.some((run) => run.step_id === step.id)" href="#model-runs-title">查看云端模型调用记录</a>
            <p>将使用 {{ analysisModel(consents) }} 重新发送本步骤的评论资料；费用未知。</p>
            <label class="consent-check"><input type="checkbox" :checked="retryApprovedStepId === step.id" :disabled="busy" @change="retryApprovedStepId = ($event.target as HTMLInputElement).checked ? step.id : ''" /> 我已核对本次请求状态，同意再次发送评论并承担可能产生的费用</label>
            <button class="button button-secondary button-small" type="button" :disabled="busy || retryApprovedStepId !== step.id" @click="retryAnalysis(step.id)">确认重试筛选评论</button>
          </div>
          <div v-if="canContinueFirstAnalysis(step)" class="task-step-actions">
            <p>尚未发送评论；将使用创建任务时已授权的模型筛选已保存的评论。</p>
            <button class="button button-secondary button-small" type="button" :disabled="busy" @click="emit('retry', step.id)">继续筛选已采集评论</button>
          </div>
          <div v-else-if="step.kind === 'analyze_comments' && step.error_code === 'comments_analysis_stale' && !['queued', 'running'].includes(task.status) && latestCommentNote(artifacts)" class="task-step-actions">
            <p>评论资料可能不完整或已经更新：{{ latestCommentNote(artifacts)?.name }}（{{ latestCommentNote(artifacts)?.id }}）。将发送给本机当前配置的文本模型，费用未知。</p>
            <label><input v-model="authorizeUpdatedComments" type="checkbox" :disabled="busy" /> 我已了解评论可能不完整，同意发送当前评论并分析</label>
            <button class="button button-secondary button-small" type="button" :disabled="busy || !authorizeUpdatedComments" @click="authorizeComments(step.id, latestCommentNote(artifacts)!.id)">重新授权并分析评论</button>
          </div>
        </div>
      </details>
    </li>
  </ol>
</template>
