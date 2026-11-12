<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElAlert, ElButton, ElCheckbox, ElInput, ElMessageBox, ElOption, ElSelect, ElTag } from 'element-plus'
import type { CheckboxValueType } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/tag/style/css'
import { ApiError, api } from '../api/client'
import type { ModelCapabilityStatus, ModelProfile, ModelProviderOption, SaveModelProfileRequest } from '../api/types'

const props = defineProps<{ providers: ModelProviderOption[] }>()
const emit = defineEmits<{
  (event: 'default-profile-updated', profile: ModelProfile | null): void
}>()

const profiles = ref<ModelProfile[]>([])
const activeProfileId = ref('')
const profileId = ref('')
const draft = reactive({
  name: '',
  provider_id: '',
  model_id: '',
  asr_model_id: '',
  model_base_url: '',
  capabilities: ['text_generation'] as Array<'text_generation' | 'image_generation'>,
  image_model_id: '',
  image_base_url: '',
  api_key: '',
})
const credential = ref<ModelProfile['credential']>({ configured: false, available: false, masked_key: null })
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const removing = ref(false)
const migrating = ref(false)
const error = ref('')
const message = ref('')
const connectionMessage = ref('')
const nameError = ref('')
const providerError = ref('')
const modelIdError = ref('')
const capability = ref<ModelCapabilityStatus | null>(null)
const capabilityLoading = ref(false)
let capabilityRequest = 0

const activeProfile = computed(() => profiles.value.find((item) => item.id === activeProfileId.value) ?? null)
const selectedProvider = computed(() => props.providers.find((item) => item.id === draft.provider_id) ?? null)
const hasProfiles = computed(() => profiles.value.length > 0)
const hasTextCapability = computed(() => draft.capabilities.includes('text_generation'))
const hasImageCapability = computed(() => draft.capabilities.includes('image_generation'))

function errorMessage(value: unknown): string {
  return value instanceof ApiError ? value.message : '模型配置请求失败，请稍后重试。'
}

function clearValidation(): void {
  nameError.value = ''
  providerError.value = ''
  modelIdError.value = ''
}

function toggleCapability(capabilityId: 'text_generation' | 'image_generation', checked: CheckboxValueType): void {
  if (Boolean(checked)) {
    draft.capabilities = Array.from(new Set([...draft.capabilities, capabilityId]))
  } else {
    draft.capabilities = draft.capabilities.filter((item) => item !== capabilityId)
  }
}

function applyProfile(profile: ModelProfile | null): void {
  clearValidation()
  message.value = ''
  error.value = ''
  connectionMessage.value = ''
  capability.value = null
  profileId.value = profile?.id ?? ''
  activeProfileId.value = profile?.id ?? ''
  draft.name = profile?.name ?? ''
  draft.provider_id = profile?.provider_id ?? props.providers[0]?.id ?? ''
  draft.model_id = profile?.model_id ?? ''
  draft.asr_model_id = profile?.asr_model_id ?? ''
  draft.capabilities = profile ? [...profile.capabilities] : ['text_generation']
  draft.image_model_id = profile?.image_model_id ?? ''
  draft.image_base_url = profile?.image_base_url ?? ''
  const provider = props.providers.find((item) => item.id === draft.provider_id)
  draft.model_base_url = profile?.model_base_url ?? provider?.default_base_url ?? ''
  draft.api_key = ''
  credential.value = profile?.credential ?? { configured: false, available: false, masked_key: null }
  if (profile?.is_default) emit('default-profile-updated', profile)
  void inspectCapability()
}

async function loadProfiles(preferredId?: string): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await api.getModelProfiles()
    profiles.value = result.items
    const selected = result.items.find((item) => item.id === preferredId)
      ?? result.items.find((item) => item.is_default)
      ?? result.items[0]
      ?? null
    applyProfile(selected)
    emit('default-profile-updated', result.items.find((item) => item.is_default) ?? null)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function selectProfile(profile: ModelProfile): void {
  if (saving.value || migrating.value) return
  applyProfile(profile)
}

function createProfile(): void {
  if (saving.value || migrating.value) return
  applyProfile(null)
  draft.name = ''
  draft.model_id = ''
  draft.asr_model_id = ''
  draft.capabilities = ['text_generation']
  draft.image_model_id = ''
  draft.image_base_url = ''
  draft.api_key = ''
  message.value = ''
}

function onProviderChange(): void {
  const provider = selectedProvider.value
  draft.model_id = ''
  draft.asr_model_id = ''
  draft.image_model_id = ''
  draft.image_base_url = ''
  draft.model_base_url = provider?.default_base_url ?? ''
  draft.api_key = ''
  credential.value = { configured: false, available: credential.value.available, masked_key: null }
  connectionMessage.value = ''
  capability.value = null
  clearValidation()
  void inspectCapability()
}

function validate(): boolean {
  clearValidation()
  let valid = true
  if (!draft.name.trim()) {
    nameError.value = '请填写配置名称。'
    valid = false
  }
  if (!draft.provider_id) {
    providerError.value = '请选择模型服务商。'
    valid = false
  }
  if (!draft.model_id.trim()) {
    modelIdError.value = '请填写模型 ID。'
    valid = false
  }
  if (selectedProvider.value && !selectedProvider.value.default_base_url && !draft.model_base_url.trim()) {
    error.value = '当前服务商需要填写服务地址。'
    valid = false
  }
  return valid
}

function payload(): SaveModelProfileRequest {
  const baseUrl = draft.model_base_url.trim()
  return {
    name: draft.name.trim(),
    provider_id: draft.provider_id,
    model_id: draft.model_id.trim(),
    asr_model_id: draft.asr_model_id.trim() || null,
    model_base_url: baseUrl && baseUrl !== selectedProvider.value?.default_base_url ? baseUrl : null,
    capabilities: [...draft.capabilities],
    image_model_id: draft.image_model_id.trim() || null,
    image_base_url: draft.image_base_url.trim() || null,
    api_key: draft.api_key.trim() || null,
    make_default: hasTextCapability.value && (profileId.value
      ? (activeProfile.value?.is_default ?? false)
      : !profiles.value.some((item) => item.is_default)),
    make_image_default: hasImageCapability.value && (profileId.value
      ? (activeProfile.value?.is_image_default ?? false)
      : !profiles.value.some((item) => item.is_image_default)),
  }
}

async function saveProfile(): Promise<void> {
  error.value = ''
  message.value = ''
  connectionMessage.value = ''
  if (saving.value || !validate()) return
  saving.value = true
  try {
    const input = payload()
    const keyProvided = Boolean(draft.api_key.trim())
    const saved = profileId.value
      ? await api.updateModelProfile(profileId.value, input)
      : await api.createModelProfile(input)
    const fallback = profiles.value.some((item) => item.id === saved.id)
      ? profiles.value.map((item) => item.id === saved.id ? saved : {
        ...item,
        ...(saved.is_default ? { is_default: false } : {}),
        ...(saved.is_image_default ? { is_image_default: false } : {}),
      })
      : [saved, ...profiles.value.map((item) => ({
        ...item,
        ...(saved.is_default ? { is_default: false } : {}),
        ...(saved.is_image_default ? { is_image_default: false } : {}),
      }))]
    profiles.value = fallback
    try {
      profiles.value = (await api.getModelProfiles()).items
    } catch {
      // The saved profile remains available in the local list if refresh fails.
    }
    const selected = profiles.value.find((item) => item.id === saved.id) ?? saved
    applyProfile(selected)
    credential.value = selected.credential
    message.value = keyProvided ? '配置和 API Key 已保存。' : '配置已保存；留空 API Key 时保留已有凭据。'
    draft.api_key = ''
    emit('default-profile-updated', profiles.value.find((item) => item.is_default) ?? null)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function migrateLegacyApiKey(): Promise<void> {
  const selected = activeProfile.value
  if (!selected || selected.id !== profileId.value || credential.value.configured || migrating.value || saving.value) return
  try {
    await ElMessageBox.confirm(
      '将把旧版 LocalVideoWorkbench 凭据空间中该服务商的 API Key 复制到 ShortVideoDesk。旧凭据会保留，不会删除。',
      '迁移旧 API Key',
      { confirmButtonText: '确认迁移', cancelButtonText: '取消', type: 'warning', customClass: 'settings-profile-remove-confirm' },
    )
  } catch {
    return
  }
  migrating.value = true
  error.value = ''
  message.value = ''
  try {
    const migrated = await api.migrateLegacyModelProfileKey(selected.id)
    profiles.value = profiles.value.map((item) => item.id === migrated.id ? migrated : item)
    if (profileId.value === migrated.id) credential.value = migrated.credential
    message.value = '旧 API Key 已复制到 ShortVideoDesk 凭据空间；旧凭据仍保留。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    migrating.value = false
  }
}

async function setDefault(): Promise<void> {
  if (!activeProfile.value || activeProfile.value.is_default || saving.value || !activeProfile.value.capabilities.includes('text_generation')) return
  error.value = ''
  message.value = ''
  saving.value = true
  try {
    const saved = await api.setDefaultModelProfile(activeProfile.value.id)
    profiles.value = profiles.value.map((item) => ({ ...item, is_default: item.id === saved.id }))
    applyProfile(saved)
    credential.value = saved.credential
    message.value = '默认文本模型已更新。'
    emit('default-profile-updated', saved)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function setImageDefault(): Promise<void> {
  if (!activeProfile.value || activeProfile.value.is_image_default || saving.value || !activeProfile.value.capabilities.includes('image_generation')) return
  error.value = ''
  message.value = ''
  saving.value = true
  try {
    const saved = await api.setDefaultImageModelProfile(activeProfile.value.id)
    profiles.value = profiles.value.map((item) => ({ ...item, is_image_default: item.id === saved.id }))
    applyProfile(saved)
    credential.value = saved.credential
    message.value = '默认配图模型已更新。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function deleteProfile(): Promise<void> {
  if (!activeProfile.value || removing.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除“${activeProfile.value.name}”配置吗？历史任务仍会保留创建时的模型信息。`,
      '删除模型配置',
      { confirmButtonText: '确认删除', cancelButtonText: '取消', confirmButtonType: 'danger', type: 'warning', customClass: 'settings-profile-remove-confirm' },
    )
  } catch {
    return
  }
  removing.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api.deleteModelProfile(activeProfile.value.id)
    const retainedCredential = result.credential_retained_for_tasks
    await loadProfiles(result.default_profile?.id)
    message.value = retainedCredential
      ? '配置已移除；为保留历史任务的重试能力，其凭据仍保存在 Windows 凭据管理器中。'
      : '配置已删除。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    removing.value = false
  }
}

async function testConnection(): Promise<void> {
  if (!profileId.value || !credential.value.configured || testing.value) return
  testing.value = true
  connectionMessage.value = ''
  try {
    const result = await api.testModelConnection({ profile_id: profileId.value })
    connectionMessage.value = result.message
  } catch (cause) {
    connectionMessage.value = errorMessage(cause)
  } finally {
    testing.value = false
  }
}

async function inspectCapability(): Promise<void> {
  const request = ++capabilityRequest
  const providerId = draft.provider_id
  const modelId = draft.model_id.trim()
  capability.value = null
  if (!providerId || !modelId) return
  capabilityLoading.value = true
  try {
    const result = await api.getModelCapability(providerId, modelId)
    if (request === capabilityRequest) capability.value = result
  } catch {
    if (request === capabilityRequest) capability.value = null
  } finally {
    if (request === capabilityRequest) capabilityLoading.value = false
  }
}

onMounted(() => void loadProfiles())
</script>

<template>
  <section class="panel settings-section model-profiles-panel" aria-labelledby="model-profiles-title">
    <div class="panel-head">
      <div><h2 id="model-profiles-title">云端模型配置</h2><p>文本生成与文章配图共用模型档案和 API Key；可为两种用途分别设置默认模型。</p></div>
      <el-button :disabled="saving || loading" @click="createProfile">＋ 新建配置</el-button>
    </div>
    <el-alert type="info" :closable="false" show-icon role="note" class="model-profile-note">
      API Key 按配置档保存在 Windows 凭据管理器中。图片生成使用 OpenAI Images 兼容接口；多模态模型可共用模型 ID，也可填写单独的图片模型 ID 或服务地址。生成每张图片前仍会要求确认费用。
    </el-alert>
    <p v-if="loading" class="helper" role="status">正在读取模型配置档…</p>
    <el-alert v-if="error" type="error" :closable="false" show-icon role="alert" class="model-profile-note">{{ error }}</el-alert>
    <div v-if="!loading" class="model-profiles-layout">
      <aside class="model-profile-list" aria-label="已保存的模型配置">
        <p v-if="!hasProfiles" class="helper model-profile-empty">还没有模型配置档，请新建一套。</p>
        <button
          v-for="profile in profiles"
          :key="profile.id"
          class="model-profile-item"
          :class="{ 'is-active': activeProfileId === profile.id }"
          type="button"
          :aria-pressed="activeProfileId === profile.id"
          @click="selectProfile(profile)"
        >
          <span class="model-profile-item-copy"><strong>{{ profile.name }}</strong><small>{{ profile.provider_name }} · {{ profile.model_id }}</small><small class="model-profile-badges"><el-tag v-if="profile.capabilities.includes('text_generation')" type="info" effect="plain" size="small">文本</el-tag><el-tag v-if="profile.capabilities.includes('image_generation')" type="info" effect="plain" size="small">配图</el-tag></small></span>
          <span class="model-profile-defaults"><el-tag v-if="profile.is_default" type="success" size="small">文本默认</el-tag><el-tag v-if="profile.is_image_default" type="success" size="small">配图默认</el-tag></span>
        </button>
      </aside>

      <form class="model-profile-editor" @submit.prevent="saveProfile">
        <div class="model-profile-editor-head">
          <h3>{{ profileId ? '编辑配置' : '新建配置' }}</h3>
          <el-tag v-if="credential.configured" type="success">API Key 已配置</el-tag>
          <el-tag v-else type="warning">API Key 未配置</el-tag>
        </div>
        <div class="model-profile-fields">
          <label class="form-label" for="model-profile-name"><span class="required-marker" aria-hidden="true">*</span>配置名称</label>
          <div>
            <el-input id="model-profile-name" v-model="draft.name" maxlength="64" placeholder="例如：DeepSeek 主力" :aria-invalid="Boolean(nameError)" @update:model-value="nameError = ''" />
            <p v-if="nameError" class="required-field-error" role="alert">{{ nameError }}</p>
          </div>

          <label class="form-label" for="model-profile-provider"><span class="required-marker" aria-hidden="true">*</span>模型服务商</label>
          <div>
            <el-select id="model-profile-provider" v-model="draft.provider_id" popper-class="settings-theme-popper" :aria-invalid="Boolean(providerError)" @change="onProviderChange">
              <el-option v-for="provider in providers" :key="provider.id" :label="provider.label" :value="provider.id" />
            </el-select>
            <p v-if="providerError" class="required-field-error" role="alert">{{ providerError }}</p>
            <p v-else-if="selectedProvider" class="helper">{{ selectedProvider.description }}</p>
          </div>

          <label class="form-label" for="model-profile-model"><span class="required-marker" aria-hidden="true">*</span>默认模型 ID</label>
          <div>
            <el-input id="model-profile-model" v-model="draft.model_id" maxlength="128" autocomplete="off" spellcheck="false" placeholder="手动输入服务商提供的模型 ID" :aria-invalid="Boolean(modelIdError)" @update:model-value="modelIdError = ''; void inspectCapability()" />
            <p v-if="modelIdError" class="required-field-error" role="alert">{{ modelIdError }}</p>
            <p v-else-if="capabilityLoading" class="helper">正在查询模型能力…</p>
            <p v-else-if="capability" class="helper">{{ capability.status === 'verified' ? `能力目录识别为 ${capability.display_name || capability.model_id}。` : capability.message }}</p>
          </div>

          <span class="form-label">支持能力</span>
          <div class="model-profile-capabilities">
            <el-checkbox :model-value="hasTextCapability" :disabled="hasTextCapability && !hasImageCapability" @update:model-value="(value: CheckboxValueType) => toggleCapability('text_generation', value)">文本生成</el-checkbox>
            <el-checkbox :model-value="hasImageCapability" :disabled="hasImageCapability && !hasTextCapability" @update:model-value="(value: CheckboxValueType) => toggleCapability('image_generation', value)">图片生成</el-checkbox>
            <p class="helper">“图片生成”表示模型能直接输出图像；能接收图片并理解内容不代表能生成图片。图片生成会共用当前配置的 API Key。</p>
          </div>

          <label class="form-label" for="model-profile-asr">音频转写模型 ID</label>
          <div>
            <el-input id="model-profile-asr" v-model="draft.asr_model_id" maxlength="128" autocomplete="off" spellcheck="false" :disabled="selectedProvider?.supports_audio_transcription === false" placeholder="手动输入服务商提供的 ASR 模型 ID" />
            <p class="helper">仅云端语音转写使用；本机离线转写可在任务中选择。</p>
          </div>

          <label class="form-label" for="model-profile-url">服务地址</label>
          <div>
            <el-input id="model-profile-url" v-model="draft.model_base_url" type="url" maxlength="2048" :placeholder="selectedProvider?.default_base_url || '填写自定义兼容 API 根地址'" />
            <p class="helper">{{ selectedProvider?.default_base_url ? `默认地址：${selectedProvider.default_base_url}。` : '自定义服务地址仅允许 HTTPS；本机服务可使用 HTTP。' }}</p>
          </div>

          <template v-if="hasImageCapability">
            <label class="form-label" for="model-profile-image-model">图片模型 ID</label>
            <div>
              <el-input id="model-profile-image-model" v-model="draft.image_model_id" maxlength="128" autocomplete="off" spellcheck="false" placeholder="留空时共用默认模型 ID" />
              <p class="helper">仅当图片生成使用不同模型时填写。</p>
              <p v-if="draft.provider_id === 'zhipu'" class="helper">智谱 GLM-5.3-Flash 是图像理解模型，只输出文本；图片模型 ID 请填 glm-image。</p>
            </div>

            <label class="form-label" for="model-profile-image-url">图片服务地址</label>
            <div>
              <el-input id="model-profile-image-url" v-model="draft.image_base_url" type="url" maxlength="2048" placeholder="留空时共用上方服务地址" />
              <p class="helper">留空时使用上方服务地址的 `/images/generations` 接口。</p>
            </div>
          </template>

          <label class="form-label" for="model-profile-key">API Key</label>
          <div>
            <el-input id="model-profile-key" v-model="draft.api_key" type="password" show-password maxlength="4096" autocomplete="new-password" :placeholder="credential.configured ? `已配置 ${credential.masked_key || '凭据'}；留空保留` : '输入此配置专用的 API Key'" />
            <p class="helper">{{ credential.available ? '保存后仅写入 Windows 凭据管理器，不会返回完整密钥。' : 'Windows 凭据管理器暂不可用。' }}</p>
            <el-button v-if="profileId && activeProfile && !credential.configured && credential.available && !draft.api_key.trim() && draft.provider_id === activeProfile.provider_id" link :disabled="saving || migrating || removing" @click="migrateLegacyApiKey">{{ migrating ? '迁移中…' : '从旧配置迁移 API Key' }}</el-button>
            <p v-if="profileId && !credential.configured" class="helper">旧密钥不会自动迁移；仅在点击并确认后复制，且保留原凭据。</p>
          </div>
        </div>

        <el-alert v-if="message" type="success" :closable="false" show-icon role="status" class="model-profile-note">{{ message }}</el-alert>
        <el-alert v-if="connectionMessage" type="info" :closable="false" show-icon role="status" class="model-profile-note">{{ connectionMessage }}</el-alert>
        <div class="model-profile-actions">
          <el-button type="primary" native-type="submit" :disabled="saving">{{ saving ? '正在保存…' : '保存配置' }}</el-button>
          <el-button v-if="profileId && hasTextCapability && !activeProfile?.is_default" :disabled="saving" @click="setDefault">设为文本默认</el-button>
          <el-button v-if="profileId && hasImageCapability && !activeProfile?.is_image_default" :disabled="saving" @click="setImageDefault">设为配图默认</el-button>
          <el-button v-if="profileId && hasTextCapability" :disabled="testing || !credential.configured" @click="testConnection">{{ testing ? '测试中…' : '测试文本连接' }}</el-button>
          <el-button v-if="profileId" link type="danger" class="model-profile-delete" :disabled="removing || saving" @click="deleteProfile">{{ removing ? '删除中…' : '删除配置' }}</el-button>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.model-profiles-layout { display: grid; grid-template-columns: minmax(190px, 0.34fr) minmax(0, 1fr); gap: 16px; margin-top: 14px; }
.model-profile-list { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.model-profile-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 66px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--paper); color: inherit; text-align: left; cursor: pointer; }
.model-profile-item:hover, .model-profile-item.is-active { border-color: var(--pine); background: var(--pine-soft); }
.model-profile-item-copy { display: grid; gap: 4px; min-width: 0; }
.model-profile-item-copy strong, .model-profile-item-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-profile-item-copy small { color: var(--muted); }
.model-profile-badges { display: flex; gap: 5px; }
.model-profile-badges .el-tag { --el-tag-bg-color: var(--paper-2); --el-tag-border-color: var(--line); --el-tag-text-color: var(--text-secondary); }
.model-profile-defaults { display: grid; justify-items: end; gap: 4px; flex: 0 0 auto; }
.model-profile-empty { padding: 12px; }
.model-profile-editor { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--paper); }
.model-profile-editor-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
.model-profile-editor-head h3 { margin: 0; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.model-profile-fields { display: grid; grid-template-columns: minmax(145px, 0.38fr) minmax(0, 1fr); align-items: start; gap: 10px 14px; }
.model-profile-fields > .form-label { margin: 9px 0 0; }
.model-profile-fields .helper { margin: 5px 0 0; }
.model-profile-fields :deep(.el-input) { --el-input-height: 36px; }
.model-profile-fields :deep(.el-select__wrapper) { min-height: 36px; }
.model-profile-capabilities { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; }
.model-profile-capabilities .helper { flex-basis: 100%; }
.model-profile-note { margin-top: 12px; }
.model-profile-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.model-profile-actions .el-button { margin-left: 0; }
.model-profile-delete { margin-left: auto; }
@media (max-width: 900px) { .model-profiles-layout { grid-template-columns: 1fr; } .model-profile-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); } }
@media (max-width: 560px) { .model-profile-fields { grid-template-columns: 1fr; gap: 6px; } .model-profile-fields > .form-label { margin-top: 8px; } }
</style>
