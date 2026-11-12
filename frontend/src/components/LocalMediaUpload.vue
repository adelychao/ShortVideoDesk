<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElAlert, ElButton, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/message-box/style/css'
import { ApiError, api } from '../api/client'
import type { CollectionMediaSelectResult, UploadReceipt } from '../api/types'

const props = withDefaults(defineProps<{
  disabled?: boolean
  resetToken?: number
  large?: boolean
  compact?: boolean
  preview?: string
  previewState?: 'idle' | 'loading' | 'ready' | 'error'
  details?: string
  publishTarget?: boolean
  restoredReceipt?: UploadReceipt | null
}>(), {
  disabled: false,
  resetToken: 0,
  large: false,
  compact: false,
  preview: '',
  previewState: 'idle',
  details: '',
  publishTarget: false,
  restoredReceipt: null,
})
const emit = defineEmits<{
  (event: 'uploaded', receipt: UploadReceipt | null, file?: File): void
  (event: 'busyChange', busy: boolean): void
}>()

const busy = ref(false)
const receipt = ref<UploadReceipt | null>(null)
const error = ref('')
const softLimit = 2 * 1024 ** 3

watch(() => props.restoredReceipt, (value) => {
  if (receipt.value?.uploadId === value?.uploadId) return
  receipt.value = value || null
  error.value = ''
}, { immediate: true })

function formatSize(bytes: number): string {
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '媒体选择失败，请确认文件后重试。'
}

async function discardReceipt(prior: UploadReceipt): Promise<void> {
  if (prior.sourceType === 'local') {
    if (!props.publishTarget) await api.deleteCollectionMedia(prior.uploadId).catch(() => undefined)
    return
  }
  await api.deleteUpload(prior.uploadId).catch(() => undefined)
}

async function applyReceipt(next: UploadReceipt): Promise<void> {
  const prior = receipt.value
  receipt.value = next
  emit('uploaded', next)
  if (prior && prior.uploadId !== next.uploadId) await discardReceipt(prior)
}

async function selectOriginalMedia(): Promise<void> {
  if (props.disabled || busy.value) return
  busy.value = true
  error.value = ''
  emit('busyChange', true)
  try {
    let result: CollectionMediaSelectResult = props.publishTarget
      ? await api.selectLocalVideo()
      : await api.selectCollectionMedia(false)
    if (!props.publishTarget && result.overLimit) {
      // 超过 2 GB 软上限：先确认，再带着确认状态重新选择原文件。
      try {
        await ElMessageBox.confirm(
          `文件大小为 ${formatSize(result.sizeBytes ?? 0)}，超过 2 GB 软上限。原文件不会被复制或移动，确定继续吗？`,
          '确认导入大文件',
          { confirmButtonText: '继续导入', cancelButtonText: '取消', type: 'warning', distinguishCancelAndClose: true },
        )
      } catch {
        return
      }
      result = await api.selectCollectionMedia(true)
    }
    if (!result.source) return
    await applyReceipt(result.source)
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
    emit('busyChange', false)
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (props.disabled || busy.value) return
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    void selectOriginalMedia()
  }
}

async function forgetReceipt(): Promise<void> {
  const prior = receipt.value
  receipt.value = null
  error.value = ''
  emit('uploaded', null)
  if (prior) await discardReceipt(prior)
}

watch(() => props.resetToken, () => {
  receipt.value = null
  error.value = ''
  emit('uploaded', null)
})
</script>

<template>
  <div class="local-upload" :class="{ 'is-large': large, 'is-compact': compact && receipt?.detectedMediaType === 'video' }">
    <template v-if="publishTarget">
      <div v-if="receipt?.detectedMediaType === 'video'" class="local-upload-compact-card">
        <figure class="local-upload-thumbnail">
          <img v-if="preview" :src="preview" alt="视频首帧预览" />
          <span v-else>{{ previewState === 'error' ? '首帧暂不可用' : '正在提取首帧…' }}</span>
        </figure>
        <div class="local-upload-compact-copy">
          <span class="local-upload-status"><i aria-hidden="true">✓</i>已关联本机原创视频</span>
          <strong :title="receipt.sourcePath || receipt.filename">{{ receipt.filename }}</strong>
          <small>{{ formatSize(receipt.sizeBytes) }} · 视频格式<span v-if="details"> · {{ details }}</span></small>
        </div>
        <div class="local-upload-compact-actions">
          <ElButton class="button button-secondary button-small" :disabled="disabled || busy" @click="selectOriginalMedia">更换视频</ElButton>
          <ElButton text class="text-button" :disabled="disabled || busy" @click="forgetReceipt">移除视频</ElButton>
        </div>
      </div>
      <div v-else class="local-upload-zone" :class="{ 'is-disabled': disabled || busy }" @click="selectOriginalMedia">
        <span class="local-upload-icon" aria-hidden="true">↥</span>
        <span class="local-upload-copy"><strong>{{ busy ? '正在读取视频…' : '选择本机原创视频' }}</strong><small>支持 MP4、MOV、MKV、WEBM；文件大小和时长要求以所选发布平台为准。发布前请保持原文件路径和内容不变。</small></span>
        <ElButton class="button button-secondary button-small" :disabled="disabled || busy" @click.stop="selectOriginalMedia">选择视频</ElButton>
      </div>
      <p v-if="!receipt" class="field-note">选择后直接使用原视频，不会复制或移动原文件。</p>
    </template>
    <div
      v-else
      class="local-upload-zone"
      :class="{ 'is-disabled': disabled || busy }"
      role="button"
      tabindex="0"
      :aria-label="receipt ? '更换本地媒体文件' : '选择本地媒体文件'"
      @click="selectOriginalMedia"
      @keydown="onKeydown"
    >
      <span class="local-upload-icon" aria-hidden="true">↥</span>
      <span class="local-upload-copy">
        <strong>{{ busy ? '正在校验所选文件…' : receipt ? `${receipt.detectedMediaType === 'audio' ? '音频' : '视频'}已选择并通过格式校验` : '点击选择本机媒体文件' }}</strong>
        <small>支持 MP4、MOV、MKV、WEBM、MP3、M4A、WAV；直接使用原文件，不会复制或移动；创建处理任务时校验后的副本写入归档目录。</small>
      </span>
      <ElButton class="button button-secondary button-small" native-type="button" :disabled="disabled || busy" @click.stop="selectOriginalMedia">
        {{ busy ? '校验中…' : receipt ? '更换文件' : '选择文件' }}
      </ElButton>
    </div>

    <div v-if="!publishTarget && receipt" class="local-upload-receipt" role="status">
      <span :title="receipt.sourcePath"><strong>{{ receipt.filename }}</strong> · {{ receipt.detectedMediaType === 'audio' ? '音频' : '视频' }} · {{ formatSize(receipt.sizeBytes) }}</span>
      <ElButton text class="text-button" native-type="button" :disabled="busy || disabled" @click.stop="forgetReceipt">移除</ElButton>
    </div>
    <p v-if="!publishTarget" class="field-note">2 GB 为软上限；超过后会先确认，再由本机处理。原文件不会被移动或删除。</p>
    <ElAlert v-if="error" class="info-box form-error" type="error" :closable="false" :title="error" />
  </div>
</template>

<style scoped>
.local-upload { display: grid; gap: 10px; }
.local-upload-zone { display: flex; align-items: center; gap: 14px; min-height: 92px; padding: 16px; border: 1px dashed var(--line); border-radius: 12px; background: var(--paper-2); cursor: pointer; transition: border-color .18s ease, background .18s ease; }
.local-upload-zone:hover { border-color: var(--pine); background: var(--pine-soft); }
.local-upload-zone.is-disabled { opacity: .65; cursor: not-allowed; }
.local-upload-icon { display: grid; width: 40px; height: 40px; place-items: center; flex: 0 0 auto; border-radius: 11px; background: var(--pine-soft); color: var(--pine); font-size: 24px; }
.local-upload-copy { display: grid; gap: 5px; flex: 1; min-width: 0; }
.local-upload-copy strong { color: var(--ink); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.local-upload-copy small { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.local-upload-receipt { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 9px; background: var(--paper-2); color: var(--muted); font-size: var(--fs-body-sm); }
.local-upload-receipt strong { color: var(--ink); }
.local-upload-compact-card { display: grid; grid-template-columns: 58px minmax(0, 1fr) auto; align-items: center; gap: 10px; min-height: 96px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: #fff; transition: border-color .18s ease, background .18s ease; }
.local-upload-thumbnail { position: relative; display: grid; width: 58px; height: 74px; place-items: center; overflow: hidden; margin: 0; border: 1px solid var(--line); border-radius: 7px; background: var(--paper-2); }
.local-upload-thumbnail img { width: 100%; height: 100%; object-fit: cover; }
.local-upload-thumbnail > span { padding: 4px; color: var(--muted); font-size: var(--fs-caption); line-height: 1.3; text-align: center; }
.local-upload-compact-copy { display: grid; min-width: 0; gap: 4px; }
.local-upload-compact-copy strong { overflow: hidden; color: var(--ink); font-size: var(--fs-body); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.local-upload-compact-copy small { overflow: hidden; color: var(--text-tertiary); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.local-upload-status { display: flex; align-items: center; gap: 5px; color: #238451; font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.local-upload-status i { display: grid; width: 16px; height: 16px; place-items: center; border-radius: 50%; background: #e5f4e9; font-size: 10px; font-style: normal; }
.local-upload-compact-actions { display: grid; justify-items: center; gap: 4px; padding-left: 10px; border-left: 1px solid var(--line); }
.local-upload-compact-actions .button { min-height: 30px; padding: 4px 10px; font-size: var(--fs-caption); }
.local-upload-compact-actions .text-button { font-size: var(--fs-caption); }
.local-upload.is-compact { gap: 8px; }
.local-upload.is-compact .field-note { display: none; }
.field-note { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.local-upload.is-large .local-upload-zone { min-height: min(54vh, 420px); flex-direction: column; justify-content: center; gap: 18px; padding: 28px; border-style: dashed; background: #fbfcfd; text-align: center; }
.local-upload.is-large .local-upload-zone:hover { background: var(--pine-soft); }
.local-upload.is-large .local-upload-icon { width: 56px; height: 56px; border-radius: 16px; font-size: 31px; }
.local-upload.is-large .local-upload-copy { flex: 0 1 auto; gap: 8px; }
.local-upload.is-large .local-upload-copy strong { font-size: 16px; }
.local-upload.is-large .local-upload-copy small { max-width: 620px; }
.local-upload.is-large .local-upload-zone > .el-button { width: min(280px, 100%); min-height: 42px; margin-left: 0; }
.local-upload.is-large .local-upload-zone :deep(.el-button.button-secondary) { border-color: var(--pine); background: var(--pine); color: #fff; }
@media (max-width: 560px) { .local-upload-zone { align-items: flex-start; flex-wrap: wrap; } .local-upload-copy { min-width: calc(100% - 60px); } .local-upload-zone > .el-button { margin-left: 54px; } }
@media (max-width: 560px) { .local-upload.is-large .local-upload-zone { min-height: 300px; align-items: center; padding: 18px; } .local-upload.is-large .local-upload-copy { min-width: 0; } }
@media (max-width: 560px) { .local-upload-compact-card { grid-template-columns: 50px minmax(0, 1fr); gap: 8px; padding: 8px; } .local-upload-thumbnail { width: 50px; height: 66px; } .local-upload-compact-actions { grid-column: 2; display: flex; justify-content: flex-start; justify-items: initial; padding-left: 0; border-left: 0; } }
</style>
