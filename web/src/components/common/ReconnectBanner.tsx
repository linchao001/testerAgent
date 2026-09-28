import { useSyncExternalStore } from 'react'
import {
  getConnection,
  subscribeConnection,
} from '../../stores/connection'

/** SSE 断线重连黄条（dd §12.4）。 */
export function ReconnectBanner() {
  const { sseStatus } = useSyncExternalStore(
    subscribeConnection,
    getConnection,
    getConnection,
  )
  if (sseStatus !== 'reconnecting' && sseStatus !== 'connecting') {
    return null
  }

  const text =
    sseStatus === 'connecting'
      ? '正在连接事件流…'
      : '连接中断，重连中…'

  return (
    <div
      role="status"
      data-testid="reconnect-banner"
      className="border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-950"
    >
      {text}
    </div>
  )
}
