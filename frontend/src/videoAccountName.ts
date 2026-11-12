import type { VideoPublishAccount } from './api/types'

export function videoAccountName(account: VideoPublishAccount): string {
  return account.display_name || account.profile_name || (/^account_[a-f0-9]{32}$/.test(account.account_alias) ? `账号 ${account.id.slice(-6)}` : account.account_alias)
}
