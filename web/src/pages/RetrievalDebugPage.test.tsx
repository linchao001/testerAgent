import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  debugHandlers,
  getLastPlaygroundBody,
  resetDebugMockState,
} from '../mocks/debugHandlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { useSession } from '../stores/session'
import { RetrievalDebugPage } from './RetrievalDebugPage'

beforeEach(() => {
  resetDebugMockState()
  useSession.getState().reset()
  clearLastError()
  setSseStatus('idle')
  useSession.getState().setWorkspaceId('ws-f4')
  useSession.getState().setTaskId('task-f4')
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...debugHandlers)
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
    <MemoryRouter initialEntries={['/debug']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/debug" element={<RetrievalDebugPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F4 RetrievalDebugPage', () => {
  it('选中 trace 后漏斗四段计数与 candidates drop_reason 可见', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('retrieval-debug-page')
    await screen.findByTestId('trace-item-trace-1')
    await user.click(screen.getByTestId('trace-item-trace-1'))

    const funnel = await screen.findByTestId('funnel-chart')
    expect(within(funnel).getByTestId('funnel-recall')).toHaveTextContent('4')
    expect(within(funnel).getByTestId('funnel-filter')).toHaveTextContent('3')
    expect(within(funnel).getByTestId('funnel-rerank')).toHaveTextContent('2')
    expect(within(funnel).getByTestId('funnel-inject')).toHaveTextContent('2')

    await user.click(screen.getByTestId('funnel-expand'))
    expect(await screen.findByText('无关文档')).toBeInTheDocument()
    expect(screen.getAllByText('filtered_type').length).toBeGreaterThan(0)
  })

  it('full 快照点 item 弹窗拉到正文；meta 档禁用并黄标', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByTestId('snap-item-snap-full')
    await user.click(screen.getByTestId('snap-item-snap-full'))
    await screen.findByTestId('snapshot-detail')
    await user.click(screen.getByTestId('snap-pos-0'))
    expect(await screen.findByTestId('snapshot-item-dialog')).toHaveTextContent(
      'FULL_PASSAGE_下单主流程正文',
    )
    await user.click(screen.getByTestId('close-snapshot-dialog'))

    await user.click(screen.getByTestId('snap-item-snap-meta'))
    const meta = await screen.findByTestId('snapshot-detail')
    expect(within(meta).getByTestId('snapshot-level-badge')).toHaveTextContent(
      'meta',
    )
    expect(within(meta).getByTestId('snap-pos-0')).toBeDisabled()
  })

  it('degraded 非空显示黄标', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByTestId('trace-item-trace-1'))
    expect(await screen.findByTestId('degraded-banner')).toHaveTextContent(
      'meta_filter',
    )
  })

  it('Playground 提交后展示 funnel', async () => {
    const user = userEvent.setup()
    renderPage()
    const input = await screen.findByTestId('playground-query')
    await user.clear(input)
    await user.type(input, '退款超时')
    await user.selectOptions(screen.getByTestId('playground-stage'), 'link_identify')
    await user.click(screen.getByTestId('playground-run'))

    await waitFor(() => {
      expect(getLastPlaygroundBody()).toEqual({
        query: '退款超时',
        stage: 'link_identify',
      })
    })
    const pg = await screen.findByTestId('playground-funnel')
    expect(pg).toHaveTextContent('kept')
    expect(pg).toHaveTextContent('1')
    expect(await screen.findByTestId('playground-degraded')).toBeInTheDocument()
  })
})
