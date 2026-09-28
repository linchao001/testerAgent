import type { CandidateView, FunnelStage } from '../../api/domain'
import { groupCandidatesByDrop } from '../../lib/funnel'

type Props = {
  stages: FunnelStage[]
  candidates?: CandidateView[]
  expanded?: boolean
  onToggleExpand?: () => void
}

export function FunnelChart({
  stages,
  candidates,
  expanded,
  onToggleExpand,
}: Props) {
  const max = Math.max(1, ...stages.map((s) => s.count))
  const groups = candidates ? groupCandidatesByDrop(candidates) : []

  return (
    <div className="space-y-2" data-testid="funnel-chart">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-stone-800">检索漏斗</h3>
        {candidates && onToggleExpand ? (
          <button
            type="button"
            className="text-xs text-stone-600 underline"
            data-testid="funnel-expand"
            onClick={onToggleExpand}
          >
            {expanded ? '收起候选' : '展开候选'}
          </button>
        ) : null}
      </div>
      <ul className="space-y-2">
        {stages.map((s) => (
          <li key={s.key} className="text-xs">
            <div className="mb-0.5 flex justify-between text-stone-600">
              <span>{s.label}</span>
              <span data-testid={`funnel-${s.key}`}>{s.count}</span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-stone-100">
              <div
                className="h-full rounded bg-stone-800"
                style={{ width: `${(s.count / max) * 100}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
      {expanded && groups.length > 0 ? (
        <div
          className="max-h-48 space-y-2 overflow-auto rounded border border-stone-200 p-2 text-xs"
          data-testid="funnel-candidates"
        >
          {groups.map((g) => (
            <div key={g.reason}>
              <div className="font-medium text-stone-700">{g.reason}</div>
              <ul className="mt-1 space-y-0.5 text-stone-600">
                {g.items.map((c) => (
                  <li key={`${c.entry_id}-${c.source_channel}`}>
                    {c.title}
                    {c.drop_reason ? (
                      <span className="ml-2 text-amber-700">{c.drop_reason}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}
