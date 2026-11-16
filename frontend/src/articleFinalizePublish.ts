export interface ArticlePublishEntryState {
  visible: boolean
  enabled: boolean
  helperText: string
}

export function createArticleEditorSnapshot<T extends { body_markdown?: unknown }>(document: T): string {
  const bodyMarkdown = document.body_markdown
  if (typeof bodyMarkdown !== 'string') return JSON.stringify(document)

  return JSON.stringify({
    ...document,
    body_markdown: bodyMarkdown.replace(/(?:\r?\n)+$/u, ''),
  })
}

export function getArticlePublishEntryState(
  status: string | null | undefined,
  isDirty: boolean,
): ArticlePublishEntryState {
  if (status !== 'finalized') {
    return { visible: false, enabled: false, helperText: '' }
  }

  if (isDirty) {
    return {
      visible: true,
      enabled: false,
      helperText: '请先保存修改并重新设为定稿，再发布。',
    }
  }

  return { visible: true, enabled: true, helperText: '' }
}
