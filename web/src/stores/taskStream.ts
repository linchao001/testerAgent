/**
 * 当前任务 SSE 流 → UI 进行态（dd §12.2）。
 * 收到 checkpoint_waiting / clarification_needed 写入 store，不强制跳转。
 */

import { create } from 'zustand'
import {
  TaskEventSource,
  type SseEvent,
  type SseStatus,
  type TaskEventSourceOptions,
} from '../api/sse'
import type {
  CheckpointWaiting,
  ClarificationQuestion,
} from '../api/domain'
import { setSseStatus } from './connection'

export type BatchProgress = {
  done: boolean
  total: number
  batch_id: string
}

export type TaskStreamState = {
  taskId: string | null
  phase: string | null
  checkpoint: CheckpointWaiting | null
  clarification: ClarificationQuestion[] | null
  batches: Record<string, BatchProgress>
  budgetWarnings: Array<{ node: string; tokens_est: number; budget: number }>
  taskError: {
    code: string
    message: string
    retryable: boolean
    node: string | null
  } | null
  /** 可注入，便于单测脚本事件 */
  eventSourceFactory: TaskEventSourceOptions['eventSourceFactory']
  attach: (taskId: string) => void
  detach: () => void
  applyEvent: (event: SseEvent) => void
  clearClarification: () => void
  clearCheckpoint: () => void
  setEventSourceFactory: (
    factory: TaskEventSourceOptions['eventSourceFactory'],
  ) => void
  reset: () => void
}

let source: TaskEventSource | null = null

function normalizeQuestions(raw: unknown): ClarificationQuestion[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((q): q is Record<string, unknown> => !!q && typeof q === 'object')
    .map((q, i) => ({
      id: String(q.id ?? `q-${i + 1}`),
      question: String(q.question ?? q.text ?? ''),
      options: Array.isArray(q.options)
        ? q.options.map(String)
        : undefined,
    }))
    .filter((q) => q.question)
}

const initialSlice = {
  taskId: null as string | null,
  phase: null as string | null,
  checkpoint: null as CheckpointWaiting | null,
  clarification: null as ClarificationQuestion[] | null,
  batches: {} as Record<string, BatchProgress>,
  budgetWarnings: [] as TaskStreamState['budgetWarnings'],
  taskError: null as TaskStreamState['taskError'],
}

export const useTaskStream = create<TaskStreamState>((set, get) => ({
  ...initialSlice,
  eventSourceFactory: undefined,

  setEventSourceFactory: (factory) => set({ eventSourceFactory: factory }),

  applyEvent: (event) => {
    const data = (event.data && typeof event.data === 'object'
      ? event.data
      : {}) as Record<string, unknown>

    switch (event.type) {
      case 'node_start':
        set({
          phase: typeof data.node === 'string' ? data.node : get().phase,
          taskError: null,
        })
        break
      case 'node_end':
        set({
          phase: typeof data.node === 'string' ? `${data.node}:done` : get().phase,
        })
        break
      case 'batch_progress':
        if (typeof data.node === 'string' && typeof data.batch_id === 'string') {
          set({
            batches: {
              ...get().batches,
              [data.node]: {
                batch_id: data.batch_id,
                done: Boolean(data.done),
                total: Number(data.total) || 0,
              },
            },
          })
        }
        break
      case 'checkpoint_waiting':
      case 'human_gate_waiting':
        if (
          typeof data.stage === 'string' &&
          typeof data.artifact_id === 'string'
        ) {
          set({
            checkpoint: {
              stage: data.stage,
              artifact_id: data.artifact_id,
              stage_version: Number(data.stage_version) || 1,
              gate_kind:
                data.gate_kind === 'review_decision'
                  ? 'review_decision'
                  : data.gate_kind === 'plan_confirm'
                    ? 'plan_confirm'
                    : undefined,
              step_id:
                typeof data.step_id === 'string' ? data.step_id : undefined,
            },
            phase: `checkpoint:${data.stage}`,
            clarification: null,
          })
        }
        break
      case 'clarification_needed':
        set({
          clarification: normalizeQuestions(data.questions),
          phase: 'clarification',
        })
        break
      case 'budget_warning':
        if (
          typeof data.node === 'string' &&
          typeof data.tokens_est === 'number' &&
          typeof data.budget === 'number'
        ) {
          set({
            budgetWarnings: [
              ...get().budgetWarnings,
              {
                node: data.node,
                tokens_est: data.tokens_est,
                budget: data.budget,
              },
            ],
          })
        }
        break
      case 'task_error':
        set({
          taskError: {
            code: String(data.code ?? 'INTERNAL'),
            message: String(data.message ?? '任务失败'),
            retryable: Boolean(data.retryable),
            node: typeof data.node === 'string' ? data.node : null,
          },
          phase: 'error',
        })
        break
      case 'task_done':
        set({ phase: 'completed' })
        break
      default:
        break
    }
  },

  attach: (taskId) => {
    const prev = get().taskId
    if (prev === taskId && source) return
    source?.stop()
    source = null
    set({
      ...initialSlice,
      taskId,
      eventSourceFactory: get().eventSourceFactory,
    })
    const src = new TaskEventSource(taskId, {
      eventSourceFactory: get().eventSourceFactory,
      onStatus: (s: SseStatus) => setSseStatus(s),
      onEvent: (e) => get().applyEvent(e),
    })
    source = src
    src.start()
  },

  detach: () => {
    source?.stop()
    source = null
    setSseStatus('idle')
    set({ ...initialSlice, eventSourceFactory: get().eventSourceFactory })
  },

  clearClarification: () => set({ clarification: null }),
  clearCheckpoint: () => set({ checkpoint: null }),

  reset: () => {
    source?.stop()
    source = null
    setSseStatus('idle')
    set({ ...initialSlice, eventSourceFactory: undefined })
  },
}))
