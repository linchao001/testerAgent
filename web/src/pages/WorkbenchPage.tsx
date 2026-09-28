/**
 * WorkbenchPage（WP-F3）：用例三栏工作台——列表/MD 渲染/编辑抽屉；
 * If-Match 保存、批量评审、采纳率摘要、文件冲突红条。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getCase,
  listCases,
  reviewCases,
  updateCase,
} from '../api/endpoints'
import type {
  CaseDetail,
  CaseSummary,
  ReviewAction,
} from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { AdoptionSummary } from '../components/case/AdoptionSummary'
import { CaseList } from '../components/case/CaseList'
import { ConflictBanner } from '../components/case/ConflictBanner'
import { MdEditor } from '../components/case/MdEditor'
import { MdViewer } from '../components/case/MdViewer'
import { ReviewBar } from '../components/case/ReviewBar'
import { computeAdoption } from '../lib/adoption'
import { clearLastError, reportError } from '../stores/connection'
import { useSession } from '../stores/session'

export function WorkbenchPage() {
  const taskId = useSession((s) => s.taskId)

  const [cases, setCases] = useState<CaseSummary[]>([])
  const [allActive, setAllActive] = useState<CaseSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set())
  const [reviewFilter, setReviewFilter] = useState('')
  const [versionFilter, setVersionFilter] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [conflictHint, setConflictHint] = useState<string | null>(null)
  const [statusHint, setStatusHint] = useState<string | null>(null)

  const versions = useMemo(() => {
    const set = new Set(allActive.map((c) => c.stage_version))
    return [...set].sort((a, b) => a - b)
  }, [allActive])

  const adoption = useMemo(() => computeAdoption(allActive), [allActive])

  const errorCode =
    detail?.error_info && typeof detail.error_info.code === 'string'
      ? detail.error_info.code
      : null

  const loadList = useCallback(async () => {
    if (!taskId) return
    clearLastError()
    try {
      const [filtered, activePage] = await Promise.all([
        listCases(taskId, {
          status: 'active',
          review: reviewFilter || undefined,
          version: versionFilter ? Number(versionFilter) : undefined,
        }),
        listCases(taskId, { status: 'active', limit: 200 }),
      ])
      setCases(filtered.items)
      setAllActive(activePage.items)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
      throw e
    }
  }, [taskId, reviewFilter, versionFilter])

  const loadDetail = useCallback(
    async (caseId: string, opts?: { keepConflictHint?: boolean }) => {
      clearLastError()
      const d = await getCase(caseId)
      setDetail(d)
      setDraft(d.markdown)
      setEditing(false)
      if (!opts?.keepConflictHint) setConflictHint(null)
      return d
    },
    [],
  )

  useEffect(() => {
    if (!taskId) {
      setLoading(false)
      return
    }
    let cancelled = false
    ;(async () => {
      setLoading(true)
      try {
        await loadList()
      } catch {
        /* reported */
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [taskId, loadList])

  const onSelect = async (id: string) => {
    setSelectedId(id)
    setBusy(true)
    try {
      await loadDetail(id)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onToggleCheck = (id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const refreshAfterMutation = async (keepSelected = true) => {
    await loadList()
    if (keepSelected && selectedId) {
      await loadDetail(selectedId)
    }
  }

  const onSave = async () => {
    if (!detail) return
    setBusy(true)
    setConflictHint(null)
    setStatusHint(null)
    try {
      const updated = await updateCase(detail.id, draft, detail.content_hash)
      setDetail(updated)
      setDraft(updated.markdown)
      setEditing(false)
      setStatusHint('已保存')
      await loadList()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'VERSION_CONFLICT') {
        const expected = String(e.details?.expected ?? detail.content_hash)
        const current = String(e.details?.current ?? '?')
        try {
          await loadDetail(detail.id, { keepConflictHint: true })
          await loadList()
        } catch {
          /* reported below */
        }
        setConflictHint(
          `内容已变更（expected=${expected}, current=${current}）`,
        )
      }
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const runReview = async (action: ReviewAction) => {
    const ids = [...checkedIds]
    if (ids.length === 0) return
    setBusy(true)
    setStatusHint(null)
    try {
      await reviewCases(ids.map((case_id) => ({ case_id, action })))
      setCheckedIds(new Set())
      await refreshAfterMutation()
      setStatusHint('评审已提交')
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onReloadFromFile = async () => {
    if (!selectedId) return
    setBusy(true)
    try {
      await loadDetail(selectedId)
      setStatusHint('已从服务端重新加载')
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  const onOverwriteFile = async () => {
    if (!detail) return
    // 先拉最新 hash，再以当前草稿覆盖
    setBusy(true)
    setConflictHint(null)
    try {
      const fresh = await getCase(detail.id)
      const updated = await updateCase(detail.id, draft, fresh.content_hash)
      setDetail(updated)
      setDraft(updated.markdown)
      setEditing(false)
      await loadList()
      setStatusHint('已覆盖文件')
    } catch (e) {
      if (e instanceof ApiError && e.code === 'VERSION_CONFLICT') {
        const expected = String(e.details?.expected ?? '')
        const current = String(e.details?.current ?? '?')
        setConflictHint(
          `内容已变更（expected=${expected}, current=${current}）`,
        )
        try {
          await loadDetail(detail.id)
        } catch {
          /* */
        }
      }
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  if (!taskId) {
    return (
      <div className="space-y-3" data-testid="workbench-page">
        <h1 className="text-lg font-semibold text-stone-900">用例工作台</h1>
        <p className="text-sm text-stone-600">
          尚未选择任务。请先在{' '}
          <Link className="underline" to="/">
            会话
          </Link>{' '}
          中创建并运行任务。
        </p>
      </div>
    )
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-3" data-testid="workbench-page">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold text-stone-900">用例工作台</h1>
        <AdoptionSummary stats={adoption} />
      </div>

      {conflictHint ? (
        <div
          className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-950"
          data-testid="version-conflict"
          role="alert"
        >
          {conflictHint}
        </div>
      ) : null}
      {statusHint ? (
        <p className="text-xs text-stone-500" data-testid="status-hint">
          {statusHint}
        </p>
      ) : null}
      {loading ? (
        <p className="text-sm text-stone-500">加载用例…</p>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[240px_1fr_minmax(280px,1fr)]">
          <aside className="flex min-h-0 flex-col gap-2 rounded border border-stone-200 bg-white p-2">
            <CaseList
              cases={cases}
              selectedId={selectedId}
              checkedIds={checkedIds}
              reviewFilter={reviewFilter}
              versionFilter={versionFilter}
              versions={versions}
              onSelect={(id) => void onSelect(id)}
              onToggleCheck={onToggleCheck}
              onReviewFilter={setReviewFilter}
              onVersionFilter={setVersionFilter}
            />
            <ReviewBar
              disabled={busy}
              selectedCount={checkedIds.size}
              onAdopt={() => void runReview('adopt')}
              onReject={() => void runReview('reject')}
              onEditedAdopt={() => void runReview('edited_adopted')}
            />
          </aside>

          <section className="flex min-h-0 flex-col gap-2">
            {detail && errorCode ? (
              <ConflictBanner
                code={errorCode}
                busy={busy}
                saveDisabled={errorCode === 'file_missing'}
                onReloadFromFile={() => void onReloadFromFile()}
                onOverwriteFile={() => void onOverwriteFile()}
              />
            ) : null}
            {detail ? (
              <>
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate text-sm font-medium text-stone-800">
                    {detail.id.slice(0, 8)} · {cases.find((c) => c.id === detail.id)?.title ?? detail.id}
                  </h2>
                  {!editing ? (
                    <button
                      type="button"
                      className="rounded border border-stone-300 px-2 py-1 text-xs"
                      data-testid="open-editor"
                      disabled={busy || errorCode === 'file_missing'}
                      onClick={() => {
                        setDraft(detail.markdown)
                        setEditing(true)
                      }}
                    >
                      编辑
                    </button>
                  ) : null}
                </div>
                <div className="min-h-0 flex-1 overflow-hidden">
                  <MdViewer markdown={detail.markdown} />
                </div>
              </>
            ) : (
              <p className="text-sm text-stone-500">选择左侧用例查看 Markdown</p>
            )}
          </section>

          <section className="flex min-h-0 flex-col gap-2 rounded border border-stone-200 bg-white p-2">
            {editing && detail ? (
              <>
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-stone-700">编辑抽屉</span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className="rounded border border-stone-300 px-2 py-1 text-xs"
                      disabled={busy}
                      onClick={() => {
                        setDraft(detail.markdown)
                        setEditing(false)
                      }}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="rounded bg-stone-900 px-2.5 py-1 text-xs text-white disabled:opacity-40"
                      data-testid="save-case"
                      disabled={busy || errorCode === 'file_missing'}
                      onClick={() => void onSave()}
                    >
                      保存
                    </button>
                  </div>
                </div>
                <div className="min-h-0 flex-1">
                  <MdEditor
                    value={draft}
                    onChange={setDraft}
                    disabled={busy || errorCode === 'file_missing'}
                  />
                </div>
              </>
            ) : (
              <p className="p-2 text-sm text-stone-500">
                点「编辑」打开抽屉；保存将带 If-Match。
              </p>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
