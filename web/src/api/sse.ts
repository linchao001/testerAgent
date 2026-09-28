/**
 * EventSource 封装（dd §12.1 / §12.2 sse.ts）：
 * - URL 带 ?after_event_id= 兜底（浏览器 Last-Event-ID 在重建连接时不可靠）
 * - lastEventId 落 localStorage
 * - 重连退避 1s / 2s / 5s … 封顶 30s
 * - 仅订阅已知事件类型；未知类型忽略
 */

export type SseStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'

export type SseEvent = {
  id: string | null
  type: string
  data: unknown
}

export type TaskEventSourceOptions = {
  /** 已知事件类型；未列出的 named event 不注册 → 忽略 */
  eventTypes?: string[]
  onEvent?: (event: SseEvent) => void
  onStatus?: (status: SseStatus) => void
  /** 可注入，便于单测 */
  eventSourceFactory?: (url: string) => EventSource
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  /** 可注入定时器 */
  schedule?: (fn: () => void, ms: number) => number
  clearSchedule?: (id: number) => void
}

const DEFAULT_EVENT_TYPES = [
  'node_start',
  'node_end',
  'batch_progress',
  'checkpoint_waiting',
  'human_gate_waiting',
  'plan_updated',
  'step_started',
  'step_finished',
  'subtask_started',
  'subtask_finished',
  'reflection_result',
  'review_proposal_ready',
  'clarification_needed',
  'case_generated',
  'task_error',
  'retrieval_summary',
  'context_assembled',
  'budget_warning',
  'ping',
] as const

const STORAGE_PREFIX = 'testerAgent:sse:lastEventId:'

/** 退避序列：1s → 2s → 5s → 10s → 20s → 30s（封顶）。 */
export function nextBackoffMs(attempt: number): number {
  const seq = [1000, 2000, 5000, 10_000, 20_000, 30_000]
  if (attempt <= 0) return seq[0]
  return seq[Math.min(attempt, seq.length - 1)]
}

export function storageKey(taskId: string): string {
  return `${STORAGE_PREFIX}${taskId}`
}

export function loadLastEventId(
  taskId: string,
  storage: Pick<Storage, 'getItem'> = localStorage,
): number | null {
  const raw = storage.getItem(storageKey(taskId))
  if (raw == null || raw === '') return null
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? n : null
}

export function saveLastEventId(
  taskId: string,
  id: number,
  storage: Pick<Storage, 'setItem'> = localStorage,
): void {
  storage.setItem(storageKey(taskId), String(id))
}

export function buildEventsUrl(
  taskId: string,
  afterEventId?: number | null,
  base = '',
): string {
  const path = `${base}/api/v1/tasks/${encodeURIComponent(taskId)}/events`
  if (afterEventId == null || afterEventId <= 0) return path
  return `${path}?after_event_id=${afterEventId}`
}

function parseData(raw: string): unknown {
  try {
    return JSON.parse(raw) as unknown
  } catch {
    return raw
  }
}

export class TaskEventSource {
  private readonly taskId: string
  private readonly eventTypes: string[]
  private readonly onEvent?: (event: SseEvent) => void
  private readonly onStatus?: (status: SseStatus) => void
  private readonly factory: (url: string) => EventSource
  private readonly storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  private readonly schedule: (fn: () => void, ms: number) => number
  private readonly clearSchedule: (id: number) => void

  private es: EventSource | null = null
  private timer: number | null = null
  private attempt = 0
  private stopped = true
  private status: SseStatus = 'idle'

  constructor(taskId: string, options: TaskEventSourceOptions = {}) {
    this.taskId = taskId
    this.eventTypes = [...(options.eventTypes ?? DEFAULT_EVENT_TYPES)]
    this.onEvent = options.onEvent
    this.onStatus = options.onStatus
    this.factory =
      options.eventSourceFactory ?? ((url) => new EventSource(url))
    this.storage = options.storage ?? localStorage
    this.schedule = options.schedule ?? ((fn, ms) => window.setTimeout(fn, ms))
    this.clearSchedule =
      options.clearSchedule ?? ((id) => window.clearTimeout(id))
  }

  get currentStatus(): SseStatus {
    return this.status
  }

  start(): void {
    if (!this.stopped && this.es) return
    this.stopped = false
    this.connect(true)
  }

  stop(): void {
    this.stopped = true
    this.clearTimer()
    this.closeEs()
    this.setStatus('closed')
  }

  /** 测试/调试：手动触发一次断线重连调度。 */
  simulateDisconnect(): void {
    if (this.stopped) return
    this.closeEs()
    this.scheduleReconnect()
  }

  private setStatus(status: SseStatus): void {
    this.status = status
    this.onStatus?.(status)
  }

  private clearTimer(): void {
    if (this.timer != null) {
      this.clearSchedule(this.timer)
      this.timer = null
    }
  }

  private closeEs(): void {
    if (this.es) {
      this.es.close()
      this.es = null
    }
  }

  private connect(initial: boolean): void {
    this.clearTimer()
    this.closeEs()
    this.setStatus(initial && this.attempt === 0 ? 'connecting' : 'reconnecting')

    const after = loadLastEventId(this.taskId, this.storage)
    const url = buildEventsUrl(this.taskId, after)
    const es = this.factory(url)
    this.es = es

    es.onopen = () => {
      this.attempt = 0
      this.setStatus('open')
    }

    es.onerror = () => {
      if (this.stopped) return
      this.closeEs()
      this.scheduleReconnect()
    }

    for (const type of this.eventTypes) {
      es.addEventListener(type, (ev) => {
        const me = ev as MessageEvent<string>
        const idRaw = me.lastEventId
        if (idRaw) {
          const n = Number(idRaw)
          if (Number.isFinite(n)) {
            saveLastEventId(this.taskId, n, this.storage)
          }
        }
        this.onEvent?.({
          id: idRaw || null,
          type,
          data: parseData(me.data),
        })
      })
    }
  }

  private scheduleReconnect(): void {
    this.setStatus('reconnecting')
    const delay = nextBackoffMs(this.attempt)
    this.attempt += 1
    this.timer = this.schedule(() => {
      this.timer = null
      if (!this.stopped) this.connect(false)
    }, delay)
  }
}
