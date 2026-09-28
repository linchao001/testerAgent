import { useState } from 'react'
import type { PlaygroundOut } from '../../api/domain'

const STAGES = ['link_identify', 'point_write', 'case_generate']

type Props = {
  disabled?: boolean
  busy?: boolean
  result: PlaygroundOut | null
  onRun: (query: string, stage: string) => void
}

export function Playground({ disabled, busy, result, onRun }: Props) {
  const [query, setQuery] = useState('')
  const [stage, setStage] = useState('link_identify')

  return (
    <aside
      className="flex h-full flex-col gap-2 rounded border border-stone-200 bg-white p-3"
      data-testid="playground"
    >
      <h3 className="text-sm font-medium text-stone-800">Playground</h3>
      {disabled ? (
        <p className="text-xs text-stone-500">请先选择工作区后再试跑检索。</p>
      ) : (
        <>
          <label className="block text-xs text-stone-600">
            query
            <textarea
              className="mt-1 w-full rounded border border-stone-300 p-2 text-sm"
              rows={3}
              data-testid="playground-query"
              value={query}
              disabled={busy}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <label className="block text-xs text-stone-600">
            stage
            <select
              className="mt-1 w-full rounded border border-stone-300 px-2 py-1 text-sm"
              data-testid="playground-stage"
              value={stage}
              disabled={busy}
              onChange={(e) => setStage(e.target.value)}
            >
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="rounded bg-stone-900 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            data-testid="playground-run"
            disabled={busy || !query.trim()}
            onClick={() => onRun(query.trim(), stage)}
          >
            试跑
          </button>
        </>
      )}

      {result ? (
        <div className="mt-2 space-y-2 border-t border-stone-100 pt-2 text-xs">
          <div
            className="rounded bg-stone-50 p-2 font-mono"
            data-testid="playground-funnel"
          >
            <div>total {result.funnel.total}</div>
            <div>kept {result.funnel.kept}</div>
            <div>errors {result.funnel.errors}</div>
            {Object.entries(result.funnel.dropped).map(([k, v]) => (
              <div key={k}>
                dropped.{k} {v}
              </div>
            ))}
          </div>
          {result.degraded.length > 0 ? (
            <div
              className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-950"
              data-testid="playground-degraded"
            >
              degraded: {result.degraded.map((d) => d.step).join(', ')}
            </div>
          ) : null}
          <div className="text-stone-500">
            injected {result.injected.length} · token_est {result.token_est}
            {result.truncated ? ' · truncated' : ''}
          </div>
        </div>
      ) : null}
    </aside>
  )
}
