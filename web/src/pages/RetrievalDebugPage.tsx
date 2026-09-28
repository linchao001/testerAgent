/**
 * RetrievalDebugPage（WP-F4）：检索调试——漏斗 / 轨迹 / 快照偏移全文 / Playground。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getSnapshot,
  getSnapshotItem,
  getTrace,
  listSnapshots,
  listTraces,
  runPlayground,
} from '../api/endpoints'
import type {
  PlaygroundOut,
  SnapshotDetail,
  SnapshotLine,
  SnapshotSummary,
  TraceDetail,
  TraceSummary,
} from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { FunnelChart } from '../components/debug/FunnelChart'
import { Playground } from '../components/debug/Playground'
import { SnapshotItemDialog } from '../components/debug/SnapshotItemDialog'
import { SnapshotList } from '../components/debug/SnapshotList'
import { TraceTree } from '../components/debug/TraceTree'
import { funnelFromTrace } from '../lib/funnel'
import { clearLastError, reportError } from '../stores/connection'
import { useSession } from '../stores/session'

export function RetrievalDebugPage() {
  const taskId = useSession((s) => s.taskId)
  const workspaceId = useSession((s) => s.workspaceId)

  const [stageFilter, setStageFilter] = useState('')
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [snapshots, setSnapshots] = useState<SnapshotSummary[]>([])
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null)
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null)
  const [selectedSnapId, setSelectedSnapId] = useState<string | null>(null)
  const [snapDetail, setSnapDetail] = useState<SnapshotDetail | null>(null)
  const [snapLine, setSnapLine] = useState<SnapshotLine | null>(null)
  const [funnelExpanded, setFunnelExpanded] = useState(false)
  const [playgroundOut, setPlaygroundOut] = useState<PlaygroundOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const funnelStages = useMemo(
    () => (traceDetail ? funnelFromTrace(traceDetail) : []),
    [traceDetail],
  )

  const loadLists = useCallback(async () => {
    if (!taskId) return
    clearLastError()
    const [tPage, sPage] = await Promise.all([
      listTraces(taskId, {
        stage: stageFilter || undefined,
        limit: 50,
      }),
      listSnapshots(taskId, 50),
    ])
    setTraces(tPage.items)
    setSnapshots(sPage.items)
  }, [taskId, stageFilter])

  useEffect(() => {
    if (!taskId) {
      setLoading(false)
      return
    }
    let cancelled = false
    ;(async () => {
      setLoading(true)
      try {
        await loadLists()
      } catch (e) {
        reportError(e as ApiError | NetworkError | Error)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [taskId, loadLists])

  const onSelectTrace = async (id: string) => {
    setSelectedTraceId(id)
    setBusy(true)
    setFunnelExpanded(false)
    try {
      const d = await getTrace(id)
      setTraceDetail(d)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onSelectSnap = async (id: string) => {
    setSelectedSnapId(id)
    setSnapLine(null)
    setBusy(true)
    try {
      const d = await getSnapshot(id)
      setSnapDetail(d)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onOpenItem = async (position: number) => {
    if (!snapDetail?.has_full) return
    setBusy(true)
    try {
      const line = await getSnapshotItem(snapDetail.id, position)
      setSnapLine(line)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onPlaygroundRun = async (query: string, stage: string) => {
    if (!workspaceId) return
    setBusy(true)
    try {
      const out = await runPlayground(workspaceId, { query, stage })
      setPlaygroundOut(out)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  if (!taskId) {
    return (
      <div className="space-y-3" data-testid="retrieval-debug-page">
        <h1 className="text-lg font-semibold text-stone-900">检索调试</h1>
        <p className="text-sm text-stone-600">
          尚未选择任务。请先在{' '}
          <Link className="underline" to="/">
            会话
          </Link>{' '}
          中运行任务以产生轨迹与快照。
        </p>
      </div>
    )
  }

  return (
    <div
      className="flex h-[calc(100vh-8rem)] flex-col gap-3"
      data-testid="retrieval-debug-page"
    >
      <h1 className="text-lg font-semibold text-stone-900">检索调试</h1>
      {loading ? (
        <p className="text-sm text-stone-500">加载轨迹…</p>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[220px_1fr_280px]">
          <aside className="min-h-0 rounded border border-stone-200 bg-white p-2">
            <TraceTree
              traces={traces}
              selectedId={selectedTraceId}
              stageFilter={stageFilter}
              onStageFilter={setStageFilter}
              onSelect={(id) => void onSelectTrace(id)}
            />
          </aside>

          <section className="min-h-0 space-y-4 overflow-auto rounded border border-stone-200 bg-white p-3">
            {traceDetail ? (
              <>
                {traceDetail.degraded.length > 0 ? (
                  <div
                    className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950"
                    data-testid="degraded-banner"
                    role="status"
                  >
                    <span className="font-medium">degraded</span>
                    <ul className="mt-1 list-inside list-disc text-xs">
                      {traceDetail.degraded.map((d, i) => (
                        <li key={`${d.step}-${i}`}>
                          {d.step}: {d.reason} → {d.fallback}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <FunnelChart
                  stages={funnelStages}
                  candidates={traceDetail.candidates}
                  expanded={funnelExpanded}
                  onToggleExpand={() => setFunnelExpanded((v) => !v)}
                />
              </>
            ) : (
              <p className="text-sm text-stone-500">选择左侧轨迹查看漏斗</p>
            )}

            <SnapshotList
              snapshots={snapshots}
              selectedId={selectedSnapId}
              onSelect={(id) => void onSelectSnap(id)}
            />
            {snapDetail ? (
              <SnapshotItemDialog
                detail={snapDetail}
                line={snapLine}
                busy={busy}
                onOpenItem={(p) => void onOpenItem(p)}
                onCloseDialog={() => setSnapLine(null)}
              />
            ) : null}
          </section>

          <Playground
            disabled={!workspaceId}
            busy={busy}
            result={playgroundOut}
            onRun={(q, s) => void onPlaygroundRun(q, s)}
          />
        </div>
      )}
    </div>
  )
}
