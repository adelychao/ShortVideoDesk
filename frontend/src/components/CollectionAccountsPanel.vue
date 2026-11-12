<script setup lang="ts">
import { onActivated, onMounted, ref } from 'vue'
import { ApiError, api } from '../api/client'
import PlatformLogo from './PlatformLogo.vue'
import type { PlatformAccountStatus } from '../api/types'

const platformAccounts = ref<PlatformAccountStatus[]>([])
const platformAccountsLoading = ref(false)
const platformAccountsError = ref('')
const platformAccountAction = ref<string | null>(null)
function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '平台账号请求失败，请稍后重试。'
}

async function loadPlatformAccounts(): Promise<void> {
  platformAccountsLoading.value = true
  platformAccountsError.value = ''
  try {
    platformAccounts.value = (await api.getPlatformAccounts()).platforms
  } catch (error) {
    platformAccountsError.value = errorMessage(error)
  } finally {
    platformAccountsLoading.value = false
  }
}

function replacePlatformAccount(status: PlatformAccountStatus): void {
  platformAccounts.value = platformAccounts.value.map((item) => item.id === status.id ? status : item)
}

async function openPlatformLogin(platform: PlatformAccountStatus): Promise<void> {
  if (platformAccountAction.value) return
  platformAccountAction.value = platform.id
  platformAccountsError.value = ''
  try {
    replacePlatformAccount(await api.openPlatformLogin(platform.id))
  } catch (error) {
    platformAccountsError.value = errorMessage(error)
  } finally {
    platformAccountAction.value = null
  }
}

async function clearPlatformLogin(platform: PlatformAccountStatus): Promise<void> {
  if (platformAccountAction.value || platform.login_window_open) return
  if (!window.confirm(`确定清除本机保存的${platform.name}登录状态吗？`)) return
  platformAccountAction.value = platform.id
  platformAccountsError.value = ''
  try {
    replacePlatformAccount(await api.clearPlatformLogin(platform.id))
  } catch (error) {
    platformAccountsError.value = errorMessage(error)
  } finally {
    platformAccountAction.value = null
  }
}

function platformAccountLabel(status: PlatformAccountStatus['status']): string {
  return ({ connected: '已检测到登录态', login_window_open: '登录窗口已打开', not_connected: '未登录', error: '读取失败' })[status]
}

onMounted(() => { void loadPlatformAccounts() })
onActivated(() => { if (!platformAccountsLoading.value) void loadPlatformAccounts() })
</script>

<template>
        <section class="panel settings-section" aria-labelledby="platform-account-title">
          <div class="panel-head"><div><h2 id="platform-account-title">视频采集与评论账号</h2><p>每个平台使用独立的应用专用浏览器档案，登录一次后供后续任务复用。</p></div><button class="text-button" type="button" :disabled="platformAccountsLoading || !!platformAccountAction" @click="loadPlatformAccounts">{{ platformAccountsLoading ? '读取中…' : '刷新状态' }}</button></div>
          <div class="info-box" role="note"><span>登录状态保存在本机项目数据目录，不读取日常浏览器的 Cookie。工作台只显示是否检测到会话，不读取账号身份；平台可能让会话到期或重新验证。</span></div>
          <p v-if="platformAccountsError" class="info-box form-error" role="alert">{{ platformAccountsError }}</p>
          <ul v-if="platformAccountsLoading && !platformAccounts.length" class="platform-account-list" aria-label="平台登录状态"><li class="helper" role="status">正在读取本机平台会话…</li></ul>
          <ul v-else class="platform-account-list" aria-label="平台登录状态">
            <li v-for="platform in platformAccounts" :key="platform.id" class="platform-account-item">
              <div class="platform-account-copy">
                <div class="platform-account-head">
                  <span class="platform-status-brand">
                    <PlatformLogo class="platform-status-logo" :platform-id="platform.id" :name="platform.name" />
                    <strong>{{ platform.name }}</strong>
                  </span>
                  <span class="status-badge" :class="platform.status === 'connected' ? '' : 'warn'">{{ platformAccountLabel(platform.status) }}</span>
                </div>
                <p>{{ platform.message }}</p>
              </div>
              <div class="platform-account-actions">
                <button class="button button-secondary button-small" type="button" :disabled="!!platformAccountAction || platform.login_window_open" @click="openPlatformLogin(platform)">{{ platformAccountAction === platform.id ? '正在打开…' : platform.login_window_open ? '请完成登录并关闭窗口' : platform.authenticated ? '重新登录' : '打开登录窗口' }}</button>
                <button class="text-button" type="button" :disabled="!!platformAccountAction || platform.login_window_open" @click="clearPlatformLogin(platform)">清除登录状态</button>
              </div>
            </li>
          </ul>
          <p class="helper">目前仅对抖音和 B 站开放。登录窗口完成后请关闭窗口并刷新状态；任务会使用该平台专属档案中的会话。</p>
        </section>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享类在本面板的覆盖(全量落地时上移 styles.css)—— */
.panel-head h2 { font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.panel-head p { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.helper { color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.info-box { font-size: var(--fs-body); line-height: var(--lh-base); }
.status-badge { min-height: 24px; padding: 0 10px; font-size: var(--fs-caption); font-weight: var(--fw-medium); }
.platform-status-brand strong { color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
</style>
