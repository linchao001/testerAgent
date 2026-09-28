import { useState } from 'react'
import { confirmTask } from '../../api/endpoints'
import type { CheckpointWaiting, GateKind } from '../../api/domain'
import { ApiError, NetworkError } from '../../api/types'

type Props = {
  taskId: string
  checkpoint: CheckpointWaiting
  onDone?: () => void
}

export function GateConfirmCard({ taskId, checkpoint, onDone }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const gateKind: GateKind = checkpoint.gate_kind ?? 'plan_confirm'

  async function submit(action: 'confirm' | 'reject_rerun') {
    setBusy(true)
    setError(null)
    try {
      await confirmTask(taskId, {
        gate_kind: gateKind,
        artifact_id: checkpoint.artifact_id,
        action,
        expected_version: checkpoint.stage_version,
        stage: checkpoint.stage,
      })
      onDone?.()
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof NetworkError
            ? e.message
            : '确认失败'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate-confirm-card" data-testid="gate-confirm-card">
      <p>
        等待确认：{checkpoint.stage}
        {checkpoint.step_id ? ` / ${checkpoint.step_id}` : ''}
      </p>
      <p className="muted">gate: {gateKind}</p>
      {error ? <p className="error">{error}</p> : null}
      <div className="actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit('confirm')}
        >
          确认继续
        </button>
        {gateKind === 'review_decision' ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => void submit('reject_rerun')}
          >
            驳回重评
          </button>
        ) : null}
      </div>
    </div>
  )
}
