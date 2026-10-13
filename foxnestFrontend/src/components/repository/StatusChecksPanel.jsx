import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  FiCheckCircle,
  FiAlertTriangle,
  FiRefreshCw,
  FiShield,
  FiSave,
  FiPlus,
  FiX,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import ChecksList from './ChecksList'
import api from '../../utils/api'
import { cn } from '../../lib/utils'

/**
 * Build and test results reported against a commit, plus the merge gate they feed.
 *
 * The required checks set here apply to every merge into every branch of the
 * repository — pull request or not. A check can be pinned to the account that is
 * allowed to report it (the CI system), so nobody else's "passed" counts.
 */

function OverallBadge({ state, satisfied, hasRequired }) {
  if (state === 'none') return <Badge variant="default">No checks</Badge>
  if (state === 'success' && (!hasRequired || satisfied))
    return <Badge variant="success">All checks passed</Badge>
  if (state === 'failure') return <Badge variant="danger">Checks failing</Badge>
  if (state === 'pending') return <Badge variant="warning">Checks running</Badge>
  return <Badge variant="default">{state}</Badge>
}

const toRows = (stored = []) =>
  stored.map((entry) =>
    typeof entry === 'string'
      ? { context: entry, reporter: '' }
      : { context: entry.context || '', reporter: entry.reporter || '' }
  )

const fromRows = (rows) =>
  rows
    .map((row) => ({ context: row.context.trim(), reporter: row.reporter.trim() }))
    .filter((row) => row.context)
    .map((row) => (row.reporter ? row : row.context))

export default function StatusChecksPanel({ repoId, commitId, canManage = false }) {
  const [combined, setCombined] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [rows, setRows] = useState([])
  const [policyLoaded, setPolicyLoaded] = useState(false)
  // Once someone has started editing, a reload (or a slow first load) must not
  // replace what they typed with the saved list.
  const edited = useRef(false)
  const [savingPolicy, setSavingPolicy] = useState(false)
  const [savedNote, setSavedNote] = useState(null)

  const load = useCallback(async () => {
    if (!repoId) {
      setLoading(false)
      return
    }
    try {
      setLoading(true)
      setError(null)
      const [statuses, policy] = await Promise.all([
        commitId ? api.getCommitStatuses(repoId, commitId) : Promise.resolve(null),
        canManage ? api.getBranchPolicy(repoId).catch(() => null) : Promise.resolve(null),
      ])
      setCombined(statuses?.combined || null)
      if (policy && !edited.current) setRows(toRows(policy.policy?.required_status_checks))
      if (policy) setPolicyLoaded(true)
    } catch (err) {
      setError(err.message || 'Failed to load status checks')
    } finally {
      setLoading(false)
    }
  }, [repoId, commitId, canManage])

  useEffect(() => {
    load()
  }, [load])

  const editRows = (change) => {
    edited.current = true
    setRows(change)
  }

  const updateRow = (index, field, value) =>
    editRows((current) => current.map((row, i) => (i === index ? { ...row, [field]: value } : row)))

  const saveRequired = async () => {
    try {
      setSavingPolicy(true)
      setError(null)
      setSavedNote(null)
      const checks = fromRows(rows)
      // PUT replaces the whole policy, so the current values have to be sent
      // back alongside the change or they would be reset to defaults.
      const current = await api.getBranchPolicy(repoId)
      const policy = { ...(current.policy || {}), required_status_checks: checks }
      await api.updateBranchPolicy(repoId, policy)
      const names = checks.map((c) => (typeof c === 'string' ? c : `${c.context} (from ${c.reporter})`))
      setSavedNote(
        names.length ? `Merges now require: ${names.join(', ')}` : 'Status checks no longer gate merges.'
      )
      edited.current = false
      await load()
    } catch (err) {
      setError(err.message || 'Failed to update required checks')
    } finally {
      setSavingPolicy(false)
    }
  }

  if (!commitId && !canManage) {
    return (
      <EmptyState
        icon={FiShield}
        title="No commit selected"
        description="Status checks are reported against a specific commit."
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h3 className="font-display text-lg font-light tracking-tight text-ink">Checks</h3>
          {combined && (
            <OverallBadge
              state={combined.state}
              satisfied={combined.required_satisfied}
              hasRequired={(combined.required || []).length > 0}
            />
          )}
          {commitId && <span className="ref">{commitId.slice(0, 8)}</span>}
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
      ) : commitId ? (
        <ChecksList
          combined={combined}
          emptyHint="Nothing has reported. A CI job reports with: fox status-check report ci/unit-tests success"
        />
      ) : null}

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
        <GlassCard hover={false} className="space-y-3 p-4" data-testid="required-checks-editor">
          <div>
            <p className="text-[13px] font-medium text-ink-soft">Required checks</p>
            <p className="mt-0.5 text-xs text-muted">
              Must pass before anything merges into any branch of this repository, by pull
              request or by a direct branch merge. Pin a check to the account your CI reports
              with, so nobody else can mark it passed. Rules for a single branch live in branch
              protection.
            </p>
          </div>

          {!policyLoaded ? (
            <div className="h-9 animate-pulse rounded-lg bg-white/[0.04]" />
          ) : (
            rows.length === 0 && <p className="text-xs text-muted">No checks are required.</p>
          )}
          {rows.map((row, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <input
                aria-label="Check name"
                value={row.context}
                onChange={(e) => updateRow(index, 'context', e.target.value)}
                placeholder="ci/unit-tests"
                className="min-w-0 flex-[2] rounded-lg border border-border bg-cream-mid px-3 py-2 font-mono text-[13px] text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
              />
              <input
                aria-label="Only counts when reported by"
                value={row.reporter}
                onChange={(e) => updateRow(index, 'reporter', e.target.value)}
                placeholder="any writer (or a username, e.g. ci-bot)"
                className="min-w-0 flex-[2] rounded-lg border border-border bg-cream-mid px-3 py-2 text-[13px] text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => editRows((current) => current.filter((_, i) => i !== index))}
                className="!px-2"
                title="Remove"
              >
                <FiX className="h-4 w-4" />
              </Button>
            </div>
          ))}

          <div className="flex flex-wrap justify-between gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => editRows((current) => [...current, { context: '', reporter: '' }])}
              disabled={!policyLoaded}
              className="flex items-center gap-1.5"
            >
              <FiPlus className="h-3.5 w-3.5" />
              Add check
            </Button>
            <Button
              onClick={saveRequired}
              disabled={savingPolicy || !policyLoaded}
              className="flex items-center gap-2"
            >
              <FiSave className="h-3.5 w-3.5" />
              {savingPolicy ? 'Saving…' : 'Save'}
            </Button>
          </div>

          <p className="text-xs text-muted">
            CI reports with an access token that has only the <span className="text-ink-soft">Report
            CI statuses</span> access: it can say whether a commit passed, and cannot push.
          </p>
        </GlassCard>
      )}
    </div>
  )
}
