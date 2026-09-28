/** 采纳率：仅 active；(adopted + edited_adopted) / active。 */

import type { CaseSummary } from '../api/domain'

export type AdoptionStats = {
  adopted: number
  total: number
  label: string
}

export function computeAdoption(cases: CaseSummary[]): AdoptionStats {
  const active = cases.filter((c) => c.status === 'active')
  const adopted = active.filter(
    (c) =>
      c.review_status === 'adopted' || c.review_status === 'edited_adopted',
  ).length
  const total = active.length
  return {
    adopted,
    total,
    label: `${adopted}/${total}`,
  }
}
