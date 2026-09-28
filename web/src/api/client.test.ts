import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { apiGetPage, apiRequest } from '../api/client'
import { ApiError, NetworkError } from '../api/types'
import { server } from '../mocks/server'

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('apiRequest', () => {
  it('maps VALIDATION_BODY envelope to Chinese ApiError', async () => {
    await expect(apiRequest('/api/v1/_dev/error-400')).rejects.toMatchObject({
      name: 'ApiError',
      code: 'VALIDATION_BODY',
      status: 400,
      displayMessage: '请求参数无效',
    } satisfies Partial<ApiError>)
  })

  it('maps NOT_FOUND', async () => {
    try {
      await apiRequest('/api/v1/_dev/error-404')
      expect.unreachable()
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).code).toBe('NOT_FOUND')
      expect((err as ApiError).displayMessage).toBe('资源不存在')
    }
  })

  it('maps INTERNAL 500', async () => {
    await expect(apiRequest('/api/v1/_dev/error-500')).rejects.toMatchObject({
      code: 'INTERNAL',
      status: 500,
      displayMessage: '服务内部错误',
    })
  })

  it('parses page envelope', async () => {
    const page = await apiGetPage<{ id: string }>('/api/v1/_dev/page')
    expect(page.items).toEqual([{ id: 'a' }, { id: 'b' }])
    expect(page.next_cursor).toBe('cur-1')
  })

  it('injects Idempotency-Key on POST by default', async () => {
    const out = await apiRequest<{ idempotency_key: string | null }>(
      '/api/v1/_dev/echo-headers',
      { method: 'POST', body: {} },
    )
    expect(out.idempotency_key).toBeTruthy()
    expect(out.idempotency_key!.length).toBeGreaterThan(8)
  })

  it('allows explicit Idempotency-Key', async () => {
    const out = await apiRequest<{ idempotency_key: string | null }>(
      '/api/v1/_dev/echo-headers',
      { method: 'POST', body: {}, idempotencyKey: 'fixed-key-1' },
    )
    expect(out.idempotency_key).toBe('fixed-key-1')
  })

  it('wraps fetch failure as NetworkError', async () => {
    const orig = globalThis.fetch
    globalThis.fetch = () => Promise.reject(new TypeError('offline'))
    try {
      await expect(apiRequest('/any')).rejects.toBeInstanceOf(NetworkError)
    } finally {
      globalThis.fetch = orig
    }
  })
})
