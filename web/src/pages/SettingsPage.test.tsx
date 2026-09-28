import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  getMockModelConfig,
  resetSettingsMockState,
  settingsHandlers,
} from '../mocks/settingsHandlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { SettingsPage } from './SettingsPage'

beforeEach(() => {
  resetSettingsMockState()
  clearLastError()
  setSseStatus('idle')
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...settingsHandlers)
})

afterEach(() => {
  server.resetHandlers()
  server.close()
  cleanup()
  clearLastError()
  setSseStatus('idle')
})

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/settings']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F5 SettingsPage', () => {
  it('保存模型配置后测试连接展示 latency 与 model', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('settings-page')

    await user.clear(screen.getByLabelText('Base URL'))
    await user.type(screen.getByLabelText('Base URL'), 'https://api.example.com/v1')
    await user.clear(screen.getByLabelText('API Key'))
    await user.type(screen.getByLabelText('API Key'), 'sk-test')
    await user.clear(screen.getByLabelText('模型'))
    await user.type(screen.getByLabelText('模型'), 'deepseek-chat')

    await user.click(screen.getByTestId('model-save-btn'))
    await waitFor(() => {
      expect(getMockModelConfig().model).toBe('deepseek-chat')
    })

    await user.click(screen.getByTestId('model-test-btn'))
    expect(await screen.findByTestId('model-test-latency')).toHaveTextContent(
      '88',
    )
    expect(screen.getByTestId('model-test-model')).toHaveTextContent(
      'deepseek-chat',
    )
  })
})
