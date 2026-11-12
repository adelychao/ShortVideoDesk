<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, api } from '../api/client'
import type { LocalAsrStatus, ModelCapabilityStatus, ModelProfile, TaskTranscriptionConfig } from '../api/types'

const props = defineProps<{
  taskId: string
  stepId: string
  config: TaskTranscriptionConfig
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'retry', stepId: string, transcriptionAuthorized: boolean): void
}>()

const editing = ref(false)
const mode = ref<'cloud' | 'local'>('cloud')
const profileId = ref('')
const asrModelId = ref('')
const localModel = ref('small')
const localLanguage = ref<'auto' | 'zh' | 'en'>('auto')
const localDevice = ref<'auto' | 'cpu' | 'cuda'>('auto')
const savedSnapshot = ref('')
const authorized = ref(false)
const saving = ref(false)
const profilesLoading = ref(false)
const capabilityLoading = ref(false)
const profilesError = ref('')
const localStatusError = ref('')
const saveError = ref('')
const message = ref('')
const profiles = ref<ModelProfile[]>([])
const localStatus = ref<LocalAsrStatus | null>(null)
const capability = ref<ModelCapabilityStatus | null>(null)
let capabilityRequest = 0

const transcriptionProfiles = computed(() => profiles.value.filter((profile) => profile.supports_audio_transcription))
const selectedProfile = computed(() => transcriptionProfiles.value.find((profile) => profile.id === profileId.value) ?? null)
const localModelStatus = computed(() => localStatus.value?.models.find((item) => item.id === localModel.value) ?? null)
const localModelReady = computed(() => Boolean(localStatus.value?.installed && localModelStatus.value?.ready))
const capabilityUnsupported = computed(() => capability.value?.status === 'verified' && !capability.value.modalities.includes('audio'))

function snapshot(): string {
  return JSON.stringify({
    mode: mode.value,
    profileId: profileId.value,
    asrModelId: asrModelId.value.trim(),
    localModel: localModel.value,
    localLanguage: localLanguage.value,
    localDevice: localDevice.value,
  })
}

const isDirty = computed(() => snapshot() !== savedSnapshot.value)
const canSave = computed(() => {
  if (!editing.value || !isDirty.value || saving.value || props.busy) return false
  if (mode.value === 'local') return localModelReady.value
  return Boolean(
    selectedProfile.value?.credential.configured
    && selectedProfile.value.credential.available
    && asrModelId.value.trim()
    && !capabilityLoading.value
    && !capabilityUnsupported.value,
  )
})
const canRetry = computed(() => {
  if (props.busy || saving.value || isDirty.value) return false
  if (mode.value === 'local') return localModelReady.value
  return Boolean(
    selectedProfile.value?.credential.configured
    && selectedProfile.value.credential.available
    && asrModelId.value.trim()
    && !capabilityLoading.value
    && !capabilityUnsupported.value
    && authorized.value,
  )
})

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '无法保存转写配置，请稍后重试。'
}

function applyConfig(config: TaskTranscriptionConfig): void {
  mode.value = config.mode
  profileId.value = config.profile_id ?? ''
  asrModelId.value = config.asr_model_id ?? ''
  localModel.value = config.local_model ?? 'small'
  localLanguage.value = config.local_language ?? 'auto'
  localDevice.value = config.local_device ?? 'auto'
  savedSnapshot.value = snapshot()
  authorized.value = false
}

watch(() => props.config, (config) => {
  if (!isDirty.value) applyConfig(config)
}, { deep: true })

watch([mode, profileId, asrModelId, selectedProfile], async ([currentMode, currentProfileId, currentModelId, profile]) => {
  capability.value = null
  capabilityLoading.value = false
  const requestId = ++capabilityRequest
  if (currentMode !== 'cloud' || !currentProfileId || !currentModelId.trim()) return
  if (!profile) return
  capabilityLoading.value = true
  try {
    const result = await api.getModelCapability(profile.provider_id, currentModelId.trim())
    if (requestId === capabilityRequest) capability.value = result
  } catch {
    if (requestId === capabilityRequest) capability.value = null
  } finally {
    if (requestId === capabilityRequest) capabilityLoading.value = false
  }
}, { immediate: true })

watch(asrModelId, () => {
  authorized.value = false
  saveError.value = ''
  message.value = ''
})

watch(() => props.taskId, () => {
  editing.value = false
  applyConfig(props.config)
  void loadOptions()
})

async function loadOptions(): Promise<void> {
  profilesLoading.value = true
  profilesError.value = ''
  localStatusError.value = ''
  const [profileResult, localResult] = await Promise.allSettled([
    api.getModelProfiles(),
    api.getLocalAsrStatus(),
  ])
  if (profileResult.status === 'fulfilled') profiles.value = profileResult.value.items
  else profilesError.value = errorMessage(profileResult.reason)
  if (localResult.status === 'fulfilled') localStatus.value = localResult.value
  else localStatusError.value = errorMessage(localResult.reason)
  profilesLoading.value = false
}

function changeMode(): void {
  saveError.value = ''
  message.value = ''
  authorized.value = false
}

function changeProfile(): void {
  const profile = selectedProfile.value
  if (profile) asrModelId.value = profile.asr_model_id ?? ''
  saveError.value = ''
  message.value = ''
  authorized.value = false
}

async function saveConfig(): Promise<void> {
  if (!canSave.value) return
  saving.value = true
  saveError.value = ''
  message.value = ''
  try {
    const result = mode.value === 'cloud'
      ? await api.updateTranscriptionConfig(props.taskId, {
          mode: 'cloud',
          profile_id: profileId.value,
          asr_model_id: asrModelId.value.trim(),
        })
      : await api.updateTranscriptionConfig(props.taskId, {
          mode: 'local',
          local_model: localModel.value,
          local_language: localLanguage.value,
          local_device: localDevice.value,
        })
    applyConfig(result.transcription_config)
    editing.value = false
    message.value = '配置已保存，仅对当前任务的语音转写生效。请确认后手动重试。'
  } catch (error) {
    saveError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}

function retry(): void {
  if (!canRetry.value) return
  emit('retry', props.stepId, mode.value === 'cloud')
}

onMounted(() => {
  applyConfig(props.config)
  void loadOptions()
})
</script>

<template>
  <section class="transcription-retry-settings" aria-label="语音转写配置与重试">
    <div class="transcription-retry-actions">
      <button class="button button-secondary button-small" type="button" :disabled="busy || saving" @click="editing = !editing">
        {{ editing ? '收起配置' : '修改本步配置' }}
      </button>
      <button class="button button-small" type="button" :disabled="!canRetry" @click="retry">
        {{ busy ? '正在提交…' : '重试此步骤' }}
      </button>
    </div>

    <div v-if="editing" class="transcription-config-editor">
      <p class="transcription-scope-note">设置只用于本任务的语音转写，不会更改全局配置或本任务其他模型步骤。</p>
      <p v-if="profilesError" class="transcription-error" role="alert">{{ profilesError }}</p>
      <p v-if="localStatusError" class="transcription-error" role="alert">{{ localStatusError }}</p>

      <label class="transcription-field">
        <span>转写方式</span>
        <select v-model="mode" class="select-control" :disabled="busy || saving" @change="changeMode">
          <option value="local">本机转写（免费 · 音频不上传）</option>
          <option value="cloud">云端转写（联网 · 按用量计费）</option>
        </select>
      </label>

      <template v-if="mode === 'cloud'">
        <label class="transcription-field">
          <span>模型配置档</span>
          <select v-model="profileId" class="select-control" :disabled="busy || saving || profilesLoading" @change="changeProfile">
            <option value="">{{ profilesLoading ? '正在读取配置档…' : '请选择支持转写的配置档' }}</option>
            <option
              v-for="profile in transcriptionProfiles"
              :key="profile.id"
              :value="profile.id"
              :disabled="!profile.credential.configured || !profile.credential.available"
            >{{ profile.name }} · {{ profile.provider_name }}{{ profile.credential.configured ? '' : ' · API Key 未配置' }}</option>
          </select>
        </label>
        <label class="transcription-field">
          <span>ASR 模型 ID</span>
          <input v-model.trim="asrModelId" class="input" maxlength="128" autocomplete="off" spellcheck="false" :disabled="busy || saving || !selectedProfile" placeholder="例如：glm-asr-2512" />
        </label>
        <p v-if="capabilityLoading" class="transcription-hint" role="status">正在核对模型能力…</p>
        <p v-else-if="capabilityUnsupported" class="transcription-error" role="alert">模型目录确认该模型不支持音频转写，请填写 ASR 模型 ID。</p>
        <p v-else-if="capability?.status === 'verified'" class="transcription-hint" role="status">模型目录已确认支持 {{ capability.modalities.includes('audio') ? '音频' : '其他' }} 输入。</p>
        <p v-else-if="asrModelId.trim() && selectedProfile" class="transcription-warning" role="status">模型目录暂未确认此模型的音频能力；请核对服务商文档，调用仍可能被拒绝。</p>
        <p v-if="!transcriptionProfiles.length && !profilesLoading" class="transcription-hint">没有可用的云端转写配置档。<RouterLink :to="{ name: 'settings', query: { tab: 'models' } }">前往模型设置</RouterLink></p>
      </template>

      <template v-else>
        <label class="transcription-field">
          <span>本地模型</span>
          <select v-model="localModel" class="select-control" :disabled="busy || saving || !localStatus">
            <option v-for="model in localStatus?.models ?? []" :key="model.id" :value="model.id" :disabled="!model.ready">
              {{ model.id }} · {{ model.ready ? '已准备' : model.job?.state === 'downloading' ? '下载中' : '未准备' }}
            </option>
          </select>
        </label>
        <label class="transcription-field">
          <span>语言</span>
          <select v-model="localLanguage" class="select-control" :disabled="busy || saving">
            <option value="auto">自动检测</option>
            <option value="zh">中文</option>
            <option value="en">英文</option>
          </select>
        </label>
        <label class="transcription-field">
          <span>设备</span>
          <select v-model="localDevice" class="select-control" :disabled="busy || saving">
            <option value="auto">自动选择</option>
            <option value="cpu">CPU</option>
            <option value="cuda">CUDA</option>
          </select>
        </label>
        <p v-if="!localModelReady" class="transcription-warning" role="status">
          本机转写尚未准备可用模型。<RouterLink :to="{ name: 'settings', query: { tab: 'models' } }">前往模型设置</RouterLink>
        </p>
      </template>

      <p v-if="saveError" class="transcription-error" role="alert">{{ saveError }}</p>
      <button class="button button-secondary button-small" type="button" :disabled="!canSave" @click="saveConfig">
        {{ saving ? '正在保存…' : '保存本任务配置' }}
      </button>
    </div>

    <p v-if="message" class="transcription-success" role="status">{{ message }}</p>
    <label v-if="mode === 'cloud'" class="consent-check transcription-consent">
      <input v-model="authorized" type="checkbox" :disabled="busy || saving || isDirty" />
      我确认将音频发送给 {{ selectedProfile?.provider_name || config.provider_name || '所选云端服务商' }} / {{ asrModelId || '当前 ASR 模型' }}，并了解可能产生费用。
    </label>
    <p v-if="isDirty" class="transcription-hint">配置有未保存的修改；保存后再重试。</p>
    <p v-else-if="mode === 'local'" class="transcription-hint">本地重试会复用已下载的视频，不会发送音频到云端。</p>
  </section>
</template>

<style scoped>
.transcription-retry-settings { display: grid; gap: 9px; width: 100%; min-width: 0; }
.transcription-retry-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.transcription-config-editor { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 12px; min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper-2); }
.transcription-scope-note, .transcription-hint, .transcription-warning, .transcription-error, .transcription-success { grid-column: 1 / -1; margin: 0; font-size: var(--fs-body-sm); line-height: var(--lh-base); overflow-wrap: anywhere; }
.transcription-scope-note, .transcription-hint { color: var(--text-secondary); }
.transcription-warning { color: var(--warning); }
.transcription-error { color: var(--danger); }
.transcription-success { color: var(--success); }
.transcription-field { display: grid; gap: 5px; min-width: 0; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.transcription-field .select-control, .transcription-field .input { width: 100%; min-width: 0; }
.transcription-consent { margin: 0; }
.transcription-config-editor a, .transcription-hint a, .transcription-warning a { color: var(--pine); text-underline-offset: 2px; }
@media (max-width: 640px) { .transcription-config-editor { grid-template-columns: minmax(0, 1fr); } }
</style>
