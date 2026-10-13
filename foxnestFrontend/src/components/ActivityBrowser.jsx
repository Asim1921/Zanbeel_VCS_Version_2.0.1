import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { FiAlertTriangle, FiDownload, FiFilter, FiRefreshCw } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import ActivityFeed from './ActivityFeed'
import api from '../utils/api'

/**
 * The activity log with its filters, for the whole server or one repository.
 *
 * Filtering and paging happen on the server. The old page fetched up to 500 rows and
 * searched those in the browser, so "nothing matches" only ever meant "nothing in the
 * last 500 rows". With ``useUrl`` the filters live in the address, so a filtered view
 * can be bookmarked or sent to someone.
 */

const PAGE = 50
const FIELDS = ['user', 'type', 'repo', 'since', 'until', 'q']

let typesPromise = null
function loadTypes() {
  if (!typesPromise) typesPromise = api.getActivityTypes().catch(() => ({ types: [] }))
  return typesPromise
}

function useFilters(useUrl) {
  const [params, setParams] = useSearchParams()
  const [local, setLocal] = useState({})
  const values = useMemo(() => {
    if (!useUrl) return local
    const out = {}
    for (const key of [...FIELDS, 'page']) out[key] = params.get(key) || ''
    return out
  }, [useUrl, params, local])
  const set = useCallback((changes) => {
    if (!useUrl) {
      setLocal((current) => ({ ...current, ...changes }))
      return
    }
    setParams((current) => {
      const next = new URLSearchParams(current)
      for (const [key, value] of Object.entries(changes)) {
        if (value === '' || value === null || value === undefined) next.delete(key)
        else next.set(key, String(value))
      }
      return next
    }, { replace: true })
  }, [useUrl, setParams])
  return [values, set]
}

const inputClass =
  'rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none'

export default function ActivityBrowser({ repoId = null, isAdmin = false, useUrl = false }) {
  const [filters, setFilters] = useFilters(useUrl)
  const [types, setTypes] = useState([])
  const [repos, setRepos] = useState([])
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [exporting, setExporting] = useState(false)
  // Typed filters apply when the person stops typing, not on every key.
  const [draft, setDraft] = useState({ user: filters.user || '', q: filters.q || '' })

  const page = Math.max(0, Number(filters.page) || 0)

  useEffect(() => {
    loadTypes().then((r) => setTypes(r.types || []))
    if (!repoId) {
      api.listRepositories().then((r) => setRepos(r.repositories || [])).catch(() => setRepos([]))
    }
  }, [repoId])

  // Back and Forward change the filters from outside; the boxes follow.
  useEffect(() => {
    setDraft({ user: filters.user || '', q: filters.q || '' })
  }, [filters.user, filters.q])

  useEffect(() => {
    const t = setTimeout(() => {
      if ((draft.user || '') !== (filters.user || '') || (draft.q || '') !== (filters.q || '')) {
        setFilters({ user: draft.user, q: draft.q, page: '' })
      }
    }, 400)
    return () => clearTimeout(t)
  }, [draft, filters.user, filters.q, setFilters])

  const query = useMemo(() => ({
    user: filters.user, type: filters.type, since: filters.since, until: filters.until, q: filters.q,
    repositoryId: repoId ? null : filters.repo, limit: PAGE, offset: page * PAGE,
  }), [filters, page, repoId])

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setData(repoId ? await api.getRepositoryActivity(repoId, query) : await api.getActivities(query))
    } catch (err) {
      setError(err.message || 'Failed to load activity')
    } finally {
      setLoading(false)
    }
  }, [repoId, query])

  useEffect(() => {
    load()
  }, [load])

  const exportCsv = async () => {
    try {
      setExporting(true)
      const { blob } = await api.downloadActivityCsv(query)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'zanbeel-activity.csv'
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setError(err.message || 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  const meta = data?.pagination || {}
  const activities = data?.activities || []
  const hasFilters = FIELDS.some((key) => filters[key])
  const grouped = useMemo(() => {
    const out = new Map()
    for (const t of types) {
      if (!out.has(t.category)) out.set(t.category, [])
      out.get(t.category).push(t)
    }
    return [...out.entries()]
  }, [types])

  return (
    <div className="space-y-4" data-testid="activity-browser">
      <GlassCard hover={false} className="space-y-3 p-4">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <input
            aria-label="Filter by user"
            value={draft.user}
            onChange={(e) => setDraft((d) => ({ ...d, user: e.target.value }))}
            placeholder="User…"
            className={inputClass}
          />
          <select
            aria-label="Filter by event type"
            value={filters.type || ''}
            onChange={(e) => setFilters({ type: e.target.value, page: '' })}
            className={inputClass}
          >
            <option value="">All events</option>
            {grouped.map(([category, list]) => (
              <optgroup key={category} label={category}>
                {list.map((t) => <option key={t.type} value={t.type}>{t.label}</option>)}
              </optgroup>
            ))}
          </select>
          {!repoId && (
            <select
              aria-label="Filter by repository"
              value={filters.repo || ''}
              onChange={(e) => setFilters({ repo: e.target.value, page: '' })}
              className={inputClass}
            >
              <option value="">All repositories</option>
              {repos.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          )}
          <input
            aria-label="From date"
            type="date"
            value={filters.since || ''}
            onChange={(e) => setFilters({ since: e.target.value, page: '' })}
            className={inputClass}
          />
          <input
            aria-label="To date"
            type="date"
            value={filters.until || ''}
            onChange={(e) => setFilters({ until: e.target.value, page: '' })}
            className={inputClass}
          />
          <input
            aria-label="Words in the description"
            value={draft.q}
            onChange={(e) => setDraft((d) => ({ ...d, q: e.target.value }))}
            placeholder="Words…"
            className={inputClass}
          />
        </div>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          {hasFilters && (
            <>
              <FiFilter className="h-3.5 w-3.5 text-accent" />
              <button
                type="button"
                onClick={() => {
                  setDraft({ user: '', q: '' })
                  setFilters(Object.fromEntries([...FIELDS, 'page'].map((k) => [k, ''])))
                }}
                className="text-xs text-accent underline-offset-2 hover:underline"
              >
                Clear filters
              </button>
            </>
          )}
          <span className="text-xs text-muted" data-testid="activity-count">
            {loading ? 'Loading…' : `${meta.total ?? 0} event${meta.total === 1 ? '' : 's'}`}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
              <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            {isAdmin && !repoId && (
              <Button variant="secondary" size="sm" onClick={exportCsv} disabled={exporting}
                      className="flex items-center gap-1.5">
                <FiDownload className="h-3.5 w-3.5" />
                {exporting ? 'Exporting…' : 'Export CSV'}
              </Button>
            )}
          </div>
        </div>
      </GlassCard>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      <GlassCard hover={false} className="p-5">
        <ActivityFeed
          activities={activities}
          loading={loading}
          showRepository={!repoId}
          emptyHint={hasFilters ? 'Nothing matches those filters.' : 'No activity has been recorded yet.'}
        />
        {(page > 0 || meta.has_more) && (
          <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-[11.5px] text-muted">
            <span>
              {meta.offset + 1}–{meta.offset + (meta.returned || 0)} of {meta.total}
            </span>
            <div className="flex gap-1.5">
              <button type="button" disabled={page === 0}
                      onClick={() => setFilters({ page: page - 1 || '' })}
                      className="rounded-lg border border-border px-2 py-1 hover:text-ink disabled:opacity-40">
                Newer
              </button>
              <button type="button" disabled={!meta.has_more}
                      onClick={() => setFilters({ page: page + 1 })}
                      className="rounded-lg border border-border px-2 py-1 hover:text-ink disabled:opacity-40">
                Older
              </button>
            </div>
          </div>
        )}
      </GlassCard>
    </div>
  )
}
