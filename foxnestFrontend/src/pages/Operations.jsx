import React, { useCallback, useEffect, useState } from 'react'
import {
  FiShield,
  FiDatabase,
  FiHardDrive,
  FiRefreshCw,
  FiAlertTriangle,
  FiCheckCircle,
  FiKey,
  FiSearch,
} from 'react-icons/fi'
import PageHeader from '../components/ui/PageHeader'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import CredentialsOverview from '../components/operations/CredentialsOverview'
import SearchIndexPanel from '../components/operations/SearchIndexPanel'
import SignInSecurityPanel from '../components/operations/SignInSecurityPanel'
import BackupsPanel from '../components/operations/BackupsPanel'
import SchemaPanel from '../components/operations/SchemaPanel'
import api from '../utils/api'
import { cn } from '../lib/utils'

/**
 * Operations: the things a shared deployment needs someone watching.
 *
 * Sign-in protection, credentials, backups and schema health sit on one screen because
 * they are the same job — knowing whether the server is in a state you would trust with
 * the team's source code. Splitting them across places means nobody looks at any of them.
 */

const TABS = [
  { id: 'lockouts', label: 'Sign-in security', icon: FiShield },
  { id: 'credentials', label: 'Credentials', icon: FiKey },
  { id: 'backups', label: 'Backups', icon: FiHardDrive },
  { id: 'schema', label: 'Schema', icon: FiDatabase },
  { id: 'search', label: 'Search index', icon: FiSearch },
]

function backupAge(summary) {
  const hours = summary?.last_verified_age_hours
  if (hours == null) return 'never'
  if (hours < 1) return '< 1 h ago'
  if (hours < 48) return `${Math.round(hours)} h ago`
  return `${Math.round(hours / 24)} d ago`
}

export default function Operations() {
  const [tab, setTab] = useState('lockouts')
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const [lockouts, setLockouts] = useState(null)
  const [backups, setBackups] = useState(null)
  const [schema, setSchema] = useState(null)
  const [loading, setLoading] = useState(true)

  const loadLockouts = useCallback(async () => {
    setLockouts(await api.getLockouts().catch((e) => ({ __error: e.message })))
  }, [])
  const loadBackups = useCallback(async () => {
    setBackups(await api.listBackups().catch((e) => ({ __error: e.message })))
  }, [])
  const loadSchema = useCallback(async () => {
    setSchema(await api.getSchemaHealth().catch((e) => ({ __error: e.message })))
  }, [])

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      await Promise.all([loadLockouts(), loadBackups(), loadSchema()])
    } catch (err) {
      setError(err.message || 'Failed to load operations data')
    } finally {
      setLoading(false)
    }
  }, [loadLockouts, loadBackups, loadSchema])

  useEffect(() => {
    load()
  }, [load])

  const locked = lockouts?.locked_accounts || []
  const throttled = (lockouts?.throttled_addresses || []).length + (lockouts?.throttled_pairs || []).length
  const summary = backups?.summary
  const health = schema?.schema
  const panelError = [lockouts, backups, schema].find((d) => d?.__error)?.__error

  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Shared deployment"
        title="Operations"
        subtitle="Sign-in protection, who holds which credentials, verified backups, and whether the database still matches the code."
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
          <FiShield className={cn('h-5 w-5 shrink-0', locked.length || throttled ? 'text-warning-fg' : 'text-success-fg')} />
          <div className="min-w-0">
            <p className="font-display text-xl font-light text-ink">{locked.length}</p>
            <p className="text-xs text-muted">
              locked account{locked.length === 1 ? '' : 's'}
              {throttled ? ` · ${throttled} throttled` : ''}
            </p>
          </div>
        </GlassCard>
        <GlassCard hover={false} className="flex items-center gap-3 p-4">
          <FiHardDrive
            className={cn('h-5 w-5 shrink-0', summary && !summary.stale ? 'text-success-fg' : 'text-warning-fg')}
          />
          <div className="min-w-0">
            <p className="truncate font-display text-xl font-light text-ink">{backupAge(summary)}</p>
            <p className="text-xs text-muted">
              last verified backup
              {summary?.last_restore_test?.status ? ` · drill ${summary.last_restore_test.status}` : ''}
            </p>
          </div>
        </GlassCard>
        <GlassCard hover={false} className="flex items-center gap-3 p-4">
          <FiDatabase className={cn('h-5 w-5 shrink-0', health?.healthy ? 'text-success-fg' : 'text-danger-fg')} />
          <div className="min-w-0">
            <p className="truncate font-display text-xl font-light text-ink">
              {health ? (health.revision?.current || '').split('_')[0] || `${health.tables_present}/${health.tables_expected}` : '—'}
            </p>
            <p className="text-xs text-muted">{health?.healthy ? 'schema revision, verified' : 'schema needs attention'}</p>
          </div>
        </GlassCard>
      </div>

      <div className="mb-5 flex flex-wrap gap-2">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => {
              setTab(id)
              setNotice(null) // a notice belongs to the tab whose action it reports
            }}
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

      {(error || panelError) && (
        <GlassCard className="mb-5 flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error || panelError}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="mb-5 flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{notice}</span>
        </GlassCard>
      )}

      {tab === 'lockouts' && (
        <SignInSecurityPanel data={lockouts} reload={loadLockouts} onNotice={setNotice} onError={setError} />
      )}
      {tab === 'credentials' && <CredentialsOverview />}
      {tab === 'backups' && (
        <BackupsPanel data={backups} loading={loading} reload={loadBackups} onNotice={setNotice} onError={setError} />
      )}
      {tab === 'schema' && <SchemaPanel data={schema} />}
      {tab === 'search' && <SearchIndexPanel />}
    </div>
  )
}
