import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  FiBookOpen,
  FiFile,
  FiRefreshCw,
  FiAlertTriangle,
  FiDownload,
  FiCpu,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'
import { API_SERVER_URL } from '../../config'
import { getSessionToken } from '../../utils/session'
import { cn } from '../../lib/utils'

/**
 * Browse the documentation the LLM pipeline generates.
 *
 * Reachable only from the CLI until now, despite four endpoints and a 2,370-line
 * generator behind it. Files are fetched with the session token rather than
 * linked directly, because the docs endpoints require authentication and a plain
 * <a href> carries no Authorization header.
 */

function prettyBytes(n = 0) {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB']
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1)
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export default function DocsPanel({ repoId, canWrite = false }) {
  const [docs, setDocs] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [content, setContent] = useState('')
  const [contentLoading, setContentLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [notice, setNotice] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.getRepositoryDocs(repoId)
      setDocs(response)
    } catch (err) {
      setError(err.message || 'Failed to load documentation')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => {
    load()
  }, [load])

  const files = useMemo(
    () => (docs?.files || []).slice().sort((a, b) => a.path.localeCompare(b.path)),
    [docs]
  )

  const openFile = async (file) => {
    setSelected(file.path)
    setContentLoading(true)
    setContent('')
    try {
      const token = getSessionToken()
      const res = await fetch(
        `${API_SERVER_URL}/api/repository/${repoId}/docs/${file.path.replace(/\\/g, '/')}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setContent(await res.text())
    } catch (err) {
      setContent(`Could not load this file: ${err.message}`)
    } finally {
      setContentLoading(false)
    }
  }

  const downloadArchive = async () => {
    try {
      const token = getSessionToken()
      const res = await fetch(`${API_SERVER_URL}/api/repository/${repoId}/docs/archive.zip`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `docs-${repoId}.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(err.message || 'Download failed')
    }
  }

  const generate = async () => {
    const ok = window.confirm(
      'Generate documentation now?\n\n' +
        'This runs the LLM pipeline over the repository and can take several minutes.'
    )
    if (!ok) return
    try {
      setGenerating(true)
      setError(null)
      setNotice(null)
      await api.generateProjectDocs(repoId, {})
      setNotice('Documentation generated.')
      await load()
    } catch (err) {
      setError(err.message || 'Generation failed')
    } finally {
      setGenerating(false)
    }
  }

  const hasDocs = docs?.success && files.length > 0

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg font-light tracking-tight text-ink">
            Generated documentation
          </h3>
          <p className="mt-1 text-sm text-muted">
            Produced by the server-side generator. Until now it was reachable only from
            the CLI.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          {hasDocs && (
            <Button variant="secondary" size="sm" onClick={downloadArchive} className="flex items-center gap-1.5">
              <FiDownload className="h-3.5 w-3.5" />
              Download .zip
            </Button>
          )}
          {canWrite && (
            <Button onClick={generate} disabled={generating} className="flex items-center gap-2">
              <FiCpu className={cn('h-4 w-4', generating && 'animate-pulse')} />
              {generating ? 'Generating…' : 'Generate'}
            </Button>
          )}
        </div>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          {notice}
        </GlassCard>
      )}

      {loading ? (
        <div className="h-40 animate-pulse rounded-xl bg-white/[0.04]" />
      ) : !hasDocs ? (
        <EmptyState
          icon={FiBookOpen}
          title="No documentation yet"
          description={
            docs?.error ||
            'Nothing has been generated for this repository. Running the generator reads the code and writes Markdown.'
          }
          action={canWrite ? <Button onClick={generate}>Generate documentation</Button> : null}
        />
      ) : (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,240px)_1fr]">
          <GlassCard hover={false} className="max-h-[46vh] overflow-y-auto p-2">
            {files.map((f) => (
              <button
                key={f.path}
                type="button"
                onClick={() => openFile(f)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors',
                  selected === f.path
                    ? 'bg-accent/12 text-ink'
                    : 'text-ink-soft hover:bg-white/[0.04] hover:text-ink'
                )}
              >
                <FiFile className="h-3.5 w-3.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate font-mono text-[12px]">{f.path}</span>
                <span className="shrink-0 font-mono text-[10px] text-muted">
                  {prettyBytes(f.size)}
                </span>
              </button>
            ))}
          </GlassCard>

          <GlassCard hover={false} className="max-h-[46vh] overflow-auto p-4">
            {!selected ? (
              <p className="py-10 text-center text-sm text-muted">
                Pick a file to read it.
              </p>
            ) : contentLoading ? (
              <div className="space-y-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="h-3 animate-pulse rounded bg-white/[0.06]" />
                ))}
              </div>
            ) : (
              <>
                <div className="mb-3 flex items-center gap-2 border-b border-border pb-2">
                  <FiFile className="h-3.5 w-3.5 text-accent" />
                  <span className="font-mono text-[12px] text-ink">{selected}</span>
                  <Badge variant="default">{content.split('\n').length} lines</Badge>
                </div>
                <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-ink-soft">
                  {content}
                </pre>
              </>
            )}
          </GlassCard>
        </div>
      )}
    </div>
  )
}
