import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { FiShield, FiLock, FiUnlock, FiArchive, FiAlertCircle, FiCheck } from 'react-icons/fi'
import api from '../utils/api'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import { cn } from '../lib/utils'

/**
 * Branch protection: what each branch will accept, and the trail of what it refused.
 *
 * The modes are deliberately blunt. "Frozen" means nothing moves this reference, for
 * anybody, which is only meaningful if the screen says so plainly rather than hiding it
 * behind a settings toggle nobody reads.
 */

const MODES = [
  { id: 'open', label: 'Open', icon: FiUnlock,
    blurb: 'Ordinary development. Repository permissions decide.' },
  { id: 'protected', label: 'Protected', icon: FiShield,
    blurb: 'No direct pushes and no force updates. Merges may still land.' },
  { id: 'frozen', label: 'Frozen', icon: FiLock,
    blurb: 'Nothing may change this reference, including administrators.' },
  { id: 'archived', label: 'Archived', icon: FiArchive,
    blurb: 'Frozen, and kept out of day-to-day listings.' },
]

const MODE_TONE = {
  open: 'border-border text-ink-soft',
  protected: 'border-info-fg/40 bg-info-fg/10 text-info-fg',
  frozen: 'border-warning-fg/40 bg-warning-fg/10 text-warning-fg',
  archived: 'border-muted/40 bg-white/[0.04] text-muted',
}

export function ModeBadge({ mode, className }) {
  if (!mode || mode === 'open') return null
  return (
    <span className={cn(
      'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
      MODE_TONE[mode] || MODE_TONE.open, className
    )}>
      {mode}
    </span>
  )
}

export default function BranchProtectionPanel({ repo }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(null)
  const [audit, setAudit] = useState([])
  const [chainIntact, setChainIntact] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [protection, trail] = await Promise.all([
        api.getBranchProtection(repo.id),
        api.getReferenceAudit(repo.id, { limit: 40 }).catch(() => null),
      ])
      setData(protection)
      if (trail) {
        setAudit(trail.events || [])
        setChainIntact(trail.chain_intact !== false)
      }
    } catch (err) {
      setError(err.message || 'Could not load branch protection')
    } finally {
      setLoading(false)
    }
  }, [repo.id])

  useEffect(() => { load() }, [load])

  const setMode = async (branch, mode) => {
    setBusy(`${branch}:${mode}`)
    setError('')
    setNotice('')
    try {
      await api.setBranchProtection(repo.id, branch, mode)
      setNotice(`'${branch}' is now ${mode}.`)
      await load()
    } catch (err) {
      // The server explains exactly why — an unlock requirement, most often.
      setError(err.message || 'Could not change protection')
    } finally {
      setBusy(null)
    }
  }

  const setApprovals = async (branch, info, count) => {
    setBusy(`${branch}:approvals`)
    setError('')
    setNotice('')
    try {
      // The mode is resent unchanged because the endpoint upserts the whole policy;
      // the rules are what this control is actually editing.
      await api.setBranchProtection(repo.id, branch, info.mode || 'open', null, {
        ...(info.rules || {}),
        required_approvals: count,
      })
      setNotice(
        count > 0
          ? `'${branch}' now requires ${count} approval(s) before anything lands on it.`
          : `'${branch}' no longer requires approvals.`
      )
      await load()
    } catch (err) {
      setError(err.message || 'Could not change the review requirement')
    } finally {
      setBusy(null)
    }
  }

  const requestUnlock = async (branch) => {
    const reason = window.prompt(
      `Why does '${branch}' need a temporary exception?\n\n` +
      'This is recorded in the audit log. The grant covers one change and expires in 30 minutes.'
    )
    if (!reason) return
    if (reason.trim().length < 10) {
      setError('The reason needs to be at least 10 characters.')
      return
    }
    setBusy(`${branch}:unlock`)
    setError('')
    try {
      await api.requestBranchUnlock(repo.id, branch, {
        reason: reason.trim(),
        operations: ['update', 'force_update', 'delete', 'rename'],
        minutes: 30,
      })
      setNotice(`Unlock granted for '${branch}'. It covers one change, then expires.`)
      await load()
    } catch (err) {
      setError(err.message || 'Could not request an unlock')
    } finally {
      setBusy(null)
    }
  }

  const branches = useMemo(() => Object.entries(data?.branches || {}), [data])

  if (loading) {
    return <GlassCard className="p-6" hover={false}>
      <p className="py-8 text-center text-sm text-muted">Loading branch protection…</p>
    </GlassCard>
  }

  return (
    <div className="space-y-4">
      {error && (
        <p className="flex items-start gap-2 rounded-xl border border-danger-fg/25 bg-danger-fg/10 px-4 py-3 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
      )}
      {notice && !error && (
        <p className="flex items-start gap-2 rounded-xl border border-success-fg/25 bg-success-fg/10 px-4 py-3 text-sm text-success-fg">
          <FiCheck className="mt-0.5 h-4 w-4 shrink-0" /> {notice}
        </p>
      )}

      <GlassCard className="p-4" hover={false}>
        <h3 className="mb-1 flex items-center gap-2 text-sm font-semibold text-ink">
          <FiShield className="h-4 w-4 text-muted" /> Branch protection
        </h3>
        <p className="mb-4 text-xs text-muted">
          Protection binds everyone, administrators included. A frozen branch needs an
          explicit, time-limited unlock before it will accept anything.
        </p>

        <div className="space-y-3">
          {branches.map(([name, info]) => (
            <div key={name} className="rounded-xl border border-border bg-white/[0.03] p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[13px] text-ink">{name}</span>
                <ModeBadge mode={info.mode} />
                {info.denied_operations?.length > 0 && (
                  <span className="text-[11px] text-muted">
                    denies {info.denied_operations.join(', ')}
                  </span>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px]">
                <span className="text-muted">Requires</span>
                <select
                  value={info.review?.required_approvals ?? 0}
                  disabled={busy === `${name}:approvals`}
                  onChange={(e) => setApprovals(name, info, Number(e.target.value))}
                  className="rounded-lg border border-border bg-white/[0.04] px-2 py-1 text-ink outline-none focus:border-accent/60 disabled:opacity-50"
                >
                  {[0, 1, 2, 3, 4, 5].map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
                <span className="text-muted">
                  approval(s){info.review?.require_code_owners ? ' plus code owners' : ''}
                </span>
                {info.requires_review && (
                  <span className="text-info-fg">
                    — merges must go through a reviewed pull request
                  </span>
                )}
                {info.requires_review && info.mode === 'open' && (
                  // Review rules gate merges, not pushes. A branch left open still
                  // accepts a direct push, so requiring approvals here without also
                  // setting Protected reads safer than it is.
                  <span className="w-full text-warning-fg">
                    This branch is still Open, so a direct push can bypass review
                    entirely. Set it to Protected to require the pull request.
                  </span>
                )}
              </div>

              <div className="mt-2 flex flex-wrap gap-1.5">
                {MODES.map((mode) => (
                  <button
                    key={mode.id}
                    type="button"
                    title={mode.blurb}
                    disabled={busy === `${name}:${mode.id}` || info.mode === mode.id}
                    onClick={() => setMode(name, mode.id)}
                    className={cn(
                      'rounded-lg border px-2.5 py-1 text-xs transition disabled:opacity-50',
                      info.mode === mode.id
                        ? 'border-accent/60 bg-accent/15 text-ink'
                        : 'border-border text-ink-soft hover:border-border-strong hover:text-ink'
                    )}
                  >
                    {mode.label}
                  </button>
                ))}
                {(info.mode === 'frozen' || info.mode === 'archived') && (
                  <Button
                    type="button"
                    onClick={() => requestUnlock(name)}
                    disabled={busy === `${name}:unlock`}
                    className="!px-2.5 !py-1 !text-xs"
                  >
                    Request unlock
                  </Button>
                )}
              </div>
            </div>
          ))}
          {!branches.length && (
            <p className="py-6 text-center text-sm text-muted">No branches yet.</p>
          )}
        </div>
      </GlassCard>

      <GlassCard className="p-4" hover={false}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-ink">Reference audit</h3>
          <span className={cn('text-[11px]', chainIntact ? 'text-muted' : 'text-danger-fg')}>
            {chainIntact ? 'hash chain intact' : 'hash chain broken — events were altered'}
          </span>
        </div>
        {!audit.length ? (
          <p className="py-6 text-center text-sm text-muted">Nothing recorded yet.</p>
        ) : (
          <ol className="space-y-1.5">
            {audit.map((event) => (
              <li key={event.id} className="flex flex-wrap items-baseline gap-2 text-[12px]">
                <span className={cn(
                  'rounded px-1.5 py-0.5 font-mono text-[10px]',
                  event.decision === 'denied'
                    ? 'bg-danger-fg/15 text-danger-fg'
                    : 'bg-success-fg/15 text-success-fg'
                )}>
                  {event.decision === 'denied' ? 'DENIED' : 'OK'}
                </span>
                <span className="font-mono text-ink-soft">{event.reference}</span>
                <span className="text-muted">{event.operation || event.event_type}</span>
                {event.error_code && (
                  <span className="font-mono text-[10.5px] text-danger-fg">{event.error_code}</span>
                )}
                {event.actor && <span className="text-muted">by {event.actor}</span>}
                <span className="ml-auto font-mono text-[10.5px] text-muted">
                  {(event.occurred_at || '').slice(0, 19).replace('T', ' ')}
                </span>
              </li>
            ))}
          </ol>
        )}
      </GlassCard>
    </div>
  )
}
