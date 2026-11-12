import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
try {
  const { default: TaskSteps } = await server.ssrLoadModule('/src/components/TaskSteps.vue')
  const step = { id: 's', kind: 'analyze_comments', ordinal: 2, status: 'blocked', error_code: 'provider_timeout', message: '模型请求超时', depends_on: [], input_artifact_ids: ['a'], output_artifact_ids: [], retry_count: 0, progress: null }
  const props = { task: { id: 't', status: 'blocked', steps: [step] }, artifacts: [{id:'a', name:'评论资料.md', kind:'note', metadata: {scope:'comments'}}], consents: [{step_kind:'analyze_comments',provider_id:'zhipu',model_id:'glm-5.3-flash',authorized:true,artifact_ids:['a']}], modelRuns: [{step_id:'s'}], commentState: null }
  const render = async (overrides = {}) => renderToString(createSSRApp(TaskSteps, {...props, ...overrides}))
  const interruptedCards = await render({
    task: {...props.task, status:'partial', steps:[{...step, id:'cards', kind:'knowledge_cards', status:'interrupted', error_code:'worker_restarted', message:'程序重启后任务中断'}]},
    artifacts: [{id:'transcript', name:'语音转写.md', kind:'transcript', metadata:{}}],
  })
  assert.match(interruptedCards, /重新生成知识卡/)
  assert.match(interruptedCards, /可能产生费用/)
  assert.match(interruptedCards, /我已了解本次重试会重新调用云端模型/)
  assert.match(interruptedCards, /<button[^>]*disabled[^>]*>\s*确认并重新生成知识卡/s)
  for (const [kind, label, buttonLabel] of [['analyze_structure', '重新分析内容结构', '确认并重新分析内容结构'], ['generate_scripts', '重新生成口播稿', '确认并重新生成口播稿']]) {
    const modelStep = await render({
      task: {...props.task, status:'partial', steps:[{...step, id:kind, kind, status:'failed', error_code:'provider_timeout', message:'模型请求超时'}]},
    })
    assert.match(modelStep, new RegExp(label))
    assert.match(modelStep, /我已了解本次重试会重新调用云端模型/)
    assert.match(modelStep, new RegExp(`<button[^>]*disabled[^>]*>\\s*${buttonLabel}`, 's'))
  }
  const html = await render()
  assert.match(html, /查看云端模型调用记录/)
  assert.match(html, /本次请求结果未知/)
  assert.match(html, /费用未知/)
  assert.match(html, /确认重试筛选评论/)
  assert.match(html, /<button[^>]*disabled[^>]*>\s*确认重试筛选评论/)
  const stale = await render({task:{...props.task,steps:[{...step,error_code:'comments_analysis_stale'}]}})
  assert.doesNotMatch(stale, /确认重试筛选评论/)
  assert.match(stale, /重新授权并分析评论/)
  const active = await render({task:{...props.task,status:'running'}})
  assert.doesNotMatch(active, /确认重试筛选评论/)
  const noRuns = await render({modelRuns: []})
  assert.doesNotMatch(noRuns, /查看云端模型调用记录/)
  const comments = await render({task:{...props.task,steps:[{...step,kind:'comments',status:'partial',error_code:null}]}, commentState:{stop_reason:'部分楼中楼未取得', collected_count:47, page_count:4}})
  assert.match(comments, /部分楼中楼未取得/)
  assert.match(comments, /47 条/)
  const running = await render({task:{...props.task,status:'running',steps:[{...step,status:'running',error_code:null}]}})
  assert.match(running, /单次连接或读取等待上限为 10 分钟/)
  assert.match(running, /取消不能立即中断当前云端请求/)
  assert.match(running, /并非整个步骤的总耗时上限/)
  console.log('TaskSteps retry safety rendering passed')
} finally { await server.close() }
