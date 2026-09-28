/** API 错误与分页公共类型（dd §10.1）。 */

export type ErrorBody = {
  code: string
  message: string
  retryable: boolean
  details?: Record<string, unknown>
}

export type ErrorEnvelope = {
  error: ErrorBody
}

export type Page<T> = {
  items: T[]
  next_cursor: string | null
}

export class ApiError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly details: Record<string, unknown>
  readonly status: number
  readonly displayMessage: string

  constructor(
    status: number,
    body: ErrorBody,
    displayMessage: string,
  ) {
    super(displayMessage)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.retryable = body.retryable
    this.details = body.details ?? {}
    this.displayMessage = displayMessage
  }
}

export class NetworkError extends Error {
  readonly displayMessage =
    '服务不可用，检查后端进程'

  constructor(cause?: unknown) {
    super('服务不可用，检查后端进程')
    this.name = 'NetworkError'
    if (cause instanceof Error) {
      this.cause = cause
    }
  }
}
