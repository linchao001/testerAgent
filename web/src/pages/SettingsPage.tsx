/**
 * SettingsPage（WP-F5）：平台全局模型配置 + 保存后探活。
 */

import { useEffect, useState } from 'react'
import {
  getModelConfig,
  putModelConfig,
  testModelConfig,
} from '../api/endpoints'
import type { ModelConfig, ModelTestOut } from '../api/domain'
import { ApiError, NetworkError } from '../api/types'
import { clearLastError, reportError } from '../stores/connection'

const EMPTY: ModelConfig = {
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.2,
  top_p: 1.0,
  timeout: 120,
}

export function SettingsPage() {
  const [form, setForm] = useState<ModelConfig>(EMPTY)
  const [testOut, setTestOut] = useState<ModelTestOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [savedHint, setSavedHint] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      clearLastError()
      setLoading(true)
      try {
        const cfg = await getModelConfig()
        if (!cancelled) setForm(cfg)
      } catch (e) {
        reportError(e as ApiError | NetworkError | Error)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  async function handleSave() {
    clearLastError()
    setBusy(true)
    setSavedHint(null)
    setTestOut(null)
    try {
      const saved = await putModelConfig(form)
      setForm(saved)
      setSavedHint('已保存')
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  async function handleTest() {
    clearLastError()
    setBusy(true)
    setTestOut(null)
    try {
      const out = await testModelConfig()
      setTestOut(out)
    } catch (e) {
      reportError(e as ApiError | NetworkError | Error)
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <p className="text-sm text-stone-500" data-testid="settings-loading">
        加载模型配置…
      </p>
    )
  }

  return (
    <div className="mx-auto max-w-xl space-y-6" data-testid="settings-page">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold text-stone-900">设置</h1>
        <p className="text-sm text-stone-500">
          平台全局模型配置。先保存再「测试连接」（探活读取已落库配置）。
        </p>
      </header>

      <section className="space-y-3 rounded-md border border-stone-200 bg-white p-4">
        <label className="block text-sm">
          <span className="mb-1 block text-stone-700">Base URL</span>
          <input
            aria-label="Base URL"
            className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
            value={form.base_url}
            disabled={busy}
            onChange={(e) =>
              setForm((f) => ({ ...f, base_url: e.target.value }))
            }
            placeholder="https://api.deepseek.com/v1"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-stone-700">API Key</span>
          <input
            aria-label="API Key"
            type="password"
            autoComplete="off"
            className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
            value={form.api_key}
            disabled={busy}
            onChange={(e) =>
              setForm((f) => ({ ...f, api_key: e.target.value }))
            }
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-stone-700">模型</span>
          <input
            aria-label="模型"
            className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
            value={form.model}
            disabled={busy}
            onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
            placeholder="deepseek-chat"
          />
        </label>
        <div className="grid grid-cols-3 gap-3">
          <label className="block text-sm">
            <span className="mb-1 block text-stone-700">temperature</span>
            <input
              aria-label="temperature"
              type="number"
              step="0.1"
              min={0}
              max={2}
              className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
              value={form.temperature}
              disabled={busy}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  temperature: Number(e.target.value),
                }))
              }
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-stone-700">top_p</span>
            <input
              aria-label="top_p"
              type="number"
              step="0.05"
              min={0}
              max={1}
              className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
              value={form.top_p}
              disabled={busy}
              onChange={(e) =>
                setForm((f) => ({ ...f, top_p: Number(e.target.value) }))
              }
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-stone-700">timeout（秒）</span>
            <input
              aria-label="timeout"
              type="number"
              min={1}
              className="w-full rounded-md border border-stone-300 px-3 py-1.5 text-sm"
              value={form.timeout}
              disabled={busy}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  timeout: Number(e.target.value),
                }))
              }
            />
          </label>
        </div>

        <div className="flex flex-wrap gap-2 pt-1">
          <button
            type="button"
            data-testid="model-save-btn"
            disabled={busy}
            onClick={() => void handleSave()}
            className="rounded-md bg-stone-900 px-3 py-1.5 text-sm text-white hover:bg-stone-700 disabled:opacity-50"
          >
            保存
          </button>
          <button
            type="button"
            data-testid="model-test-btn"
            disabled={busy}
            onClick={() => void handleTest()}
            className="rounded-md border border-stone-300 px-3 py-1.5 text-sm text-stone-800 hover:bg-stone-50 disabled:opacity-50"
          >
            测试连接
          </button>
          {savedHint ? (
            <span className="self-center text-sm text-teal-800">{savedHint}</span>
          ) : null}
        </div>

        {testOut ? (
          <div
            className="rounded border border-teal-200 bg-teal-50/50 px-3 py-2 text-sm text-teal-950"
            data-testid="model-test-result"
          >
            探活成功 · 延迟{' '}
            <span data-testid="model-test-latency">{testOut.latency_ms}</span> ms
            · 模型{' '}
            <span data-testid="model-test-model">{testOut.model ?? '—'}</span>
          </div>
        ) : null}
      </section>
    </div>
  )
}
