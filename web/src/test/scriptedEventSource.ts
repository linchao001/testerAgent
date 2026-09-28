/** 可脚本驱动的 EventSource 替身（验收：事件序列 → checkpoint_waiting）。 */

type Listener = (ev: MessageEvent<string>) => void

export class ScriptedEventSource {
  static lastInstance: ScriptedEventSource | null = null
  static script: Array<{ type: string; data: unknown; id?: string }> = []

  readonly url: string
  onopen: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  readyState = 0

  private listeners = new Map<string, Set<Listener>>()
  private closed = false

  constructor(url: string) {
    this.url = url
    ScriptedEventSource.lastInstance = this
    queueMicrotask(() => {
      if (this.closed) return
      this.readyState = 1
      this.onopen?.(new Event('open'))
      for (const frame of ScriptedEventSource.script) {
        this.emit(frame.type, frame.data, frame.id)
      }
    })
  }

  addEventListener(type: string, listener: EventListener): void {
    const set = this.listeners.get(type) ?? new Set()
    set.add(listener as Listener)
    this.listeners.set(type, set)
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.get(type)?.delete(listener as Listener)
  }

  close(): void {
    this.closed = true
    this.readyState = 2
  }

  emit(type: string, data: unknown, id = String(Date.now())): void {
    if (this.closed) return
    const me = new MessageEvent(type, {
      data: typeof data === 'string' ? data : JSON.stringify(data),
      lastEventId: id,
    })
    this.listeners.get(type)?.forEach((fn) => fn(me))
  }

  static reset(): void {
    ScriptedEventSource.lastInstance = null
    ScriptedEventSource.script = []
  }
}
