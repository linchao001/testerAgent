type Props = {
  title: string
  note?: string
}

export function PlaceholderPage({ title, note }: Props) {
  return (
    <div className="rounded-lg border border-dashed border-stone-300 bg-white p-8">
      <h1 className="text-xl font-semibold text-stone-900">{title}</h1>
      <p className="mt-2 text-sm text-stone-500">
        {note ?? '占位页，业务交互在后续 WP 实现。'}
      </p>
    </div>
  )
}
