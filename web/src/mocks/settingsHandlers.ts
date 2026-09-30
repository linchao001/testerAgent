/** WP-F5 Workspaces/Settings MSW。 */

import { http, HttpResponse } from 'msw'
import type {
  Agent,
  KbConfig,
  KbTestOut,
  ModelConfig,
  Workspace,
} from '../api/domain'

const now = () => new Date().toISOString()

const BUILTIN_AGENT: Agent = {
  id: 'builtin-case-designer',
  name: '用例设计智能体',
  agent_type: 'case_designer',
  config: { snapshot_level: 'meta' },
  builtin: true,
  created_at: now(),
}

let workspaces: Workspace[] = []
let boundByWs: Record<string, Set<string>> = {}
let modelConfig: ModelConfig = {
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.2,
  top_p: 1.0,
  timeout: 120,
}
/** 有活跃任务的工作区 id → DELETE 返回 409 */
let blockedDeleteIds = new Set<string>()
let seq = 0

function envelope(
  status: number,
  code: string,
  message: string,
  retryable = false,
  details: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    { error: { code, message, retryable, details } },
    { status },
  )
}

export function resetSettingsMockState() {
  workspaces = [
    {
      id: 'ws-f5',
      name: '演示工作区',
      description: 'WP-F5 mock',
      root_dir: '',
      kb_config: {
        kb_id: 'zhb_kb',
        options: {},
      },
      created_at: now(),
    },
  ]
  boundByWs = { 'ws-f5': new Set() }
  modelConfig = {
    base_url: '',
    api_key: '',
    model: '',
    temperature: 0.2,
    top_p: 1.0,
    timeout: 120,
  }
  blockedDeleteIds = new Set()
  seq = 0
}

export function setDeleteBlocked(workspaceId: string, blocked: boolean) {
  if (blocked) blockedDeleteIds.add(workspaceId)
  else blockedDeleteIds.delete(workspaceId)
}

export function getMockWorkspaces(): Workspace[] {
  return workspaces
}

export function getMockModelConfig(): ModelConfig {
  return { ...modelConfig }
}

export const settingsHandlers = [
  http.get('/api/v1/workspaces', () =>
    HttpResponse.json({ items: workspaces, next_cursor: null }),
  ),

  http.get('/api/v1/workspaces/:id', ({ params }) => {
    const ws = workspaces.find((w) => w.id === params.id)
    if (!ws) return envelope(404, 'NOT_FOUND', '工作区不存在')
    return HttpResponse.json(ws)
  }),

  http.post('/api/v1/workspaces', async ({ request }) => {
    const body = (await request.json()) as {
      name: string
      description?: string
      root_dir?: string
      kb_config: KbConfig
    }
    seq += 1
    const ws: Workspace = {
      id: `ws-new-${seq}`,
      name: body.name,
      description: body.description ?? '',
      root_dir: body.root_dir ?? '',
      kb_config: body.kb_config,
      created_at: now(),
    }
    workspaces = [...workspaces, ws]
    boundByWs[ws.id] = new Set()
    return HttpResponse.json(ws, {
      status: 201,
      headers: { Location: `/api/v1/workspaces/${ws.id}` },
    })
  }),

  http.put('/api/v1/workspaces/:id', async ({ params, request }) => {
    const idx = workspaces.findIndex((w) => w.id === params.id)
    if (idx < 0) return envelope(404, 'NOT_FOUND', '工作区不存在')
    const body = (await request.json()) as {
      name?: string
      description?: string
      root_dir?: string
      kb_config?: KbConfig
    }
    const prev = workspaces[idx]
    const next: Workspace = {
      ...prev,
      name: body.name ?? prev.name,
      description: body.description ?? prev.description,
      root_dir: body.root_dir ?? prev.root_dir,
      kb_config: body.kb_config ?? prev.kb_config,
    }
    workspaces = [
      ...workspaces.slice(0, idx),
      next,
      ...workspaces.slice(idx + 1),
    ]
    return HttpResponse.json(next)
  }),

  http.delete('/api/v1/workspaces/:id', ({ params }) => {
    const id = String(params.id)
    if (!workspaces.some((w) => w.id === id)) {
      return envelope(404, 'NOT_FOUND', '工作区不存在')
    }
    if (blockedDeleteIds.has(id)) {
      return envelope(
        409,
        'TASK_STATE_CONFLICT',
        '工作区存在活跃任务，无法删除',
        false,
        { workspace_id: id },
      )
    }
    workspaces = workspaces.filter((w) => w.id !== id)
    delete boundByWs[id]
    return HttpResponse.json({ ok: true })
  }),

  http.post('/api/v1/workspaces/:id/kb/test', ({ params }) => {
    const ws = workspaces.find((w) => w.id === params.id)
    if (!ws) return envelope(404, 'NOT_FOUND', '工作区不存在')
    const out: KbTestOut = {
      ok: true,
      latency_ms: 42,
      capabilities: {
        metadata_filter: false,
        entry_version: false,
        passage_api: true,
      },
      error_code: null,
    }
    return HttpResponse.json(out)
  }),

  http.get('/api/v1/agents', () =>
    HttpResponse.json({ items: [BUILTIN_AGENT], next_cursor: null }),
  ),

  http.get('/api/v1/workspaces/:id/agents', ({ params }) => {
    const id = String(params.id)
    if (!workspaces.some((w) => w.id === id)) {
      return envelope(404, 'NOT_FOUND', '工作区不存在')
    }
    const bound = boundByWs[id] ?? new Set()
    const items = [BUILTIN_AGENT].filter((a) => bound.has(a.id))
    return HttpResponse.json({ items, next_cursor: null })
  }),

  http.post('/api/v1/workspaces/:id/agents', async ({ params, request }) => {
    const id = String(params.id)
    if (!workspaces.some((w) => w.id === id)) {
      return envelope(404, 'NOT_FOUND', '工作区不存在')
    }
    const body = (await request.json()) as { agent_id: string }
    if (body.agent_id !== BUILTIN_AGENT.id) {
      return envelope(404, 'NOT_FOUND', '资源不存在')
    }
    if (!boundByWs[id]) boundByWs[id] = new Set()
    boundByWs[id].add(body.agent_id)
    return HttpResponse.json(BUILTIN_AGENT)
  }),

  http.get('/api/v1/config/model', () => HttpResponse.json(modelConfig)),

  http.put('/api/v1/config/model', async ({ request }) => {
    const body = (await request.json()) as ModelConfig
    modelConfig = { ...body }
    return HttpResponse.json(modelConfig)
  }),

  http.post('/api/v1/config/model/test', () => {
    if (!modelConfig.base_url || !modelConfig.api_key || !modelConfig.model) {
      return envelope(400, 'LLM_BAD_REQUEST', '模型未配置完整', false)
    }
    return HttpResponse.json({
      ok: true,
      latency_ms: 88,
      model: modelConfig.model,
      error_code: null,
    })
  }),
]
