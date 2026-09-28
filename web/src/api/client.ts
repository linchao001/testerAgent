/**
 * fetch 封装（dd §12.1 client.ts）：
 * - 解包 §10.1 错误信封
 * - 列表分页 {items, next_cursor}
 * - POST 可选自动注入 Idempotency-Key
 * - 网络失败 → NetworkError（固定中文文案）
 */

import { messageForCode } from './errors'
import {
  ApiError,
  NetworkError,
  type ErrorEnvelope,
  type Page,
} from './types'

export type RequestOptions = {
  method?: string
  body?: unknown
  headers?: Record<string, string>
  /** POST/PUT/PATCH 时注入；传 false 关闭；传字符串用指定键 */
  idempotencyKey?: string | false
  signal?: AbortSignal
}

function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function isErrorEnvelope(data: unknown): data is ErrorEnvelope {
  if (!data || typeof data !== 'object') return false
  const err = (data as ErrorEnvelope).error
  return (
    !!err &&
    typeof err === 'object' &&
    typeof err.code === 'string' &&
    typeof err.message === 'string'
  )
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...options.headers,
  }

  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const mutating = method === 'POST' || method === 'PUT' || method === 'PATCH'
  if (mutating && options.idempotencyKey !== false) {
    headers['Idempotency-Key'] =
      typeof options.idempotencyKey === 'string'
        ? options.idempotencyKey
        : newIdempotencyKey()
  }

  let resp: Response
  try {
    resp = await fetch(path, {
      method,
      headers,
      body,
      signal: options.signal,
    })
  } catch (cause) {
    throw new NetworkError(cause)
  }

  const text = await resp.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text) as unknown
    } catch {
      data = text
    }
  }

  if (!resp.ok) {
    if (isErrorEnvelope(data)) {
      const display = messageForCode(data.error.code, data.error.message)
      throw new ApiError(resp.status, data.error, display)
    }
    throw new ApiError(
      resp.status,
      {
        code: 'INTERNAL',
        message: typeof data === 'string' && data ? data : resp.statusText,
        retryable: resp.status >= 500,
      },
      typeof data === 'string' && data
        ? data
        : `HTTP ${resp.status}`,
    )
  }

  return data as T
}

export async function apiGetPage<T>(
  path: string,
  options: RequestOptions = {},
): Promise<Page<T>> {
  return apiRequest<Page<T>>(path, { ...options, method: 'GET' })
}
