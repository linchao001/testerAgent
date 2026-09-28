import { useSyncExternalStore } from 'react'
import {
  clearLastError,
  getConnection,
  subscribeConnection,
} from '../../stores/connection'

export function ErrorBanner() {
  const { lastError } = useSyncExternalStore(
    subscribeConnection,
    getConnection,
    getConnection,
  )
  if (!lastError) return null

  return (
    <div
      role="alert"
      data-testid="error-banner"
      className="flex items-start justify-between gap-3 border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-900"
    >
      <div>
        <span className="font-semibold">[{lastError.code}]</span>{' '}
        {lastError.message}
        {lastError.retryable ? (
          <span className="ml-2 text-red-700/80">（可重试）</span>
        ) : null}
      </div>
      <button
        type="button"
        className="shrink-0 rounded px-2 py-0.5 text-red-800 hover:bg-red-100"
        onClick={() => clearLastError()}
      >
        关闭
      </button>
    </div>
  )
}
