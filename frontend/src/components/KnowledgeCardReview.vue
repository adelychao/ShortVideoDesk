<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElButton, ElDialog } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/dialog/style/css'
import { ApiError, api } from '../api/client'
import type { ObsidianOutputDirectory, TaskArtifact } from '../api/types'
import MarkdownPreview from './MarkdownPreview.vue'
import MarkdownRichTextEditor from './MarkdownRichTextEditor.vue'

const props = defineProps<{ artifacts: TaskArtifact[] }>()
const emit = defineEmits<{ (event: 'updated'): void }>()
const selectedId = ref('')
const content = ref('')
const title = ref('')
const expectedHash = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
const loadedContent = ref('')
const loadedTitle = ref('')
const editing = ref(false)
let loadSequence = 0

const libraryDialogOpen = ref(false)
const libraryChoice = ref('')
const libraryOptions = ref<ObsidianOutputDirectory[]>([])
let libraryChoiceResolve: ((choice: string | null) => void) | null = null

const cards = computed(() => props.artifacts
  .filter((artifact) => ['knowledge_card', 'knowledge_card_revision', 'knowledge_card_approved'].includes(artifact.kind))
  .sort((left, right) => right.created_at.localeCompare(left.created_at)))
const selectedArtifact = computed(() => cards.value.find((item) => item.id === selectedId.value) ?? null)
const cardTitle = computed(() => {
  const title = selectedArtifact.value?.metadata.title
  return typeof title === 'string' && title.trim() ? title.trim() : (selectedArtifact.value?.name ?? '知识卡')
})
const hasUnsavedChanges = computed(() => content.value !== loadedContent.value || title.value.trim() !== loadedTitle.value)
const approved = computed(() => selectedArtifact.value?.kind === 'knowledge_card_approved')
const vaultNotePath = computed(() => {
  const value = selectedArtifact.value?.metadata.vault_note_path
  return typeof value === 'string' && value.trim() ? value.trim() : ''
})
const vaultLibraryName = computed(() => {
  const value = selectedArtifact.value?.metadata.vault_library_name
  return typeof value === 'string' && value.trim() ? value.trim() : ''
})
const statusLabel = computed(() => {
  if (approved.value) return '已审核'
  return selectedArtifact.value?.metadata.human_edited === true ? '人工修订 · 待审核' : '待审核'
})

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : '知识卡操作失败，请稍后重试。'
}

function titleForArtifact(artifact: TaskArtifact | undefined, markdown: string): string {
  const savedTitle = artifact?.metadata.title
  if (typeof savedTitle === 'string' && savedTitle.trim()) return savedTitle.trim()
  const heading = markdown.match(/^#[ \t]+([^\r\n]+)$/m)?.[1]?.trim()
  return heading || artifact?.name || '知识卡'
}

function contentWithTitle(markdown: string, nextTitle: string): string {
  return markdown.replace(/^(\uFEFF?#)[ \t]+[^\r\n]*$/m, (_heading, marker: string) => `${marker} ${nextTitle}`)
}

async function load(artifactId = selectedId.value, artifactOverride?: TaskArtifact): Promise<void> {
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
    const artifact = artifactOverride ?? cards.value.find((item) => item.id === artifactId)
    title.value = titleForArtifact(artifact, result.content)
    loadedTitle.value = title.value
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
  if (editing.value && hasUnsavedChanges.value && !window.confirm('当前知识卡有未保存的修改，切换会放弃这些修改。继续吗？')) return
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
  title.value = loadedTitle.value
  editing.value = false
  error.value = ''
  message.value = ''
}

async function saveRevision(): Promise<void> {
  if (!selectedArtifact.value || busy.value || !title.value.trim() || !hasUnsavedChanges.value) return
  busy.value = true
  error.value = ''
  try {
    const nextTitle = title.value.trim()
    const revision = await api.saveTextArtifactRevision(
      selectedArtifact.value.id,
      expectedHash.value,
      contentWithTitle(content.value, nextTitle),
      nextTitle,
    )
    emit('updated')
    await load(revision.id, revision)
    if (revision.vault_sync?.synced) {
      const status = revision.vault_sync.renamed ? '知识库文件内容与文件名已同步' : '知识库文件已同步'
      const path = revision.vault_sync.path ? `：${revision.vault_sync.path}` : '。'
      const reason = revision.vault_sync.reason ? `；${revision.vault_sync.reason}` : ''
      message.value = `知识卡已更新，${status}${path}${reason}`
    } else if (revision.vault_sync) {
      message.value = `知识卡已更新；${revision.vault_sync.reason || '知识库文件暂未同步。'}`
    } else {
      message.value = '知识卡已更新。'
    }
  } catch (cause) {
    error.value = safeError(cause)
  } finally {
    busy.value = false
  }
}

function chooseLibrary(options: ObsidianOutputDirectory[]): Promise<string | null> {
  libraryOptions.value = options
  libraryChoice.value = options[0]?.name ?? ''
  libraryDialogOpen.value = true
  return new Promise((resolve) => {
    libraryChoiceResolve = resolve
  })
}

function confirmLibraryChoice(): void {
  const choice = libraryChoice.value
  if (!choice) return
  const resolve = libraryChoiceResolve
  libraryChoiceResolve = null
  libraryDialogOpen.value = false
  resolve?.(choice)
}

function cancelLibraryChoice(): void {
  const resolve = libraryChoiceResolve
  libraryChoiceResolve = null
  libraryDialogOpen.value = false
  resolve?.(null)
}

function handleLibraryDialogClose(): void {
  // 关闭按钮 / ESC / 点击遮罩也视为取消选择。
  if (libraryChoiceResolve) cancelLibraryChoice()
}

async function resolveApprovalLibrary(): Promise<{ cancelled: boolean; libraryName: string | null }> {
  try {
    const configured = await api.getConfig()
    const libraries = configured.settings.obsidian_output_directories ?? []
    if (libraries.length > 1) {
      const choice = await chooseLibrary(libraries)
      if (choice === null) return { cancelled: true, libraryName: null }
      return { cancelled: false, libraryName: choice }
    }
    if (libraries.length === 1) return { cancelled: false, libraryName: libraries[0].name }
  } catch {
    // 读取设置失败时仍继续审核；无法写入时后端会返回提示。
  }
  return { cancelled: false, libraryName: null }
}

async function approve(): Promise<void> {
  if (!selectedArtifact.value || busy.value) return
  const { cancelled, libraryName } = await resolveApprovalLibrary()
  if (cancelled) return
  busy.value = true
  error.value = ''
  try {
    const approvedArtifact = await api.reviewGeneratedArtifact(selectedArtifact.value.id, expectedHash.value, libraryName)
    emit('updated')
    selectedId.value = approvedArtifact.id
    await load(approvedArtifact.id, approvedArtifact)
    const vaultWrite = approvedArtifact.vault_write
    if (vaultWrite?.written && vaultWrite.path) {
      message.value = vaultWrite.library_name
        ? `已审核并写入「${vaultWrite.library_name}」：${vaultWrite.path}`
        : `已审核并写入知识库：${vaultWrite.path}`
    }
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
      <h2 id="knowledge-card-title" class="card-title">{{ editing ? (title.trim() || '知识卡') : cardTitle }}</h2>
      <span class="status-badge" :class="{ warn: !approved }">{{ statusLabel }}</span>
    </div>
    <div v-if="editing" class="card-title-field">
      <label class="card-title-label" for="knowledge-card-title-input">标题</label>
      <input
        id="knowledge-card-title-input"
        v-model="title"
        class="input card-title-input"
        type="text"
        maxlength="160"
        required
        :disabled="busy || !selectedArtifact"
        :aria-invalid="!title.trim()"
      />
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
        <button class="button" type="button" :disabled="busy || !selectedArtifact || !title.trim() || !hasUnsavedChanges" @click="saveRevision">{{ busy ? '正在保存…' : '保存' }}</button>
        <button class="button button-secondary" type="button" :disabled="busy" @click="cancelEdit">取消</button>
        <span class="helper">标题最多 160 个字符，保存时会同步更新正文标题。</span>
      </template>
      <template v-else>
        <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="beginEdit">编辑</button>
        <button v-if="!approved" class="button" type="button" :disabled="busy || !selectedArtifact" @click="approve">审核通过</button>
        <template v-else-if="vaultNotePath">
          <span v-if="vaultLibraryName" class="status-badge">{{ vaultLibraryName }}</span>
          <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="openVaultNote(false)">打开知识库文件</button>
          <button class="button button-secondary" type="button" :disabled="busy || !selectedArtifact" @click="openVaultNote(true)">打开所在目录</button>
        </template>
        <span v-if="!approved" class="helper">审核通过后写入知识库输出目录；配置多个目录时会先请你选择。</span>
      </template>
    </div>
    <p v-if="error" class="info-box form-error" role="alert">{{ error }}</p>
    <p v-if="message" class="helper" role="status">{{ message }}</p>

    <el-dialog
      v-model="libraryDialogOpen"
      class="card-library-dialog"
      title="选择要写入的知识库"
      width="520"
      :teleported="false"
      :close-on-click-modal="false"
      @close="handleLibraryDialogClose"
    >
      <p class="library-dialog-hint">这张知识卡审核通过后将写入所选目录。</p>
      <fieldset class="library-choice-list">
        <legend class="library-choice-legend">知识库目录</legend>
        <label v-for="library in libraryOptions" :key="library.name" class="library-choice-item">
          <input v-model="libraryChoice" type="radio" name="card-vault-library" :value="library.name" />
          <span class="library-choice-name">{{ library.name }}</span>
          <span class="library-choice-directory">{{ library.directory }}</span>
        </label>
      </fieldset>
      <template #footer>
        <el-button @click="cancelLibraryChoice">取消</el-button>
        <el-button type="primary" :disabled="!libraryChoice" @click="confirmLibraryChoice">审核并写入</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.editor-panel { display: grid; gap: .9rem; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.card-title { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); color: var(--text-primary); overflow-wrap: anywhere; }
.card-title-field { display: grid; gap: 6px; min-width: 0; }
.card-title-label { color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-semibold); }
.card-title-input { min-height: 42px; padding: 9px 12px; border-color: var(--line); border-radius: 8px; background: var(--paper); color: var(--text-primary); font-size: var(--fs-body); line-height: var(--lh-base); }
.card-title-input:focus { border-color: var(--pine); box-shadow: 0 0 0 3px var(--pine-soft); }
.card-title-input:disabled { opacity: .7; cursor: not-allowed; }
.card-rich-editor { min-height: 18rem; }
.card-rich-editor.is-busy { pointer-events: none; opacity: .7; }
.card-markdown-preview { min-height: 18rem; padding: 1rem; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }

/* —— 审核通过时选择知识库目录的弹窗 —— */
.card-library-dialog :deep(.el-dialog__title) { color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.card-library-dialog :deep(.el-dialog__body) { padding-top: 8px; color: var(--text-secondary); }
.library-dialog-hint { margin: 0 0 10px; font-size: var(--fs-body); line-height: var(--lh-base); color: var(--text-secondary); }
.library-choice-list { display: grid; gap: 8px; margin: 0; padding: 0; border: 0; }
.library-choice-legend { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.library-choice-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-areas: "radio name" "radio directory";
  align-items: center;
  column-gap: 10px;
  row-gap: 2px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  cursor: pointer;
}
.library-choice-item:hover { border-color: var(--pine); }
.library-choice-item:has(input:checked) { border-color: var(--pine); background: var(--pine-soft); }
.library-choice-item input { grid-area: radio; width: 16px; height: 16px; accent-color: var(--pine); }
.library-choice-name { grid-area: name; font-size: var(--fs-body); font-weight: var(--fw-semibold); color: var(--text-primary); overflow-wrap: anywhere; }
.library-choice-directory { grid-area: directory; font-size: var(--fs-caption); color: var(--text-tertiary); overflow-wrap: anywhere; }
</style>
