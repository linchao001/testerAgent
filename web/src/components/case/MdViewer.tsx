import ReactMarkdown from 'react-markdown'

type Props = {
  markdown: string
}

export function MdViewer({ markdown }: Props) {
  return (
    <div
      className="prose prose-sm max-w-none overflow-auto rounded border border-stone-200 bg-white p-4 text-stone-800"
      data-testid="md-viewer"
    >
      <ReactMarkdown>{markdown}</ReactMarkdown>
    </div>
  )
}
