type Props = {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
}

export function MdEditor({ value, onChange, disabled }: Props) {
  return (
    <textarea
      className="h-full min-h-[240px] w-full resize-y rounded border border-stone-300 bg-white p-3 font-mono text-sm text-stone-900"
      data-testid="md-editor"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
    />
  )
}
