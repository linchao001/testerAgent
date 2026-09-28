/** 澄清问题内联回答卡（dd §12.3）。 */

import { useState } from 'react'
import type { ClarificationQuestion } from '../../api/domain'

type Props = {
  questions: ClarificationQuestion[]
  disabled?: boolean
  onSubmit: (
    answers: Array<{ question_id: string; answer: string }>,
  ) => void | Promise<void>
}

export function ClarificationCard({ questions, disabled, onSubmit }: Props) {
  const [answers, setAnswers] = useState<Record<string, string>>(() =>
    Object.fromEntries(questions.map((q) => [q.id, ''])),
  )
  const [pending, setPending] = useState(false)

  const ready = questions.every((q) => (answers[q.id] ?? '').trim())

  async function handleSubmit() {
    if (!ready || pending || disabled) return
    setPending(true)
    try {
      await onSubmit(
        questions.map((q) => ({
          question_id: q.id,
          answer: (answers[q.id] ?? '').trim(),
        })),
      )
    } finally {
      setPending(false)
    }
  }

  return (
    <div
      data-testid="clarification-card"
      className="rounded-md border border-amber-300 bg-amber-50/80 p-4"
    >
      <h3 className="text-sm font-semibold text-amber-950">需要澄清</h3>
      <p className="mt-1 text-xs text-amber-900/80">
        请回答后再继续生成，避免智能体自行假设。
      </p>
      <ul className="mt-3 space-y-3">
        {questions.map((q) => (
          <li key={q.id} className="space-y-1.5">
            <label
              className="block text-sm text-stone-800"
              htmlFor={`clarify-${q.id}`}
            >
              {q.question}
            </label>
            {q.options && q.options.length > 0 ? (
              <select
                id={`clarify-${q.id}`}
                data-testid={`clarify-select-${q.id}`}
                className="w-full rounded border border-stone-300 bg-white px-2 py-1.5 text-sm"
                value={answers[q.id] ?? ''}
                disabled={disabled || pending}
                onChange={(e) =>
                  setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))
                }
              >
                <option value="">请选择…</option>
                {q.options.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : (
              <textarea
                id={`clarify-${q.id}`}
                data-testid={`clarify-input-${q.id}`}
                className="min-h-16 w-full rounded border border-stone-300 bg-white px-2 py-1.5 text-sm"
                value={answers[q.id] ?? ''}
                disabled={disabled || pending}
                onChange={(e) =>
                  setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))
                }
              />
            )}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex justify-end">
        <button
          type="button"
          data-testid="clarification-submit"
          disabled={!ready || pending || disabled}
          className="rounded-md bg-amber-900 px-3 py-1.5 text-sm text-white hover:bg-amber-800 disabled:opacity-50"
          onClick={() => void handleSubmit()}
        >
          {pending ? '提交中…' : '提交答复'}
        </button>
      </div>
    </div>
  )
}
