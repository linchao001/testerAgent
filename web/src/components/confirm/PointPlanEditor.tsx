/** PointPlan 清单编辑器：开放 title/angle/method/priority 与增删。 */

import type { PointPlan, TestPoint } from '../../api/domain'

type Props = {
  plan: PointPlan
  selectedId: string | null
  onSelect: (id: string | null) => void
  onChange: (plan: PointPlan) => void
}

function nextPointId(points: TestPoint[], storyId: string): string {
  const prefix = `pt-${storyId}-`
  let n = 1
  const ids = new Set(points.map((p) => p.point_id))
  // 兼容后端 pt-{story序号}-{n}：本地补充用 story_id 片段
  while (ids.has(`${prefix}${n}`) || ids.has(`pt-x-${n}`)) {
    n += 1
  }
  return `pt-x-${n}`
}

export function PointPlanEditor({
  plan,
  selectedId,
  onSelect,
  onChange,
}: Props) {
  const selected = plan.points.find((p) => p.point_id === selectedId)

  function updatePoint(pointId: string, patch: Partial<TestPoint>) {
    onChange({
      points: plan.points.map((p) =>
        p.point_id === pointId ? { ...p, ...patch } : p,
      ),
    })
  }

  function deletePoint(pointId: string) {
    onChange({ points: plan.points.filter((p) => p.point_id !== pointId) })
    if (selectedId === pointId) onSelect(null)
  }

  function addPoint() {
    const storyId = plan.points[0]?.story_id ?? 'S1'
    const pointId = nextPointId(plan.points, storyId)
    const point: TestPoint = {
      point_id: pointId,
      story_id: storyId,
      title: '新测试点',
      angle: '正常',
      method: '场景法',
      clause_ids: [],
      source_entry_ids: [],
      priority: 'P1',
    }
    onChange({ points: [...plan.points, point] })
    onSelect(pointId)
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="space-y-3" data-testid="point-plan-list">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-stone-800">测试点清单</h3>
          <button
            type="button"
            data-testid="add-point"
            className="text-xs text-teal-800 underline"
            onClick={addPoint}
          >
            补充测试点
          </button>
        </div>
        <ul className="space-y-1">
          {plan.points.map((p) => (
            <li key={p.point_id}>
              <button
                type="button"
                data-testid={`list-item-${p.point_id}`}
                onClick={() => onSelect(p.point_id)}
                className={`flex w-full items-center justify-between rounded border px-2 py-1.5 text-left text-sm ${
                  selectedId === p.point_id
                    ? 'border-teal-400 bg-teal-50'
                    : 'border-stone-200 bg-white hover:bg-stone-50'
                }`}
              >
                <span className="truncate font-medium text-stone-900">
                  {p.title}
                </span>
                <span className="ml-2 shrink-0 text-xs text-stone-500">
                  {p.priority}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div
        className="rounded border border-stone-200 bg-white p-3"
        data-testid="point-plan-detail"
      >
        {!selected ? (
          <p className="text-sm text-stone-500">从左侧选择一条测试点</p>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-stone-900">编辑测试点</h3>
              <button
                type="button"
                data-testid="delete-item"
                className="text-xs text-red-700 underline"
                onClick={() => deletePoint(selected.point_id)}
              >
                删除
              </button>
            </div>
            <label className="block text-xs text-stone-600">
              标题
              <input
                data-testid="edit-title"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selected.title}
                onChange={(e) =>
                  updatePoint(selected.point_id, { title: e.target.value })
                }
              />
            </label>
            <label className="block text-xs text-stone-600">
              覆盖角度
              <input
                data-testid="edit-angle"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selected.angle}
                onChange={(e) =>
                  updatePoint(selected.point_id, { angle: e.target.value })
                }
              />
            </label>
            <label className="block text-xs text-stone-600">
              设计方法
              <input
                data-testid="edit-method"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selected.method}
                onChange={(e) =>
                  updatePoint(selected.point_id, { method: e.target.value })
                }
              />
            </label>
            <label className="block text-xs text-stone-600">
              优先级
              <select
                data-testid="edit-priority"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selected.priority}
                onChange={(e) =>
                  updatePoint(selected.point_id, {
                    priority: e.target.value as TestPoint['priority'],
                  })
                }
              >
                <option value="P0">P0</option>
                <option value="P1">P1</option>
                <option value="P2">P2</option>
              </select>
            </label>
            <p className="text-xs text-stone-500">
              故事：{selected.story_id} · 条款：
              {selected.clause_ids.join(', ') || '—'} · 知识：
              {selected.source_entry_ids.join(', ') || '—'}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
