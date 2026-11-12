<script setup lang="ts">
import { ElAlert, ElEmpty, ElTag } from 'element-plus'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/tag/style/css'
import type { DiagnosticResult } from '../api/types'

defineProps<{
  results: DiagnosticResult[]
  loading: boolean
  error?: string
}>()

const labelById: Record<string, string> = {
  python: 'Python 运行时',
  'dependency-fastapi': 'FastAPI',
  'dependency-pydantic': 'Pydantic',
  'dependency-uvicorn': 'Uvicorn',
  'dependency-yt-dlp': 'yt-dlp',
  'dependency-playwright': 'Playwright',
  'dependency-keyring': '系统凭据存储',
  'dependency-faster-whisper': '本机离线转写组件',
  'local-asr-model': '本地语音模型',
  'playwright-browser': '平台浏览器运行时',
  ffmpeg: 'FFmpeg',
  ffprobe: 'ffprobe',
  sqlite: 'SQLite 数据库',
  'archive-root': '归档目录',
  'obsidian-reference-root': '知识库参考目录',
  'obsidian-output-root': '知识库输出目录',
  model: '模型配置',
  frontend: '工作台前端',
}

const statusLabel: Record<DiagnosticResult['status'], string> = {
  available: '可用',
  unconfigured: '未配置',
  limited: '受限',
  error: '异常',
}

const statusTagType: Record<DiagnosticResult['status'], 'success' | 'warning' | 'danger'> = {
  available: 'success',
  unconfigured: 'warning',
  limited: 'warning',
  error: 'danger',
}
</script>

<template>
  <div>
    <p v-if="loading" class="helper" role="status">正在检查本机环境…</p>
    <el-alert v-else-if="error" class="diagnostics-error" type="error" :closable="false" show-icon role="alert">{{ error }}</el-alert>
    <ul v-else-if="results.length" class="diagnostics-list" aria-label="本机环境检查结果">
      <li v-for="item in results" :key="item.id" class="diagnostic-item">
        <div class="diagnostic-item-head">
          <strong>{{ labelById[item.id] ?? item.id }}</strong>
          <el-tag :type="statusTagType[item.status]">{{ statusLabel[item.status] }}</el-tag>
        </div>
        <p>{{ item.message }}</p>
        <small v-if="item.remediation">{{ item.remediation }}</small>
      </li>
    </ul>
    <el-empty v-else :image-size="56" description="暂无诊断结果。" role="status" />
  </div>
</template>

<style scoped>
.diagnostics-error { margin: 0 0 6px; }
</style>
