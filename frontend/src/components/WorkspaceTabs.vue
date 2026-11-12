<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { useWorkspaceTabs } from '../stores/workspaceTabs'

const { tabs, activeKey, hasClosableOtherTabs, activateTab, closeTab, closeOtherTabs } = useWorkspaceTabs()
const tabList = ref<HTMLElement | null>(null)
const canScrollLeft = ref(false)
const canScrollRight = ref(false)

function updateScrollState(): void {
  const element = tabList.value
  if (!element) return
  canScrollLeft.value = element.scrollLeft > 1
  canScrollRight.value = element.scrollLeft + element.clientWidth < element.scrollWidth - 1
}

function scrollTabs(direction: -1 | 1): void {
  const element = tabList.value
  if (!element) return
  element.scrollBy({ left: direction * Math.max(180, element.clientWidth * 0.65), behavior: 'smooth' })
}

function handleWheel(event: WheelEvent): void {
  const element = tabList.value
  if (!element || element.scrollWidth <= element.clientWidth) return
  const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY
  if (!delta) return
  event.preventDefault()
  element.scrollLeft += delta
}

watch([activeKey, () => tabs.value.length], async () => {
  await nextTick()
  tabList.value?.querySelector<HTMLElement>('[aria-current="page"]')?.scrollIntoView({
    block: 'nearest',
    inline: 'nearest',
    behavior: 'smooth',
  })
  updateScrollState()
}, { flush: 'post' })

onMounted(() => requestAnimationFrame(updateScrollState))
</script>

<template>
  <div class="workspace-tabs">
    <button
      class="workspace-tabs__scroll"
      type="button"
      aria-label="向左滚动页签"
      :disabled="!canScrollLeft"
      @click="scrollTabs(-1)"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 5-7 7 7 7M21 5l-7 7 7 7" /></svg>
    </button>

    <nav ref="tabList" class="workspace-tabs__list" aria-label="已打开的页面" @scroll="updateScrollState" @wheel="handleWheel">
      <div
        v-for="tab in tabs"
        :key="tab.key"
        class="workspace-tabs__item"
        :class="{ 'is-active': tab.key === activeKey }"
      >
        <button
          class="workspace-tabs__open"
          type="button"
          :aria-current="tab.key === activeKey ? 'page' : undefined"
          :title="tab.title"
          @click="activateTab(tab.key)"
        >
          <svg v-if="!tab.closable" class="workspace-tabs__home" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3.5 10.5 12 4l8.5 6.5M6.5 9.5v10h11v-10M10 19.5v-6h4v6" />
          </svg>
          <span class="workspace-tabs__label">{{ tab.title }}</span>
        </button>
        <button
          v-if="tab.closable"
          class="workspace-tabs__close"
          type="button"
          :aria-label="`关闭${tab.title}`"
          :title="`关闭${tab.title}`"
          @click.stop="closeTab(tab.key)"
        >
          <span aria-hidden="true">×</span>
        </button>
      </div>
    </nav>

    <div class="workspace-tabs__tools">
      <button
        class="workspace-tabs__scroll"
        type="button"
        aria-label="向右滚动页签"
        :disabled="!canScrollRight"
        @click="scrollTabs(1)"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 5 7 7-7 7M3 5l7 7-7 7" /></svg>
      </button>
      <span class="workspace-tabs__divider" aria-hidden="true"></span>
      <button
        class="workspace-tabs__manage"
        type="button"
        aria-label="关闭其他页签"
        title="关闭其他页签"
        :disabled="!hasClosableOtherTabs"
        @click="closeOtherTabs"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <rect x="4" y="4" width="6" height="6" rx="1" />
          <rect x="14" y="4" width="6" height="6" rx="1" />
          <rect x="4" y="14" width="6" height="6" rx="1" />
          <rect x="14" y="14" width="6" height="6" rx="1" />
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.workspace-tabs {
  position: sticky;
  top: 48px;
  z-index: 25;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  width: 100%;
  height: 35px;
  padding: 0 8px 0 5px;
  border-bottom: 1px solid #d8dce5;
  background: #fff;
  box-shadow: 0 1px 3px rgb(0 0 0 / 6%);
}

.workspace-tabs__list {
  display: flex;
  min-width: 0;
  height: 34px;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
  white-space: nowrap;
}

.workspace-tabs__list::-webkit-scrollbar { display: none; }

.workspace-tabs__item {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  height: 29px;
  max-width: min(260px, 36vw);
  padding: 0 4px 0 9px;
  border: 1px solid #d8dce5;
  border-radius: 2px 2px 0 0;
  background: #fff;
  color: #495060;
  transition: border-color .14s ease, background-color .14s ease, color .14s ease;
}

.workspace-tabs__item:hover { border-color: #b6c9e8; background: #f8faff; }

.workspace-tabs__item.is-active {
  border-color: #409eff;
  background: #409eff;
  color: #fff;
}

.workspace-tabs__open,
.workspace-tabs__close,
.workspace-tabs__scroll,
.workspace-tabs__manage {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.workspace-tabs__open {
  min-width: 0;
  height: 27px;
  gap: 5px;
  padding: 0 2px;
  font-size: 12px;
  line-height: 1;
}

.workspace-tabs__label { overflow: hidden; text-overflow: ellipsis; }

.workspace-tabs__home {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.workspace-tabs__close {
  width: 18px;
  height: 20px;
  flex: 0 0 auto;
  border-radius: 50%;
  font-size: 16px;
  line-height: 1;
}

.workspace-tabs__close:hover { background: rgb(0 0 0 / 8%); }
.workspace-tabs__item.is-active .workspace-tabs__close:hover { background: rgb(255 255 255 / 20%); }

.workspace-tabs__tools { display: flex; height: 34px; align-items: center; border-left: 1px solid #e5e7eb; }

.workspace-tabs__scroll,
.workspace-tabs__manage {
  width: 34px;
  height: 32px;
  color: #7b8491;
}

.workspace-tabs__scroll svg,
.workspace-tabs__manage svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.workspace-tabs__scroll:hover:not(:disabled),
.workspace-tabs__manage:hover:not(:disabled) { background: #f3f6fb; color: var(--pine); }
.workspace-tabs__scroll:disabled,
.workspace-tabs__manage:disabled { cursor: default; opacity: .45; }
.workspace-tabs__divider { width: 1px; height: 18px; background: #e5e7eb; }

@media (max-width: 520px) {
  .workspace-tabs { grid-template-columns: 29px minmax(0, 1fr) auto; padding: 0 3px; }
  .workspace-tabs__scroll,
  .workspace-tabs__manage { width: 29px; }
  .workspace-tabs__item { max-width: min(190px, 55vw); }
}

@media (prefers-reduced-motion: reduce) {
  .workspace-tabs__item { transition: none; }
}
</style>
