import { reactive } from 'vue'
import { api } from '../api/client'
import type { CreateTaskRequest, TaskDetail, TaskSummary } from '../api/types'

export const taskState = reactive({
  creating: false,
  current: null as TaskSummary | null,
  detail: null as TaskDetail | null,
})

export function useTasksStore() {
  async function createTask(request: CreateTaskRequest): Promise<TaskSummary> {
    taskState.creating = true
    taskState.detail = null
    try {
      const result = await api.createTask(request)
      taskState.current = result
      return result
    } finally {
      taskState.creating = false
    }
  }

  async function getTask(taskId: string): Promise<TaskDetail> {
    const result = await api.getTask(taskId)
    taskState.detail = result
    return result
  }

  return { state: taskState, createTask, getTask }
}
