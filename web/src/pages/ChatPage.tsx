/**
 * ChatPage（WP-F1）：需求输入 → 建任务自动 run → SSE → 澄清卡 / checkpoint 提示；
 * change_request 带 @阶段 时 ImpactPreview 二次确认后 rollback。
 */

import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  answerTask,
  createConversation,
  createTask,
  getTask,
  listMessages,
  listWorkspaces,
  rollbackTask,
  runTask,
  sendMessage,
} from '../api/endpoints'
import type { Message, StageName, Task } from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { ClarificationCard } from '../components/chat/ClarificationCard'
import { MessageList } from '../components/chat/MessageList'
import { RequirementInput } from '../components/chat/RequirementInput'
import { GateConfirmCard } from '../components/session/GateConfirmCard'
import { ReviewProposalCard } from '../components/session/ReviewProposalCard'
import { ImpactPreview } from '../components/confirm/ImpactPreview'
import { parseStageMention, stageLabel } from '../lib/parseStageMention'
import { clearLastError, reportError } from '../stores/connection'
import { useSession } from '../stores/session'
import { useTaskStream } from '../stores/taskStream'

type PendingRollback = {
  stage: StageName
  content: string
}

export function ChatPage() {
  const workspaceId = useSession((s) => s.workspaceId)
  const conversationId = useSession((s) => s.conversationId)
  const taskId = useSession((s) => s.taskId)
  const setWorkspaceId = useSession((s) => s.setWorkspaceId)
  const setConversationId = useSession((s) => s.setConversationId)
  const setTaskId = useSession((s) => s.setTaskId)

  const checkpoint = useTaskStream((s) => s.checkpoint)
  const clarification = useTaskStream((s) => s.clarification)
  const phase = useTaskStream((s) => s.phase)
  const taskError = useTaskStream((s) => s.taskError)
  const attach = useTaskStream((s) => s.attach)
  const detach = useTaskStream((s) => s.detach)
  const clearClarification = useTaskStream((s) => s.clearClarification)
  const clearCheckpoint = useTaskStream((s) => s.clearCheckpoint)

  const [bootstrapping, setBootstrapping] = useState(true)
  const [messages, setMessages] = useState<Message[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const [composer, setComposer] = useState('')
  const [busy, setBusy] = useState(false)
  const [statusHint, setStatusHint] = useState<string | null>(null)
  const [pendingRollback, setPendingRollback] =
    useState<PendingRollback | null>(null)
  const [impactPending, setImpactPending] = useState(false)
  const [impactResult, setImpactResult] = useState<string | null>(null)

  const refreshMessages = useCallback(async (convId: string) => {
    const page = await listMessages(convId, 100)
    setMessages(page.items)
  }, [])

  const refreshTask = useCallback(async (id: string) => {
    const t = await getTask(id)
    setTask(t)
    return t
  }, [])

  // 引导：选用已有工作区 + 建会话；无工作区则引导去 /workspaces（不静默建默认区）
  useEffect(() => {
    let cancelled = false
    async function boot() {
      clearLastError()
      try {
        let wsId = useSession.getState().workspaceId
        let convId = useSession.getState().conversationId
        if (!wsId) {
          const page = await listWorkspaces(1)
          if (page.items[0]) {
            wsId = page.items[0].id
            if (!cancelled) setWorkspaceId(wsId)
          } else {
            return
          }
        }
        if (!convId && wsId) {
          const conv = await createConversation({
            workspace_id: wsId,
            title: '用例设计会话',
          })
          convId = conv.id
          if (!cancelled) setConversationId(convId)
        }
        if (!cancelled && convId) await refreshMessages(convId)
      } catch (err) {
        if (!cancelled) reportError(err as ApiError | NetworkError | Error)
      } finally {
        if (!cancelled) setBootstrapping(false)
      }
    }
    void boot()
    return () => {
      cancelled = true
    }
  }, [refreshMessages, setConversationId, setWorkspaceId])

  // 任务 SSE
  useEffect(() => {
    if (!taskId) {
      detach()
      return
    }
    attach(taskId)
    void refreshTask(taskId).catch((err) =>
      reportError(err as ApiError | NetworkError | Error),
    )
    return () => detach()
  }, [taskId, attach, detach, refreshTask])

  // checkpoint 后刷新任务（拿 active_artifacts）
  useEffect(() => {
    if (checkpoint && taskId) {
      void refreshTask(taskId).catch(() => undefined)
      setStatusHint(
        `已到达检查点：${stageLabel(checkpoint.stage)}（v${checkpoint.stage_version}）`,
      )
    }
  }, [checkpoint, taskId, refreshTask])

  async function handleRequirementSubmit(md: string) {
    if (!conversationId) return
    clearLastError()
    setBusy(true)
    setStatusHint('创建任务…')
    setImpactResult(null)
    try {
      const created = await createTask({
        conversation_id: conversationId,
        requirement_md: md,
      })
      setTaskId(created.id)
      setTask(created)
      setStatusHint('启动生成…')
      await runTask(created.id)
      setStatusHint('生成中，已订阅事件流…')
      await refreshMessages(conversationId)
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
      setStatusHint(null)
    } finally {
      setBusy(false)
    }
  }

  async function handleClarificationSubmit(
    answers: Array<{ question_id: string; answer: string }>,
  ) {
    if (!taskId || !conversationId) return
    clearLastError()
    setBusy(true)
    try {
      await answerTask(taskId, answers)
      clearClarification()
      setStatusHint('已提交澄清答复，继续生成…')
      await refreshMessages(conversationId)
      await refreshTask(taskId)
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  function handleComposerSend() {
    const content = composer.trim()
    if (!content || !conversationId || busy) return
    const stage = parseStageMention(content)
    if (stage) {
      setPendingRollback({ stage, content })
      return
    }
    void sendChat(content)
  }

  async function sendChat(content: string) {
    if (!conversationId) return
    clearLastError()
    setBusy(true)
    try {
      await sendMessage(conversationId, { content, kind: 'chat' })
      setComposer('')
      await refreshMessages(conversationId)
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  async function confirmRollback() {
    if (!pendingRollback || !taskId || !conversationId) return
    const { stage, content } = pendingRollback
    clearLastError()
    setImpactPending(true)
    try {
      const fresh = await refreshTask(taskId)
      const art = fresh.active_artifacts[stage]
      if (!art) {
        throw new Error(
          `当前任务尚无「${stageLabel(stage)}」阶段产物，无法回退。请先完成该阶段。`,
        )
      }
      await sendMessage(conversationId, {
        content,
        kind: 'change_request',
        context: { target_stage: stage },
      })
      const out = await rollbackTask(taskId, {
        target_stage: stage,
        artifact_id: art.id,
        expected_version: art.stage_version,
      })
      setComposer('')
      setPendingRollback(null)
      setImpactResult(
        out.impact.summary ||
          `已回退到「${stageLabel(stage)}」，下游将重新生成。`,
      )
      setStatusHint(`回退已启动（run ${out.graph_run_id.slice(0, 8)}…）`)
      await refreshMessages(conversationId)
      await refreshTask(taskId)
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
    } finally {
      setImpactPending(false)
    }
  }

  if (bootstrapping) {
    return (
      <p className="text-sm text-stone-500" data-testid="chat-booting">
        正在准备工作区与会话…
      </p>
    )
  }

  if (!workspaceId) {
    return (
      <div className="space-y-3" data-testid="chat-need-workspace">
        <h1 className="text-xl font-semibold text-stone-900">会话</h1>
        <p className="text-sm text-stone-600">
          尚未配置工作区。请先创建工作区并绑定知识库连接，再回来发起需求。
        </p>
        <Link
          to="/workspaces"
          className="inline-block text-sm font-medium text-teal-800 underline"
          data-testid="goto-workspaces"
        >
          前往工作区 →
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-6" data-testid="chat-page">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold text-stone-900">会话</h1>
        <p className="text-sm text-stone-500">
          粘贴或导入需求 MD，平台将自动创建任务并启动四阶段生成。
        </p>
        <p className="text-xs text-stone-400">
          工作区 {workspaceId ?? '—'} · 会话 {conversationId ?? '—'}
          {taskId ? ` · 任务 ${taskId}` : ''}
        </p>
      </header>

      {!taskId ? (
        <RequirementInput disabled={busy} onSubmit={handleRequirementSubmit} />
      ) : (
        <section
          className="rounded-md border border-stone-200 bg-white px-4 py-3"
          data-testid="task-status"
        >
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="font-medium text-stone-800">
              状态：{task?.status ?? '…'}
            </span>
            <span className="text-stone-500">
              阶段：{task?.current_stage ?? phase ?? '—'}
            </span>
            {phase ? (
              <span className="text-xs text-stone-400" data-testid="stream-phase">
                流：{phase}
              </span>
            ) : null}
          </div>
          {statusHint ? (
            <p className="mt-1 text-sm text-teal-800" data-testid="status-hint">
              {statusHint}
            </p>
          ) : null}
          {taskError ? (
            <p className="mt-1 text-sm text-red-700" data-testid="task-error">
              [{taskError.code}] {taskError.message}
            </p>
          ) : null}
          {checkpoint && taskId ? (
            <div className="mt-3" data-testid="checkpoint-banner">
              {checkpoint.gate_kind === 'review_decision' ? (
                <ReviewProposalCard
                  taskId={taskId}
                  checkpoint={checkpoint}
                  onDone={() => {
                    clearCheckpoint()
                    void refreshTask(taskId)
                  }}
                />
              ) : (
                <>
                  <GateConfirmCard
                    taskId={taskId}
                    checkpoint={checkpoint}
                    onDone={() => {
                      clearCheckpoint()
                      void refreshTask(taskId)
                    }}
                  />
                  <Link
                    to="/confirm"
                    className="mt-2 inline-block text-sm font-medium text-teal-800 underline"
                  >
                    打开完整确认页（可选）→
                  </Link>
                </>
              )}
            </div>
          ) : null}
          {impactResult ? (
            <p className="mt-2 text-sm text-stone-600" data-testid="impact-result">
              {impactResult}
            </p>
          ) : null}
        </section>
      )}

      {clarification && clarification.length > 0 ? (
        <ClarificationCard
          questions={clarification}
          disabled={busy}
          onSubmit={handleClarificationSubmit}
        />
      ) : null}

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-stone-800">消息</h2>
        <MessageList messages={messages} />
      </section>

      {taskId ? (
        <section className="space-y-2" data-testid="composer">
          <label className="block text-sm font-medium text-stone-800" htmlFor="chat-composer">
            对话 / 变更请求
          </label>
          <p className="text-xs text-stone-500">
            普通消息直接发送；含{' '}
            <code className="rounded bg-stone-100 px-1">@链路</code> /{' '}
            <code className="rounded bg-stone-100 px-1">@测试点</code>{' '}
            时作为 change_request，确认后回退对应阶段。
          </p>
          <textarea
            id="chat-composer"
            data-testid="composer-input"
            className="min-h-20 w-full rounded-md border border-stone-300 bg-white px-3 py-2 text-sm outline-none focus:border-stone-400"
            value={composer}
            disabled={busy}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="例如：@链路 需求范围调整，请从链路识别重跑…"
          />
          <div className="flex justify-end">
            <button
              type="button"
              data-testid="composer-send"
              disabled={busy || !composer.trim()}
              className="rounded-md bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700 disabled:opacity-50"
              onClick={handleComposerSend}
            >
              发送
            </button>
          </div>
        </section>
      ) : null}

      <ImpactPreview
        open={pendingRollback != null}
        targetStage={pendingRollback?.stage ?? 'link_identify'}
        messagePreview={pendingRollback?.content ?? ''}
        pending={impactPending}
        onCancel={() => setPendingRollback(null)}
        onConfirm={confirmRollback}
      />
    </div>
  )
}
