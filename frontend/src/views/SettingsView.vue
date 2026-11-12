<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElAlert, ElButton, ElCheckbox, ElCollapse, ElCollapseItem, ElConfigProvider, ElEmpty, ElInput, ElInputNumber, ElMenu, ElMenuItem, ElSelect, ElOption, ElSwitch, ElTabs, ElTabPane, ElTag } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/checkbox/style/css'
import 'element-plus/es/components/collapse/style/css'
import 'element-plus/es/components/collapse-item/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/input-number/style/css'
import 'element-plus/es/components/menu/style/css'
import 'element-plus/es/components/menu-item/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tag/style/css'
import 'element-plus/es/components/switch/style/css'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import type { AppSettings, DiagnosticResult, ModelProfile, ModelProviderOption, ScriptStyleTemplate } from '../api/types'
import { useConfigStore } from '../stores/config'
import DiagnosticsPanel from '../components/DiagnosticsPanel.vue'
import LocalAsrSettings from '../components/LocalAsrSettings.vue'
import ModelProfilesPanel from '../components/ModelProfilesPanel.vue'

function emptySettings(): AppSettings {
  return {
    archive_root: null,
    obsidian_reference_root: null,
    obsidian_output_root: null,
    model_provider: 'zhipu',
    model_id: 'glm-5.3-flash',
    asr_model_id: 'glm-asr-2512',
    model_base_url: null,
    vector_search_enabled: false,
    local_asr_device: 'auto',
    knowledge_card_prompt: '',
    script_style_templates: [],
    video_publish_auto_cleanup_enabled: true,
    video_publish_retention_days: 15,
    video_publish_cover_root: null,
    article_image_root: null,
  }
}

const settings = reactive<AppSettings>(emptySettings())
const route = useRoute()
const settingsTabs = ['folders', 'models', 'knowledge', 'script-styles', 'checks', 'other'] as const
type SettingsTab = typeof settingsTabs[number]
const tabLabels: Record<SettingsTab, string> = {
  folders: '目录与知识库',
  models: '模型与转写',
  knowledge: '知识卡提示词',
  'script-styles': '口播稿风格',
  checks: '运行检查',
  other: '其他设置',
}
const activeTab = ref<SettingsTab>('folders')
const overview = useConfigStore()
const defaultKnowledgeCardPrompt = ref('')
const modelProviders = ref<ModelProviderOption[]>([])
const providerCatalogAvailable = ref(true)
const diagnostics = ref<DiagnosticResult[]>([])
const directoryConflicts = ref<Array<{ code: string; message: string }>>([])
const diagnosticsLoading = ref(false)
const diagnosticsError = ref('')
const loading = ref(true)
const saving = ref(false)
const formError = ref('')
const saveReminder = ref('')
const saveFieldErrors = reactive({ archive_root: '' })
const saveMessage = ref('')
const referencePaths = ref('')
const referenceNotes = ref<Array<{ relative_path: string; title: string; content: string; sha256: string }>>([])
const referenceMessage = ref('')
const readingReferences = ref(false)
const activeScriptStyleId = ref('')
const styleReferenceCheckMessage = ref('')
const checkedStyleReferencePaths = ref<string[]>([])
const checkingStyleReferences = ref(false)
const activeScriptStyle = computed(() => settings.script_style_templates.find((item) => item.id === activeScriptStyleId.value) ?? null)
const imageFolders = [
  { field: 'video_publish_cover_root', label: '视频发布封面目录' },
  { field: 'article_image_root', label: '文章图片目录' },
] as const
type ImageFolderSetting = typeof imageFolders[number]['field']
type FolderSetting = 'archive_root' | 'obsidian_reference_root' | 'obsidian_output_root' | ImageFolderSetting
const selectingFolder = ref<FolderSetting | null>(null)
const folderError = ref('')
const retentionDaysInput = ref<number | undefined>(settings.video_publish_retention_days)

watch(() => settings.video_publish_retention_days, (value) => {
  retentionDaysInput.value = value
}, { immediate: true })

async function selectFolder(field: FolderSetting): Promise<void> {
  if (selectingFolder.value) return
  selectingFolder.value = field
  folderError.value = ''
  try {
    const result = await api.selectLocalFolder()
    if (result.path !== null) {
      settings[field] = result.path
      saveMessage.value = ''
      if (field === 'archive_root') clearSaveValidation()
    }
  } catch (error) {
    folderError.value = errorMessage(error)
  } finally {
    selectingFolder.value = null
  }
}

const selectedProvider = computed(() => modelProviders.value.find((item) => item.id === settings.model_provider) ?? null)

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '本机设置请求失败，请稍后重试。'
}

function applySettings(value: AppSettings): void {
  Object.assign(settings, value)
  if (!settings.script_style_templates.some((item) => item.id === activeScriptStyleId.value)) {
    activeScriptStyleId.value = settings.script_style_templates[0]?.id ?? ''
  }
}

function selectScriptStyle(value: string): void {
  activeScriptStyleId.value = value
  styleReferenceCheckMessage.value = ''
  checkedStyleReferencePaths.value = []
}

function addScriptStyleTemplate(): void {
  const template: ScriptStyleTemplate = {
    id: crypto.randomUUID(),
    name: '',
    instructions: '',
    enabled: true,
    reference_paths: [],
  }
  settings.script_style_templates.push(template)
  activeScriptStyleId.value = template.id
  saveMessage.value = ''
  styleReferenceCheckMessage.value = ''
}

function removeScriptStyleTemplate(): void {
  const current = activeScriptStyle.value
  if (!current || !window.confirm(`确定删除“${current.name || '未命名模板'}”吗？已创建任务中保存的模板快照不会受影响。`)) return
  settings.script_style_templates = settings.script_style_templates.filter((item) => item.id !== current.id)
  activeScriptStyleId.value = settings.script_style_templates[0]?.id ?? ''
  saveMessage.value = ''
  styleReferenceCheckMessage.value = ''
}

function addStyleReferencePath(): void {
  if (!activeScriptStyle.value) return
  activeScriptStyle.value.reference_paths.push('')
  styleReferenceCheckMessage.value = ''
  checkedStyleReferencePaths.value = []
}

function removeStyleReferencePath(index: number): void {
  activeScriptStyle.value?.reference_paths.splice(index, 1)
  styleReferenceCheckMessage.value = ''
  checkedStyleReferencePaths.value = []
}

async function checkStyleReferences(): Promise<void> {
  const template = activeScriptStyle.value
  const paths = template?.reference_paths.map((item) => item.trim()).filter(Boolean) ?? []
  if (!template || checkingStyleReferences.value) return
  if (!paths.length) {
    styleReferenceCheckMessage.value = '当前模板未关联参考 Markdown。'
    checkedStyleReferencePaths.value = []
    return
  }
  checkingStyleReferences.value = true
  styleReferenceCheckMessage.value = ''
  checkedStyleReferencePaths.value = []
  try {
    const result = await api.readReferenceNotes(paths)
    checkedStyleReferencePaths.value = result.items.map((item) => item.relative_path)
    styleReferenceCheckMessage.value = `已读取并校验 ${result.items.length} 篇参考 Markdown。`
  } catch (error) {
    styleReferenceCheckMessage.value = errorMessage(error)
  } finally {
    checkingStyleReferences.value = false
  }
}

function applyDefaultModelProfile(profile: ModelProfile | null): void {
  settings.model_provider = profile?.provider_id ?? null
  settings.model_id = profile?.model_id ?? null
  settings.asr_model_id = profile?.asr_model_id ?? null
  settings.model_base_url = profile?.model_base_url ?? null
}

function applyProviderOptions(value: unknown): boolean {
  if (!Array.isArray(value)) {
    modelProviders.value = []
    providerCatalogAvailable.value = false
    return false
  }
  modelProviders.value = value as ModelProviderOption[]
  providerCatalogAvailable.value = true
  return true
}

function requiredSettingsMessage(): string {
  saveFieldErrors.archive_root = ''

  if (activeTab.value === 'folders' && !settings.archive_root?.trim()) {
    saveFieldErrors.archive_root = '请选择媒体归档目录。'
    return saveFieldErrors.archive_root
  }
  if (activeTab.value === 'script-styles') {
    const invalid = settings.script_style_templates.find((item) => !item.name.trim() || !item.instructions.trim())
    if (invalid) return '请为每个风格模板填写名称和写作要求后再保存。'
  }
  const retentionDays = retentionDaysInput.value
  if (typeof retentionDays !== 'number' || !Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 3650) {
    return '视频发布文件保留期限请输入 1 到 3650 之间的整数天。'
  }
  return ''
}

function clearSaveValidation(): void {
  saveFieldErrors.archive_root = ''
  saveReminder.value = ''
  formError.value = ''
}

function validateRequiredSettings(): boolean {
  saveReminder.value = requiredSettingsMessage()
  if (!saveReminder.value) return true

  formError.value = saveReminder.value
  if (saveFieldErrors.archive_root) document.querySelector<HTMLButtonElement>('[aria-label="选择媒体归档目录"]')?.focus()
  else document.querySelector<HTMLInputElement>('#video-publish-retention-days')?.focus()
  return false
}

async function loadDiagnostics(): Promise<void> {
  diagnosticsLoading.value = true
  diagnosticsError.value = ''
  try {
    diagnostics.value = await api.getDiagnostics()
  } catch (error) {
    diagnosticsError.value = errorMessage(error)
  } finally {
    diagnosticsLoading.value = false
  }
}

function setSettingsTab(value: unknown): void {
  if (typeof value === 'string' && settingsTabs.includes(value as SettingsTab)) {
    activeTab.value = value as SettingsTab
  }
}

function formatCheckedAt(value: string | null): string {
  if (!value) return '暂无实测时间'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '实测时间未知' : date.toLocaleString('zh-CN')
}

async function loadSettings(): Promise<void> {
  loading.value = true
  formError.value = ''
  const [configResult, diagnosticResult] = await Promise.allSettled([
    api.getConfig(),
    api.getDiagnostics(),
  ])
  if (configResult.status === 'fulfilled') {
    const hasProviderCatalog = applyProviderOptions(configResult.value.model_providers)
    defaultKnowledgeCardPrompt.value = configResult.value.default_knowledge_card_prompt
    applySettings(configResult.value.settings)
    await nextTick()
    directoryConflicts.value = configResult.value.directory_conflicts
    if (!hasProviderCatalog) {
      formError.value = '本机服务版本较旧，尚未提供模型服务商列表。请完全退出并重新启动工作台后重试。'
    }
  } else {
    formError.value = errorMessage(configResult.reason)
  }
  if (diagnosticResult.status === 'fulfilled') diagnostics.value = diagnosticResult.value
  else diagnosticsError.value = errorMessage(diagnosticResult.reason)
  loading.value = false
}

async function saveSettings(): Promise<void> {
  if (saving.value || !validateRequiredSettings()) return
  saving.value = true
  formError.value = ''
  saveReminder.value = ''
  saveMessage.value = ''
  try {
    const result = await api.saveConfig({ ...settings, video_publish_retention_days: retentionDaysInput.value as number })
    const hasProviderCatalog = applyProviderOptions(result.model_providers)
    defaultKnowledgeCardPrompt.value = result.default_knowledge_card_prompt
    applySettings(result.settings)
    directoryConflicts.value = result.directory_conflicts
    if (!hasProviderCatalog) {
      formError.value = '设置已保存，但本机服务版本较旧，尚未提供模型服务商列表。请完全退出并重新启动工作台。'
    }
    const cleanedCount = result.video_publish_media_cleanup?.cleaned_batches || 0
    saveMessage.value = cleanedCount
      ? `设置已保存，并清理了 ${cleanedCount} 条到期任务的本机视频文件。`
      : activeTab.value === 'knowledge' ? '知识卡总结提示词和其他设置已保存。'
        : activeTab.value === 'script-styles' ? '口播稿风格模板已保存。' : '设置已保存。'
    await loadDiagnostics()
  } catch (error) {
    formError.value = errorMessage(error)
    saveReminder.value = formError.value
  } finally {
    saving.value = false
  }
}

async function readReferences(): Promise<void> {
  const paths = referencePaths.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
  if (!paths.length || readingReferences.value) return
  readingReferences.value = true
  referenceMessage.value = ''
  referenceNotes.value = []
  try {
    const result = await api.readReferenceNotes(paths)
    referenceNotes.value = result.items
    referenceMessage.value = result.scope
  } catch (error) {
    referenceMessage.value = errorMessage(error)
  } finally {
    readingReferences.value = false
  }
}

watch(() => route.query.tab, (value) => setSettingsTab(value))
watch(activeTab, () => {
  clearSaveValidation()
})

onMounted(() => {
  setSettingsTab(route.query.tab)
  void loadSettings()
  void overview.loadOverview()
})
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div class="settings-view">
      <p v-if="loading" class="panel panel-pad" role="status">正在读取本机设置…</p>
      <div v-else>
        <el-alert
          v-if="!providerCatalogAvailable"
          class="settings-version-alert"
          type="warning"
          :closable="false"
          show-icon
          role="alert"
        >当前前端与本机服务版本不一致。页面已避免崩溃，但模型服务商选择不可用；请完全退出并重新启动工作台。</el-alert>

        <el-tabs class="settings-tabs" :model-value="activeTab" @update:model-value="setSettingsTab">
          <el-tab-pane :label="tabLabels.folders" name="folders">
            <div class="settings-grid settings-single-column"><div class="stack">
              <section class="panel settings-section" aria-labelledby="archive-root-title">
                <div class="panel-head">
                  <div><h2 id="archive-root-title">本机目录</h2><p>从本机选择文件夹，保存时会检查访问权限，路径只用于本机处理。</p></div>
                </div>

                <span id="archive-root-label" class="form-label settings-field"><span class="required-marker" aria-hidden="true">*</span>媒体归档目录</span>
                <div class="folder-setting">
                  <output id="archive-root" class="folder-path" :class="{ 'is-invalid': Boolean(saveFieldErrors.archive_root) }" :aria-invalid="Boolean(saveFieldErrors.archive_root)" aria-labelledby="archive-root-label" aria-live="polite">{{ settings.archive_root || '尚未选择文件夹' }}</output>
                  <el-button size="small" aria-label="选择媒体归档目录" :disabled="!!selectingFolder || saving" @click="selectFolder('archive_root')">{{ selectingFolder === 'archive_root' ? '选择中…' : '选择文件夹' }}</el-button>
                  <el-button v-if="settings.archive_root" size="small" link type="primary" aria-label="清除媒体归档目录" :disabled="!!selectingFolder || saving" @click="settings.archive_root = null; saveMessage = ''; clearSaveValidation()">清除</el-button>
                </div>
                <p v-if="saveFieldErrors.archive_root" class="required-field-error" role="alert">{{ saveFieldErrors.archive_root }}</p>
                <p class="helper">下载与导入媒体会复制到此目录。选择后请保存设置。</p>

                <div class="settings-divider"></div>
                <span id="obsidian-reference-root-label" class="form-label settings-field">知识库风格参考目录</span>
                <div class="folder-setting">
                  <output id="obsidian-reference-root" class="folder-path" aria-labelledby="obsidian-reference-root-label" aria-live="polite">{{ settings.obsidian_reference_root || '尚未选择文件夹' }}</output>
                  <el-button size="small" aria-label="选择知识库风格参考目录" :disabled="!!selectingFolder || saving" @click="selectFolder('obsidian_reference_root')">{{ selectingFolder === 'obsidian_reference_root' ? '选择中…' : '选择文件夹' }}</el-button>
                  <el-button v-if="settings.obsidian_reference_root" size="small" link type="primary" aria-label="清除知识库风格参考目录" :disabled="!!selectingFolder || saving" @click="settings.obsidian_reference_root = null; saveMessage = ''">清除</el-button>
                </div>
                <p class="helper">工作台只会读取你明确选择的参考范围；该目录必须已存在。</p>

                <span id="obsidian-output-root-label" class="form-label settings-field">知识库输出目录</span>
                <div class="folder-setting">
                  <output id="obsidian-output-root" class="folder-path" aria-labelledby="obsidian-output-root-label" aria-live="polite">{{ settings.obsidian_output_root || '尚未选择文件夹' }}</output>
                  <el-button size="small" aria-label="选择知识库输出目录" :disabled="!!selectingFolder || saving" @click="selectFolder('obsidian_output_root')">{{ selectingFolder === 'obsidian_output_root' ? '选择中…' : '选择文件夹' }}</el-button>
                  <el-button v-if="settings.obsidian_output_root" size="small" link type="primary" aria-label="清除知识库输出目录" :disabled="!!selectingFolder || saving" @click="settings.obsidian_output_root = null; saveMessage = ''">清除</el-button>
                </div>
                <p class="helper">未来生成的 Markdown 只写入该目录；写入前仍会检查冲突。</p>
                <el-alert v-if="folderError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ folderError }}</el-alert>

                <el-collapse class="settings-advanced">
                  <el-collapse-item title="按需读取参考文档" name="reference-paths">
                    <label class="form-label settings-field" for="obsidian-reference-paths">本次要读取的参考 Markdown 相对路径</label>
                    <el-input
                      id="obsidian-reference-paths"
                      v-model="referencePaths"
                      class="reference-paths-input"
                      type="textarea"
                      :rows="3"
                      maxlength="5000"
                      placeholder="每行一个，例如：写作风格/口播范例.md"
                    />
                    <p class="helper">读取范围仅限这里逐项指定的 `.md` 文件；不会遍历整个 Vault。</p>
                    <el-button size="small" :disabled="readingReferences || !referencePaths.trim()" @click="readReferences">
                      {{ readingReferences ? '正在读取…' : '读取选定参考文档' }}
                    </el-button>
                    <p v-if="referenceMessage" class="helper" role="status">{{ referenceMessage }}</p>
                    <el-collapse class="reference-notes">
                      <el-collapse-item v-for="note in referenceNotes" :key="note.relative_path" :name="note.relative_path">
                        <template #title>{{ note.title }} · {{ note.relative_path }}</template>
                        <pre class="reference-note-pre">{{ note.content }}</pre>
                      </el-collapse-item>
                    </el-collapse>
                  </el-collapse-item>
                </el-collapse>

                <el-alert v-if="directoryConflicts.length" class="settings-form-alert" type="warning" :closable="false" show-icon role="status">
                  {{ directoryConflicts.map((item) => item.message).join(' ') }}
                </el-alert>
                <div class="settings-form-actions">
                  <div aria-live="polite">
                    <el-alert v-if="formError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ formError }}</el-alert>
                    <el-alert v-if="saveMessage" class="settings-form-alert" type="success" :closable="false" show-icon role="status">{{ saveMessage }}</el-alert>
                  </div>
                  <div class="form-submit-actions">
                    <el-button type="primary" :disabled="saving || !!selectingFolder" @click="saveSettings">
                      {{ saving ? '正在保存…' : '检查并保存设置' }}
                    </el-button>
                    <p v-if="saveReminder" class="task-submit-reminder" role="alert">{{ saveReminder }}</p>
                  </div>
                </div>
              </section>
            </div></div>
          </el-tab-pane>

          <el-tab-pane :label="tabLabels.models" name="models">
            <div class="settings-grid settings-model-grid"><div class="stack">
              <ModelProfilesPanel :providers="modelProviders" @default-profile-updated="applyDefaultModelProfile" />
            </div>
              <section class="panel settings-section" aria-labelledby="transcription-settings-title">
                <div class="panel-head"><div><h2 id="transcription-settings-title">语音转写</h2></div></div>
                <LocalAsrSettings v-model:device="settings.local_asr_device" />
              </section>
            </div>
          </el-tab-pane>

          <el-tab-pane :label="tabLabels.knowledge" name="knowledge">
            <div class="settings-grid settings-single-column"><div class="stack">
              <section class="panel settings-section" aria-labelledby="knowledge-card-prompt-title">
                <div class="panel-head">
                  <div><h2 id="knowledge-card-prompt-title">知识卡总结提示词</h2><p>生成知识卡时自动使用，可按你的阅读习惯修改；此设置只影响知识卡。</p></div>
                  <el-button link type="primary" :disabled="saving || !defaultKnowledgeCardPrompt" @click="settings.knowledge_card_prompt = defaultKnowledgeCardPrompt; saveMessage = ''">恢复默认</el-button>
                </div>
                <label class="form-label" for="knowledge-card-prompt">总结要求</label>
                <el-input
                  id="knowledge-card-prompt"
                  v-model="settings.knowledge_card_prompt"
                  type="textarea"
                  :rows="7"
                  maxlength="20000"
                  :disabled="saving"
                  aria-describedby="knowledge-card-prompt-help"
                />
            <p id="knowledge-card-prompt-help" class="helper">系统会固定知识卡的输出字段，并要求只根据整篇口播稿总结；知识点数量按文案语义决定。{{ settings.knowledge_card_prompt.length.toLocaleString('zh-CN') }} / 20,000 字符</p>
                <div class="settings-form-actions">
                  <div aria-live="polite">
                    <el-alert v-if="formError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ formError }}</el-alert>
                    <el-alert v-if="saveMessage" class="settings-form-alert" type="success" :closable="false" show-icon role="status">{{ saveMessage }}</el-alert>
                  </div>
                  <div class="form-submit-actions">
                    <el-button type="primary" :disabled="saving" @click="saveSettings">{{ saving ? '正在保存…' : '保存提示词与其他设置' }}</el-button>
                    <p v-if="saveReminder" class="task-submit-reminder" role="alert">{{ saveReminder }}</p>
                  </div>
                </div>
              </section>
            </div></div>
          </el-tab-pane>

          <el-tab-pane :label="tabLabels['script-styles']" name="script-styles">
            <section class="panel settings-section script-style-manager" aria-labelledby="script-style-manager-title">
              <div class="panel-head">
                <div><h2 id="script-style-manager-title">口播稿风格模板</h2><p>模板定义生成表达方式；新稿事实以原视频最终识别文案为准。创建素材时从已启用模板中手动多选。</p></div>
              </div>
              <el-alert class="script-style-info" type="info" :closable="false" show-icon role="note">每个模板可以关联多篇 Markdown 范例。系统只读取模板明确列出的文件；范例只影响写法，不会作为事实来源。参考路径使用“目录与知识库”中设置的知识库风格参考目录。</el-alert>

              <div class="script-style-layout">
                <aside class="script-style-sidebar" aria-label="口播稿风格模板列表">
                  <div class="script-style-sidebar-head"><strong>我的风格模板</strong><el-tag type="info" effect="plain">{{ settings.script_style_templates.length }}</el-tag></div>
                  <el-menu :default-active="activeScriptStyleId" class="script-style-menu" @select="selectScriptStyle">
                    <el-menu-item v-for="template in settings.script_style_templates" :key="template.id" :index="template.id">
                      <span class="script-style-menu-name">{{ template.name || '未命名模板' }}</span>
                      <el-tag size="small" :type="template.enabled ? 'success' : 'info'" effect="plain">{{ template.enabled ? '启用' : '停用' }}</el-tag>
                    </el-menu-item>
                  </el-menu>
                  <el-button class="script-style-add" @click="addScriptStyleTemplate">＋ 新建风格模板</el-button>
                  <p class="helper">停用的模板不会出现在新建素材的选择列表中。已创建任务会保留当时的模板快照。</p>
                </aside>

                <section v-if="activeScriptStyle" class="script-style-detail" aria-label="编辑风格模板">
                  <div class="script-style-detail-head">
                    <div><h3>{{ activeScriptStyle.name || '新建风格模板' }}</h3><p>设置名称、写作要求和风格参考 Markdown。</p></div>
                    <el-switch v-model="activeScriptStyle.enabled" inline-prompt active-text="启用" inactive-text="停用" :disabled="saving" aria-label="启用风格模板" />
                  </div>

                  <div class="script-style-field">
                    <label class="form-label" for="script-style-name">模板名称 <span class="required-marker" aria-hidden="true">*</span></label>
                    <el-input id="script-style-name" v-model="activeScriptStyle.name" maxlength="64" show-word-limit :disabled="saving" placeholder="例如：真实故事叙述" />
                  </div>
                  <div class="script-style-field">
                    <label class="form-label" for="script-style-instructions">写作要求 <span class="required-marker" aria-hidden="true">*</span></label>
                    <el-input
                      id="script-style-instructions"
                      v-model="activeScriptStyle.instructions"
                      type="textarea"
                      :rows="8"
                      maxlength="8000"
                      show-word-limit
                      :disabled="saving"
                      placeholder="描述叙述口吻、节奏、结构、句式等要求；不要在这里写需要模型补充的事实。"
                    />
                  </div>

                  <div class="script-style-field">
                    <div class="script-style-reference-head">
                      <div><span class="form-label">风格参考 Markdown</span><p class="helper">填写相对路径，例如：写作风格/科普讲解样例.md</p></div>
                      <el-button size="small" :disabled="saving" @click="addStyleReferencePath">＋ 添加参考文档</el-button>
                    </div>
                    <div v-if="activeScriptStyle.reference_paths.length" class="script-style-reference-list">
                      <div v-for="(path, index) in activeScriptStyle.reference_paths" :key="`${activeScriptStyle.id}-${index}`" class="script-style-reference-row">
                        <el-input v-model="activeScriptStyle.reference_paths[index]" maxlength="1024" :disabled="saving" placeholder="相对于知识库风格参考目录的 .md 路径" />
                        <el-tag v-if="checkedStyleReferencePaths.includes(path.trim())" type="success" effect="plain">路径有效</el-tag>
                        <el-button link type="danger" :disabled="saving" :aria-label="`移除参考文档 ${path || index + 1}`" @click="removeStyleReferencePath(index)">移除</el-button>
                      </div>
                    </div>
                    <el-empty v-else class="script-style-reference-empty" description="尚未添加参考文档" :image-size="52" />
                    <div class="script-style-reference-actions">
                      <el-button size="small" :disabled="saving || checkingStyleReferences" @click="checkStyleReferences">{{ checkingStyleReferences ? '正在检查…' : '检查参考文件' }}</el-button>
                      <span v-if="styleReferenceCheckMessage" class="helper" role="status">{{ styleReferenceCheckMessage }}</span>
                    </div>
                  </div>

                  <div class="script-style-detail-actions">
                    <el-button type="danger" plain :disabled="saving" @click="removeScriptStyleTemplate">删除模板</el-button>
                    <el-button type="primary" :disabled="saving || !!selectingFolder" @click="saveSettings">{{ saving ? '正在保存…' : '保存风格模板' }}</el-button>
                  </div>
                  <div aria-live="polite">
                    <el-alert v-if="formError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ formError }}</el-alert>
                    <el-alert v-if="saveMessage" class="settings-form-alert" type="success" :closable="false" show-icon role="status">{{ saveMessage }}</el-alert>
                    <p v-if="saveReminder" class="task-submit-reminder" role="alert">{{ saveReminder }}</p>
                  </div>
                </section>
                <el-empty v-else class="script-style-no-selection" description="暂无风格模板，请先新建一个模板。">
                  <el-button type="primary" @click="addScriptStyleTemplate">新建风格模板</el-button>
                </el-empty>
              </div>
            </section>
          </el-tab-pane>

          <el-tab-pane :label="tabLabels.checks" name="checks">
            <div class="settings-grid settings-checks-grid">
              <section class="panel settings-section" aria-labelledby="diagnostics-title">
                <div class="panel-head"><div><h2 id="diagnostics-title">本机运行自检</h2><p>检查本地依赖、数据库、目录和工作台前端。</p></div><el-button link type="primary" :disabled="diagnosticsLoading" @click="loadDiagnostics">{{ diagnosticsLoading ? '检查中…' : '重新检查' }}</el-button></div>
                <DiagnosticsPanel :results="diagnostics" :loading="diagnosticsLoading" :error="diagnosticsError" />
              </section>
              <section class="panel settings-section" aria-labelledby="platform-status-title">
                <div class="panel-head"><div><h2 id="platform-status-title">平台实测记录</h2><p>历史测试结果不代表当前账号登录状态或长期可用性。</p></div><el-button link type="primary" :disabled="overview.state.loading" @click="overview.loadOverview">{{ overview.state.loading ? '读取中…' : '重新读取' }}</el-button></div>
                <el-alert v-if="overview.state.error" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ overview.state.error }}</el-alert>
                <p v-else-if="overview.state.loading" class="helper" role="status">正在读取平台实测记录…</p>
                <ul v-else-if="overview.state.platforms.length" class="platform-status-list" aria-label="平台实测记录">
                  <li v-for="platform in overview.state.platforms" :key="platform.id">
                    <div class="platform-status-head">
                      <span class="platform-status-brand">
                        <PlatformLogo class="platform-status-logo" :platform-id="platform.id" :name="platform.name" />
                        <strong>{{ platform.name }}</strong>
                      </span>
                      <el-tag :type="platform.validation_status === '未验证' ? 'warning' : 'success'">{{ platform.validation_status }}</el-tag>
                    </div>
                    <p>{{ platform.summary || '暂无补充说明' }}</p>
                    <small>{{ formatCheckedAt(platform.checked_at) }}</small>
                  </li>
                </ul>
                <p v-else class="helper">暂无平台实测记录。</p>
              </section>
            </div>
          </el-tab-pane>

          <el-tab-pane :label="tabLabels.other" name="other">
            <div class="settings-grid settings-single-column"><div class="stack">
              <section class="panel settings-section" aria-labelledby="image-storage-title">
                <div class="panel-head">
                  <div><h2 id="image-storage-title">图片保存目录</h2><p>更换目录后，新图片保存到所选位置，已有图片仍从原位置读取。</p></div>
                </div>
                <template v-for="folder in imageFolders" :key="folder.field">
                  <span :id="`${folder.field}-label`" class="form-label settings-field">{{ folder.label }}</span>
                  <div class="folder-setting">
                    <output class="folder-path" :aria-labelledby="`${folder.field}-label`" aria-live="polite">{{ settings[folder.field] || '保存后使用系统默认目录' }}</output>
                    <el-button size="small" :aria-label="`选择${folder.label}`" :disabled="!!selectingFolder || saving" @click="selectFolder(folder.field)">{{ selectingFolder === folder.field ? '选择中…' : '选择文件夹' }}</el-button>
                    <el-button v-if="settings[folder.field]" size="small" link type="primary" :aria-label="`恢复${folder.label}默认值`" :disabled="!!selectingFolder || saving" @click="settings[folder.field] = null; saveMessage = ''; folderError = ''">恢复默认</el-button>
                  </div>
                </template>
                <el-alert v-if="folderError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ folderError }}</el-alert>
              </section>
              <section class="panel settings-section" aria-labelledby="video-publish-cleanup-title">
                <div class="panel-head"><div><h2 id="video-publish-cleanup-title">视频发布文件清理</h2><p>任务记录和原创视频保留；封面按保存时配置的目录清理，旧任务的视频副本也会按规则清理。</p></div></div>
                <el-checkbox v-model="settings.video_publish_auto_cleanup_enabled" :disabled="saving" class="settings-field settings-check">自动清理已到期的视频发布文件</el-checkbox>
                <label class="form-label settings-field" for="video-publish-retention-days">保留期限</label>
                <div class="folder-setting">
                  <el-input-number
                    id="video-publish-retention-days"
                    v-model="retentionDaysInput"
                    :min="1"
                    :max="3650"
                    :step="1"
                    controls-position="right"
                    :disabled="saving"
                    aria-describedby="video-publish-cleanup-help"
                  />
                  <span>天</span>
                </div>
                <p id="video-publish-cleanup-help" class="helper">默认开启，15 天。所有平台结果明确结束后开始计时；排队中、发布中、待确认和结果未知的任务不会清理。到期后清理任务封面和旧任务的视频副本，保留原创视频及历史记录；失败任务清理后需重新选择视频和封面创建任务。已有任务也按其完成时间计算。</p>
                <div class="settings-form-actions">
                  <div aria-live="polite">
                    <el-alert v-if="formError" class="settings-form-alert" type="error" :closable="false" show-icon role="alert">{{ formError }}</el-alert>
                    <el-alert v-if="saveMessage" class="settings-form-alert" type="success" :closable="false" show-icon role="status">{{ saveMessage }}</el-alert>
                  </div>
                  <div class="form-submit-actions">
                    <el-button type="primary" :disabled="saving || !!selectingFolder" @click="saveSettings">{{ saving ? '正在保存…' : '保存其他设置' }}</el-button>
                    <p v-if="saveReminder" class="task-submit-reminder" role="alert">{{ saveReminder }}</p>
                  </div>
                </div>
              </section>
            </div></div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>
  </el-config-provider>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本页(含子面板)的覆盖(全量落地时上移 styles.css)—— */
.settings-view :deep(.panel-head p) { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.settings-view :deep(.helper) { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.settings-view :deep(.form-label) { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }

/* —— Element Plus 主题:令牌对接(弹层在 body 下,另见末尾全局 popper 块)—— */
.settings-view {
  --el-color-primary: var(--pine);
  --el-color-primary-light-3: var(--pine-2);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-color-danger-light-9: var(--danger-soft);
  --el-color-warning: var(--warning);
  --el-color-warning-light-9: var(--warning-soft);
  --el-color-success: var(--success);
  --el-color-success-light-9: var(--success-soft);
  --el-color-info: var(--pine);
  --el-color-info-light-9: var(--pine-soft);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-text-color-placeholder: var(--text-placeholder);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
  --el-border-radius-base: 6px;
}

/* —— 按钮:对齐本页原 .button / .button-small 高度与字号 —— */
.settings-view :deep(.el-button) { height: 36px; margin-left: 0; font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.settings-view :deep(.el-button--small) { height: 32px; padding: 0 11px; }
.settings-view :deep(.el-button.is-link) { height: auto; padding: 0; }

/* —— 页签:保持原下划线式设置页签观感 —— */
.settings-view :deep(.el-tabs__header) { margin: 0 0 16px; }
.settings-view :deep(.el-tabs__nav-wrap::after) { height: 1px; background-color: var(--line); }
.settings-view :deep(.el-tabs__item) { height: 40px; color: var(--text-tertiary); font-size: var(--fs-body); }
.settings-view :deep(.el-tabs__item.is-active) { font-weight: var(--fw-semibold); }
.settings-view :deep(.el-tabs__item:not(.is-active):hover) { color: var(--text-primary); }

/* —— 提示条 el-alert 对齐原 info-box —— */
.settings-view :deep(.el-alert) { padding: 10px 14px; border-radius: 6px; }
.settings-view :deep(.el-alert__description) { margin: 0; color: var(--text-secondary); font-size: var(--fs-body); line-height: var(--lh-base); }
.settings-version-alert { margin-bottom: 14px; }
.settings-form-alert { margin: 0 0 6px; }
.settings-form-alert:last-child { margin-bottom: 0; }

/* —— 折叠面板 el-collapse 对齐原 details/summary —— */
.settings-view :deep(.el-collapse) { --el-collapse-header-height: 34px; border-top: 0; border-bottom: 0; }
.settings-view :deep(.el-collapse-item__header) { border-bottom: 0; background: transparent; color: var(--pine); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.settings-view :deep(.el-collapse-item__wrap) { border-bottom: 0; background: transparent; }
.settings-view :deep(.el-collapse-item__content) { padding-bottom: 4px; color: var(--text-secondary); }
.settings-advanced { margin-top: 16px; }
.settings-view :deep(.reference-notes) { margin-top: 10px; }
.settings-view :deep(.reference-notes .el-collapse-item__header) { color: var(--ink); }
.reference-note-pre { max-height: 360px; overflow: auto; margin: 4px 0 8px; color: var(--muted); font: inherit; font-size: var(--fs-caption); line-height: var(--lh-loose); white-space: pre-wrap; }

/* —— 表单控件补充 —— */
.settings-view :deep(.reference-paths-input .el-textarea__inner) { min-height: 70px; resize: vertical; }
.settings-view :deep(.el-input-number) { width: 150px; }
.settings-view :deep(.settings-check .el-checkbox__label) { color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.script-style-manager { min-width: 0; }
.script-style-info { margin-bottom: 14px; }
.script-style-layout { display: grid; grid-template-columns: minmax(220px, 280px) minmax(0, 1fr); min-width: 0; min-height: 520px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.script-style-sidebar { display: grid; grid-template-rows: auto minmax(0, 1fr) auto auto; align-content: start; gap: 10px; min-width: 0; padding: 14px; border-right: 1px solid var(--line); background: var(--surface); }
.script-style-sidebar-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 2px 2px 5px; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.script-style-menu { min-width: 0; border-right: 0; background: transparent; }
.settings-view :deep(.script-style-menu .el-menu-item) { display: flex; align-items: center; justify-content: space-between; gap: 8px; height: auto; min-height: 48px; margin-bottom: 5px; padding: 9px 10px !important; border-radius: 7px; color: var(--text-primary); line-height: var(--lh-base); white-space: normal; }
.settings-view :deep(.script-style-menu .el-menu-item:hover) { color: var(--text-primary); background: var(--pine-soft); }
.settings-view :deep(.script-style-menu .el-menu-item.is-active) { color: var(--pine); background: var(--pine-soft); }
.script-style-menu-name { flex: 1; min-width: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); line-height: var(--lh-tight); overflow-wrap: anywhere; }
.script-style-add { width: 100%; }
.script-style-detail { display: grid; align-content: start; gap: 18px; min-width: 0; padding: 20px; }
.script-style-detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.script-style-detail-head h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.script-style-detail-head p { margin: 5px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.script-style-field { display: grid; gap: 7px; min-width: 0; }
.script-style-field .form-label { margin: 0; }
.script-style-reference-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.script-style-reference-head .helper { margin: 3px 0 0; }
.script-style-reference-list { display: grid; gap: 8px; }
.script-style-reference-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 8px; min-width: 0; }
.script-style-reference-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.script-style-reference-empty { padding: 0; }
.script-style-reference-empty :deep(.el-empty__description) { margin-top: 6px; font-size: var(--fs-body-sm); }
.script-style-detail-actions { display: flex; justify-content: space-between; gap: 10px; padding-top: 14px; border-top: 1px solid var(--line); }
.script-style-no-selection { align-self: center; }
@media (max-width: 760px) {
  .script-style-layout { grid-template-columns: minmax(0, 1fr); }
  .script-style-sidebar { grid-template-rows: auto minmax(0, auto) auto auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .script-style-menu { max-height: 250px; overflow: auto; }
  .script-style-detail { padding: 15px; }
}
@media (max-width: 520px) {
  .script-style-reference-row { grid-template-columns: minmax(0, 1fr) auto; }
  .script-style-reference-row :deep(.el-tag) { grid-column: 1; justify-self: start; }
  .script-style-reference-row :deep(.el-button) { grid-column: 2; grid-row: 1 / span 2; }
  .script-style-reference-head { align-items: flex-start; flex-direction: column; }
}
</style>

<style>
/* —— 传送门弹层(el-select 下拉 / ElMessageBox)的主题令牌 —— */
.settings-theme-popper,
.settings-profile-remove-confirm {
  --el-color-primary: var(--pine);
  --el-color-primary-light-9: var(--pine-soft);
  --el-color-danger: var(--danger);
  --el-color-danger-light-9: var(--danger-soft);
  --el-color-warning: var(--warning);
  --el-color-warning-light-9: var(--warning-soft);
  --el-color-success: var(--success);
  --el-color-success-light-9: var(--success-soft);
  --el-color-info: var(--pine);
  --el-color-info-light-9: var(--pine-soft);
  --el-text-color-primary: var(--text-primary);
  --el-text-color-regular: var(--text-secondary);
  --el-text-color-placeholder: var(--text-placeholder);
  --el-border-color: var(--line);
  --el-bg-color: var(--paper);
  --el-fill-color-blank: var(--paper);
  --el-font-size-base: var(--fs-body);
}
.settings-profile-remove-confirm .el-message-box__title { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.settings-profile-remove-confirm .el-message-box__message { color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); }
</style>
