import axios from 'axios'
import type {
  AgentMatrixResponse,
  AgentTaskDetailResponse,
  AgentTaskKind,
  MutationCatalogueResponse,
  TaskMutationsResponse,
} from './types'

const client = axios.create({
  baseURL: '/',
  headers: { 'Content-Type': 'application/json' },
})

export async function fetchAgentMatrix(params: {
  includeArchived: boolean
  kind: AgentTaskKind
}): Promise<AgentMatrixResponse> {
  const res = await client.get<AgentMatrixResponse>('/api/agent/matrix', {
    params: {
      include_archived: params.includeArchived,
      kind: params.kind,
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

export async function fetchMutationCatalogue(): Promise<MutationCatalogueResponse> {
  const res = await client.get<MutationCatalogueResponse>('/api/agent/mutations')
  return res.data
}

export async function fetchTaskMutations(fileName: string): Promise<TaskMutationsResponse> {
  const res = await client.get<TaskMutationsResponse>(`/api/agent/mutations/${encodeURIComponent(fileName)}`)
  return res.data
}
