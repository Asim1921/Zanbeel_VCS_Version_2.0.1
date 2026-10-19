import React from 'react'
import { FiAlertTriangle, FiCheckCircle, FiGitCommit } from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import { relativeTime, absoluteTime } from '../../lib/time'
import { cn } from '../../lib/utils'

/**
 * Whether the database is at this server's revision and matches what the code will
 * query. Schema changes are versioned migrations now: applied once each, in one
 * transaction, after a copy of the database is taken -- so this also lists what
 * was applied, when, and where that copy is.
 */

export default function SchemaPanel({ data }) {
  const health = data?.schema
  const runs = data?.migrations || []
  if (!health) return <div className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />
  const revision = health.revision || {}

  return (
    <div className="space-y-4">
      <GlassCard
        hover={false}
        className={cn(
          'flex items-start gap-3 p-4',
          health.healthy ? 'border-success-fg/25 bg-success-bg' : 'border-danger-fg/25 bg-danger-bg'
        )}
      >
        {health.healthy ? (
          <FiCheckCircle className="mt-0.5 h-5 w-5 shrink-0 text-success-fg" />
        ) : (
          <FiAlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-danger-fg" />
        )}
        <div className="min-w-0">
          <p className={cn('font-medium', health.healthy ? 'text-success-fg' : 'text-danger-fg')}>
            {health.healthy
              ? `The schema is at ${revision.current || 'its revision'} and matches the models`
              : 'Schema drift — the database does not match the code'}
          </p>
          <p className="mt-1 text-xs text-muted">
            {health.tables_present} of {health.tables_expected} expected tables present.
            {health.revision_problem ? ` ${health.revision_problem}` : ''}
            {health.healthy
              ? ' Queries will find what they expect.'
              : ' The server refuses to start against this unless FOXNEST_ALLOW_SCHEMA_DRIFT is set.'}
          </p>
        </div>
      </GlassCard>

      {(health.missing_tables.length > 0 || health.missing_columns.length > 0
        || (health.missing_unique || []).length > 0) && (
        <GlassCard hover={false} className="space-y-3 p-4">
          {health.missing_tables.length > 0 && (
            <div>
              <p className="mb-1.5 text-[13px] font-medium text-ink-soft">Missing tables</p>
              <div className="flex flex-wrap gap-1.5">
                {health.missing_tables.map((t) => <span key={t} className="ref">{t}</span>)}
              </div>
            </div>
          )}
          {health.missing_columns.length > 0 && (
            <div>
              <p className="mb-1.5 text-[13px] font-medium text-ink-soft">Missing columns</p>
              <div className="space-y-1">
                {health.missing_columns.slice(0, 30).map((c) => (
                  <p key={`${c.table}.${c.column}`} className="font-mono text-[12px] text-muted">
                    {c.table}.{c.column} <span className="text-ink-soft">{c.type}</span>
                  </p>
                ))}
              </div>
            </div>
          )}
          {(health.missing_unique || []).length > 0 && (
            <div>
              <p className="mb-1.5 text-[13px] font-medium text-ink-soft">Missing uniqueness rules</p>
              <div className="space-y-1">
                {health.missing_unique.map((u) => (
                  <p key={`${u.table}:${u.columns.join(',')}`} className="font-mono text-[12px] text-muted">
                    {u.table}({u.columns.join(', ')})
                  </p>
                ))}
              </div>
            </div>
          )}
        </GlassCard>
      )}

      {(health.missing_indexes || []).length > 0 && (
        <GlassCard hover={false} className="p-4">
          <p className="mb-1.5 text-[13px] font-medium text-ink-soft">
            Indexes the models declare that the database lacks (slower, not wrong)
          </p>
          <div className="space-y-1">
            {health.missing_indexes.slice(0, 20).map((i) => (
              <p key={`${i.table}:${i.index}`} className="font-mono text-[12px] text-muted">
                {i.table}({i.columns.join(', ')})
              </p>
            ))}
          </div>
        </GlassCard>
      )}

      <GlassCard hover={false} className="space-y-2 p-4">
        <p className="text-[13px] font-medium text-ink-soft">Upgrades applied</p>
        {runs.length === 0 ? (
          <p className="text-xs text-muted">None recorded on this database yet.</p>
        ) : (
          <div className="space-y-2">
            {runs.map((run) => (
              <div key={`${run.finished_at}-${run.to_revision}`} className="flex items-start gap-2 text-[12px]">
                <FiGitCommit className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
                <div className="min-w-0">
                  <p className="text-ink-soft">
                    {run.from_revision || 'no revision'} → {run.to_revision}
                    <span className="text-muted" title={absoluteTime(run.finished_at)}>
                      {' '}· {relativeTime(run.finished_at)} · {run.duration_ms} ms
                    </span>
                  </p>
                  <p className="font-mono text-[11px] text-muted">{(run.revisions || []).join(', ')}</p>
                  {run.snapshot_path && (
                    <p className="break-all font-mono text-[10.5px] text-muted">copy taken before: {run.snapshot_path}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </GlassCard>

      <GlassCard hover={false} className="space-y-2 p-4">
        <p className="text-[13px] font-medium text-ink-soft">How schema changes work</p>
        <p className="text-xs text-muted">
          Every change is an Alembic revision, applied once, in order, inside one transaction: an
          upgrade completes or leaves the database exactly as it was. The server applies pending
          revisions when it starts, after taking a copy of the database, and refuses to start if one
          fails, if the database is behind with startup migration turned off, or if it was upgraded
          by a newer server.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`..\\venv\\Scripts\\python.exe tools/schema.py --status
..\\venv\\Scripts\\python.exe tools/schema.py --check
..\\venv\\Scripts\\python.exe tools/schema.py --upgrade
..\\venv\\Scripts\\python.exe -m alembic revision -m "add x"`}
        </pre>
      </GlassCard>
    </div>
  )
}
