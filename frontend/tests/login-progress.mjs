import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const server = await createServer({ root, server: { middlewareMode: true }, appType: 'custom', logLevel: 'silent' })
try {
  const { default: LoginProgress } = await server.ssrLoadModule('/src/components/LoginProgress.vue')
  for (const ready of [true, false]) {
    const html = await renderToString(createSSRApp({ render: () => h(LoginProgress, { qrReady: ready }) }))
    assert.match(html, /等待登录结果/)
    assert.match(html, /确认后请稍候/)
    assert.match(html, /自动更新/)
    assert.match(html, /<progress[^>]*aria-label="等待登录结果"/)
    assert.doesNotMatch(html, /<progress[^>]*\svalue=/)
    assert.match(html, ready ? /扫码/ : /登录窗口/)
  }
  const inPage = await renderToString(createSSRApp({ render: () => h(LoginProgress, { qrReady: false, inPageOnly: true }) }))
  assert.match(inPage, /正在准备二维码/)
  assert.doesNotMatch(inPage, /弹出的登录窗口/)
  console.log('Login waiting feedback renders for both QR states.')
} finally {
  await server.close()
}
