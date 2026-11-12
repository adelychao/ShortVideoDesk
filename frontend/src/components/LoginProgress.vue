<script setup lang="ts">
import { ElProgress } from 'element-plus'
import 'element-plus/es/components/progress/style/css'

defineProps<{ qrReady: boolean; inPageOnly?: boolean; waitingHint?: string }>()
</script>

<template>
  <div class="login-progress" role="status" aria-live="polite">
    <strong>等待登录结果</strong>
    <p v-if="qrReady">请使用对应平台的 App 扫码，并在手机上确认。</p>
    <p v-else-if="waitingHint">{{ waitingHint }}</p>
    <p v-else-if="inPageOnly">正在准备二维码，请稍候。</p>
    <p v-else>请留意弹出的登录窗口，扫码并在手机上确认。</p>
    <ElProgress class="login-progress-bar" :percentage="35" :indeterminate="true" :duration="2" :show-text="false" aria-label="等待登录结果" />
    <p>确认后请稍候，账号信息会自动更新。</p>
  </div>
</template>

<style scoped>
.login-progress { display: grid; gap: 6px; flex: 1 1 150px; min-width: 0; color: var(--ink); font-size: 13px; line-height: 1.5; }
.login-progress strong { font-size: 13px; }
.login-progress p { margin: 0; color: var(--muted); overflow-wrap: anywhere; }
.login-progress-bar { width: min(100%, 180px); }
.login-progress-bar :deep(.el-progress-bar__outer) { height: 6px !important; }
@media (prefers-reduced-motion: reduce) { .login-progress-bar { display: none; } }
</style>
