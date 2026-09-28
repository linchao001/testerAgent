/**
 * F0 验收页（仅开发）：MSW 模拟错误码 + SSE 断线黄条。
 * 生产构建不挂路由（见 router.tsx）。
 */

import { useEffect, useRef, useState } from 'react'
import { apiRequest } from '../api/client'
import { TaskEventSource } from '../api/sse'
import { ApiError, NetworkError } from '../api/types'
import {
  clearLastError,
  reportError,
  setSseStatus,
} from '../stores/connection'

export function F0DevPage() {
  const [log, setLog] = useState<string[]>([])
  const sourceRef = useRef<TaskEventSource | null>(null)

  useEffect(() => {
    return () => {
      sourceRef.current?.stop()
      sourceRef.current = null
      setSseStatus('idle')
    }
  }, [])

  function push(line: string) {
    setLog((prev) => [line, ...prev].slice(0, 12))
  }

  async function triggerError(path: string) {
    clearLastError()
    try {
      await apiRequest(path)
      push(`${path} → unexpected ok`)
    } catch (err) {
      reportError(err as ApiError | NetworkError | Error)
      if (err instanceof ApiError) {
        push(`${path} → [${err.code}] ${err.displayMessage}`)
      } else if (err instanceof NetworkError) {
        push(`${path} → ${err.displayMessage}`)
      } else {
        push(`${path} → ${(err as Error).message}`)
      }
    }
  }

  function startSse() {
    sourceRef.current?.stop()
    const src = new TaskEventSource('task-f0', {
      onStatus: (s) => {
        setSseStatus(s)
        push(`sse status → ${s}`)
      },
      onEvent: (e) => push(`sse event → ${e.type}`),
    })
    sourceRef.current = src
    src.start()
  }

  function dropSse() {
    if (!sourceRef.current) {
      push('sse not started')
      return
    }
    sourceRef.current.simulateDisconnect()
    push('sse simulateDisconnect()')
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-stone-900">F0 验收台</h1>
        <p className="mt-1 text-sm text-stone-500">
          用 MSW 验证错误信封展示与 SSE 重连黄条（WBS WP-F0）。
        </p>
      </div>

      <section className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid="btn-400"
          className="rounded bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700"
          onClick={() => void triggerError('/api/v1/_dev/error-400')}
        >
          触发 400 VALIDATION_BODY
        </button>
        <button
          type="button"
          data-testid="btn-404"
          className="rounded bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700"
          onClick={() => void triggerError('/api/v1/_dev/error-404')}
        >
          触发 404 NOT_FOUND
        </button>
        <button
          type="button"
          data-testid="btn-500"
          className="rounded bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700"
          onClick={() => void triggerError('/api/v1/_dev/error-500')}
        >
          触发 500 INTERNAL
        </button>
        <button
          type="button"
          data-testid="btn-sse-start"
          className="rounded border border-stone-300 bg-white px-3 py-1.5 text-sm hover:bg-stone-50"
          onClick={startSse}
        >
          启动 SSE
        </button>
        <button
          type="button"
          data-testid="btn-sse-drop"
          className="rounded border border-amber-400 bg-amber-50 px-3 py-1.5 text-sm text-amber-950 hover:bg-amber-100"
          onClick={dropSse}
        >
          模拟 SSE 断线
        </button>
      </section>

      <ul className="space-y-1 rounded border border-stone-200 bg-white p-3 font-mono text-xs text-stone-700">
        {log.length === 0 ? (
          <li className="text-stone-400">操作日志空</li>
        ) : (
          log.map((line, i) => <li key={`${i}-${line}`}>{line}</li>)
        )}
      </ul>
    </div>
  )
}
