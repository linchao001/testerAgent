/**
 * ChatPage：聊天优先会话壳。
 * 自由对话（可附 .md）→ 助手经 start_case_generation 建任务；门禁/澄清内联。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  answerTask,
  createConversation,
  getTask,
  listMessages,
  listWorkspaces,
  rollbackTask,
  sendMessage,
} from '../api/endpoints'
import type { Message, StageName, Task } from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import {
  ChatComposer,
  composeMessageWithAttachments,
  type PendingAttachment,
} from '../components/chat/ChatComposer'
import { ClarificationCard } from '../components/chat/ClarificationCard'
import { MessageList } from '../components/chat/MessageList'
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
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [busy, setBusy] = useState(false)
  const [statusHint, setStatusHint] = useState<string | null>(null)
  const [pendingRollback, setPendingRollback] =
    useState<PendingRollback | null>(null)
  const [impactPending, setImpactPending] = useState(false)
  const [impactResult, setImpactResult] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const refreshMessages = useCallback(async (convId: string) => {
    const page = await listMessages(convId, 100)
    setMessages(page.items)
  }, [])

  const refreshTask = useCallback(async (id: string) => {
    const t = await getTask(id)
    setTask(t)
    return t
  }, [])

  useEffect(() => {
    const el = messagesEndRef.current
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, clarification, checkpoint])

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

  useEffect(() => {
    if (checkpoint && taskId) {
      void refreshTask(taskId).catch(() => undefined)
      setStatusHint(
        `已到达检查点：${stageLabel(checkpoint.stage)}（v${checkpoint.stage_version}）`,
      )
    }
  }, [checkpoint, taskId, refreshTask])

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
    const content = composeMessageWithAttachments(composer, attachments)
    if (!content || !conversationId || busy) return
    const stage = parseStageMention(content)
    if (stage && taskId) {
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
      const out = await sendMessage(conversationId, { content, kind: 'chat' })
      setComposer('')
      setAttachments([])
      const started =
        out.started_task_id ||
        (typeof out.assistant?.payload?.started_task_id === 'string'
          ? out.assistant.payload.started_task_id
          : null)
      if (started) {
        setTaskId(started)
        setStatusHint('已启动用例生成，订阅事件流…')
        setImpactResult(null)
      }
      await refreshMessages(conversationId)
      if (started) {
        await refreshTask(started).catch(() => undefined)
      }
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
      setAttachments([])
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
      <p className="p-4 text-sm text-stone-500" data-testid="chat-booting">
        正在准备工作区与会话…
      </p>
    )
  }

  if (!workspaceId) {
    return (
      <div className="space-y-3 p-4" data-testid="chat-need-workspace">
        <h1 className="text-xl font-semibold text-stone-900">会话</h1>
        <p className="text-sm text-stone-600">
          尚未配置工作区。请先创建工作区并绑定知识库连接，再回来对话。
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
    <div className="flex h-full min-h-0 flex-col" data-testid="chat-page">
      <header className="shrink-0 space-y-1 border-b border-stone-100 px-1 pb-3 pt-2">
        <h1 className="text-lg font-semibold text-stone-900">会话</h1>
        <p className="text-xs text-stone-400">
          工作区 {workspaceId} · 会话 {conversationId ?? '—'}
          {taskId ? ` · 任务 ${taskId}` : ''}
        </p>
      </header>

      {taskId ? (
        <section
          className="mx-1 mt-2 shrink-0 rounded-lg border border-stone-200 bg-white px-3 py-2"
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
          {impactResult ? (
            <p className="mt-2 text-sm text-stone-600" data-testid="impact-result">
              {impactResult}
            </p>
          ) : null}
        </section>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto px-1">
        <MessageList messages={messages} />

        {clarification && clarification.length > 0 ? (
          <div className="mb-4">
            <ClarificationCard
              questions={clarification}
              disabled={busy}
              onSubmit={handleClarificationSubmit}
            />
          </div>
        ) : null}

        {checkpoint && taskId ? (
          <div className="mb-4" data-testid="checkpoint-banner">
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

        <div ref={messagesEndRef} />
      </div>

      <ChatComposer
        value={composer}
        attachments={attachments}
        disabled={busy}
        onChange={setComposer}
        onAttachmentsChange={setAttachments}
        onSend={handleComposerSend}
      />

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
