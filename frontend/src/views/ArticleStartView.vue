<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { ElCard } from 'element-plus'
import 'element-plus/es/components/card/style/css'
</script>

<template>
  <section class="article-start-page" aria-labelledby="article-start-title">
    <header class="page-heading">
      <div>
        <h1 id="article-start-title">新建文章</h1>
        <p class="lead">选择一种方式开始创作。</p>
      </div>
    </header>
    <div class="article-start-choices">
      <RouterLink class="article-start-link" :to="{ name: 'article-from-text' }">
        <ElCard class="article-start-card article-start-card-primary" shadow="never">
          <span class="article-start-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none"><path d="M12 2.5c.7 5.4 3.1 7.8 8.5 8.5-5.4.7-7.8 3.1-8.5 8.5-.7-5.4-3.1-7.8-8.5-8.5 5.4-.7 7.8-3.1 8.5-8.5ZM19 16v6m-3-3h6"/></svg>
          </span>
          <h2>文案生成文章</h2>
          <p>粘贴已有口播稿，生成文章标题、摘要和正文草稿。</p>
          <span class="article-start-action">粘贴文案<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 12h15m-6-6 6 6-6 6"/></svg></span>
        </ElCard>
      </RouterLink>
      <RouterLink class="article-start-link" :to="{ name: 'article-manual' }">
        <ElCard class="article-start-card" shadow="never">
          <span class="article-start-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none"><path d="m14 5 5 5M4 20l4-1 12-12-4-4L4 15zM4 20h16"/></svg>
          </span>
          <h2>手动创建文章</h2>
          <p>打开空白文章编辑器，自行填写标题、摘要和正文。</p>
          <span class="article-start-action">开始写作<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 12h15m-6-6 6 6-6 6"/></svg></span>
        </ElCard>
      </RouterLink>
    </div>
  </section>
</template>

<style scoped>
.article-start-page { display: grid; gap: 24px; }
.page-heading { margin-bottom: 0; }
.article-start-choices { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.article-start-link { display: block; min-width: 0; border-radius: 9px; color: var(--text-primary); }
.article-start-card { min-height: 228px; height: 100%; border: 1px solid var(--line); border-radius: 9px; transition: border-color .16s ease, background .16s ease; }
.article-start-card-primary { border-color: var(--accent-line); }
.article-start-link:hover .article-start-card { border-color: var(--pine); background: var(--paper); }
.article-start-link:focus-visible { outline-offset: 4px; }
.article-start-card :deep(.el-card__body) { display: grid; grid-template-rows: auto auto 1fr auto; justify-items: start; gap: 10px; min-height: 226px; padding: 24px; }
.article-start-icon { display: grid; width: 44px; height: 44px; place-items: center; margin-bottom: 4px; border-radius: 8px; color: var(--text-secondary); background: var(--paper-2); }
.article-start-icon svg { width: 24px; height: 24px; stroke: currentColor; stroke-width: 1.65; stroke-linecap: round; stroke-linejoin: round; }
.article-start-card-primary .article-start-icon { color: var(--pine); background: var(--pine-soft); }
.article-start-card h2 { margin: 0; font-size: var(--fs-title-xl); font-weight: var(--fw-semibold); }
.article-start-card p { margin: 0; color: var(--text-secondary); font-size: var(--fs-body); line-height: var(--lh-base); overflow-wrap: anywhere; }
.article-start-action { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 36px; border-top: 1px solid var(--line); padding-top: 12px; color: var(--text-secondary); font-size: var(--fs-body); font-weight: var(--fw-semibold); }
.article-start-card-primary .article-start-action { color: var(--pine); }
.article-start-action svg { width: 17px; height: 17px; margin-left: auto; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
@media (max-width: 680px) { .article-start-choices { grid-template-columns: minmax(0, 1fr); } .article-start-card, .article-start-card :deep(.el-card__body) { min-height: 196px; } }
</style>
