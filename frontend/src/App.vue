<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import WorkspaceTabs from './components/WorkspaceTabs.vue'
import brandLogo from './assets/digua-logo.svg'

const route = useRoute()
const pageTitle = computed(() => String(route.meta.title ?? '工作区'))
const isNewArticleNavActive = computed(() => route.name === 'article-new'
  || (route.name === 'article-edit' && route.query.source === 'new-article'))
const isArticleManagerNavActive = computed(() => route.name === 'articles'
  || route.name === 'article-publish'
  || route.name === 'article-publish-task'
  || (route.name === 'article-edit' && route.query.source !== 'new-article'))
const restartRequired = ref(false)
let healthTimer: ReturnType<typeof setInterval> | undefined

async function checkRuntimeVersion(): Promise<void> {
  try {
    const response = await fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' })
    if (response.ok) {
      const health = await response.json()
      restartRequired.value = health.restart_required === true
    }
  } catch {
    return
  }
}

onMounted(() => {
  void checkRuntimeVersion()
  healthTimer = setInterval(() => { void checkRuntimeVersion() }, 30_000)
})
onUnmounted(() => { if (healthTimer) clearInterval(healthTimer) })
</script>

<template>
  <a class="skip-link" href="#main">跳至主要内容</a>
  <div class="app-shell">
    <aside class="sidebar" aria-label="主导航">
      <RouterLink class="brand" to="/" aria-label="地瓜创作者工作台首页">
        <span class="brand-mark">
          <img class="brand-logo" :src="brandLogo" alt="" aria-hidden="true" />
        </span>
        <span>
          <span class="brand-name">地瓜创作者工作台</span>
        </span>
      </RouterLink>

      <div>
        <nav class="side-nav" aria-label="工作区页面">
          <RouterLink class="nav-link" to="/" exact-active-class="is-active">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M4 10.5 12 4l8 6.5M6.5 9.5v10h11v-10M10 19.5v-6h4v6" />
            </svg>
            素材采集
          </RouterLink>
          <RouterLink class="nav-link" to="/history" :class="{ 'is-active': ['history', 'task-detail'].includes(String(route.name)) }" :aria-current="['history', 'task-detail'].includes(String(route.name)) ? 'page' : undefined">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M5 5h14M5 10h14M5 15h9M5 20h9M3 5h.01M3 10h.01M3 15h.01M3 20h.01" />
            </svg>
            素材档案
          </RouterLink>
          <RouterLink class="nav-link" to="/articles/new" :class="{ 'is-active': isNewArticleNavActive }" :aria-current="isNewArticleNavActive ? 'page' : undefined">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M5 3.5h9l4 4v13H5z" />
              <path d="M14 3.5v4h4M8 12h7M8 15.5h4M17 14v6M14 17h6" />
            </svg>
            新建文章
          </RouterLink>
          <RouterLink class="nav-link" to="/articles" :class="{ 'is-active': isArticleManagerNavActive }" :aria-current="isArticleManagerNavActive ? 'page' : undefined">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M5 3.5h10l4 4v13H5z" />
              <path d="M14.5 3.5v4h4M8 12h8M8 15.5h8" />
            </svg>
            文章管理
          </RouterLink>
          <RouterLink class="nav-link" to="/video-publish/new" exact-active-class="is-active">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <rect x="4" y="5" width="16" height="14" rx="2" />
              <path d="m10 9 5 3-5 3zM19 3v5M16.5 5.5h5" />
            </svg>
            发布视频
          </RouterLink>
          <RouterLink class="nav-link" to="/video-publish" :class="{ 'is-active': ['video-publish-history', 'video-publish-detail'].includes(String(route.name)) }" :aria-current="['video-publish-history', 'video-publish-detail'].includes(String(route.name)) ? 'page' : undefined">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M5 5h14M5 10h14M5 15h9M5 20h9M3 5h.01M3 10h.01M3 15h.01M3 20h.01" />
            </svg>
            视频发布记录
          </RouterLink>
          <RouterLink class="nav-link" to="/accounts" active-class="is-active">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <circle cx="12" cy="8" r="3.5" />
              <path d="M5 20v-1.5a7 7 0 0 1 14 0V20z" />
            </svg>
            账号管理
          </RouterLink>
          <RouterLink class="nav-link" to="/settings" exact-active-class="is-active">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z" />
              <path d="m19.4 15 .1.1 1.2 2.1-2.1 2.1-2.1-1.2-.1-.1a7.8 7.8 0 0 1-1.7.7l-.7 2.3h-3l-.7-2.3a7.8 7.8 0 0 1-1.7-.7l-.1.1-2.1 1.2-2.1-2.1 1.2-2.1.1-.1a7.8 7.8 0 0 1-.7-1.7L3 12.6v-3l2.3-.7A7.8 7.8 0 0 1 6 7.2l-.1-.1L4.7 5l2.1-2.1 2.1 1.2.1.1a7.8 7.8 0 0 1 1.7-.7l.7-2.3h3l.7 2.3a7.8 7.8 0 0 1 1.7.7l.1-.1 2.1-1.2L21 5l-1.2 2.1-.1.1c.3.5.5 1.1.7 1.7l2.3.7v3l-2.3.7a7.8 7.8 0 0 1-1 1.7Z" />
            </svg>
            知识库与设置
          </RouterLink>
        </nav>
      </div>

      <div class="sidebar-spacer"></div>
    </aside>

    <div class="main-area">
      <header class="topbar">
        <nav class="breadcrumb" aria-label="面包屑导航">
          <RouterLink to="/">工作区</RouterLink>
          <span aria-hidden="true">/</span>
          <strong aria-current="page">{{ pageTitle }}</strong>
        </nav>
        <div class="topbar-actions">
          <RouterLink v-if="route.name !== 'settings'" class="top-link" to="/settings">知识库与设置</RouterLink>
          <RouterLink v-else class="top-link" to="/">返回素材采集</RouterLink>
        </div>
      </header>
      <WorkspaceTabs />
      <main id="main" class="content" tabindex="-1">
        <div v-if="restartRequired" class="runtime-version-warning" role="status">
          程序代码已更新，但后台仍在运行旧版本。请在任务结束后关闭并重新启动程序，然后刷新页面。
        </div>
        <RouterView v-slot="{ Component, route: viewRoute }">
          <KeepAlive>
            <component :is="Component" :key="viewRoute.fullPath" />
          </KeepAlive>
        </RouterView>
      </main>
    </div>
  </div>
</template>

<style scoped>
.brand { gap: 8px; padding-inline: 0; }
.brand-mark { flex: 0 0 auto; background: transparent; }
.brand-logo { display: block; width: 100%; height: 100%; }
.brand-name { font-size: var(--fs-title-lg); font-weight: var(--fw-semibold); white-space: nowrap; }
</style>
