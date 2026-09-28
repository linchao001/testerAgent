import type { SnapshotSummary } from '../../api/domain'

type Props = {
  snapshots: SnapshotSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}

export function SnapshotList({ snapshots, selectedId, onSelect }: Props) {
  return (
    <div className="space-y-2" data-testid="snapshot-list">
      <h3 className="text-sm font-medium text-stone-800">上下文快照</h3>
      <ul className="max-h-48 space-y-1 overflow-auto text-sm">
        {snapshots.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              className={[
                'w-full rounded border px-2 py-1.5 text-left',
                selectedId === s.id
                  ? 'border-stone-800 bg-stone-100'
                  : 'border-stone-200 hover:bg-stone-50',
              ].join(' ')}
              data-testid={`snap-item-${s.id}`}
              onClick={() => onSelect(s.id)}
            >
              <div className="font-medium text-stone-900">
                {s.stage} · {s.node}
                {s.batch_id ? ` · ${s.batch_id}` : ''}
              </div>
              <div className="mt-0.5 flex flex-wrap gap-2 text-xs text-stone-500">
                <span>{s.item_count} items</span>
                <span>{s.total_tokens_est} tok</span>
                <span
                  className={
                    s.has_full ? 'text-emerald-700' : 'text-amber-700'
                  }
                >
                  {s.has_full ? 'full' : 'meta'}
                </span>
              </div>
            </button>
          </li>
        ))}
        {snapshots.length === 0 ? (
          <li className="text-xs text-stone-500">暂无快照</li>
        ) : null}
      </ul>
    </div>
  )
}
