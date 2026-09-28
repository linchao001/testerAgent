/** WP-F1 ChatPage MSW：工作区/会话/任务 REST（SSE 由测试注入 EventSource）。 */

import { http, HttpResponse } from 'msw'
import type { Message, Task, Workspace } from '../api/domain'

const now = () => new Date().toISOString()

let workspace: Workspace = {
  id: 'ws-f1',
  name: '默认工作区',
  description: '',
  kb_config: {
    mode: 'service',
    target: 'http://127.0.0.1:8182',
    kb_id: 'zhb_kb',
  },
  created_at: now(),
}

let conversationId = 'conv-f1'
let task: Task | null = null
const messages: Message[] = []
let msgSeq = 0

function pushMessage(
  partial: Omit<Message, 'id' | 'created_at' | 'author'> & {
    author?: string
  },
): Message {
  msgSeq += 1
  const m: Message = {
    id: `msg-${msgSeq}`,
    created_at: now(),
    author:
      partial.author ??
      (partial.role === 'user'
        ? '用户'
        : partial.role === 'system'
          ? '系统'
          : '助手'),
    ...partial,
  }
  messages.unshift(m)
  return m
}

export function resetChatMockState() {
  workspace = {
    id: 'ws-f1',
    name: '默认工作区',
    description: '',
    kb_config: {
      mode: 'service',
      target: 'http://127.0.0.1:8182',
      kb_id: 'zhb_kb',
    },
    created_at: now(),
  }
  conversationId = 'conv-f1'
  task = null
  messages.length = 0
  msgSeq = 0
}

export function getMockTask(): Task | null {
  return task
}

export const chatHandlers = [
  http.get('/api/v1/workspaces', () =>
    HttpResponse.json({ items: [workspace], next_cursor: null }),
  ),

  http.post('/api/v1/workspaces', async ({ request }) => {
    const body = (await request.json()) as {
      name: string
      description?: string
      kb_config: Workspace['kb_config']
    }
    workspace = {
      id: 'ws-f1-new',
      name: body.name,
      description: body.description ?? '',
      kb_config: body.kb_config,
      created_at: now(),
    }
    return HttpResponse.json(workspace, {
      status: 201,
      headers: { Location: `/api/v1/workspaces/${workspace.id}` },
    })
  }),

  http.post('/api/v1/conversations', async ({ request }) => {
    const body = (await request.json()) as {
      workspace_id: string
      title?: string | null
    }
    conversationId = 'conv-f1'
    return HttpResponse.json(
      {
        id: conversationId,
        workspace_id: body.workspace_id,
        title: body.title ?? '用例设计会话',
        created_at: now(),
        updated_at: now(),
      },
      { status: 201 },
    )
  }),

  http.get('/api/v1/conversations/:id/messages', () =>
    HttpResponse.json({ items: messages, next_cursor: null }),
  ),

  http.post('/api/v1/conversations/:id/messages', async ({ request, params }) => {
    const body = (await request.json()) as {
      content: string
      kind?: 'chat' | 'change_request'
      context?: Record<string, unknown> | null
    }
    const user = pushMessage({
      conversation_id: String(params.id),
      task_id: task?.id ?? null,
      role: 'user',
      kind: body.kind ?? 'chat',
      content: body.content,
      ref_artifact_id: null,
      payload: body.context ?? {},
    })
    let assistant = null
    if ((body.kind ?? 'chat') === 'chat') {
      assistant = pushMessage({
        conversation_id: String(params.id),
        task_id: task?.id ?? null,
        role: 'assistant',
        kind: 'chat',
        content: '（mock）已收到。',
        ref_artifact_id: null,
        payload: { tool_trace: [] },
      })
    }
    return HttpResponse.json({ user, assistant }, { status: 201 })
  }),

  http.post('/api/v1/tasks', async ({ request }) => {
    const body = (await request.json()) as {
      conversation_id: string
      requirement_md: string
    }
    task = {
      id: 'task-f1',
      workspace_id: workspace.id,
      conversation_id: body.conversation_id,
      status: 'waiting_input',
      current_stage: 'intake',
      active_artifacts: {},
      progress: {},
      error_info: null,
      stale: false,
      created_at: now(),
      updated_at: now(),
    }
    pushMessage({
      conversation_id: body.conversation_id,
      task_id: task.id,
      role: 'user',
      kind: 'chat',
      content: body.requirement_md.slice(0, 200),
      ref_artifact_id: null,
      payload: {},
    })
    return HttpResponse.json(task, {
      status: 201,
      headers: { Location: `/api/v1/tasks/${task.id}` },
    })
  }),

  http.get('/api/v1/tasks/:id', ({ params }) => {
    if (!task || task.id !== params.id) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'task missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json(task)
  }),

  http.post('/api/v1/tasks/:id/run', ({ params }) => {
    if (!task || task.id !== params.id) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'task missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }
    task = {
      ...task,
      status: 'running',
      current_stage: 'intake',
      updated_at: now(),
    }
    return HttpResponse.json({
      task_id: task.id,
      graph_run_id: 'run-f1',
      events_url: `/api/v1/tasks/${task.id}/events`,
      resume_from: {},
    })
  }),

  http.post('/api/v1/tasks/:id/answer', async ({ request, params }) => {
    if (!task || task.id !== params.id) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'task missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }
    const body = (await request.json()) as {
      answers: Array<{ question_id: string; answer: string }>
    }
    pushMessage({
      conversation_id: task.conversation_id,
      task_id: task.id,
      role: 'user',
      kind: 'clarification_qa',
      content: body.answers.map((a) => a.answer).join('\n'),
      ref_artifact_id: null,
      payload: { answers: body.answers, answered: true },
    })
    task = { ...task, status: 'running', updated_at: now() }
    return HttpResponse.json({ task_id: task.id, status: task.status })
  }),

  http.post('/api/v1/tasks/:id/rollback', async ({ request, params }) => {
    if (!task || task.id !== params.id) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'task missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }
    const body = (await request.json()) as {
      target_stage: string
      artifact_id: string
      expected_version: number
    }
    task = {
      ...task,
      status: 'running',
      current_stage: body.target_stage,
      updated_at: now(),
    }
    return HttpResponse.json({
      graph_run_id: 'run-rollback-1',
      impact: {
        target_stage: body.target_stage,
        affected_point_ids: ['pt-1'],
        summary: '影响 1 条测试点，下游用例将重生成',
        downstream: [],
      },
    })
  }),
]

/** 模拟到达 CP1：更新 mock task 的 active artifact（供 change_request 回退）。 */
export function mockArriveCheckpoint() {
  if (!task) return
  task = {
    ...task,
    status: 'waiting_confirm',
    current_stage: 'link_identify',
    active_artifacts: {
      link_identify: {
        id: 'art-link-1',
        stage: 'link_identify',
        stage_version: 1,
        origin: 'system',
        status: 'active',
        confirmed_by: null,
        created_at: now(),
      },
    },
    updated_at: now(),
  }
}
