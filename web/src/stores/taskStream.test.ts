import { afterEach, describe, expect, it } from 'vitest'
import { useTaskStream } from './taskStream'

afterEach(() => {
  useTaskStream.getState().reset()
})

describe('taskStream applyEvent', () => {
  it('records checkpoint_waiting', () => {
    useTaskStream.getState().applyEvent({
      id: '1',
      type: 'checkpoint_waiting',
      data: {
        stage: 'link_identify',
        artifact_id: 'art-1',
        stage_version: 1,
      },
    })
    const s = useTaskStream.getState()
    expect(s.checkpoint).toEqual({
      stage: 'link_identify',
      artifact_id: 'art-1',
      stage_version: 1,
    })
    expect(s.phase).toBe('checkpoint:link_identify')
  })

  it('records clarification_needed', () => {
    useTaskStream.getState().applyEvent({
      id: '2',
      type: 'clarification_needed',
      data: {
        questions: [{ id: 'q-1', question: '角色是谁？' }],
      },
    })
    expect(useTaskStream.getState().clarification).toEqual([
      { id: 'q-1', question: '角色是谁？', options: undefined },
    ])
  })
})
