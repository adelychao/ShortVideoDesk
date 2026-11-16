<script setup lang="ts">
import type { CommentAnalysisOptions } from '../api/types'
const model = defineModel<CommentAnalysisOptions>({ required: true })
</script>

<template>
  <fieldset class="analysis-options">
    <legend>评论分析与预算</legend>
    <div class="fields">
      <label>模式<select v-model="model.mode" class="select-control"><option value="economy">经济模式</option><option value="full">全量模式</option></select></label>
      <label>每批最多条数<input v-model.number="model.batch_size" class="input" type="number" min="25" max="300" required /></label>
      <label v-if="model.mode === 'economy'">最多分析不同文本<input v-model.number="model.candidate_limit" class="input" type="number" min="100" max="50000" required /></label>
      <label>费用预算（元）<input v-model.number="model.budget_cny" class="input" type="number" min="0.01" max="10000" step="0.01" required /></label>
      <label>Token 总预算<input v-model.number="model.token_budget" class="input" type="number" min="10000" max="100000000" step="1000" required /></label>
      <label>每批最大输出 Token<input v-model.number="model.max_output_tokens" class="input" type="number" min="256" max="32768" required /></label>
      <label>输入单价（元/百万 Token）<input v-model.number="model.input_price_per_million" class="input" type="number" min="0" max="100000" step="0.01" required /></label>
      <label>输出单价（元/百万 Token）<input v-model.number="model.output_price_per_million" class="input" type="number" min="0" max="100000" step="0.01" required /></label>
    </div>
    <p class="helper">每批最大输出为本系统单次请求的输出限制，允许 256～32,768；它与模型的上下文长度、分析的 Token 总预算不同。</p>
    <p class="helper">默认单价仅供 GLM-5.3-Flash 参考；请按实际模型与服务商价格核对。预算根据估算控制，不代替服务商账户限额；超时或缺少用量时会停止发送后续批次。</p>
  </fieldset>
</template>

<style scoped>
.analysis-options { min-width:0; margin:0; padding:12px; border:1px solid var(--line); border-radius:6px; }
.analysis-options legend { color:var(--text-primary); font-size:var(--fs-body); font-weight:var(--fw-semibold); }
.fields { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%, 180px),1fr)); gap:10px; }
.fields label { display:grid; gap:4px; min-width:0; color:var(--text-secondary); font-size:var(--fs-body-sm); }
.fields .input,.fields .select-control { width:100%; min-width:0; }
.analysis-options .helper { margin:8px 0 0; }
</style>
