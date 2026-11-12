import { computed, ref, watch } from 'vue'
import { isNavigationFailure, useRoute, useRouter, type Router } from 'vue-router'

export interface WorkspaceTab {
  key: string
  path: string
  title: string
  closable: boolean
}

const storageKey = 'local-video-workbench:open-tabs'
const tabs = ref<WorkspaceTab[]>([])
const activeKey = ref('')
let initialized = false
let activeTabClosePending = false

function tabFromPath(router: Router, path: string): WorkspaceTab | null {
  const resolved = router.resolve(path)
  if (!resolved.matched.length) return null

  const taskId = typeof resolved.params.taskId === 'string' ? resolved.params.taskId : ''
  const batchId = typeof resolved.params.batchId === 'string' ? resolved.params.batchId : ''
  const title = resolved.name === 'task-detail' && taskId
    ? `任务 · ${taskId.slice(-6)}`
    : resolved.name === 'video-publish-detail' && batchId
      ? `发布 · ${batchId.slice(-6)}`
    : String(resolved.meta.title ?? resolved.path)

  return {
    key: resolved.fullPath,
    path: resolved.fullPath,
    title,
    closable: resolved.name !== 'workbench',
  }
}

function persistTabs(): void {
  try {
    window.sessionStorage.setItem(storageKey, JSON.stringify(tabs.value.map((tab) => tab.path)))
  } catch {
    // Tab state remains available in memory when browser storage is unavailable.
  }
}

function initializeTabs(router: Router): void {
  const restored: WorkspaceTab[] = []
  try {
    const stored = window.sessionStorage.getItem(storageKey)
    const paths: unknown = stored ? JSON.parse(stored) : []
    if (Array.isArray(paths)) {
      for (const path of paths) {
        if (typeof path !== 'string') continue
        const tab = tabFromPath(router, path)
        if (tab && !restored.some((item) => item.key === tab.key)) restored.push(tab)
      }
    }
  } catch {
    // Ignore stale or malformed session state and start with the current route.
  }

  const homeTab = tabFromPath(router, '/')
  if (homeTab && !restored.some((tab) => tab.key === homeTab.key)) restored.unshift(homeTab)
  tabs.value = restored
  initialized = true
}

export function useWorkspaceTabs() {
  const route = useRoute()
  const router = useRouter()

  if (!initialized) initializeTabs(router)

  function syncCurrentRoute(): void {
    const currentTab = tabFromPath(router, route.fullPath)
    if (!currentTab) return

    const existingIndex = tabs.value.findIndex((tab) => tab.key === currentTab.key)
    if (existingIndex === -1) tabs.value.push(currentTab)
    else tabs.value[existingIndex] = { ...tabs.value[existingIndex], title: currentTab.title }
    activeKey.value = currentTab.key
    persistTabs()
  }

  watch(() => route.fullPath, syncCurrentRoute, { immediate: true })

  function activateTab(key: string): void {
    const tab = tabs.value.find((item) => item.key === key)
    if (tab && router.currentRoute.value.fullPath !== tab.path) void router.push(tab.path)
  }

  async function closeTab(key: string): Promise<void> {
    if (activeTabClosePending) return
    const index = tabs.value.findIndex((tab) => tab.key === key)
    if (index === -1 || !tabs.value[index].closable) return

    const wasActive = activeKey.value === key
    if (wasActive) {
      const remainingTabs = tabs.value.filter((tab) => tab.key !== key)
      const nextTab = remainingTabs[Math.min(index, remainingTabs.length - 1)] ?? remainingTabs[0]
      if (!nextTab) return
      activeTabClosePending = true
      try {
        if (router.currentRoute.value.fullPath !== nextTab.path) {
          const failure = await router.replace(nextTab.path)
          if (isNavigationFailure(failure)) return
        }
      } catch {
        return
      } finally {
        activeTabClosePending = false
      }
    }

    const currentIndex = tabs.value.findIndex((tab) => tab.key === key)
    if (currentIndex === -1 || !tabs.value[currentIndex].closable) return
    tabs.value.splice(currentIndex, 1)
    persistTabs()

    if (wasActive) {
      const currentTab = tabFromPath(router, router.currentRoute.value.fullPath)
      activeKey.value = currentTab && tabs.value.some((tab) => tab.key === currentTab.key)
        ? currentTab.key
        : tabs.value[Math.min(currentIndex, tabs.value.length - 1)]?.key ?? tabs.value[0]?.key ?? ''
    }
  }

  function closeOtherTabs(): void {
    if (activeTabClosePending) return
    tabs.value = tabs.value.filter((tab) => !tab.closable || tab.key === activeKey.value)
    persistTabs()
  }

  const hasClosableOtherTabs = computed(() =>
    tabs.value.some((tab) => tab.closable && tab.key !== activeKey.value),
  )

  return { tabs, activeKey, hasClosableOtherTabs, activateTab, closeTab, closeOtherTabs }
}
