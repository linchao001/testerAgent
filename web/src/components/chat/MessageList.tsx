/** 会话消息列表（用户 / 助手 / 系统）。 */

import type { Message } from '../../api/domain'
import { ToolTraceSummary } from './ToolTraceSummary'

type Props = {
  messages: Message[]
}

const KIND_HINT: Record<string, string> = {
  chat: '对话',
  change_request: '变更请求',
  clarification_qa: '澄清',
  checkpoint_revision: '检查点修订',
  regen_instruction: '重生成',
}

export function MessageList({ messages }: Props) {
  if (messages.length === 0) {
    return (
      <p className="text-sm text-stone-500" data-testid="message-list-empty">
        尚无消息。提交需求后将在此展示对话与澄清记录。
      </p>
    )
  }

  // API 默认倒序分页；展示时按时间正序
  const ordered = [...messages].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  )

  return (
    <ul className="space-y-3" data-testid="message-list">
      {ordered.map((m) => {
        const isUser = m.role === 'user'
        return (
          <li
            key={m.id}
            data-testid={`message-${m.id}`}
            className={[
              'rounded-md border px-3 py-2 text-sm',
              isUser
                ? 'ml-8 border-teal-200 bg-teal-50/50'
                : 'mr-8 border-stone-200 bg-white',
            ].join(' ')}
          >
            <div className="mb-1 flex items-center gap-2 text-xs text-stone-500">
              <span className="font-medium text-stone-700">{m.author}</span>
              <span>{KIND_HINT[m.kind] ?? m.kind}</span>
              <span className="ml-auto tabular-nums">
                {m.created_at.replace('T', ' ').slice(0, 19)}
              </span>
            </div>
            <div className="whitespace-pre-wrap text-stone-800">{m.content}</div>
            {!isUser ? <ToolTraceSummary payload={m.payload} /> : null}
          </li>
        )
      })}
    </ul>
  )
}
