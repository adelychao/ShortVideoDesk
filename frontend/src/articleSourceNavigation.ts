export type ArticleSourceLeaveDecision = 'allow' | 'block' | 'confirm'

export function getArticleSourceLeaveDecision(input: {
  hasContent: boolean
  submitting: boolean
  generationSucceeded: boolean
}): ArticleSourceLeaveDecision {
  if (input.generationSucceeded) return 'allow'
  if (input.submitting) return 'block'
  if (input.hasContent) return 'confirm'
  return 'allow'
}

export async function closeArticleSourceTabAfterNavigation<T>(
  navigation: Promise<T>,
  isNavigationFailure: (result: T) => boolean,
  waitForRouteSync: () => Promise<unknown>,
  closeSourceTab: () => Promise<void>,
): Promise<boolean> {
  const result = await navigation
  if (isNavigationFailure(result)) return false
  await waitForRouteSync()
  await closeSourceTab()
  return true
}
