/** 会话消息气泡列表（用户右 / 助手左）。 */

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
      <div
        className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-16 text-center"
        data-testid="message-list-empty"
      >
        <p className="text-base font-medium text-stone-700">开始对话</p>
        <p className="max-w-sm text-sm text-stone-500">
          在对话框中说明需求或附加 .md 文件；准备好后告诉助手开始生成用例。
        </p>
      </div>
    )
  }

  // API 默认倒序分页；展示时按时间正序
  const ordered = [...messages].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  )

  return (
    <ul className="flex flex-col gap-4 px-1 py-4" data-testid="message-list">
      {ordered.map((m) => {
        const isUser = m.role === 'user'
        const isSystem = m.role === 'system'
        return (
          <li
            key={m.id}
            data-testid={`message-${m.id}`}
            className={[
              'flex w-full',
              isUser ? 'justify-end' : 'justify-start',
            ].join(' ')}
          >
            <div
              className={[
                'max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm shadow-sm',
                isUser
                  ? 'rounded-br-md bg-teal-700 text-white'
                  : isSystem
                    ? 'rounded-bl-md border border-amber-200 bg-amber-50 text-stone-800'
                    : 'rounded-bl-md border border-stone-200 bg-white text-stone-800',
              ].join(' ')}
            >
              <div
                className={[
                  'mb-1 flex items-center gap-2 text-[11px]',
                  isUser ? 'text-teal-100/90' : 'text-stone-500',
                ].join(' ')}
              >
                <span className={isUser ? 'font-medium text-white' : 'font-medium text-stone-700'}>
                  {m.author}
                </span>
                <span>{KIND_HINT[m.kind] ?? m.kind}</span>
                <span className="ml-auto tabular-nums opacity-80">
                  {m.created_at.replace('T', ' ').slice(0, 19)}
                </span>
              </div>
              <div className="whitespace-pre-wrap leading-relaxed">{m.content}</div>
              {!isUser ? <ToolTraceSummary payload={m.payload} /> : null}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
