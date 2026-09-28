type Props = {
  code: string
  onReloadFromFile: () => void
  onOverwriteFile: () => void
  busy?: boolean
  saveDisabled?: boolean
}

export function ConflictBanner({
  code,
  onReloadFromFile,
  onOverwriteFile,
  busy,
  saveDisabled,
}: Props) {
  const isMissing = code === 'file_missing'
  const isHash = code === 'hash_conflict'
  if (!isMissing && !isHash) return null

  return (
    <div
      className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900"
      data-testid="conflict-banner"
      role="alert"
    >
      <div className="font-medium">
        {isMissing ? '用例文件缺失（file_missing）' : '文件哈希冲突（hash_conflict）'}
      </div>
      <p className="mt-1 text-xs text-red-800">
        {isMissing
          ? '磁盘文件不存在，暂不可保存；请先对账或重新生成。'
          : '磁盘内容与库内 content_hash 不一致，请选择消解方式。'}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          className="rounded border border-red-400 bg-white px-2 py-1 text-xs disabled:opacity-40"
          data-testid="resolve-reload"
          disabled={busy}
          onClick={onReloadFromFile}
        >
          以文件为准
        </button>
        <button
          type="button"
          className="rounded border border-red-400 bg-white px-2 py-1 text-xs disabled:opacity-40"
          data-testid="resolve-overwrite"
          disabled={busy || saveDisabled || isMissing}
          onClick={onOverwriteFile}
        >
          覆盖文件
        </button>
      </div>
    </div>
  )
}
