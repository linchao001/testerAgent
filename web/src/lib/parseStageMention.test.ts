import { describe, expect, it } from 'vitest'
import { parseStageMention, stageLabel } from './parseStageMention'

describe('parseStageMention', () => {
  it('parses English and Chinese aliases', () => {
    expect(parseStageMention('请从 @link_identify 重跑')).toBe('link_identify')
    expect(parseStageMention('@链路 范围变了')).toBe('link_identify')
    expect(parseStageMention('@测试点 补充边界')).toBe('point_write')
    expect(parseStageMention('@point_write please')).toBe('point_write')
  })

  it('returns null without mention', () => {
    expect(parseStageMention('普通对话')).toBeNull()
  })

  it('stageLabel', () => {
    expect(stageLabel('link_identify')).toBe('链路识别')
    expect(stageLabel('point_write')).toBe('测试点编写')
  })
})
