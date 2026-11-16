import type { CommentAnalysisOptions } from './api/types'

export function defaultCommentAnalysisOptions(): CommentAnalysisOptions {
  return { mode: 'economy', batch_size: 200, candidate_limit: 2000, budget_cny: 1,
    token_budget: 300000, input_price_per_million: 0.8, output_price_per_million: 2.8, max_output_tokens: 32768 }
}

export function validateCommentAnalysisOptions(options: CommentAnalysisOptions): string {
  const integer = (value: number, min: number, max: number) => typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max
  const finite = (value: number, min: number, max: number) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max
  if (!['economy', 'full'].includes(options.mode)) return '请选择有效的分析模式。'
  if (!integer(options.batch_size, 25, 300)) return '每批最多条数须为 25～300 的整数。'
  if (!integer(options.candidate_limit, 100, 50000)) return '最多分析不同文本须为 100～50,000 的整数。'
  if (!finite(options.budget_cny, 0, 10000) || options.budget_cny <= 0) return '费用预算须大于 0 且不超过 10,000 元。'
  if (!integer(options.token_budget, 10000, 100000000)) return 'Token 总预算须为 10,000～100,000,000 的整数。'
  if (!integer(options.max_output_tokens, 256, 32768)) return '每批最大输出 Token 须为 256～32,768 的整数。'
  if (!finite(options.input_price_per_million, 0, 100000) || !finite(options.output_price_per_million, 0, 100000)) return '模型输入和输出单价须为 0～100,000 的有效数字。'
  return ''
}
