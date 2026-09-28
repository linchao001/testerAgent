import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import { F0DevPage } from '../pages/F0DevPage'
import { clearLastError, setSseStatus } from '../stores/connection'
import { server } from '../mocks/server'

beforeEach(() => {
  server.listen({ onUnhandledRequest: 'error' })
  clearLastError()
  setSseStatus('idle')
})

afterEach(() => {
  server.resetHandlers()
  server.close()
  cleanup()
  clearLastError()
  setSseStatus('idle')
})

function renderF0() {
  return render(
    <MemoryRouter initialEntries={['/_dev/f0']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/_dev/f0" element={<F0DevPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('F0 acceptance UI', () => {
  it('shows error code in banner after 400', async () => {
    const user = userEvent.setup()
    renderF0()
    await user.click(screen.getByTestId('btn-400'))
    const banner = await screen.findByTestId('error-banner')
    expect(banner).toHaveTextContent('[VALIDATION_BODY]')
    expect(banner).toHaveTextContent('请求参数无效')
  })

  it('shows reconnect yellow bar when SSE reconnecting', () => {
    setSseStatus('reconnecting')
    renderF0()
    expect(screen.getByTestId('reconnect-banner')).toHaveTextContent(
      '连接中断，重连中…',
    )
  })
})
