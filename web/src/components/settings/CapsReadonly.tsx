/** ReMe 能力位只读勾选（dd §12.3；不可手改）。 */

import type { ReMeCapabilities } from '../../api/domain'

const LABELS: { key: keyof ReMeCapabilities; hint: string }[] = [
  { key: 'metadata_filter', hint: '服务端 types/scope 过滤' },
  { key: 'entry_version', hint: '条目版本/更新时间' },
  { key: 'passage_api', hint: '段落级召回' },
]

type Props = {
  caps: ReMeCapabilities
}

export function CapsReadonly({ caps }: Props) {
  return (
    <fieldset
      data-testid="kb-capabilities"
      className="space-y-1.5"
      disabled
    >
      <legend className="sr-only">知识库能力位</legend>
      {LABELS.map(({ key, hint }) => (
        <label
          key={key}
          className="flex items-center gap-2 text-sm text-stone-800"
        >
          <input
            type="checkbox"
            aria-label={key}
            checked={caps[key]}
            disabled
            readOnly
          />
          <span>
            <code className="rounded bg-stone-100 px-1 text-xs">{key}</code>
            <span className="ml-2 text-xs text-stone-500">{hint}</span>
          </span>
        </label>
      ))}
    </fieldset>
  )
}
