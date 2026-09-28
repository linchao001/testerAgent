import type { AdoptionStats } from '../../lib/adoption'

type Props = {
  stats: AdoptionStats
}

export function AdoptionSummary({ stats }: Props) {
  const pct =
    stats.total === 0 ? 0 : Math.round((stats.adopted / stats.total) * 100)
  return (
    <div
      className="rounded border border-stone-200 bg-stone-50 px-3 py-2 text-sm"
      data-testid="adoption-summary"
    >
      <span className="font-medium text-stone-900">采纳率</span>
      <span className="ml-2 text-stone-700">
        {stats.label}（{pct}%）
      </span>
      <span className="ml-2 text-xs text-stone-500">仅 active</span>
    </div>
  )
}
