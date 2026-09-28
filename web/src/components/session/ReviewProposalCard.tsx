import { useEffect, useMemo, useState } from 'react'
import { confirmTask, getReviewProposal } from '../../api/endpoints'
import type {
  CheckpointWaiting,
  ReviewProposal,
  ReviewProposalItem,
  ReviewProposalItemAction,
} from '../../api/domain'
import { ApiError, NetworkError } from '../../api/types'

type Props = {
  taskId: string
  checkpoint: CheckpointWaiting
  onDone?: () => void
}

const ACTION_OPTIONS: ReviewProposalItemAction[] = [
  'adopt',
  'edit_adopt',
  'reject',
  'add_point',
  'add_case',
  'repair',
]

const ACTION_LABEL: Record<ReviewProposalItemAction, string> = {
  adopt: '采纳',
  edit_adopt: '改后采纳',
  reject: '驳回',
  add_point: '补测试点',
  add_case: '补用例',
  repair: '修补',
}

function cloneProposal(p: ReviewProposal): ReviewProposal {
  return {
    scope: p.scope,
    items: p.items.map((it) => ({ ...it, patch: it.patch ?? null })),
    matrix_ref: p.matrix_ref ?? null,
    degraded: Boolean(p.degraded),
  }
}

function proposalsEqual(a: ReviewProposal, b: ReviewProposal): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function ReviewProposalCard({ taskId, checkpoint, onDone }: Props) {
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [original, setOriginal] = useState<ReviewProposal | null>(null)
  const [draft, setDraft] = useState<ReviewProposal | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    void getReviewProposal(taskId, checkpoint.artifact_id)
      .then((p) => {
        if (cancelled) return
        const cloned = cloneProposal(p)
        setOriginal(cloned)
        setDraft(cloneProposal(p))
      })
      .catch((e: unknown) => {
        if (cancelled) return
        const msg =
          e instanceof ApiError
            ? e.message
            : e instanceof NetworkError
              ? e.message
              : '加载评审提案失败'
        setError(msg)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId, checkpoint.artifact_id])

  const dirty = useMemo(() => {
    if (!original || !draft) return false
    return !proposalsEqual(original, draft)
  }, [original, draft])

  function setItemAction(targetId: string, action: ReviewProposalItemAction) {
    setDraft((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        items: prev.items.map((it) =>
          it.target_id === targetId ? { ...it, action } : it,
        ),
      }
    })
  }

  async function submit(action: 'confirm' | 'modify' | 'reject_rerun') {
    setBusy(true)
    setError(null)
    try {
      const body: Parameters<typeof confirmTask>[1] = {
        gate_kind: 'review_decision',
        artifact_id: checkpoint.artifact_id,
        action,
        expected_version: checkpoint.stage_version,
        stage: checkpoint.stage,
      }
      if (action === 'modify' && draft) {
        body.payload = draft as unknown as Record<string, unknown>
      }
      await confirmTask(taskId, body)
      onDone?.()
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof NetworkError
            ? e.message
            : '提交失败'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  async function handleAccept() {
    if (dirty) {
      await submit('modify')
    } else {
      await submit('confirm')
    }
  }

  return (
    <div
      className="rounded-md border border-teal-300 bg-teal-50/70 p-4"
      data-testid="review-proposal-card"
    >
      <h3 className="text-sm font-semibold text-teal-950">评审提案待确认</h3>
      <p className="mt-1 text-xs text-teal-900/80">
        {checkpoint.stage}
        {checkpoint.step_id ? ` · ${checkpoint.step_id}` : ''}
        {draft ? ` · scope=${draft.scope}` : ''}
        {draft?.degraded ? ' · 已降级' : ''}
      </p>

      {loading ? (
        <p className="mt-2 text-sm text-stone-600">加载提案…</p>
      ) : null}
      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="review-error">
          {error}
        </p>
      ) : null}

      {draft ? (
        <ul className="mt-3 space-y-2">
          {draft.items.map((item: ReviewProposalItem) => (
            <li
              key={item.target_id}
              className="rounded border border-teal-200 bg-white px-3 py-2"
              data-testid={`review-item-${item.target_id}`}
            >
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-medium text-stone-800">{item.target_id}</span>
                <select
                  data-testid={`review-action-${item.target_id}`}
                  className="rounded border border-stone-300 bg-white px-2 py-1 text-sm"
                  value={item.action}
                  disabled={busy}
                  onChange={(e) =>
                    setItemAction(
                      item.target_id,
                      e.target.value as ReviewProposalItemAction,
                    )
                  }
                >
                  {ACTION_OPTIONS.map((a) => (
                    <option key={a} value={a}>
                      {ACTION_LABEL[a]}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-stone-500">
                  置信 {Math.round((item.confidence ?? 0) * 100)}%
                </span>
              </div>
              <p className="mt-1 text-sm text-stone-700">{item.rationale}</p>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy || loading || !draft}
          className="rounded-md bg-teal-900 px-3 py-1.5 text-sm text-white hover:bg-teal-800 disabled:opacity-50"
          onClick={() => void handleAccept()}
        >
          采纳提案
        </button>
        <button
          type="button"
          disabled={busy || loading}
          className="rounded-md border border-stone-400 bg-white px-3 py-1.5 text-sm text-stone-800 hover:bg-stone-50 disabled:opacity-50"
          onClick={() => void submit('reject_rerun')}
        >
          驳回重评
        </button>
      </div>
    </div>
  )
}
