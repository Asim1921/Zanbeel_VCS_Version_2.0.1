import React, { useCallback, useEffect, useState } from 'react'
import { FiAlertTriangle, FiCheckCircle, FiRefreshCw, FiSearch } from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import api from '../../utils/api'

/**
 * The code search index: what it holds and what is waiting.
 *
 * It holds nothing that is not derived from the object store, so rebuilding it is
 * always safe -- searches answer from the old index until the new one catches up.
 */

const formatBytes = (n) => {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = n
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`
}

export default function SearchIndexPanel() {
  const [index, setIndex] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setIndex((await api.getSearchIndexStatus()).index)
    } catch (err) {
      setError(err.message || 'Could not read the search index status')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // While anything is queued, look again every few seconds.
  useEffect(() => {
    if (!index?.queued?.length && !index?.running) return undefined
    const t = setTimeout(load, 3000)
    return () => clearTimeout(t)
  }, [index, load])

  const rebuild = async () => {
    if (!window.confirm('Rebuild the code search index from every repository? Searches keep working while it runs.')) return
    try {
      setBusy(true)
      const response = await api.rebuildSearchIndex()
      setNotice(`Queued ${response.queued} repositor${response.queued === 1 ? 'y' : 'ies'} for indexing.`)
      setIndex(response.index)
    } catch (err) {
      setError(err.message || 'Rebuild failed')
    } finally {
      setBusy(false)
    }
  }

  if (loading && !index) return <div className="h-32 animate-pulse rounded-xl bg-white/[0.04]" />

  return (
    <div className="space-y-4" data-testid="search-index-panel">
      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" /> {error}
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" /> {notice}
        </GlassCard>
      )}

      <GlassCard hover={false} className="p-5">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <FiSearch className="h-5 w-5 text-accent" />
          <h3 className="text-sm font-semibold text-ink">Code search index</h3>
          {index?.enabled ? (
            index.queued?.length || index.running
              ? <Badge variant="warning">Indexing</Badge>
              : <Badge variant="success">Up to date</Badge>
          ) : (
            <Badge variant="default">Disabled</Badge>
          )}
          <div className="ml-auto flex gap-2">
            <Button variant="ghost" size="sm" onClick={load} className="!px-2" title="Refresh">
              <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            {index?.enabled && (
              <Button variant="secondary" size="sm" onClick={rebuild} disabled={busy}>
                {busy ? 'Queuing…' : 'Rebuild'}
              </Button>
            )}
          </div>
        </div>

        {!index?.enabled ? (
          <p className="text-sm text-muted">
            The index is turned off (FOXNEST_SEARCH_INDEX=0), so code search covers one repository at a time.
          </p>
        ) : (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-[13px] sm:grid-cols-4">
            <div><dt className="text-muted">Repositories</dt><dd className="text-ink">{index.repositories}</dd></div>
            <div><dt className="text-muted">Files</dt><dd className="text-ink">{index.files} ({index.indexed_files} text)</dd></div>
            <div><dt className="text-muted">Unique contents</dt><dd className="text-ink">{index.unique_blobs} · {formatBytes(index.indexed_text_bytes)}</dd></div>
            <div><dt className="text-muted">On disk</dt><dd className="text-ink">{formatBytes(index.size_bytes)}</dd></div>
            <div className="col-span-2 sm:col-span-4">
              <dt className="text-muted">Waiting</dt>
              <dd className="text-ink">
                {index.running ? `indexing ${index.running}` : 'nothing running'}
                {index.queued?.length ? `, ${index.queued.length} queued` : ''}
              </dd>
            </div>
            {index.last_error && (
              <div className="col-span-2 sm:col-span-4">
                <dt className="text-muted">Last error</dt>
                <dd className="break-words text-danger-fg">{index.last_error}</dd>
              </div>
            )}
            <div className="col-span-2 sm:col-span-4">
              <dt className="text-muted">File</dt>
              <dd className="break-all font-mono text-[11.5px] text-ink-soft">{index.path}</dd>
            </div>
          </dl>
        )}
      </GlassCard>
    </div>
  )
}
