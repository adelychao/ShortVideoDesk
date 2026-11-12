import { readFile } from 'node:fs/promises'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [
    vue(),
    {
      name: 'workbench-infographic-runtime',
      apply: 'build',
      async generateBundle() {
        const bundle = new URL('./node_modules/@antv/infographic/dist/infographic.min.js', import.meta.url)
        this.emitFile({ type: 'asset', fileName: 'assets/infographic.min.js', source: await readFile(bundle) })
      },
    },
  ],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8767',
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
