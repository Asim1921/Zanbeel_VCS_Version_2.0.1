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

const SCOPE_HELP = {
  'repo:read': 'Read repositories, files, commits and history.',
  'repo:write': 'Everything read can do, plus push, merge and create.',
  admin: 'Administrative endpoints. Only available to team leads.',
}

function relativeTime(iso) {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 'never'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

export default function AccessTokensPanel({ isAdmin = false }) {
  const [tokens, setTokens] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [includeRevoked, setIncludeRevoked] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState(['repo:read'])
  const [expiresInDays, setExpiresInDays] = useState('')
  const [creating, setCreating] = useState(false)

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

  const toggleScope = (scope) => {
    setScopes((current) =>
      current.includes(scope) ? current.filter((s) => s !== scope) : [...current, scope]
    )
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    try {
      setCreating(true)
      setError(null)
      const response = await api.createAccessToken({
        name: name.trim(),
        scopes,
        expiresInDays: expiresInDays || null,
      })
      setNewToken(response)
      setCreateOpen(false)
      setName('')
      setScopes(['repo:read'])
      setExpiresInDays('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create token')
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

  const availableScopes = ['repo:read', 'repo:write', ...(isAdmin ? ['admin'] : [])]

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-light tracking-tight text-ink">
            Personal access tokens
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Use a token instead of your password on build agents and scripts. Each one is
            named, limited to the scopes you pick, and can be revoked on its own.
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
        <div className="space-y-2">
          {tokens.map((token) => (
            <GlassCard
              key={token.id}
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
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted">
                  <span className="ref">{token.prefix}…</span>
                  <span>{(token.scopes || []).join(', ')}</span>
                  <span>last used {relativeTime(token.last_used_at)}</span>
                  {token.expires_at && (
                    <span>expires {new Date(token.expires_at).toLocaleDateString()}</span>
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
        onClose={() => setCreateOpen(false)}
        title="New access token"
        subtitle="Give it the narrowest scope that still does the job."
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
            <span className="text-[13px] font-medium text-ink-soft">Scopes</span>
            {availableScopes.map((scope) => (
              <label
                key={scope}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-white/[0.02] p-3 transition-colors hover:border-border-strong"
              >
                <input
                  type="checkbox"
                  checked={scopes.includes(scope)}
                  onChange={() => toggleScope(scope)}
                  className="mt-0.5 h-4 w-4 accent-[#3b9dff]"
                />
                <span className="min-w-0">
                  <span className="block font-mono text-[13px] text-ink">{scope}</span>
                  <span className="block text-xs text-muted">{SCOPE_HELP[scope]}</span>
                </span>
              </label>
            ))}
          </div>

          <Input
            label="Expires after (days)"
            type="number"
            min="1"
            max="3650"
            placeholder="Leave blank for no expiry"
            value={expiresInDays}
            onChange={(e) => setExpiresInDays(e.target.value)}
          />

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={() => setCreateOpen(false)}>
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
            <code className="min-w-0 flex-1 break-all font-mono text-[13px] text-ink">
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

          <div className="rounded-lg border border-border bg-white/[0.02] p-3">
            <p className="mb-1.5 text-xs font-medium text-ink-soft">Use it from the CLI:</p>
            <code className="block break-all font-mono text-[12px] text-muted">
              fox token use {newToken?.token}
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
