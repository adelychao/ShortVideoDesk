import { reactive } from 'vue'
import { ApiError, api } from '../api/client'
import type { PlatformStatus, WorkbenchHealth } from '../api/types'

export const configState = reactive({
  health: null as WorkbenchHealth | null,
  platforms: [] as PlatformStatus[],
  loading: false,
  error: '',
})

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : '无法读取本机工作台状态。'
}

export function useConfigStore() {
  async function loadOverview(): Promise<void> {
    configState.loading = true
    configState.error = ''
    const [healthResult, platformsResult] = await Promise.allSettled([
      api.getHealth(),
      api.getPlatforms(),
    ])

    if (healthResult.status === 'fulfilled') configState.health = healthResult.value
    else configState.error = messageFor(healthResult.reason)

    if (platformsResult.status === 'fulfilled') configState.platforms = platformsResult.value
    else if (!configState.error) configState.error = messageFor(platformsResult.reason)

    configState.loading = false
  }

  return { state: configState, loadOverview }
}
