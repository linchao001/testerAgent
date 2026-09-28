import type { CaseSummary } from '../../api/domain'

type Props = {
  cases: CaseSummary[]
  selectedId: string | null
  checkedIds: Set<string>
  reviewFilter: string
  versionFilter: string
  versions: number[]
  onSelect: (id: string) => void
  onToggleCheck: (id: string) => void
  onReviewFilter: (v: string) => void
  onVersionFilter: (v: string) => void
}

const REVIEW_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '全部评审' },
  { value: 'pending', label: '待评审' },
  { value: 'adopted', label: '已采纳' },
  { value: 'edited_adopted', label: '改后采纳' },
  { value: 'rejected', label: '已拒绝' },
]

export function CaseList({
  cases,
  selectedId,
  checkedIds,
  reviewFilter,
  versionFilter,
  versions,
  onSelect,
  onToggleCheck,
  onReviewFilter,
  onVersionFilter,
}: Props) {
  return (
    <div className="flex h-full flex-col gap-2" data-testid="case-list">
      <div className="flex flex-wrap gap-2 text-xs">
        <select
          className="rounded border border-stone-300 px-2 py-1"
          data-testid="filter-review"
          value={reviewFilter}
          onChange={(e) => onReviewFilter(e.target.value)}
        >
          {REVIEW_OPTIONS.map((o) => (
            <option key={o.value || 'all'} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          className="rounded border border-stone-300 px-2 py-1"
          data-testid="filter-version"
          value={versionFilter}
          onChange={(e) => onVersionFilter(e.target.value)}
        >
          <option value="">全部版本</option>
          {versions.map((v) => (
            <option key={v} value={String(v)}>
              v{v}
            </option>
          ))}
        </select>
      </div>
      <ul className="min-h-0 flex-1 space-y-1 overflow-auto text-sm">
        {cases.map((c) => {
          const errCode =
            c.error_info && typeof c.error_info.code === 'string'
              ? c.error_info.code
              : null
          return (
            <li key={c.id}>
              <div
                className={[
                  'flex items-start gap-2 rounded border px-2 py-1.5',
                  selectedId === c.id
                    ? 'border-stone-800 bg-stone-100'
                    : 'border-stone-200 hover:bg-stone-50',
                  errCode ? 'border-red-300' : '',
                ].join(' ')}
              >
                <input
                  type="checkbox"
                  className="mt-1"
                  data-testid={`case-check-${c.id}`}
                  checked={checkedIds.has(c.id)}
                  onChange={() => onToggleCheck(c.id)}
                />
                <button
                  type="button"
                  className="min-w-0 flex-1 text-left"
                  data-testid={`case-item-${c.id}`}
                  onClick={() => onSelect(c.id)}
                >
                  <div className="truncate font-medium text-stone-900">
                    {c.title}
                  </div>
                  <div className="mt-0.5 flex flex-wrap gap-2 text-xs text-stone-500">
                    <span data-testid={`case-review-${c.id}`}>
                      {c.review_status}
                    </span>
                    <span>v{c.stage_version}</span>
                    {errCode ? (
                      <span className="text-red-600">{errCode}</span>
                    ) : null}
                  </div>
                </button>
              </div>
            </li>
          )
        })}
        {cases.length === 0 ? (
          <li className="px-2 py-4 text-sm text-stone-500">暂无用例</li>
        ) : null}
      </ul>
    </div>
  )
}
