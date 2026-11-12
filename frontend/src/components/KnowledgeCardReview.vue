<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { TaskArtifact } from '../api/types'
import MarkdownPreview from './MarkdownPreview.vue'
import MarkdownRichTextEditor from './MarkdownRichTextEditor.vue'

const props = defineProps<{ artifacts: TaskArtifact[] }>()
const emit = defineEmits<{ (event: 'updated'): void }>()
const selectedId = ref('')
const content = ref('')
const expectedHash = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
const loadedContent = ref('')
const editing = ref(false)
let loadSequence = 0

const cards = computed(() => props.artifacts
  .filter((artifact) => ['knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved'].includes(artifact.kind))
  .sort((left, right) => right.created_at.localeCompare(left.created_at)))
const selectedArtifact = computed(() => cards.value.find((item) => item.id === selectedId.value) ?? null)
const cardTitle = computed(() => {
  const title = selectedArtifact.value?.metadata.title
  return typeof title === 'string' && title.trim() ? title.trim() : (selectedArtifact.value?.name ?? '知识卡')
})
const approved = computed(() => selectedArtifact.value?.kind === 'knowledge_card_approved')
const vaultNotePath = computed(() => {
  const value = selectedArtifact.value?.metadata.vault_note_path
  return typeof value === 'string' && value.trim() ? value.trim() : ''
})
const statusLabel = computed(() => {
  if (approved.value) return '已审核'
  return selectedArtifact.value?.metadata.human_edited === true ? '人工修订 · 待审核' : '待审核'
})

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '知识卡操作失败，请稍后重试。'
}

async function load(artifactId = selectedId.value): Promise<void> {
  if (!artifactId) return
  const sequence = ++loadSequence
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api.getTextArtifact(artifactId)
    if (sequence !== loadSequence) return
    selectedId.value = artifactId
    content.value = result.content
    loadedContent.value = result.content
    expectedHash.value = result.sha256
    editing.value = false
  } catch (cause) {
    if (sequence === loadSequence) error.value = safeError(cause)
  } finally {
    if (sequence === loadSequence) busy.value = false
  }
}

async function openArtifact(id: string): Promise<void> {
  if (!cards.value.some(item => item.id === id)) return
  if (id === selectedId.value) return
  if (editing.value && content.value !== loadedContent.value && !window.confirm('当前知识卡有未保存的修改，切换会放弃这些修改。继续吗？')) return
  await load(id)
}
defineExpose({ openArtifact })

function beginEdit(): void {
  editing.value = true
  error.value = ''
  message.value = ''
}

function cancelEdit(): void {
  content.value = loadedContent.value
  editing.value = false
  error.value = ''
  message.value = ''
}

async function saveRevision(): Promise<void> {
  if (!selectedArtifact.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const revision = await api.saveTextArtifactRevision(selectedArtifact.value.id, expectedHash.value, content.value)
    emit('updated')
    await load(revision.id)
    message.value = '知识卡已更新。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

async function approve(): Promise<void> {
  if (!selectedArtifact.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const approvedArtifact = await api.reviewGeneratedArtifact(selectedArtifact.value.id, expectedHash.value)
    emit('updated')
    selectedId.value = approvedArtifact.id
    await load(approvedArtifact.id)
    const vaultWrite = approvedArtifact.vault_write
    if (vaultWrite?.written && vaultWrite.path) message.value = `已审核并写入知识库：${vaultWrite.path}`
    else if (vaultWrite?.reason) message.value = `已审核；${vaultWrite.reason}`
    else message.value = '已审核通过。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

async function openVaultNote(reveal: boolean): Promise<void> {
  if (!selectedArtifact.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.openVaultNote(selectedArtifact.value.id, reveal)
    message.value = reveal ? '已在文件管理器中定位知识库笔记。' : '已用系统默认程序打开知识库笔记。'
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

watch(cards, (items) => {
  if (!items.some((item) => item.id === selectedId.value)) {
    const next = items[0]?.id ?? ''
    selectedId.value = next
    if (next) void load(next)
  }
}, { immediate: true })
</script>

<template>
  <section v-if="cards.length" class="panel panel-pad editor-panel" aria-labelledby="knowledge-card-title">
    <div class="card-head">
      <h2 id="knowledge-card-title" class="card-title">{{ cardTitle }}</h2>
      <span class="status-badge" :class="{ warn: !approved }">{{ statusLabel }}</span>
    </div>
    <MarkdownPreview v-if="!editing" class="card-markdown-preview" :content="content || '这张知识卡目前没有正文。'" aria-label="知识卡 Markdown 预览" />
    <MarkdownRichTextEditor
      v-else
      v-model="content"
      class="card-rich-editor"
      :class="{ 'is-busy': busy || !selectedArtifact }"
      id="knowledge-card-editor"
      labelled-by="knowledge-card-title"
      placeholder="编辑知识卡正文…"
    />
    <div class="button-row">
      <template v-if="editing">
        <button class="button" type="button" :disabled="busy || !selectedArtifact || content === loadedContent" @click="saveRevision">{{ busy ? '正在保存…' : '保存' }}</button>
        <button class="button button-secondary" type="button" :disabled="busy" @click="cancelEdit">取消</button>
        <span class="helper">保存会直接更新这张知识卡。</span>
      </template>
      <template v-else>
        <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="beginEdit">编辑</button>
        <button v-if="!approved" class="button" type="button" :disabled="busy || !selectedArtifact" @click="approve">审核通过</button>
        <template v-else-if="vaultNotePath">
          <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="openVaultNote(false)">打开知识库文件</button>
          <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="openVaultNote(true)">打开所在目录</button>
        </template>
        <span v-if="!approved" class="helper">审核通过后自动写入预设的知识库目录。</span>
      </template>
    </div>
    <p v-if="error" class="info-box form-error" role="alert">{{ error }}</p>
    <p v-if="message" class="helper" role="status">{{ message }}</p>
  </section>
</template>

<style scoped>
.editor-panel { display: grid; gap: .9rem; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.card-title { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); color: var(--text-primary); overflow-wrap: anywhere; }
.card-rich-editor { min-height: 18rem; }
.card-rich-editor.is-busy { pointer-events: none; opacity: .7; }
.card-markdown-preview { min-height: 18rem; padding: 1rem; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }
</style>
