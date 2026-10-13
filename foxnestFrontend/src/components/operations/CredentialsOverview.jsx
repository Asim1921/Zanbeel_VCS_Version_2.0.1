import React, { useCallback, useEffect, useState } from 'react'
import { FiKey, FiTerminal, FiSlash, FiAlertTriangle, FiCheckCircle, FiUsers } from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Which machines can act as whom.
 *
 * The first question after a laptop goes missing, and until now one only the
 * owner could answer. Metadata only: no screen and no endpoint shows a token's
 * value. "Revoke all" ends every token and removes every key for one person
 * without disabling their account — they sign in with their password and make
 * fresh ones. There is deliberately no way to *create* a credential for someone.
 */

const scopeText = (scopes = []) =>
  [
    scopes.includes('repo:write') || scopes.includes('admin')
      ? 'read & write'
      : scopes.includes('repo:status')
        ? 'read & report statuses'
        : 'read only',
    scopes.includes('admin') && 'admin',
  ]
    .filter(Boolean)
    .join(' · ')

// Server timestamps are naive UTC; parseServerTimestamp reads them as such.
const day = (iso) => api.parseServerTimestamp(iso)?.toLocaleDateString() ?? null

export default function CredentialsOverview() {
  const [entries, setEntries] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    try {
      setError(null)
      const response = await api.listAllCredentials()
      setEntries(response.users || [])
    } catch (err) {
      setError(err.message || 'Failed to load credentials')
      setEntries([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const revokeAll = async (username, tokens, keys) => {
    const ok = window.confirm(
      `Revoke every credential ${username} holds?\n\n` +
        `${tokens} access token(s) stop working and ${keys} SSH key(s) are removed, ` +
        'immediately, on every machine. Their account stays active: they can sign in ' +
        'with their password and create new ones. They will be notified.'
    )
    if (!ok) return
    try {
      setBusy(username)
      setError(null)
      const response = await api.revokeUserCredentials(username)
      setNotice(
        `${username}: ${response.revoked_tokens} token(s) revoked, ` +
          `${response.removed_ssh_keys} SSH key(s) removed.`
      )
      await load()
    } catch (err) {
      setError(err.message || 'Revocation failed')
    } finally {
      setBusy(null)
    }
  }

  if (entries === null) {
    return <div className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />
  }

  return (
    <div className="space-y-4" data-testid="credentials-overview">
      <p className="max-w-3xl text-sm text-muted">
        Every live access token and registered SSH key, by person. Use{' '}
        <span className="text-ink-soft">Revoke all</span> when a machine is lost or a credential
        may have leaked; it does not lock the account.
      </p>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{notice}</span>
        </GlassCard>
      )}

      {entries.length === 0 ? (
        <EmptyState
          icon={FiUsers}
          title="No credentials in use"
          description="Nobody holds an access token or an SSH key. People who sign in from the CLI will appear here."
        />
      ) : (
        entries.map(({ user, tokens, ssh_keys: keys }) => (
          <GlassCard key={user.id} hover={false} className="space-y-3 p-4" data-testid="credential-holder">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{user.username}</span>
                {user.full_name && <span className="text-xs text-muted">{user.full_name}</span>}
                <Badge>{user.role}</Badge>
                {!user.is_active && <Badge variant="danger">disabled</Badge>}
              </div>
              <Button
                variant="danger"
                size="sm"
                disabled={busy === user.username}
                onClick={() => revokeAll(user.username, tokens.length, keys.length)}
                className="flex items-center gap-1.5"
              >
                <FiSlash className="h-3.5 w-3.5" />
                {busy === user.username ? 'Revoking…' : 'Revoke all'}
              </Button>
            </div>

            <div className="grid gap-2 lg:grid-cols-2">
              <div className="space-y-1.5">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  Access tokens ({tokens.length})
                </p>
                {tokens.length === 0 && <p className="text-xs text-muted">None</p>}
                {tokens.map((token) => (
                  <div
                    key={token.id}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-white/[0.02] px-3 py-2 text-xs"
                  >
                    <FiKey className="h-3.5 w-3.5 shrink-0 text-accent" />
                    <span className="text-ink">{token.name}</span>
                    <span className="font-mono text-muted">{token.prefix}…</span>
                    <span className="text-muted">{scopeText(token.scopes)}</span>
                    <span className="text-muted">
                      {token.expires_at ? `expires ${day(token.expires_at)}` : 'no expiry'}
                    </span>
                    <span className="text-muted">
                      {token.last_used_at ? `used ${day(token.last_used_at)}` : 'never used'}
                    </span>
                  </div>
                ))}
              </div>

              <div className="space-y-1.5">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  SSH keys ({keys.length})
                </p>
                {keys.length === 0 && <p className="text-xs text-muted">None</p>}
                {keys.map((key) => (
                  <div
                    key={key.id}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-white/[0.02] px-3 py-2 text-xs"
                  >
                    <FiTerminal className="h-3.5 w-3.5 shrink-0 text-accent" />
                    <span className="text-ink">{key.title}</span>
                    <span className="text-muted">{scopeText(key.scopes)}</span>
                    <span className="min-w-0 truncate font-mono text-muted">{key.fingerprint}</span>
                    {key.active_sessions > 0 && (
                      <span className="text-ink-soft">
                        {key.active_sessions} active sign-in{key.active_sessions === 1 ? '' : 's'}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </GlassCard>
        ))
      )}
    </div>
  )
}
