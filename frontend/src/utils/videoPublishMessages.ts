const legacyMessageRewrites: Array<[string, string]> = [
  ['正在调用 SAU 上传。', '正在上传视频…'],
  ['SAU 发布流程已结束，平台作品状态等待单独同步确认。', '发布任务已提交，请到平台确认作品状态。'],
  ['SAU 视频号草稿保存流程已结束，请在视频号草稿箱核实。', '草稿保存请求已提交，请到视频号草稿箱确认。'],
  ['SAU 上传超过最长等待时间，平台是否接收作品未知；请先检查平台后台。', '上传超时，平台是否接收作品未知；请先检查平台后台。'],
  ['SAU 返回异常，平台是否接收作品未知；请先检查平台后台。', '发布未能正常完成，平台是否接收作品未知；请先检查平台后台。'],
  ['应用正在关闭，SAU 命令尚未启动；请在平台后台核实后再处理。', '应用正在关闭，发布任务尚未开始；请先检查平台后台。'],
]

export function videoPublishUserMessage(message: string | null | undefined): string {
  if (!message) return ''
  const normalized = message.trim()
  for (const [legacy, friendly] of legacyMessageRewrites) {
    if (normalized.startsWith(legacy)) return `${friendly}${normalized.slice(legacy.length)}`
  }
  return normalized
}
