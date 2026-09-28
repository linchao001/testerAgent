type Props = {
  disabled?: boolean
  selectedCount: number
  onAdopt: () => void
  onReject: () => void
  onEditedAdopt: () => void
}

export function ReviewBar({
  disabled,
  selectedCount,
  onAdopt,
  onReject,
  onEditedAdopt,
}: Props) {
  return (
    <div
      className="flex flex-wrap items-center gap-2 border-t border-stone-200 pt-2"
      data-testid="review-bar"
    >
      <span className="text-xs text-stone-500">已选 {selectedCount}</span>
      <button
        type="button"
        className="rounded bg-stone-900 px-2.5 py-1 text-xs text-white disabled:opacity-40"
        data-testid="review-adopt"
        disabled={disabled || selectedCount === 0}
        onClick={onAdopt}
      >
        采纳
      </button>
      <button
        type="button"
        className="rounded border border-stone-300 px-2.5 py-1 text-xs disabled:opacity-40"
        data-testid="review-reject"
        disabled={disabled || selectedCount === 0}
        onClick={onReject}
      >
        拒绝
      </button>
      <button
        type="button"
        className="rounded border border-stone-300 px-2.5 py-1 text-xs disabled:opacity-40"
        data-testid="review-edited-adopt"
        disabled={disabled || selectedCount === 0}
        onClick={onEditedAdopt}
      >
        改后采纳
      </button>
    </div>
  )
}
