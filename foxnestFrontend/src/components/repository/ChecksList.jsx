import React from 'react'
import {
  FiCheckCircle,
  FiXCircle,
  FiClock,
  FiAlertTriangle,
  FiExternalLink,
  FiShield,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Badge from '../ui/Badge'
import { cn } from '../../lib/utils'

/**
 * The checks on one commit: the required ones first, each with where it stands,
 * then anything else that reported.
 *
 * The distinction this has to make loudly is between a check that *failed* and one
 * that has *not reported at all* — the second looks like nothing is wrong, and is
 * exactly the case the gate exists to catch when CI fails to start. A report that
 * did not count (a pinned check reported by someone else) says so, with who.
 *
 * `combined` is the server's /statuses response: statuses plus required_checks.
 */

const STATE_META = {
  success: { icon: FiCheckCircle, tone: 'text-success-fg', label: 'Passed' },
  failure: { icon: FiXCircle, tone: 'text-danger-fg', label: 'Failed' },
  error: { icon: FiAlertTriangle, tone: 'text-danger-fg', label: 'Errored' },
  pending: { icon: FiClock, tone: 'text-warning-fg', label: 'Running' },
}

function Details({ url }) {
  if (!url) return null
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      className="flex shrink-0 items-center gap-1 text-xs text-accent hover:underline"
    >
      Details
      <FiExternalLink className="h-3 w-3" />
    </a>
  )
}

export default function ChecksList({ combined, emptyHint }) {
  const required = combined?.required_checks || []
  const requiredNames = new Set(required.map((entry) => entry.context))
  const others = (combined?.statuses || []).filter((s) => !requiredNames.has(s.context))

  if (!required.length && !others.length) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-white/[0.02] p-3 text-xs text-muted">
        <FiShield className="h-4 w-4 shrink-0" />
        {emptyHint || 'Nothing has reported on this commit.'}
      </div>
    )
  }

  return (
    <div className="space-y-2" data-testid="checks-list">
      {required.map((entry) => {
        const status = entry.status
        const meta = status ? STATE_META[status.state] || STATE_META.pending : null
        const Icon = meta ? meta.icon : FiClock
        return (
          <GlassCard
            key={`required-${entry.context}`}
            hover={false}
            data-testid="required-check"
            className={cn('flex items-center gap-3 p-3', !status && 'border-warning-fg/25')}
          >
            <Icon className={cn('h-4 w-4 shrink-0', meta ? meta.tone : 'text-warning-fg')} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="truncate font-mono text-[13px] text-ink">{entry.context}</span>
                <Badge variant="info">Required</Badge>
                {entry.reporter && (
                  <span className="text-[11px] text-muted">counts only from {entry.reporter}</span>
                )}
              </div>
              {status ? (
                <p className="mt-0.5 truncate text-xs text-muted">
                  {meta.label}
                  {status.description && ` — ${status.description}`}
                  {status.reported_by && ` · by ${status.reported_by}`}
                </p>
              ) : (
                <p className="mt-0.5 text-xs text-warning-fg">
                  {entry.ignored_report
                    ? `${entry.ignored_report.reported_by} reported ${entry.ignored_report.state}, but only ${entry.reporter} counts. Waiting for ${entry.reporter}.`
                    : 'Has not reported. A required check that never runs blocks the merge.'}
                </p>
              )}
            </div>
            <Details url={status?.target_url} />
          </GlassCard>
        )
      })}

      {others.map((status) => {
        const meta = STATE_META[status.state] || STATE_META.pending
        const Icon = meta.icon
        return (
          <GlassCard key={status.id} hover={false} className="flex items-center gap-3 p-3">
            <Icon className={cn('h-4 w-4 shrink-0', meta.tone)} />
            <div className="min-w-0 flex-1">
              <span className="truncate font-mono text-[13px] text-ink">{status.context}</span>
              <p className="mt-0.5 truncate text-xs text-muted">
                {meta.label}
                {status.description && ` — ${status.description}`}
                {status.reported_by && ` · by ${status.reported_by}`}
              </p>
            </div>
            <Details url={status.target_url} />
          </GlassCard>
        )
      })}
    </div>
  )
}
