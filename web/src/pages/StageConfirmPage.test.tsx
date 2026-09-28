import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  confirmHandlers,
  getLastConfirmBody,
  resetConfirmMockState,
  setConfirmForceConflict,
} from '../mocks/confirmHandlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { useSession } from '../stores/session'
import { useTaskStream } from '../stores/taskStream'
import { StageConfirmPage } from './StageConfirmPage'

beforeEach(() => {
  resetConfirmMockState()
  useSession.getState().reset()
  useTaskStream.getState().reset()
  clearLastError()
  setSseStatus('idle')
  useSession.getState().setWorkspaceId('ws-f2')
  useSession.getState().setConversationId('conv-f2')
  useSession.getState().setTaskId('task-f2')
  useTaskStream.setState({
    checkpoint: {
      stage: 'link_identify',
      artifact_id: 'art-link-1',
      stage_version: 1,
    },
  })
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...confirmHandlers)
})

afterEach(() => {
  server.resetHandlers()
  server.close()
  cleanup()
  useSession.getState().reset()
  useTaskStream.getState().reset()
  clearLastError()
  setSseStatus('idle')
})

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/confirm']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/confirm" element={<StageConfirmPage />} />
          <Route path="/" element={<div>Chat</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F2 StageConfirmPage', () => {
  it('无脏改放行走 confirm 且带 expected_version', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('stage-confirm-page')
    await screen.findByText('下单链路')

    await user.click(screen.getByTestId('confirm-submit'))

    await waitFor(() => {
      const body = getLastConfirmBody()
      expect(body).toMatchObject({
        stage: 'link_identify',
        artifact_id: 'art-link-1',
        expected_version: 1,
        action: 'confirm',
      })
    })
  })

  it('脏改后放行走 modify 且带全量 payload 与 expected_version', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('下单链路')

    await user.click(screen.getByTestId('list-item-L1'))
    const title = await screen.findByTestId('edit-title')
    await user.clear(title)
    await user.type(title, '下单链路-修订')

    await user.click(screen.getByTestId('confirm-submit'))

    await waitFor(() => {
      const body = getLastConfirmBody()
      expect(body).toMatchObject({
        action: 'modify',
        expected_version: 1,
        artifact_id: 'art-link-1',
        stage: 'link_identify',
      })
      const payload = body!.payload as {
        links: Array<{ link_id: string; title: string }>
      }
      expect(payload.links.find((l) => l.link_id === 'L1')?.title).toBe(
        '下单链路-修订',
      )
    })
  })

  it('VERSION_CONFLICT 时提示并刷新产物', async () => {
    const user = userEvent.setup()
    setConfirmForceConflict(true)
    renderPage()
    await screen.findByText('下单链路')

    await user.click(screen.getByTestId('confirm-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('version-conflict')).toHaveTextContent(
        '版本已变更',
      )
    })
    await waitFor(() => {
      expect(screen.getByText('他页已改标题')).toBeInTheDocument()
    })
  })
})
