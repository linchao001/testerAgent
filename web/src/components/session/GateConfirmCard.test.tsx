import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { GateConfirmCard } from './GateConfirmCard'

const confirmTask = vi.fn()

vi.mock('../../api/endpoints', () => ({
  confirmTask: (...args: unknown[]) => confirmTask(...args),
}))

describe('GateConfirmCard', () => {
  beforeEach(() => {
    confirmTask.mockReset()
    confirmTask.mockResolvedValue({
      task_id: 't1',
      status: 'running',
      artifact_id: 'a1',
      stage_version: 1,
    })
  })

  it('posts gate_kind plan_confirm on confirm', async () => {
    render(
      <GateConfirmCard
        taskId="t1"
        checkpoint={{
          stage: 'coverage_design',
          artifact_id: 'a1',
          stage_version: 1,
          gate_kind: 'plan_confirm',
        }}
      />,
    )
    fireEvent.click(screen.getByText('确认继续'))
    await waitFor(() => expect(confirmTask).toHaveBeenCalled())
    expect(confirmTask).toHaveBeenCalledWith('t1', {
      gate_kind: 'plan_confirm',
      artifact_id: 'a1',
      action: 'confirm',
      expected_version: 1,
      stage: 'coverage_design',
    })
  })
})
