/** 回退影响二次确认（F1：change_request；F2 复用扩展）。 */

import type { StageName } from '../../api/domain'
import { stageLabel } from '../../lib/parseStageMention'

type Props = {
  open: boolean
  targetStage: StageName
  messagePreview: string
  pending?: boolean
  onCancel: () => void
  onConfirm: () => void | Promise<void>
}

export function ImpactPreview({
  open,
  targetStage,
  messagePreview,
  pending,
  onCancel,
  onConfirm,
}: Props) {
  if (!open) return null

  return (
    <div
      data-testid="impact-preview"
      className="fixed inset-0 z-50 flex items-center justify-center bg-stone-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="impact-preview-title"
    >
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-lg">
        <h2
          id="impact-preview-title"
          className="text-base font-semibold text-stone-900"
        >
          确认回退到「{stageLabel(targetStage)}」
        </h2>
        <p className="mt-2 text-sm text-stone-600">
          回退后，该阶段下游产物将标记作废并重新生成。请确认变更意图后再继续。
        </p>
        {messagePreview.trim() ? (
          <blockquote className="mt-3 max-h-28 overflow-auto rounded border border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-700 whitespace-pre-wrap">
            {messagePreview}
          </blockquote>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            data-testid="impact-cancel"
            disabled={pending}
            className="rounded-md px-3 py-1.5 text-sm text-stone-600 hover:bg-stone-100"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            type="button"
            data-testid="impact-confirm"
            disabled={pending}
            className="rounded-md bg-teal-800 px-3 py-1.5 text-sm text-white hover:bg-teal-700 disabled:opacity-50"
            onClick={() => void onConfirm()}
          >
            {pending ? '执行中…' : '确认回退'}
          </button>
        </div>
      </div>
    </div>
  )
}
