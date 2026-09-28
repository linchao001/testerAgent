/** WP-F4 RetrievalDebugPage MSW：traces / snapshots / playground。 */

import { http, HttpResponse } from 'msw'
import type {
  PlaygroundOut,
  SnapshotDetail,
  SnapshotLine,
  SnapshotSummary,
  TraceDetail,
  TraceSummary,
} from '../api/domain'

const now = () => new Date().toISOString()

let lastPlayground: { query: string; stage: string } | null = null

const TRACE_SUMMARY: TraceSummary = {
  id: 'trace-1',
  task_id: 'task-f4',
  graph_run_id: 'run-1',
  stage: 'link_identify',
  stage_version: 1,
  node: 'link_identify',
  batch_id: null,
  query_count: 3,
  candidate_count: 4,
  kept_count: 2,
  injected_count: 2,
  referenced_count: 1,
  degraded_count: 1,
  created_at: now(),
}

const TRACE_DETAIL: TraceDetail = {
  id: 'trace-1',
  task_id: 'task-f4',
  graph_run_id: 'run-1',
  stage: 'link_identify',
  stage_version: 1,
  node: 'link_identify',
  batch_id: null,
  query_variant: {
    queries: [
      { channel: 'raw', text: '下单' },
      { channel: 'keyword', text: '订单 提交' },
    ],
  },
  candidates: [
    {
      entry_id: 'e1',
      entry_version: 'h1',
      title: '下单链路',
      score: 0.9,
      source_channel: 'raw:0',
      entry_type: 'link_index',
      kept: true,
      drop_reason: null,
    },
    {
      entry_id: 'e2',
      entry_version: 'h2',
      title: '支付规则',
      score: 0.7,
      source_channel: 'keyword:1',
      entry_type: 'business',
      kept: true,
      drop_reason: null,
    },
    {
      entry_id: 'e3',
      entry_version: 'h3',
      title: '无关文档',
      score: 0.2,
      source_channel: 'raw:0',
      entry_type: 'defect',
      kept: false,
      drop_reason: 'filtered_type',
    },
    {
      entry_id: 'e4',
      entry_version: 'h4',
      title: '截断条目',
      score: 0.5,
      source_channel: 'raw:0',
      entry_type: 'business',
      kept: false,
      drop_reason: 'rerank_cutoff',
    },
  ],
  injected_ids: ['e1', 'e2'],
  referenced_ids: ['e1'],
  hallucinated_ids: [],
  weak_ref_ids: [],
  degraded: [
    { step: 'meta_filter', reason: 'metadata_filter_unavailable', fallback: 'mirror' },
  ],
  created_at: now(),
}

const SNAP_FULL: SnapshotSummary = {
  id: 'snap-full',
  task_id: 'task-f4',
  graph_run_id: 'run-1',
  stage: 'link_identify',
  stage_version: 1,
  node: 'link_identify',
  batch_id: null,
  item_count: 2,
  total_tokens_est: 120,
  budget: 12000,
  truncated: false,
  has_full: true,
  prompt_template_ver: '2026-09-26.1',
  created_at: now(),
}

const SNAP_META: SnapshotSummary = {
  id: 'snap-meta',
  task_id: 'task-f4',
  graph_run_id: 'run-1',
  stage: 'point_write',
  stage_version: 1,
  node: 'point_write',
  batch_id: 'b0',
  item_count: 1,
  total_tokens_est: 40,
  budget: 16000,
  truncated: false,
  has_full: false,
  prompt_template_ver: '2026-09-26.1',
  created_at: now(),
}

const SNAP_FULL_DETAIL: SnapshotDetail = {
  ...SNAP_FULL,
  items: [
    {
      entry_id: 'e1',
      entry_version: 'h1',
      title: '下单链路',
      tokens_est: 60,
      position: 0,
      char_offset: 0,
      byte_length: 100,
    },
    {
      entry_id: 'e2',
      entry_version: 'h2',
      title: '支付规则',
      tokens_est: 60,
      position: 1,
      char_offset: 101,
      byte_length: 80,
    },
  ],
  model_ref: { provider: 'openai', model: 'fake' },
  usage: { prompt_tokens: 10, completion_tokens: 5 },
  latencies: { recall: 12, assemble: 3 },
}

const SNAP_META_DETAIL: SnapshotDetail = {
  ...SNAP_META,
  items: [
    {
      entry_id: 'e9',
      entry_version: 'h9',
      title: '仅 meta',
      tokens_est: 40,
      position: 0,
      char_offset: null,
      byte_length: null,
    },
  ],
  model_ref: {},
  usage: {},
  latencies: {},
}

const FULL_LINES: Record<number, SnapshotLine> = {
  0: {
    position: 0,
    entry_id: 'e1',
    entry_version: 'h1',
    title: '下单链路',
    content: 'FULL_PASSAGE_下单主流程正文',
  },
  1: {
    position: 1,
    entry_id: 'e2',
    entry_version: 'h2',
    title: '支付规则',
    content: 'FULL_PASSAGE_支付规则正文',
  },
}

export function resetDebugMockState() {
  lastPlayground = null
}

export function getLastPlaygroundBody() {
  return lastPlayground
}

export const debugHandlers = [
  http.get('/api/v1/tasks/:taskId/traces', ({ params, request }) => {
    if (params.taskId !== 'task-f4') {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    const url = new URL(request.url)
    const stage = url.searchParams.get('stage')
    const items = stage && stage !== TRACE_SUMMARY.stage ? [] : [TRACE_SUMMARY]
    return HttpResponse.json({ items, next_cursor: null })
  }),

  http.get('/api/v1/traces/:traceId', ({ params }) => {
    if (params.traceId !== 'trace-1') {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json(TRACE_DETAIL)
  }),

  http.get('/api/v1/tasks/:taskId/snapshots', ({ params }) => {
    if (params.taskId !== 'task-f4') {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json({
      items: [SNAP_FULL, SNAP_META],
      next_cursor: null,
    })
  }),

  http.get('/api/v1/snapshots/:snapshotId', ({ params }) => {
    if (params.snapshotId === 'snap-full') {
      return HttpResponse.json(SNAP_FULL_DETAIL)
    }
    if (params.snapshotId === 'snap-meta') {
      return HttpResponse.json(SNAP_META_DETAIL)
    }
    return HttpResponse.json(
      {
        error: {
          code: 'NOT_FOUND',
          message: 'missing',
          retryable: false,
          details: {},
        },
      },
      { status: 404 },
    )
  }),

  http.get('/api/v1/snapshots/:snapshotId/items/:position', ({ params }) => {
    if (params.snapshotId !== 'snap-full') {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'full snapshot only',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    const pos = Number(params.position)
    const line = FULL_LINES[pos]
    if (!line) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'position',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json(line)
  }),

  http.post(
    '/api/v1/workspaces/:workspaceId/retrieval/playground',
    async ({ request, params }) => {
      if (params.workspaceId !== 'ws-f4') {
        return HttpResponse.json(
          {
            error: {
              code: 'NOT_FOUND',
              message: 'ws missing',
              retryable: false,
              details: {},
            },
          },
          { status: 404 },
        )
      }
      const body = (await request.json()) as { query: string; stage: string }
      lastPlayground = { query: body.query, stage: body.stage }
      const out: PlaygroundOut = {
        funnel: {
          total: 3,
          kept: 1,
          errors: 0,
          dropped: { filtered_type: 1, rerank_cutoff: 1 },
        },
        candidates: TRACE_DETAIL.candidates.slice(0, 3),
        injected: [
          {
            entry_id: 'e1',
            entry_version: 'h1',
            title: '下单链路',
            tokens_est: 60,
            position: 0,
          },
        ],
        degraded: [
          { step: 'rerank', reason: 'llm_failed', fallback: 'rule_score' },
        ],
        latencies: { multi_query: 5, recall: 10 },
        token_est: 60,
        truncated: false,
      }
      return HttpResponse.json(out)
    },
  ),
]
