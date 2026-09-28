import type { TraceSummary } from '../../api/domain'

const STAGES = [
  { value: '', label: '全部阶段' },
  { value: 'link_identify', label: '链路识别' },
  { value: 'point_write', label: '测试点' },
  { value: 'case_generate', label: '用例生成' },
]

type Props = {
  traces: TraceSummary[]
  selectedId: string | null
  stageFilter: string
  onStageFilter: (v: string) => void
  onSelect: (id: string) => void
}

export function TraceTree({
  traces,
  selectedId,
  stageFilter,
  onStageFilter,
  onSelect,
}: Props) {
  return (
    <div className="flex h-full flex-col gap-2" data-testid="trace-tree">
      <div className="flex flex-wrap gap-1">
        {STAGES.map((s) => (
          <button
            key={s.value || 'all'}
            type="button"
            className={[
              'rounded px-2 py-1 text-xs',
              stageFilter === s.value
                ? 'bg-stone-900 text-white'
                : 'border border-stone-300 text-stone-700',
            ].join(' ')}
            data-testid={`stage-tab-${s.value || 'all'}`}
            onClick={() => onStageFilter(s.value)}
          >
            {s.label}
          </button>
        ))}
      </div>
      <ul className="min-h-0 flex-1 space-y-1 overflow-auto text-sm">
        {traces.map((t) => (
          <li key={t.id}>
            <button
              type="button"
              className={[
                'w-full rounded border px-2 py-1.5 text-left',
                selectedId === t.id
                  ? 'border-stone-800 bg-stone-100'
                  : 'border-stone-200 hover:bg-stone-50',
              ].join(' ')}
              data-testid={`trace-item-${t.id}`}
              onClick={() => onSelect(t.id)}
            >
              <div className="font-medium text-stone-900">
                {t.stage} · v{t.stage_version}
                {t.batch_id ? ` · ${t.batch_id}` : ''}
              </div>
              <div className="mt-0.5 text-xs text-stone-500">
                cand {t.candidate_count} · kept {t.kept_count} · inj{' '}
                {t.injected_count}
                {t.degraded_count > 0 ? (
                  <span className="ml-2 text-amber-700">
                    degraded {t.degraded_count}
                  </span>
                ) : null}
              </div>
            </button>
          </li>
        ))}
        {traces.length === 0 ? (
          <li className="px-2 py-3 text-xs text-stone-500">暂无轨迹</li>
        ) : null}
      </ul>
    </div>
  )
}
