<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { ElAlert, ElButton, ElMessageBox, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/tag/style/css'
import { ApiError, api } from '../api/client'
import PlatformLogo from './PlatformLogo.vue'
import LoginProgress from './LoginProgress.vue'
import type { VideoPublishAccount, VideoPublishPlatform, VideoPublishSauStatus } from '../api/types'
import { videoAccountName } from '../videoAccountName'

const props = withDefaults(defineProps<{
  initialPlatform?: string
  platformFilter?: string[]
  embedded?: boolean
  context?: 'video' | 'article'
}>(), {
  platformFilter: () => [],
  embedded: false,
  context: 'video',
})
const emit = defineEmits<{ (event: 'accounts-changed'): void }>()
const platforms = ref<VideoPublishPlatform[]>([])
const accounts = ref<VideoPublishAccount[]>([])
const sau = ref<VideoPublishSauStatus | null>(null)
const selectedPlatform = ref(props.initialPlatform || '')
const loading = ref(false)
const busy = ref('')
const error = ref('')
const message = ref('')
const qrKey = ref(Date.now())
const qrReady = ref<Record<string, boolean>>({})
let timer: number | undefined
let active = false
const currentPlatform = computed(() => platforms.value.find((item) => item.id === selectedPlatform.value))
const currentAccounts = computed(() => accounts.value.filter((item) => item.platform_id === selectedPlatform.value))
const panelDescription = computed(() => {
  if (props.context === 'article') {
    return currentPlatform.value?.id === 'baijiahao'
      ? '登录状态用于确认百家号账号；文章仍在官方编辑页手动发布。'
      : '登录状态用于知乎文章发布。'
  }
  if (['douyin', 'bilibili'].includes(currentPlatform.value?.id || '')) {
    return '登录状态可用于视频发布、素材下载和评论采集。'
  }
  return '登录状态用于视频发布。'
})
const emptyAccountMessage = computed(() => {
  if (props.context !== 'article') return '该平台尚未添加视频发布账号。'
  return currentPlatform.value?.id === 'baijiahao' ? '该平台尚未添加百家号账号。' : '该平台尚未添加知乎账号。'
})
watch(() => props.initialPlatform, (id) => {
  if (id) selectedPlatform.value = id
})
const labels: Record<VideoPublishAccount['status'], string> = {
  not_connected: '未登录', login_in_progress: '等待网页登录', checking: '正在检查', connected: '已连接', expired: '登录失效', error: '暂不可用',
}
const workbenchPlatforms = new Set(['douyin', 'xiaohongshu', 'kuaishou', 'bilibili', 'wechat_channels', 'baijiahao', 'zhihu'])

function errorText(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '平台账号请求失败，请稍后重试。'
}
function statusTagType(status: VideoPublishAccount['status']): 'success' | 'info' | 'warning' | 'danger' {
  if (status === 'connected') return 'success'
  if (status === 'checking' || status === 'login_in_progress') return 'info'
  if (status === 'expired' || status === 'error') return 'danger'
  return 'warning'
}
function replaceAccounts(next: VideoPublishAccount[]): void {
  const state = (items: VideoPublishAccount[]) => JSON.stringify(items.map((item) => [
    item.id, item.status, item.display_name, item.profile_name, item.avatar_url, item.message, item.checked_at,
  ]))
  const changed = state(accounts.value) !== state(next)
  accounts.value = next
  if (changed) emit('accounts-changed')
}
function stopPolling(): void {
  if (timer !== undefined) window.clearInterval(timer)
  timer = undefined
}
function startPolling(): void {
  if (!active || timer !== undefined || !accounts.value.some((item) => item.status === 'checking' || item.status === 'login_in_progress')) return
  timer = window.setInterval(() => {
    if (!accounts.value.some((item) => item.status === 'checking' || item.status === 'login_in_progress')) return stopPolling()
    qrKey.value = Date.now()
    void refreshAccounts()
  }, 3000)
}
async function refreshAccounts(): Promise<void> {
  try {
    const result = await api.getVideoPublishAccounts()
    const pendingIds = new Set(accounts.value.filter((item) => item.status === 'login_in_progress').map((item) => item.id))
    const notice = result.notices?.find((item) => pendingIds.has(item.account_id))
    replaceAccounts(result.items)
    if (notice) { error.value = notice.message; message.value = '' }
    if (!accounts.value.some((item) => item.status === 'login_in_progress' || item.status === 'checking')) message.value = ''
  } catch (cause) { error.value = errorText(cause) }
}
async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  message.value = ''
  const results = await Promise.allSettled([api.getVideoPublishStatus(), api.getVideoPublishPlatforms(props.context === 'article' ? 'article' : 'video'), api.getVideoPublishAccounts()] as const)
  if (results[0].status === 'fulfilled') {
    sau.value = results[0].value.sau
  } else error.value = errorText(results[0].reason)
  if (results[1].status === 'fulfilled') {
    const availablePlatforms = results[1].value.platforms
    platforms.value = props.platformFilter.length
      ? availablePlatforms.filter((item) => props.platformFilter.includes(item.id))
      : availablePlatforms
    if (!platforms.value.some((item) => item.id === selectedPlatform.value)) selectedPlatform.value = platforms.value[0]?.id || ''
  } else if (!error.value) error.value = errorText(results[1].reason)
  if (results[2].status === 'fulfilled') replaceAccounts(results[2].value.items)
  else if (!error.value) error.value = errorText(results[2].reason)
  loading.value = false
  startPolling()
}
defineExpose({ refresh: load })
async function login(account: VideoPublishAccount): Promise<void> {
  if (!platforms.value.find((item) => item.id === account.platform_id)?.login_available) return
  if (account.platform_id === 'wechat_channels') { qrReady.value[account.id] = false; qrKey.value = Date.now() }
  busy.value = account.id; error.value = ''; message.value = ''
  try {
    const updated = await api.loginVideoPublishAccount(account.id)
    replaceAccounts(accounts.value.map((item) => item.id === account.id ? updated : item))
    message.value = ['bilibili', 'wechat_channels'].includes(account.platform_id) ? '正在准备登录二维码，请稍候。' : '正在打开登录窗口，请留意弹出的页面。'
    startPolling()
  } catch (cause) { error.value = errorText(cause); await refreshAccounts() }
  finally { busy.value = '' }
}
async function check(account: VideoPublishAccount): Promise<void> {
  busy.value = account.id; error.value = ''; message.value = ''
  try {
    const updated = await api.checkVideoPublishAccount(account.id)
    replaceAccounts(accounts.value.map((item) => item.id === account.id ? updated : item))
    startPolling()
  } catch (cause) { error.value = errorText(cause) }
  finally { busy.value = '' }
}
async function openWorkbench(account: VideoPublishAccount): Promise<void> {
  busy.value = account.id; error.value = ''; message.value = ''
  try {
    const result = await api.openVideoPublishAccountWorkbench(account.id)
    message.value = result.message
  } catch (cause) { error.value = errorText(cause) }
  finally { busy.value = '' }
}
async function add(): Promise<void> {
  if (busy.value || !sau.value?.configured || !currentPlatform.value?.login_available) return
  busy.value = 'add'; error.value = ''
  try {
    const account = await api.addVideoPublishAccount({ platform_id: selectedPlatform.value })
    replaceAccounts([...accounts.value, account])
    await login(account)
  } catch (cause) { error.value = errorText(cause) }
  finally { busy.value = '' }
}
async function logout(account: VideoPublishAccount): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `退出并移除「${accountName(account)}」？本机登录状态将被清除，平台账号及发布历史会保留。`,
      '退出并移除账号',
      {
        confirmButtonText: '退出并移除',
        cancelButtonText: '取消',
        confirmButtonType: 'danger',
        type: 'warning',
        customClass: 'video-account-logout-confirm',
        customStyle: { width: 'min(480px, calc(100vw - 32px))' },
        closeOnClickModal: false,
      },
    )
  } catch {
    return
  }
  busy.value = account.id; error.value = ''
  try {
    await api.logoutVideoPublishAccount(account.id)
    replaceAccounts(accounts.value.filter((item) => item.id !== account.id))
  } catch (cause) { error.value = errorText(cause) }
  finally { busy.value = '' }
}
function selectPlatform(id: string): void {
  selectedPlatform.value = id
}
function accountName(account: VideoPublishAccount): string {
  return videoAccountName(account)
}
onMounted(() => { active = true; void load() })
onActivated(() => { active = true; if (!loading.value) void load() })
onDeactivated(() => { active = false; stopPolling() })
onBeforeUnmount(() => { active = false; stopPolling() })
</script>

<template>
  <section class="panel settings-section video-accounts" :class="{ 'is-embedded': embedded }" :aria-labelledby="embedded ? undefined : 'video-accounts-title'" :aria-label="embedded ? (context === 'article' ? '知乎登录账号' : '小红书登录账号') : undefined">
    <div v-if="!embedded" class="panel-head"><div><h2 id="video-accounts-title">视频发布账号</h2><p>按平台管理视频发布账号。</p></div><ElButton text class="text-button" native-type="button" :disabled="loading || !!busy" @click="load">{{ loading ? '读取中…' : '刷新状态' }}</ElButton></div>
    <ElAlert v-if="!embedded" class="info-box video-account-note" type="info" :closable="false" :show-icon="false" role="note" title="抖音和 B 站账号可共用于视频发布、素材下载与评论采集；添加账号时无需额外设置用途。" />
    <ElAlert v-if="error" class="info-box form-error" type="error" :closable="false" :show-icon="false" role="alert" :title="error" />
    <ElAlert v-if="message" class="info-box task-success" type="success" :closable="false" :show-icon="false" role="status" :title="message" />
    <p v-if="loading && !platforms.length" role="status">正在读取账号…</p>
    <div v-else class="account-layout" :class="{ 'account-layout-embedded': embedded }">
      <nav v-if="!embedded" class="account-platforms" :aria-label="context === 'article' ? '文章发布平台' : '视频发布平台'">
        <div class="account-platform-heading">
          <strong>发布平台</strong>
          <span>{{ platforms.length }}</span>
        </div>
        <ElButton v-for="platform in platforms" :key="platform.id" native-type="button" :class="{ selected: selectedPlatform === platform.id }" :aria-pressed="selectedPlatform === platform.id" @click="selectPlatform(platform.id)">
          <PlatformLogo class="account-platform-mark" :platform-id="platform.id" :name="platform.name" />
          <span class="account-platform-label">{{ platform.name }}</span>
          <span class="account-platform-summary">{{ accounts.filter(item => item.platform_id === platform.id).length }} 个账号</span>
        </ElButton>
      </nav>
      <div class="account-detail">
        <div class="panel-head"><div v-if="!embedded || context !== 'article'"><h3 class="account-platform-name"><PlatformLogo v-if="currentPlatform" :platform-id="currentPlatform.id" :name="currentPlatform.name" />{{ currentPlatform?.name || '暂无可用平台' }}</h3><p>{{ panelDescription }}</p></div><ElButton v-if="currentPlatform" class="button button-small" native-type="button" :disabled="!sau?.configured || !!busy || !currentPlatform.login_available" @click="add">{{ busy === 'add' ? '正在打开…' : '添加账号' }}</ElButton></div>
        <p v-if="sau && !sau.configured" class="helper" role="status">账号登录功能暂不可用，请联系管理员。</p>
        <p v-if="currentPlatform && !currentPlatform.login_available" class="helper" role="status">该平台账号登录暂不可用，请稍后再试。</p>
        <p v-if="!currentAccounts.length" class="helper">{{ emptyAccountMessage }}</p>
        <ul v-else class="account-list">
          <li v-for="account in currentAccounts" :key="account.id" class="account-item">
            <div class="account-item-head">
              <div class="account-identity">
                <img v-if="account.avatar_url" class="account-avatar" :src="account.avatar_url" alt="" referrerpolicy="no-referrer" />
                <strong>{{ accountName(account) }}</strong>
              </div>
              <ElTag class="account-status" :class="`is-${account.status}`" :type="statusTagType(account.status)" effect="light" size="small">{{ labels[account.status] }}</ElTag>
            </div>
            <div class="account-info">
              <p v-if="account.display_name && account.profile_name && account.display_name !== account.profile_name" class="helper">平台昵称：{{ account.profile_name }}</p>
              <p v-if="account.duplicate_notice" class="account-duplicate" role="status">{{ account.duplicate_notice }}</p>
              <p v-if="account.message" class="helper">{{ account.message }}</p>
              <p v-if="account.checked_at" class="helper">上次检查：{{ new Date(account.checked_at).toLocaleString('zh-CN') }}</p>
            </div>
            <div v-if="account.status === 'login_in_progress'" class="account-qr">
              <img :src="api.videoPublishAccountLoginQrUrl(account.id, qrKey)" alt="账号登录二维码" :class="{ ready: qrReady[account.id] }" @load="qrReady[account.id] = true" @error="qrReady[account.id] = false" />
              <LoginProgress :qr-ready="!!qrReady[account.id]" :in-page-only="['bilibili', 'wechat_channels'].includes(account.platform_id)" :waiting-hint="account.platform_id === 'wechat_channels' ? '正在连接官方登录页并获取二维码；遇到可恢复的加载异常时系统会尝试恢复一次。如仍未显示，可在登录窗口手动重试。' : undefined" />
            </div>
            <div class="account-actions">
              <ElButton v-if="account.status !== 'connected'" class="button button-secondary button-small" native-type="button" :disabled="!!busy || !sau?.configured || !currentPlatform?.login_available || account.status === 'checking' || account.status === 'login_in_progress'" @click="login(account)">{{ account.status === 'login_in_progress' ? '登录中…' : account.status === 'checking' ? '检查中…' : '登录' }}</ElButton>
              <ElButton v-else class="button button-secondary button-small" native-type="button" :disabled="!!busy || !sau?.configured" @click="check(account)">检查</ElButton>
              <ElButton v-if="workbenchPlatforms.has(account.platform_id) && account.status === 'connected'" class="button button-secondary button-small" native-type="button" :disabled="!!busy || !sau?.configured" @click="openWorkbench(account)">{{ busy === account.id ? '正在打开…' : '打开工作台' }}</ElButton>
              <ElButton text class="text-button" native-type="button" :disabled="!!busy || account.status === 'checking' || account.status === 'login_in_progress'" @click="logout(account)">{{ account.status === 'connected' ? '退出登录' : '移除账号' }}</ElButton>
            </div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本面板的覆盖(全量落地时上移 styles.css)—— */
.panel-head h2 { font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.video-accounts :deep(.el-button) { margin-left: 0; font-family: inherit; font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.video-accounts :deep(.el-button.text-button) { color: var(--pine); font-size: var(--fs-body); background: transparent; border-color: transparent; }
.video-accounts :deep(.el-button.text-button:hover:not(.is-disabled)) { color: var(--pine); text-decoration: underline; text-underline-offset: 3px; }
.video-accounts :deep(.el-alert.info-box) { align-items: flex-start; padding: 10px 12px; border-radius: 11px; font-size: var(--fs-body); line-height: var(--lh-base); }
.video-accounts :deep(.el-alert.info-box .el-alert__title) { font-size: inherit; font-weight: var(--fw-regular); line-height: inherit; }
.video-account-note { margin-bottom: 12px; }

.account-layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; min-height: 420px; }
.account-platforms { display: flex; flex-direction: column; gap: 6px; padding: 15px 11px; border-right: 1px solid var(--line); background: var(--paper-2); }
.account-platform-heading { display: flex; justify-content: space-between; align-items: center; margin: 0 5px 8px; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.account-platform-heading > span { min-width: 25px; padding: 2px 7px; border-radius: 99px; background: var(--paper-2); color: var(--text-tertiary); text-align: center; font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.video-accounts :deep(.account-platforms .el-button) { position: relative; display: grid; grid-template-columns: 32px minmax(0, 1fr) auto; align-items: center; gap: 9px; width: 100%; min-width: 0; min-height: 48px; height: auto; margin: 0; padding: 6px 9px; border: 0; border-radius: 9px; background: transparent; color: var(--text-primary); font-family: inherit; font-size: var(--fs-body); font-weight: var(--fw-regular); text-align: left; white-space: normal; }
.video-accounts :deep(.account-platforms .el-button > span) { display: contents; }
.video-accounts :deep(.account-platforms .el-button:hover:not(.is-disabled)) { color: var(--text-primary); background: var(--surface-hover); }
.video-accounts :deep(.account-platforms .el-button.selected) { color: var(--text-selected); background: var(--surface-selected); font-weight: var(--fw-semibold); }
.video-accounts :deep(.account-platforms .el-button.selected::before) { position: absolute; inset: 8px auto 8px 0; width: 3px; border-radius: 4px; background: var(--pine); content: ''; }
.account-platform-mark { width: 32px; height: 32px; display: inline-grid; flex: 0 0 auto; place-items: center; border-radius: 9px; font-size: var(--fs-body-sm); font-weight: var(--fw-bold); }
.account-platform-label { min-width: 0; overflow: hidden; font-size: var(--fs-body); text-overflow: ellipsis; white-space: nowrap; }
.account-platform-summary { max-width: 86px; padding: 3px 6px; overflow: hidden; border-radius: 99px; background: var(--paper); color: var(--text-tertiary); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.account-detail { padding: 22px 24px; min-width: 0; container-type: inline-size; }
.account-detail h3 { margin: 0; font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.account-add { display: grid; gap: 8px; max-width: 480px; padding: 14px; margin-bottom: 14px; background: #f7f9fc; border-radius: 8px; }
.account-add label { display: grid; gap: 5px; }
.account-add small { color: var(--muted); }
.account-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 10px; list-style: none; margin: 0; padding: 0; }
.account-item { display: flex; flex-direction: column; gap: 10px; min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 8px; }
.account-item-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; min-width: 0; }
.account-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.account-identity strong { min-width: 0; overflow-wrap: anywhere; font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.account-avatar { width: 36px; height: 36px; flex: 0 0 36px; border-radius: 50%; object-fit: cover; }
.account-status.el-tag { flex: 0 0 auto; min-height: 24px; padding: 0 9px; border-color: transparent; font-size: var(--fs-caption); font-weight: var(--fw-medium); line-height: 1.4; }
.account-status.el-tag--success { --el-tag-bg-color: var(--success-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--success); }
.account-status.el-tag--warning { --el-tag-bg-color: var(--warning-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--warning); }
.account-status.el-tag--danger { --el-tag-bg-color: var(--danger-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--danger); }
.account-status.el-tag--info { --el-tag-bg-color: var(--paper-2); --el-tag-border-color: transparent; --el-tag-text-color: var(--text-secondary); }
.account-info { min-width: 0; overflow-wrap: anywhere; }
.account-info p { margin: 0 0 5px; }
.account-info p:last-child { margin-bottom: 0; }
.account-info .account-duplicate { color: #a34e42; font-size: 13px; }
.account-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: auto; }
.account-qr { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.account-qr img { display: none; width: 140px; height: 140px; object-fit: contain; }
.account-qr img.ready { display: block; }
.video-accounts.is-embedded { margin: 0; padding: 0; border: 0; background: transparent; box-shadow: none; }
.account-layout-embedded { display: block; min-height: 0; overflow: visible; border: 0; border-radius: 0; }
.account-layout-embedded .account-detail { padding: 0; }
@container (max-width: 780px) { .account-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (max-width: 520px) { .account-list { grid-template-columns: 1fr; } }
@media (max-width: 850px) {
  .account-layout { grid-template-columns: minmax(0, 1fr); }
  .account-platforms { flex-direction: row; align-items: stretch; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .account-platform-heading { display: none; }
  .video-accounts :deep(.account-platforms .el-button) { min-width: 145px; grid-template-columns: 30px minmax(0, 1fr); }
  .account-platform-mark { width: 30px; height: 30px; }
  .account-platform-summary { grid-column: 2; margin-top: -6px; }
}
@media (max-width: 650px) { .account-detail { padding: 17px 13px; } }
</style>
