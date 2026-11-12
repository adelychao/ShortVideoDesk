import { createRouter, createWebHistory, isNavigationFailure, NavigationFailureType } from 'vue-router'
import type { RouteLocationNormalized, Router } from 'vue-router'
import WorkbenchView from './views/WorkbenchView.vue'
import AssetDetailView from './views/AssetDetailView.vue'
import SettingsView from './views/SettingsView.vue'
import VideoPublishHistoryView from './views/VideoPublishHistoryView.vue'

const browserHistory = createWebHistory(import.meta.env.BASE_URL)
const go = browserHistory.go
const listen = browserHistory.listen

type NavigationEntry = { key: string; id?: string }
type BrowserNavigation = {
  currentEntry: NavigationEntry | null
  traverseTo: (key: string) => {
    committed: Promise<NavigationEntry>
    finished: Promise<NavigationEntry>
  }
}
type HistoryEntry = NavigationEntry & { location: string }

const navigationApi = (window as Window & { navigation?: BrowserNavigation }).navigation
const navigation = typeof navigationApi?.traverseTo === 'function' ? navigationApi : undefined
const historyRestoreTimeout = 5000

function currentHistoryEntry(): HistoryEntry | undefined {
  const entry = navigation?.currentEntry
  if (entry?.key) return { key: entry.key, id: entry.id, location: browserHistory.location }
}

let committedEntry = currentHistoryEntry()

type DraftLeave = {
  target: RouteLocationNormalized
  source: RouteLocationNormalized
  sourceEntry: HistoryEntry
  targetEntry: HistoryEntry
  approved: boolean
  settled: Promise<boolean>
  settle: (aligned: boolean) => void
  reportRecoveryError: (realigned: boolean) => void
  recovery?: Promise<boolean>
  checkAlignment?: () => void
}

const draftLeaves = new Map<RouteLocationNormalized, DraftLeave>()
let activeDraftLeave: DraftLeave | undefined
let quietRollback: { delta: number } | undefined

export function beginDraftLeave(target: RouteLocationNormalized, reportRecoveryError: DraftLeave['reportRecoveryError']): boolean {
  const targetEntry = currentHistoryEntry()
  if (!committedEntry || !targetEntry) return false
  let settle!: DraftLeave['settle']
  const settled = new Promise<boolean>((resolve) => { settle = resolve })
  activeDraftLeave = {
    target,
    source: router.currentRoute.value,
    sourceEntry: committedEntry,
    targetEntry,
    approved: false,
    settled,
    settle,
    reportRecoveryError,
  }
  draftLeaves.set(target, activeDraftLeave)
  return true
}

export function isCurrentDraftLeave(target: RouteLocationNormalized): boolean {
  return activeDraftLeave?.target === target && !activeDraftLeave.recovery
}

export function approveDraftSave(target: RouteLocationNormalized): void {
  const leave = draftLeaves.get(target)
  if (leave) leave.approved = true
}

export function redirectDraftLeave(source: RouteLocationNormalized, target: RouteLocationNormalized): void {
  const leave = draftLeaves.get(source)
  if (!leave) return
  draftLeaves.delete(source)
  leave.target = target
  draftLeaves.set(target, leave)
}

function matchesHistoryEntry(current: NavigationEntry | null | undefined, entry: HistoryEntry): boolean {
  return current?.key === entry.key && (!entry.id || current.id === entry.id)
}

function historyIsAligned(entry: HistoryEntry): boolean {
  return matchesHistoryEntry(navigation?.currentEntry, entry) && browserHistory.location === entry.location
}

function alignHistoryEntry(leave: DraftLeave, entry: HistoryEntry): Promise<void> {
  if (!navigation || historyIsAligned(entry)) return Promise.resolve()

  return new Promise<void>((resolve, reject) => {
    let committed = false
    let completed = false
    const timeout = window.setTimeout(() => finish(new Error('恢复文章草稿的浏览器历史位置超时。')), historyRestoreTimeout)

    function finish(error?: unknown): void {
      if (completed) return
      completed = true
      window.clearTimeout(timeout)
      leave.checkAlignment = undefined
      if (error !== undefined) reject(error)
      else resolve()
    }

    const checkAlignment = () => {
      if (committed && historyIsAligned(entry)) finish()
    }
    leave.checkAlignment = checkAlignment

    // zhw 2026-11-05 20:43:24 以条目身份恢复，避免排队中的浏览器前进改变相对位移的起点。
    let traversal: ReturnType<BrowserNavigation['traverseTo']>
    try {
      traversal = navigation.traverseTo(entry.key)
    } catch (error) {
      finish(error)
      return
    }
    void traversal.finished.catch(finish)
    void traversal.committed.then((current) => {
      if (!matchesHistoryEntry(current, entry)) {
        finish(new Error('浏览器未恢复到预期的文章草稿历史条目。'))
        return
      }
      committed = true
      checkAlignment()
    }, finish)
  })
}

function realignMountedDraft(leave: DraftLeave): boolean {
  if (activeDraftLeave !== leave || router.currentRoute.value !== leave.source) return false
  const editorPath = leave.source.fullPath

  // zhw 2026-11-05 20:50:50 原条目不可恢复时，仅校正当前条目的地址，保留编辑器和文档。
  try {
    const editorUrl = new URL(browserHistory.createHref(editorPath), window.location.href)
    if (browserHistory.location !== editorPath || window.location.href !== editorUrl.href) {
      const state = window.history.state
      if (!state) return false
      const nextState = { ...state, current: editorPath, replaced: true, scroll: null }
      // zhw 2026-11-05 20:57:10 避免路由器在历史写入失败时退回到整页加载。
      window.history.replaceState(nextState, '', editorUrl.href)
      window.dispatchEvent(new PopStateEvent('popstate', { state: nextState }))
    }
    if (browserHistory.location !== editorPath || window.location.href !== editorUrl.href) return false
    committedEntry = currentHistoryEntry()
    return true
  } catch (error) {
    console.error(error)
    return false
  }
}

function recoverDraftSource(leave: DraftLeave): Promise<boolean> {
  leave.recovery ??= alignHistoryEntry(leave, leave.sourceEntry).then(() => true, (error) => {
    console.error(error)
    leave.reportRecoveryError(realignMountedDraft(leave))
    return false
  }).finally(() => {
    if (activeDraftLeave === leave) activeDraftLeave = undefined
  })
  return leave.recovery
}

browserHistory.listen = (callback) => listen((to, from, info) => {
  const leave = activeDraftLeave
  if (leave?.approved || leave?.recovery) {
    leave.checkAlignment?.()
    return
  }
  activeDraftLeave = undefined
  callback(to, from, info)
})

browserHistory.go = (delta, triggerListeners) => {
  if (triggerListeners !== false) {
    go(delta, triggerListeners)
    return
  }
  if (!draftLeaves.size) {
    if (!activeDraftLeave?.recovery) go(delta, false)
    return
  }
  // zhw 2026-11-05 20:33:00 等路由器报告失败目标后，再决定是否回滚浏览器历史。
  const rollback = { delta }
  quietRollback = rollback
  queueMicrotask(() => {
    if (quietRollback !== rollback) return
    quietRollback = undefined
    go(delta, false)
  })
}

const router = createRouter({
  history: browserHistory,
  routes: [
    { path: '/', name: 'workbench', component: WorkbenchView, meta: { title: '素材采集' } },
    { path: '/history', name: 'history', component: () => import('./views/ArchiveView.vue'), meta: { title: '素材档案' } },
    { path: '/task', redirect: { name: 'history' } },
    { path: '/task/:taskId', name: 'task-detail', component: AssetDetailView, meta: { title: '任务详情' } },
    { path: '/articles/new', name: 'article-new', component: () => import('./views/ArticleEditorView.vue'), meta: { title: '新建文章' } },
    { path: '/articles', name: 'articles', component: () => import('./views/ArticleManagerView.vue'), meta: { title: '文章管理' } },
    { path: '/articles/:articleId/edit', name: 'article-edit', component: () => import('./views/ArticleEditorView.vue'), meta: { title: '编辑文章' } },
    { path: '/articles/:articleId/publish-task', name: 'article-publish-task', component: () => import('./views/ArticlePublishTaskView.vue'), meta: { title: '任务执行' } },
    { path: '/articles/:articleId/publish', name: 'article-publish', component: () => import('./views/ArticlePublishView.vue'), meta: { title: '发布文章' } },
    { path: '/video-publish', name: 'video-publish-history', component: VideoPublishHistoryView, meta: { title: '视频发布记录' } },
    { path: '/video-publish/new', name: 'video-publish-create', component: () => import('./views/VideoPublishCreateView.vue'), meta: { title: '发布视频' } },
    { path: '/video-publish/:batchId', name: 'video-publish-detail', component: () => import('./views/VideoPublishDetailView.vue'), meta: { title: '发布任务详情' } },
    { path: '/accounts', name: 'accounts', component: () => import('./views/AccountsView.vue'), meta: { title: '账号管理' } },
    { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '知识库与设置' } },
  ],
  scrollBehavior() {
    return { top: 0 }
  },
})

function protectDraftHistory(navigate: Router['push']): Router['push'] {
  return (to) => {
    const leave = activeDraftLeave
    if (!leave) return navigate(to)
    if (leave.approved || leave.recovery) {
      return (leave.recovery ?? leave.settled).then((aligned) => {
        if (!aligned) throw new Error('文章离页导航已取消，请重试。')
        return navigate(router.currentRoute.value.fullPath)
      })
    }
    if (historyIsAligned(leave.sourceEntry)) {
      activeDraftLeave = undefined
      return navigate(to)
    }
    const result = navigate(router.currentRoute.value.fullPath)
    return recoverDraftSource(leave).then((aligned) => {
      if (!aligned) throw new Error('文章离页导航已取消，请重试。')
      return result
    })
  }
}

router.push = protectDraftHistory(router.push)
router.replace = protectDraftHistory(router.replace)

function settleDraftLeave(target: RouteLocationNormalized, restore: boolean): void {
  const rollback = quietRollback
  quietRollback = undefined
  const leave = draftLeaves.get(target)
  if (!leave) {
    if (rollback) go(rollback.delta, false)
    return
  }
  draftLeaves.delete(target)
  if (activeDraftLeave === leave) {
    if (restore || leave.recovery) {
      void recoverDraftSource(leave).then(leave.settle)
      return
    }
    activeDraftLeave = undefined
  }
  leave.settle(true)
}

router.afterEach((to, _from, failure) => {
  if (!failure) committedEntry = currentHistoryEntry()
  settleDraftLeave(to, isNavigationFailure(failure, NavigationFailureType.aborted))
})

router.onError((error, to) => {
  settleDraftLeave(to, true)
  console.error(error)
})

router.beforeResolve((to) => {
  const leave = activeDraftLeave
  if (leave?.target === to && leave.approved) return alignHistoryEntry(leave, leave.targetEntry)
})

router.beforeEach((to) => {
  if (to.name === 'settings' && to.query.tab === 'accounts') {
    return { name: 'accounts', query: { ...to.query, tab: 'article' }, replace: true }
  }
})

export default router
