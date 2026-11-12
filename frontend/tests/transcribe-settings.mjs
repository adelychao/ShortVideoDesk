import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
try {
  const { default: StepSelector } = await server.ssrLoadModule('/src/components/StepSelector.vue')
  const render = (props) => renderToString(createSSRApp(StepSelector, props))
  const selected = await render({ modelValue: ['download', 'transcribe'], modelConfigured: true })
  assert.match(selected, /aria-label="语音转文字设置"/)
  const unconfigured = await render({ modelValue: ['download'], modelConfigured: false })
  assert.match(unconfigured, /aria-label="语音转文字设置"/)
  console.log('Transcription settings entry is available for selected and unconfigured tasks')
} finally { await server.close() }
