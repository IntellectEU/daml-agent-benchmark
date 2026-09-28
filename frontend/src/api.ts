import axios from 'axios'
import type { AgentMatrixResponse, AgentTaskDetailResponse } from './types'

const client = axios.create({
  baseURL: '/',
  headers: { 'Content-Type': 'application/json' },
})

export async function fetchAgentMatrix(params: { includeArchived: boolean }): Promise<AgentMatrixResponse> {
  const res = await client.get<AgentMatrixResponse>('/api/agent/matrix', {
    params: {
      include_archived: params.includeArchived,
    },
  })
  return res.data
}

export async function archiveAgentRuns(runIds: string[], reason: string): Promise<void> {
  await client.post('/api/agent/runs/archive', { run_ids: runIds, reason })
}

export async function deleteAgentRuns(runIds: string[]): Promise<void> {
  await client.post('/api/agent/runs/delete', { run_ids: runIds })
}

export async function fetchAgentTaskDetail(runId: string, taskId: string): Promise<AgentTaskDetailResponse> {
  const res = await client.get<AgentTaskDetailResponse>(
    `/api/agent/runs/${encodeURIComponent(runId)}/tasks/detail`,
    { params: { task_id: taskId } }
  )
  return res.data
}
