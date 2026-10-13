import React, { useCallback, useEffect, useState } from 'react'
import {
  FiKey,
  FiPlus,
  FiTrash2,
  FiCopy,
  FiCheck,
  FiAlertTriangle,
  FiRefreshCw,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Personal access tokens.
 *
 * The one-time secret is shown in a modal that has to be dismissed
 * deliberately, because the server keeps only a hash and genuinely cannot show
 * it again — a toast that auto-dismisses would lose it for good.
 */

const ACCESS = [
  {
    id: 'read',
    scopes: ['repo:read'],
    label: 'Read only',
    help: 'Read repositories, files, commits and history.',
  },
  {
    id: 'status',
    scopes: ['repo:status'],
    label: 'Report CI statuses',
    help: 'For a CI job: read, and report whether a commit passed. Cannot push, so a leaked CI token cannot change what gets tested.',
  },
  {
    id: 'write',
    scopes: ['repo:write'],
    label: 'Read and write',
    help: 'Everything read can do, plus push, merge and create.',
  },
]

// A token that never expires is a standing liability if it leaks, so the
// choices nudge towards a lifetime and "never" is the last option, not the first.
const EXPIRY = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
  { value: '', label: 'Never' },
]

// admin implies write, write implies status, and status implies read -- the same
// hierarchy the server expands.
const scopeLabel = (scopes = []) =>
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

function relativeTime(iso) {
  // Server timestamps are naive UTC; new Date() alone would read them as local time
  // and show a key used a moment ago as used hours ago.
  const then = api.parseServerTimestamp(iso)?.getTime()
  if (!then) return 'never'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return api.parseServerTimestamp(iso).toLocaleDateString()
}

export default function AccessTokensPanel({ isAdmin = false }) {
  const [tokens, setTokens] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [includeRevoked, setIncludeRevoked] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [access, setAccess] = useState('read')
  const [withAdmin, setWithAdmin] = useState(false)
  const [expiresInDays, setExpiresInDays] = useState('90')
  const [creating, setCreating] = useState(false)
  const [formError, setFormError] = useState(null)

  const [newToken, setNewToken] = useState(null)
  const [copied, setCopied] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listAccessTokens({ includeRevoked })
      setTokens(response.tokens || [])
    } catch (err) {
      setError(err.message || 'Failed to load access tokens')
    } finally {
      setLoading(false)
    }
  }, [includeRevoked])

  useEffect(() => {
    load()
  }, [load])

  const closeCreate = () => {
    setCreateOpen(false)
    setFormError(null)
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    const base = ACCESS.find((option) => option.id === access)?.scopes || ['repo:read']
    try {
      setCreating(true)
      setFormError(null)
      const response = await api.createAccessToken({
        name: name.trim(),
        scopes: withAdmin && isAdmin ? [...base, 'admin'] : base,
        expiresInDays: expiresInDays || null,
      })
      setNewToken(response)
      setCreateOpen(false)
      setName('')
      setAccess('read')
      setWithAdmin(false)
      setExpiresInDays('90')
      await load()
    } catch (err) {
      setFormError(err.message || 'Failed to create token')
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (token) => {
    const confirmed = window.confirm(
      `Revoke "${token.name}"? Any machine using it loses access immediately. This cannot be undone.`
    )
    if (!confirmed) return
    try {
      await api.revokeAccessToken(token.id)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to revoke token')
    }
  }

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(newToken.token)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard can be blocked; the value is on screen to select manually */
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-light tracking-tight text-ink">
            Personal access tokens
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Use a token instead of your password on build agents and scripts. Each one is
            named, limited to the access you pick, and can be revoked on its own. Signing in
            with <code className="ref">fox login</code> creates one for that machine.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          <Button onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
            <FiPlus className="h-4 w-4" />
            New token
          </Button>
        </div>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </GlassCard>
      )}

      <label className="flex w-fit cursor-pointer items-center gap-2 text-sm text-ink-soft">
        <input
          type="checkbox"
          checked={includeRevoked}
          onChange={(e) => setIncludeRevoked(e.target.checked)}
          className="h-4 w-4 accent-[#3b9dff]"
        />
        Show revoked and expired
      </label>

      {loading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : tokens.length === 0 ? (
        <EmptyState
          icon={FiKey}
          title="No access tokens"
          description="Create one to authenticate a CI runner or a script without handing it your password."
          action={<Button onClick={() => setCreateOpen(true)}>Create a token</Button>}
        />
      ) : (
        <div className="space-y-2" data-testid="token-list">
          {tokens.map((token) => (
            <GlassCard
              key={token.id}
              data-testid="token-row"
              className={`flex flex-wrap items-center gap-4 p-4 ${
                token.active ? '' : 'opacity-60'
              }`}
            >
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-accent">
                <FiKey className="h-4 w-4" />
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{token.name}</span>
                  {token.revoked ? (
                    <Badge variant="danger">Revoked</Badge>
                  ) : token.expired ? (
                    <Badge variant="warning">Expired</Badge>
                  ) : (
                    <Badge variant="success">Active</Badge>
                  )}
                  {token.active && !token.expires_at && (
                    <Badge variant="warning" dot={false}>No expiry</Badge>
                  )}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted">
                  <span className="ref">{token.prefix}…</span>
                  <span title={(token.scopes || []).join(', ')}>{scopeLabel(token.scopes)}</span>
                  <span>last used {relativeTime(token.last_used_at)}</span>
                  {token.expires_at && (
                    <span>expires {api.parseServerTimestamp(token.expires_at).toLocaleDateString()}</span>
                  )}
                </div>
              </div>

              {!token.revoked && (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => handleRevoke(token)}
                  className="flex items-center gap-1.5"
                >
                  <FiTrash2 className="h-3.5 w-3.5" />
                  Revoke
                </Button>
              )}
            </GlassCard>
          ))}
        </div>
      )}

      {/* --- create --- */}
      <Modal
        open={createOpen}
        onClose={closeCreate}
        title="New access token"
        subtitle="Give it the narrowest access that still does the job."
      >
        <div className="space-y-5">
          <Input
            label="Name"
            placeholder="ci-runner"
            value={name}
            onChange={(e) => setName(e.target.value)}
            hint="Something you will recognise in six months."
          />

          <div className="space-y-2">
            <span className="text-[13px] font-medium text-ink-soft">Access</span>
            {ACCESS.map((option) => (
              <label
                key={option.id}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-white/[0.02] p-3 transition-colors hover:border-border-strong"
              >
                <input
                  type="radio"
                  name="token-access"
                  value={option.id}
                  checked={access === option.id}
                  onChange={() => setAccess(option.id)}
                  className="mt-0.5 h-4 w-4 accent-[#3b9dff]"
                />
                <span className="min-w-0">
                  <span className="block text-[13px] text-ink">
                    {option.label} <span className="font-mono text-muted">{option.scopes[0]}</span>
                  </span>
                  <span className="block text-xs text-muted">{option.help}</span>
                </span>
              </label>
            ))}
            {isAdmin && (
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-white/[0.02] p-3 transition-colors hover:border-border-strong">
                <input
                  type="checkbox"
                  checked={withAdmin}
                  onChange={(e) => setWithAdmin(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-[#3b9dff]"
                />
                <span className="min-w-0">
                  <span className="block font-mono text-[13px] text-ink">admin</span>
                  <span className="block text-xs text-muted">
                    Administrative endpoints and server-wide settings. Without it, even your
                    token cannot use your team-lead powers.
                  </span>
                </span>
              </label>
            )}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="token-expiry" className="block text-[13px] font-medium text-ink-soft">
              Expires after
            </label>
            <select
              id="token-expiry"
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(e.target.value)}
              className="w-full rounded-lg border border-border bg-cream-mid px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
            >
              {EXPIRY.map((option) => (
                <option key={option.label} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {!expiresInDays && (
              <p className="text-xs text-warning-fg">
                A token that never expires stays valid until someone remembers to revoke it.
              </p>
            )}
          </div>

          <p className="text-xs text-muted">
            A token cannot create other tokens, add SSH keys or change your password or
            email — those always need your password.
          </p>

          {formError && (
            <div className="flex items-start gap-2 rounded-lg border border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
              <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="min-w-0 break-words">{formError}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={closeCreate}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={!name.trim() || creating}>
              {creating ? 'Creating…' : 'Create token'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* --- the one-time reveal --- */}
      <Modal
        open={Boolean(newToken)}
        onClose={() => setNewToken(null)}
        title="Copy your token now"
        subtitle="The server stores only a hash of it, so this is the only time it can be shown."
      >
        <div className="space-y-4">
          <div className="flex items-center gap-2 rounded-lg border border-warning-fg/25 bg-warning-bg p-3 text-sm text-warning-fg">
            <FiAlertTriangle className="h-4 w-4 shrink-0" />
            <span>Once you close this dialog the value is gone for good.</span>
          </div>

          <div className="flex items-center gap-2 rounded-lg border border-border bg-cream-mid p-3">
            <code
              data-testid="new-token-value"
              className="min-w-0 flex-1 break-all font-mono text-[13px] text-ink"
            >
              {newToken?.token}
            </code>
            <Button variant="secondary" size="sm" onClick={copyToken} className="shrink-0">
              {copied ? (
                <>
                  <FiCheck className="mr-1.5 h-3.5 w-3.5 text-success-fg" />
                  Copied
                </>
              ) : (
                <>
                  <FiCopy className="mr-1.5 h-3.5 w-3.5" />
                  Copy
                </>
              )}
            </Button>
          </div>

          <div className="space-y-2 rounded-lg border border-border bg-white/[0.02] p-3">
            <p className="text-xs font-medium text-ink-soft">On a build agent, from its secret store:</p>
            <code className="block break-all font-mono text-[12px] text-muted">
              {scopeLabel(newToken?.access_token?.scopes) === 'read & report statuses'
                ? 'FOXNEST_TOKEN=<token> FOXNEST_REPO_ID=<repo id> fox status-check report ci/build success --commit <id>'
                : 'FOXNEST_TOKEN=<token> fox pull'}
            </code>
            <p className="pt-1 text-xs font-medium text-ink-soft">Or save it on a machine:</p>
            <code className="block break-all font-mono text-[12px] text-muted">
              fox login --token &lt;token&gt;
            </code>
          </div>

          <div className="flex justify-end">
            <Button onClick={() => setNewToken(null)}>I have saved it</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
