import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ReviewProposalCard } from './ReviewProposalCard'

const confirmTask = vi.fn()
const getReviewProposal = vi.fn()

vi.mock('../../api/endpoints', () => ({
  confirmTask: (...args: unknown[]) => confirmTask(...args),
  getReviewProposal: (...args: unknown[]) => getReviewProposal(...args),
}))

const PROPOSAL = {
  scope: 'adoption',
  items: [
    {
      target_id: 'case-1',
      action: 'adopt' as const,
      rationale: '覆盖主路径',
      confidence: 0.9,
      patch: null,
    },
    {
      target_id: 'case-2',
      action: 'reject' as const,
      rationale: '重复',
      confidence: 0.7,
      patch: null,
    },
  ],
  matrix_ref: null,
  degraded: false,
}

describe('ReviewProposalCard', () => {
  beforeEach(() => {
    confirmTask.mockReset()
    getReviewProposal.mockReset()
    getReviewProposal.mockResolvedValue(PROPOSAL)
    confirmTask.mockResolvedValue({
      task_id: 't1',
      status: 'running',
      artifact_id: 'art-rev',
      stage_version: 1,
    })
  })

  it('loads proposal and confirms with gate_kind review_decision', async () => {
    render(
      <ReviewProposalCard
        taskId="t1"
        checkpoint={{
          stage: 'review_adoption',
          artifact_id: 'art-rev',
          stage_version: 1,
          gate_kind: 'review_decision',
          step_id: 's7',
        }}
      />,
    )

    await waitFor(() =>
      expect(screen.getByTestId('review-proposal-card')).toBeInTheDocument(),
    )
    expect(getReviewProposal).toHaveBeenCalledWith('t1', 'art-rev')
    expect(screen.getByText(/case-1/)).toBeInTheDocument()
    expect(screen.getByText(/覆盖主路径/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '采纳提案' }))
    await waitFor(() => expect(confirmTask).toHaveBeenCalled())
    expect(confirmTask).toHaveBeenCalledWith('t1', {
      gate_kind: 'review_decision',
      artifact_id: 'art-rev',
      action: 'confirm',
      expected_version: 1,
      stage: 'review_adoption',
    })
  })

  it('posts modify when item action is changed', async () => {
    render(
      <ReviewProposalCard
        taskId="t1"
        checkpoint={{
          stage: 'review_adoption',
          artifact_id: 'art-rev',
          stage_version: 1,
          gate_kind: 'review_decision',
        }}
      />,
    )
    await waitFor(() =>
      expect(screen.getByTestId('review-item-case-1')).toBeInTheDocument(),
    )

    fireEvent.change(screen.getByTestId('review-action-case-1'), {
      target: { value: 'reject' },
    })
    fireEvent.click(screen.getByRole('button', { name: '采纳提案' }))

    await waitFor(() => expect(confirmTask).toHaveBeenCalled())
    const [, body] = confirmTask.mock.calls[0]
    expect(body.action).toBe('modify')
    expect(body.gate_kind).toBe('review_decision')
    expect(body.payload.items[0].action).toBe('reject')
    expect(body.payload.items[1].action).toBe('reject')
  })

  it('posts reject_rerun', async () => {
    render(
      <ReviewProposalCard
        taskId="t1"
        checkpoint={{
          stage: 'review_quality',
          artifact_id: 'art-rev',
          stage_version: 2,
          gate_kind: 'review_decision',
        }}
      />,
    )
    await waitFor(() =>
      expect(screen.getByTestId('review-proposal-card')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: '驳回重评' }))
    await waitFor(() => expect(confirmTask).toHaveBeenCalled())
    expect(confirmTask).toHaveBeenCalledWith('t1', {
      gate_kind: 'review_decision',
      artifact_id: 'art-rev',
      action: 'reject_rerun',
      expected_version: 2,
      stage: 'review_quality',
    })
  })
})
