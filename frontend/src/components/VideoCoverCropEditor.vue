<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDialog, ElTabPane, ElTabs, ElUpload } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/upload/style/css'
import { Cropper } from 'vue-advanced-cropper'
import 'vue-advanced-cropper/dist/style.css'

const props = defineProps<{
  open: boolean
  title: string
  aspectRatio: number
  videoSrc: string
  initialImageSrc?: string
  initialImageName?: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  (event: 'save', file: File): void
  (event: 'close'): void
}>()

const stage = ref<'source' | 'crop'>('source')
const sourceTab = ref<'image' | 'video'>('image')
const imageSource = ref('')
const imageName = ref('')
const imagePicker = ref<{ clearFiles?: () => void } | null>(null)
const video = ref<HTMLVideoElement | null>(null)
const cropCanvas = ref<HTMLCanvasElement | null>(null)
const cropReady = ref(false)
const busy = ref(false)
const error = ref('')
const objectUrls = new Set<string>()
const ratioLabel = computed(() => {
  const ratio = props.aspectRatio
  const known: Array<[number, string]> = [[3 / 4, '3:4'], [4 / 3, '4:3'], [16 / 9, '16:9'], [1, '1:1'], [9 / 16, '9:16']]
  const match = known.find(([value]) => Math.abs(value - ratio) / value < 0.02)
  return match?.[1] || `原始比例 ${ratio.toFixed(2)}:1`
})

function defaultCropSize({ imageSize, sizeRestrictions }: {
  imageSize: { width: number; height: number }
  sizeRestrictions?: { maxWidth: number; maxHeight: number }
}): { width: number; height: number } {
  const ratio = props.aspectRatio
  if (!Number.isFinite(ratio) || ratio <= 0 || imageSize.width <= 0 || imageSize.height <= 0) return imageSize
  const maxWidth = Math.min(
    imageSize.width,
    imageSize.height * ratio,
    sizeRestrictions?.maxWidth ?? Number.POSITIVE_INFINITY,
    (sizeRestrictions?.maxHeight ?? Number.POSITIVE_INFINITY) * ratio,
  )
  return { width: maxWidth, height: maxWidth / ratio }
}

function ownObjectUrl(blob: Blob): string {
  const url = URL.createObjectURL(blob)
  objectUrls.add(url)
  return url
}

function clearImageSource(): void {
  if (imageSource.value && objectUrls.has(imageSource.value)) {
    URL.revokeObjectURL(imageSource.value)
    objectUrls.delete(imageSource.value)
  }
  imageSource.value = ''
  imageName.value = ''
  cropReady.value = false
  cropCanvas.value = null
}

function reset(): void {
  clearImageSource()
  stage.value = 'source'
  sourceTab.value = 'image'
  error.value = ''
  busy.value = false
  imagePicker.value?.clearFiles?.()
}

watch(() => props.open, (open) => {
  reset()
  if (open && props.initialImageSrc) {
    imageSource.value = props.initialImageSrc
    imageName.value = props.initialImageName || '当前封面'
    stage.value = 'crop'
  }
})

function chooseImage(file?: File): void {
  if (!file) return
  if (!/^image\/(jpeg|png|webp)$/i.test(file.type) && !/\.(jpe?g|png|webp)$/i.test(file.name)) {
    error.value = '封面图片仅支持 JPG、PNG 或 WEBP。'
    return
  }
  if (file.size > 20 * 1024 * 1024) {
    error.value = '封面图片不能超过 20 MB。'
    return
  }
  clearImageSource()
  imageSource.value = ownObjectUrl(file)
  imageName.value = file.name
  stage.value = 'crop'
  error.value = ''
}

function onImageUploadChange(uploadFile: { raw?: File }): void {
  const file = uploadFile.raw
  imagePicker.value?.clearFiles?.()
  if (file) chooseImage(file)
}

function handleSourceTabChange(value: string | number): void {
  if (value === 'image' || value === 'video') {
    sourceTab.value = value
    error.value = ''
  }
}

function captureFrame(): void {
  const player = video.value
  if (!player || player.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !player.videoWidth || !player.videoHeight) {
    error.value = '视频帧尚未加载，请稍候再截取。'
    return
  }
  const canvas = document.createElement('canvas')
  canvas.width = player.videoWidth
  canvas.height = player.videoHeight
  const context = canvas.getContext('2d')
  if (!context) {
    error.value = '当前浏览器无法读取视频帧。'
    return
  }
  player.pause()
  context.drawImage(player, 0, 0, canvas.width, canvas.height)
  canvas.toBlob((blob) => {
    if (!blob) {
      error.value = '截取视频帧失败，请换一个时间点重试。'
      return
    }
    clearImageSource()
    imageSource.value = ownObjectUrl(blob)
    imageName.value = `视频帧 ${player.currentTime.toFixed(1)} 秒`
    stage.value = 'crop'
    error.value = ''
  }, 'image/jpeg', 0.94)
}

function onCropChange(result: { canvas?: HTMLCanvasElement }): void {
  cropCanvas.value = result.canvas || null
  cropReady.value = Boolean(result.canvas)
}

function goBack(): void {
  clearImageSource()
  stage.value = 'source'
  error.value = ''
}

function close(): void {
  reset()
  emit('close')
}

function save(): void {
  const canvas = cropCanvas.value
  if (!canvas || busy.value) return
  busy.value = true
  canvas.toBlob((blob) => {
    busy.value = false
    if (!blob) {
      error.value = '封面裁剪结果无法导出，请调整选区后重试。'
      return
    }
    const safeStem = imageName.value.replace(/\.[^.]+$/, '').replace(/[^\p{L}\p{N}._-]+/gu, '-').slice(0, 70) || 'video-cover'
    emit('save', new File([blob], `${safeStem}-${ratioLabel.value.replace(':', 'x')}.jpg`, { type: 'image/jpeg' }))
  }, 'image/jpeg', 0.92)
}

onBeforeUnmount(() => {
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.clear()
})
</script>

<template>
  <ElDialog :model-value="open" class="cover-editor-dialog" :width="'min(780px, calc(100% - 48px))'" :show-close="false" :close-on-click-modal="true" :close-on-press-escape="true" :append-to-body="true" :aria-label="title" @close="close">
    <template #header>
      <header class="cover-editor-header">
        <div><strong>{{ title }}</strong><small>封面比例 {{ ratioLabel }}</small></div>
        <ElButton text class="cover-editor-close" native-type="button" aria-label="关闭" @click="close">×</ElButton>
      </header>
    </template>

    <div v-if="stage === 'source'" class="cover-editor-source">
      <ElTabs :model-value="sourceTab" class="cover-editor-tabs" aria-label="封面来源" @tab-change="handleSourceTabChange">
        <ElTabPane name="image" label="上传图片" />
        <ElTabPane name="video" label="从视频截帧" :disabled="!videoSrc" />
      </ElTabs>
      <div v-if="sourceTab === 'image'" class="cover-editor-image-source">
        <p>选择图片后可拖动、缩放裁剪框，裁剪结果将保存为 {{ ratioLabel }} 封面。</p>
        <ElUpload ref="imagePicker" :auto-upload="false" :show-file-list="false" :multiple="false" :accept="'.jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp'" :disabled="disabled" :on-change="onImageUploadChange">
          <ElButton class="button button-primary" native-type="button" :disabled="disabled">选择封面图片</ElButton>
        </ElUpload>
      </div>
      <div v-else class="cover-editor-video-source">
        <video ref="video" :src="videoSrc" controls playsinline preload="metadata" aria-label="选择视频封面帧" />
        <p>拖动进度条定位画面，暂停后截取当前帧，再调整 {{ ratioLabel }} 裁剪区域。</p>
        <ElButton class="button button-primary" native-type="button" :disabled="disabled" @click="captureFrame">截取当前画面</ElButton>
      </div>
    </div>

    <div v-else class="cover-editor-crop-stage">
      <div class="cover-editor-crop-toolbar">
        <span>{{ imageName }} · 拖动或缩放图片调整取景</span>
        <ElButton text class="text-button" native-type="button" :disabled="busy" @click="goBack">重新选择素材</ElButton>
      </div>
      <Cropper
        :src="imageSource"
        class="cover-editor-cropper"
        :default-size="defaultCropSize"
        :stencil-props="{ aspectRatio: aspectRatio }"
        :canvas="{ maxArea: 20000000, fillColor: '#ffffff' }"
        image-restriction="stencil"
        @change="onCropChange"
      />
    </div>

    <ElAlert v-if="error" class="cover-editor-error" type="error" :closable="false" :title="error" />
    <template #footer>
      <footer class="cover-editor-footer">
        <ElButton class="button button-secondary" native-type="button" :disabled="busy" @click="close">取消</ElButton>
        <ElButton v-if="stage === 'crop'" class="button button-primary" native-type="button" :disabled="disabled || busy || !cropReady" @click="save">{{ busy ? '处理中…' : '使用此封面' }}</ElButton>
      </footer>
    </template>
  </ElDialog>
</template>

<style scoped>
.cover-editor-dialog { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(300px, 1fr) auto; max-height: min(92vh, 820px); overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--paper, #fff); color: var(--ink, #1f2329); box-shadow: 0 24px 70px rgb(10 19 25 / 20%); }
.cover-editor-header { display: flex; align-items: center; justify-content: space-between; padding: 15px 18px; border-bottom: 1px solid var(--line); }
.cover-editor-header > div { display: grid; gap: 4px; }
.cover-editor-header strong { color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.cover-editor-header small, .cover-editor-video-source p, .cover-editor-image-source p, .cover-editor-crop-toolbar span { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.cover-editor-close { width: 32px; height: 32px; border: 0; border-radius: 8px; background: transparent; color: var(--muted, #646a73); font-size: 26px; }
.cover-editor-close:hover { background: var(--paper-2, #f8f9fb); color: var(--ink, #1f2329); }
.cover-editor-source { display: grid; grid-template-rows: auto 1fr; min-height: 0; padding: 18px; }
.cover-editor-tabs { margin-bottom: 15px; }
.cover-editor-tabs :deep(.el-tabs__header) { margin: 0; border: 0; }
.cover-editor-tabs :deep(.el-tabs__nav-wrap::after), .cover-editor-tabs :deep(.el-tabs__active-bar), .cover-editor-tabs :deep(.el-tabs__content) { display: none; }
.cover-editor-tabs :deep(.el-tabs__item) { height: 34px; padding: 0 12px; border: 1px solid var(--line); border-radius: 6px; color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: 32px; }
.cover-editor-tabs :deep(.el-tabs__item.is-active) { border-color: var(--pine); background: var(--pine-soft); color: var(--pine); }
.cover-editor-image-source, .cover-editor-video-source { display: grid; align-content: center; justify-items: center; gap: 14px; min-height: 300px; padding: 15px; border: 1px dashed var(--line); border-radius: 12px; background: var(--paper-2, #f8f9fb); text-align: center; }
.cover-editor-video-source video { width: min(100%, 560px); max-height: 380px; border-radius: 8px; background: #111; }
.cover-editor-image-source p, .cover-editor-video-source p { max-width: 460px; margin: 0; line-height: 1.6; }
.cover-editor-crop-stage { display: grid; grid-template-rows: auto minmax(280px, 1fr); gap: 10px; min-width: 0; min-height: 360px; padding: 14px 18px; }
.cover-editor-crop-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.cover-editor-crop-toolbar span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cover-editor-cropper { width: 100%; min-width: 0; height: min(57vh, 500px); background: #1d252b; }
.cover-editor-error { margin: 0; padding: 0 18px 10px; }
.cover-editor-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 13px 18px; border-top: 1px solid var(--line); }
.cover-editor-dialog :deep(.el-dialog__header) { margin: 0; padding: 0; }
.cover-editor-dialog :deep(.el-dialog__body) { display: grid; min-height: 0; overflow: auto; padding: 0; }
.cover-editor-dialog :deep(.el-dialog__footer) { padding: 0; }
.cover-editor-dialog :deep(.el-alert.cover-editor-error) { border: 0; background: transparent; }
.cover-editor-dialog :deep(.el-alert__title) { color: var(--danger); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.cover-editor-dialog :deep(.el-button.button) { min-height: 36px; border-color: var(--pine); background: var(--pine); color: #fff; font-size: var(--fs-body-sm); box-shadow: none; }
.cover-editor-dialog :deep(.el-button.button-secondary) { border-color: var(--line); background: var(--paper); color: var(--text-secondary); }
.cover-editor-dialog :deep(.el-button.text-button), .cover-editor-dialog :deep(.el-button.cover-editor-close) { min-height: 32px; border: 0; background: transparent; color: var(--pine); box-shadow: none; }
.cover-editor-image-source :deep(.el-upload) { display: block; }
@media (max-width: 600px) { .cover-editor-dialog { max-height: 96vh; } .cover-editor-source, .cover-editor-crop-stage { padding: 12px; } .cover-editor-crop-toolbar { align-items: flex-start; } .cover-editor-cropper { height: 48vh; } }
</style>
