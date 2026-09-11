import React, { useCallback, useEffect, useState } from 'react'
import {
  FiCheckCircle,
  FiXCircle,
  FiClock,
  FiAlertTriangle,
  FiExternalLink,
  FiRefreshCw,
  FiShield,
  FiSave,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'
import { cn } from '../../lib/utils'

/**
 * Build and test results reported against a commit, plus the merge gate they
 * feed.
 *
 * The distinction the UI has to make loudly is between a check that *failed*
 * and one that has *not reported at all* — the second looks like nothing is
 * wrong, and is exactly the case the gate exists to catch when CI fails to
 * start.
 */

const STATE_META = {
  success: { icon: FiCheckCircle, tone: 'text-success-fg', label: 'Passed' },
  failure: { icon: FiXCircle, tone: 'text-danger-fg', label: 'Failed' },
  error: { icon: FiAlertTriangle, tone: 'text-danger-fg', label: 'Errored' },
  pending: { icon: FiClock, tone: 'text-warning-fg', label: 'Running' },
}

function OverallBadge({ state, satisfied, hasRequired }) {
  if (state === 'none') return <Badge variant="default">No checks</Badge>
  if (state === 'success' && (!hasRequired || satisfied))
    return <Badge variant="success">All checks passed</Badge>
  if (state === 'failure') return <Badge variant="danger">Checks failing</Badge>
  if (state === 'pending') return <Badge variant="warning">Checks running</Badge>
  return <Badge variant="default">{state}</Badge>
}

export default function StatusChecksPanel({ repoId, commitId, canManage = false }) {
  const [combined, setCombined] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [required, setRequired] = useState('')
  const [savingPolicy, setSavingPolicy] = useState(false)
  const [savedNote, setSavedNote] = useState(null)

  const load = useCallback(async () => {
    if (!repoId || !commitId) {
      setLoading(false)
      return
    }
    try {
      setLoading(true)
      setError(null)
      const response = await api.getCommitStatuses(repoId, commitId)
      setCombined(response.combined)
      setRequired((response.combined?.required || []).join(', '))
    } catch (err) {
      setError(err.message || 'Failed to load status checks')
    } finally {
      setLoading(false)
    }
  }, [repoId, commitId])

  useEffect(() => {
    load()
  }, [load])

  const saveRequired = async () => {
    try {
      setSavingPolicy(true)
      setError(null)
      setSavedNote(null)
      const contexts = required
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      // PUT replaces the whole policy, so the current values have to be sent
      // back alongside the change or they would be reset to defaults.
      const current = await api.getBranchPolicy(repoId)
      const policy = { ...(current.policy || {}), required_status_checks: contexts }
      await api.updateBranchPolicy(repoId, policy)
      setSavedNote(
        contexts.length
          ? `Merges now require: ${contexts.join(', ')}`
          : 'Status checks no longer gate merges.'
      )
      await load()
    } catch (err) {
      setError(err.message || 'Failed to update required checks')
    } finally {
      setSavingPolicy(false)
    }
  }

  if (!commitId) {
    return (
      <EmptyState
        icon={FiShield}
        title="No commit selected"
        description="Status checks are reported against a specific commit."
      />
    )
  }

  const statuses = combined?.statuses || []
  const missing = combined?.missing_required || []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h3 className="font-display text-lg font-light tracking-tight text-ink">
            Checks
          </h3>
          {combined && (
            <OverallBadge
              state={combined.state}
              satisfied={combined.required_satisfied}
              hasRequired={(combined.required || []).length > 0}
            />
          )}
          <span className="ref">{commitId.slice(0, 8)}</span>
        </div>
        <Button variant="ghost" size="sm" onClick={load} className="!px-2" title="Refresh">
          <FiRefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
        </Button>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {savedNote && (
        <GlassCard className="flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span>{savedNote}</span>
        </GlassCard>
      )}

      {loading ? (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded-lg bg-white/[0.04]" />
          ))}
        </div>
      ) : statuses.length === 0 && missing.length === 0 ? (
        <EmptyState
          icon={FiShield}
          title="Nothing has reported"
          description="A CI job reports here with: fox status-check report ci/unit-tests success"
        />
      ) : (
        <div className="space-y-2">
          {statuses.map((status) => {
            const meta = STATE_META[status.state] || STATE_META.pending
            const Icon = meta.icon
            const isRequired = (combined.required || []).includes(status.context)
            return (
              <GlassCard key={status.id} hover={false} className="flex items-center gap-3 p-3">
                <Icon className={cn('h-4 w-4 shrink-0', meta.tone)} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-mono text-[13px] text-ink">
                      {status.context}
                    </span>
                    {isRequired && <Badge variant="info">Required</Badge>}
                  </div>
                  <p className="mt-0.5 truncate text-xs text-muted">
                    {meta.label}
                    {status.description && ` — ${status.description}`}
                    {status.reported_by && ` · by ${status.reported_by}`}
                  </p>
                </div>
                {status.target_url && (
                  <a
                    href={status.target_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="flex shrink-0 items-center gap-1 text-xs text-accent hover:underline"
                  >
                    Details
                    <FiExternalLink className="h-3 w-3" />
                  </a>
                )}
              </GlassCard>
            )
          })}

          {/* Never-reported required checks. Shown explicitly because an absent
              row reads as "fine" and this is the opposite of fine. */}
          {missing.map((context) => (
            <GlassCard
              key={`missing-${context}`}
              hover={false}
              className="flex items-center gap-3 border-warning-fg/25 p-3"
            >
              <FiClock className="h-4 w-4 shrink-0 text-warning-fg" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-mono text-[13px] text-ink">{context}</span>
                  <Badge variant="info">Required</Badge>
                </div>
                <p className="mt-0.5 text-xs text-warning-fg">
                  Has not reported. A required check that never runs blocks the merge.
                </p>
              </div>
            </GlassCard>
          ))}
        </div>
      )}

      {combined && (combined.required || []).length > 0 && (
        <div
          className={cn(
            'rounded-lg border p-3 text-sm',
            combined.required_satisfied
              ? 'border-success-fg/25 bg-success-bg text-success-fg'
              : 'border-danger-fg/25 bg-danger-bg text-danger-fg'
          )}
        >
          {combined.required_satisfied
            ? 'Required checks satisfied — this commit can merge.'
            : 'Required checks not satisfied — merges are blocked.'}
        </div>
      )}

      {canManage && (
        <GlassCard hover={false} className="space-y-3 p-4">
          <div>
            <p className="text-[13px] font-medium text-ink-soft">Required checks</p>
            <p className="mt-0.5 text-xs text-muted">
              Comma-separated contexts that must pass before a pull request can merge.
              Leave empty to gate nothing.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              value={required}
              onChange={(e) => setRequired(e.target.value)}
              placeholder="ci/unit-tests, security/scan"
              className="min-w-0 flex-1 rounded-lg border border-border bg-cream-mid px-3 py-2 font-mono text-[13px] text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
            />
            <Button
              onClick={saveRequired}
              disabled={savingPolicy}
              className="flex shrink-0 items-center gap-2"
            >
              <FiSave className="h-3.5 w-3.5" />
              {savingPolicy ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </GlassCard>
      )}
    </div>
  )
}
