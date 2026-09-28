import { describe, expect, it } from 'vitest'
import { funnelFromTrace } from './funnel'
import type { TraceDetail } from '../api/domain'

describe('funnelFromTrace', () => {
  it('按 filtered_* / kept / injected 切四段', () => {
    const detail: TraceDetail = {
      id: 't',
      task_id: 'task',
      graph_run_id: 'r',
      stage: 'link_identify',
      stage_version: 1,
      node: 'link_identify',
      batch_id: null,
      query_variant: {},
      candidates: [
        {
          entry_id: 'a',
          entry_version: '1',
          title: 'a',
          score: 1,
          source_channel: 'raw:0',
          entry_type: 'api',
          kept: true,
          drop_reason: null,
        },
        {
          entry_id: 'b',
          entry_version: '1',
          title: 'b',
          score: 1,
          source_channel: 'raw:0',
          entry_type: 'api',
          kept: false,
          drop_reason: 'filtered_type',
        },
        {
          entry_id: 'c',
          entry_version: '1',
          title: 'c',
          score: 1,
          source_channel: 'raw:0',
          entry_type: 'api',
          kept: false,
          drop_reason: 'rerank_cutoff',
        },
      ],
      injected_ids: ['a'],
      referenced_ids: [],
      hallucinated_ids: [],
      weak_ref_ids: [],
      degraded: [],
      created_at: '',
    }
    expect(funnelFromTrace(detail).map((s) => s.count)).toEqual([3, 2, 1, 1])
  })
})
