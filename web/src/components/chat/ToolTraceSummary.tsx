/** Collapsible summary of assistant payload.tool_trace (audit only). */

export type ToolTraceEntry = {
  tool: string
  ok: boolean
  latency_ms?: number
  error_code?: string
  args_digest?: string
}

type Props = {
  payload: Record<string, unknown>
}

function asTrace(payload: Record<string, unknown>): ToolTraceEntry[] {
  const raw = payload.tool_trace
  if (!Array.isArray(raw) || raw.length === 0) return []
  return raw.filter(
    (x): x is ToolTraceEntry =>
      !!x &&
      typeof x === 'object' &&
      typeof (x as ToolTraceEntry).tool === 'string' &&
      typeof (x as ToolTraceEntry).ok === 'boolean',
  )
}

export function ToolTraceSummary({ payload }: Props) {
  const entries = asTrace(payload)
  if (entries.length === 0) return null

  return (
    <details className="mt-2 text-xs text-stone-500" data-testid="tool-trace">
      <summary className="cursor-pointer select-none text-stone-600">
        调用了 {entries.length} 次工具
      </summary>
      <ul className="mt-1 space-y-0.5 pl-1">
        {entries.map((e, i) => (
          <li key={`${e.tool}-${i}`} data-testid={`tool-trace-row-${i}`}>
            <span className="font-medium text-stone-700">{e.tool}</span>
            {' · '}
            {e.ok ? '成功' : `失败${e.error_code ? `(${e.error_code})` : ''}`}
            {typeof e.latency_ms === 'number' ? ` · ${e.latency_ms} ms` : null}
          </li>
        ))}
      </ul>
    </details>
  )
}
