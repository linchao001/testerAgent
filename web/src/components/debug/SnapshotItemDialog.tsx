import type { SnapshotDetail, SnapshotLine } from '../../api/domain'

type Props = {
  detail: SnapshotDetail
  line: SnapshotLine | null
  busy?: boolean
  onOpenItem: (position: number) => void
  onCloseDialog: () => void
}

export function SnapshotItemDialog({
  detail,
  line,
  busy,
  onOpenItem,
  onCloseDialog,
}: Props) {
  const level = detail.has_full ? 'full' : 'meta'

  return (
    <div className="space-y-2" data-testid="snapshot-detail">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          className={[
            'rounded px-1.5 py-0.5',
            detail.has_full
              ? 'bg-emerald-50 text-emerald-800'
              : 'bg-amber-50 text-amber-900',
          ].join(' ')}
          data-testid="snapshot-level-badge"
        >
          {level}
        </span>
        {!detail.has_full ? (
          <span className="text-amber-800">非 full 档，无法拉偏移全文</span>
        ) : null}
      </div>
      <ul className="space-y-1 text-sm">
        {detail.items.map((it) => (
          <li key={it.position}>
            <button
              type="button"
              className="w-full rounded border border-stone-200 px-2 py-1 text-left hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-40"
              data-testid={`snap-pos-${it.position}`}
              disabled={!detail.has_full || busy}
              onClick={() => onOpenItem(it.position)}
            >
              <span className="font-medium">#{it.position}</span>{' '}
              {it.title}
              <span className="ml-2 text-xs text-stone-500">
                {it.tokens_est} tok
              </span>
            </button>
          </li>
        ))}
      </ul>

      {line ? (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
        >
          <div
            className="max-h-[80vh] w-full max-w-lg overflow-auto rounded bg-white p-4 shadow-lg"
            data-testid="snapshot-item-dialog"
          >
            <div className="mb-2 flex items-start justify-between gap-2">
              <div>
                <div className="font-medium text-stone-900">{line.title}</div>
                <div className="text-xs text-stone-500">
                  {line.entry_id} · pos {line.position}
                </div>
              </div>
              <button
                type="button"
                className="rounded border border-stone-300 px-2 py-1 text-xs"
                data-testid="close-snapshot-dialog"
                onClick={onCloseDialog}
              >
                关闭
              </button>
            </div>
            <pre className="whitespace-pre-wrap rounded bg-stone-50 p-3 text-xs text-stone-800">
              {line.content}
            </pre>
          </div>
        </div>
      ) : null}
    </div>
  )
}
