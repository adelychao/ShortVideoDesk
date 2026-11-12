<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ElButton, ElCheckbox, ElDialog, ElOption, ElSelect } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/select/style/css'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import type { LocalAsrStatus, ModelCapabilityStatus, ModelProfile, ScriptStyleTemplate, SourcePreview, StepConsent, StepKind, TaskSummary, UploadReceipt, VideoPublishAccount } from '../api/types'
import LocalMediaUpload from '../components/LocalMediaUpload.vue'
import StepSelector from '../components/StepSelector.vue'
import { useConfigStore } from '../stores/config'
import { useTasksStore } from '../stores/tasks'
import { localModelAdvice, localModelLabel } from '../utils/localWhisperModels'

const configStore = useConfigStore()
const taskStore = useTasksStore()
const router = useRouter()
const configState = configStore.state
const taskState = taskStore.state

const sourceText = ref('')
const sourceMode = ref<'url' | 'file'>('url')
const sourcePreview = ref<SourcePreview | null>(null)
const platformAccessMode = ref<'anonymous' | 'account'>('anonymous')
const platformAccounts = ref<VideoPublishAccount[]>([])
const selectedPlatformAccountId = ref('')
const platformAccountsLoading = ref(false)
const platformAccountsError = ref('')
let platformAccountsRequest = 0
const previewing = ref(false)
const previewError = ref('')
const taskError = ref('')
const submitReminder = ref('')
const taskSuccess = ref<TaskSummary | null>(null)
const selectedSteps = ref<StepKind[]>(['download'])
const scriptStyleIds = ref<string[]>([])
const scriptStyleTemplates = ref<ScriptStyleTemplate[]>([])
const scriptStyleTemplatesLoading = ref(false)
const scriptStyleTemplatesError = ref('')
const commentSortOrder = ref<'likes_desc' | 'time_desc' | 'time_asc'>('likes_desc')
const allowFirstFrame = ref(true)
const transcriptionModePreferenceKey = 'local-video-workbench.transcription-mode'
function loadTranscriptionModePreference(): 'cloud' | 'local' {
  try {
    const stored = window.localStorage.getItem(transcriptionModePreferenceKey)
    return stored === 'local' || stored === 'cloud' ? stored : 'local'
  } catch {
    return 'local'
  }
}
const transcriptionMode = ref<'cloud' | 'local'>(loadTranscriptionModePreference())
const ocrMode = ref<'cloud' | 'local'>('local')
const ocrInterval = ref(2)
const ocrScanProfile = ref<'full' | 'subtitles'>('full')
const localOcrStatus = ref<{ installed: boolean; ready: boolean; model: string; device: string; workers?: number } | null>(null)
const localOcrError = ref('')
async function refreshOcrStatus(): Promise<void> {
  try {
    localOcrStatus.value = await api.getLocalOcrStatus()
    localOcrError.value = ''
  } catch (error) {
    localOcrStatus.value = null
    localOcrError.value = errorMessage(error)
  }
}
const localModel = ref('small')
const localLanguage = ref<'auto' | 'zh' | 'en'>('auto')
const localStatus = ref<LocalAsrStatus | null>(null)
const localStatusError = ref('')
const localStatusLoading = ref(false)
let localStatusRequest = 0
async function refreshLocalStatus(): Promise<void> {
  const request = ++localStatusRequest
  localStatusLoading.value = true
  localStatusError.value = ''
  try {
    const status = await api.getLocalAsrStatus()
    if (request === localStatusRequest) localStatus.value = status
  } catch (error) {
    if (request === localStatusRequest) {
      localStatus.value = null
      localStatusError.value = errorMessage(error)
    }
  } finally {
    if (request === localStatusRequest) localStatusLoading.value = false
  }
}
const uploadReceipt = ref<UploadReceipt | null>(null)
const uploadBusy = ref(false)
const uploadResetToken = ref(0)
const settingsDialogVisible = ref(false)
const activeSettings = ref<'download' | 'comments' | 'ocr' | 'transcribe'>('download')
const textModelCapability = ref<ModelCapabilityStatus | null>(null)
const modelProfiles = ref<ModelProfile[]>([])
const selectedModelProfileId = ref('')
const modelProfilesLoading = ref(false)
const modelProfilesError = ref('')
const manuallySelectedModelProfileId = ref('')
let modelCapabilityRequest = 0

function openStepSettings(kind: 'download' | 'comments' | 'ocr' | 'transcribe'): void {
  activeSettings.value = kind
  if (kind === 'transcribe') void refreshLocalStatus()
  if (kind === 'ocr') void refreshOcrStatus()
  settingsDialogVisible.value = true
}

// el-select 的 update 事件负载类型较宽，经窄化校验后再写入各联合类型状态。
function setTranscriptionMode(value: unknown): void {
  if (value === 'local' || value === 'cloud') transcriptionMode.value = value
}
function setLocalModel(value: unknown): void {
  if (typeof value === 'string') localModel.value = value
}
function setLocalLanguage(value: unknown): void {
  if (value === 'auto' || value === 'zh' || value === 'en') localLanguage.value = value
}
function setCloudModelProfile(value: unknown): void {
  if (typeof value === 'string') {
    selectedModelProfileId.value = value
    rememberModelProfileSelection()
  }
}
function setCommentSortOrder(value: unknown): void {
  if (value === 'likes_desc' || value === 'time_desc' || value === 'time_asc') commentSortOrder.value = value
}
function setOcrMode(value: unknown): void {
  if (value === 'local' || value === 'cloud') ocrMode.value = value
}
function setOcrScanProfile(value: unknown): void {
  if (value === 'full' || value === 'subtitles') ocrScanProfile.value = value
}
function setOcrInterval(value: unknown): void {
  if (typeof value === 'number') ocrInterval.value = value
}

const archiveRootConfigured = computed(() => configState.health?.archive_root_configured ?? null)
const selectedCloudSteps = computed(() => selectedSteps.value.filter((step) => [
  'transcribe', 'analyze_comments', 'analyze_structure', 'knowledge_cards', 'generate_scripts',
].includes(step) && !(step === 'transcribe' && transcriptionMode.value === 'local')))
const selectedOcr = computed(() => selectedSteps.value.includes('ocr'))
const selectedScriptStyles = computed(() => scriptStyleTemplates.value.filter((item) => item.enabled && scriptStyleIds.value.includes(item.id)))
const selectedScriptReferencePaths = computed(() => [...new Set(selectedScriptStyles.value.flatMap((item) => item.reference_paths))])
const scriptStyleSelectionInvalid = computed(() => selectedSteps.value.includes('generate_scripts') && selectedScriptStyles.value.length === 0)
const selectedModelProfile = computed(() => modelProfiles.value.find((profile) => profile.id === selectedModelProfileId.value) ?? null)
const requiresCloudModel = computed(() => selectedCloudSteps.value.length > 0 || (selectedOcr.value && ocrMode.value === 'cloud'))
const modelSupportsText = computed(() => textModelCapability.value?.status !== 'verified' || textModelCapability.value.modalities.includes('text'))
const modelSupportsImage = computed(() => textModelCapability.value?.status !== 'verified' || textModelCapability.value.modalities.includes('image'))
const profileCredentialConfigured = computed(() => Boolean(selectedModelProfile.value?.credential.configured))
const textModelConfigured = computed(() => Boolean(selectedModelProfile.value?.model_id && profileCredentialConfigured.value && modelSupportsText.value))
const imageModelConfigured = computed(() => Boolean(selectedModelProfile.value?.model_id && profileCredentialConfigured.value && modelSupportsImage.value))
const ocrConfigured = computed(() => ocrMode.value === 'local' ? Boolean(localOcrStatus.value?.ready) : imageModelConfigured.value)
const ocrSummary = computed(() => ocrMode.value === 'local' ? `本地 RapidOCR · ${localOcrStatus.value?.ready ? '已准备' : '未准备'}` : `云端图像模型 · ${selectedModelProfile.value?.model_id || '未配置'}`)
const asrModelConfigured = computed(() => Boolean(selectedModelProfile.value?.supports_audio_transcription && selectedModelProfile.value.asr_model_id && profileCredentialConfigured.value))
const asrCapableProfiles = computed(() => modelProfiles.value.filter((profile) => profile.supports_audio_transcription && profile.asr_model_id))
const localModelPlaceholder = computed(() => localStatusLoading.value ? '正在读取模型…' : localStatusError.value ? '无法读取模型' : !localStatus.value?.models.length ? '暂无可用模型' : '选择模型')
const cloudProfilePlaceholder = computed(() => modelProfilesLoading.value && !modelProfiles.value.length ? '正在读取配置档…' : !asrCapableProfiles.value.length ? '设置中暂无支持语音转写的云端模型' : asrCapableProfiles.value.some((profile) => profile.id === selectedModelProfileId.value) ? '选择云端模型' : '当前配置档不支持语音转写，请选择')
const settingsDialogTitle = computed(() => activeSettings.value === 'download' ? '下载视频与封面设置' : activeSettings.value === 'comments' ? '采集公开评论设置' : activeSettings.value === 'transcribe' ? '语音转写设置' : '字幕 OCR 与校对设置')
const localModelReady = computed(() => Boolean(localStatus.value?.installed && localStatus.value.models.some((item) => item.id === localModel.value && item.ready)))
const transcriptionConfigured = computed(() => transcriptionMode.value === 'local' ? localModelReady.value : asrModelConfigured.value)
const providerName = computed(() => selectedModelProfile.value?.provider_name || '所选服务商')
const transcriptionSummary = computed(() => transcriptionMode.value === 'local' ? `本机转写 · ${localModelLabel(localModel.value)}${localModelReady.value ? '' : '（未准备）'}` : `云端转写 · ${providerName.value}`)
const cloudModelConfigured = computed(() => selectedCloudSteps.value.every((step) => (
  step === 'transcribe' ? asrModelConfigured.value : textModelConfigured.value
)))
const platformAccountsForSource = computed(() => platformAccounts.value.filter((item) => (
  item.platform_id === sourcePreview.value?.platform && item.enabled
)))
const connectedPlatformAccounts = computed(() => platformAccountsForSource.value.filter((item) => item.status === 'connected'))
const selectedPlatformAccount = computed(() => platformAccountsForSource.value.find((item) => item.id === selectedPlatformAccountId.value) ?? null)
const accountAccessAvailable = computed(() => Boolean(sourcePreview.value && connectedPlatformAccounts.value.length))
const accountSelectionValid = computed(() => Boolean(selectedPlatformAccount.value?.status === 'connected'))
const cloudModelName = computed(() => {
  const models = selectedCloudSteps.value.map((step) => step === 'transcribe'
    ? `语音转写：${selectedModelProfile.value?.asr_model_id || '未配置'}`
    : `文本/图像：${selectedModelProfile.value?.model_id || '未配置'}`)
  if (selectedOcr.value && ocrMode.value === 'cloud') models.push(`OCR：${selectedModelProfile.value?.model_id || '未配置'}`)
  return [...new Set(models)].join('；') || '未配置'
})

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '本机工作台请求失败，请稍后重试。'
}

async function loadScriptStyleTemplates(): Promise<void> {
  if (scriptStyleTemplatesLoading.value) return
  scriptStyleTemplatesLoading.value = true
  scriptStyleTemplatesError.value = ''
  try {
    const result = await api.getConfig()
    scriptStyleTemplates.value = result.settings.script_style_templates
    const enabledIds = new Set(scriptStyleTemplates.value.filter((item) => item.enabled).map((item) => item.id))
    scriptStyleIds.value = scriptStyleIds.value.filter((id) => enabledIds.has(id))
  } catch (error) {
    scriptStyleTemplates.value = []
    scriptStyleTemplatesError.value = errorMessage(error)
  } finally {
    scriptStyleTemplatesLoading.value = false
  }
}

function createConsents(): StepConsent[] {
  const profile = selectedModelProfile.value
  return selectedCloudSteps.value.map((step) => ({
    stepKind: step,
    providerId: profile?.provider_id || '',
    modelId: step === 'transcribe' ? (profile?.asr_model_id || '') : (profile?.model_id || ''),
    artifactIds: [],
    authorized: true,
    authorizedAt: new Date().toISOString(),
  }))
}

async function loadModelProfiles(): Promise<void> {
  modelProfilesLoading.value = true
  modelProfilesError.value = ''
  try {
    const result = await api.getModelProfiles()
    modelProfiles.value = result.items
    const manualSelection = result.items.find((profile) => profile.id === manuallySelectedModelProfileId.value)
    if (manuallySelectedModelProfileId.value && !manualSelection) manuallySelectedModelProfileId.value = ''
    selectedModelProfileId.value = manualSelection?.id
      ?? result.items.find((profile) => profile.is_default)?.id
      ?? result.items[0]?.id
      ?? ''
  } catch (error) {
    modelProfiles.value = []
    selectedModelProfileId.value = ''
    modelProfilesError.value = errorMessage(error)
  } finally {
    modelProfilesLoading.value = false
  }
}

function rememberModelProfileSelection(): void {
  manuallySelectedModelProfileId.value = selectedModelProfileId.value
}

watch(transcriptionMode, (mode) => {
  try {
    window.localStorage.setItem(transcriptionModePreferenceKey, mode)
  } catch {
    return
  }
})

watch(() => [selectedModelProfile.value?.provider_id, selectedModelProfile.value?.model_id] as const, ([providerId, modelId], _previous, onCleanup) => {
  const request = ++modelCapabilityRequest
  textModelCapability.value = null
  if (!providerId || !modelId) return
  const timer = window.setTimeout(() => {
    void api.getModelCapability(providerId, modelId).then((capability) => {
      if (request === modelCapabilityRequest && providerId === selectedModelProfile.value?.provider_id && modelId === selectedModelProfile.value?.model_id) {
        textModelCapability.value = capability
      }
    }).catch(() => {
      if (request === modelCapabilityRequest) textModelCapability.value = null
    })
  }, 200)
  onCleanup(() => window.clearTimeout(timer))
})

watch(uploadReceipt, (receipt) => {
  if (receipt) {
    taskError.value = ''
    submitReminder.value = ''
  }
  if (receipt?.detectedMediaType === 'audio' && selectedSteps.value.includes('ocr')) {
    selectedSteps.value = selectedSteps.value.filter((step) => step !== 'ocr')
    taskError.value = '该文件只有音轨，已移除字幕 OCR 步骤。'
  }
})

function clearSourceResult(): void {
  sourcePreview.value = null
  platformAccessMode.value = 'anonymous'
  platformAccounts.value = []
  selectedPlatformAccountId.value = ''
  platformAccountsError.value = ''
  previewError.value = ''
  taskError.value = ''
  submitReminder.value = ''
  taskSuccess.value = null
}

async function refreshPlatformAccounts(): Promise<void> {
  const request = ++platformAccountsRequest
  platformAccountsLoading.value = true
  platformAccountsError.value = ''
  try {
    const result = await api.getVideoPublishAccounts()
    if (request === platformAccountsRequest) {
      platformAccounts.value = result.items
      const currentAccounts = result.items.filter((item) => item.platform_id === sourcePreview.value?.platform && item.enabled)
      platformAccessMode.value = currentAccounts.length ? 'account' : 'anonymous'
      if (!currentAccounts.some((item) => item.id === selectedPlatformAccountId.value)) {
        const fallback = currentAccounts.find((item) => item.status === 'connected') ?? currentAccounts[0]
        selectedPlatformAccountId.value = fallback?.id ?? ''
      }
    }
  } catch (error) {
    if (request === platformAccountsRequest) platformAccountsError.value = errorMessage(error)
  } finally {
    if (request === platformAccountsRequest) platformAccountsLoading.value = false
  }
}

async function runPreview(): Promise<boolean> {
  previewError.value = ''
  taskError.value = ''
  taskSuccess.value = null
  sourcePreview.value = null
  if (!sourceText.value.trim()) {
    previewError.value = '请先粘贴抖音或 B 站的公开作品链接或分享文案。'
    return false
  }

  previewing.value = true
  try {
    sourcePreview.value = await api.previewSource(sourceText.value)
    return true
  } catch (error) {
    previewError.value = errorMessage(error)
    return false
  } finally {
    previewing.value = false
  }
}

watch(sourcePreview, (preview) => {
  selectedPlatformAccountId.value = ''
  if (preview) void refreshPlatformAccounts()
})

function videoAccountName(account: VideoPublishAccount): string {
  return account.profile_name || account.display_name || account.account_alias || '未命名账号'
}

function videoAccountStatusLabel(account: VideoPublishAccount): string {
  return account.status === 'connected' ? '已连接'
    : account.status === 'login_in_progress' ? '等待登录'
      : account.status === 'checking' ? '正在检查'
        : account.status === 'expired' ? '登录失效'
          : account.status === 'error' ? '暂不可用' : '未登录'
}

async function submitTask(): Promise<void> {
  if (taskState.creating || previewing.value || uploadBusy.value) return
  taskError.value = ''
  submitReminder.value = ''
  taskSuccess.value = null

  if (selectedSteps.value.includes('generate_scripts') && scriptStyleSelectionInvalid.value) {
    taskError.value = '请至少选择一个已启用的口播稿风格模板。'
    submitReminder.value = taskError.value
    document.querySelector<HTMLElement>('.script-style-picker')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    return
  }

  if (requiresCloudModel.value) {
    if (modelProfilesLoading.value) {
      taskError.value = '正在读取模型配置，请稍后再创建任务。'
    } else if (!selectedModelProfile.value) {
      taskError.value = '请先在“知识库与设置 → 模型与转写”中保存模型配置。'
    } else if (!profileCredentialConfigured.value) {
      taskError.value = '所选模型配置尚未保存 API Key，请先在设置中补全。'
    } else if (!cloudModelConfigured.value || (selectedOcr.value && ocrMode.value === 'cloud' && !imageModelConfigured.value)) {
      taskError.value = '所选配置缺少当前云端步骤需要的模型 ID，请到模型与转写设置中补全。'
    }
    if (taskError.value) {
      submitReminder.value = taskError.value
      return
    }
  }

  if (sourceMode.value === 'file') {
    if (!uploadReceipt.value) {
      taskError.value = '请先选择一个本地媒体文件。'
      submitReminder.value = taskError.value
      document.querySelector<HTMLElement>('.local-upload-zone')?.focus()
      return
    }
    if (selectedSteps.value[0] !== 'import' || selectedSteps.value.some((step) => !['import', 'transcribe', 'ocr', 'analyze_structure', 'knowledge_cards', 'generate_scripts'].includes(step))) {
      taskError.value = '本地媒体处理需要先导入；文本分析需先选择转写。'
      submitReminder.value = taskError.value
      return
    }
    try {
      const createdTask = await taskStore.createTask({
        // 本地选择的原文件走登记路径（不复制）；旧上传暂存凭据仍走 uploadId。
        ...(uploadReceipt.value.sourceType === 'local'
          ? { localSourceId: uploadReceipt.value.uploadId }
          : { uploadId: uploadReceipt.value.uploadId }),
        steps: selectedSteps.value,
        scriptStyleIds: selectedSteps.value.includes('generate_scripts') ? scriptStyleIds.value : [],
        allowFirstFrame: false,
        consents: createConsents(),
        transcriptionMode: transcriptionMode.value,
        ocrMode: ocrMode.value,
        ocrIntervalSeconds: ocrInterval.value,
        ocrScanProfile: ocrScanProfile.value,
        localModel: localModel.value,
        localLanguage: localLanguage.value,
        localDevice: configState.health?.local_asr_device ?? 'auto',
        platformAccessMode: 'anonymous',
        modelProfileId: requiresCloudModel.value ? selectedModelProfileId.value : undefined,
      })
      taskSuccess.value = createdTask
      uploadReceipt.value = null
      uploadResetToken.value += 1
      void router.push({ name: 'task-detail', params: { taskId: createdTask.task.id } })
    } catch (error) {
      taskError.value = errorMessage(error)
      submitReminder.value = taskError.value
    }
    return
  }

  if (!sourceText.value.trim()) {
    previewError.value = '请先填写视频链接或分享文案。'
    submitReminder.value = previewError.value
    document.getElementById('source-text')?.focus()
    return
  }

  if (!selectedSteps.value.includes('download') || selectedSteps.value.some((step) => !['download', 'comments', 'analyze_comments', 'transcribe', 'ocr', 'analyze_structure', 'knowledge_cards', 'generate_scripts'].includes(step))) {
    taskError.value = '公开视频任务需要先下载；请检查所选步骤的前置依赖。'
    submitReminder.value = taskError.value
    return
  }
  if (!sourcePreview.value && !(await runPreview())) {
    submitReminder.value = previewError.value || '请检查视频链接后再创建任务。'
    return
  }
  if (platformAccessMode.value === 'account' && !accountSelectionValid.value) {
    taskError.value = platformAccountsForSource.value.length > 1
      ? '请选择本次任务使用的已连接账号。'
      : '所选账号暂不可用，请先在账号管理中完成登录并检查状态。'
    submitReminder.value = taskError.value
    return
  }

  try {
    const createdTask = await taskStore.createTask({
      sourceUrl: sourceText.value,
      steps: selectedSteps.value,
      scriptStyleIds: selectedSteps.value.includes('generate_scripts') ? scriptStyleIds.value : [],
      allowFirstFrame: allowFirstFrame.value,
      commentSortOrder: commentSortOrder.value,
      consents: createConsents(),
      transcriptionMode: transcriptionMode.value,
      ocrMode: ocrMode.value,
      ocrIntervalSeconds: ocrInterval.value,
      ocrScanProfile: ocrScanProfile.value,
      localModel: localModel.value,
      localLanguage: localLanguage.value,
      localDevice: configState.health?.local_asr_device ?? 'auto',
      platformAccessMode: platformAccessMode.value,
      platformAccountId: platformAccessMode.value === 'account' ? selectedPlatformAccountId.value : undefined,
      modelProfileId: requiresCloudModel.value ? selectedModelProfileId.value : undefined,
    })
    taskSuccess.value = createdTask
    void router.push({ name: 'task-detail', params: { taskId: createdTask.task.id } })
  } catch (error) {
    taskError.value = errorMessage(error)
    submitReminder.value = taskError.value
  }
}

function switchSourceMode(mode: 'url' | 'file'): void {
  sourceMode.value = mode
  selectedSteps.value = mode === 'file' ? ['import'] : ['download']
  scriptStyleIds.value = []
  taskError.value = ''
  submitReminder.value = ''
  taskSuccess.value = null
  if (mode === 'url') clearSourceResult()
}

onMounted(() => {
  void configStore.loadOverview()
  void refreshLocalStatus()
  void refreshOcrStatus()
  void loadScriptStyleTemplates()
})

onActivated(() => {
  void loadModelProfiles()
  void loadScriptStyleTemplates()
})
</script>

<template>
  <div>
    <form class="task-layout" :aria-busy="taskState.creating || uploadBusy" @submit.prevent="submitTask">
      <div class="source-column">
      <div class="source-mode-switch" role="tablist" aria-label="素材来源">
        <button class="tab-button source-tab-button" type="button" role="tab" :aria-selected="sourceMode === 'url'" @click="switchSourceMode('url')">
          <svg class="icon source-tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="m10 13.5-1.4 1.4a3 3 0 0 1-4.2-4.2l3.3-3.3a3 3 0 0 1 4.2 0" />
            <path d="m14 10.5 1.4-1.4a3 3 0 0 1 4.2 4.2l-3.3 3.3a3 3 0 0 1-4.2 0" />
            <path d="m8.5 15.5 7-7" />
          </svg>
          <span class="source-tab-copy">
            <span class="source-tab-label">公开视频链接</span>
            <span class="source-tab-description">粘贴抖音 / B 站分享链接</span>
          </span>
        </button>
        <button class="tab-button source-tab-button source-tab-button--file" type="button" role="tab" :aria-selected="sourceMode === 'file'" @click="switchSourceMode('file')">
          <svg class="icon source-tab-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M3 7.5h6l2 2H21v9.5H3z" />
            <path d="M12 16v-5m-2 2 2-2 2 2" />
          </svg>
          <span class="source-tab-copy">
            <span class="source-tab-label">本地文件</span>
            <span class="source-tab-description">选择视频或图片素材</span>
          </span>
          <svg class="source-tab-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M5 12h14m-6-6 6 6-6 6" />
          </svg>
        </button>
      </div>
      <section class="panel panel-pad" aria-labelledby="workbench-source-title">
        <div class="panel-head">
          <div class="panel-title-wrap">
            <span class="step-number" aria-hidden="true">01</span>
            <div>
              <h2 id="workbench-source-title">{{ sourceMode === 'url' ? '添加一个公开视频' : '导入已有本地媒体' }}</h2>
              <p>{{ sourceMode === 'url' ? '抖音：打开作品页，点击右侧“分享”→面板底部“复制链接”；B 站：在视频页点击“分享”→“复制链接”。复制后粘贴到下方，也可直接粘贴包含链接的分享文案。' : '直接使用所选的原文件，不会复制或移动；创建任务时校验后的副本写入归档目录。' }}</p>
            </div>
          </div>
          <span class="quiet-pill">单条任务</span>
        </div>

          <div v-show="sourceMode === 'url'">
          <label class="form-label" for="source-text"><span class="required-marker" aria-hidden="true">*</span>视频链接或分享文案</label>
          <div class="field-row">
            <textarea
              id="source-text"
              v-model="sourceText"
              class="textarea"
              name="source"
              rows="3"
              maxlength="4096"
              :disabled="previewing"
              aria-required="true"
              :aria-describedby="previewError ? 'source-error' : undefined"
              :aria-invalid="Boolean(previewError)"
              placeholder="粘贴抖音作品页“分享→复制链接”或 B 站视频页“分享→复制链接”得到的内容，也可粘贴分享文案…"
              @input="clearSourceResult"
            ></textarea>
            <button class="button" type="button" :disabled="previewing" @click="runPreview">
              {{ previewing ? '正在识别…' : '识别链接' }}
            </button>
          </div>

          <div v-if="sourcePreview" class="url-preview is-visible" role="status" aria-live="polite">
            <div class="preview-art" aria-hidden="true"></div>
            <div class="preview-copy">
              <strong>{{ sourcePreview.platform_name }}作品链接已识别</strong>
              <span v-if="sourcePreview.source_id">作品 ID：{{ sourcePreview.source_id }}</span>
            </div>
            <span class="platform-badge"><PlatformLogo class="source-platform-logo" :platform-id="sourcePreview.platform" :name="sourcePreview.platform_name" />{{ sourcePreview.platform_name }}</span>
          </div>
          <div v-if="previewError" id="source-error" class="info-box form-error" role="alert">{{ previewError }}</div>
          </div>

          <div v-show="sourceMode === 'file'" class="local-import-panel">
            <label class="form-label" for="local-media-picker"><span class="required-marker" aria-hidden="true">*</span>本地媒体文件</label>
            <LocalMediaUpload
              :disabled="taskState.creating"
              :reset-token="uploadResetToken"
              @uploaded="uploadReceipt = $event"
              @busy-change="uploadBusy = $event"
            />
          </div>

          <div v-if="sourceMode === 'url'" class="access-section">
            <div class="panel-title-wrap"><span class="step-number" aria-hidden="true">＋</span><h2>访问方式</h2></div>
            <div class="access-options" role="group" aria-label="访问方式">
              <label class="access-choice" :class="{ 'is-selected': platformAccessMode === 'anonymous' }"><input v-model="platformAccessMode" type="radio" name="access-mode" value="anonymous" /> 匿名访问</label>
              <label class="access-choice" :class="{ 'is-selected': platformAccessMode === 'account', 'is-disabled': !accountAccessAvailable }"><input v-model="platformAccessMode" type="radio" name="access-mode" value="account" :disabled="!accountAccessAvailable" /> 使用已登录账号 <small>{{ platformAccountsLoading ? '读取状态…' : accountAccessAvailable ? `${connectedPlatformAccounts.length} 个可用账号` : '未登录' }}</small></label>
            </div>
            <p v-if="platformAccountsError" class="helper form-error-text" role="alert">{{ platformAccountsError }}</p>
            <p v-else-if="!sourcePreview" class="helper">先识别作品链接，再检查对应平台的登录状态。</p>
            <p v-else-if="!platformAccountsForSource.length" class="helper">该平台尚未添加账号。<RouterLink class="text-button" :to="{ name: 'accounts', query: { tab: 'video', platform: sourcePreview.platform } }">前往视频发布账号管理 →</RouterLink></p>
            <p v-else-if="!accountAccessAvailable" class="helper">该平台账号当前未连接，请先在视频发布账号管理中完成登录。<RouterLink class="text-button" :to="{ name: 'accounts', query: { tab: 'video', platform: sourcePreview.platform } }">前往账号管理 →</RouterLink></p>
            <template v-else-if="platformAccessMode === 'account'">
              <span id="collection-account-picker-label" class="form-label platform-account-picker-label">本次使用账号</span>
              <div class="account-card-list" role="radiogroup" aria-labelledby="collection-account-picker-label">
                <label v-for="account in platformAccountsForSource" :key="account.id" class="account-card" :class="{ 'is-selected': selectedPlatformAccountId === account.id, 'is-unavailable': account.status !== 'connected' }">
                  <input v-model="selectedPlatformAccountId" type="radio" name="collection-account" :value="account.id" :disabled="account.status !== 'connected'" />
                  <img v-if="account.avatar_url" class="account-card-avatar" :src="account.avatar_url" alt="" referrerpolicy="no-referrer" />
                  <span v-else class="account-card-avatar account-card-avatar-fallback" aria-hidden="true">{{ videoAccountName(account).slice(0, 1) }}</span>
                  <span class="account-card-info">
                    <strong class="account-card-name">{{ videoAccountName(account) }}</strong>
                    <small class="account-card-status" :class="{ 'is-ok': account.status === 'connected' }">{{ videoAccountStatusLabel(account) }}</small>
                  </span>
                </label>
              </div>
              <p v-if="selectedPlatformAccount?.status === 'connected'" class="helper">将使用「{{ videoAccountName(selectedPlatformAccount) }}」下载素材和采集评论；视频发布与采集共用此登录状态。</p>
              <p v-else-if="selectedPlatformAccount" class="helper">账号「{{ videoAccountName(selectedPlatformAccount) }}」当前{{ videoAccountStatusLabel(selectedPlatformAccount) }}，请先完成登录或重新检查。</p>
              <p v-else class="helper">请选择本次任务使用的账号。</p>
            </template>
            <p v-else class="helper">该平台已有 {{ platformAccountsForSource.length }} 个账号。选择账号访问后，可指定本次任务使用的账号。</p>
            <p v-if="platformAccessMode === 'account'" class="helper">如果账号登录状态失效，任务会提示重新登录，不会自动改用匿名访问。</p>
          </div>
          <details class="status-details">
            <summary>查看本地运行状态</summary>
            <p>归档目录：{{ archiveRootConfigured === null ? '状态未知' : archiveRootConfigured ? '已设置' : '未设置' }} · 本机任务队列：{{ configState.health?.worker_count ?? '—' }}</p>
            <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'checks' } }">查看运行检查与平台实测 →</RouterLink>
          </details>
      </section>
      </div>
      <section class="panel panel-pad steps-panel" aria-labelledby="workbench-steps-title">
          <div class="panel-head">
            <div class="panel-title-wrap">
              <span class="step-number" aria-hidden="true">02</span>
              <div><h2 id="workbench-steps-title">选择处理步骤</h2><p>按需勾选，自动添加前置步骤。</p></div>
            </div>
          </div>
          <StepSelector
            v-model="selectedSteps"
            v-model:script-style-ids="scriptStyleIds"
            :source-kind="sourceMode"
            :model-configured="cloudModelConfigured"
            :text-model-configured="textModelConfigured"
            :asr-model-configured="transcriptionConfigured"
            :transcription-summary="transcriptionSummary"
            :image-model-configured="ocrConfigured"
            :ocr-summary="ocrSummary"
            :media-type="uploadReceipt?.detectedMediaType ?? null"
            :script-style-templates="scriptStyleTemplates"
            :script-style-templates-loading="scriptStyleTemplatesLoading"
            :script-style-templates-error="scriptStyleTemplatesError"
            @settings="openStepSettings"
          />
          <div v-if="selectedOcr" class="info-box task-cloud-consent" role="note">
            {{ ocrMode === 'local' ? `本地 OCR 每 ${ocrInterval} 秒抽取画面，覆盖视频全时长；图片不上传。短暂出现的文字可能遗漏。` : `云端 OCR 会把抽取的图片发送给 ${providerName}，可能产生费用；目前最多抽取 8 张。` }}
          </div>

          <div v-if="archiveRootConfigured === false" class="info-box archive-warning" role="note">
            <span>尚未设置归档目录。提交任务时服务端会要求先配置本机归档位置。</span>
            <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'folders' } }">查看目录设置 →</RouterLink>
          </div>

          <div v-if="taskError" class="info-box form-error" role="alert">{{ taskError }}</div>
          <div v-if="taskSuccess" class="info-box task-success" role="status" aria-live="polite">
            <span>{{ taskSuccess.task.source.kind === 'file' ? '导入任务' : selectedSteps.includes('comments') ? '下载与评论任务' : '下载任务' }}已提交，当前状态：{{ taskSuccess.task.status }}。</span>
            <code>任务 ID：{{ taskSuccess.task.id }}</code>
          </div>

          <div class="path-preview" role="note">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M3 7h7l2 2h9v10H3zM3 7V5h7l2 2" />
            </svg>
            <span>{{ sourceMode === 'file' ? '原媒体保持不动；校验后的副本写入你选择的归档目录。' : '媒体会复制到你选择的归档目录；原分享地址不会作为任务结果返回。' }}</span>
            <RouterLink class="text-button path-setting-link" :to="{ name: 'settings', query: { tab: 'folders' } }">设置</RouterLink>
          </div>
          <section v-if="requiresCloudModel" class="task-model-profile-panel" aria-labelledby="task-model-profile-title">
            <div class="task-model-profile-choice">
              <label id="task-model-profile-title" class="form-label" for="task-model-profile-select"><span class="required-marker" aria-hidden="true">*</span>本次云端模型配置</label>
              <select id="task-model-profile-select" v-model="selectedModelProfileId" class="select-control" :disabled="modelProfilesLoading || !modelProfiles.length" aria-required="true" @change="rememberModelProfileSelection">
                <option v-if="modelProfilesLoading" value="">正在读取配置档…</option>
                <option v-else-if="!modelProfiles.length" value="">暂无已保存的配置档</option>
                <option v-for="profile in modelProfiles" :key="profile.id" :value="profile.id">{{ profile.name }} · {{ profile.provider_name }} · {{ profile.model_id }}</option>
              </select>
              <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'models' } }">管理模型配置</RouterLink>
            </div>
            <div v-if="selectedModelProfile" class="task-model-profile-summary" aria-live="polite">
              <span><small>服务商</small><strong>{{ selectedModelProfile.provider_name }}</strong></span>
              <span><small>文本与图像模型</small><strong>{{ selectedModelProfile.model_id }}</strong></span>
              <span><small>音频转写模型</small><strong>{{ selectedModelProfile.asr_model_id || '未配置' }}</strong></span>
              <span class="task-model-profile-key" :class="selectedModelProfile.credential.configured ? 'is-ready' : 'is-missing'">{{ selectedModelProfile.credential.configured ? 'API Key 已配置' : 'API Key 未配置' }}</span>
            </div>
            <p v-if="modelProfilesError" class="required-field-error" role="alert">{{ modelProfilesError }}</p>
            <p v-else-if="!selectedModelProfile && !modelProfilesLoading" class="helper">请先保存一套模型配置，任务才会调用云端模型。</p>
          </section>
          <div v-if="requiresCloudModel" class="info-box task-cloud-consent" role="note">
            点击“创建任务”时会自动授权本次所选云端步骤。对应材料将发送给 {{ providerName }}（{{ cloudModelName }}），费用无法估算。
            <span v-if="selectedScriptStyles.length">本次稿件风格：{{ selectedScriptStyles.map((item) => item.name).join('、') }}。{{ selectedScriptReferencePaths.length ? `模型还会收到这些风格参考 Markdown：${selectedScriptReferencePaths.join('、')}。` : '所选模板没有关联参考 Markdown。' }}</span>
          </div>
          <div class="form-footer">
            <span class="helper">{{ selectedCloudSteps.length ? '创建任务将按本次选择执行云端模型步骤。' : selectedOcr ? (ocrMode === 'local' ? 'OCR 在本机离线识别。' : `OCR 会自动将抽取的图片发送给${providerName}识别。`) : sourceMode === 'file' ? '导入只在本机校验原文件并写入归档目录，不会调用云端模型。' : platformAccessMode === 'account' ? '本次平台请求使用已登录账号会话；不会因此调用云端模型。' : selectedSteps.includes('comments') ? '评论仅通过平台匿名公开页面采集；当前任务不调用云端模型。' : '当前任务只在本机下载与归档，不会调用云端模型。' }}</span>
            <div class="form-submit-actions">
              <button class="button" type="submit" :disabled="taskState.creating || previewing || uploadBusy || (selectedSteps.includes('generate_scripts') && (scriptStyleTemplatesLoading || Boolean(scriptStyleTemplatesError) || scriptStyleSelectionInvalid))">
                {{ taskState.creating ? '正在提交…' : '创建任务 · 查看后续流程' }}
              </button>
              <p v-if="submitReminder" class="task-submit-reminder" role="alert">{{ submitReminder }}</p>
            </div>
          </div>
      </section>
    </form>
    <el-dialog v-model="settingsDialogVisible" class="step-dialog" :title="settingsDialogTitle" width="min(650px, calc(100% - 32px))" :close-on-click-modal="false">
      <template v-if="activeSettings === 'download'"><p>视频下载到本机归档目录；无封面时可选择生成首帧预览。</p><el-checkbox v-model="allowFirstFrame">无封面时生成首帧预览</el-checkbox></template>
      <template v-else-if="activeSettings === 'comments'"><label class="form-label comment-sort-choice">评论排序<el-select :model-value="commentSortOrder" class="select-field" @update:model-value="setCommentSortOrder"><el-option value="likes_desc" label="点赞从高到低" /><el-option value="time_desc" label="时间从新到旧" /><el-option value="time_asc" label="时间从旧到新" /></el-select></label><p>默认只获取公开主评论，不获取楼中楼回复；排序仅作用于本次实际获取的评论。</p></template>
      <template v-else-if="activeSettings === 'transcribe'">
        <section class="transcribe-settings" aria-labelledby="transcribe-settings-title">
          <h3 id="transcribe-settings-title">语音转写方式</h3>
          <div class="transcribe-fields">
            <label class="form-label">本次识别方式<el-select :model-value="transcriptionMode" class="select-field" @update:model-value="setTranscriptionMode"><el-option value="local" label="本机转写 · 免费 · 音频不上传" /><el-option value="cloud" label="云端大模型 API 调用 · 按用量计费" /></el-select></label>
            <label v-if="transcriptionMode === 'local'" class="form-label">本次模型<el-select :model-value="localModel" class="select-field" :disabled="localStatusLoading || !!localStatusError || !localStatus?.models.length" :placeholder="localModelPlaceholder" @update:model-value="setLocalModel"><el-option v-for="item in localStatus?.models || []" :key="item.id" :value="item.id" :label="`${localModelLabel(item.id)} · ${item.ready ? '已准备' : item.job?.state === 'downloading' ? '下载中' : '未准备'}`" /></el-select></label>
            <label v-else class="form-label">本次模型<el-select :model-value="selectedModelProfileId" class="select-field" :disabled="modelProfilesLoading" :placeholder="cloudProfilePlaceholder" @update:model-value="setCloudModelProfile"><el-option v-for="profile in asrCapableProfiles" :key="profile.id" :value="profile.id" :label="`${profile.name} · ${profile.provider_name} · ${profile.asr_model_id}`" /></el-select></label>
            <label v-if="transcriptionMode === 'local'" class="form-label transcribe-language">语音语言<el-select :model-value="localLanguage" class="select-field" @update:model-value="setLocalLanguage"><el-option value="auto" label="自动检测" /><el-option value="zh" label="中文" /><el-option value="en" label="英文" /></el-select></label>
          </div>
          <p v-if="transcriptionMode === 'local'">{{ localModelAdvice(localModel) }}模型通常越大，资源占用越高；效果取决于音频和设备。</p>
          <p v-if="transcriptionMode === 'local'">音频仅在本机转写；中文结果默认处理为简体，模型原文单独保留。“未准备”表示此项目尚未完成模型下载和校验。</p>
          <p v-else>所选音频片段将在授权后发送给 {{ providerName }}（语言由服务商自动识别），费用无法估算。</p>
          <p class="transcribe-status" role="status">{{ transcriptionMode === 'local' ? (localStatusLoading ? '正在读取本地模型状态…' : localStatusError || (localModelReady ? '本地模型已准备，可以选择转写。' : '模型或依赖尚未准备，请前往模型与服务设置。')) : configState.health === null ? '模型配置状态暂不可用。' : !asrCapableProfiles.length ? '设置中暂无支持语音转写的云端模型；请到「模型与服务设置」添加。' : !asrModelConfigured ? (selectedModelProfile?.credential.configured ? '当前选中的配置档不支持语音转写，请在上方「本次模型」中切换。' : '当前配置档的 API Key 未配置；请在设置中补全后重试。') : '云端转写已配置，可以选择转写并确认授权。' }}</p>
          <el-button v-if="transcriptionMode === 'local' && localStatusError" link type="primary" @click="refreshLocalStatus">重试读取模型</el-button>
          <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'models' } }" @click="settingsDialogVisible = false">模型与服务设置 →</RouterLink>
        </section>
      </template>
      <template v-else>
        <label class="form-label">识别方式<el-select :model-value="ocrMode" class="select-field" @update:model-value="setOcrMode"><el-option value="local" label="本地离线 OCR · RapidOCR" /><el-option value="cloud" :label="`云端图像模型 · ${providerName}`" /></el-select></label>
        <label v-if="ocrMode === 'local'" class="form-label">识别范围<el-select :model-value="ocrScanProfile" class="select-field" @update:model-value="setOcrScanProfile"><el-option value="full" label="全画面文字（范围更全，速度较慢）" /><el-option value="subtitles" label="下方字幕区域（更快，适合口播字幕）" /></el-select></label>
        <label v-if="ocrMode === 'local'" class="form-label">抽帧间隔<el-select :model-value="ocrInterval" class="select-field" @update:model-value="setOcrInterval"><el-option :value="0.5" label="每 0.5 秒（快字幕，识别量较多）" /><el-option :value="1" label="每 1 秒（更细致）" /><el-option :value="2" label="每 2 秒（推荐）" /><el-option :value="5" label="每 5 秒（较快）" /></el-select></label>
        <p v-if="ocrMode === 'local'">使用单个 FFmpeg 进程连续解码并按间隔抽帧；最多 2 路 OCR 并行。字幕区域模式只识别画面下方约 42%，画面中部或顶部的文字不会纳入结果。</p>
        <p v-if="ocrMode === 'local'" role="status">{{ localOcrError || (localOcrStatus?.ready ? `本地 OCR 已准备：${localOcrStatus.model}，${localOcrStatus.device === 'cuda' ? 'NVIDIA GPU' : 'CPU'}；最多 ${localOcrStatus.workers || 2} 路并行。` : '本地 OCR 依赖或模型未准备，请检查项目依赖和模型文件。') }}</p>
        <p v-else>图片自动发送给 {{ providerName }} 识别，可能产生云端费用。目前最多抽取 8 张，可能遗漏画面文字。</p>
      </template>
      <template #footer><el-button type="primary" @click="settingsDialogVisible = false">确认并关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本页的覆盖(全量落地时上移 styles.css)—— */
.panel-head h2 { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.form-label { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.quiet-pill { min-height: 24px; padding: 0 10px; color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-medium); font-variant-numeric: tabular-nums; }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.platform-badge { min-height: 24px; color: #486673; font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.source-mode-switch { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; min-width: 0; margin: 0; }
.source-tab-button { position: relative; display: grid; grid-template-columns: 20px minmax(0, 1fr); align-items: center; gap: 10px; min-width: 0; min-height: 68px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; color: var(--text-primary); background: var(--paper); box-shadow: none; text-align: left; font-size: var(--fs-body); line-height: var(--lh-tight); white-space: normal; transition: background .16s ease, border-color .16s ease, color .16s ease; }
.source-tab-button:hover { border-color: var(--pine); color: var(--text-primary); background: var(--pine-soft); }
.source-tab-button[aria-selected="true"] { border-color: var(--pine); color: var(--text-on-accent); background: var(--pine); box-shadow: none; }
.source-tab-button[aria-selected="true"]:hover { border-color: var(--pine-2); color: var(--text-on-accent); background: var(--pine-2); }
.source-tab-button:focus-visible { outline: 3px solid var(--pine-2); outline-offset: 2px; }
.source-tab-icon { width: 20px; height: 20px; color: var(--text-tertiary); }
.source-tab-button[aria-selected="true"] .source-tab-icon { color: inherit; }
.source-tab-copy { display: grid; gap: 3px; min-width: 0; }
.source-tab-label { font-size: var(--fs-body); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.source-tab-description { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); overflow-wrap: anywhere; }
.source-tab-button[aria-selected="true"] .source-tab-description { color: var(--text-on-accent); }
.source-tab-button--file { padding-right: 36px; }
.source-tab-arrow { position: absolute; top: 50%; right: 12px; width: 16px; height: 16px; transform: translateY(-50%); color: var(--text-tertiary); fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.source-tab-button--file[aria-selected="true"] .source-tab-arrow { color: inherit; }

.task-layout { display: grid; grid-template-columns: minmax(330px, .78fr) minmax(0, 1.22fr); gap: 12px; align-items: start; }
.source-column { display: grid; gap: 12px; min-width: 0; }
.task-layout .panel-pad { padding: 14px; }
.task-layout .panel-head { margin-bottom: 10px; }
.task-layout .textarea { min-height: 48px; }
.task-layout .field-row { align-items: stretch; }
.task-layout .field-row .button { min-height: 0; padding: 0 16px; }
.task-layout .field-note { font-size: var(--fs-body-sm); }
.access-section { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
.access-options { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; margin-top: 8px; }
.access-choice { display: flex; align-items: center; gap: 7px; padding: 9px; border: 1px solid var(--line); border-radius: 6px; font-size: var(--fs-body); }
.access-choice.is-selected { border-color: #7da5ff; background: #f5f8ff; }
.access-choice.is-disabled { color: var(--muted); }
.access-choice small { font-size: var(--fs-caption); }
.platform-account-picker-label { margin: 10px 0 0; font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.account-card-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; margin-top: 8px; }
.account-card { display: flex; align-items: center; gap: 9px; min-width: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); cursor: pointer; transition: border-color .16s ease, background-color .16s ease; }
.account-card:hover { border-color: var(--pine-2); }
.account-card input[type="radio"] { flex: none; margin: 0; accent-color: var(--pine); }
.account-card.is-selected { border-color: var(--pine); background: var(--pine-soft); }
.account-card:has(input:focus-visible) { outline: 2px solid var(--pine); outline-offset: 1px; }
.account-card.is-unavailable { background: var(--paper-2); cursor: not-allowed; }
.account-card.is-unavailable .account-card-name { color: var(--text-tertiary); }
.account-card-avatar { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; flex: none; }
.account-card-avatar-fallback { display: flex; align-items: center; justify-content: center; background: var(--pine-soft); color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.account-card-info { display: grid; gap: 2px; min-width: 0; }
.account-card-name { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); color: var(--text-primary); line-height: var(--lh-tight); }
.account-card-status { font-size: var(--fs-caption); font-weight: var(--fw-medium); color: var(--text-tertiary); }
.account-card-status.is-ok { color: var(--success); }
.access-section .helper { margin: 7px 0 0; }
.status-details { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.6; }
.status-details summary { color: var(--pine); cursor: pointer; }
.status-details ul { padding-left: 18px; }
.steps-panel .path-preview { background: #f5f8ff; }
.steps-panel .form-footer { margin-top: 10px; padding-top: 10px; }
.task-model-profile-panel { display: grid; gap: 9px; margin-top: 10px; padding: 12px 14px; border: 1px solid #d9e5ff; border-radius: 7px; background: #fff; }
.task-model-profile-choice { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 7px 12px; }
.task-model-profile-choice .form-label { grid-column: 1 / -1; margin: 0; }
.task-model-profile-choice .select-control { width: 100%; min-width: 0; }
.task-model-profile-choice .text-button { white-space: nowrap; }
.task-model-profile-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)) auto; align-items: center; gap: 8px; padding: 9px 10px; border-radius: 6px; background: #f5f8ff; }
.task-model-profile-summary > span:not(.task-model-profile-key) { display: grid; gap: 3px; min-width: 0; }
.task-model-profile-summary small { color: var(--text-tertiary); font-size: var(--fs-caption); }
.task-model-profile-summary strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-body-sm); }
.task-model-profile-key { padding: 4px 8px; border-radius: 5px; font-size: var(--fs-caption); font-weight: var(--fw-medium); white-space: nowrap; }
.task-model-profile-key.is-ready { color: #147d48; background: #e9f7ef; }
.task-model-profile-key.is-missing { color: #9b5a00; background: #fff4d8; }
:deep(.step-dialog) { border-radius: 9px; box-shadow: 0 18px 55px rgba(31,35,41,.22); }
:deep(.step-dialog .el-dialog__header) { padding: 16px 20px; margin-right: 0; border-bottom: 1px solid var(--line); }
:deep(.step-dialog .el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
:deep(.step-dialog .el-dialog__headerbtn) { top: 16px; right: 20px; width: 24px; height: 24px; }
:deep(.step-dialog .el-dialog__body) { padding: 18px 20px; color: var(--text-primary); font-size: var(--fs-body); line-height: 1.65; }
:deep(.step-dialog .el-dialog__body p) { margin: 0 0 10px; color: var(--text-secondary); }
:deep(.step-dialog .el-dialog__footer) { padding: 16px 20px; border-top: 1px solid var(--line); }
.select-field { width: 100%; }
:deep(.select-field .el-select__wrapper) { min-height: 38px; border-radius: 6px; }
.transcribe-settings { padding: 15px; border: 1px solid #d9e5ff; border-radius: 6px; background: #f8faff; }
.transcribe-settings h3 { margin: 0 0 12px; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.transcribe-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.transcribe-fields label { display: grid; gap: 5px; }
.transcribe-language { grid-column: 1 / -1; }
.transcribe-status { padding: 10px; border: 1px solid #d9e5ff; border-radius: 5px; background: #f2f6ff; }
@media (max-width: 850px) { .task-layout { grid-template-columns: 1fr; } }
@media (max-width: 850px) { .task-model-profile-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 520px) { .source-mode-switch { grid-template-columns: 1fr; } }
@media (max-width: 480px) { .transcribe-fields { grid-template-columns: 1fr; } .transcribe-language { grid-column: auto; } }

.local-import-panel { display: grid; gap: 12px; }
.comment-sort-choice { display: grid; gap: .45rem; max-width: 18rem; margin-top: .75rem; }
</style>
