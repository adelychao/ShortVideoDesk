<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElTabPane, ElTabs } from 'element-plus'
import 'element-plus/es/components/tabs/style/css'
import 'element-plus/es/components/tab-pane/style/css'
import { useRoute } from 'vue-router'
import ArticlePlatformAccountsPanel from '../components/ArticlePlatformAccountsPanel.vue'
import VideoPublishAccountsPanel from '../components/VideoPublishAccountsPanel.vue'

const tabs = [
  { id: 'article', label: '文章发布' },
  { id: 'video', label: '视频发布' },
] as const
type AccountTab = typeof tabs[number]['id']
const route = useRoute()
const initial = tabs.find((tab) => tab.id === route.query.tab)?.id ?? (route.query.tab === 'collection' ? 'video' : 'article')
const activeTab = ref<AccountTab>(initial)
watch(() => route.query.tab, (tab) => {
  activeTab.value = tabs.find((item) => item.id === tab)?.id ?? (tab === 'collection' ? 'video' : 'article')
})

</script>

<template>
  <div>
    <ElTabs v-model="activeTab" class="settings-tabs" aria-label="账号用途">
      <ElTabPane label="文章发布" name="article">
        <ArticlePlatformAccountsPanel v-if="activeTab === 'article'" />
      </ElTabPane>
      <ElTabPane label="视频发布" name="video">
        <VideoPublishAccountsPanel v-if="activeTab === 'video'" :initial-platform="typeof route.query.platform === 'string' ? route.query.platform : ''" />
      </ElTabPane>
    </ElTabs>
  </div>
</template>

<style scoped>
/* —— 排版系统 v2 试点:共享 Tab 在本页的覆盖(全量落地时上移 styles.css)—— */
.settings-tabs { display: block; margin: 0 0 16px; overflow: visible; border-bottom: 0; }
.settings-tabs :deep(.el-tabs__header) { margin: 0 0 16px; }
.settings-tabs :deep(.el-tabs__nav-wrap::after) { height: 1px; background: var(--line); }
.settings-tabs :deep(.el-tabs__nav) { gap: 2px; }
.settings-tabs :deep(.el-tabs__item) { height: auto; padding: 11px 16px 10px !important; color: var(--text-secondary); font-size: var(--fs-body); line-height: var(--lh-tight); }
.settings-tabs :deep(.el-tabs__item.is-active) { color: var(--pine); font-weight: var(--fw-semibold); }
.settings-tabs :deep(.el-tabs__active-bar) { height: 2px; background: var(--pine); }
.settings-tabs :deep(.el-tabs__item:focus-visible) { outline: 2px solid var(--pine); outline-offset: -3px; }
</style>
