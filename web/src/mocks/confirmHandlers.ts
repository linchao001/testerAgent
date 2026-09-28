/** WP-F2 StageConfirmPage MSW：artifact 详情 + confirm/modify + 冲突。 */

import { http, HttpResponse } from 'msw'
import type {
  ArtifactDetail,
  LinkPlan,
  PointPlan,
  Task,
} from '../api/domain'

const now = () => new Date().toISOString()

export const SAMPLE_LINK_PLAN: LinkPlan = {
  links: [
    {
      link_id: 'L1',
      title: '下单链路',
      summary: '用户下单主流程',
      hit: true,
      entry_id: 'e-link-1',
      entry_version: 'h-abc',
      confidence: 0.9,
      story_ids: ['S1'],
    },
    {
      link_id: 'new-link-1',
      title: '疑似新链路',
      summary: '知识库未命中',
      hit: false,
      entry_id: null,
      entry_version: null,
      confidence: 0.3,
      story_ids: ['new-story-1'],
    },
  ],
  stories: [
    {
      story_id: 'S1',
      link_id: 'L1',
      title: '提交订单',
      summary: '用户提交订单',
      hit: true,
      entry_id: 'e-story-1',
      entry_version: 'h-def',
      confidence: 0.85,
      rationale: '需求条款 h2-1',
      related_clause_ids: ['h2-1'],
    },
    {
      story_id: 'new-story-1',
      link_id: 'new-link-1',
      title: '新故事',
      summary: '待绑定',
      hit: false,
      entry_id: null,
      entry_version: null,
      confidence: 0.25,
      rationale: '新增建议',
      related_clause_ids: [],
    },
  ],
  new_suggestions: [],
}

export const SAMPLE_POINT_PLAN: PointPlan = {
  points: [
    {
      point_id: 'pt-1-1',
      story_id: 'S1',
      title: '正常提交订单',
      angle: '正常',
      method: '场景法',
      clause_ids: ['h2-1'],
      source_entry_ids: ['e-story-1'],
      priority: 'P0',
    },
  ],
}

let task: Task | null = null
let artifacts = new Map<string, ArtifactDetail>()
let confirmForceConflict = false
let lastConfirmBody: Record<string, unknown> | null = null

export function resetConfirmMockState() {
  confirmForceConflict = false
  lastConfirmBody = null
  artifacts = new Map()
  task = {
    id: 'task-f2',
    workspace_id: 'ws-f2',
    conversation_id: 'conv-f2',
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
    progress: {},
    error_info: null,
    stale: false,
    created_at: now(),
    updated_at: now(),
  }
  artifacts.set('art-link-1', {
    id: 'art-link-1',
    task_id: 'task-f2',
    stage: 'link_identify',
    stage_version: 1,
    origin: 'system',
    status: 'active',
    confirmed_by: null,
    created_at: now(),
    payload: structuredClone(SAMPLE_LINK_PLAN),
  })
}

export function seedPointCheckpoint() {
  if (!task) return
  task = {
    ...task,
    status: 'waiting_confirm',
    current_stage: 'point_write',
    active_artifacts: {
      ...task.active_artifacts,
      point_write: {
        id: 'art-point-1',
        stage: 'point_write',
        stage_version: 1,
        origin: 'system',
        status: 'active',
        confirmed_by: null,
        created_at: now(),
      },
    },
    updated_at: now(),
  }
  artifacts.set('art-point-1', {
    id: 'art-point-1',
    task_id: 'task-f2',
    stage: 'point_write',
    stage_version: 1,
    origin: 'system',
    status: 'active',
    confirmed_by: null,
    created_at: now(),
    payload: structuredClone(SAMPLE_POINT_PLAN),
  })
}

export function setConfirmForceConflict(v: boolean) {
  confirmForceConflict = v
}

export function getLastConfirmBody() {
  return lastConfirmBody
}

export function getMockConfirmTask() {
  return task
}

export const confirmHandlers = [
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

  http.get('/api/v1/artifacts/:id', ({ params }) => {
    const art = artifacts.get(String(params.id))
    if (!art) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'artifact missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json(art)
  }),

  http.post('/api/v1/tasks/:id/confirm', async ({ request, params }) => {
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
      stage: string
      artifact_id: string
      expected_version: number
      action: 'confirm' | 'modify'
      payload?: Record<string, unknown>
    }
    lastConfirmBody = body

    const art = artifacts.get(body.artifact_id)
    if (!art) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'artifact missing',
            retryable: false,
          },
        },
        { status: 404 },
      )
    }

    if (confirmForceConflict || art.stage_version !== body.expected_version) {
      // 模拟他页已修订：抬升版本供刷新后展示
      const bumped: ArtifactDetail = {
        ...art,
        stage_version: art.stage_version + 1,
        payload:
          art.stage === 'link_identify'
            ? {
                ...structuredClone(SAMPLE_LINK_PLAN),
                links: SAMPLE_LINK_PLAN.links.map((l) =>
                  l.link_id === 'L1'
                    ? { ...l, title: '他页已改标题' }
                    : l,
                ),
              }
            : structuredClone(SAMPLE_POINT_PLAN),
      }
      artifacts.set(art.id, bumped)
      if (task.active_artifacts[art.stage]) {
        task = {
          ...task,
          active_artifacts: {
            ...task.active_artifacts,
            [art.stage]: {
              ...task.active_artifacts[art.stage],
              stage_version: bumped.stage_version,
            },
          },
          updated_at: now(),
        }
      }
      return HttpResponse.json(
        {
          error: {
            code: 'VERSION_CONFLICT',
            message: `产物版本冲突：期望 v${body.expected_version}，实际 v${bumped.stage_version}`,
            retryable: false,
            details: {
              expected: body.expected_version,
              actual: bumped.stage_version,
            },
          },
        },
        { status: 409 },
      )
    }

    if (body.action === 'modify' && body.payload) {
      const newId = `${art.id}-v2`
      const newArt: ArtifactDetail = {
        ...art,
        id: newId,
        stage_version: art.stage_version + 1,
        origin: 'user_revised',
        confirmed_by: 'user',
        payload: body.payload,
        created_at: now(),
      }
      artifacts.set(art.id, { ...art, status: 'superseded' })
      artifacts.set(newId, newArt)
      task = {
        ...task,
        status: 'running',
        active_artifacts: {
          ...task.active_artifacts,
          [art.stage]: {
            id: newId,
            stage: art.stage,
            stage_version: newArt.stage_version,
            origin: 'user_revised',
            status: 'active',
            confirmed_by: 'user',
            created_at: newArt.created_at,
          },
        },
        updated_at: now(),
      }
      return HttpResponse.json({
        task_id: task.id,
        status: 'running',
        artifact_id: newId,
        stage_version: newArt.stage_version,
      })
    }

    artifacts.set(art.id, { ...art, confirmed_by: 'user' })
    task = { ...task, status: 'running', updated_at: now() }
    return HttpResponse.json({
      task_id: task.id,
      status: 'running',
      artifact_id: art.id,
      stage_version: art.stage_version,
    })
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
    const body = (await request.json()) as { target_stage: string }
    task = {
      ...task,
      status: 'running',
      current_stage: body.target_stage,
      updated_at: now(),
    }
    return HttpResponse.json({
      graph_run_id: 'run-rollback-f2',
      impact: {
        target_stage: body.target_stage,
        summary: '下游将重生成',
        downstream: [],
      },
    })
  }),
]
