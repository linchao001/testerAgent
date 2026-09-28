import { describe, expect, it } from 'vitest'
import { computeAdoption } from './adoption'
import type { CaseSummary } from '../api/domain'

function c(
  partial: Partial<CaseSummary> & Pick<CaseSummary, 'id' | 'review_status'>,
): CaseSummary {
  return {
    point_id: 'pt-1',
    stage_version: 1,
    title: partial.id,
    status: 'active',
    batch_id: 'b0',
    error_info: null,
    created_at: 't',
    updated_at: 't',
    ...partial,
  }
}

describe('computeAdoption', () => {
  it('仅统计 active，采纳含 edited_adopted', () => {
    const stats = computeAdoption([
      c({ id: '1', review_status: 'adopted' }),
      c({ id: '2', review_status: 'edited_adopted' }),
      c({ id: '3', review_status: 'pending' }),
      c({ id: '4', review_status: 'rejected' }),
      c({ id: '5', review_status: 'adopted', status: 'obsolete' }),
    ])
    expect(stats).toEqual({ adopted: 2, total: 4, label: '2/4' })
  })
})
