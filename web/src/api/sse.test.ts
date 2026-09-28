import { describe, expect, it, vi } from 'vitest'
import {
  buildEventsUrl,
  loadLastEventId,
  nextBackoffMs,
  saveLastEventId,
  TaskEventSource,
} from './sse'

describe('sse helpers', () => {
  it('backoff sequence caps at 30s', () => {
    expect(nextBackoffMs(0)).toBe(1000)
    expect(nextBackoffMs(1)).toBe(2000)
    expect(nextBackoffMs(2)).toBe(5000)
    expect(nextBackoffMs(5)).toBe(30_000)
    expect(nextBackoffMs(99)).toBe(30_000)
  })

  it('buildEventsUrl adds after_event_id', () => {
    expect(buildEventsUrl('t1')).toBe('/api/v1/tasks/t1/events')
    expect(buildEventsUrl('t1', 12)).toBe(
      '/api/v1/tasks/t1/events?after_event_id=12',
    )
  })

  it('persists last event id', () => {
    const mem = new Map<string, string>()
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v)
      },
      removeItem: (k: string) => {
        mem.delete(k)
      },
    }
    expect(loadLastEventId('task-a', storage)).toBeNull()
    saveLastEventId('task-a', 7, storage)
    expect(loadLastEventId('task-a', storage)).toBe(7)
  })
})

describe('TaskEventSource', () => {
  it('enters reconnecting after error and uses backoff', () => {
    vi.useFakeTimers()
    const statuses: string[] = []
    const listeners = new Map<string, EventListener>()

    class FakeES {
      url: string
      onerror: ((ev: Event) => void) | null = null
      onopen: ((ev: Event) => void) | null = null
      constructor(url: string) {
        this.url = url
      }
      addEventListener(type: string, listener: EventListener) {
        listeners.set(type, listener)
      }
      close() {}
    }

    const mem = new Map<string, string>()
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v)
      },
      removeItem: (k: string) => {
        mem.delete(k)
      },
    }

    const urls: string[] = []
    let current: FakeES | null = null
    const src = new TaskEventSource('task-x', {
      eventTypes: ['task_error'],
      storage,
      onStatus: (s) => statuses.push(s),
      eventSourceFactory: (url) => {
        urls.push(url)
        current = new FakeES(url)
        return current as unknown as EventSource
      },
      schedule: (fn, ms) => window.setTimeout(fn, ms) as unknown as number,
      clearSchedule: (id) => window.clearTimeout(id),
    })

    src.start()
    expect(statuses.at(-1)).toBe('connecting')
    current!.onopen?.(new Event('open'))
    expect(statuses.at(-1)).toBe('open')

    // deliver an event with id
    listeners.get('task_error')?.(
      new MessageEvent('task_error', {
        data: '{"code":"X"}',
        lastEventId: '42',
      }),
    )
    expect(loadLastEventId('task-x', storage)).toBe(42)

    current!.onerror?.(new Event('error'))
    expect(statuses.at(-1)).toBe('reconnecting')

    vi.advanceTimersByTime(1000)
    expect(urls.length).toBe(2)
    expect(urls[1]).toContain('after_event_id=42')

    src.stop()
    expect(statuses.at(-1)).toBe('closed')
    vi.useRealTimers()
  })

  it('simulateDisconnect schedules reconnect without waiting native error', () => {
    vi.useFakeTimers()
    const statuses: string[] = []
    class FakeES {
      addEventListener() {}
      close() {}
      onerror: ((ev: Event) => void) | null = null
      onopen: ((ev: Event) => void) | null = null
    }
    const src = new TaskEventSource('task-y', {
      onStatus: (s) => statuses.push(s),
      eventSourceFactory: () => new FakeES() as unknown as EventSource,
      schedule: (fn, ms) => window.setTimeout(fn, ms) as unknown as number,
      clearSchedule: (id) => window.clearTimeout(id),
      storage: {
        getItem: () => null,
        setItem: () => {},
        removeItem: () => {},
      },
    })
    src.start()
    src.simulateDisconnect()
    expect(statuses).toContain('reconnecting')
    src.stop()
    vi.useRealTimers()
  })
})
