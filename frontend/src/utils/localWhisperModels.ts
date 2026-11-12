const modelDescriptions: Record<string, { name: string; advice: string }> = {
  tiny: { name: '极速版', advice: '适合快速试用，但容易遗漏或误识别；对准确度要求高时建议换更大模型。' },
  base: { name: '轻量版', advice: '资源占用较低，适合电脑性能有限或希望较快完成转写时使用。' },
  small: { name: '均衡版', advice: '适合多数中文口播，可先用它测试效果；CPU 转写可能需要一定时间。' },
  medium: { name: '增强版', advice: '可用于对文字质量要求更高的素材；通常更慢，占用更多资源。' },
  'large-v3': { name: '高质量版', advice: '适合愿意等待并有充足计算资源的情况；CPU 处理较慢且占用较高。' },
}

export function localModelLabel(id: string): string {
  const name = modelDescriptions[id]?.name
  return name ? `${name}（${id}）${id === 'small' ? ' · 推荐试用' : ''}` : id
}

export function localModelAdvice(id: string): string {
  return modelDescriptions[id]?.advice ?? '识别效果和速度取决于音频内容及本机性能。'
}
