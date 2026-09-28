import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  getLastCaseUpdate,
  getLastReviewBody,
  resetWorkbenchMockState,
  setWorkbenchForceConflict,
  workbenchHandlers,
} from '../mocks/workbenchHandlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { useSession } from '../stores/session'
import { WorkbenchPage } from './WorkbenchPage'

beforeEach(() => {
  resetWorkbenchMockState()
  useSession.getState().reset()
  clearLastError()
  setSseStatus('idle')
  useSession.getState().setWorkspaceId('ws-f3')
  useSession.getState().setConversationId('conv-f3')
  useSession.getState().setTaskId('task-f3')
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...workbenchHandlers)
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
    <MemoryRouter initialEntries={['/workbench']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/workbench" element={<WorkbenchPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F3 WorkbenchPage', () => {
  it('保存带 If-Match 且成功后刷新为 edited_adopted', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('workbench-page')
    await screen.findByText('正常提交订单')

    await user.click(screen.getByTestId('case-item-case-1'))
    await screen.findByTestId('md-viewer')

    await user.click(screen.getByTestId('open-editor'))
    const editor = await screen.findByTestId('md-editor')
    fireEvent.change(editor, {
      target: {
        value:
          '---\ncase_id: case-1\npoint_id: pt-1-1\nstage_version: 1\npriority: P0\ncontent_hash: hash-v1\ntrace_refs: {}\n---\n\n# 修订后的标题\n\n## 步骤\n1. 新步骤\n   - 预期：ok\n',
      },
    })
    await user.click(screen.getByTestId('save-case'))

    await waitFor(() => {
      const upd = getLastCaseUpdate()
      expect(upd).toMatchObject({
        caseId: 'case-1',
        ifMatch: 'hash-v1',
      })
      expect(upd!.markdown).toContain('修订后的标题')
    })
    await waitFor(() => {
      expect(screen.getByTestId('case-review-case-1')).toHaveTextContent(
        'edited_adopted',
      )
    })
  })

  it('If-Match VERSION_CONFLICT 时提示 expected/current 并刷新正文', async () => {
    const user = userEvent.setup()
    setWorkbenchForceConflict(true)
    renderPage()
    await screen.findByText('正常提交订单')
    await user.click(screen.getByTestId('case-item-case-1'))
    await screen.findByTestId('md-viewer')
    await user.click(screen.getByTestId('open-editor'))
    const editor = await screen.findByTestId('md-editor')
    await user.type(editor, '\n追加一行')
    await user.click(screen.getByTestId('save-case'))

    await waitFor(() => {
      expect(screen.getByTestId('version-conflict')).toHaveTextContent(
        '内容已变更',
      )
      expect(screen.getByTestId('version-conflict')).toHaveTextContent(
        'hash-server',
      )
    })
    await waitFor(() => {
      expect(screen.getByTestId('md-viewer')).toHaveTextContent('他页已改标题')
    })
  })

  it('批量采纳后采纳率摘要刷新（仅 active）', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('adoption-summary')
    expect(screen.getByTestId('adoption-summary')).toHaveTextContent('0/4')

    await user.click(screen.getByTestId('case-check-case-1'))
    await user.click(screen.getByTestId('case-check-case-2'))
    await user.click(screen.getByTestId('review-adopt'))

    await waitFor(() => {
      const body = getLastReviewBody()
      expect(body?.items).toEqual([
        { case_id: 'case-1', action: 'adopt' },
        { case_id: 'case-2', action: 'adopt' },
      ])
    })
    await waitFor(() => {
      expect(screen.getByTestId('adoption-summary')).toHaveTextContent('2/4')
    })
  })
})
