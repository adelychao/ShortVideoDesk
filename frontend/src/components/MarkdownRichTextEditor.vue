<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CrepeBuilder } from '@milkdown/crepe/builder'
import { blockEdit } from '@milkdown/crepe/feature/block-edit'
import { cursor } from '@milkdown/crepe/feature/cursor'
import { linkTooltip } from '@milkdown/crepe/feature/link-tooltip'
import { listItem } from '@milkdown/crepe/feature/list-item'
import { placeholder } from '@milkdown/crepe/feature/placeholder'
import { table } from '@milkdown/crepe/feature/table'
import { topBar } from '@milkdown/crepe/feature/top-bar'
import { toolbar } from '@milkdown/crepe/feature/toolbar'
import { editorViewCtx } from '@milkdown/kit/core'
import { markdownToSlice, replaceAll } from '@milkdown/kit/utils'
import '@milkdown/crepe/theme/common/prosemirror.css'
import '@milkdown/crepe/theme/common/reset.css'
import '@milkdown/crepe/theme/common/block-edit.css'
import '@milkdown/crepe/theme/common/cursor.css'
import '@milkdown/crepe/theme/common/link-tooltip.css'
import '@milkdown/crepe/theme/common/list-item.css'
import '@milkdown/crepe/theme/common/placeholder.css'
import '@milkdown/crepe/theme/common/table.css'
import '@milkdown/crepe/theme/common/top-bar.css'
import '@milkdown/crepe/theme/common/toolbar.css'
import '@milkdown/crepe/theme/classic.css'

const props = withDefaults(defineProps<{
  modelValue: string
  id: string
  labelledBy: string
  disabled?: boolean
  placeholder?: string
}>(), {
  disabled: false,
  placeholder: '在这里开始写正文。',
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const editorRoot = ref<HTMLDivElement | null>(null)
const editorReady = ref(false)
const editorError = ref(false)
const blockedImagePaste = ref(false)
let crepe: CrepeBuilder | null = null
let disposed = false
let createFinished = false

type ImageInsertResult = 'replaced' | 'inserted-after-paragraph' | 'inserted-at-cursor' | 'unavailable'

function syncContent(value: string): void {
  if (!crepe || !editorReady.value || crepe.getMarkdown() === value) return
  crepe.editor.action(replaceAll(value, true))
}

function syncEditable(disabled: boolean): void {
  if (!crepe || !editorReady.value || editorError.value) return
  crepe.editor.action((ctx) => {
    ctx.get(editorViewCtx).setProps({ editable: () => !disabled })
  })
}

function blockLocalImageFiles(event: ClipboardEvent | DragEvent): void {
  const dataTransfer = 'clipboardData' in event ? event.clipboardData : event.dataTransfer
  const hasImageFile = Array.from(dataTransfer?.items ?? []).some((item) => item.kind === 'file' && item.type.startsWith('image/'))
  if (!hasImageFile) return
  event.preventDefault()
  event.stopPropagation()
  blockedImagePaste.value = true
}

function insertImageAtAnchor(input: { src: string; alt: string; slotId: string; anchorText: string }): ImageInsertResult {
  if (!crepe || !editorReady.value || editorError.value) return 'unavailable'

  let result: ImageInsertResult = 'inserted-at-cursor'
  const slotMarker = `slot=${encodeURIComponent(input.slotId)}`
  const safeAlt = input.alt.replace(/\]/g, ' ')
  const imageMarkdown = `![${safeAlt}](${input.src} "正文配图")`

  crepe.editor.action((ctx) => {
    const view = ctx.get(editorViewCtx)
    const state = view.state
    let matchingImagePosition: number | null = null
    state.doc.descendants((node, position) => {
      if (matchingImagePosition === null && node.type.name === 'image' && String(node.attrs.src || '').includes(slotMarker)) {
        matchingImagePosition = position
        return false
      }
      return true
    })

    if (matchingImagePosition !== null) {
      const position = matchingImagePosition
      const image = state.doc.nodeAt(position)
      if (!image) return
      view.dispatch(state.tr.setNodeMarkup(position, undefined, { ...image.attrs, src: input.src }).scrollIntoView())
      result = 'replaced'
      return
    }

    const normalizedAnchor = input.anchorText.trim().replace(/\s+/g, ' ')
    const matches: number[] = []
    if (normalizedAnchor) {
      state.doc.descendants((node, position) => {
        const insertionPosition = position + 1
        if (node.type.name === 'paragraph' && node.textContent.trim().replace(/\s+/g, ' ') === normalizedAnchor &&
            state.doc.resolve(insertionPosition).depth === 1) {
          matches.push(insertionPosition)
        }
        return true
      })
    }

    const referencePosition = matches.length === 1 ? matches[0] : state.selection.from
    const safeReferencePosition = Math.min(Math.max(referencePosition, 0), state.doc.content.size)
    let resolvedPosition = state.doc.resolve(safeReferencePosition)
    if (resolvedPosition.depth === 0 && safeReferencePosition < state.doc.content.size) {
      resolvedPosition = state.doc.resolve(safeReferencePosition + 1)
    }
    const insertPosition = resolvedPosition.depth > 0 ? resolvedPosition.after(1) : state.doc.content.size
    result = matches.length === 1 ? 'inserted-after-paragraph' : 'inserted-at-cursor'
    const imageSlice = markdownToSlice(`\n\n${imageMarkdown}\n\n`)(ctx)
    view.dispatch(state.tr.replace(insertPosition, insertPosition, imageSlice).scrollIntoView())
  })

  return result
}

function replaceManagedImageSource(slotId: string, src: string): boolean {
  if (!crepe || !editorReady.value || editorError.value) return false

  let replaced = false
  const slotMarker = `slot=${encodeURIComponent(slotId)}`
  crepe.editor.action((ctx) => {
    const view = ctx.get(editorViewCtx)
    let transaction = view.state.tr
    view.state.doc.descendants((node, position) => {
      if (node.type.name !== 'image' || !String(node.attrs.src || '').includes(slotMarker)) return true
      transaction = transaction.setNodeMarkup(position, undefined, { ...node.attrs, src })
      replaced = true
      return false
    })
    if (replaced) view.dispatch(transaction.scrollIntoView())
  })
  return replaced
}

defineExpose({ insertImageAtAnchor, replaceManagedImageSource })

watch(() => props.modelValue, syncContent)
watch(() => props.disabled, syncEditable)

onMounted(async () => {
  const root = editorRoot.value
  if (!root) return

  const instance = new CrepeBuilder({
    root,
    defaultValue: props.modelValue,
  })
    .addFeature(blockEdit)
    .addFeature(cursor)
    .addFeature(linkTooltip)
    .addFeature(listItem)
    .addFeature(placeholder, {
      text: props.placeholder,
      mode: 'block',
    })
    .addFeature(table)
    .addFeature(topBar)
    .addFeature(toolbar)
  crepe = instance
  instance.on((listener) => {
    listener.markdownUpdated((_ctx, value) => {
      if (value !== props.modelValue) emit('update:modelValue', value)
    })
  })

  try {
    await instance.create()
    createFinished = true
    if (disposed) {
      await instance.destroy()
      return
    }
    editorReady.value = true
    syncContent(props.modelValue)
    syncEditable(props.disabled)
    const editable = root.querySelector<HTMLElement>('.ProseMirror')
    if (editable) {
      editable.id = props.id
      editable.setAttribute('role', 'textbox')
      editable.setAttribute('aria-multiline', 'true')
      editable.setAttribute('aria-labelledby', props.labelledBy)
      editable.setAttribute('spellcheck', 'true')
    }
  } catch {
    createFinished = true
    if (disposed) return
    editorError.value = true
  }
})

onBeforeUnmount(() => {
  disposed = true
  if (createFinished && crepe) void crepe.destroy()
})
</script>

<template>
  <div class="rich-text-editor" :class="{ 'has-error': editorError, 'is-disabled': disabled }" :aria-disabled="disabled">
    <div
      ref="editorRoot"
      class="rich-text-editor__mount"
      :aria-busy="!editorReady && !editorError"
      @paste.capture="blockLocalImageFiles"
      @drop.capture="blockLocalImageFiles"
    ></div>
    <div v-if="!editorReady && !editorError" class="rich-text-editor__loading" role="status">正在加载正文编辑器…</div>
    <textarea
      v-if="editorError"
      :id="id"
      class="textarea rich-text-editor__fallback"
      :value="modelValue"
      :aria-labelledby="labelledBy"
      :placeholder="placeholder"
      :disabled="disabled"
      rows="18"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    ></textarea>
    <p v-if="blockedImagePaste" class="rich-text-editor__notice" role="status">正文暂不支持直接上传本地图片；可使用右侧的封面配图或正文配图功能。</p>
    <p v-if="editorError" class="rich-text-editor__notice" role="status">富文本编辑器加载失败，当前可继续使用纯文本输入框。</p>
  </div>
</template>

<style scoped>
.rich-text-editor { min-width: 0; min-height: max(420px, 54vh); overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }
.rich-text-editor:focus-within { border-color: var(--pine); box-shadow: 0 0 0 3px var(--pine-soft); }
.rich-text-editor__mount { min-height: max(420px, 54vh); }
.rich-text-editor__mount[aria-busy="true"] { display: none; }
.rich-text-editor__loading { display: grid; min-height: max(420px, 54vh); place-items: center; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.rich-text-editor__fallback { display: block; width: 100%; min-height: max(420px, 54vh); border: 0; border-radius: 0; font-size: var(--fs-body-lg); line-height: var(--lh-loose); }
.rich-text-editor__notice { margin: 0; padding: 8px 12px; border-top: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.rich-text-editor__mount :deep(.milkdown) {
  --crepe-color-background: var(--paper);
  --crepe-color-on-background: var(--text-primary);
  --crepe-color-surface: var(--paper-2);
  --crepe-color-surface-low: var(--pine-soft);
  --crepe-color-on-surface: var(--text-primary);
  --crepe-color-on-surface-variant: var(--text-secondary);
  --crepe-color-outline: var(--line);
  --crepe-color-primary: var(--pine);
  --crepe-color-secondary: var(--pine-soft);
  --crepe-color-on-secondary: var(--text-primary);
  --crepe-color-hover: var(--pine-soft);
  --crepe-color-selected: var(--pine-soft);
  --crepe-base-font-size: var(--fs-body-lg);
  font-family: inherit;
  color: var(--text-primary);
  background: var(--paper);
}
.rich-text-editor__mount :deep(.milkdown .ProseMirror) { min-height: max(420px, 54vh); padding: 20px; font-family: inherit; font-size: var(--fs-body-lg); line-height: var(--lh-loose); color: var(--text-primary); }
.rich-text-editor__mount :deep(.milkdown .ProseMirror img) { max-width: 100%; max-height: 640px; height: auto; vertical-align: middle; object-fit: contain; }
.rich-text-editor__mount :deep(.milkdown .ProseMirror:focus-visible) { outline: none; }
@media (max-width: 560px) { .rich-text-editor__mount :deep(.milkdown .ProseMirror) { padding: 16px; } }
</style>
