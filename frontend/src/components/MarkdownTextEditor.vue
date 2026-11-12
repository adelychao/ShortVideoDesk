<script setup lang="ts">
import { nextTick, ref } from 'vue'
import MarkdownPreview from './MarkdownPreview.vue'

const props = withDefaults(defineProps<{
  modelValue: string
  disabled?: boolean
  maxLength?: number
}>(), {
  disabled: false,
  maxLength: 5_000_000,
})
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const editor = ref<HTMLTextAreaElement | null>(null)
const previewMode = ref(false)

function updateText(event: Event): void {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}

async function wrapSelection(prefix: string, suffix = prefix, placeholder = '文本'): Promise<void> {
  const element = editor.value
  if (!element || props.disabled) return
  const start = element.selectionStart
  const end = element.selectionEnd
  const selected = props.modelValue.slice(start, end)
  const content = selected || placeholder
  const replacement = `${prefix}${content}${suffix}`
  emit('update:modelValue', `${props.modelValue.slice(0, start)}${replacement}${props.modelValue.slice(end)}`)
  await nextTick()
  element.focus()
  element.setSelectionRange(start + prefix.length, start + prefix.length + content.length)
}

async function prefixLines(prefix: string, placeholder: string): Promise<void> {
  const element = editor.value
  if (!element || props.disabled) return
  const start = element.selectionStart
  const end = element.selectionEnd
  const lineStart = props.modelValue.lastIndexOf('\n', Math.max(0, start - 1)) + 1
  const nextBreak = props.modelValue.indexOf('\n', end)
  const lineEnd = nextBreak === -1 ? props.modelValue.length : nextBreak
  const selection = props.modelValue.slice(lineStart, lineEnd)
  const lines = selection ? selection.split('\n') : [placeholder]
  const replacement = lines.map((line) => line ? `${prefix}${line}` : line).join('\n')
  emit('update:modelValue', `${props.modelValue.slice(0, lineStart)}${replacement}${props.modelValue.slice(lineEnd)}`)
  await nextTick()
  element.focus()
  if (start === end) {
    const caret = lineStart + replacement.length
    element.setSelectionRange(caret, caret)
  } else {
    element.setSelectionRange(lineStart, lineStart + replacement.length)
  }
}
</script>

<template>
  <section class="markdown-editor" aria-label="Markdown 文案编辑器">
    <div class="markdown-editor-toolbar" role="toolbar" aria-label="文本格式工具">
      <div class="markdown-editor-tools">
        <button class="markdown-tool" type="button" title="加粗" aria-label="加粗" :disabled="disabled" @click="wrapSelection('**', '**', '加粗文本')"><strong>B</strong></button>
        <span class="markdown-tool-divider" aria-hidden="true"></span>
        <button class="markdown-tool markdown-tool-heading" type="button" title="标题" aria-label="标题" :disabled="disabled" @click="prefixLines('## ', '标题')">H₂</button>
        <button class="markdown-tool" type="button" title="引用" aria-label="引用" :disabled="disabled" @click="prefixLines('> ', '引用内容')">❝</button>
        <button class="markdown-tool" type="button" title="项目符号" aria-label="项目符号" :disabled="disabled" @click="prefixLines('- ', '列表项')">☷</button>
      </div>
      <span class="markdown-editor-hint">支持 Markdown</span>
      <button class="button button-secondary button-small markdown-preview-toggle" type="button" :disabled="disabled" :aria-pressed="previewMode" @click="previewMode = !previewMode">
        {{ previewMode ? '返回编辑' : '预览' }}
      </button>
    </div>
    <textarea
      v-if="!previewMode"
      ref="editor"
      class="markdown-editor-input"
      :value="modelValue"
      :maxlength="maxLength"
      :disabled="disabled"
      aria-label="校对文案正文"
      spellcheck="false"
      @input="updateText"
    ></textarea>
    <div v-else class="markdown-editor-preview" aria-label="文案预览">
      <MarkdownPreview :content="modelValue || '当前版本没有正文。'" />
    </div>
    <div class="markdown-editor-footer">
      <span>{{ modelValue.length.toLocaleString('zh-CN') }} / {{ maxLength.toLocaleString('zh-CN') }} 字符</span>
      <span>编辑内容会另存为新版本</span>
    </div>
  </section>
</template>

<style scoped>
.markdown-editor { overflow: hidden; border: 1px solid #d8dcd3; border-radius: 10px; background: #fff; }
.markdown-editor-toolbar { display: flex; align-items: center; gap: 8px; min-height: 45px; padding: 5px 9px; border-bottom: 1px solid #e6e8e3; background: #fafbf9; }
.markdown-editor-tools { display: flex; align-items: center; gap: 4px; }
.markdown-tool { display: inline-grid; place-items: center; width: 32px; height: 30px; border: 0; border-radius: 6px; background: transparent; color: var(--ink); font-size: 14px; cursor: pointer; }
.markdown-tool:hover:not(:disabled) { background: #e9eee9; }
.markdown-tool:focus-visible, .markdown-preview-toggle:focus-visible { outline: 2px solid #72947b; outline-offset: 2px; }
.markdown-tool:disabled { opacity: .5; cursor: not-allowed; }
.markdown-tool-heading { font-size: 12px; font-weight: 600; }
.markdown-tool-divider { width: 1px; height: 20px; margin: 0 3px; background: #dfe3dc; }
.markdown-editor-hint { margin-left: 3px; color: var(--muted); font-size: 13px; }
.markdown-preview-toggle { min-height: 30px; margin-left: auto; padding: 0 9px; font-size: 13px; }
.markdown-editor-input { display: block; width: 100%; min-height: 390px; max-height: 65vh; resize: vertical; padding: 16px; border: 0; border-radius: 0; outline: none; color: var(--ink); background: #fff; font: 13px/1.8 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.markdown-editor-input:focus { box-shadow: inset 0 0 0 2px rgba(87,132,103,.18); }
.markdown-editor-input:disabled { color: var(--muted); background: #fafbf9; }
.markdown-editor-preview { min-height: 390px; max-height: 65vh; overflow: auto; padding: 16px; }
.markdown-editor-footer { display: flex; justify-content: space-between; gap: 10px; padding: 7px 12px; border-top: 1px solid #e6e8e3; color: var(--muted); background: #fafbf9; font-size: 12px; }
@media (max-width: 560px) { .markdown-editor-toolbar { flex-wrap: wrap; } .markdown-editor-hint { margin-right: auto; } .markdown-editor-input, .markdown-editor-preview { min-height: 300px; } }
</style>
