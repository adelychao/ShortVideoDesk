<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, api } from '../api/client'
import type { CommentAnalysisOptions, CommentAnalysisPreview, CommentCollectionState, CommentInsights, TaskStep } from '../api/types'
import { defaultCommentAnalysisOptions, validateCommentAnalysisOptions } from '../commentAnalysisOptions'

type Section = 'learn' | 'ideas' | 'reply' | 'raw'
type Classified = NonNullable<CommentInsights['classification_page']>[number]
const props = withDefaults(defineProps<{ taskId: string; transcriptArtifactId: string; sourceUrl?: string; active?: boolean; hasAnalysis: boolean; analysisRevision?: string; analysisStep?: TaskStep; clockNow?: number; taskStatus: string; collectedCount?: number; commentState?: CommentCollectionState | null; busy?: boolean }>(), { active: true, collectedCount: 0, busy: false })
const emit = defineEmits<{ (event: 'update:selectedCommentIds', ids: string[]): void; (event: 'merged'): void; (event: 'start-analysis', options: CommentAnalysisOptions, previewRevision: string): void; (event: 'retry-analysis', id: string): void; (event: 'open-scripts'): void }>()
const section = ref<Section>('learn')
const tabs: Array<{ id: Section; title: string }> = [{ id: 'learn', title: '学习笔记' }, { id: 'ideas', title: '选题与复盘' }, { id: 'reply', title: '回复清单' }, { id: 'raw', title: '原始评论' }]
const result = ref<CommentInsights | null>(null)
const selected = ref<string[]>([])
const category = ref('选题请求')
const offset = ref(0)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const authorized = ref(false)
const options = ref(defaultCommentAnalysisOptions())
const preview = ref<CommentAnalysisPreview | null>(null)
const previewBusy = ref(false)
const previewError = ref('')
const settingsError = ref('')
const settingsLoading = ref(false)
const settingsReady = ref(false)
const configure = ref(false)
const continueOpen = ref(false)
const continueAuthorized = ref(false)
const continueBusy = ref(false)
const continueBudget = ref(0)
const continueTokens = ref(0)
const owner = ref(false)
const expanded = ref<Record<string, string>>({})
const openReply = ref<Record<string, boolean>>({})
const drafts = ref<Record<string, string>>({})
const statuses = ref<Record<string, NonNullable<Classified['response_status']>>>({})
const saving = ref<Record<string, boolean>>({})
const merging = ref(false)
let controller: AbortController | null = null
let sequence = 0
let previewSequence = 0
let settingsSequence = 0
const rows = computed(() => result.value?.classification_page ?? [])
const total = computed(() => result.value?.classification_total ?? 0)
const page = computed(() => Math.floor(offset.value / 50) + 1)
const pages = computed(() => Math.max(1, Math.ceil(total.value / 50)))
const isRunning = computed(() => ['queued', 'running'].includes(props.analysisStep?.status ?? ''))
const analysisElapsed = computed(() => {
  const start = Date.parse(props.analysisStep?.started_at ?? '')
  if (!Number.isFinite(start) || !props.clockNow) return ''
  const seconds = Math.max(0, Math.floor((props.clockNow - start) / 1000))
  return Math.floor(seconds / 60) + ' 分 ' + (seconds % 60) + ' 秒'
})
const retryable = computed(() => !!props.analysisStep && ['failed', 'interrupted', 'blocked', 'cancelled'].includes(props.analysisStep.status) && props.analysisStep.error_code !== 'comments_analysis_stale')
const canStart = computed(() => !isRunning.value && !['queued', 'running', 'awaiting_authorization'].includes(props.taskStatus))
const statusLabels: Record<string, string> = { pending: '待处理', drafted: '草稿已保存', marked_replied: '已标记回复', ignore: '暂不处理' }

async function load(): Promise<void> {
  if (!props.active || !props.hasAnalysis) return
  const current = ++sequence
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const data = await api.getCommentInsights(props.taskId, { category: category.value, limit: 50, offset: offset.value, signal: controller.signal })
    if (current !== sequence) return
    result.value = data
    for (const item of data.classification_page ?? []) {
      if (drafts.value[item.platform_comment_id] === undefined) drafts.value[item.platform_comment_id] = item.reply_draft ?? ''
      if (statuses.value[item.platform_comment_id] === undefined) statuses.value[item.platform_comment_id] = item.response_status ?? 'pending'
    }
    const valid = new Set(data.selected.map(item => item.comment_id))
    selected.value = selected.value.filter(id => valid.has(id))
    emit('update:selectedCommentIds', [...selected.value])
  } catch (cause) {
    if (current !== sequence || (cause instanceof Error && cause.name === 'AbortError')) return
    error.value = cause instanceof ApiError ? cause.message : '暂时无法读取评论分析结果。'
  } finally { if (current === sequence) loading.value = false }
}
function chooseTab(id: Section): void {
  section.value = id
  if (id === 'ideas' || id === 'reply') {
    category.value = id === 'ideas' ? '选题请求' : '观众提问'
    offset.value = 0
    void load()
  }
}
function changeCategory(): void { offset.value = 0; void load() }
function turnPage(next: number): void { if (next >= 1 && next <= pages.value && !loading.value) { offset.value = (next - 1) * 50; void load() } }
function toggle(id: string, event: Event): void {
  selected.value = (event.target as HTMLInputElement).checked ? [...new Set([...selected.value, id])] : selected.value.filter(value => value !== id)
  emit('update:selectedCommentIds', [...selected.value])
}
async function mergeIntoTranscript(): Promise<void> {
  if (!props.transcriptArtifactId || !selected.value.length || merging.value || !result.value) return
  merging.value = true; error.value = ''; notice.value = ''
  try {
    const source = await api.getTextArtifact(props.transcriptArtifactId)
    const lines = source.content.replace(/\r\n?/g, '\n').split('\n')
    const at = lines.findIndex(line => line.trim() === '## 评论反馈参考')
    const comments = result.value.selected.filter(item => selected.value.includes(item.comment_id))
    const table = comments.map((item, index) => '| ' + (index + 1) + ' | ' + (item.like_count ?? '未提供') + ' | ' + item.text.replace(/\r?\n/g, '；').replace(/\|/g, '\\|') + ' |').join('\n')
    const content = (at >= 0 ? lines.slice(0, at) : lines).join('\n').trimEnd() + '\n\n## 评论反馈参考\n\n以下公开评论仅供后续整理参考，观点和经历需核实。\n\n| 序号 | 点赞数 | 评论内容 |\n| ---: | ---: | :--- |\n' + table + '\n'
    await api.saveTextArtifactRevision(props.transcriptArtifactId, source.sha256, content)
    selected.value = []; emit('update:selectedCommentIds', []); emit('merged'); notice.value = '已生成合并版转写稿。'
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '合并评论失败。' } finally { merging.value = false }
}
async function expand(item: Classified): Promise<void> {
  try { expanded.value = { ...expanded.value, [item.platform_comment_id]: (await api.getComment(props.taskId, item.platform_comment_id)).text } }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '读取评论全文失败。' }
}
async function save(item: Classified): Promise<void> {
  const id = item.platform_comment_id
  if (!owner.value || saving.value[id]) return
  saving.value = { ...saving.value, [id]: true }; notice.value = ''
  try {
    const saved = await api.saveCommentResponseNote(props.taskId, id, { owner_authorized: true, reply_draft: drafts.value[id] ?? '', status: statuses.value[id] ?? 'pending' })
    item.reply_draft = saved.reply_draft; item.response_status = saved.status
    notice.value = '已保存本机回复记录。'
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '保存回复记录失败。' }
  finally { saving.value = { ...saving.value, [id]: false } }
}
async function copy(value: string): Promise<void> {
  try { await navigator.clipboard.writeText(value); notice.value = '已复制。' } catch { notice.value = '复制失败，请手动选择文本复制。' }
}
async function copyComment(item: Classified): Promise<void> {
  try {
    const text = expanded.value[item.platform_comment_id] ?? (item.has_more_text ? (await api.getComment(props.taskId, item.platform_comment_id)).text : item.text)
    await copy(text)
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '读取评论全文失败。' }
}
async function previewAnalysis(): Promise<void> {
  if (!settingsReady.value || validateCommentAnalysisOptions(options.value)) return
  const current = ++previewSequence
  const snapshot = JSON.stringify(options.value)
  previewBusy.value = true; previewError.value = ''
  try {
    const response = await api.previewCommentAnalysis(props.taskId, { ...options.value })
    if (current === previewSequence && snapshot === JSON.stringify(options.value)) preview.value = response
  } catch (cause) {
    if (current === previewSequence) previewError.value = cause instanceof ApiError ? cause.message : '无法估算评论范围。'
  } finally { if (current === previewSequence) previewBusy.value = false }
}
function start(): void { if (!settingsReady.value || !authorized.value || props.busy || !preview.value) return; emit('start-analysis', { ...options.value }, preview.value.preview_revision); configure.value = false }
async function loadAnalysisSettings(): Promise<void> {
  const current = ++settingsSequence
  previewSequence++; previewBusy.value = false; preview.value = null; authorized.value = false
  settingsLoading.value = true; settingsReady.value = false; settingsError.value = ''; previewError.value = ''
  try {
    const config = await api.getConfig()
    if (current !== settingsSequence) return
    const saved = config.settings.comment_analysis_options
    if (!saved) throw new Error('本机服务版本较旧，请重启工作台后读取评论分析设置。')
    const invalid = validateCommentAnalysisOptions(saved)
    if (invalid) throw new Error(invalid)
    options.value = { ...saved }; settingsReady.value = true
  } catch (cause) {
    if (current === settingsSequence) settingsError.value = cause instanceof Error ? cause.message : '无法读取评论分析设置。'
  } finally { if (current === settingsSequence) settingsLoading.value = false }
}
function openContinue(): void {
  if (!result.value?.coverage) return
  continueBudget.value = result.value.coverage.budget_cny
  continueTokens.value = result.value.coverage.token_budget
  continueAuthorized.value = false
  continueOpen.value = true
}
async function continueAnalysis(): Promise<void> {
  if (!props.analysisStep || !continueAuthorized.value || continueBusy.value) return
  continueBusy.value = true; error.value = ''
  try { await api.continueCommentAnalysis(props.taskId, props.analysisStep.id, continueBudget.value, continueTokens.value); continueOpen.value = false; emit('merged') }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '无法继续评论分析。' }
  finally { continueBusy.value = false }
}
watch(() => props.taskId, () => { previewSequence++; previewBusy.value = false; preview.value = null; configure.value = false; section.value = 'learn'; result.value = null; selected.value = []; owner.value = false; authorized.value = false; drafts.value = {}; statuses.value = {}; offset.value = 0; category.value = '选题请求' })
watch(options, () => { previewSequence++; previewBusy.value = false; preview.value = null; authorized.value = false }, { deep: true })
watch(configure, value => { if (value) void loadAnalysisSettings(); else { settingsSequence++; settingsReady.value = false; preview.value = null; authorized.value = false } })
onActivated(() => { if (configure.value) void loadAnalysisSettings() })
watch(() => [props.taskId, props.active, props.hasAnalysis, props.analysisRevision], () => { controller?.abort(); sequence++; if (props.active && props.hasAnalysis) void load() }, { immediate: true })
onBeforeUnmount(() => { controller?.abort(); sequence++; settingsSequence++; previewSequence++ })
</script>

<template>
  <section class="panel panel-pad insights" aria-labelledby="insight-title">
    <div class="insight-head"><div><h2 id="insight-title">评论洞察</h2><p class="helper">本次采集的公开评论 · 楼中楼正文以实际采集范围为准</p></div><span class="insight-badge">{{ isRunning ? '分析中' : result ? '分析结果' : loading ? '正在读取' : error ? '读取失败' : '尚未分析' }}</span></div>
    <div class="metrics"><span><strong>{{ (commentState?.collected_count ?? collectedCount).toLocaleString('zh-CN') }}</strong> 条已采集</span><span v-if="commentState?.page_count"><strong>{{ commentState.page_count }}</strong> 页</span><span v-if="result"><strong>{{ (result.processed_comment_count ?? 0).toLocaleString('zh-CN') }}</strong> 条已分析</span><span v-if="commentState?.stop_reason">{{ commentState.stop_reason }}</span></div>
    <div v-if="result?.coverage" class="metrics"><span>不同文本 {{ result.coverage.unique_text_count }}</span><span>已尝试 {{ result.coverage.attempted_original_count }} 条</span><span>未完成分析 {{ result.coverage.unanalysed_original_count }} 条</span><span>未选入候选 {{ result.coverage.not_selected_original_count }} 条</span><span>完成 {{ result.coverage.completed_batches }} / {{ result.coverage.total_batches }} 批</span><span>输入 {{ result.coverage.input_tokens }} · 输出 {{ result.coverage.output_tokens }} Token</span><span>估算 {{ result.coverage.estimated_cost_cny }} / {{ result.coverage.budget_cny }} 元{{ result.coverage.unknown_cost ? ' · 存在未知费用' : '' }}</span><span v-if="result.coverage.stop_reason">{{ result.coverage.stop_reason }}</span></div>
    <p v-if="isRunning" class="helper">{{ analysisStep?.message }}<template v-if="analysisElapsed"> · 本次分析已用 {{ analysisElapsed }}</template> · 正在展示最近保存的结果。</p>
    <button v-if="result?.coverage?.stop_reason?.includes('预算') && !result.coverage.unknown_cost && analysisStep?.status === 'partial'" class="button button-secondary button-small configure-button" type="button" @click="openContinue">调整预算并继续</button>
    <div v-if="continueOpen && result?.coverage" class="analysis-empty"><p>继续处理相同的候选与批次，已保存结果将按匹配条件复用。</p><label>总费用预算（元） <input v-model.number="continueBudget" class="input" type="number" :min="result.coverage.budget_cny" step="0.01" /></label><label>总 Token 预算 <input v-model.number="continueTokens" class="input" type="number" :min="result.coverage.token_budget" step="1000" /></label><label class="consent"><input v-model="continueAuthorized" type="checkbox" /> 我同意使用调整后的预算继续分析。</label><button class="button button-small" type="button" :disabled="!continueAuthorized || continueBusy || continueBudget < result.coverage.budget_cny || continueTokens < result.coverage.token_budget" @click="continueAnalysis">{{ continueBusy ? '正在提交…' : '确认继续' }}</button></div>
    <div class="inner-tabs" role="tablist" aria-label="评论用途">
      <button v-for="tab in tabs" :id="'comment-tab-' + tab.id" :key="tab.id" type="button" role="tab" :aria-selected="section === tab.id" :aria-controls="'comment-panel-' + tab.id" @click="chooseTab(tab.id)">{{ tab.title }}</button>
    </div>
    <p v-if="loading && !result" class="helper" role="status">正在读取评论洞察…</p>
    <div v-if="error && section !== 'raw'" class="info-box form-error" role="alert">{{ error }} <button class="text-button" type="button" @click="load">重试读取</button></div>
    <div v-if="section !== 'raw' && !result && !loading && !error" class="analysis-empty">
      <h3>{{ isRunning ? '正在分析评论' : '评论洞察尚未生成' }}</h3>
      <p v-if="isRunning">{{ analysisStep?.message || '正在处理已采集评论，完成后结果会显示在这里。' }}</p>
      <template v-else-if="retryable || analysisStep?.error_code === 'comments_analysis_stale'"><p>旧分析未完成。新分析会按新设置重新处理，旧批次不会自动复用；旧请求可能已经产生费用，请先核对调用记录。</p><button class="button button-secondary button-small" type="button" :disabled="!canStart || busy || !collectedCount" @click="configure = !configure">配置新的预算分析</button></template>
      <template v-else><p>可先估算评论范围，再确认模型、单价和预算。</p><button class="button button-secondary button-small" type="button" :disabled="!canStart || busy || !collectedCount" @click="configure = !configure">配置分析</button></template>
    </div>
    <button v-if="result && !isRunning && canStart" class="button button-secondary button-small configure-button" type="button" @click="configure = !configure">{{ configure ? '收起分析配置' : '新建分析' }}</button>
    <div v-if="configure && section !== 'raw'" class="analysis-empty">
      <p v-if="retryable || analysisStep?.error_code === 'comments_analysis_stale'">旧请求可能已经产生费用；新分析不自动复用旧批次，请先核对调用记录。</p>
      <p v-if="settingsLoading" role="status">正在读取评论分析设置…</p>
      <p v-if="settingsError" role="alert">{{ settingsError }} <button class="text-button" type="button" @click="loadAnalysisSettings">重试读取</button></p>
      <p v-if="settingsReady">使用全局设置：{{ options.mode === 'economy' ? '经济模式' : '全量模式' }} · 每批最多 {{ options.batch_size }} 条 · {{ options.mode === 'economy' ? `最多 ${options.candidate_limit} 种文本 · ` : '' }}预算 {{ options.budget_cny }} 元 / {{ options.token_budget }} Token · 每批输出最多 {{ options.max_output_tokens }} Token · 输入 / 输出单价 {{ options.input_price_per_million }} / {{ options.output_price_per_million }} 元/百万 Token。</p>
      <RouterLink class="text-button" :to="{ name: 'settings', query: { tab: 'comments' } }">前往评论分析设置 →</RouterLink>
      <button class="button button-secondary button-small" type="button" :disabled="!settingsReady || previewBusy || busy" @click="previewAnalysis">{{ previewBusy ? '正在估算…' : '预览分析范围' }}</button>
      <p v-if="previewError" role="alert">{{ previewError }} <button class="text-button" type="button" @click="previewAnalysis">重试预览</button></p>
      <p v-if="preview">{{ preview.original_count }} 条原评论 · {{ preview.unique_text_count }} 种文本 · 模型候选 {{ preview.candidate_text_count }} 种文本 / {{ preview.candidate_original_count }} 条原评论 · 预计 {{ preview.estimated_batch_count }} 批。按每批用满输出上限估算：输入约 {{ preview.estimated_input_tokens }} Token、输出最多约 {{ preview.estimated_max_output_tokens }} Token、约 {{ preview.estimated_upper_cost_cny }} 元。{{ preview.estimate_note }}</p>
      <p v-if="preview && (preview.estimated_upper_cost_cny > options.budget_cny || preview.estimated_input_tokens + preview.estimated_max_output_tokens > options.token_budget)">按上述保守估算，处理全部候选可能超过当前预算；达到预算时将保留已完成结果并停止发送。</p>
      <label class="consent"><input v-model="authorized" type="checkbox" /> 我已核对模型单价、预算和发送范围，同意本次分析。</label>
      <button class="button button-small" type="button" :disabled="!settingsReady || !authorized || !preview || busy" @click="start">{{ busy ? '正在提交…' : '开始分析' }}</button>
    </div>
    <template v-if="result">
      <div v-show="section === 'learn'" id="comment-panel-learn" role="tabpanel" aria-labelledby="comment-tab-learn" class="output">
        <div class="output-head"><div><h3>学习笔记</h3><p class="helper">将观众的问题、观点和待核实信息整理为学习线索。</p></div></div>
        <div class="summary-grid"><section><h4>观众问题</h4><ul><li v-for="value in result.audience_questions" :key="value">{{ value }}</li><li v-if="!result.audience_questions.length">暂无</li></ul></section><section><h4>主题观点</h4><ul><li v-for="value in result.themes" :key="value">{{ value }}</li><li v-if="!result.themes.length">暂无</li></ul></section><section><h4>不同看法</h4><ul><li v-for="value in result.disagreements" :key="value">{{ value }}</li><li v-if="!result.disagreements.length">暂无</li></ul></section><section><h4>待核实</h4><ul><li v-for="value in result.claims_to_verify" :key="value">{{ value }}</li><li v-if="!result.claims_to_verify.length">暂无</li></ul></section></div>
        <h4>精选原评论</h4><p class="helper">勾选可将原评论用于稿件或合并到转写参考。</p>
        <ol v-if="result.selected.length" class="items"><li v-for="item in result.selected" :key="item.comment_id"><label class="select-row"><input type="checkbox" :checked="selected.includes(item.comment_id)" @change="toggle(item.comment_id, $event)" /><span><span class="meta">{{ item.category }} · 点赞 {{ item.like_count ?? '未提供' }}</span><span class="text">{{ item.text }}</span><span class="helper">{{ item.reason }}</span></span></label></li></ol><p v-else class="helper">暂无精选评论。</p>
        <div class="actions"><button class="button button-secondary button-small" type="button" :disabled="!selected.length || !transcriptArtifactId || merging" @click="mergeIntoTranscript">{{ merging ? '正在合并…' : '合并到转写稿' }}</button><button class="button button-small" type="button" :disabled="!selected.length" @click="emit('open-scripts')">用于生成稿件</button><span class="helper">已选 {{ selected.length }} 条</span></div>
      </div>
      <div v-show="section === 'ideas'" id="comment-panel-ideas" role="tabpanel" aria-labelledby="comment-tab-ideas" class="output">
        <div class="output-head"><div><h3>选题与复盘</h3><p class="helper">根据主题线索和具体评论整理下一期选题、制作反馈。评论反馈不能单独证明播放原因。</p></div></div>
        <div v-if="result.themes.length" class="theme-list"><div v-for="value in result.themes" :key="value" class="theme-row"><span class="tag">主题线索</span><span>{{ value }}</span></div></div>
        <div v-if="result.classification_counts" class="counts"><span v-for="label in ['观众提问','选题请求','经验与观点','纠错与具体反馈','普通互动','垃圾信息','待复核']" :key="label">{{ label }} {{ result.classification_counts[label] ?? 0 }}</span></div>
        <div class="output-head"><h4>分类评论与原文</h4><label>类型 <select v-model="category" class="select-control" @change="changeCategory"><option>选题请求</option><option>纠错与具体反馈</option><option>经验与观点</option><option>观众提问</option><option>全部</option><option>普通互动</option><option>垃圾信息</option><option>待复核</option></select></label></div>
        <p v-if="result.schema_version < 2" class="helper">旧版结果没有逐条分类，可查看上方主题和精选评论。</p>
        <ol v-else-if="rows.length" class="items"><li v-for="item in rows" :key="item.platform_comment_id"><div class="meta">{{ item.labels.join(' · ') || '待复核' }} · 点赞 {{ item.like_count ?? '未提供' }}</div><p class="text">{{ expanded[item.platform_comment_id] ?? item.text }}</p><button v-if="item.has_more_text && !expanded[item.platform_comment_id]" class="text-button" type="button" @click="expand(item)">展开全文</button></li></ol><p v-else-if="!loading" class="helper">当前类型暂无评论。</p>
        <div v-if="total > 50" class="pagination"><button class="button button-secondary button-small" :disabled="page <= 1 || loading" @click="turnPage(page - 1)">上一页</button><span>第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条</span><button class="button button-secondary button-small" :disabled="page >= pages || loading" @click="turnPage(page + 1)">下一页</button></div>
        <div class="actions"><button class="button button-small" type="button" :disabled="!selected.length" @click="emit('open-scripts')">将已选评论用于稿件</button><span class="helper">请先在学习笔记中勾选精选评论。</span></div>
      </div>
      <div v-show="section === 'reply'" id="comment-panel-reply" role="tabpanel" aria-labelledby="comment-tab-reply" class="output">
        <div class="output-head"><div><h3>回复清单</h3><p class="helper">逐条整理草稿与处理状态；平台回复状态以人工核对为准。</p></div><label>类型 <select v-model="category" class="select-control" @change="changeCategory"><option>观众提问</option><option>选题请求</option><option>纠错与具体反馈</option><option>全部</option></select></label></div>
        <div class="reply-gate"><label><input v-model="owner" type="checkbox" /> 我是该作品的创作者或获授权管理者</label><span>草稿和状态保存在本机，由使用者审核并手动处理。</span><a v-if="sourceUrl && /^https?:\/\//.test(sourceUrl)" :href="sourceUrl" target="_blank" rel="noopener noreferrer">打开原作品</a></div>
        <p v-if="result.schema_version < 2" class="helper">旧版分析没有逐条分类，请重新分析后使用回复清单。</p>
        <ol v-else-if="rows.length" class="items"><li v-for="item in rows" :key="item.platform_comment_id"><div class="meta">{{ item.labels.join(' · ') || '待复核' }} · 点赞 {{ item.like_count ?? '未提供' }} · {{ statusLabels[item.response_status || 'pending'] }}</div><p class="text">{{ expanded[item.platform_comment_id] ?? item.text }}</p><button v-if="item.has_more_text && !expanded[item.platform_comment_id]" class="text-button" type="button" @click="expand(item)">展开全文</button><div class="actions"><button class="text-button" type="button" @click="openReply[item.platform_comment_id] = !openReply[item.platform_comment_id]">{{ openReply[item.platform_comment_id] ? '收起' : '整理回复' }}</button><button class="text-button" type="button" @click="copyComment(item)">复制原文</button></div><div v-if="openReply[item.platform_comment_id]" class="reply-box"><label :for="'draft-' + item.platform_comment_id">回复草稿</label><textarea :id="'draft-' + item.platform_comment_id" v-model="drafts[item.platform_comment_id]" class="input" maxlength="5000" rows="3" /><div class="actions"><label>处理状态 <select v-model="statuses[item.platform_comment_id]" class="select-control"><option value="pending">待处理</option><option value="drafted">草稿已保存</option><option value="marked_replied">已标记回复</option><option value="ignore">暂不处理</option></select></label><button class="button button-secondary button-small" :disabled="!owner || saving[item.platform_comment_id]" @click="save(item)">保存本机记录</button><button v-if="drafts[item.platform_comment_id]" class="text-button" @click="copy(drafts[item.platform_comment_id])">复制草稿</button></div></div></li></ol><p v-else-if="!loading" class="helper">当前类型暂无评论。</p>
        <div v-if="total > 50" class="pagination"><button class="button button-secondary button-small" :disabled="page <= 1 || loading" @click="turnPage(page - 1)">上一页</button><span>第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条</span><button class="button button-secondary button-small" :disabled="page >= pages || loading" @click="turnPage(page + 1)">下一页</button></div>
      </div>
    </template>
    <div v-if="section === 'raw'" id="comment-panel-raw" role="tabpanel" aria-labelledby="comment-tab-raw"><slot name="raw-comments" :active="active && section === 'raw'" /></div>
    <p v-if="notice" class="helper" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.insights { display:grid; gap:14px; min-width:0; }
.insight-head,.output-head,.actions { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px 14px; }
.insight-head h2,.output h3,.output h4 { margin:0; }
.insight-head p,.output p { margin:4px 0; }
.insight-badge,.tag { color:var(--pine); background:var(--pine-soft); border-radius:5px; padding:3px 8px; font-size:var(--fs-caption); }
.metrics,.reply-gate { display:flex; flex-wrap:wrap; align-items:center; gap:8px 20px; padding:10px 12px; background:var(--paper-2); border:1px solid var(--line); border-radius:7px; color:var(--text-secondary); }
.metrics strong { color:var(--text-primary); font-variant-numeric:tabular-nums; }
.inner-tabs { display:flex; flex-wrap:wrap; gap:4px 14px; border-bottom:1px solid var(--line); }
.inner-tabs button { min-height:43px; padding:0 9px; border:0; border-bottom:2px solid transparent; background:transparent; color:var(--text-secondary); cursor:pointer; }
.inner-tabs button[aria-selected="true"] { color:var(--pine); border-bottom-color:var(--pine); font-weight:600; }
.inner-tabs button:focus-visible { outline:2px solid var(--pine); outline-offset:2px; }
.output,.analysis-empty { display:grid; gap:12px; min-width:0; padding-top:4px; }
.analysis-empty { padding:16px; border:1px solid var(--line); border-radius:7px; background:var(--paper-2); justify-items:start; }
.analysis-empty p { margin:0; color:var(--text-secondary); }
.consent,.reply-gate label { display:flex; align-items:center; gap:6px; }
.summary-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.summary-grid section { padding:12px; background:var(--paper-2); border-radius:7px; }
.summary-grid ul { margin:8px 0 0; padding-left:20px; color:var(--text-secondary); }
.items { list-style:none; margin:0; padding:0; }
.items li,.theme-row { padding:13px 0; border-bottom:1px solid var(--line); min-width:0; }
.select-row { display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px; align-items:start; }
.select-row > span { display:grid; gap:5px; }
.meta { color:var(--text-secondary); font-size:var(--fs-caption); }
.text { display:block; margin:5px 0; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text-primary); }
.theme-row { display:flex; align-items:start; gap:10px; }
.counts { display:flex; flex-wrap:wrap; gap:6px; }
.counts span { padding:4px 8px; border:1px solid var(--line); border-radius:5px; color:var(--text-secondary); font-size:var(--fs-caption); }
.actions { justify-content:flex-start; }
.configure-button { justify-self:start; }
.output-head label,.reply-box label { color:var(--text-secondary); }
.reply-box { display:grid; gap:8px; margin-top:10px; padding:12px; border:1px solid var(--line); border-radius:7px; }
.reply-box textarea { width:100%; min-height:78px; resize:vertical; }
.pagination { display:flex; justify-content:center; align-items:center; flex-wrap:wrap; gap:8px; color:var(--text-secondary); }
@media(max-width:680px) { .summary-grid { grid-template-columns:1fr; } .inner-tabs { gap:0 4px; } .inner-tabs button { padding:0 6px; } }
</style>
