/** 底部固定聊天输入（附件 + 发送）。 */

import { useRef } from 'react'

export type PendingAttachment = {
  name: string
  content: string
}

type Props = {
  value: string
  attachments: PendingAttachment[]
  disabled?: boolean
  onChange: (value: string) => void
  onAttachmentsChange: (files: PendingAttachment[]) => void
  onSend: () => void
}

export function ChatComposer({
  value,
  attachments,
  disabled,
  onChange,
  onAttachmentsChange,
  onSend,
}: Props) {
  const fileRef = useRef<HTMLInputElement>(null)

  async function addFiles(fileList: FileList | null) {
    if (!fileList?.length) return
    const next = [...attachments]
    for (const file of Array.from(fileList)) {
      const name = file.name
      const lower = name.toLowerCase()
      if (
        !lower.endsWith('.md') &&
        !lower.endsWith('.txt') &&
        !lower.endsWith('.markdown')
      ) {
        continue
      }
      const content = await file.text()
      next.push({ name, content })
    }
    onAttachmentsChange(next)
    if (fileRef.current) fileRef.current.value = ''
  }

  return (
    <section
      className="shrink-0 border-t border-stone-200 bg-stone-50/90 px-1 pb-3 pt-2 backdrop-blur"
      data-testid="composer"
    >
      <div className="rounded-2xl border border-stone-200 bg-white px-3 py-2 shadow-sm">
        {attachments.length > 0 ? (
          <ul
            className="mb-2 flex flex-wrap gap-2"
            data-testid="composer-attachments"
          >
            {attachments.map((a, i) => (
              <li
                key={`${a.name}-${i}`}
                className="flex items-center gap-1 rounded-full border border-stone-200 bg-stone-50 px-2 py-0.5 text-xs text-stone-700"
              >
                <span className="max-w-[10rem] truncate">{a.name}</span>
                <button
                  type="button"
                  className="text-stone-400 hover:text-stone-700"
                  aria-label={`移除 ${a.name}`}
                  disabled={disabled}
                  onClick={() =>
                    onAttachmentsChange(attachments.filter((_, j) => j !== i))
                  }
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <textarea
          id="chat-composer"
          data-testid="composer-input"
          className="max-h-40 min-h-[4.5rem] w-full resize-none border-0 bg-transparent px-1 py-1 text-sm text-stone-800 outline-none placeholder:text-stone-400"
          value={value}
          disabled={disabled}
          rows={3}
          placeholder="输入消息或附加 .md 需求文件…（有任务时可用 @链路 / @测试点）"
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
          }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault()
            void addFiles(e.dataTransfer.files)
          }}
        />
        <div className="mt-1 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              data-testid="composer-attach"
              disabled={disabled}
              className="rounded-lg border border-stone-200 px-2 py-1 text-xs text-stone-600 hover:bg-stone-50 disabled:opacity-50"
              onClick={() => fileRef.current?.click()}
            >
              附件
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".md,.txt,.markdown,text/markdown,text/plain"
              multiple
              className="hidden"
              onChange={(e) => void addFiles(e.target.files)}
            />
            <p className="text-[11px] text-stone-400">
              Enter 发送 · Shift+Enter 换行
            </p>
          </div>
          <button
            type="button"
            data-testid="composer-send"
            disabled={disabled || (!value.trim() && attachments.length === 0)}
            className="rounded-lg bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700 disabled:opacity-50"
            onClick={onSend}
          >
            发送
          </button>
        </div>
      </div>
    </section>
  )
}

/** 把附件拼进用户消息正文，供助手调用 start_case_generation。 */
export function composeMessageWithAttachments(
  text: string,
  attachments: PendingAttachment[],
): string {
  const body = text.trim()
  if (attachments.length === 0) return body
  const blocks = attachments.map(
    (a) =>
      `附件 \`${a.name}\`：\n\`\`\`markdown\n${a.content.trim()}\n\`\`\``,
  )
  return [body, ...blocks].filter(Boolean).join('\n\n')
}
