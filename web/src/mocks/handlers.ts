import { http, HttpResponse } from 'msw'
import { chatHandlers } from './chatHandlers'
import { confirmHandlers } from './confirmHandlers'
import { debugHandlers } from './debugHandlers'
import { settingsHandlers } from './settingsHandlers'
import { workbenchHandlers } from './workbenchHandlers'

function envelope(
  status: number,
  code: string,
  message: string,
  retryable = false,
  details: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    { error: { code, message, retryable, details } },
    { status },
  )
}

export const handlers = [
  http.get('/api/v1/_dev/error-400', () =>
    envelope(400, 'VALIDATION_BODY', 'body invalid', false, {
      field: 'name',
    }),
  ),
  http.get('/api/v1/_dev/error-404', () =>
    envelope(404, 'NOT_FOUND', 'missing', false),
  ),
  http.get('/api/v1/_dev/error-500', () =>
    envelope(500, 'INTERNAL', 'boom', false, { trace_id: 't-msw' }),
  ),
  http.get('/api/v1/_dev/page', () =>
    HttpResponse.json({
      items: [{ id: 'a' }, { id: 'b' }],
      next_cursor: 'cur-1',
    }),
  ),
  http.post('/api/v1/_dev/echo-headers', ({ request }) =>
    HttpResponse.json({
      idempotency_key: request.headers.get('Idempotency-Key'),
    }),
  ),
  // chat 优先：会话页依赖「已有工作区」引导；其后补确认/工作台/调试/设置
  ...chatHandlers,
  ...confirmHandlers,
  ...workbenchHandlers,
  ...debugHandlers,
  ...settingsHandlers,
]
