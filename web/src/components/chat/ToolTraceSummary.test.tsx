import { describe, expect, it } from 'vitest'
import { ToolTraceSummary } from './ToolTraceSummary'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

describe('ToolTraceSummary', () => {
  it('returns null when tool_trace empty or missing', () => {
    const { container } = render(<ToolTraceSummary payload={{}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows count and expands name/ok/latency', async () => {
    const user = userEvent.setup()
    render(
      <ToolTraceSummary
        payload={{
          tool_trace: [
            {
              tool: 'bash',
              ok: true,
              latency_ms: 40,
              args_digest: 'deadbeefdeadbeef',
            },
            {
              tool: 'str_replace_editor',
              ok: false,
              latency_ms: 12,
              error_code: 'ToolError',
            },
          ],
        }}
      />,
    )
    expect(screen.getByText('调用了 2 次工具')).toBeInTheDocument()
    await user.click(screen.getByText('调用了 2 次工具'))
    expect(screen.getByText(/bash/)).toBeInTheDocument()
    expect(screen.getByText(/str_replace_editor/)).toBeInTheDocument()
    expect(screen.getByText(/40 ms/)).toBeInTheDocument()
    expect(screen.queryByText(/deadbeef/)).not.toBeInTheDocument()
  })
})
