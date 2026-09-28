import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  resetSettingsMockState,
  setDeleteBlocked,
  settingsHandlers,
} from '../mocks/settingsHandlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { useSession } from '../stores/session'
import { WorkspacesPage } from './WorkspacesPage'

beforeEach(() => {
  resetSettingsMockState()
  useSession.getState().reset()
  clearLastError()
  setSseStatus('idle')
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...settingsHandlers)
})

afterEach(() => {
  server.resetHandlers()
  server.close()
  cleanup()
  useSession.getState().reset()
  clearLastError()
  setSseStatus('idle')
})

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/workspaces']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/workspaces" element={<WorkspacesPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F5 WorkspacesPage', () => {
  it('选中工作区后测试连接展示只读能力位', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('workspaces-page')
    await user.click(await screen.findByTestId('ws-item-ws-f5'))

    await user.click(screen.getByTestId('kb-test-btn'))
    const caps = await screen.findByTestId('kb-capabilities')
    expect(within(caps).getByLabelText('metadata_filter')).toBeDisabled()
    expect(within(caps).getByLabelText('entry_version')).toBeDisabled()
    expect(within(caps).getByLabelText('passage_api')).toBeDisabled()
    expect(within(caps).getByLabelText('passage_api')).toBeChecked()
    expect(within(caps).getByLabelText('metadata_filter')).not.toBeChecked()
    expect(screen.getByTestId('kb-test-latency')).toHaveTextContent('42')
  })

  it('创建工作区并设为当前会话工作区；绑定智能体', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('workspaces-page')
    await user.click(screen.getByTestId('ws-new-btn'))

    await user.clear(screen.getByLabelText('名称'))
    await user.type(screen.getByLabelText('名称'), '新业务区')
    await user.clear(screen.getByLabelText('服务地址'))
    await user.type(screen.getByLabelText('服务地址'), 'http://127.0.0.1:9000')
    await user.clear(screen.getByLabelText('知识库 ID'))
    await user.type(screen.getByLabelText('知识库 ID'), 'demo_kb')
    await user.click(screen.getByTestId('ws-save-btn'))

    await waitFor(() => {
      expect(useSession.getState().workspaceId).toMatch(/^ws-new-/)
    })
    expect(await screen.findByText('新业务区')).toBeInTheDocument()

    const agentBox = await screen.findByTestId('agent-bind-builtin-case-designer')
    expect(agentBox).not.toBeChecked()
    await user.click(agentBox)
    await waitFor(() => {
      expect(screen.getByTestId('agent-bind-builtin-case-designer')).toBeChecked()
    })
  })

  it('删除有活跃任务的工作区时 ErrorBanner 展示 TASK_STATE_CONFLICT', async () => {
    const user = userEvent.setup()
    setDeleteBlocked('ws-f5', true)
    renderPage()
    await screen.findByTestId('workspaces-page')
    await user.click(await screen.findByTestId('ws-item-ws-f5'))
    await user.click(screen.getByTestId('ws-delete-btn'))
    const banner = await screen.findByTestId('error-banner')
    expect(banner).toHaveTextContent('TASK_STATE_CONFLICT')
  })
})
