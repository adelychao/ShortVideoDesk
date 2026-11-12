<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import type { RouteLocationRaw } from 'vue-router'
import { ApiError, api } from '../api/client'
import PlatformLogo from '../components/PlatformLogo.vue'
import type { ArticlePublication, ArticleRecord } from '../api/types'

const route = useRoute()
const router = useRouter()
// 2026-11-10 21:41:04 zhw：URL 目标 ID 锁定本次提交；无 ID 时按当前版本、平台和账号取最新记录，不猜提交批次。
const articleId = String(route.params.articleId || '')
const article = ref<ArticleRecord | null>(null)
const selectedPublicationId = ref('')
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const notice = ref('')
const lastRefreshedAt = ref('')
let pageActive = false
let requestId = 0
let refreshTimer: number | undefined

const requestedPublicationIds = computed(() => {
  const raw = route.query.publications
  const values = Array.isArray(raw) ? raw : [raw]
  return [...new Set(values.flatMap((value) => String(value || '').split(',').map((id) => id.trim()).filter(Boolean)))]
})
const hasExplicitPublicationScope = computed(() => requestedPublicationIds.value.length > 0)
const latestCurrentVersionPublications = computed(() => {
  const currentArticle = article.value
  if (!currentArticle) return []

  const latestByTarget = new Map<string, ArticlePublication>()
  const ordered = [...currentArticle.publications].sort((left, right) => (
    right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id)
  ))
  for (const publication of ordered) {
    if (publication.article_version !== currentArticle.version) continue
    const targetKey = JSON.stringify([publication.platform_id, publication.account_id])
    if (!latestByTarget.has(targetKey)) latestByTarget.set(targetKey, publication)
  }
  return [...latestByTarget.values()]
})
const publications = computed(() => {
  if (!hasExplicitPublicationScope.value) return latestCurrentVersionPublications.value
  const available = new Map((article.value?.publications || []).map((publication) => [publication.id, publication]))
  return requestedPublicationIds.value
    .map((id) => available.get(id))
    .filter((publication): publication is ArticlePublication => Boolean(publication))
})
const missingPublicationCount = computed(() => Math.max(0, requestedPublicationIds.value.length - publications.value.length))
const selectedPublication = computed(() => publications.value.find((publication) => publication.id === selectedPublicationId.value) || publications.value[0] || null)
const newerPublication = computed(() => {
  const selected = selectedPublication.value
  if (!selected) return null
  const latest = (article.value?.publications || []).find((item) => item.platform_id === selected.platform_id
    && item.account_id === selected.account_id && item.article_version === selected.article_version)
  return latest && latest.id !== selected.id && latest.created_at >= selected.created_at ? latest : null
})
const totalTargets = computed(() => publications.value.length)
const confirmedCount = computed(() => publications.value.filter((publication) => (
  publication.status === 'published' || publication.status === 'manual_published'
)).length)
const progressPercent = computed(() => totalTargets.value ? Math.round(confirmedCount.value * 100 / totalTargets.value) : 0)
const statusCounts = computed(() => ({
  published: confirmedCount.value,
  processing: publications.value.filter((publication) => publication.status === 'publishing').length,
  draft: publications.value.filter((publication) => publication.status === 'draft_saved').length,
  manual: publications.value.filter((publication) => publication.status === 'manual_required').length,
  unknown: publications.value.filter((publication) => publication.status === 'unknown').length,
  failed: publications.value.filter((publication) => publication.status === 'failed').length,
}))
const taskStateLabel = computed(() => {
  if (!totalTargets.value) return '暂无目标'
  if (statusCounts.value.processing > 0 || publications.value.some(hasPollableWork)) return '进行中'
  if (statusCounts.value.manual + statusCounts.value.unknown + statusCounts.value.failed > 0) return '需要处理'
  return '已结束'
})
const taskStateTone = computed(() => {
  if (taskStateLabel.value === '进行中') return 'is-info'
  if (taskStateLabel.value === '需要处理') return 'is-warning'
  return taskStateLabel.value === '已结束' ? 'is-success' : 'is-neutral'
})
const taskStateMessage = computed(() => {
  if (!totalTargets.value) return hasExplicitPublicationScope.value
    ? '没有找到这次提交的发布目标。'
    : '当前文章版本还没有发布记录。'
  if (taskStateLabel.value === '进行中') return '平台结果正在逐项更新。'
  if (taskStateLabel.value === '需要处理') return '请按平台状态继续操作或核实发布结果。'
  if (taskStateLabel.value === '已结束') return hasExplicitPublicationScope.value
    ? '本次提交的目标均已完成或已保存为草稿。'
    : '当前文章版本各账号的最新记录均已完成或保存为草稿。'
  return '当前发布记录的状态正在整理。'
})
const articleTitle = computed(() => article.value?.document.title || article.value?.title || '未命名文章')
const submittedAt = computed(() => {
  const dates = publications.value.map((publication) => publication.created_at).filter(Boolean).sort()
  return dates[0] || ''
})
const mostRecentUpdate = computed(() => {
  const dates = publications.value.map((publication) => publication.updated_at).filter(Boolean).sort()
  return dates[dates.length - 1] || ''
})
const displayedTimestamp = computed(() => hasExplicitPublicationScope.value ? submittedAt.value : mostRecentUpdate.value)
const displayedTimestampLabel = computed(() => hasExplicitPublicationScope.value ? '提交于' : '最新状态更新于')
const hasCover = computed(() => Boolean(article.value?.document.cover_image_path))
const statusItems = computed(() => [
  { label: '已发布', count: statusCounts.value.published, tone: 'is-success' },
  { label: '处理中', count: statusCounts.value.processing, tone: 'is-info' },
  { label: '草稿', count: statusCounts.value.draft, tone: 'is-neutral' },
  { label: '待手动', count: statusCounts.value.manual, tone: 'is-warning' },
  { label: '待核实', count: statusCounts.value.unknown, tone: 'is-warning' },
  { label: '失败', count: statusCounts.value.failed, tone: 'is-danger' },
])

const platformNames: Record<string, string> = {
  wechat: '微信公众号',
  csdn: 'CSDN',
  zhihu: '知乎',
  xiaohongshu: '小红书',
  toutiao: '今日头条',
  baijiahao: '百家号',
  weibo: '微博',
}
const manualPlatformIds = new Set(['baijiahao'])

function platformName(platformId: string): string {
  return platformNames[platformId] || platformId
}

function statusLabel(status: ArticlePublication['status']): string {
  return ({
    draft_saved: '已存入草稿箱',
    published: '已发布',
    publishing: '平台处理中',
    failed: '发布失败',
    unknown: '结果待核实',
    manual_required: '待手动发布',
    manual_published: '已发布',
  } as Record<ArticlePublication['status'], string>)[status]
}

function statusTone(status: ArticlePublication['status']): string {
  if (status === 'published' || status === 'manual_published') return 'is-success'
  if (status === 'failed') return 'is-danger'
  if (status === 'publishing') return 'is-info'
  if (status === 'manual_required' || status === 'unknown') return 'is-warning'
  return 'is-neutral'
}

function publicationMessage(publication: ArticlePublication): string {
  if (publication.message.trim()) return publication.message.trim()
  return ({
    draft_saved: '平台草稿已保存。',
    published: '平台已确认发布成功。',
    publishing: '平台正在处理本次提交。',
    failed: '本次发布失败，可仅重试此目标。',
    unknown: '暂未取得最终回执，请先核对平台后台。',
    manual_required: '请前往平台完成发布，再回来确认结果。',
    manual_published: '已记录为手动发布成功。',
  } as Record<ArticlePublication['status'], string>)[publication.status]
}

function fanNotificationLabel(status: ArticlePublication['fan_notification_status']): string {
  return ({
    not_requested: '未启用粉丝通知',
    waiting_publication: '等待文章发表',
    pending: '等待提交群发',
    sending: '群发处理中',
    sent: '已通知粉丝',
    failed: '粉丝通知失败',
    unknown: '粉丝通知结果待核实',
  } as Record<ArticlePublication['fan_notification_status'], string>)[status]
}

function fanNotificationTone(status: ArticlePublication['fan_notification_status']): string {
  if (status === 'sent') return 'is-success'
  if (status === 'failed') return 'is-danger'
  if (status === 'not_requested') return 'is-neutral'
  if (status === 'unknown') return 'is-warning'
  return 'is-info'
}

function hasPollableWork(publication: ArticlePublication): boolean {
  return publication.status === 'publishing'
    || publication.fan_notification_status === 'pending'
    || publication.fan_notification_status === 'sending'
    || (publication.fan_notification_status === 'unknown' && Boolean(publication.fan_notification_remote_id))
}

function shouldAutoRefresh(publication: ArticlePublication): boolean {
  return publication.status !== 'unknown' && hasPollableWork(publication)
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ')
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date)
}

function fanNotificationIsRequested(publication: ArticlePublication): boolean {
  return publication.fan_notification_status !== 'not_requested'
}

function applyPublication(updated: ArticlePublication): void {
  if (!article.value) return
  article.value = {
    ...article.value,
    publications: article.value.publications.map((publication) => (
      publication.id === updated.id ? updated : publication
    )),
  }
}

function stopPolling(): void {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
  refreshTimer = undefined
}

function startPolling(): void {
  stopPolling()
  if (!pageActive || !publications.value.some(shouldAutoRefresh)) return
  refreshTimer = window.setInterval(() => { void refreshPending(false) }, 5000)
}

async function refreshPending(includeWechatUnknown: boolean): Promise<void> {
  if (refreshing.value || !article.value) return
  const pending = publications.value.filter((publication) => (
    shouldAutoRefresh(publication)
    || (includeWechatUnknown && publication.platform_id === 'wechat' && publication.status === 'unknown')
  ))
  if (!pending.length) {
    lastRefreshedAt.value = new Date().toISOString()
    startPolling()
    return
  }
  refreshing.value = true
  error.value = ''
  try {
    for (const publication of pending) {
      const updated = await api.refreshArticlePublication(articleId, publication.id)
      if (!pageActive) return
      applyPublication(updated)
    }
    lastRefreshedAt.value = new Date().toISOString()
    notice.value = '已刷新可查询的平台状态。'
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '刷新发布状态失败，请稍后重试。'
  } finally {
    refreshing.value = false
    startPolling()
  }
}

async function loadTask(): Promise<void> {
  pageActive = true
  const currentRequestId = ++requestId
  loading.value = !article.value
  error.value = ''
  notice.value = ''
  try {
    const result = await api.getArticle(articleId)
    if (!pageActive || currentRequestId !== requestId) return
    article.value = result
    if (!publications.value.some((publication) => publication.id === selectedPublicationId.value)) {
      selectedPublicationId.value = publications.value[0]?.id || ''
    }
    lastRefreshedAt.value = new Date().toISOString()
  } catch (cause) {
    if (!pageActive || currentRequestId !== requestId) return
    error.value = cause instanceof ApiError ? cause.message : '无法读取文章发布任务，请稍后重试。'
  } finally {
    if (pageActive && currentRequestId === requestId) {
      loading.value = false
      startPolling()
    }
  }
}

function publishTargetLocation(publication: ArticlePublication): RouteLocationRaw {
  const query: Record<string, string> = { focusPlatform: publication.platform_id }
  if (publication.account_id) query.focusAccount = publication.account_id
  if (publication.status === 'failed') query.retryPublication = publication.id
  return { name: 'article-publish', params: { articleId }, query }
}

function processHistory(publication: ArticlePublication): Array<{ label: string; at: string; message?: string }> {
  const history: Array<{ label: string; at: string; message?: string }> = [
    { label: '发布任务已提交', at: publication.created_at },
  ]
  if (publication.updated_at && publication.updated_at !== publication.created_at) {
    history.unshift({
      label: '状态更新：' + statusLabel(publication.status),
      at: publication.updated_at,
      message: publication.message || undefined,
    })
  }
  return history
}

function publicationMethod(publication: ArticlePublication): string {
  if (publication.platform_id === 'xiaohongshu') {
    return ['manual_required', 'manual_published'].includes(publication.status) ? '手动发布' : '历史浏览器提交'
  }
  if (publication.platform_id === 'csdn') {
    return ['manual_required', 'manual_published'].includes(publication.status) ? '手动发布' : '浏览器发表'
  }
  if (publication.platform_id === 'toutiao') {
    return ['manual_required', 'manual_published'].includes(publication.status) ? '手动发布' : '浏览器发表'
  }
  return manualPlatformIds.has(publication.platform_id) || publication.status === 'manual_published'
    ? '手动发布'
    : '平台接口'
}

function statusActionLabel(publication: ArticlePublication): string {
  if (publication.status === 'failed') return '仅重试此目标'
  if (publication.status === 'manual_required') return '继续手动发布'
  if (publication.status === 'unknown') return '前往核实结果'
  return '返回发布页面'
}

onActivated(() => { void loadTask() })
onDeactivated(() => {
  pageActive = false
  requestId += 1
  stopPolling()
})
onBeforeUnmount(() => {
  pageActive = false
  requestId += 1
  stopPolling()
})
</script>

<template>
  <section class="article-publish-task-page" aria-label="文章发布任务执行">
    <div class="task-page-heading">
      <div class="task-page-heading-copy">
        <RouterLink class="task-back-link" :to="{ name: 'articles' }">← 返回文章管理</RouterLink>
        <h1>文章发布任务</h1>
        <p v-if="publications.length" class="task-page-meta">
          {{ displayedTimestampLabel }} {{ formatDate(displayedTimestamp) }}　·　{{ hasExplicitPublicationScope ? '定稿版本' : '当前版本' }} v{{ publications[0].article_version }}　·　{{ totalTargets }} 个发布目标
        </p>
      </div>
      <div class="task-page-actions">
        <RouterLink class="button button-secondary task-button" :to="{ name: 'article-edit', params: { articleId } }">返回文章</RouterLink>
        <button class="button task-button" type="button" :disabled="loading || refreshing || !publications.length" @click="refreshPending(true)">
          {{ refreshing ? '正在刷新…' : '↻ 刷新状态' }}
        </button>
      </div>
    </div>

    <p v-if="error" class="task-alert is-danger" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="task-alert is-info" role="status">{{ notice }}</p>
    <p v-if="missingPublicationCount" class="task-alert is-warning" role="status">
      有 {{ missingPublicationCount }} 个目标记录暂时不可读取；下方只显示已找到的本次提交记录。
    </p>

    <div v-if="loading && !article" class="task-loading task-card" role="status">正在读取文章发布任务…</div>
    <div v-else-if="!article && !error" class="task-loading task-card">没有找到这篇文章。</div>
    <div v-else-if="article && !publications.length" class="task-empty task-card">
      <h2>{{ hasExplicitPublicationScope ? '没有找到本次提交的发布目标' : '当前文章版本没有发布记录' }}</h2>
      <p>{{ hasExplicitPublicationScope ? '请返回文章管理，再从定稿文章重新进入发布流程。' : '完成发布后，可从文章列表重新打开此进度页。' }}</p>
      <RouterLink v-if="hasExplicitPublicationScope" class="button task-button" :to="{ name: 'articles' }">返回文章管理</RouterLink>
      <RouterLink v-else class="button task-button" :to="{ name: 'article-publish', params: { articleId } }">去发布</RouterLink>
    </div>
    <template v-else-if="article && publications.length">
      <div class="task-alert is-success" role="status">
        <span class="task-alert-mark" aria-hidden="true">✓</span>
        <span>{{ hasExplicitPublicationScope ? `已提交 ${totalTargets} 个发布目标，后台结果按平台账号逐项更新。任务记录会保留在文章发布历史中。` : '当前文章版本各账号的最新发布记录，状态按平台账号逐项展示。' }}</span>
      </div>

      <section class="task-overview task-card" aria-label="任务总体信息">
        <div class="task-overview-article">
          <div class="task-overview-label">发布文章</div>
          <div class="task-article-info">
            <img
              v-if="hasCover"
              class="task-article-cover"
              :src="api.articleCoverUrl(articleId) + '?v=' + article.version"
              alt="文章封面"
            />
            <div v-else class="task-article-cover task-cover-placeholder" aria-hidden="true">文</div>
            <div class="task-article-copy">
              <strong>{{ articleTitle }}</strong>
              <span>{{ hasExplicitPublicationScope ? '定稿版本' : '当前版本' }} v{{ publications[0].article_version }}　·　{{ totalTargets }} 个发布目标</span>
              <span>{{ hasExplicitPublicationScope ? '提交时间' : '最新状态更新' }} {{ formatDate(displayedTimestamp) }}</span>
            </div>
          </div>
        </div>

        <div class="task-overview-progress">
          <div class="task-overview-label-row">
            <span class="task-overview-label">总体发布进度</span>
            <strong>{{ confirmedCount }} <small>/ {{ totalTargets }} 个目标已确认发布</small></strong>
          </div>
          <div
            class="task-progress-track"
            role="progressbar"
            aria-label="已确认发布的目标占比"
            :aria-valuenow="progressPercent"
            aria-valuemin="0"
            aria-valuemax="100"
          ><span :style="{ width: progressPercent + '%' }"></span></div>
          <p>仅统计平台已确认发布的目标；草稿、处理中、待手动和待核实结果不计入。</p>
          <div class="task-status-counts">
            <span v-for="item in statusItems" :key="item.label" :class="item.tone">{{ item.label }} <b>{{ item.count }}</b></span>
          </div>
        </div>

        <div class="task-overview-state">
          <span class="task-overview-label">{{ hasExplicitPublicationScope ? '本次任务状态' : '当前发布状态' }}</span>
          <div class="task-state-title">
            <span class="task-state-badge" :class="taskStateTone"><i></i>{{ taskStateLabel }}</span>
            <strong>{{ taskStateMessage }}</strong>
          </div>
          <p>每个平台独立执行与记录；部分成功不会影响其他目标。</p>
          <div class="task-updated-at"><i></i>{{ refreshing ? '正在刷新平台状态…' : '最近刷新 ' + formatDate(lastRefreshedAt || mostRecentUpdate) }}</div>
        </div>
      </section>

      <div class="task-main-grid">
        <section class="task-targets task-card" aria-label="各平台发布状态">
          <header class="task-section-head">
            <h2>平台发布进度 <small>{{ totalTargets }} 个{{ hasExplicitPublicationScope ? '已选目标' : '最新目标记录' }}</small></h2>
            <button class="button button-secondary task-button" type="button" :disabled="refreshing" @click="refreshPending(true)">
              {{ refreshing ? '刷新中…' : '↻ 刷新进度' }}
            </button>
          </header>
          <div class="task-target-table-head" aria-hidden="true">
            <span>平台 / 账号</span><span>当前状态</span><span>最新进展</span><span></span>
          </div>
          <button
            v-for="publication in publications"
            :key="publication.id"
            class="task-target-row"
            :class="{ 'is-selected': selectedPublication?.id === publication.id }"
            type="button"
            :aria-pressed="selectedPublication?.id === publication.id"
            @click="selectedPublicationId = publication.id"
          >
            <span class="task-target-account">
              <PlatformLogo class="task-platform-logo" :platform-id="publication.platform_id" :name="platformName(publication.platform_id)" />
              <span><strong>{{ platformName(publication.platform_id) }}</strong><small>{{ publication.account_name || platformName(publication.platform_id) + '发布目标' }}</small></span>
            </span>
            <span class="task-status-pill" :class="statusTone(publication.status)"><i></i>{{ statusLabel(publication.status) }}</span>
            <span class="task-target-latest">
              <strong>{{ publicationMessage(publication) }}</strong>
              <small>{{ formatDate(publication.updated_at) }}</small>
            </span>
            <span class="task-target-link">{{ selectedPublication?.id === publication.id ? '当前查看' : '查看详情 ›' }}</span>
          </button>
          <p class="task-list-foot">平台没有提供可核验的上传百分比时显示当前阶段；待核实目标请先确认平台结果，再决定是否重试。</p>
        </section>

        <aside v-if="selectedPublication" class="task-inspector task-card" aria-label="发布目标详情">
          <header class="task-inspector-head">
            <div>
              <h2>目标详情</h2>
              <p>当前查看：{{ platformName(selectedPublication.platform_id) }} · {{ selectedPublication.account_name || '默认发布目标' }}</p>
            </div>
            <span class="task-status-pill" :class="statusTone(selectedPublication.status)"><i></i>{{ statusLabel(selectedPublication.status) }}</span>
          </header>
          <div class="task-inspector-body">
            <div class="task-detail-platform">
              <PlatformLogo class="task-platform-logo task-platform-logo-large" :platform-id="selectedPublication.platform_id" :name="platformName(selectedPublication.platform_id)" />
              <div><strong>{{ platformName(selectedPublication.platform_id) }}</strong><small>{{ selectedPublication.account_name || '默认发布目标' }}</small></div>
            </div>

            <div
              class="task-detail-message"
              :class="statusTone(selectedPublication.status)"
              role="status"
            >
              <strong>{{ selectedPublication.status === 'failed' ? '失败原因' : selectedPublication.status === 'unknown' ? '核实提示' : selectedPublication.status === 'manual_required' ? '待完成操作' : '最新平台信息' }}</strong>
              <p>{{ publicationMessage(selectedPublication) }}</p>
              <p v-if="selectedPublication.status === 'unknown'">请先在平台后台确认是否已发布；未确认前不要重复提交，避免产生重复文章。</p>
            </div>
            <div v-if="newerPublication" class="task-detail-message is-info" role="status">
              <strong>该目标有后续任务</strong>
              <p>后续任务当前状态：{{ statusLabel(newerPublication.status) }}。</p>
              <RouterLink :to="{ name: 'article-publish-task', params: { articleId }, query: { publications: newerPublication.id } }">查看最新任务 →</RouterLink>
            </div>

            <section class="task-detail-section">
              <h3>发布信息</h3>
              <div class="task-info-row"><span>发布方式</span><strong>{{ publicationMethod(selectedPublication) }}</strong></div>
              <div class="task-info-row"><span>文章版本</span><strong>定稿 v{{ selectedPublication.article_version }}</strong></div>
              <div class="task-info-row"><span>开始时间</span><strong>{{ formatDate(selectedPublication.created_at) }}</strong></div>
              <div class="task-info-row"><span>最近更新</span><strong>{{ formatDate(selectedPublication.updated_at) }}</strong></div>
            </section>

            <section v-if="fanNotificationIsRequested(selectedPublication)" class="task-detail-section">
              <h3>粉丝通知</h3>
              <div class="task-fan-state" :class="fanNotificationTone(selectedPublication.fan_notification_status)">
                <span>{{ fanNotificationLabel(selectedPublication.fan_notification_status) }}</span>
                <p v-if="selectedPublication.fan_notification_message">{{ selectedPublication.fan_notification_message }}</p>
              </div>
            </section>

            <section class="task-detail-section">
              <h3>处理过程</h3>
              <ol class="task-timeline">
                <li v-for="(event, index) in processHistory(selectedPublication)" :key="event.at + event.label" :class="{ 'is-current': index === 0 }">
                  <span class="task-timeline-dot"></span>
                  <div><strong>{{ event.label }}</strong><p v-if="event.message">{{ event.message }}</p><time>{{ formatDate(event.at) }}</time></div>
                </li>
              </ol>
            </section>

            <section class="task-detail-section">
              <h3>平台回执</h3>
              <div class="task-receipt">
                <strong>{{ selectedPublication.remote_id ? '已取得平台作品编号' : statusLabel(selectedPublication.status) }}</strong>
                <p>{{ publicationMessage(selectedPublication) }}</p>
                <p v-if="selectedPublication.remote_id" class="task-remote-id">作品 ID：{{ selectedPublication.remote_id }}</p>
                <a v-if="selectedPublication.remote_url" :href="selectedPublication.remote_url" target="_blank" rel="noopener noreferrer">打开平台内容 ↗</a>
              </div>
            </section>

            <div class="task-detail-actions">
              <RouterLink
                v-if="['failed', 'manual_required', 'unknown'].includes(selectedPublication.status)"
                class="button task-button"
                :to="publishTargetLocation(selectedPublication)"
              >{{ statusActionLabel(selectedPublication) }}</RouterLink>
              <RouterLink
                v-else
                class="button button-secondary task-button"
                :to="publishTargetLocation(selectedPublication)"
              >返回发布页面</RouterLink>
              <RouterLink class="button button-secondary task-button" :to="{ name: 'article-edit', params: { articleId } }">返回文章</RouterLink>
            </div>
          </div>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
.article-publish-task-page { display: grid; gap: 13px; min-width: 0; }
.task-page-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 0; }
.task-page-heading-copy { min-width: 0; }
.task-back-link { display: inline-block; margin-bottom: 6px; color: var(--text-secondary); font-size: var(--fs-body-sm); }
.task-back-link:hover { color: var(--pine); }
.task-page-heading h1 { margin: 0 0 4px; color: var(--text-primary); font-size: var(--fs-title-2xl); font-weight: var(--fw-semibold); line-height: var(--lh-tight); }
.task-page-meta { margin: 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); font-variant-numeric: tabular-nums; }
.task-page-actions { display: flex; flex: none; gap: 8px; }
.task-button { min-height: 36px; padding-inline: 12px; font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.task-card { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 7px; background: var(--paper); box-shadow: var(--shadow); }
.task-alert { display: flex; align-items: center; gap: 9px; min-height: 42px; margin: 0; padding: 9px 12px; border: 1px solid var(--line); border-radius: 6px; color: var(--text-secondary); background: var(--paper); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-alert.is-success { border-color: color-mix(in srgb, var(--success) 22%, var(--line)); color: var(--success); background: var(--success-soft); }
.task-alert.is-info { border-color: color-mix(in srgb, var(--pine) 22%, var(--line)); color: var(--pine); background: var(--pine-soft); }
.task-alert.is-warning { border-color: color-mix(in srgb, var(--warning) 24%, var(--line)); color: var(--warning); background: var(--warning-soft); }
.task-alert.is-danger { border-color: color-mix(in srgb, var(--danger) 25%, var(--line)); color: var(--danger); background: var(--danger-soft); }
.task-alert-mark { display: grid; width: 20px; height: 20px; flex: none; place-items: center; border-radius: 50%; background: var(--success); color: var(--text-on-accent); font-size: var(--fs-caption); font-weight: var(--fw-semibold); }
.task-loading, .task-empty { padding: 28px; color: var(--text-secondary); font-size: var(--fs-body); }
.task-empty h2 { margin: 0 0 8px; color: var(--text-primary); font-size: var(--fs-title-xl); }
.task-empty p { margin: 0 0 16px; color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.task-overview { display: grid; grid-template-columns: minmax(270px, 1.12fr) minmax(360px, 1.42fr) minmax(260px, 1fr); }
.task-overview > div { min-width: 0; padding: 15px 18px; }
.task-overview > div + div { border-left: 1px solid var(--line); }
.task-overview-label { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.task-article-info { display: flex; align-items: center; gap: 12px; margin-top: 9px; }
.task-article-cover { width: 92px; height: 66px; flex: none; border: 1px solid var(--line); border-radius: 6px; object-fit: cover; background: var(--paper-2); }
.task-cover-placeholder { display: grid; place-items: center; color: var(--pine); background: var(--pine-soft); font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.task-article-copy { display: grid; gap: 4px; min-width: 0; }
.task-article-copy strong { display: -webkit-box; overflow: hidden; color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); line-height: var(--lh-tight); -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.task-article-copy span { color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.task-overview-label-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.task-overview-label-row > strong { color: var(--text-primary); font-size: var(--fs-title-xl); font-variant-numeric: tabular-nums; }
.task-overview-label-row small { color: var(--text-tertiary); font-size: var(--fs-body-sm); font-weight: var(--fw-regular); }
.task-progress-track { display: block; height: 8px; overflow: hidden; margin: 10px 0 7px; border-radius: 99px; background: var(--paper-2); }
.task-progress-track span { display: block; height: 100%; border-radius: inherit; background: var(--pine); transition: width .2s ease; }
.task-overview-progress > p, .task-overview-state > p { margin: 0; color: var(--text-tertiary); font-size: var(--fs-caption); line-height: var(--lh-base); }
.task-status-counts { display: flex; flex-wrap: wrap; gap: 5px 14px; margin-top: 7px; color: var(--text-secondary); font-size: var(--fs-caption); }
.task-status-counts b { margin-left: 2px; color: var(--text-primary); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
.task-status-counts .is-success { color: var(--success); }
.task-status-counts .is-info { color: var(--pine); }
.task-status-counts .is-warning { color: var(--warning); }
.task-status-counts .is-danger { color: var(--danger); }
.task-overview-state { display: grid; align-content: start; gap: 6px; }
.task-state-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.task-state-title > strong { min-width: 0; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-medium); }
.task-state-badge, .task-status-pill { display: inline-flex; align-items: center; gap: 6px; width: fit-content; padding: 4px 8px; border-radius: 5px; font-size: var(--fs-caption); font-weight: var(--fw-medium); line-height: var(--lh-tight); white-space: nowrap; }
.task-state-badge > i, .task-status-pill > i { width: 7px; height: 7px; flex: none; border-radius: 50%; background: currentColor; }
.is-success { color: var(--success); background: var(--success-soft); }
.is-info { color: var(--pine); background: var(--pine-soft); }
.is-warning { color: var(--warning); background: var(--warning-soft); }
.is-danger { color: var(--danger); background: var(--danger-soft); }
.is-neutral { color: var(--text-secondary); background: var(--paper-2); }
.task-updated-at { display: flex; align-items: center; gap: 6px; color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.task-updated-at > i { width: 7px; height: 7px; border-radius: 50%; background: var(--success); }
.task-main-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 390px); gap: 14px; align-items: start; }
.task-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 16px; border-bottom: 1px solid var(--line); }
.task-section-head h2, .task-inspector-head h2 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.task-section-head h2 small { margin-left: 7px; color: var(--text-tertiary); font-size: var(--fs-body-sm); font-weight: var(--fw-regular); }
.task-target-table-head, .task-target-row { display: grid; grid-template-columns: minmax(155px, 1fr) minmax(115px, .78fr) minmax(180px, 1.35fr) 78px; gap: 12px; align-items: center; }
.task-target-table-head { min-height: 37px; padding: 7px 16px; background: var(--paper-2); color: var(--text-tertiary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.task-target-row { width: 100%; min-height: 68px; padding: 9px 16px; border: 0; border-bottom: 1px solid var(--line); background: var(--paper); color: inherit; text-align: left; cursor: pointer; }
.task-target-row:last-of-type { border-bottom: 0; }
.task-target-row:hover { background: var(--paper-2); }
.task-target-row.is-selected { background: var(--pine-soft); box-shadow: inset 3px 0 var(--pine); }
.task-target-account { display: flex; align-items: center; gap: 10px; min-width: 0; }
.task-platform-logo { width: 34px; height: 34px; border-radius: 7px; }
.task-target-account > span:last-child { display: grid; gap: 3px; min-width: 0; }
.task-target-account strong { overflow: hidden; color: var(--text-primary); font-size: var(--fs-body); font-weight: var(--fw-semibold); text-overflow: ellipsis; white-space: nowrap; }
.task-target-account small { overflow: hidden; color: var(--text-tertiary); font-size: var(--fs-body-sm); text-overflow: ellipsis; white-space: nowrap; }
.task-target-latest { display: grid; gap: 3px; min-width: 0; }
.task-target-latest strong { overflow: hidden; color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); line-height: var(--lh-base); text-overflow: ellipsis; white-space: nowrap; }
.task-target-latest small { color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.task-target-link { color: var(--pine); font-size: var(--fs-body-sm); text-align: right; white-space: nowrap; }
.task-list-foot { margin: 0; padding: 10px 16px; border-top: 1px solid var(--line); color: var(--text-tertiary); font-size: var(--fs-caption); line-height: var(--lh-base); }
.task-inspector-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 14px 16px 12px; border-bottom: 1px solid var(--line); }
.task-inspector-head p { margin: 5px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-inspector-body { display: grid; gap: 13px; padding: 13px 16px 16px; }
.task-detail-platform { display: flex; align-items: center; gap: 10px; }
.task-platform-logo-large { width: 38px; height: 38px; }
.task-detail-platform > div { display: grid; gap: 3px; }
.task-detail-platform strong { color: var(--text-primary); font-size: var(--fs-title-md); font-weight: var(--fw-semibold); }
.task-detail-platform small { color: var(--text-tertiary); font-size: var(--fs-body-sm); }
.task-detail-message { padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-detail-message strong { display: block; margin-bottom: 3px; font-weight: var(--fw-semibold); }
.task-detail-message p { margin: 0; overflow-wrap: anywhere; }
.task-detail-message p + p { margin-top: 5px; }
.task-detail-section { display: grid; gap: 7px; min-width: 0; }
.task-detail-section h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); }
.task-info-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-info-row > span { flex: none; color: var(--text-tertiary); }
.task-info-row > strong { min-width: 0; color: var(--text-secondary); font-weight: var(--fw-medium); text-align: right; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.task-fan-state { padding: 8px 9px; border-radius: 6px; font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-fan-state p { margin: 4px 0 0; overflow-wrap: anywhere; }
.task-timeline { display: grid; margin: 0; padding: 0 0 0 5px; list-style: none; }
.task-timeline li { position: relative; display: grid; grid-template-columns: 12px minmax(0, 1fr); gap: 7px; min-width: 0; padding-bottom: 10px; }
.task-timeline li:last-child { padding-bottom: 0; }
.task-timeline li::before { position: absolute; top: 8px; bottom: -1px; left: 3px; width: 1px; background: var(--line); content: ''; }
.task-timeline li:last-child::before { display: none; }
.task-timeline-dot { z-index: 1; width: 7px; height: 7px; margin-top: 5px; border-radius: 50%; background: var(--text-tertiary); }
.task-timeline li.is-current .task-timeline-dot { background: var(--pine); }
.task-timeline li > div { min-width: 0; }
.task-timeline strong { color: var(--text-secondary); font-size: var(--fs-body-sm); font-weight: var(--fw-medium); }
.task-timeline p { margin: 3px 0 0; color: var(--text-tertiary); font-size: var(--fs-body-sm); overflow-wrap: anywhere; }
.task-timeline time { display: block; margin-top: 3px; color: var(--text-tertiary); font-size: var(--fs-caption); font-variant-numeric: tabular-nums; }
.task-receipt { display: grid; gap: 4px; min-width: 0; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--paper-2); color: var(--text-secondary); font-size: var(--fs-body-sm); line-height: var(--lh-base); }
.task-receipt strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
.task-receipt p { margin: 0; overflow-wrap: anywhere; }
.task-receipt .task-remote-id { font-variant-numeric: tabular-nums; }
.task-receipt a { color: var(--pine); overflow-wrap: anywhere; }
.task-detail-actions { display: flex; flex-wrap: wrap; gap: 7px; }

@media (max-width: 1320px) {
  .task-overview { grid-template-columns: minmax(250px, 1fr) minmax(330px, 1.3fr); }
  .task-overview > div:last-child { grid-column: 1 / -1; border-top: 1px solid var(--line); border-left: 0; }
  .task-main-grid { grid-template-columns: minmax(0, 1fr) minmax(300px, 350px); }
  .task-target-table-head, .task-target-row { grid-template-columns: minmax(140px, 1fr) minmax(105px, .78fr) minmax(145px, 1.2fr) 70px; gap: 8px; }
}
@media (max-width: 1240px) {
  .task-main-grid { grid-template-columns: minmax(0, 1fr); }
  .task-inspector { max-width: none; }
}
@media (max-width: 680px) {
  .article-publish-task-page { gap: 10px; }
  .task-page-heading { align-items: flex-start; flex-direction: column; }
  .task-page-actions { width: 100%; }
  .task-page-actions > * { flex: 1; }
  .task-overview { grid-template-columns: minmax(0, 1fr); }
  .task-overview > div { padding: 13px; }
  .task-overview > div + div { border-top: 1px solid var(--line); border-left: 0; }
  .task-overview > div:last-child { grid-column: auto; }
  .task-target-table-head { display: none; }
  .task-target-row { grid-template-columns: minmax(0, 1fr) auto; gap: 8px; padding: 11px 12px; }
  .task-target-account { grid-column: 1; }
  .task-target-row > .task-status-pill { grid-column: 2; grid-row: 1; }
  .task-target-latest { grid-column: 1 / -1; }
  .task-target-link { grid-column: 2; grid-row: 2; }
  .task-section-head { padding: 12px; }
  .task-section-head h2 { font-size: var(--fs-title-lg); }
  .task-section-head h2 small { display: block; margin: 3px 0 0; }
  .task-inspector-head { flex-direction: column; }
  .task-detail-actions > * { flex: 1; }
}
</style>
