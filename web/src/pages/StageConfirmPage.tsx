/**
 * StageConfirmPage（WP-F2）：链路 / 测试点清单确认；
 * confirm / modify(expected_version) + VERSION_CONFLICT 刷新；复用 ImpactPreview 回退。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  confirmTask,
  getArtifact,
  getTask,
  rollbackTask,
} from '../api/endpoints'
import type {
  ArtifactDetail,
  LinkPlan,
  PointPlan,
  StageName,
  Task,
} from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { ImpactPreview } from '../components/confirm/ImpactPreview'
import { LinkPlanEditor } from '../components/confirm/LinkPlanEditor'
import { PointPlanEditor } from '../components/confirm/PointPlanEditor'
import { stageLabel } from '../lib/parseStageMention'
import { clearLastError, reportError } from '../stores/connection'
import { useSession } from '../stores/session'
import { useTaskStream } from '../stores/taskStream'

type LinkSel =
  | { kind: 'link'; id: string }
  | { kind: 'story'; id: string }
  | null

function isLinkPlan(p: unknown): p is LinkPlan {
  return (
    !!p &&
    typeof p === 'object' &&
    Array.isArray((p as LinkPlan).links) &&
    Array.isArray((p as LinkPlan).stories)
  )
}

function isPointPlan(p: unknown): p is PointPlan {
  return !!p && typeof p === 'object' && Array.isArray((p as PointPlan).points)
}

function asStage(stage: string): StageName | null {
  if (stage === 'link_identify' || stage === 'point_write') return stage
  return null
}

export function StageConfirmPage() {
  const navigate = useNavigate()
  const taskId = useSession((s) => s.taskId)
  const checkpoint = useTaskStream((s) => s.checkpoint)
  const clearCheckpoint = useTaskStream((s) => s.clearCheckpoint)

  const [task, setTask] = useState<Task | null>(null)
  const [artifact, setArtifact] = useState<ArtifactDetail | null>(null)
  const [linkPlan, setLinkPlan] = useState<LinkPlan | null>(null)
  const [pointPlan, setPointPlan] = useState<PointPlan | null>(null)
  const [baselineJson, setBaselineJson] = useState<string>('')
  const [linkSel, setLinkSel] = useState<LinkSel>(null)
  const [pointSel, setPointSel] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [conflictHint, setConflictHint] = useState<string | null>(null)
  const [statusHint, setStatusHint] = useState<string | null>(null)
  const [rollbackOpen, setRollbackOpen] = useState(false)
  const [rollbackPending, setRollbackPending] = useState(false)

  const stage = asStage(
    checkpoint?.stage ?? task?.current_stage ?? artifact?.stage ?? '',
  )

  const dirty = useMemo(() => {
    if (!baselineJson) return false
    if (stage === 'link_identify' && linkPlan) {
      return JSON.stringify(linkPlan) !== baselineJson
    }
    if (stage === 'point_write' && pointPlan) {
      return JSON.stringify(pointPlan) !== baselineJson
    }
    return false
  }, [baselineJson, linkPlan, pointPlan, stage])

  const load = useCallback(async () => {
    if (!taskId) {
      setLoading(false)
      return
    }
    clearLastError()
    setLoading(true)
    try {
      const t = await getTask(taskId)
      setTask(t)
      const stageKey = asStage(
        checkpoint?.stage ?? t.current_stage,
      )
      const summary =
        (stageKey && t.active_artifacts[stageKey]) ||
        Object.values(t.active_artifacts)[0]
      const artId = checkpoint?.artifact_id ?? summary?.id
      if (!artId) {
        setArtifact(null)
        setLinkPlan(null)
        setPointPlan(null)
        setBaselineJson('')
        return
      }
      const art = await getArtifact(artId)
      setArtifact(art)
      if (isLinkPlan(art.payload)) {
        const plan = structuredClone(art.payload)
        setLinkPlan(plan)
        setPointPlan(null)
        setBaselineJson(JSON.stringify(plan))
        setLinkSel(plan.links[0] ? { kind: 'link', id: plan.links[0].link_id } : null)
      } else if (isPointPlan(art.payload)) {
        const plan = structuredClone(art.payload)
        setPointPlan(plan)
        setLinkPlan(null)
        setBaselineJson(JSON.stringify(plan))
        setPointSel(plan.points[0]?.point_id ?? null)
      } else {
        setLinkPlan(null)
        setPointPlan(null)
        setBaselineJson('')
      }
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
    } finally {
      setLoading(false)
    }
  }, [taskId, checkpoint?.artifact_id, checkpoint?.stage])

  useEffect(() => {
    void load()
  }, [load])

  async function handleSubmit() {
    if (!taskId || !artifact || !stage) return
    setBusy(true)
    setConflictHint(null)
    setStatusHint(null)
    clearLastError()
    try {
      const payload =
        stage === 'link_identify'
          ? (linkPlan as unknown as Record<string, unknown>)
          : (pointPlan as unknown as Record<string, unknown>)
      const action = dirty ? 'modify' : 'confirm'
      await confirmTask(taskId, {
        gate_kind: 'plan_confirm',
        stage,
        artifact_id: artifact.id,
        expected_version: artifact.stage_version,
        action,
        payload: action === 'modify' ? payload : null,
      })
      clearCheckpoint()
      setStatusHint('已放行，任务继续执行')
      navigate('/')
    } catch (err) {
      if (err instanceof ApiError && err.code === 'VERSION_CONFLICT') {
        setConflictHint('版本已变更，已刷新最新产物，请核对后重试')
        await load()
      } else {
        reportError(err as ApiError | NetworkError | Error)
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleRollback() {
    if (!taskId || !task) return
    const linkArt = task.active_artifacts.link_identify
    if (!linkArt) {
      reportError(new Error('缺少链路识别产物，无法回退'))
      return
    }
    setRollbackPending(true)
    clearLastError()
    try {
      await rollbackTask(taskId, {
        target_stage: 'link_identify',
        artifact_id: linkArt.id,
        expected_version: linkArt.stage_version,
      })
      clearCheckpoint()
      setRollbackOpen(false)
      navigate('/')
    } catch (err) {
      if (err instanceof ApiError && err.code === 'VERSION_CONFLICT') {
        setConflictHint('版本已变更，已刷新最新产物，请核对后重试')
        setRollbackOpen(false)
        await load()
      } else {
        reportError(err as ApiError | NetworkError | Error)
      }
    } finally {
      setRollbackPending(false)
    }
  }

  if (!taskId) {
    return (
      <div className="mx-auto max-w-3xl space-y-3 p-4" data-testid="stage-confirm-empty">
        <h1 className="text-lg font-semibold text-stone-900">阶段确认</h1>
        <p className="text-sm text-stone-600">尚无活跃任务。</p>
        <Link to="/" className="text-sm text-teal-800 underline">
          返回会话
        </Link>
      </div>
    )
  }

  if (!checkpoint && !loading && task?.status !== 'waiting_confirm') {
    return (
      <div className="mx-auto max-w-3xl space-y-3 p-4" data-testid="stage-confirm-empty">
        <h1 className="text-lg font-semibold text-stone-900">阶段确认</h1>
        <p className="text-sm text-stone-600">
          当前任务不在等待确认状态（{task?.status ?? '未知'}）。
        </p>
        <Link to="/" className="text-sm text-teal-800 underline">
          返回会话
        </Link>
      </div>
    )
  }

  return (
    <div
      className="mx-auto flex max-w-5xl flex-col gap-4 p-4"
      data-testid="stage-confirm-page"
    >
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-stone-900">阶段确认</h1>
          <p className="mt-1 text-sm text-stone-600">
            {stage ? stageLabel(stage) : '…'}
            {artifact ? ` · v${artifact.stage_version}` : ''}
            {dirty ? ' · 已修改' : ''}
          </p>
        </div>
        <Link to="/" className="text-sm text-stone-600 underline">
          返回会话
        </Link>
      </header>

      {conflictHint ? (
        <div
          data-testid="version-conflict"
          className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950"
        >
          {conflictHint}
        </div>
      ) : null}
      {statusHint ? (
        <p className="text-sm text-teal-800" data-testid="status-hint">
          {statusHint}
        </p>
      ) : null}

      {loading ? (
        <p className="text-sm text-stone-500">加载产物…</p>
      ) : stage === 'link_identify' && linkPlan ? (
        <LinkPlanEditor
          plan={linkPlan}
          selection={linkSel}
          onSelect={setLinkSel}
          onChange={setLinkPlan}
        />
      ) : stage === 'point_write' && pointPlan ? (
        <PointPlanEditor
          plan={pointPlan}
          selectedId={pointSel}
          onSelect={setPointSel}
          onChange={setPointPlan}
        />
      ) : (
        <p className="text-sm text-stone-500">无法加载阶段产物。</p>
      )}

      <footer className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-stone-200 bg-[var(--color-canvas,#fafaf9)] py-3">
        <div>
          {stage === 'point_write' ? (
            <button
              type="button"
              data-testid="rollback-to-link"
              disabled={busy || rollbackPending}
              className="text-sm text-stone-600 underline disabled:opacity-50"
              onClick={() => setRollbackOpen(true)}
            >
              回退到链路识别…
            </button>
          ) : null}
        </div>
        <button
          type="button"
          data-testid="confirm-submit"
          disabled={busy || loading || !artifact || !stage}
          className="rounded-md bg-teal-800 px-4 py-2 text-sm text-white hover:bg-teal-700 disabled:opacity-50"
          onClick={() => void handleSubmit()}
        >
          {busy ? '提交中…' : dirty ? '保存修订并放行' : '确认放行'}
        </button>
      </footer>

      <ImpactPreview
        open={rollbackOpen}
        targetStage="link_identify"
        messagePreview={
          dirty
            ? '将带着当前页面上的修订回退到链路识别，下游测试点/用例将作废重生成。'
            : '回退到链路识别后，下游测试点/用例将作废并重新生成。'
        }
        pending={rollbackPending}
        onCancel={() => setRollbackOpen(false)}
        onConfirm={handleRollback}
      />
    </div>
  )
}
