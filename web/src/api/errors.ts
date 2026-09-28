/** 错误码 → 中文文案（dd §12.4）；未知 code 回退 message 原文。 */

const CODE_ZH: Record<string, string> = {
  VALIDATION_BODY: '请求参数无效',
  VALIDATION_ARTIFACT_REVISION: '阶段产物修订不合法',
  VALIDATION_REVIEW_TRANSITION: '评审状态转换不合法',
  NOT_FOUND: '资源不存在',
  TASK_STATE_CONFLICT: '任务状态冲突',
  VERSION_CONFLICT: '版本冲突，请刷新后重试',
  FILE_CONFLICT: '文件内容与预期不一致',
  KB_TOKEN_INVALID: '知识库确认令牌无效',
  PROPOSAL_EXPIRED: '提案已过期',
  TASK_BUSY: '任务正在执行',
  LLM_UPSTREAM: '模型服务异常',
  LLM_TIMEOUT: '模型调用超时',
  RATE_LIMITED: '模型调用被限流',
  LLM_BAD_REQUEST: '模型请求参数错误',
  LLM_BAD_OUTPUT: '模型输出无法解析',
  KB_UNREACHABLE: '知识库不可达',
  INTERNAL: '服务内部错误',
}

export function messageForCode(code: string, fallback: string): string {
  return CODE_ZH[code] ?? fallback
}
