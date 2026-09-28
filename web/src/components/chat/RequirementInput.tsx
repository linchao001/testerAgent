/** 需求大文本框 + 拖拽 .md（dd §12.3 ChatPage）。 */

import { useRef, useState } from 'react'

type Props = {
  disabled?: boolean
  onSubmit: (md: string) => void | Promise<void>
}

export function RequirementInput({ disabled, onSubmit }: Props) {
  const [text, setText] = useState('')
  const [dragging, setDragging] = useState(false)
  const [pending, setPending] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  async function handleSubmit() {
    const md = text.trim()
    if (!md || pending || disabled) return
    setPending(true)
    try {
      await onSubmit(md)
    } finally {
      setPending(false)
    }
  }

  async function readFile(file: File) {
    const content = await file.text()
    setText(content)
  }

  return (
    <div className="space-y-3" data-testid="requirement-input">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-stone-800">需求文档（Markdown）</h2>
        <button
          type="button"
          className="text-xs text-stone-500 underline hover:text-stone-800"
          disabled={disabled || pending}
          onClick={() => inputRef.current?.click()}
        >
          导入 .md
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".md,text/markdown,text/plain"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) void readFile(f)
            e.target.value = ''
          }}
        />
      </div>
      <textarea
        data-testid="requirement-textarea"
        className={[
          'min-h-48 w-full resize-y rounded-md border bg-white px-3 py-2 text-sm',
          'text-stone-800 outline-none focus:border-stone-400',
          dragging ? 'border-teal-600 bg-teal-50/40' : 'border-stone-300',
        ].join(' ')}
        placeholder="粘贴需求 Markdown，或拖拽 .md 文件到此处…"
        value={text}
        disabled={disabled || pending}
        onChange={(e) => setText(e.target.value)}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          const f = e.dataTransfer.files?.[0]
          if (f) void readFile(f)
        }}
      />
      <div className="flex justify-end">
        <button
          type="button"
          data-testid="requirement-submit"
          disabled={disabled || pending || !text.trim()}
          className="rounded-md bg-teal-800 px-4 py-2 text-sm font-medium text-white hover:bg-teal-700 disabled:cursor-not-allowed disabled:opacity-50"
          onClick={() => void handleSubmit()}
        >
          {pending ? '启动中…' : '开始生成用例'}
        </button>
      </div>
    </div>
  )
}
