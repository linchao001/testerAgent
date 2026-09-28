/** WP-F3 WorkbenchPage MSW：用例列表/详情/编辑 If-Match/批量评审。 */

import { http, HttpResponse } from 'msw'
import type { CaseDetail, CaseSummary, ReviewStatus } from '../api/domain'

const now = () => new Date().toISOString()

const SAMPLE_MD_V1 = `---
case_id: case-1
point_id: pt-1-1
stage_version: 1
priority: P0
content_hash: hash-v1
trace_refs:
  clause_ids: [h2-1]
  entry_ids: []
  point_ids: [pt-1-1]
---

# 正常提交订单

## 前置条件
- 用户已登录

## 步骤
1. 打开下单页
   - 预期：页面加载成功
2. 点击提交
   - 预期：订单创建成功

## 说明
（由系统生成）
`

const SAMPLE_MD_CONFLICT = `---
case_id: case-1
point_id: pt-1-1
stage_version: 1
priority: P0
content_hash: hash-server
trace_refs:
  clause_ids: [h2-1]
  entry_ids: []
  point_ids: [pt-1-1]
---

# 他页已改标题

## 步骤
1. 已变更步骤
   - 预期：新预期

## 说明
（由系统生成）
`

type StoreCase = CaseSummary & {
  markdown: string
  content_hash: string
  lineage: { root_case_id: string; regenerated_from_case_id: string | null }
  trace_refs: {
    clause_ids: string[]
    entry_ids: string[]
    point_ids: string[]
  }
}

let cases: StoreCase[] = []
let forceConflict = false
let lastUpdate: { caseId: string; ifMatch: string | null; markdown: string } | null =
  null
let lastReview: { items: Array<{ case_id: string; action: string }> } | null = null

function seedDefaults() {
  cases = [
    {
      id: 'case-1',
      point_id: 'pt-1-1',
      stage_version: 1,
      title: '正常提交订单',
      status: 'active',
      review_status: 'pending',
      batch_id: 'b0',
      error_info: null,
      created_at: '2026-09-28T01:00:00+00:00',
      updated_at: '2026-09-28T01:00:00+00:00',
      markdown: SAMPLE_MD_V1,
      content_hash: 'hash-v1',
      lineage: { root_case_id: 'case-1', regenerated_from_case_id: null },
      trace_refs: {
        clause_ids: ['h2-1'],
        entry_ids: [],
        point_ids: ['pt-1-1'],
      },
    },
    {
      id: 'case-2',
      point_id: 'pt-1-2',
      stage_version: 1,
      title: '异常提交',
      status: 'active',
      review_status: 'pending',
      batch_id: 'b0',
      error_info: null,
      created_at: '2026-09-28T01:01:00+00:00',
      updated_at: '2026-09-28T01:01:00+00:00',
      markdown: SAMPLE_MD_V1.replace('case-1', 'case-2').replace(
        '正常提交订单',
        '异常提交',
      ),
      content_hash: 'hash-c2',
      lineage: { root_case_id: 'case-2', regenerated_from_case_id: null },
      trace_refs: {
        clause_ids: ['h2-2'],
        entry_ids: [],
        point_ids: ['pt-1-2'],
      },
    },
    {
      id: 'case-conflict',
      point_id: 'pt-2-1',
      stage_version: 1,
      title: '哈希冲突用例',
      status: 'active',
      review_status: 'pending',
      batch_id: 'b1',
      error_info: { code: 'hash_conflict' },
      created_at: '2026-09-28T01:02:00+00:00',
      updated_at: '2026-09-28T01:02:00+00:00',
      markdown: SAMPLE_MD_V1.replace('case-1', 'case-conflict').replace(
        '正常提交订单',
        '哈希冲突用例',
      ),
      content_hash: 'hash-cf',
      lineage: { root_case_id: 'case-conflict', regenerated_from_case_id: null },
      trace_refs: {
        clause_ids: [],
        entry_ids: [],
        point_ids: ['pt-2-1'],
      },
    },
    {
      id: 'case-missing',
      point_id: 'pt-2-2',
      stage_version: 2,
      title: '缺文件用例',
      status: 'active',
      review_status: 'pending',
      batch_id: 'b1',
      error_info: { code: 'file_missing' },
      created_at: '2026-09-28T01:03:00+00:00',
      updated_at: '2026-09-28T01:03:00+00:00',
      markdown: '# 缺文件\n',
      content_hash: 'hash-miss',
      lineage: { root_case_id: 'case-missing', regenerated_from_case_id: null },
      trace_refs: {
        clause_ids: [],
        entry_ids: [],
        point_ids: ['pt-2-2'],
      },
    },
  ]
}

function toSummary(c: StoreCase): CaseSummary {
  return {
    id: c.id,
    point_id: c.point_id,
    stage_version: c.stage_version,
    title: c.title,
    status: c.status,
    review_status: c.review_status,
    batch_id: c.batch_id,
    error_info: c.error_info,
    created_at: c.created_at,
    updated_at: c.updated_at,
  }
}

function toDetail(c: StoreCase): CaseDetail {
  return {
    id: c.id,
    point_id: c.point_id,
    stage_version: c.stage_version,
    lineage: c.lineage,
    review_status: c.review_status,
    markdown: c.markdown,
    content_hash: c.content_hash,
    trace_refs: c.trace_refs,
    error_info: c.error_info,
  }
}

function actionToStatus(action: string): ReviewStatus {
  if (action === 'adopt') return 'adopted'
  if (action === 'reject') return 'rejected'
  return 'edited_adopted'
}

export function resetWorkbenchMockState() {
  forceConflict = false
  lastUpdate = null
  lastReview = null
  seedDefaults()
}

export function setWorkbenchForceConflict(v: boolean) {
  forceConflict = v
}

export function getLastCaseUpdate() {
  return lastUpdate
}

export function getLastReviewBody() {
  return lastReview
}

seedDefaults()

export const workbenchHandlers = [
  http.get('/api/v1/tasks/:taskId/cases', ({ request, params }) => {
    if (params.taskId !== 'task-f3') {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'task missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    const url = new URL(request.url)
    const review = url.searchParams.get('review')
    const version = url.searchParams.get('version')
    const status = url.searchParams.get('status') ?? 'active'

    let items = cases.filter((c) => (status ? c.status === status : true))
    if (review) items = items.filter((c) => c.review_status === review)
    if (version) {
      const v = Number(version)
      items = items.filter((c) => c.stage_version === v)
    }
    return HttpResponse.json({
      items: items.map(toSummary),
      next_cursor: null,
    })
  }),

  http.get('/api/v1/cases/:caseId', ({ params }) => {
    const c = cases.find((x) => x.id === params.caseId)
    if (!c) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'case missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    return HttpResponse.json(toDetail(c))
  }),

  http.put('/api/v1/cases/:caseId', async ({ request, params }) => {
    const c = cases.find((x) => x.id === params.caseId)
    if (!c) {
      return HttpResponse.json(
        {
          error: {
            code: 'NOT_FOUND',
            message: 'case missing',
            retryable: false,
            details: {},
          },
        },
        { status: 404 },
      )
    }
    const ifMatch = request.headers.get('If-Match')
    const body = (await request.json()) as { markdown: string }
    lastUpdate = {
      caseId: String(params.caseId),
      ifMatch,
      markdown: body.markdown,
    }

    if (forceConflict || ifMatch !== c.content_hash) {
      // 模拟服务端已被他页改写
      c.markdown = SAMPLE_MD_CONFLICT
      c.content_hash = 'hash-server'
      c.title = '他页已改标题'
      c.updated_at = now()
      return HttpResponse.json(
        {
          error: {
            code: 'VERSION_CONFLICT',
            message: '用例已被他人修改',
            retryable: false,
            details: {
              case_id: c.id,
              expected: ifMatch,
              current: 'hash-server',
            },
          },
        },
        { status: 409 },
      )
    }

    c.markdown = body.markdown
    c.content_hash = `hash-${Date.now()}`
    c.review_status = 'edited_adopted'
    c.error_info = null
    const titleMatch = /^#\s+(.+)$/m.exec(body.markdown)
    if (titleMatch) c.title = titleMatch[1].trim()
    c.updated_at = now()
    return HttpResponse.json(toDetail(c))
  }),

  http.post('/api/v1/cases/review', async ({ request }) => {
    const body = (await request.json()) as {
      items: Array<{ case_id: string; action: string }>
    }
    lastReview = body
    const results = body.items.map((item) => {
      const c = cases.find((x) => x.id === item.case_id)
      if (!c) {
        return {
          case_id: item.case_id,
          ok: false,
          review_status: null,
          error: 'not_found',
        }
      }
      c.review_status = actionToStatus(item.action)
      c.updated_at = now()
      return {
        case_id: item.case_id,
        ok: true,
        review_status: c.review_status,
        error: null,
      }
    })
    return HttpResponse.json({ results })
  }),
]
