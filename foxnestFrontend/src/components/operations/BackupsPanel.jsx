import React, { useEffect, useRef, useState } from 'react'
import {
  FiAlertTriangle,
  FiCheckCircle,
  FiChevronDown,
  FiChevronRight,
  FiClock,
  FiHardDrive,
  FiLink,
  FiPlay,
  FiRotateCcw,
  FiShield,
  FiXCircle,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'
import { absoluteTime, relativeTime } from '../../lib/time'
import { cn } from '../../lib/utils'

/**
 * Backups, as the server now takes them: jobs that read every blob back, hard-link
 * what the previous backup already holds, and can be restored into a scratch
 * directory to prove they work. A backup nobody has restored is a hope; the drill
 * is the evidence.
 */

export function bytes(n = 0) {
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

function ageText(hours) {
  if (hours == null) return 'never'
  if (hours < 1) return 'under an hour ago'
  if (hours < 48) return `${Math.round(hours)} hours ago`
  return `${Math.round(hours / 24)} days ago`
}

export function isRunning(b) {
  return b.status === 'running' || b.verify_status === 'running' || b.restore_test_status === 'running'
}

function StatusBadge({ backup }) {
  if (backup.status === 'running') return <Badge variant="info">Backing up</Badge>
  if (backup.status === 'failed') return <Badge variant="danger">Failed</Badge>
  if (backup.status === 'interrupted') return <Badge variant="danger">Interrupted</Badge>
  if (backup.verify_status === 'running') return <Badge variant="info">Verifying</Badge>
  return backup.verified ? <Badge variant="success">Verified</Badge> : <Badge variant="danger">Not verified</Badge>
}

function DrillBadge({ backup }) {
  const state = backup.restore_test_status
  if (state === 'running') return <Badge variant="info">Restore drill running</Badge>
  if (state === 'passed') return <Badge variant="success">Restore drill passed</Badge>
  if (state === 'failed') return <Badge variant="danger">Restore drill failed</Badge>
  return <Badge>Never test-restored</Badge>
}

function Report({ title, report }) {
  if (!report) return null
  const problems = report.problems || []
  return (
    <div className="rounded-lg border border-border bg-black/20 p-3 text-[12px] text-muted">
      <p className="mb-1.5 font-medium text-ink-soft">{title}</p>
      {problems.length === 0 ? (
        <p className="text-success-fg">No problems found.</p>
      ) : (
        <ul className="list-disc space-y-0.5 pl-4 text-danger-fg">
          {problems.map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}
      <div className="mt-2 grid gap-x-4 gap-y-0.5 font-mono text-[11px] sm:grid-cols-2">
        {report.referenced_hashes != null && <span>referenced blobs: {report.referenced_hashes}</span>}
        {report.present != null && <span>present: {report.present}</span>}
        {report.missing != null && <span>missing: {report.missing}</span>}
        {report.corrupt != null && <span>corrupt: {report.corrupt}</span>}
        {report.integrity && <span>database integrity: {report.integrity}</span>}
        {report.schema_revision && <span>schema: {report.schema_revision}</span>}
        {report.upgraded_through && report.upgraded_through.length > 0 && (
          <span>upgraded through: {report.upgraded_through.join(', ')}</span>
        )}
        {report.branches_checked != null && <span>branch heads checked: {report.branches_checked}</span>}
        {report.head_files_checked != null && <span>files read back: {report.head_files_checked}</span>}
        {report.restore?.restored_blobs != null && <span>blobs restored: {report.restore.restored_blobs}</span>}
        {report.duration_ms != null && <span>took: {duration(report.duration_ms)}</span>}
      </div>
    </div>
  )
}

function BackupRow({ backup, busy, onVerify, onDrill }) {
  const [open, setOpen] = useState(false)
  const running = isRunning(backup)
  const linked = backup.linked_blobs || 0
  return (
    <GlassCard hover={false} className="p-4">
      <div className="flex flex-wrap items-start gap-3">
        {backup.status === 'complete' && backup.verified ? (
          <FiCheckCircle className="mt-0.5 h-4 w-4 shrink-0 text-success-fg" />
        ) : running ? (
          <FiClock className="mt-0.5 h-4 w-4 shrink-0 animate-pulse text-info-fg" />
        ) : (
          <FiXCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger-fg" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-ink">{backup.label}</span>
            <StatusBadge backup={backup} />
            {backup.status === 'complete' && <DrillBadge backup={backup} />}
            {backup.kind === 'scheduled' && <Badge variant="accent">Scheduled</Badge>}
            <span className="text-xs text-muted" title={absoluteTime(backup.created_at)}>
              {relativeTime(backup.created_at)}
              {backup.created_by ? ` · ${backup.created_by}` : ''}
            </span>
          </div>
          {backup.status !== 'running' && (
            <p className="mt-1 flex flex-wrap gap-x-3 font-mono text-[11px] text-muted">
              <span>db {bytes(backup.database_bytes)}</span>
              <span>blobs {bytes(backup.blob_bytes)} / {backup.blob_count} objects</span>
              {linked > 0 && (
                <span className="inline-flex items-center gap-1" title="Unchanged blobs are hard-linked from the previous verified backup instead of copied again">
                  <FiLink className="h-3 w-3" />{linked} linked
                </span>
              )}
              <span>{duration(backup.duration_ms)}</span>
              {backup.schema_revision && <span>schema {backup.schema_revision.split('_')[0]}</span>}
              {backup.format === 1 && <span title="Taken before backups were verified by reading every blob back">format 1</span>}
            </p>
          )}
          <p className="mt-1 break-all font-mono text-[10.5px] text-muted">{backup.path}</p>
          {backup.error && <p className="mt-1 text-[11px] text-warning-fg">{backup.error}</p>}
        </div>
        {backup.status === 'complete' && (
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={() => onVerify(backup)} disabled={busy || running}>
              {backup.verify_status === 'running' ? 'Verifying…' : 'Re-verify'}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => onDrill(backup)} disabled={busy || running}
              className="flex items-center gap-1.5">
              <FiRotateCcw className="h-3.5 w-3.5" />
              {backup.restore_test_status === 'running' ? 'Restoring…' : 'Test restore'}
            </Button>
          </div>
        )}
      </div>
      {(backup.verify_report || backup.restore_test_report) && (
        <div className="mt-2">
          <button type="button" onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-1 text-[12px] text-ink-soft hover:text-ink">
            {open ? <FiChevronDown className="h-3.5 w-3.5" /> : <FiChevronRight className="h-3.5 w-3.5" />}
            Reports
          </button>
          {open && (
            <div className="mt-2 grid gap-2 lg:grid-cols-2">
              <Report
                title={`Verification${backup.verified_at ? ` · ${relativeTime(backup.verified_at)}` : ''}`}
                report={backup.verify_report}
              />
              <Report
                title={`Restore drill${backup.restore_tested_at ? ` · ${relativeTime(backup.restore_tested_at)}` : ''}`}
                report={backup.restore_test_report}
              />
            </div>
          )}
        </div>
      )}
    </GlassCard>
  )
}

export default function BackupsPanel({ data, loading, reload, onNotice, onError }) {
  const [busy, setBusy] = useState(false)
  const [drillAfter, setDrillAfter] = useState(true)
  const watching = useRef(null) // { id, job } the user started, to announce when it ends
  const list = data?.backups || []
  const summary = data?.summary || {}
  const anyRunning = list.some(isRunning) || !!summary.running

  // Jobs run on the server; while one does, refresh until it finishes.
  useEffect(() => {
    if (!anyRunning) return undefined
    const timer = setInterval(reload, 2000)
    return () => clearInterval(timer)
  }, [anyRunning, reload])

  useEffect(() => {
    const watched = watching.current
    if (!watched) return
    const row = list.find((b) => b.id === watched.id)
    if (!row || isRunning(row)) return
    // Between the backup finishing and its drill starting, the row looks idle; wait for
    // the drill's own result rather than announcing one that has not run.
    if (watched.job === 'backup' && watched.drill && row.status === 'complete' && row.verified
        && !['passed', 'failed'].includes(row.restore_test_status)) return
    watching.current = null
    if (watched.job === 'backup') {
      if (row.status !== 'complete') onError(`The backup failed: ${row.error || row.status}`)
      else if (!row.verified) onError(`The backup finished but did not verify: ${row.error || 'see its report'}`)
      else if (watched.drill && row.restore_test_status === 'failed') {
        onError('The backup verified, but its restore drill failed. Open its reports.')
      } else {
        onNotice(`Backup verified — ${row.blob_count} objects (${row.linked_blobs || 0} linked from the previous backup), ${bytes(row.total_bytes)}`
          + (watched.drill ? ', and its restore drill passed.' : '.'))
      }
    } else if (watched.job === 'verify') {
      if (row.verify_status === 'passed') onNotice(`Verified: every blob in "${row.label}" read back and matched its hash.`)
      else onError(`"${row.label}" did not verify. Open its reports.`)
    } else if (watched.job === 'drill') {
      if (row.restore_test_status === 'passed') {
        onNotice(`Restore drill passed: "${row.label}" restored into a scratch directory, upgraded and read back.`)
      } else onError(`The restore drill for "${row.label}" failed. Open its reports.`)
    }
  }, [list, onNotice, onError])

  const start = async (job, call, extra = {}) => {
    try {
      setBusy(true)
      onError(null)
      onNotice(null)
      const response = await call()
      if (response?.backup?.id) watching.current = { id: response.backup.id, job, ...extra }
      await reload()
    } catch (err) {
      onError(err.message || 'Could not start the job')
    } finally {
      setBusy(false)
    }
  }

  const runBackup = () => start('backup', () => api.createBackup('manual', { testRestore: drillAfter }), { drill: drillAfter })
  const verify = (backup) => start('verify', () => api.verifyBackup(backup.id))
  const drill = (backup) => start('drill', () => api.restoreTestBackup(backup.id))

  const schedule = summary.schedule || {}
  const last = summary.last_verified
  const lastDrill = summary.last_restore_test

  return (
    <div className="space-y-4">
      <GlassCard hover={false} className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[13px] font-medium text-ink-soft">
              The database, its blobs and the generated documents, backed up together
            </p>
            <p className="mt-1 text-xs text-muted">
              Rows hold only hashes, so the parts are worthless apart. A backup is verified by
              reading every blob back and checking it against its hash; blobs the previous
              backup already holds are hard-linked, so each backup costs only what changed.
            </p>
            {data?.destination && (
              <p className="mt-1.5 break-all font-mono text-[11px] text-muted">{data.destination}</p>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
            <Button onClick={runBackup} disabled={busy || anyRunning} className="flex items-center gap-2">
              <FiPlay className={cn('h-4 w-4', anyRunning && 'animate-pulse')} />
              {anyRunning ? 'A job is running…' : 'Back up now'}
            </Button>
            <label className="flex items-center gap-1.5 text-[12px] text-muted">
              <input type="checkbox" checked={drillAfter} onChange={(e) => setDrillAfter(e.target.checked)} />
              then test-restore it
            </label>
          </div>
        </div>

        <div className="grid gap-3 text-[12px] text-muted sm:grid-cols-3">
          <div className={cn('rounded-lg border p-2.5', summary.stale ? 'border-warning-fg/30 bg-warning-bg' : 'border-border')}>
            <span className="block font-mono text-ink">{ageText(summary.last_verified_age_hours)}</span>
            last verified backup{summary.stale ? ` — older than ${summary.stale_after_hours}h` : ''}
          </div>
          <div className="rounded-lg border border-border p-2.5">
            <span className="block font-mono text-ink">
              {lastDrill ? `${lastDrill.status} · ${relativeTime(lastDrill.at)}` : 'never'}
            </span>
            last restore drill
          </div>
          <div className="rounded-lg border border-border p-2.5">
            <span className="block font-mono text-ink">
              {schedule.interval_hours ? `every ${schedule.interval_hours}h` : 'off'}
            </span>
            scheduled backups · keeping {schedule.keep ?? '—'} verified
            {schedule.interval_hours && schedule.test_restore ? ' · each one test-restored' : ''}
          </div>
        </div>

        {summary.same_volume_as_data && (
          <p className="flex items-start gap-2 text-[12px] text-warning-fg">
            <FiAlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Backups are on the same disk as the live data, so losing that disk loses both. Set
            FOXNEST_BACKUP_DIR to another disk, or copy backups off this machine.
          </p>
        )}
        {!schedule.interval_hours && (
          <p className="flex items-start gap-2 text-[12px] text-muted">
            <FiClock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Nothing takes backups on a schedule. Set FOXNEST_BACKUP_INTERVAL_HOURS (24 is usual)
            and the server takes, verifies and test-restores one itself.
          </p>
        )}
        {last == null && (
          <p className="flex items-start gap-2 text-[12px] text-danger-fg">
            <FiShield className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            There is no verified backup of this server.
          </p>
        )}
      </GlassCard>

      {loading && !data ? (
        <div className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />
      ) : list.length === 0 ? (
        <EmptyState
          icon={FiHardDrive}
          title="No backups yet"
          description="Nothing has been backed up. The CLI equivalent is: ..\venv\Scripts\python.exe tools/backup.py --create --drill"
          action={<Button onClick={runBackup} disabled={busy || anyRunning}>Take the first backup</Button>}
        />
      ) : (
        <div className="space-y-2">
          {list.map((b) => (
            <BackupRow key={b.id} backup={b} busy={busy || anyRunning} onVerify={verify} onDrill={drill} />
          ))}
        </div>
      )}

      <GlassCard hover={false} className="space-y-2 p-4">
        <p className="text-[13px] font-medium text-ink-soft">Restoring</p>
        <p className="text-xs text-muted">
          A real restore is deliberately a terminal step: it replaces the live database, so it should
          be a considered act with the server stopped, not a button. It refuses a backup that does not
          verify, moves everything it replaces aside rather than deleting it, and the server upgrades
          an older backup to its schema when it starts. The restore drill above runs the same code
          into a scratch directory, so a backup that passed it is one this will restore.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`..\\venv\\Scripts\\python.exe tools/backup.py --list
..\\venv\\Scripts\\python.exe tools/backup.py --test-restore <id>
..\\venv\\Scripts\\python.exe tools/backup.py --restore <id>`}
        </pre>
      </GlassCard>
    </div>
  )
}
