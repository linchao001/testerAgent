/**
 * WorkspacesPage（WP-F5）：工作区 CRUD + kb/test 能力位只读 + 智能体绑定。
 */

import { useCallback, useEffect, useState } from 'react'
import {
  bindWorkspaceAgent,
  createWorkspace,
  deleteWorkspace,
  listAgents,
  listWorkspaceAgents,
  listWorkspaces,
  testKb,
  updateWorkspace,
} from '../api/endpoints'
import type {
  Agent,
  KbConfig,
  KbTestOut,
  ReMeCapabilities,
  Workspace,
} from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { CapsReadonly } from '../components/settings/CapsReadonly'
import { clearLastError, reportError } from '../stores/connection'
import { useSession } from '../stores/session'

const EMPTY_KB: KbConfig = {
  kb_id: 'zhb_kb',
  knowledge_dir: 'knowledge',
  options: {},
}

function asKbConfig(raw: Workspace['kb_config']): KbConfig {
  const r = raw as Partial<KbConfig>
  return {
    kb_id: typeof r.kb_id === 'string' ? r.kb_id : '',
    knowledge_bases_dir:
      typeof r.knowledge_bases_dir === 'string' ? r.knowledge_bases_dir : '',
    knowledge_dir:
      typeof r.knowledge_dir === 'string' ? r.knowledge_dir : 'knowledge',
    create_knowledge_base: Boolean(r.create_knowledge_base),
    options: r.options,
  }
}

type FormState = {
  name: string
  description: string
  kb: KbConfig
}

function formFromWs(ws: Workspace | null): FormState {
  if (!ws) {
    return { name: '', description: '', kb: { ...EMPTY_KB } }
  }
  return {
    name: ws.name,
    description: ws.description,
    kb: asKbConfig(ws.kb_config),
  }
}

export function WorkspacesPage() {
  const sessionWsId = useSession((s) => s.workspaceId)
  const setWorkspaceId = useSession((s) => s.setWorkspaceId)

  const [list, setList] = useState<Workspace[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState<FormState>(formFromWs(null))
  const [agents, setAgents] = useState<Agent[]>([])
  const [boundIds, setBoundIds] = useState<Set<string>>(new Set())
  const [kbResult, setKbResult] = useState<KbTestOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const selected =
    creating || !selectedId
      ? null
      : (list.find((w) => w.id === selectedId) ?? null)

  const reloadList = useCallback(async () => {
    const page = await listWorkspaces(100)
    setList(page.items)
    return page.items
  }, [])

  const loadAgents = useCallback(async (wsId: string | null) => {
    const all = await listAgents(50)
    setAgents(all.items)
    if (!wsId) {
      setBoundIds(new Set())
      return
    }
    const bound = await listWorkspaceAgents(wsId, 50)
    setBoundIds(new Set(bound.items.map((a) => a.id)))
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      clearLastError()
      setLoading(true)
      try {
        const items = await reloadList()
        if (cancelled) return
        const pick =
          (sessionWsId && items.find((w) => w.id === sessionWsId)?.id) ||
          items[0]?.id ||
          null
        if (pick) {
          setSelectedId(pick)
          setCreating(false)
          const ws = items.find((w) => w.id === pick)!
          setForm(formFromWs(ws))
          if (!sessionWsId) setWorkspaceId(pick)
          await loadAgents(pick)
        } else {
          setCreating(true)
          setForm(formFromWs(null))
          await loadAgents(null)
        }
      } catch (e) {
        reportError(e as ApiError | NetworkError | Error)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [reloadList, loadAgents, sessionWsId])

  async function selectWs(id: string) {
    const ws = list.find((w) => w.id === id)
    if (!ws) return
    setCreating(false)
    setSelectedId(id)
    setForm(formFromWs(ws))
    setKbResult(null)
    setWorkspaceId(id)
    clearLastError()
    setBusy(true)
    try {
      await loadAgents(id)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  function startCreate() {
    setCreating(true)
    setSelectedId(null)
    setForm(formFromWs(null))
    setKbResult(null)
    setBoundIds(new Set())
    clearLastError()
  }

  async function handleSave() {
    clearLastError()
    setBusy(true)
    try {
      if (creating) {
        const ws = await createWorkspace({
          name: form.name.trim(),
          description: form.description,
          kb_config: form.kb,
        })
        await reloadList()
        setCreating(false)
        setSelectedId(ws.id)
        setForm(formFromWs(ws))
        setWorkspaceId(ws.id)
        await loadAgents(ws.id)
      } else if (selectedId) {
        const ws = await updateWorkspace(selectedId, {
          name: form.name.trim(),
          description: form.description,
          kb_config: form.kb,
        })
        await reloadList()
        setForm(formFromWs(ws))
      }
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!selectedId || creating) return
    clearLastError()
    setBusy(true)
    try {
      await deleteWorkspace(selectedId)
      const items = await reloadList()
      setKbResult(null)
      if (sessionWsId === selectedId) setWorkspaceId(null)
      const next = items[0]
      if (next) {
        setSelectedId(next.id)
        setForm(formFromWs(next))
        setCreating(false)
        setWorkspaceId(next.id)
        await loadAgents(next.id)
      } else {
        startCreate()
      }
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  async function handleKbTest() {
    if (!selectedId || creating) return
    clearLastError()
    setBusy(true)
    setKbResult(null)
    try {
      const out = await testKb(selectedId)
      setKbResult(out)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  async function handleBind(agentId: string) {
    if (!selectedId || creating || boundIds.has(agentId)) return
    clearLastError()
    setBusy(true)
    try {
      await bindWorkspaceAgent(selectedId, agentId)
      setBoundIds((prev) => new Set([...prev, agentId]))
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <p className="text-sm text-stone-500" data-testid="workspaces-loading">
        加载工作区…
      </p>
    )
  }

  const caps: ReMeCapabilities | null = kbResult?.capabilities ?? null

  return (
    <div className="space-y-6" data-testid="workspaces-page">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold text-stone-900">工作区</h1>
        <p className="text-sm text-stone-500">
          绑定 ReMe 知识库连接；测试连接后能力位只读展示。选中即设为当前会话工作区。
        </p>
      </header>

      <div className="grid gap-6 md:grid-cols-[220px_1fr]">
        <aside className="space-y-2">
          <button
            type="button"
            data-testid="ws-new-btn"
            disabled={busy}
            onClick={startCreate}
            className="w-full rounded-md border border-dashed border-stone-300 px-3 py-2 text-sm text-stone-700 hover:bg-stone-50 disabled:opacity-50"
          >
            + 新建工作区
          </button>
          <ul className="space-y-1" data-testid="ws-list">
            {list.map((w) => {
              const active = !creating && w.id === selectedId
              return (
                <li key={w.id}>
                  <button
                    type="button"
                    data-testid={`ws-item-${w.id}`}
                    onClick={() => void selectWs(w.id)}
                    className={[
                      'w-full rounded-md px-3 py-2 text-left text-sm',
                      active
                        ? 'bg-stone-900 text-white'
                        : 'bg-white text-stone-700 hover:bg-stone-100',
                    ].join(' ')}
                  >
                    {w.name}
                    {sessionWsId === w.id ? (
                      <span className="ml-1 text-xs opacity-70">·当前</span>
                    ) : null}
                  </button>
                </li>
              )
            })}
          </ul>
        </aside>

        <section className="space-y-4 rounded-md border border-stone-200 bg-white p-4">
          <h2 className="text-sm font-medium text-stone-800">
            {creating ? '新建工作区' : selected ? '编辑工作区' : '工作区'}
          </h2>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-sm sm:col-span-2">
              <span className="mb-1 block text-stone-700">名称</span>
              <input
                aria-label="名称"
                className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
                value={form.name}
                disabled={busy}
                onChange={(e) =>
                  setForm((f) => ({ ...f, name: e.target.value }))
                }
              />
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="mb-1 block text-stone-700">描述</span>
              <input
                aria-label="描述"
                className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
                value={form.description}
                disabled={busy}
                onChange={(e) =>
                  setForm((f) => ({ ...f, description: e.target.value }))
                }
              />
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="mb-1 block text-stone-700">知识库 ID</span>
              <input
                aria-label="知识库 ID"
                className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
                value={form.kb.kb_id}
                disabled={busy}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    kb: { ...f.kb, kb_id: e.target.value },
                  }))
                }
              />
            </label>
          </div>

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              data-testid="ws-save-btn"
              disabled={busy || !form.name.trim()}
              onClick={() => void handleSave()}
              className="rounded-md bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700 disabled:opacity-50"
            >
              {creating ? '创建' : '保存'}
            </button>
            {!creating && selectedId ? (
              <>
                <button
                  type="button"
                  data-testid="kb-test-btn"
                  disabled={busy}
                  onClick={() => void handleKbTest()}
                  className="rounded-md border border-stone-300 px-3 py-1.5 text-sm text-stone-800 hover:bg-stone-50 disabled:opacity-50"
                >
                  测试连接
                </button>
                <button
                  type="button"
                  data-testid="ws-delete-btn"
                  disabled={busy}
                  onClick={() => void handleDelete()}
                  className="rounded-md border border-red-200 px-3 py-1.5 text-sm text-red-800 hover:bg-red-50 disabled:opacity-50"
                >
                  删除
                </button>
              </>
            ) : null}
          </div>

          {kbResult ? (
            <div
              className="space-y-2 rounded border border-teal-200 bg-teal-50/50 px-3 py-2"
              data-testid="kb-test-result"
            >
              <p className="text-sm text-teal-950">
                连接成功 · 延迟{' '}
                <span data-testid="kb-test-latency">{kbResult.latency_ms}</span>{' '}
                ms
              </p>
              {caps ? <CapsReadonly caps={caps} /> : null}
            </div>
          ) : null}

          {!creating && selectedId ? (
            <div className="space-y-2 border-t border-stone-100 pt-3">
              <h3 className="text-sm font-medium text-stone-800">绑定智能体</h3>
              <p className="text-xs text-stone-500">
                一期仅内置用例智能体；勾选即绑定（不可解绑，API 未开放）。
              </p>
              <ul className="space-y-1" data-testid="agent-bind-list">
                {agents.map((a) => {
                  const checked = boundIds.has(a.id)
                  return (
                    <li key={a.id} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        id={`agent-${a.id}`}
                        data-testid={`agent-bind-${a.id}`}
                        checked={checked}
                        disabled={busy || checked}
                        onChange={() => void handleBind(a.id)}
                      />
                      <label htmlFor={`agent-${a.id}`} className="text-stone-800">
                        {a.name}
                        {a.builtin ? (
                          <span className="ml-1 text-xs text-stone-400">
                            （内置）
                          </span>
                        ) : null}
                      </label>
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  )
}
