import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { FiRefreshCw, FiAlertTriangle, FiFilter } from 'react-icons/fi'
import PageHeader from '../components/ui/PageHeader'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import ActivityFeed from '../components/ActivityFeed'
import api from '../utils/api'

/**
 * The audit view.
 *
 * /api/activities has been implemented and populated for a long time, and the
 * only thing rendering it was a ten-row preview on the dashboard — so nobody
 * could answer "who did what" beyond the last few minutes. This is the whole
 * feed, filterable by who, what and which repository.
 *
 * Filtering is done client-side on a fetched page because the endpoint takes
 * only a limit. That is honest about what it is: the count control says how many
 * records were pulled, so a filter that finds nothing is visibly a question of
 * range rather than of truth.
 */

const TYPE_LABELS = {
  push_commit: 'Pushes',
  pending_commit: 'Pending commits',
  create_repository: 'Repositories created',
  request_repository: 'Repositories requested',
  merge_branch: 'Branch merges',
  publish_branch: 'Branch publishes',
  copy_files: 'File copies',
  create_pull_request: 'Pull requests',
  merge_pull_request: 'PR merges',
  review_pull_request: 'PR reviews',
  archive_repository: 'Archives',
  update_branch_policy: 'Policy changes',
  register_user: 'Registrations',
}

const PAGE_SIZES = [50, 100, 250, 500]

export default function Activity() {
  const [activities, setActivities] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [limit, setLimit] = useState(100)

  const [user, setUser] = useState('')
  const [type, setType] = useState('')
  const [repo, setRepo] = useState('')

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.getActivities(limit)
      setActivities(response.activities || [])
    } catch (err) {
      setError(err.message || 'Failed to load activity')
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    load()
  }, [load])

  // Offer only the types actually present, so the filter never lists an option
  // that can return nothing.
  const availableTypes = useMemo(() => {
    const seen = new Map()
    for (const a of activities) {
      const t = a.activity_type || a.type
      if (t) seen.set(t, (seen.get(t) || 0) + 1)
    }
    return [...seen.entries()].sort((a, b) => b[1] - a[1])
  }, [activities])

  const availableRepos = useMemo(() => {
    const seen = new Set()
    for (const a of activities) if (a.repository) seen.add(a.repository)
    return [...seen].sort()
  }, [activities])

  const filtered = useMemo(() => {
    const u = user.trim().toLowerCase()
    return activities.filter((a) => {
      if (u && !(a.user || a.username || '').toLowerCase().includes(u)) return false
      if (type && (a.activity_type || a.type) !== type) return false
      if (repo && a.repository !== repo) return false
      return true
    })
  }, [activities, user, type, repo])

  // ActivityFeed reads `username` and `repository_name`; the endpoint returns
  // `user` and `repository`. Map rather than changing the shared component,
  // which the dashboard also uses.
  const feedItems = useMemo(
    () =>
      filtered.map((a) => ({
        ...a,
        username: a.user || a.username,
        repository_name: a.repository || a.repository_name,
      })),
    [filtered]
  )

  const clearFilters = () => {
    setUser('')
    setType('')
    setRepo('')
  }
  const hasFilters = Boolean(user || type || repo)

  return (
    <div className="p-6">
      <PageHeader
        eyebrow={`${activities.length} record${activities.length === 1 ? '' : 's'} loaded`}
        title="Activity"
        subtitle="Everything that has happened across the platform — who did it, to which repository, and when."
        actions={
          <Button variant="secondary" onClick={load} className="flex items-center gap-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        }
      />

      <GlassCard hover={false} className="mb-5 space-y-3 p-4">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <input
            value={user}
            onChange={(e) => setUser(e.target.value)}
            placeholder="Filter by user…"
            className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none"
          />

          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none"
          >
            <option value="">All event types</option>
            {availableTypes.map(([t, n]) => (
              <option key={t} value={t}>
                {TYPE_LABELS[t] || t.replace(/_/g, ' ')} ({n})
              </option>
            ))}
          </select>

          <select
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none"
          >
            <option value="">All repositories</option>
            {availableRepos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>

          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none"
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                Load {n} records
              </option>
            ))}
          </select>
        </div>

        {hasFilters && (
          <div className="flex items-center gap-2 pt-1">
            <FiFilter className="h-3.5 w-3.5 text-accent" />
            <span className="text-xs text-muted">
              Showing {filtered.length} of {activities.length}
            </span>
            <button
              type="button"
              onClick={clearFilters}
              className="text-xs text-accent underline-offset-2 hover:underline"
            >
              Clear filters
            </button>
          </div>
        )}
      </GlassCard>

      {error && (
        <GlassCard className="mb-5 flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      <GlassCard hover={false} className="p-5">
        <ActivityFeed
          activities={feedItems}
          loading={loading}
          emptyHint={
            hasFilters
              ? 'Nothing matches those filters in the records loaded. Try loading more.'
              : 'No activity has been recorded yet.'
          }
        />
      </GlassCard>
    </div>
  )
}
