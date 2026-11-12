<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElAlert, ElButton, ElCollapse, ElCollapseItem, ElDialog, ElOption, ElSelect } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/collapse/style/css'
import 'element-plus/es/components/collapse-item/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/select/style/css'
import { ApiError, api } from '../api/client'
import type { LocalAsrStatus, LocalModelPreview } from '../api/types'
import { localModelAdvice, localModelLabel } from '../utils/localWhisperModels'

const device = defineModel<'auto' | 'cpu' | 'cuda'>('device', { required: true })
const status = ref<LocalAsrStatus | null>(null)
const model = ref('small')
const preview = ref<LocalModelPreview | null>(null)
const busy = ref(false)
const message = ref('')
const managing = ref(false)
const copyMessage = ref('')
const commandField = ref<HTMLTextAreaElement | null>(null)
const installCommand = "& '.\\.venv\\Scripts\\python.exe' -m pip install 'faster-whisper>=1.1,<2' 'opencc-python-reimplemented>=0.1.7,<0.2'"
let timer: ReturnType<typeof setInterval> | undefined
const selected = computed(() => status.value?.models.find((item) => item.id === model.value))
const readyModel = computed(() => status.value?.models.find((item) => item.ready))
const installing = computed(() => status.value?.installation?.state === 'installing')
const downloading = computed(() => status.value?.models.some((item) => item.job?.state === 'downloading') ?? false)
type Stage = 'checking' | 'setup' | 'installing' | 'install-error' | 'choose' | 'downloading' | 'download-error' | 'ready' | 'manage'
const stage = computed<Stage>(() => {
  if (!status.value) return 'checking'
  if (installing.value) return 'installing'
  if (!status.value.installed) return status.value.installation?.state === 'failed' ? 'install-error' : 'setup'
  if (downloading.value) return 'downloading'
  if (managing.value) return 'manage'
  if (readyModel.value) return 'ready'
  return selected.value?.job?.state === 'failed' ? 'download-error' : 'choose'
})
const modelName = (id: string) => localModelLabel(id).split('（')[0]
const stageTitle = computed(() => ({
  checking: '正在检查本机环境', setup: '首次使用需要联网', installing: '正在安装转写组件',
  'install-error': '转写组件安装失败', choose: '下载一个转写模型',
  downloading: '正在下载模型', 'download-error': '模型下载失败',
  ready: `${modelName(readyModel.value?.id || 'small')}已就绪`, manage: '管理转写模型',
})[stage.value])
const stageHelp = computed(() => {
  if (stage.value === 'setup') return '先安装转写组件，再下载一个转写模型。准备好后即可离线使用。'
  if (stage.value === 'installing' || stage.value === 'install-error') return status.value?.installation?.message
  if (stage.value === 'downloading') return status.value?.models.find((item) => item.job?.state === 'downloading')?.job?.message
  if (stage.value === 'download-error') return selected.value?.job?.message
  if (stage.value === 'ready') return '新建任务时，在「语音转文字」中选择「本机离线转写」。'
  return ''
})
const badge = computed(() => ({
  checking: '检查中', setup: '未启用', installing: '准备中', 'install-error': '安装失败',
  choose: '待下载', downloading: '下载中', 'download-error': '下载失败',
  ready: '可离线使用', manage: '可离线使用',
})[stage.value])

async function refresh() {
  try { status.value = await api.getLocalAsrStatus(); message.value = '' }
  catch (error) { message.value = error instanceof ApiError ? error.message : '无法检查本机转写环境。' }
}
async function install() {
  busy.value = true; message.value = ''
  try { await api.installLocalAsr(); await refresh() }
  catch (error) { message.value = error instanceof ApiError ? error.message : '无法开始安装。' }
  finally { busy.value = false }
}
async function inspect() {
  busy.value = true; preview.value = null; message.value = ''
  try { preview.value = await api.previewLocalModel(model.value) }
  catch (error) { message.value = error instanceof ApiError ? error.message : '无法获取模型信息。' }
  finally { busy.value = false }
}
async function prepare() {
  if (!preview.value) return
  busy.value = true; message.value = ''
  try { await api.prepareLocalModel(preview.value.model, preview.value.revision); preview.value = null; await refresh() }
  catch (error) { message.value = error instanceof ApiError ? error.message : '无法开始下载。' }
  finally { busy.value = false }
}
async function copyCommand() {
  try { await navigator.clipboard.writeText(installCommand); copyMessage.value = '已复制' }
  catch { commandField.value?.select(); copyMessage.value = '命令已选中，请按 Ctrl+C 复制' }
}
function startStatusPolling() {
  if (timer) return
  timer = setInterval(() => {
    if (installing.value || downloading.value) void refresh()
  }, 3000)
}
function stopStatusPolling() {
  if (timer) clearInterval(timer)
  timer = undefined
}
watch(model, () => { preview.value = null; message.value = '' })
onMounted(() => { void refresh(); startStatusPolling() })
onActivated(startStatusPolling)
onDeactivated(stopStatusPolling)
onUnmounted(stopStatusPolling)
</script>

<template>
  <div class="local-transcription">
    <div class="local-transcription-head">
      <h3 class="settings-subheading">本机离线转写</h3>
      <span class="local-transcription-badge" :data-stage="stage" role="status">{{ badge }}</span>
    </div>
    <p class="helper">在这台电脑上把语音转成文字，音频不会上传。</p>
    <div class="local-transcription-stage">
      <h4>{{ stageTitle }}</h4>
      <p v-if="stageHelp" class="helper" role="status">{{ stageHelp }}</p>
      <template v-if="stage === 'choose' || stage === 'manage' || stage === 'download-error'">
        <label class="form-label" for="local-model-prepare">转写模型</label>
        <el-select id="local-model-prepare" v-model="model" class="local-asr-select" popper-class="settings-theme-popper">
          <el-option v-for="item in status?.models || []" :key="item.id" :label="`${modelName(item.id)}${item.id === 'small' ? '（推荐）' : ''} · ${item.ready ? '已就绪' : '待下载'}`" :value="item.id" />
        </el-select>
        <p class="helper">{{ selected?.ready ? '此模型已就绪，可在任务中选择。' : localModelAdvice(model) }}</p>
      </template>
      <div class="local-asr-actions">
        <el-button v-if="stage === 'setup' || stage === 'install-error'" type="primary" :loading="busy" @click="install">{{ stage === 'install-error' ? '重新安装' : '启用本机转写' }}</el-button>
        <el-button v-else-if="stage === 'choose' || stage === 'download-error' || stage === 'manage'" v-show="!selected?.ready" type="primary" :loading="busy" @click="inspect">{{ stage === 'download-error' ? '重新下载' : '查看下载信息' }}</el-button>
        <el-button v-if="stage === 'ready'" @click="managing = true">管理模型</el-button>
        <el-button v-if="stage === 'manage'" @click="managing = false">完成</el-button>
        <el-button v-if="stage === 'checking'" @click="refresh">重新检查</el-button>
      </div>
      <el-collapse v-if="stage === 'setup' || stage === 'install-error'" class="settings-advanced">
        <el-collapse-item title="手动安装说明" name="local-asr-manual-install">
          <p class="helper">在工作台安装目录打开 PowerShell，运行以下命令。安装完成后重启工作台。</p>
          <textarea ref="commandField" class="local-asr-command" readonly :value="installCommand" aria-label="手动安装命令" />
          <div class="local-asr-actions"><el-button @click="copyCommand">复制安装命令</el-button><span class="helper" role="status">{{ copyMessage }}</span></div>
        </el-collapse-item>
      </el-collapse>
    </div>
    <el-alert v-if="message" type="error" :closable="false" show-icon role="alert" class="local-asr-error">{{ message }}</el-alert>
    <el-collapse v-if="status?.installed" class="settings-advanced local-asr-advanced">
      <el-collapse-item title="高级设置" name="local-asr-advanced">
        <label class="form-label" for="local-device">计算设备</label>
        <el-select id="local-device" :model-value="device" class="local-asr-select" popper-class="settings-theme-popper" @update:model-value="(value: string) => (device = value as 'auto' | 'cpu' | 'cuda')">
          <el-option label="自动检测" value="auto" />
          <el-option label="CPU" value="cpu" />
          <el-option label="GPU（需兼容环境）" value="cuda" />
        </el-select>
        <p class="helper">设备设置保存后作用于新建任务。GPU 需要兼容驱动和运行库。</p>
        <p class="helper local-asr-path">模型位置：{{ status.model_root }}</p>
      </el-collapse-item>
    </el-collapse>
    <el-dialog :model-value="preview !== null" title="下载转写模型" width="min(430px, 90vw)" @close="preview = null">
      <template v-if="preview">
        <p>{{ modelName(preview.model) }} · 约 {{ (preview.size_bytes / 1024 / 1024).toFixed(1) }} MiB</p>
        <p class="helper local-asr-path">保存到 {{ preview.directory }}。下载时请保持工作台运行。</p>
      </template>
      <template #footer>
        <el-button @click="preview = null">取消</el-button>
        <el-button type="primary" :loading="busy" @click="prepare">确认下载</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.local-transcription-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.local-transcription-head .settings-subheading { margin: 0; }
.local-transcription-badge { padding: 2px 8px; border-radius: 4px; color: var(--text-secondary); background: var(--paper-2); font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.local-transcription-badge[data-stage="installing"], .local-transcription-badge[data-stage="downloading"] { color: var(--pine); background: var(--pine-soft); }
.local-transcription-badge[data-stage="ready"], .local-transcription-badge[data-stage="manage"] { color: var(--success); background: var(--success-soft); }
.local-transcription-badge[data-stage="install-error"], .local-transcription-badge[data-stage="download-error"] { color: var(--danger); background: var(--danger-soft); }
.local-transcription-stage { display: grid; justify-items: start; gap: 8px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); }
.local-transcription-stage h4 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.local-asr-select { max-width: min(100%, 360px); }
.local-asr-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 5px; }
.local-asr-actions .el-button { margin-left: 0; }
.local-asr-command { width: 100%; min-height: 76px; margin-top: 8px; padding: 8px; border: 1px solid var(--line); border-radius: 4px; color: var(--text-secondary); background: var(--paper-2); font-size: var(--fs-body-sm); overflow-wrap: anywhere; resize: vertical; }
.local-asr-path { overflow-wrap: anywhere; }
.local-asr-advanced { margin-top: 14px; border-top: 1px solid var(--line); }
.local-asr-error { margin-top: 10px; }
</style>
