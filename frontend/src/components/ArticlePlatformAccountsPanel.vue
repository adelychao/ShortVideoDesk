<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDialog, ElInput, ElLink, ElMessageBox, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/link/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/tag/style/css'
import { ApiError, api } from '../api/client'
import type { ArticlePlatformAccount, ArticlePlatformStatus } from '../api/types'
import PlatformLogo from './PlatformLogo.vue'
import VideoPublishAccountsPanel from './VideoPublishAccountsPanel.vue'

const platforms = ref<ArticlePlatformStatus[]>([])
const loading = ref(false)
const refreshing = ref(false)
const busyAction = ref('')
const errorMessage = ref('')
const successMessage = ref('')
const selectedPlatformId = ref('wechat')
const accountDialogOpen = ref(false)
const accountFormElement = ref<HTMLFormElement | null>(null)
const dialogPlatformId = ref('')
const editingAccountId = ref('')
const originalPublicValue = ref('')
const secretConfigured = ref(false)
const secretReset = ref(false)
const accountForm = reactive({ accountName: '', publicValue: '', secretValue: '' })
const publicIp = ref('')
const publicIpLoading = ref(false)
const publicIpError = ref('')
const publicIpCopied = ref(false)
const xiaohongshuAccountManager = ref<InstanceType<typeof VideoPublishAccountsPanel> | null>(null)
const zhihuAccountManager = ref<InstanceType<typeof VideoPublishAccountsPanel> | null>(null)
const baijiahaoAccountManager = ref<InstanceType<typeof VideoPublishAccountsPanel> | null>(null)
let toutiaoStatusPoll: number | undefined
let statusRequestId = 0
let platformRevision = 0
let panelMounted = false

const selectedPlatform = computed(() =>
  platforms.value.find((item) => item.id === selectedPlatformId.value) ?? platforms.value[0] ?? null,
)
const dialogPlatform = computed(() =>
  platforms.value.find((item) => item.id === dialogPlatformId.value) ?? null,
)

function syncToutiaoStatusPolling(): void {
  const shouldPoll = selectedPlatformId.value === 'toutiao' && selectedPlatform.value?.login_in_progress
  if (shouldPoll && toutiaoStatusPoll === undefined) {
    toutiaoStatusPoll = window.setInterval(() => {
      if (!loading.value && !refreshing.value && !busyAction.value) void loadPlatforms()
    }, 2000)
  } else if (!shouldPoll && toutiaoStatusPoll !== undefined) {
    window.clearInterval(toutiaoStatusPoll)
    toutiaoStatusPoll = undefined
  }
}

watch(
  [selectedPlatformId, () => selectedPlatform.value?.login_in_progress ?? false],
  syncToutiaoStatusPolling,
  { immediate: true },
)

watch(selectedPlatformId, (platformId, previousPlatformId) => {
  if (platformId === 'toutiao' && previousPlatformId !== 'toutiao' && panelMounted) {
    void loadPlatforms()
  }
})

onUnmounted(() => {
  panelMounted = false
  statusRequestId += 1
  if (toutiaoStatusPoll !== undefined) window.clearInterval(toutiaoStatusPoll)
})
const isWechatDialog = computed(() => dialogPlatformId.value === 'wechat')
const isEditing = computed(() => Boolean(editingAccountId.value))
const publicValueChanged = computed(() => accountForm.publicValue.trim() !== originalPublicValue.value)
const accountNameRequired = computed(() => dialogPlatformId.value === 'wechat' || dialogPlatformId.value === 'zhihu')
const showSecretInput = computed(() =>
  !isEditing.value || !secretConfigured.value || secretReset.value || publicValueChanged.value,
)
const secretRequired = computed(() => showSecretInput.value)
const dialogTitle = computed(() => {
  const platformName = dialogPlatform.value?.name ?? '发布平台'
  return (isEditing.value ? '编辑' : '添加') + platformName + '账号'
})
const submitLabel = computed(() => {
  if (busyAction.value === 'save') return '正在验证并保存…'
  if (isWechatDialog.value) return isEditing.value ? '验证并保存' : '验证并添加公众号'
  return isEditing.value ? '保存账号修改' : '添加平台账号'
})

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : '本机服务暂时无法完成请求，请稍后重试。'
}

async function confirmAction(message: string, title: string, confirmButtonText: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText,
      cancelButtonText: '取消',
      confirmButtonType: 'danger',
      type: 'warning',
      customClass: 'publishing-account-confirm',
      customStyle: { width: 'min(480px, calc(100vw - 32px))' },
      closeOnClickModal: false,
    })
    return true
  } catch {
    return false
  }
}

function platformSummary(platform: ArticlePlatformStatus): string {
  if (platform.id === 'wechat' || platform.id === 'zhihu') {
    return platform.accounts.length ? platform.accounts.length + ' 个账号' : '未添加'
  }
  if (platform.id === 'csdn' || platform.id === 'toutiao') {
    if (platform.connection_checking) return '检查中'
    if (platform.login_in_progress) return '登录中'
    if (platform.connected) return '已登录'
    return platform.connection_checked === false ? '状态异常' : '未登录'
  }
  if (['xiaohongshu', 'baijiahao'].includes(platform.id)) {
    const connected = platform.accounts.filter((account) => account.configured).length
    return connected ? connected + ' 个已连接' : platform.accounts.length ? '待连接' : '未添加'
  }
  if (platform.id === 'weibo') {
    if (!platform.cli_installed) return '需安装 CLI'
    if (platform.login_in_progress) return '授权中'
    if (platform.connection_checking) return '检查中'
    if (!platform.connection_checked) return '连接状态待确认'
    if (!platform.connected) return '未登录'
    if (!platform.readiness_checked) return '发布准备待确认'
    return platform.publish_ready ? '可发布' : '需开通服务'
  }
  return platform.mode === 'manual' ? '手动发布' : platform.connected ? '已连接' : '未连接'
}

function accountStatusLabel(account: ArticlePlatformAccount): string {
  if (account.auth_type === 'browser') {
    return account.status === 'connected' ? '已连接' : account.status === 'expired' ? '登录失效' : '未登录'
  }
  if (!account.configured) return '凭据不完整'
  if (account.credential_verified) return '凭据已验证'
  return '待验证'
}

function replacePlatformStatus(status: ArticlePlatformStatus): void {
  platformRevision += 1
  platforms.value = platforms.value.map((item) => item.id === status.id ? status : item)
}

async function loadPlatforms(options: { includeLiveStatus?: boolean; initial?: boolean } = {}): Promise<boolean> {
  const includeLiveStatus = options.includeLiveStatus ?? true
  const initial = options.initial ?? false
  const requestId = ++statusRequestId
  const revisionAtRequest = platformRevision
  if (initial) loading.value = true
  else refreshing.value = true
  errorMessage.value = ''
  try {
    const response = await api.getArticlePlatforms(includeLiveStatus)
    if (requestId !== statusRequestId || revisionAtRequest !== platformRevision) return false
    if (includeLiveStatus && selectedPlatformId.value === 'toutiao') {
      const toutiao = response.platforms.find((item) => item.id === 'toutiao')
      const account = toutiao?.accounts[0]
      if (toutiao?.connected && account && (!account.avatar_url || account.name === '当前今日头条账号')) {
        try {
          const profile = await api.ensureToutiaoProfile()
          if (profile.authenticated) {
            toutiao.accounts = [{
              ...account,
              name: profile.profile_name || account.name,
              avatar_url: profile.profile_avatar_url || account.avatar_url,
            }]
          }
        } catch {
          // Account metadata is optional; a profile lookup failure must not block login status.
        }
        if (requestId !== statusRequestId || revisionAtRequest !== platformRevision) return false
      }
    }
    platforms.value = response.platforms
    if (!platforms.value.some((item) => item.id === selectedPlatformId.value)) {
      selectedPlatformId.value = platforms.value[0]?.id ?? 'wechat'
    }
    return true
  } catch (error) {
    if (requestId === statusRequestId && revisionAtRequest === platformRevision) {
      errorMessage.value = messageFor(error)
      platforms.value = platforms.value.map((item) => item.connection_checking
        ? { ...item, connection_checking: false, message: '未能确认账号登录状态，请点击刷新重试。' }
        : item)
    }
    return false
  } finally {
    if (initial) loading.value = false
    if (!initial && requestId === statusRequestId) refreshing.value = false
  }
}

async function loadInitialPlatforms(): Promise<void> {
  const loaded = await loadPlatforms({ includeLiveStatus: false, initial: true })
  if (loaded && panelMounted) void loadPlatforms()
}

function reloadPlatforms(): void {
  void loadPlatforms()
}

async function refreshStatuses(): Promise<void> {
  const refreshes: Promise<unknown>[] = [loadPlatforms()]
  if (selectedPlatformId.value === 'xiaohongshu') {
    refreshes.push(xiaohongshuAccountManager.value?.refresh() ?? Promise.resolve())
  }
  if (selectedPlatformId.value === 'zhihu') {
    refreshes.push(zhihuAccountManager.value?.refresh() ?? Promise.resolve())
  }
  if (selectedPlatformId.value === 'baijiahao') {
    refreshes.push(baijiahaoAccountManager.value?.refresh() ?? Promise.resolve())
  }
  await Promise.all(refreshes)
}

async function loginWeibo(): Promise<void> {
  if (busyAction.value) return
  busyAction.value = 'weibo_login'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const result = await api.startWeiboLogin()
    successMessage.value = result.message
    await loadPlatforms()
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

async function openCsdnEditor(): Promise<void> {
  if (busyAction.value) return
  busyAction.value = 'csdn_editor'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const status = await api.openCsdnEditor()
    successMessage.value = status.message
    await loadPlatforms()
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

async function clearCsdnSession(): Promise<void> {
  if (busyAction.value) return
  if (!await confirmAction('确定清除本机保存的 CSDN 登录状态？', '清除 CSDN 登录状态', '清除')) return
  busyAction.value = 'csdn_clear'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await api.clearCsdnSession()
    await loadPlatforms()
    successMessage.value = '本机 CSDN 登录状态已清除。'
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

async function openToutiaoEditor(): Promise<void> {
  if (busyAction.value) return
  busyAction.value = 'toutiao_editor'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const status = await api.openToutiaoEditor()
    successMessage.value = status.message
    await loadPlatforms()
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

async function clearToutiaoSession(): Promise<void> {
  if (busyAction.value) return
  if (!await confirmAction('确定清除本机保存的今日头条登录状态？', '清除今日头条登录状态', '清除')) return
  busyAction.value = 'toutiao_clear'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await api.clearToutiaoSession()
    await loadPlatforms()
    successMessage.value = '本机今日头条登录状态已清除。'
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

function resetForm(): void {
  accountForm.accountName = ''
  accountForm.publicValue = ''
  accountForm.secretValue = ''
  originalPublicValue.value = ''
  secretConfigured.value = false
  secretReset.value = false
  editingAccountId.value = ''
  publicIp.value = ''
  publicIpError.value = ''
  publicIpCopied.value = false
}

async function showAccountDialog(platformId: string, account?: ArticlePlatformAccount): Promise<void> {
  if (busyAction.value) return
  selectedPlatformId.value = platformId
  dialogPlatformId.value = platformId
  errorMessage.value = ''
  successMessage.value = ''
  resetForm()
  if (account) {
    editingAccountId.value = account.id
    accountForm.accountName = account.name
    accountForm.publicValue = account.public_value
    originalPublicValue.value = account.public_value
    secretConfigured.value = account.configured
  }
  accountDialogOpen.value = true
  await nextTick()
  accountFormElement.value?.querySelector<HTMLInputElement>('input:not([type="password"])')?.focus()
  if (platformId === 'wechat') void detectPublicIp()
}

async function detectPublicIp(): Promise<void> {
  if (publicIpLoading.value) return
  publicIpLoading.value = true
  publicIpError.value = ''
  publicIpCopied.value = false
  try {
    const result = await api.getPublicIpv4()
    publicIp.value = result.ip
  } catch (error) {
    publicIp.value = ''
    publicIpError.value = messageFor(error)
  } finally {
    publicIpLoading.value = false
  }
}

async function copyPublicIp(): Promise<void> {
  if (!publicIp.value) return
  try {
    await navigator.clipboard.writeText(publicIp.value)
    publicIpCopied.value = true
    window.setTimeout(() => { publicIpCopied.value = false }, 1800)
  } catch {
    publicIpError.value = '复制失败，请手动选中并复制上方 IP。'
  }
}

async function resetSecretInput(): Promise<void> {
  errorMessage.value = ''
  secretReset.value = true
  await nextTick()
  document.getElementById(dialogPlatformId.value === 'wechat' ? 'wechat-account-secret' : 'zhihu-account-secret')?.focus()
}

function cancelSecretReset(): void {
  errorMessage.value = ''
  secretReset.value = false
  accountForm.secretValue = ''
}

function closeAccountDialog(): void {
  if (busyAction.value) return
  accountDialogOpen.value = false
  errorMessage.value = ''
  resetForm()
}

function beforeCloseAccountDialog(done: (cancel?: boolean) => void): void {
  if (!busyAction.value) done()
}

function handleAccountDialogClosed(): void {
  if (busyAction.value) return
  errorMessage.value = ''
  resetForm()
}

async function saveAccount(): Promise<void> {
  if (busyAction.value || !accountFormElement.value?.reportValidity()) return
  if (secretRequired.value && !accountForm.secretValue.trim()) {
    errorMessage.value = isWechatDialog.value ? '请输入新的 AppSecret。' : '请输入新的 App Secret。'
    accountFormElement.value.querySelector<HTMLInputElement>('input[type="password"]')?.focus()
    return
  }
  const platformId = dialogPlatformId.value
  const platformName = dialogPlatform.value?.name ?? '平台'
  busyAction.value = 'save'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const status = editingAccountId.value
      ? await api.updateArticlePlatformAccount(
          platformId,
          editingAccountId.value,
          accountForm.accountName.trim(),
          accountForm.publicValue.trim(),
          accountForm.secretValue,
        )
      : await api.addArticlePlatformAccount(
          platformId,
          accountForm.accountName.trim(),
          accountForm.publicValue.trim(),
          accountForm.secretValue,
        )
    replacePlatformStatus(status)
    const savedName = status.accounts.find((item) => item.id === editingAccountId.value)?.name
    successMessage.value = isWechatDialog.value
      ? (savedName || accountForm.accountName || '公众号') + '的 AppID 与 AppSecret 已通过接口验证。'
      : platformName + '账号凭据已保存。'
    closeAccountDialogAfterSave()
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

function closeAccountDialogAfterSave(): void {
  accountDialogOpen.value = false
  resetForm()
}

async function verifyAccount(account: ArticlePlatformAccount): Promise<void> {
  if (busyAction.value) return
  busyAction.value = 'verify:' + account.id
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const status = await api.verifyWechatAccountCredentials(account.id)
    replacePlatformStatus(status)
    successMessage.value = account.name + '的 AppID 与 AppSecret 已通过微信接口验证。'
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

async function removeAccount(platform: ArticlePlatformStatus, account: ArticlePlatformAccount): Promise<void> {
  if (busyAction.value) return
  const credentialLabel = platform.id === 'wechat' ? '及其本机保存的 AppSecret' : '及其本机保存的 App Secret'
  if (!await confirmAction('确定移除「' + account.name + '」' + credentialLabel + '？历史发布记录会保留。', '移除发布账号', '移除')) return
  busyAction.value = 'remove:' + account.id
  errorMessage.value = ''
  successMessage.value = ''
  try {
    replacePlatformStatus(await api.removeArticlePlatformAccount(platform.id, account.id))
    successMessage.value = platform.name + '账号「' + account.name + '」已移除。'
  } catch (error) {
    errorMessage.value = messageFor(error)
  } finally {
    busyAction.value = ''
  }
}

onMounted(() => {
  panelMounted = true
  void loadInitialPlatforms()
})
</script>

<template>
  <section class="panel settings-section publishing-accounts" aria-labelledby="article-platform-account-title">
    <div class="panel-head">
      <div>
        <h2 id="article-platform-account-title">文章发布账号</h2>
        <p>按平台管理发布账号。敏感密钥保存在 Windows 凭据管理器，不会回显到页面。</p>
      </div>
      <ElButton text class="text-button" native-type="button" :disabled="loading || refreshing || Boolean(busyAction)" @click="refreshStatuses">
        {{ loading ? '读取中…' : refreshing ? '检查中…' : '刷新状态' }}
      </ElButton>
    </div>

    <ElAlert class="info-box publishing-account-note" type="info" :closable="false" :show-icon="false" role="note" title="CSDN 和头条号使用本机隔离浏览器档案；知乎、百家号和小红书复用 SAU 登录会话。公众号凭据通过微信接口验证。" />
    <ElAlert v-if="errorMessage" class="info-box form-error" type="error" :closable="false" :show-icon="false" role="alert" :title="errorMessage" />
    <ElAlert v-if="successMessage" class="info-box task-success" type="success" :closable="false" :show-icon="false" role="status" :title="successMessage" />

    <div v-if="loading && !platforms.length" class="publishing-loading" role="status">正在读取发布账号状态…</div>

    <div v-else class="publishing-layout">
      <nav class="publishing-platform-nav" aria-label="发布平台">
        <div class="publishing-nav-heading">
          <strong>发布平台</strong>
          <span>{{ platforms.length }}</span>
        </div>
        <ElButton
          v-for="platform in platforms"
          :key="platform.id"
          class="publishing-platform-tab"
          :class="{ 'is-active': selectedPlatform?.id === platform.id }"
          native-type="button"
          :aria-current="selectedPlatform?.id === platform.id ? 'page' : undefined"
          @click="selectedPlatformId = platform.id"
        >
          <PlatformLogo class="publishing-platform-mark" :platform-id="platform.id" :name="platform.name" />
          <span class="publishing-platform-name">{{ platform.name }}</span>
          <span class="publishing-platform-summary">{{ platformSummary(platform) }}</span>
        </ElButton>
        <div class="publishing-nav-foot">
          <strong>账号安全</strong>
          <p>AppSecret 只保存在本机 Windows 凭据管理器。发布请求由本机后端发送。</p>
        </div>
      </nav>

      <section v-if="selectedPlatform" class="publishing-platform-workspace" :aria-labelledby="'publishing-platform-' + selectedPlatform.id">
        <header class="publishing-platform-header">
          <div class="publishing-platform-title">
            <PlatformLogo class="publishing-platform-mark publishing-platform-mark-large" :platform-id="selectedPlatform.id" :name="selectedPlatform.name" />
            <div>
              <h3 :id="'publishing-platform-' + selectedPlatform.id">{{ selectedPlatform.name }}</h3>
              <p>
                {{ selectedPlatform.message }}
                <span v-if="selectedPlatform.id === 'csdn'">CSDN 目前仅支持绑定一个账号。</span>
                <span v-if="selectedPlatform.id === 'toutiao'">今日头条目前最多支持绑定一个账号。</span>
              </p>
            </div>
          </div>
          <ElButton
            v-if="selectedPlatform.id === 'wechat'"
            class="button publishing-add-button"
            native-type="button"
            :disabled="Boolean(busyAction)"
            @click="showAccountDialog(selectedPlatform.id)"
          >
            <span aria-hidden="true">＋</span>
            添加公众号
          </ElButton>
        </header>

        <template v-if="selectedPlatform.id === 'wechat'">
          <div class="publishing-list-heading">
            <div>
              <strong>已添加账号</strong>
              <span class="publishing-count">{{ selectedPlatform.accounts.length }}</span>
            </div>
            <span>账号资料仅在本机显示</span>
          </div>

          <ul v-if="selectedPlatform.accounts.length" class="publishing-account-list" :aria-label="selectedPlatform.name + '已添加账号'">
            <li v-for="account in selectedPlatform.accounts" :key="account.id" class="publishing-account-card">
              <div class="publishing-account-head">
                <div class="publishing-account-identity">
                  <div class="publishing-account-avatar">
                    <PlatformLogo class="publishing-platform-mark publishing-platform-mark-avatar" :platform-id="selectedPlatform.id" :name="selectedPlatform.name" />
                  </div>
                  <strong>{{ account.name }}</strong>
                </div>
                <ElTag class="status-badge" :type="account.credential_verified ? 'success' : 'warning'" effect="light" size="small">
                  {{ accountStatusLabel(account) }}
                </ElTag>
              </div>
              <div class="publishing-account-info">
                <p class="publishing-account-meta">AppID：<code>{{ account.public_value }}</code></p>
                <p v-if="account.credential_verified_at" class="publishing-account-meta">上次验证：{{ new Date(account.credential_verified_at).toLocaleString('zh-CN') }}</p>
                <p v-if="selectedPlatform.id === 'wechat'" class="publishing-account-hint">公众号头像需第三方授权能力；当前直连接口不提供自动读取。</p>
              </div>
              <div class="publishing-account-actions">
                <ElButton
                  v-if="selectedPlatform.id === 'wechat'"
                  class="button button-secondary button-small"
                  native-type="button"
                  :disabled="Boolean(busyAction) || !account.configured"
                  @click="verifyAccount(account)"
                >
                  {{ busyAction === 'verify:' + account.id ? '验证中…' : '验证凭据' }}
                </ElButton>
                <ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(busyAction)" @click="showAccountDialog(selectedPlatform.id, account)">编辑</ElButton>
                <ElButton text class="text-button publishing-remove-button" native-type="button" :disabled="Boolean(busyAction)" @click="removeAccount(selectedPlatform, account)">移除</ElButton>
              </div>
            </li>
          </ul>

          <div v-else class="publishing-empty-state">
            <PlatformLogo class="publishing-platform-mark publishing-platform-mark-empty" :platform-id="selectedPlatform.id" :name="selectedPlatform.name" />
            <strong>还没有添加公众号</strong>
            <p>填写 AppID 与 AppSecret 后，系统会验证调用凭据。当前直连接口不会读取公众号头像。</p>
            <ElButton class="button button-small" native-type="button" :disabled="Boolean(busyAction)" @click="showAccountDialog(selectedPlatform.id)">
              添加公众号
            </ElButton>
          </div>
        </template>

        <VideoPublishAccountsPanel
          v-else-if="selectedPlatform.id === 'zhihu'"
          ref="zhihuAccountManager"
          embedded
          context="article"
          initial-platform="zhihu"
          :platform-filter="['zhihu']"
          @accounts-changed="reloadPlatforms"
        />

        <div v-else-if="selectedPlatform.id === 'csdn'" class="publishing-csdn-account">
          <ul v-if="selectedPlatform.connected && selectedPlatform.accounts[0]" class="publishing-account-list publishing-account-list--single" aria-label="CSDN 已连接账号">
            <li class="publishing-account-card">
              <div class="publishing-account-head">
                <div class="publishing-account-identity">
                  <div class="publishing-account-avatar publishing-account-avatar--round">
                    <img
                      v-if="selectedPlatform.accounts[0].avatar_url"
                      :src="selectedPlatform.accounts[0].avatar_url"
                      :alt="selectedPlatform.accounts[0].name + ' 头像'"
                      referrerpolicy="no-referrer"
                      decoding="async"
                    >
                    <PlatformLogo v-else class="publishing-platform-mark publishing-platform-mark-avatar" :platform-id="selectedPlatform.id" :name="selectedPlatform.name" />
                  </div>
                  <strong>{{ selectedPlatform.accounts[0].name }}</strong>
                </div>
                <ElTag class="status-badge" type="success" effect="light" size="small">已连接</ElTag>
              </div>
              <div class="publishing-account-info">
                <p v-if="selectedPlatform.accounts[0].public_value" class="publishing-account-meta">CSDN ID：<code>{{ selectedPlatform.accounts[0].public_value }}</code></p>
                <p class="publishing-account-meta">{{ selectedPlatform.message }}</p>
                <p class="publishing-account-meta">目前使用一个本机登录档案；更换账号前请先清除登录状态。</p>
              </div>
              <div class="publishing-account-actions">
                <ElButton class="button button-small" native-type="button" :disabled="Boolean(busyAction) || selectedPlatform.login_in_progress" @click="openCsdnEditor">
                  {{ busyAction === 'csdn_editor' ? '正在打开…' : '打开工作台' }}
                </ElButton>
                <ElButton class="button button-secondary button-small" native-type="button" :disabled="loading || refreshing || Boolean(busyAction)" @click="refreshStatuses">
                  {{ loading || refreshing ? '检查中…' : '检查连接' }}
                </ElButton>
                <ElButton text class="text-button" native-type="button" :disabled="Boolean(busyAction)" @click="clearCsdnSession">
                  {{ busyAction === 'csdn_clear' ? '正在清除…' : '清除登录状态' }}
                </ElButton>
              </div>
            </li>
          </ul>
          <div v-else class="publishing-empty-state">
            <PlatformLogo class="publishing-platform-mark publishing-platform-mark-empty" :platform-id="selectedPlatform.id" :name="selectedPlatform.name" />
            <strong>{{ selectedPlatform.connection_checking ? '正在检查 CSDN 登录状态' : selectedPlatform.login_in_progress ? 'CSDN 登录窗口已打开' : '还没有连接 CSDN 账号' }}</strong>
            <p>{{ selectedPlatform.message }}</p>
            <p>编辑器会在工作台单独的浏览器窗口打开。完成登录后可在同一窗口手动发布。</p>
            <div class="publishing-account-actions">
              <ElButton class="button button-small" native-type="button" :disabled="Boolean(busyAction) || selectedPlatform.login_in_progress" @click="openCsdnEditor">
                {{ busyAction === 'csdn_editor' ? '正在打开…' : '打开工作台并登录' }}
              </ElButton>
              <ElButton class="button button-secondary button-small" native-type="button" :disabled="loading || refreshing || Boolean(busyAction)" @click="refreshStatuses">
                {{ loading || refreshing ? '检查中…' : '检查连接' }}
              </ElButton>
              <ElButton text v-if="selectedPlatform.login_in_progress" class="text-button" native-type="button" :disabled="Boolean(busyAction)" @click="clearCsdnSession">
                {{ busyAction === 'csdn_clear' ? '正在清除…' : '清除登录状态' }}
              </ElButton>
            </div>
          </div>
        </div>

        <ul v-else-if="selectedPlatform.id === 'toutiao'" class="publishing-account-list toutiao-account-list">
          <li class="publishing-account-card">
            <div class="publishing-account-head">
              <div class="publishing-account-identity">
                <div class="publishing-account-avatar">
                  <img
                    v-if="selectedPlatform.connected && selectedPlatform.accounts[0]?.avatar_url"
                    :src="selectedPlatform.accounts[0].avatar_url"
                    :alt="`${selectedPlatform.accounts[0].name} 头像`"
                    referrerpolicy="no-referrer"
                    decoding="async"
                  >
                  <PlatformLogo
                    v-else
                    class="publishing-platform-mark publishing-platform-mark-avatar"
                    platform-id="toutiao"
                    name="今日头条"
                  />
                </div>
                <div class="publishing-account-info">
                  <strong>{{ selectedPlatform.connected ? selectedPlatform.accounts[0]?.name || '当前今日头条账号' : '今日头条账号' }}</strong>
                  <span>{{ selectedPlatform.connected ? '头条号创作账号' : '登录后显示账号昵称和头像' }}</span>
                </div>
              </div>
              <ElTag class="status-badge" :type="selectedPlatform.connected ? 'success' : selectedPlatform.connection_checking || selectedPlatform.login_in_progress ? 'info' : selectedPlatform.connection_checked === false ? 'danger' : 'warning'" effect="light" size="small">
                {{ selectedPlatform.connection_checking ? '检查中' : selectedPlatform.connected ? '已登录' : selectedPlatform.login_in_progress ? '登录中' : selectedPlatform.connection_checked === false ? '状态异常' : '未登录' }}
              </ElTag>
            </div>
            <p class="publishing-account-meta">{{ selectedPlatform.message }}</p>
            <p class="publishing-account-meta">自动发布会复用这个工作台账号；需要人工复核时，也可在同一工作台页面操作。</p>
            <div class="publishing-account-actions">
              <ElButton class="button button-small" native-type="button" :disabled="Boolean(busyAction) || selectedPlatform.login_in_progress" @click="openToutiaoEditor">
                {{ busyAction === 'toutiao_editor' ? '正在打开…' : selectedPlatform.connected ? '打开工作台' : '打开工作台并登录' }}
              </ElButton>
              <ElButton class="button button-secondary button-small" native-type="button" :disabled="loading || refreshing || Boolean(busyAction)" @click="refreshStatuses">
                {{ loading || refreshing ? '检查中…' : '检查连接' }}
              </ElButton>
              <ElButton text v-if="selectedPlatform.connected || selectedPlatform.login_in_progress" class="text-button" native-type="button" :disabled="Boolean(busyAction)" @click="clearToutiaoSession">
                {{ busyAction === 'toutiao_clear' ? '正在清除…' : '清除登录状态' }}
              </ElButton>
            </div>
          </li>
        </ul>

        <div v-else-if="selectedPlatform.id === 'weibo'" class="publishing-platform-empty">
          <div class="weibo-connection-heading">
            <strong>{{ selectedPlatform.connection_checking ? '正在检查微博账号状态' : selectedPlatform.connected ? `当前账号：${selectedPlatform.name || '微博账号'}` : selectedPlatform.cli_installed ? '连接微博账号' : '安装微博官方 CLI' }}</strong>
            <ElTag class="status-badge" :type="selectedPlatform.publish_ready ? 'success' : selectedPlatform.login_in_progress || selectedPlatform.connection_checking || !selectedPlatform.connection_checked ? 'info' : 'warning'" effect="light" size="small">
              {{ selectedPlatform.login_in_progress ? '授权中' : selectedPlatform.connection_checking ? '检查中' : selectedPlatform.publish_ready ? '可发布' : !selectedPlatform.connection_checked ? '连接待确认' : !selectedPlatform.connected ? '未登录' : !selectedPlatform.readiness_checked ? '准备状态待确认' : '待开通服务' }}
            </ElTag>
          </div>
          <p>{{ selectedPlatform.message }}</p>
          <p v-if="selectedPlatform.cli_installed" class="weibo-readiness-list">
            登录 {{ selectedPlatform.connection_checked ? (selectedPlatform.connected ? '完成' : '未连接') : '状态待确认' }} · 开发者认证 {{ selectedPlatform.readiness_checked ? (selectedPlatform.developer_verified ? '完成' : '待完成') : '状态待确认' }} · 服务开通 {{ selectedPlatform.readiness_checked ? (selectedPlatform.service_ready ? '完成' : '待完成') : '状态待确认' }}
          </p>
          <p v-else class="weibo-install-instruction">需要 Node.js 18 或更高版本，然后在终端运行 <code>npm install -g @weibo-ai/weibo-cli</code>。工作台不会自动安装 CLI。</p>
          <div class="weibo-account-actions">
            <ElButton v-if="selectedPlatform.cli_installed" class="button button-small" native-type="button" :disabled="Boolean(busyAction) || selectedPlatform.login_in_progress" @click="loginWeibo">
              {{ busyAction === 'weibo_login' ? '正在打开…' : selectedPlatform.connected ? '重新登录' : '登录微博' }}
            </ElButton>
            <ElButton class="button button-secondary button-small" native-type="button" :disabled="loading || refreshing || Boolean(busyAction)" @click="refreshStatuses">
              {{ loading || refreshing ? '检查中…' : '检查连接' }}
            </ElButton>
            <ElLink :underline="false" v-if="selectedPlatform.authorization_url" class="text-button" :href="selectedPlatform.authorization_url" target="_blank" rel="noopener noreferrer">官方 CLI 使用说明 ↗</ElLink>
            <ElLink :underline="false" v-if="selectedPlatform.developer_url" class="text-button" :href="selectedPlatform.developer_url" target="_blank" rel="noopener noreferrer">完成开发者认证 ↗</ElLink>
            <ElLink :underline="false" v-if="selectedPlatform.service_url" class="text-button" :href="selectedPlatform.service_url" target="_blank" rel="noopener noreferrer">开通服务 ↗</ElLink>
          </div>
          <p v-if="selectedPlatform.connected" class="weibo-publish-note">微博文章发布可能消耗积分。配图需在微博端手动添加。</p>
        </div>

        <VideoPublishAccountsPanel
          v-else-if="selectedPlatform.id === 'xiaohongshu'"
          ref="xiaohongshuAccountManager"
          embedded
          context="article"
          initial-platform="xiaohongshu"
          :platform-filter="['xiaohongshu']"
          @accounts-changed="reloadPlatforms"
        />

        <VideoPublishAccountsPanel
          v-else-if="selectedPlatform.id === 'baijiahao'"
          ref="baijiahaoAccountManager"
          embedded
          context="article"
          initial-platform="baijiahao"
          :platform-filter="['baijiahao']"
          @accounts-changed="reloadPlatforms"
        />

        <div v-else class="publishing-platform-empty">
          <strong>通过创作平台网页手动发布</strong>
          <p>在发布页复制正文和图片，再粘贴到对应平台完成发布。</p>
          <ElLink :underline="false" v-if="selectedPlatform.manual_url" class="button button-secondary button-small" :href="selectedPlatform.manual_url" target="_blank" rel="noopener noreferrer">打开{{ selectedPlatform.name }}创作平台 ↗</ElLink>
        </div>
      </section>

      <section v-else class="publishing-platform-workspace publishing-no-platform" role="status">
        <strong>暂时没有可管理的平台</strong>
        <ElButton text class="text-button" native-type="button" @click="reloadPlatforms">重新读取</ElButton>
      </section>
    </div>

    <ElDialog
      v-model="accountDialogOpen"
      class="publishing-account-dialog"
      width="min(1000px, calc(100vw - 30px))"
      :show-close="false"
      :close-on-click-modal="false"
      :close-on-press-escape="true"
      :before-close="beforeCloseAccountDialog"
      :append-to-body="true"
      :align-center="true"
      modal-class="publishing-account-dialog-mask"
      :aria-label="dialogTitle"
      @close="handleAccountDialogClosed"
    >
      <div class="publishing-dialog-shell">
        <header class="publishing-dialog-header">
          <div>
            <h2 id="publishing-account-dialog-title">{{ dialogTitle }}</h2>
            <p>{{ isWechatDialog ? '保存前会通过微信接口验证 AppID 与 AppSecret。' : '凭据由本机后端保存并按账号隔离。' }}</p>
          </div>
          <ElButton class="publishing-dialog-close" native-type="button" aria-label="关闭弹窗" :disabled="Boolean(busyAction)" @click="closeAccountDialog">×</ElButton>
        </header>

        <form id="publishing-account-form" ref="accountFormElement" class="publishing-dialog-form" @submit.prevent="saveAccount">
          <div v-if="isWechatDialog" class="publishing-dialog-columns">
            <div class="publishing-form-fields">
              <ElAlert v-if="errorMessage" class="info-box form-error" type="error" :closable="false" :show-icon="false" role="alert" :title="errorMessage" />
              <p class="publishing-form-required">带 <span aria-hidden="true">*</span> 的字段为必填项。已配置的 AppSecret 默认隐藏；点“重置”后输入新密钥。</p>

              <label class="form-label" for="wechat-account-name">账号名称 <span class="publishing-required">*</span></label>
              <ElInput
                id="wechat-account-name"
                v-model="accountForm.accountName"
                class="publishing-input"
                :required="accountNameRequired"
                maxlength="80"
                autocomplete="organization"
                placeholder="例如：品牌公众号"
                aria-describedby="wechat-name-help"
              />
              <p id="wechat-name-help" class="publishing-field-help">此名称用于工作台区分账号，不会修改公众号后台名称；当前直连方式无法读取官方名称。</p>

              <label class="form-label" for="wechat-account-appid">公众号 AppID <span class="publishing-required">*</span></label>
              <ElInput
                id="wechat-account-appid"
                v-model="accountForm.publicValue"
                class="publishing-input"
                required
                autocomplete="off"
                maxlength="128"
                pattern="[A-Za-z0-9_-]{8,128}"
                title="请输入有效的公众号 AppID。"
                placeholder="例如 wx1234567890abcdef"
                aria-describedby="wechat-appid-help"
              />
              <p id="wechat-appid-help" class="publishing-field-help">填写微信开发者平台中对应公众号的 AppID。</p>

              <div class="publishing-secret-label-row">
                <label class="form-label" for="wechat-account-secret">AppSecret <span v-if="secretRequired" class="publishing-required">*</span></label>
                <ElTag v-if="isEditing && secretConfigured && !publicValueChanged" class="status-badge" type="success" effect="light" size="small">已配置</ElTag>
                <ElTag v-else-if="isEditing && publicValueChanged" class="status-badge" type="warning" effect="light" size="small">更换账号需新密钥</ElTag>
                <ElButton
                  v-if="isEditing && secretConfigured && !publicValueChanged"
                  class="publishing-secret-action"
                  native-type="button"
                  :disabled="Boolean(busyAction)"
                  @click="secretReset ? cancelSecretReset() : resetSecretInput()"
                >{{ secretReset ? '取消' : '重置' }}</ElButton>
              </div>
              <ElInput
                v-if="showSecretInput"
                id="wechat-account-secret"
                v-model="accountForm.secretValue"
                class="publishing-input"
                type="password"
                :required="secretRequired"
                autocomplete="new-password"
                maxlength="4096"
                :placeholder="publicValueChanged ? '更换 AppID 后请填写新 AppSecret' : isEditing ? '填写要替换的新 AppSecret' : '填写微信开发者平台生成的 AppSecret'"
                aria-describedby="wechat-secret-help"
              />
              <p id="wechat-secret-help" class="publishing-field-help">
                {{ showSecretInput ? '新密钥仅保存到本机 Windows 凭据管理器；保存后不会回显。' : 'AppSecret 已配置并安全保存在本机，页面不会回显。点击“重置”可替换密钥。' }}
              </p>

              <div v-if="errorMessage" class="publishing-inline-reminder" role="note">
                如果提示 IP 不在白名单，请把运行本工作台电脑的公网出口 IP 加入该公众号的 API IP 白名单后重试。
              </div>
            </div>

            <aside class="publishing-config-guide" aria-labelledby="wechat-config-guide-title">
              <h3 id="wechat-config-guide-title">在微信开发者平台找到配置信息</h3>
              <ol>
                <li>
                  <strong>找到 AppID</strong>
                  <span>登录微信开发者平台，进入“我的业务与服务 → 公众号/服务号”，打开对应账号的信息页查看 AppID。<ElLink :underline="false" href="https://developers.weixin.qq.com/platform/" target="_blank" rel="noopener noreferrer">打开微信开发者平台 ↗</ElLink></span>
                </li>
                <li>
                  <strong>获取 AppSecret</strong>
                  <span>在对应账号的“开发密钥”中生成或重置。密钥通常只在生成时显示一次，请立即复制并安全保存。</span>
                </li>
                <li>
                  <strong>设置 API IP 白名单</strong>
                  <span>将下方识别到的正常网络 IPv4 粘贴到对应公众号的 API IP 白名单。家庭路由器的 192.168.x.x 等局域网地址不能代替公网 IP。</span>
                  <div class="publishing-public-ip-tools" role="group" aria-label="本机公网 IP">
                    <code>{{ publicIpLoading ? '正在识别…' : publicIp || '等待识别' }}</code>
                    <ElButton v-if="publicIp" class="publishing-secret-action" native-type="button" @click="copyPublicIp">复制</ElButton>
                    <ElButton class="publishing-secret-action" native-type="button" :disabled="publicIpLoading" @click="detectPublicIp">{{ publicIpLoading ? '识别中…' : publicIp ? '重新识别' : '重试' }}</ElButton>
                  </div>
                  <span v-if="publicIpCopied" class="publishing-public-ip-result" role="status">公网 IP 已复制。</span>
                  <span v-if="publicIpError" class="publishing-public-ip-error" role="alert">{{ publicIpError }}</span>
                  <span class="publishing-public-ip-note">通过 IPIP 国内免费服务查询当前公网 IPv4，不会发送 AppID 或 AppSecret。若 VPN 使用全局代理，结果仍可能是 VPN 出口。</span>
                  <ElLink :underline="false" class="publishing-public-ip-link" href="https://qifu.baidu.com/?activeId=SEARCH_IP_ADDRESS" target="_blank" rel="noopener noreferrer">打开百度查询浏览器公网 IP ↗</ElLink>
                  <span class="publishing-public-ip-note">该页面可用于核对；若与上方地址不同，请先检查 VPN 分流，再按微信 API 实际出口配置白名单。</span>
                </li>
              </ol>
              <div class="publishing-guide-reminder">
                <strong>头像与名称读取限制</strong>
                <p>普通公众号 AppID / AppSecret 接口无法读取公众号自身头像与名称，因此列表使用公众号平台图标，不提供手动上传。自动显示官方头像需要另行接入微信开放平台第三方平台授权。</p>
              </div>
            </aside>
          </div>

          <div v-else class="publishing-dialog-columns">
            <div class="publishing-form-fields">
              <ElAlert v-if="errorMessage" class="info-box form-error" type="error" :closable="false" :show-icon="false" role="alert" :title="errorMessage" />
              <p class="publishing-form-required">带 <span aria-hidden="true">*</span> 的字段为必填项。</p>
              <label class="form-label" for="zhihu-account-name">账号名称 <span class="publishing-required">*</span></label>
              <ElInput id="zhihu-account-name" v-model="accountForm.accountName" class="publishing-input" required maxlength="80" placeholder="例如：知乎主号" />
              <label class="form-label" for="zhihu-account-key">知乎主页标识 / App Key <span class="publishing-required">*</span></label>
              <ElInput id="zhihu-account-key" v-model="accountForm.publicValue" class="publishing-input" required autocomplete="off" maxlength="128" placeholder="填写开放平台分配的 App Key" />
              <div class="publishing-secret-label-row">
                <label class="form-label" for="zhihu-account-secret">OpenAPI App Secret <span v-if="secretRequired" class="publishing-required">*</span></label>
                <ElTag v-if="isEditing && secretConfigured && !publicValueChanged" class="status-badge" type="success" effect="light" size="small">已配置</ElTag>
                <ElTag v-else-if="isEditing && publicValueChanged" class="status-badge" type="warning" effect="light" size="small">更换账号需新密钥</ElTag>
                <ElButton
                  v-if="isEditing && secretConfigured && !publicValueChanged"
                  class="publishing-secret-action"
                  native-type="button"
                  :disabled="Boolean(busyAction)"
                  @click="secretReset ? cancelSecretReset() : resetSecretInput()"
                >{{ secretReset ? '取消' : '重置' }}</ElButton>
              </div>
              <ElInput
                v-if="showSecretInput"
                id="zhihu-account-secret"
                v-model="accountForm.secretValue"
                class="publishing-input"
                type="password"
                :required="secretRequired"
                autocomplete="new-password"
                maxlength="4096"
                :placeholder="publicValueChanged ? '更换 App Key 后请填写新 App Secret' : isEditing ? '填写要替换的新 App Secret' : '填写开放平台申请的 App Secret'"
                aria-describedby="zhihu-secret-help"
              />
              <p id="zhihu-secret-help" class="publishing-field-help">
                {{ showSecretInput ? '新密钥保存后不会回显。' : 'App Secret 已配置并安全保存，页面不会回显。点击“重置”可替换密钥。' }}
              </p>
              <p class="publishing-field-help">凭据已保存不代表知乎账号已获准发布文章。</p>
            </div>
            <aside class="publishing-config-guide publishing-config-guide--zhihu">
              <h3>知乎账号连接说明</h3>
              <ol>
                <li><strong>申请开放平台应用</strong><span>使用知乎开放平台审核通过的 App Key 与 App Secret。</span></li>
                <li><strong>确认账号权限</strong><span>知乎文章发布 OpenAPI 仅对获准的创作者开放。</span></li>
              </ol>
              <ElLink :underline="false" class="publishing-guide-link" :href="dialogPlatform?.authorization_url || 'https://www.zhihu.com/creator'" target="_blank" rel="noopener noreferrer">打开知乎开放平台 ↗</ElLink>
            </aside>
          </div>
        </form>

        <footer class="publishing-dialog-footer">
          <span v-if="isWechatDialog" class="publishing-footer-security">AppSecret 仅保存在本机；当前直连方式不支持自动同步公众号头像。</span>
          <span v-else class="publishing-footer-security">App Secret 保存后不会再次显示。</span>
          <div>
            <ElButton class="button button-secondary button-small" native-type="button" :disabled="Boolean(busyAction)" @click="closeAccountDialog">取消</ElButton>
            <ElButton class="button button-small" native-type="submit" form="publishing-account-form" :disabled="Boolean(busyAction)">
              {{ submitLabel }}
            </ElButton>
          </div>
        </footer>
      </div>
    </ElDialog>
  </section>
</template>

<style scoped>
.publishing-accounts { padding: 18px; }
.publishing-accounts .panel-head { align-items: flex-start; margin-bottom: 14px; }
.publishing-accounts .panel-head h2 { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.publishing-accounts .panel-head p { margin: 5px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.publishing-accounts :deep(.el-button) { margin-left: 0; font-family: inherit; font-weight: var(--fw-medium); }
.publishing-accounts :deep(.el-button.button-small) { font-size: var(--fs-body-sm); }
.publishing-accounts :deep(.el-button.text-button) { min-height: 36px; color: var(--pine); font-size: var(--fs-body); background: transparent; border-color: transparent; }
.publishing-accounts :deep(.el-button.text-button:hover:not(.is-disabled)) { color: var(--pine); text-decoration: underline; text-underline-offset: 3px; }
.publishing-accounts :deep(.el-button.publishing-remove-button) { color: var(--danger); }
.publishing-accounts :deep(.el-link.text-button) { min-height: 36px; color: var(--pine); font-size: var(--fs-body); font-weight: var(--fw-medium); }
.publishing-accounts :deep(.el-link.button) { font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.publishing-accounts :deep(.el-alert.info-box) { align-items: flex-start; padding: 10px 12px; border-radius: 11px; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.publishing-accounts :deep(.el-alert.info-box .el-alert__title) { font-size: inherit; font-weight: var(--fw-regular); line-height: inherit; }
.publishing-account-note { margin-bottom: 12px; padding: 10px 12px; line-height: 1.5; font-size: var(--fs-body-sm); }
.publishing-loading { padding: 26px; color: var(--muted); text-align: center; font-size: var(--fs-body-sm); }
.publishing-layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); min-height: 420px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: #fff; }
.publishing-platform-nav { display: flex; flex-direction: column; gap: 6px; padding: 15px 11px; border-right: 1px solid var(--line); background: var(--paper-2); }
.publishing-nav-heading { display: flex; justify-content: space-between; align-items: center; margin: 0 5px 8px; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.publishing-nav-heading > span { min-width: 25px; padding: 2px 7px; border-radius: 99px; background: var(--paper-2); color: var(--text-tertiary); text-align: center; font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.publishing-platform-tab { position: relative; display: grid; grid-template-columns: 32px minmax(0, 1fr) auto; align-items: center; gap: 9px; min-width: 0; min-height: 48px; padding: 6px 9px; border: 0; border-radius: 9px; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }
.publishing-platform-tab.el-button { width: 100%; height: auto; margin: 0; justify-content: stretch; font-family: inherit; font-size: var(--fs-body); font-weight: var(--fw-regular); white-space: normal; }
.publishing-accounts :deep(.publishing-platform-tab.el-button > span) { display: contents; }
.publishing-platform-tab:hover { background: var(--surface-hover); }
.publishing-platform-tab.el-button:hover:not(.is-disabled) { color: var(--text-primary); background: var(--surface-hover); }
.publishing-platform-tab.is-active { background: var(--surface-selected); color: var(--text-selected); font-weight: var(--fw-semibold); }
.publishing-platform-tab.el-button.is-active { background: var(--surface-selected); color: var(--text-selected); font-weight: var(--fw-semibold); }
.publishing-platform-tab.is-active::before { position: absolute; inset: 8px auto 8px 0; width: 3px; border-radius: 4px; background: var(--pine); content: ''; }
.publishing-platform-mark { width: 32px; height: 32px; display: inline-grid; flex: 0 0 auto; place-items: center; border-radius: 9px; font-size: var(--fs-body-sm); font-weight: var(--fw-bold); }
.publishing-platform-name { min-width: 0; overflow: hidden; font-size: var(--fs-body); text-overflow: ellipsis; white-space: nowrap; }
.publishing-platform-summary { max-width: 86px; padding: 3px 6px; overflow: hidden; border-radius: 99px; background: var(--paper); color: var(--text-tertiary); font-size: var(--fs-caption); text-overflow: ellipsis; white-space: nowrap; }
.publishing-nav-foot { margin: auto 4px 0; padding: 13px 6px 2px; border-top: 1px solid var(--line); color: var(--muted); }
.publishing-nav-foot strong { color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.publishing-nav-foot p { margin: 5px 0 0; font-size: var(--fs-caption); line-height: 1.55; }
.publishing-platform-workspace { min-width: 0; padding: 22px 24px; container-type: inline-size; }
.publishing-platform-header { display: flex; justify-content: space-between; align-items: center; gap: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
.publishing-platform-title { display: flex; align-items: center; gap: 11px; min-width: 0; }
.publishing-platform-mark-large { width: 42px; height: 42px; border-radius: 11px; font-size: 15px; }
.publishing-platform-title h3 { margin: 0; color: var(--ink); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.publishing-platform-title p { max-width: 620px; margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.publishing-add-button { flex: 0 0 auto; min-height: 38px; border-radius: 9px; }
.publishing-list-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 18px 1px 10px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.publishing-list-heading > div { display: flex; align-items: center; gap: 8px; color: var(--ink); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.publishing-count { min-width: 21px; padding: 2px 6px; border-radius: 99px; background: #eef1ed; color: #5d6b60; text-align: center; font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.publishing-account-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 12px; margin: 0; padding: 0; list-style: none; }
.publishing-account-card { display: flex; flex-direction: column; gap: 10px; min-width: 0; padding: 15px; border: 1px solid #dfe5de; border-radius: 11px; background: #fff; }
.publishing-account-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; min-width: 0; }
.publishing-account-identity { display: flex; align-items: center; gap: 11px; min-width: 0; }
.publishing-account-identity > strong { min-width: 0; overflow-wrap: anywhere; color: var(--ink); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.publishing-accounts :deep(.publishing-account-head .status-badge.el-tag), .publishing-accounts :deep(.publishing-secret-label-row .status-badge.el-tag) { flex: 0 0 auto; min-height: 24px; padding: 0 10px; border-color: transparent; font-size: var(--fs-caption); font-weight: var(--fw-medium); line-height: 1.4; }
.publishing-accounts :deep(.status-badge.el-tag--success) { --el-tag-bg-color: var(--success-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--success); }
.publishing-accounts :deep(.status-badge.el-tag--warning) { --el-tag-bg-color: var(--warning-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--warning); }
.publishing-accounts :deep(.status-badge.el-tag--danger) { --el-tag-bg-color: var(--danger-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--danger); }
.publishing-accounts :deep(.status-badge.el-tag--info) { --el-tag-bg-color: var(--paper-2); --el-tag-border-color: transparent; --el-tag-text-color: var(--text-secondary); }
.publishing-account-avatar { width: 44px; height: 44px; flex: 0 0 44px; display: grid; place-items: center; overflow: hidden; border-radius: 12px; background: #edf6ef; }
.publishing-account-avatar img { width: 100%; height: 100%; object-fit: cover; }
.publishing-platform-mark-avatar { width: 100%; height: 100%; border-radius: 0; font-size: 17px; }
.publishing-account-info { display: grid; gap: 3px; min-width: 0; }
.publishing-account-meta { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: 1.5; }
.publishing-account-meta code { color: #4a5d4f; font-size: var(--fs-caption); word-break: break-all; }
.publishing-account-hint { margin: 0; color: #9a6844; font-size: var(--fs-caption); line-height: 1.5; }
.publishing-account-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: auto; padding-top: 10px; border-top: 1px solid var(--line); }
.publishing-account-actions .button { min-height: 30px; padding: 0 11px; border-radius: 8px; }
.publishing-remove-button { margin-left: auto; color: #8a5b54; }
.publishing-empty-state, .publishing-platform-empty { display: grid; justify-items: center; gap: 10px; padding: 58px 20px; color: var(--muted); text-align: center; }
.publishing-platform-mark-empty { width: 50px; height: 50px; border-radius: 14px; font-size: 20px; }
.publishing-empty-state > strong, .publishing-platform-empty > strong { color: var(--ink); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.publishing-empty-state > p, .publishing-platform-empty > p { max-width: 430px; margin: 0 0 3px; font-size: var(--fs-body-sm); line-height: 1.6; }
.publishing-empty-state .button, .publishing-platform-empty .button { text-decoration: none; }
.weibo-connection-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.weibo-connection-heading > strong { min-width: 0; color: var(--ink); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); text-align: left; overflow-wrap: anywhere; }
.publishing-account-list.publishing-account-list--single { grid-template-columns: minmax(0, min(100%, 450px)); }
.publishing-account-avatar--round { border-radius: 50%; }
.publishing-csdn-account { min-width: 0; padding-top: 18px; }
.weibo-account-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 8px; }
.weibo-install-instruction, .weibo-readiness-list, .weibo-publish-note { font-size: var(--fs-body-sm); }
.weibo-install-instruction code { display: inline-block; padding: 2px 5px; border-radius: 4px; background: var(--paper-2); overflow-wrap: anywhere; }
.publishing-no-platform { display: flex; align-items: center; justify-content: center; gap: 12px; color: var(--muted); }
:global(.el-dialog.publishing-account-dialog) { max-width: 1000px; max-height: min(90dvh, 840px); padding: 0; overflow: hidden; border: 1px solid #dce3da; border-radius: 14px; background: var(--paper); color: var(--ink); box-shadow: 0 24px 72px rgba(24, 39, 29, .24); }
:global(.el-dialog.publishing-account-dialog .el-dialog__header) { display: none; }
:global(.el-dialog.publishing-account-dialog .el-dialog__body) { max-height: min(90dvh, 840px); padding: 0; color: inherit; }
:global(.el-dialog.publishing-account-dialog .el-alert.info-box) { align-items: flex-start; padding: 10px 12px; border-radius: 11px; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
:global(.el-dialog.publishing-account-dialog .el-alert.info-box .el-alert__title) { font-size: inherit; font-weight: var(--fw-regular); line-height: inherit; }
:global(.publishing-account-dialog-mask) { background: rgba(17, 28, 22, .52) !important; backdrop-filter: blur(2px); }
:global(.el-dialog.publishing-account-dialog .el-button) { margin-left: 0; font-family: inherit; font-weight: var(--fw-medium); }
:global(.el-dialog.publishing-account-dialog .el-button.button-small) { font-size: var(--fs-body-sm); }
:global(.el-dialog.publishing-account-dialog .el-button.text-button.is-text) { min-height: 36px; color: var(--pine); font-size: var(--fs-body); background: transparent; border-color: transparent; }
:global(.el-dialog.publishing-account-dialog .publishing-dialog-close.el-button) { width: 31px; height: 31px; min-height: 31px; flex: 0 0 auto; padding: 0; border: 1px solid var(--line); border-radius: 50%; background: var(--paper); color: var(--text-secondary); font-size: 20px; line-height: 1; }
:global(.el-dialog.publishing-account-dialog .publishing-dialog-close.el-button > span) { display: inline-flex; align-items: center; justify-content: center; }
:global(.el-dialog.publishing-account-dialog .status-badge.el-tag) { min-height: 22px; padding: 0 9px; border-color: transparent; font-size: var(--fs-caption); font-weight: var(--fw-medium); }
:global(.el-dialog.publishing-account-dialog .status-badge.el-tag--success) { --el-tag-bg-color: var(--success-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--success); }
:global(.el-dialog.publishing-account-dialog .status-badge.el-tag--warning) { --el-tag-bg-color: var(--warning-soft); --el-tag-border-color: transparent; --el-tag-text-color: var(--warning); }
:global(.el-dialog.publishing-account-dialog .publishing-guide-link.el-link), :global(.el-dialog.publishing-account-dialog .publishing-public-ip-link.el-link) { color: var(--pine); }
:global(.el-dialog.publishing-account-dialog .publishing-guide-link.el-link) { display: inline-flex; margin-top: 13px; font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
:global(.el-dialog.publishing-account-dialog .publishing-public-ip-link.el-link) { display: inline-flex; align-self: flex-start; font-size: var(--fs-caption); font-weight: var(--fw-semibold); line-height: 1.45; }
.publishing-dialog-shell { display: flex; flex-direction: column; max-height: min(90dvh, 840px); }
.publishing-dialog-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 18px 22px; border-bottom: 1px solid var(--line); }
.publishing-dialog-header h2 { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.publishing-dialog-header p { margin: 5px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.publishing-dialog-form { min-height: 0; overflow-y: auto; padding: 18px 22px; }
.publishing-dialog-columns { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(300px, .95fr); gap: 24px; }
.publishing-form-fields { display: grid; align-content: start; gap: 8px; min-width: 0; }
.publishing-form-fields .form-label { margin: 4px 0 0; color: var(--text-primary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.publishing-secret-label-row { display: flex; align-items: center; gap: 8px; min-height: 26px; }
.publishing-secret-label-row .form-label { margin: 0; }
.publishing-secret-label-row .status-badge { min-height: 22px; padding: 0 9px; font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.publishing-secret-action { margin-left: auto; padding: 4px 9px; border: 1px solid #d9e2d8; border-radius: 6px; background: #fff; color: #356c48; font-size: var(--fs-caption); font-weight: var(--fw-semibold); cursor: pointer; }
.publishing-secret-action:hover { border-color: #9bbba1; background: #f4f8f3; }
.publishing-secret-action:disabled { opacity: .55; cursor: not-allowed; }
.publishing-secret-action:focus-visible { outline: 2px solid var(--pine); outline-offset: 2px; }
.publishing-form-fields :deep(.publishing-input .el-input__wrapper) { min-height: 40px; padding: 1px 11px; border-radius: 8px; box-shadow: 0 0 0 1px var(--line) inset; }
.publishing-form-fields :deep(.publishing-input .el-input__inner) { color: var(--text-primary); font-size: var(--fs-body); }
.publishing-form-fields :deep(.publishing-input .el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--pine) inset, 0 0 0 3px var(--pine-soft); }
.publishing-form-fields :deep(.publishing-input .el-input__inner::placeholder) { color: var(--text-placeholder); }
.publishing-required, .publishing-form-required > span { color: #c43e32; }
.publishing-form-required { margin: 0 0 3px; color: var(--text-tertiary); font-size: var(--fs-caption); }
.publishing-field-help { margin: -3px 0 2px; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.45; }
.publishing-inline-reminder { margin-top: 5px; padding: 9px 10px; border: 1px solid #ead6b9; border-radius: 8px; background: #fff9ee; color: #805b2c; font-size: var(--fs-caption); line-height: 1.5; }
.publishing-config-guide { align-self: stretch; padding: 16px; border: 1px solid #dfe7dd; border-radius: 11px; background: #f6f9f5; }
.publishing-config-guide h3 { margin: 0 0 14px; color: var(--ink); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.publishing-config-guide ol { display: grid; gap: 15px; margin: 0; padding: 0; list-style: none; counter-reset: guide-step; }
.publishing-config-guide li { position: relative; display: grid; gap: 4px; padding-left: 31px; counter-increment: guide-step; }
.publishing-config-guide li::before { position: absolute; top: 0; left: 0; width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; background: #e2ede3; color: #2d7048; font-size: var(--fs-caption); font-weight: var(--fw-bold); content: counter(guide-step); }
.publishing-config-guide li strong { color: var(--ink); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.publishing-config-guide li span { color: var(--text-tertiary); font-size: var(--fs-caption); line-height: 1.6; }
.publishing-config-guide li span :deep(.el-link) { display: inline-flex; margin-left: 4px; color: var(--pine); font-size: inherit; font-weight: var(--fw-semibold); vertical-align: baseline; }
.publishing-guide-reminder { margin-top: 16px; padding: 10px; border-radius: 8px; background: #edf3ed; color: #3e5f47; }
.publishing-guide-reminder strong { font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.publishing-guide-reminder p { margin: 4px 0 0; font-size: var(--fs-caption); line-height: 1.55; }
.publishing-public-ip-tools { display: flex; align-items: center; gap: 6px; min-width: 0; }
.publishing-public-ip-tools code { min-width: 0; flex: 1 1 auto; overflow: hidden; padding: 6px 8px; border: 1px solid #dfe7dd; border-radius: 6px; background: #fff; color: var(--ink); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.publishing-public-ip-tools .publishing-secret-action { flex: 0 0 auto; margin-left: 0; }
.publishing-public-ip-result, .publishing-public-ip-note, .publishing-public-ip-error { font-size: var(--fs-caption) !important; line-height: 1.45 !important; }
.publishing-public-ip-result { color: #287247 !important; }
.publishing-public-ip-note { color: var(--text-tertiary) !important; }
.publishing-accounts :deep(.publishing-public-ip-link.el-link) { display: inline-flex; align-self: flex-start; color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-semibold); line-height: 1.45; }
.publishing-public-ip-error { color: #a33c33 !important; }
.publishing-accounts :deep(.publishing-guide-link.el-link) { display: inline-flex; margin-top: 13px; color: var(--pine); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.publishing-config-guide--zhihu { align-self: start; }
.publishing-dialog-footer { display: flex; justify-content: space-between; align-items: center; gap: 16px; min-height: 62px; padding: 11px 22px; border-top: 1px solid var(--line); background: #fff; }
.publishing-footer-security { color: var(--text-tertiary); font-size: var(--fs-caption); }
.publishing-dialog-footer > div { display: flex; gap: 8px; }
.publishing-dialog-footer .button { min-height: 34px; border-radius: 8px; }

@container (max-width: 780px) { .publishing-account-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (max-width: 520px) { .publishing-account-list { grid-template-columns: 1fr; } }
.toutiao-account-list { grid-template-columns: minmax(0, 520px); }

@media (max-width: 850px) {
  .publishing-layout { grid-template-columns: minmax(0, 1fr); }
  .publishing-platform-nav { flex-direction: row; align-items: stretch; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .publishing-nav-heading, .publishing-nav-foot { display: none; }
  .publishing-platform-tab { min-width: 145px; grid-template-columns: 30px minmax(0, 1fr); }
  .publishing-platform-summary { grid-column: 2; margin-top: -6px; }
}
@media (max-width: 650px) {
  .publishing-accounts { padding: 12px; }
  .publishing-platform-workspace { padding: 17px 13px; }
  .publishing-platform-header { align-items: flex-start; flex-direction: column; gap: 12px; }
  .publishing-add-button { width: 100%; }
  .publishing-account-card { padding: 12px; }
  .publishing-account-avatar { width: 40px; height: 40px; flex-basis: 40px; }
  .publishing-remove-button { margin-left: 0; }
  :global(.el-dialog.publishing-account-dialog) { width: calc(100vw - 18px); max-height: 94dvh; }
  :global(.el-dialog.publishing-account-dialog .el-dialog__body) { max-height: 94dvh; }
  .publishing-dialog-shell { max-height: 94dvh; }
  .publishing-dialog-header, .publishing-dialog-form { padding: 14px; }
  .publishing-dialog-columns { grid-template-columns: minmax(0, 1fr); gap: 17px; }
  .publishing-config-guide { padding: 13px; }
  .publishing-dialog-footer { align-items: stretch; flex-direction: column; padding: 12px 14px; }
  .publishing-dialog-footer > div { justify-content: flex-end; }
  .publishing-footer-security { line-height: 1.45; }
}
</style>
