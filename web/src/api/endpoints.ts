/** 类型化端点（dd §10.2；WP-F1/F2/F3 所需子集）。 */

import { apiGetPage, apiRequest } from './client'
import type {
  Agent,
  ArtifactDetail,
  CaseDetail,
  CaseSummary,
  ConfirmOut,
  Conversation,
  KbConfig,
  KbTestOut,
  Message,
  ModelConfig,
  ModelTestOut,
  PlaygroundOut,
  ReviewAction,
  ReviewOut,
  RollbackOut,
  RunHandle,
  SendMessageOut,
  SnapshotDetail,
  SnapshotLine,
  SnapshotSummary,
  StageName,
  Task,
  TraceDetail,
  TraceSummary,
  Workspace,
} from './domain'
import type { Page } from './types'

export function listWorkspaces(limit = 50): Promise<Page<Workspace>> {
  return apiGetPage<Workspace>(`/api/v1/workspaces?limit=${limit}`)
}

export function getWorkspace(id: string): Promise<Workspace> {
  return apiRequest<Workspace>(
    `/api/v1/workspaces/${encodeURIComponent(id)}`,
  )
}

export function createWorkspace(body: {
  name: string
  description?: string
  kb_config: KbConfig
}): Promise<Workspace> {
  return apiRequest<Workspace>('/api/v1/workspaces', {
    method: 'POST',
    body,
  })
}

export function updateWorkspace(
  id: string,
  body: {
    name?: string
    description?: string
    kb_config?: KbConfig
  },
): Promise<Workspace> {
  return apiRequest<Workspace>(
    `/api/v1/workspaces/${encodeURIComponent(id)}`,
    { method: 'PUT', body },
  )
}

export function deleteWorkspace(id: string): Promise<{ ok: boolean }> {
  return apiRequest<{ ok: boolean }>(
    `/api/v1/workspaces/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  )
}

export function testKb(workspaceId: string): Promise<KbTestOut> {
  return apiRequest<KbTestOut>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/kb/test`,
    { method: 'POST', body: {} },
  )
}

export function listAgents(limit = 50): Promise<Page<Agent>> {
  return apiGetPage<Agent>(`/api/v1/agents?limit=${limit}`)
}

export function listWorkspaceAgents(
  workspaceId: string,
  limit = 50,
): Promise<Page<Agent>> {
  return apiGetPage<Agent>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/agents?limit=${limit}`,
  )
}

export function bindWorkspaceAgent(
  workspaceId: string,
  agentId: string,
): Promise<Agent> {
  return apiRequest<Agent>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/agents`,
    { method: 'POST', body: { agent_id: agentId } },
  )
}

export function getModelConfig(): Promise<ModelConfig> {
  return apiRequest<ModelConfig>('/api/v1/config/model')
}

export function putModelConfig(body: ModelConfig): Promise<ModelConfig> {
  return apiRequest<ModelConfig>('/api/v1/config/model', {
    method: 'PUT',
    body,
  })
}

export function testModelConfig(): Promise<ModelTestOut> {
  return apiRequest<ModelTestOut>('/api/v1/config/model/test', {
    method: 'POST',
    body: {},
  })
}

export function createConversation(body: {
  workspace_id: string
  title?: string | null
}): Promise<Conversation> {
  return apiRequest<Conversation>('/api/v1/conversations', {
    method: 'POST',
    body,
  })
}

export function getConversation(id: string): Promise<
  Conversation & {
    tasks: Array<{
      id: string
      status: string
      current_stage: string
      created_at: string
      updated_at: string
    }>
    messages: Page<Message>
  }
> {
  return apiRequest(`/api/v1/conversations/${encodeURIComponent(id)}`)
}

export function listMessages(
  conversationId: string,
  limit = 50,
): Promise<Page<Message>> {
  return apiGetPage<Message>(
    `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages?limit=${limit}`,
  )
}

export function sendMessage(
  conversationId: string,
  body: {
    content: string
    kind?: 'chat' | 'change_request'
    context?: Record<string, unknown> | null
  },
): Promise<SendMessageOut> {
  return apiRequest<SendMessageOut>(
    `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
    { method: 'POST', body },
  )
}

export function createTask(body: {
  conversation_id: string
  requirement_md: string
  snapshot_level?: 'off' | 'meta' | 'full' | null
}): Promise<Task> {
  return apiRequest<Task>('/api/v1/tasks', { method: 'POST', body })
}

export function getTask(taskId: string): Promise<Task> {
  return apiRequest<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
}

export function getArtifact(artifactId: string): Promise<ArtifactDetail> {
  return apiRequest<ArtifactDetail>(
    `/api/v1/artifacts/${encodeURIComponent(artifactId)}`,
  )
}

export function confirmTask(
  taskId: string,
  body: {
    gate_kind: 'plan_confirm' | 'review_decision'
    artifact_id: string
    action: 'confirm' | 'modify' | 'reject_rerun'
    expected_version?: number | null
    stage?: StageName | string | null
    payload?: Record<string, unknown> | null
  },
): Promise<ConfirmOut> {
  return apiRequest<ConfirmOut>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/confirm`,
    { method: 'POST', body },
  )
}

export function getTaskPlan(taskId: string): Promise<{
  plan_id: string
  version: number
  goal: string
  steps: Array<Record<string, unknown>>
  status: string
  replan_count: number
}> {
  return apiRequest(`/api/v1/tasks/${encodeURIComponent(taskId)}/plan`)
}

export function runTask(taskId: string): Promise<RunHandle> {
  return apiRequest<RunHandle>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/run`,
    { method: 'POST', body: {} },
  )
}

export function answerTask(
  taskId: string,
  answers: Array<{ question_id: string; answer: string }>,
): Promise<{ task_id: string; status: string }> {
  return apiRequest(`/api/v1/tasks/${encodeURIComponent(taskId)}/answer`, {
    method: 'POST',
    body: { answers },
  })
}

export function rollbackTask(
  taskId: string,
  body: {
    target_stage: StageName
    artifact_id: string
    expected_version: number
    revised_artifact?: Record<string, unknown> | null
  },
): Promise<RollbackOut> {
  return apiRequest<RollbackOut>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/rollback`,
    { method: 'POST', body },
  )
}

export function listCases(
  taskId: string,
  opts: {
    status?: string
    review?: string
    version?: number
    limit?: number
    cursor?: string | null
  } = {},
): Promise<Page<CaseSummary>> {
  const q = new URLSearchParams()
  if (opts.status) q.set('status', opts.status)
  if (opts.review) q.set('review', opts.review)
  if (opts.version != null) q.set('version', String(opts.version))
  q.set('limit', String(opts.limit ?? 100))
  if (opts.cursor) q.set('cursor', opts.cursor)
  const qs = q.toString()
  return apiGetPage<CaseSummary>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/cases${qs ? `?${qs}` : ''}`,
  )
}

export function getCase(caseId: string): Promise<CaseDetail> {
  return apiRequest<CaseDetail>(
    `/api/v1/cases/${encodeURIComponent(caseId)}`,
  )
}

export function updateCase(
  caseId: string,
  markdown: string,
  contentHash: string,
): Promise<CaseDetail> {
  return apiRequest<CaseDetail>(
    `/api/v1/cases/${encodeURIComponent(caseId)}`,
    {
      method: 'PUT',
      body: { markdown },
      headers: { 'If-Match': contentHash },
    },
  )
}

export function reviewCases(
  items: Array<{ case_id: string; action: ReviewAction }>,
): Promise<ReviewOut> {
  return apiRequest<ReviewOut>('/api/v1/cases/review', {
    method: 'POST',
    body: { items },
  })
}

export function listTraces(
  taskId: string,
  opts: { stage?: string; version?: number; limit?: number } = {},
): Promise<Page<TraceSummary>> {
  const q = new URLSearchParams()
  if (opts.stage) q.set('stage', opts.stage)
  if (opts.version != null) q.set('version', String(opts.version))
  q.set('limit', String(opts.limit ?? 50))
  const qs = q.toString()
  return apiGetPage<TraceSummary>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/traces?${qs}`,
  )
}

export function getTrace(traceId: string): Promise<TraceDetail> {
  return apiRequest<TraceDetail>(
    `/api/v1/traces/${encodeURIComponent(traceId)}`,
  )
}

export function listSnapshots(
  taskId: string,
  limit = 50,
): Promise<Page<SnapshotSummary>> {
  return apiGetPage<SnapshotSummary>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/snapshots?limit=${limit}`,
  )
}

export function getSnapshot(snapshotId: string): Promise<SnapshotDetail> {
  return apiRequest<SnapshotDetail>(
    `/api/v1/snapshots/${encodeURIComponent(snapshotId)}`,
  )
}

export function getSnapshotItem(
  snapshotId: string,
  position: number,
): Promise<SnapshotLine> {
  return apiRequest<SnapshotLine>(
    `/api/v1/snapshots/${encodeURIComponent(snapshotId)}/items/${position}`,
  )
}

export function runPlayground(
  workspaceId: string,
  body: {
    query: string
    stage: string
    overrides?: Record<string, unknown> | null
  },
): Promise<PlaygroundOut> {
  return apiRequest<PlaygroundOut>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/retrieval/playground`,
    { method: 'POST', body },
  )
}
