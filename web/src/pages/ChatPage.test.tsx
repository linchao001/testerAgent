import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import {
  chatHandlers,
  mockArriveCheckpoint,
  resetChatMockState,
} from '../mocks/chatHandlers'
import { handlers as f0Handlers } from '../mocks/handlers'
import { server } from '../mocks/server'
import { clearLastError, setSseStatus } from '../stores/connection'
import { useSession } from '../stores/session'
import { useTaskStream } from '../stores/taskStream'
import { ScriptedEventSource } from '../test/scriptedEventSource'
import { ChatPage } from './ChatPage'

beforeEach(() => {
  resetChatMockState()
  ScriptedEventSource.reset()
  useSession.getState().reset()
  useTaskStream.getState().reset()
  clearLastError()
  setSseStatus('idle')
  server.listen({ onUnhandledRequest: 'error' })
  server.use(...f0Handlers, ...chatHandlers)
  useTaskStream
    .getState()
    .setEventSourceFactory(
      (url) => new ScriptedEventSource(url) as unknown as EventSource,
    )
})

afterEach(() => {
  server.resetHandlers()
  server.close()
  cleanup()
  useTaskStream.getState().reset()
  useSession.getState().reset()
  ScriptedEventSource.reset()
  clearLastError()
  setSseStatus('idle')
})

function renderChat() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<ChatPage />} />
          <Route path="/confirm" element={<div>ConfirmPage</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WP-F1 ChatPage', () => {
  it('脚本事件驱动到 checkpoint_waiting 并展示确认入口', async () => {
    const user = userEvent.setup()
    ScriptedEventSource.script = [
      {
        type: 'node_start',
        data: { node: 'intake', stage_version: 1, batch_id: null },
        id: '1',
      },
      {
        type: 'node_end',
        data: { node: 'intake', latency_ms: 10 },
        id: '2',
      },
      {
        type: 'node_start',
        data: { node: 'link_identify', stage_version: 1, batch_id: null },
        id: '3',
      },
      {
        type: 'checkpoint_waiting',
        data: {
          stage: 'link_identify',
          artifact_id: 'art-link-1',
          stage_version: 1,
        },
        id: '4',
      },
    ]

    renderChat()
    await screen.findByTestId('requirement-input')

    await user.type(
      screen.getByTestId('requirement-textarea'),
      '## 需求\n用户可登录',
    )
    await user.click(screen.getByTestId('requirement-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('checkpoint-banner')).toHaveTextContent(
        'link_identify',
      )
    })
    expect(screen.getByTestId('stream-phase')).toHaveTextContent(
      'checkpoint:link_identify',
    )
    expect(screen.getByTestId('gate-confirm-card')).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /打开完整确认页/ }),
    ).toHaveAttribute('href', '/confirm')
  })

  it('澄清卡提交答复调用 answer', async () => {
    const user = userEvent.setup()
    ScriptedEventSource.script = [
      {
        type: 'clarification_needed',
        data: {
          questions: [{ id: 'q-1', question: '入口角色是谁？' }],
        },
        id: '10',
      },
    ]

    renderChat()
    await screen.findByTestId('requirement-input')
    await user.type(screen.getByTestId('requirement-textarea'), '## 模糊需求')
    await user.click(screen.getByTestId('requirement-submit'))

    await screen.findByTestId('clarification-card')
    await user.type(screen.getByTestId('clarify-input-q-1'), '管理员')
    await user.click(screen.getByTestId('clarification-submit'))

    await waitFor(() => {
      expect(screen.queryByTestId('clarification-card')).toBeNull()
    })
  })

  it('change_request @链路 二次确认后 rollback', async () => {
    const user = userEvent.setup()
    ScriptedEventSource.script = [
      {
        type: 'checkpoint_waiting',
        data: {
          stage: 'link_identify',
          artifact_id: 'art-link-1',
          stage_version: 1,
        },
        id: '20',
      },
    ]

    renderChat()
    await screen.findByTestId('requirement-input')
    await user.type(screen.getByTestId('requirement-textarea'), '## 需求 A')
    await user.click(screen.getByTestId('requirement-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('checkpoint-banner')).toBeInTheDocument()
    })
    // 模拟后端在 CP1 落 artifact（SSE 只带摘要，详情靠 GET）
    mockArriveCheckpoint()

    await user.type(
      screen.getByTestId('composer-input'),
      '@链路 需求范围调整，请重跑',
    )
    await user.click(screen.getByTestId('composer-send'))

    expect(screen.getByTestId('impact-preview')).toHaveTextContent('链路识别')
    await user.click(screen.getByTestId('impact-confirm'))

    await waitFor(() => {
      expect(screen.getByTestId('impact-result')).toHaveTextContent(
        '影响 1 条测试点',
      )
    })
  })

  it('human_gate_waiting review_decision 展示 ReviewProposalCard', async () => {
    const user = userEvent.setup()
    ScriptedEventSource.script = [
      {
        type: 'human_gate_waiting',
        data: {
          stage: 'review_adoption',
          artifact_id: 'art-rev-1',
          stage_version: 1,
          gate_kind: 'review_decision',
          step_id: 's7',
        },
        id: '30',
      },
    ]

    renderChat()
    await screen.findByTestId('requirement-input')
    await user.type(screen.getByTestId('requirement-textarea'), '## 需求 B')
    await user.click(screen.getByTestId('requirement-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('review-proposal-card')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('gate-confirm-card')).toBeNull()
    await waitFor(() => {
      expect(screen.getByText(/覆盖主路径/)).toBeInTheDocument()
    })
  })
})
