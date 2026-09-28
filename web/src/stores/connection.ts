/** 连接态（SSE 黄条）与最近一次 API 错误（ErrorBanner）。 */

import type { SseStatus } from '../api/sse'
import type { ApiError, NetworkError } from '../api/types'

export type UiError = {
  code: string
  message: string
  retryable: boolean
}

type ConnectionState = {
  sseStatus: SseStatus
  lastError: UiError | null
}

let state: ConnectionState = {
  sseStatus: 'idle',
  lastError: null,
}

const listeners = new Set<() => void>()

function emit(): void {
  listeners.forEach((l) => l())
}

export function getConnection(): ConnectionState {
  return state
}

export function setSseStatus(sseStatus: SseStatus): void {
  state = { ...state, sseStatus }
  emit()
}

export function setLastError(err: UiError | null): void {
  state = { ...state, lastError: err }
  emit()
}

export function reportError(err: ApiError | NetworkError | Error): void {
  if ('code' in err && typeof (err as ApiError).code === 'string') {
    const api = err as ApiError
    setLastError({
      code: api.code,
      message: api.displayMessage,
      retryable: api.retryable,
    })
    return
  }
  if (err.name === 'NetworkError') {
    setLastError({
      code: 'NETWORK',
      message: (err as NetworkError).displayMessage,
      retryable: true,
    })
    return
  }
  setLastError({
    code: 'INTERNAL',
    message: err.message || '未知错误',
    retryable: false,
  })
}

export function clearLastError(): void {
  setLastError(null)
}

export function subscribeConnection(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
