/** 从 TraceDetail 派生漏斗四段：召回 → 过滤后 → 重排保留 → 注入。 */

import type { CandidateView, FunnelStage, TraceDetail } from '../api/domain'

const FILTER_DROPS = new Set(['filtered_type', 'filtered_scope'])

export function funnelFromTrace(detail: TraceDetail): FunnelStage[] {
  const cands = detail.candidates
  const afterFilter = cands.filter(
    (c) => !c.drop_reason || !FILTER_DROPS.has(c.drop_reason),
  ).length
  const kept = cands.filter((c) => c.kept).length
  return [
    { key: 'recall', label: '召回', count: cands.length },
    { key: 'filter', label: '过滤后', count: afterFilter },
    { key: 'rerank', label: '重排保留', count: kept },
    { key: 'inject', label: '注入', count: detail.injected_ids.length },
  ]
}

export function groupCandidatesByDrop(
  cands: CandidateView[],
): Array<{ reason: string; items: CandidateView[] }> {
  const map = new Map<string, CandidateView[]>()
  for (const c of cands) {
    const key = c.kept ? 'kept' : c.drop_reason || c.error || 'unknown'
    const list = map.get(key) ?? []
    list.push(c)
    map.set(key, list)
  }
  return [...map.entries()].map(([reason, items]) => ({ reason, items }))
}
