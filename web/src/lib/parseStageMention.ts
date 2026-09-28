/** 从 change_request 文本解析 @阶段 回退目标（dd §12.3 ChatPage）。 */

import type { StageName } from '../api/domain'

// 中文后勿用 \b：JS 的 \w 不含汉字，边界恒不成立。
const ALIASES: Array<{ stage: StageName; patterns: RegExp[] }> = [
  {
    stage: 'link_identify',
    patterns: [
      /@\s*link[_-]?identify\b/i,
      /@\s*链路识别/,
      /@\s*链路/,
    ],
  },
  {
    stage: 'point_write',
    patterns: [/@\s*point[_-]?write\b/i, /@\s*测试点/],
  },
]

export function parseStageMention(text: string): StageName | null {
  for (const { stage, patterns } of ALIASES) {
    if (patterns.some((re) => re.test(text))) return stage
  }
  return null
}

export function stageLabel(stage: StageName | string): string {
  if (stage === 'link_identify') return '链路识别'
  if (stage === 'point_write') return '测试点编写'
  return stage
}
