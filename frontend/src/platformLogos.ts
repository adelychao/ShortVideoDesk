const logoFiles: Record<string, string> = {
  wechat: 'wechat.svg',
  wechat_channels: 'wechat-channels.ico',
  csdn: 'csdn.svg',
  zhihu: 'zhihu.svg',
  xiaohongshu: 'xiaohongshu.svg',
  toutiao: 'toutiao.ico',
  baijiahao: 'baijiahao.png',
  weibo: 'weibo.svg',
  douyin: 'douyin.ico',
  kuaishou: 'kuaishou.svg',
  bilibili: 'bilibili.svg',
  tiktok: 'tiktok.svg',
  instagram: 'instagram.svg',
  twitter: 'twitter.svg',
  reddit: 'reddit.svg',
}

export function platformLogoUrl(platformId: string): string | undefined {
  const file = logoFiles[platformId]
  return file ? `${import.meta.env.BASE_URL}platform-logos/${file}` : undefined
}
