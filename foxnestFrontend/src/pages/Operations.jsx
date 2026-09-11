import React, { useCallback, useEffect, useState } from 'react'
import {
  FiShield,
  FiDatabase,
  FiHardDrive,
  FiRefreshCw,
  FiAlertTriangle,
  FiCheckCircle,
  FiXCircle,
  FiUnlock,
  FiPlay,
  FiClock,
} from 'react-icons/fi'
import PageHeader from '../components/ui/PageHeader'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import Badge from '../components/ui/Badge'
import EmptyState from '../components/ui/EmptyState'
import api from '../utils/api'
import { cn } from '../lib/utils'

/**
 * Operations: the three things a shared deployment needs someone watching.
 *
 * Lockouts, schema health and backups sit on one screen because they are the
 * same job — knowing whether the server is in a state you would trust with the
 * team's source code. Splitting them across three places means nobody looks at
 * any of them.
 */

function bytes(n = 0) {
  const num = Number(n) || 0
  if (!num) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(num) / Math.log(1024)), units.length - 1)
  return `${(num / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function duration(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  const s = ms / 1000
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

const TABS = [
  { id: 'lockouts', label: 'Sign-in security', icon: FiShield },
  { id: 'backups', label: 'Backups', icon: FiHardDrive },
  { id: 'schema', label: 'Schema', icon: FiDatabase },
]

export default function Operations() {
  const [tab, setTab] = useState('lockouts')
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const [lockouts, setLockouts] = useState(null)
  const [backups, setBackups] = useState(null)
  const [schema, setSchema] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const [l, b, s] = await Promise.all([
        api.getLockouts().catch((e) => ({ __error: e.message })),
        api.listBackups().catch((e) => ({ __error: e.message })),
        api.getSchemaHealth().catch((e) => ({ __error: e.message })),
      ])
      setLockouts(l)
      setBackups(b)
      setSchema(s)
    } catch (err) {
      setError(err.message || 'Failed to load operations data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const unlock = async (username) => {
    try {
      setBusy(`unlock:${username}`)
      setError(null)
      await api.unlockAccount(username)
      setNotice(`${username} unlocked.`)
      await load()
    } catch (err) {
      setError(err.message || 'Unlock failed')
    } finally {
      setBusy(null)
    }
  }

  const runBackup = async () => {
    const ok = window.confirm(
      'Take a backup now?\n\n' +
        'This snapshots the database and copies every blob it references. ' +
        'On a large repository it takes minutes, and the page will wait for the result.'
    )
    if (!ok) return
    try {
      setBusy('backup')
      setError(null)
      setNotice(null)
      const response = await api.createBackup('manual')
      const b = response.backup
      setNotice(
        b.verified
          ? `Backup complete and verified — ${b.blob_count} objects, ${bytes(b.total_bytes)}.`
          : `Backup finished but did NOT verify: ${b.missing_blobs} blob(s) missing.`
      )
      await load()
    } catch (err) {
      setError(err.message || 'Backup failed')
    } finally {
      setBusy(null)
    }
  }

  const verify = async (id) => {
    try {
      setBusy(`verify:${id}`)
      setError(null)
      setNotice(null)
      const response = await api.verifyBackup(id)
      const v = response.verification
      setNotice(
        v.verified
          ? `Verified: all ${v.referenced_hashes} referenced objects are present.`
          : `NOT verified: ${v.missing} of ${v.referenced_hashes} objects missing.`
      )
      await load()
    } catch (err) {
      setError(err.message || 'Verification failed')
    } finally {
      setBusy(null)
    }
  }

  const locked = lockouts?.locked_accounts || []
  const policy = lockouts?.policy || {}
  const backupList = backups?.backups || []
  const health = schema?.schema

  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Shared deployment"
        title="Operations"
        subtitle="Sign-in protection, verified backups, and whether the database still matches the code."
        actions={
          <Button variant="secondary" onClick={load} className="flex items-center gap-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        }
      />

      {/* At-a-glance state, so a problem is visible without picking a tab. */}
      <div className="mb-6 grid gap-3 sm:grid-cols-3">
        <GlassCard hover={false} className="flex items-center gap-3 p-4">
          <FiShield className={cn('h-5 w-5 shrink-0', locked.length ? 'text-warning-fg' : 'text-success-fg')} />
          <div className="min-w-0">
            <p className="font-display text-xl font-light text-ink">{locked.length}</p>
            <p className="text-xs text-muted">locked account{locked.length === 1 ? '' : 's'}</p>
          </div>
        </GlassCard>
        <GlassCard hover={false} className="flex items-center gap-3 p-4">
          <FiHardDrive
            className={cn(
              'h-5 w-5 shrink-0',
              backupList.length && backupList[0].verified ? 'text-success-fg' : 'text-warning-fg'
            )}
          />
          <div className="min-w-0">
            <p className="truncate font-display text-xl font-light text-ink">
              {backupList.length ? (backupList[0].created_at || '').slice(0, 10) : 'never'}
            </p>
            <p className="text-xs text-muted">last backup</p>
          </div>
        </GlassCard>
        <GlassCard hover={false} className="flex items-center gap-3 p-4">
          <FiDatabase className={cn('h-5 w-5 shrink-0', health?.healthy ? 'text-success-fg' : 'text-danger-fg')} />
          <div className="min-w-0">
            <p className="font-display text-xl font-light text-ink">
              {health ? `${health.tables_present}/${health.tables_expected}` : '—'}
            </p>
            <p className="text-xs text-muted">tables verified</p>
          </div>
        </GlassCard>
      </div>

      <div className="mb-5 flex flex-wrap gap-2">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              'flex items-center gap-2 rounded-full border px-4 py-1.5 text-sm transition-colors',
              tab === id
                ? 'border-accent/50 bg-accent/10 text-ink'
                : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink'
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      {error && (
        <GlassCard className="mb-5 flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="mb-5 flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{notice}</span>
        </GlassCard>
      )}

      {/* ---------------- sign-in security ---------------- */}
      {tab === 'lockouts' && (
        <div className="space-y-4">
          <GlassCard hover={false} className="p-4">
            <p className="mb-2 text-[13px] font-medium text-ink-soft">Active policy</p>
            <div className="grid gap-3 text-[13px] text-muted sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <span className="block font-mono text-ink">{policy.max_account_failures ?? '—'}</span>
                failures per account
              </div>
              <div>
                <span className="block font-mono text-ink">{policy.account_window_minutes ?? '—'} min</span>
                counting window
              </div>
              <div>
                <span className="block font-mono text-ink">{policy.lockout_minutes ?? '—'} min</span>
                lockout duration
              </div>
              <div>
                <span className="block font-mono text-ink">{policy.max_ip_failures ?? '—'}</span>
                failures per address
              </div>
            </div>
          </GlassCard>

          {locked.length === 0 ? (
            <EmptyState
              icon={FiShield}
              title="Nothing is locked"
              description="Accounts lock automatically after repeated failures and unlock themselves when the window passes."
            />
          ) : (
            <div className="space-y-2">
              {locked.map((l) => (
                <GlassCard key={l.username} hover={false} className="flex flex-wrap items-center gap-3 p-4">
                  <FiClock className="h-4 w-4 shrink-0 text-warning-fg" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate font-medium text-ink">{l.username}</span>
                      <Badge variant="warning">
                        {Math.max(1, Math.round(l.seconds_remaining / 60))} min left
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-xs text-muted">
                      {l.reason}
                      {l.last_ip && ` · last attempt from ${l.last_ip}`}
                    </p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => unlock(l.username)}
                    disabled={busy === `unlock:${l.username}`}
                    className="flex items-center gap-1.5"
                  >
                    <FiUnlock className="h-3.5 w-3.5" />
                    Unlock
                  </Button>
                </GlassCard>
              ))}
            </div>
          )}

          {(lockouts?.recent_failures_by_ip || []).length > 0 && (
            <GlassCard hover={false} className="p-4">
              <p className="mb-2 text-[13px] font-medium text-ink-soft">
                Failures in the last {lockouts.window_minutes} minutes, by source
              </p>
              <div className="space-y-1">
                {lockouts.recent_failures_by_ip.slice(0, 8).map((r) => (
                  <div key={r.ip_address} className="flex items-center justify-between font-mono text-[12px]">
                    <span className="text-ink-soft">{r.ip_address}</span>
                    <span className="text-muted">{r.failures}</span>
                  </div>
                ))}
              </div>
            </GlassCard>
          )}
        </div>
      )}

      {/* ---------------- backups ---------------- */}
      {tab === 'backups' && (
        <div className="space-y-4">
          <GlassCard hover={false} className="flex flex-wrap items-center justify-between gap-3 p-4">
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-ink-soft">
                The database and the blob store are backed up together
              </p>
              <p className="mt-1 text-xs text-muted">
                Rows hold only hashes, so a database copy without its blobs restores an
                index pointing at nothing. Every backup is verified before it is called good.
              </p>
              {backups?.destination && (
                <p className="mt-1.5 break-all font-mono text-[11px] text-muted">
                  {backups.destination}
                </p>
              )}
            </div>
            <Button onClick={runBackup} disabled={busy === 'backup'} className="flex shrink-0 items-center gap-2">
              <FiPlay className={cn('h-4 w-4', busy === 'backup' && 'animate-pulse')} />
              {busy === 'backup' ? 'Backing up…' : 'Back up now'}
            </Button>
          </GlassCard>

          {loading ? (
            <div className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />
          ) : backupList.length === 0 ? (
            <EmptyState
              icon={FiHardDrive}
              title="No backups yet"
              description="Nothing has been backed up. The CLI equivalent is: py -3.11 tools/backup.py --create"
              action={<Button onClick={runBackup}>Take the first backup</Button>}
            />
          ) : (
            <div className="space-y-2">
              {backupList.map((b) => (
                <GlassCard key={b.id} hover={false} className="flex flex-wrap items-center gap-3 p-4">
                  {b.verified ? (
                    <FiCheckCircle className="h-4 w-4 shrink-0 text-success-fg" />
                  ) : (
                    <FiXCircle className="h-4 w-4 shrink-0 text-danger-fg" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-ink">{b.label}</span>
                      {b.verified ? (
                        <Badge variant="success">Verified</Badge>
                      ) : (
                        <Badge variant="danger">Unverified</Badge>
                      )}
                      <span className="text-xs text-muted">
                        {(b.created_at || '').slice(0, 19).replace('T', ' ')}
                      </span>
                    </div>
                    <p className="mt-1 flex flex-wrap gap-x-3 font-mono text-[11px] text-muted">
                      <span>db {bytes(b.database_bytes)}</span>
                      <span>blobs {bytes(b.blob_bytes)} / {b.blob_count} objects</span>
                      <span>{duration(b.duration_ms)}</span>
                      {b.missing_blobs > 0 && (
                        <span className="text-danger-fg">{b.missing_blobs} missing</span>
                      )}
                    </p>
                    <p className="mt-1 break-all font-mono text-[10.5px] text-muted">{b.path}</p>
                    {b.error && (
                      <p className="mt-1 text-[11px] text-warning-fg">{b.error}</p>
                    )}
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => verify(b.id)}
                    disabled={busy === `verify:${b.id}`}
                  >
                    {busy === `verify:${b.id}` ? 'Checking…' : 'Re-verify'}
                  </Button>
                </GlassCard>
              ))}
            </div>
          )}

          <GlassCard hover={false} className="space-y-2 p-4">
            <p className="text-[13px] font-medium text-ink-soft">Restoring</p>
            <p className="text-xs text-muted">
              Restore is deliberately CLI-only: it replaces the live database, so it should
              be a considered act at a terminal rather than a button someone can hit by
              accident. It refuses unless the backup verifies, and moves the current
              database aside instead of deleting it.
            </p>
            <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`py -3.11 tools/backup.py --list
py -3.11 tools/backup.py --verify <path>
py -3.11 tools/backup.py --restore <path>`}
            </pre>
          </GlassCard>
        </div>
      )}

      {/* ---------------- schema ---------------- */}
      {tab === 'schema' && (
        <div className="space-y-4">
          {!health ? (
            <div className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />
          ) : (
            <>
              <GlassCard
                hover={false}
                className={cn(
                  'flex items-start gap-3 p-4',
                  health.healthy
                    ? 'border-success-fg/25 bg-success-bg'
                    : 'border-danger-fg/25 bg-danger-bg'
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
                      ? 'The live schema matches the models'
                      : 'Schema drift — the database does not match the code'}
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {health.tables_present} of {health.tables_expected} expected tables present.
                    {health.healthy
                      ? ' Queries will find what they expect.'
                      : ' Queries against the objects below will fail at request time.'}
                  </p>
                </div>
              </GlassCard>

              {!health.healthy && (
                <GlassCard hover={false} className="space-y-3 p-4">
                  {health.missing_tables.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-[13px] font-medium text-ink-soft">Missing tables</p>
                      <div className="flex flex-wrap gap-1.5">
                        {health.missing_tables.map((t) => (
                          <span key={t} className="ref">{t}</span>
                        ))}
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
                </GlassCard>
              )}

              <GlassCard hover={false} className="space-y-2 p-4">
                <p className="text-[13px] font-medium text-ink-soft">How this is checked</p>
                <p className="text-xs text-muted">
                  The startup patches each swallow their own errors, so &ldquo;nothing
                  raised&rdquo; never meant the schema was right. The server now asks the
                  database what it actually has and refuses to start on drift. Schema
                  changes from here on are Alembic revisions.
                </p>
                <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`py -3.11 tools/schema.py --check
py -3.11 tools/schema.py --current
py -3.11 -m alembic revision --autogenerate -m "add x"`}
                </pre>
              </GlassCard>
            </>
          )}
        </div>
      )}
    </div>
  )
}
