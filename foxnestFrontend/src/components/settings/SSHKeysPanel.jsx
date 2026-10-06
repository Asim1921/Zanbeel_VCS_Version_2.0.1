import React, { useCallback, useEffect, useState } from 'react'
import {
  FiTerminal,
  FiPlus,
  FiTrash2,
  FiAlertTriangle,
  FiRefreshCw,
  FiKey,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * SSH keys for CLI sign-in.
 *
 * Signing happens on the client with the private key, so the browser cannot
 * complete an SSH login — this screen only registers and revokes keys, and
 * points at `fox login --ssh` for the rest. Saying so plainly avoids the obvious
 * confusion of "why is there no Sign in with SSH button here".
 *
 * A key is a machine credential, so it carries scopes like a token: a sign-in
 * with a read-only key can fetch but not push. Removing a key ends every sign-in
 * it made, immediately.
 */

const ACCESS = [
  {
    id: 'write',
    scopes: ['repo:write'],
    label: 'Read and write',
    help: 'Push, merge and everything else you can do to repositories.',
  },
  {
    id: 'read',
    scopes: ['repo:read'],
    label: 'Read only',
    help: 'Fetch and browse. Good for a build agent or a deploy machine.',
  },
]

function relativeTime(iso) {
  // Server timestamps are naive UTC; new Date() alone would read them as local time
  // and show a key used a moment ago as used hours ago.
  const then = api.parseServerTimestamp(iso)?.getTime()
  if (!then) return 'never used'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return 'used just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `used ${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `used ${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `used ${days}d ago`
  return `used ${api.parseServerTimestamp(iso).toLocaleDateString()}`
}

export default function SSHKeysPanel({ isAdmin = false }) {
  const [keys, setKeys] = useState([])
  const [sessionHours, setSessionHours] = useState(8)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [addOpen, setAddOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [access, setAccess] = useState('write')
  const [withAdmin, setWithAdmin] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listSSHKeys()
      setKeys(response.keys || [])
      if (response.session_hours) setSessionHours(response.session_hours)
    } catch (err) {
      setError(err.message || 'Failed to load SSH keys')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const closeAdd = () => {
    setAddOpen(false)
    setFormError(null)
  }

  const handleAdd = async () => {
    if (!title.trim() || !publicKey.trim()) return
    const base = ACCESS.find((option) => option.id === access)?.scopes || ['repo:write']
    try {
      setSaving(true)
      setFormError(null)
      await api.addSSHKey({
        title: title.trim(),
        publicKey: publicKey.trim(),
        scopes: withAdmin && isAdmin ? [...base, 'admin'] : base,
      })
      setAddOpen(false)
      setTitle('')
      setPublicKey('')
      setAccess('write')
      setWithAdmin(false)
      await load()
    } catch (err) {
      // Shown in the dialog, next to the key the server refused, rather than
      // behind it where it is easy to miss.
      setFormError(err.message || 'Failed to add the key')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (key) => {
    const sessions = key.active_sessions
      ? ` ${key.active_sessions} active sign-in${key.active_sessions === 1 ? '' : 's'} will end now.`
      : ''
    const confirmed = window.confirm(
      `Remove "${key.title}"? It can no longer be used to sign in.${sessions}`
    )
    if (!confirmed) return
    try {
      await api.deleteSSHKey(key.id)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to remove the key')
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-light tracking-tight text-ink">
            SSH keys
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Sign in from the CLI with a key instead of a password. Nothing reusable
            crosses the wire — the server sends a one-time challenge and your machine
            signs it. Each key is limited to the access you give it.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          <Button onClick={() => setAddOpen(true)} className="flex items-center gap-2">
            <FiPlus className="h-4 w-4" />
            Add key
          </Button>
        </div>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      {loading ? (
        <div className="space-y-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : keys.length === 0 ? (
        <EmptyState
          icon={FiTerminal}
          title="No SSH keys"
          description="Add your public key, then sign in from the CLI with: fox login --ssh"
          action={<Button onClick={() => setAddOpen(true)}>Add a key</Button>}
        />
      ) : (
        <div className="space-y-2" data-testid="ssh-key-list">
          {keys.map((key) => (
            <GlassCard
              key={key.id}
              hover={false}
              className="flex flex-wrap items-center gap-4 p-4"
              data-testid="ssh-key-row"
            >
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-accent">
                <FiKey className="h-4 w-4" />
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{key.title}</span>
                  <Badge variant="default">{key.key_type}</Badge>
                  {(key.scopes || []).includes('repo:write') ? (
                    <Badge variant="info">read &amp; write</Badge>
                  ) : (
                    <Badge variant="success">read only</Badge>
                  )}
                  {(key.scopes || []).includes('admin') && <Badge variant="warning">admin</Badge>}
                </div>
                <p className="mt-1 truncate font-mono text-[11px] text-muted">
                  {key.fingerprint}
                </p>
                <p className="mt-0.5 text-xs text-muted">
                  {relativeTime(key.last_used_at)}
                  {key.active_sessions > 0 && (
                    <>
                      {' · '}
                      <span className="text-ink-soft">
                        {key.active_sessions} active sign-in{key.active_sessions === 1 ? '' : 's'}
                      </span>
                    </>
                  )}
                </p>
              </div>

              <Button
                variant="danger"
                size="sm"
                onClick={() => handleDelete(key)}
                className="flex items-center gap-1.5"
              >
                <FiTrash2 className="h-3.5 w-3.5" />
                Remove
              </Button>
            </GlassCard>
          ))}
        </div>
      )}

      <GlassCard hover={false} className="space-y-2 p-4">
        <p className="text-[13px] font-medium text-ink-soft">Using a key</p>
        <p className="text-xs text-muted">
          Signing needs your private key, which never leaves your machine — so the
          sign-in itself happens in the CLI, not here. A sign-in lasts {sessionHours} hours
          and the CLI renews it with the key on its own. Keys protected by a passphrase or
          held in ssh-agent work too.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`ssh-keygen -t ed25519          # if you do not have a key yet
fox ssh add work-laptop        # uploads ~/.ssh/id_ed25519.pub
fox login --ssh                # signs a challenge; no password from now on`}
        </pre>
      </GlassCard>

      <Modal
        open={addOpen}
        onClose={closeAdd}
        title="Add an SSH key"
        subtitle="Paste the contents of your .pub file — never the private key."
      >
        <div className="space-y-5">
          <Input
            label="Title"
            placeholder="work-laptop"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            hint="So you know which machine to revoke later."
          />

          <div>
            <label className="mb-1.5 block text-[13px] font-medium text-ink-soft">
              Public key
            </label>
            <textarea
              rows={5}
              value={publicKey}
              onChange={(e) => setPublicKey(e.target.value)}
              placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... you@laptop"
              className="w-full resize-y rounded-lg border border-border bg-cream-mid px-3 py-2 font-mono text-[12px] text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
            />
            <p className="mt-1 text-xs text-muted">
              One line, starting with <code className="ref">ssh-ed25519</code>,{' '}
              <code className="ref">ssh-rsa</code> (2048 bits or more) or{' '}
              <code className="ref">ecdsa-sha2-…</code>.
            </p>
          </div>

          <div className="space-y-2">
            <span className="text-[13px] font-medium text-ink-soft">Access</span>
            {ACCESS.map((option) => (
              <label
                key={option.id}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-white/[0.02] p-3 transition-colors hover:border-border-strong"
              >
                <input
                  type="radio"
                  name="ssh-key-access"
                  value={option.id}
                  checked={access === option.id}
                  onChange={() => setAccess(option.id)}
                  className="mt-0.5 h-4 w-4 accent-[#3b9dff]"
                />
                <span className="min-w-0">
                  <span className="block text-[13px] text-ink">{option.label}</span>
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
                    Also allow administrative commands. Leave off unless this machine needs them.
                  </span>
                </span>
              </label>
            )}
          </div>

          {formError && (
            <div className="flex items-start gap-2 rounded-lg border border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
              <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="min-w-0 break-words">{formError}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={closeAdd}>
              Cancel
            </Button>
            <Button onClick={handleAdd} disabled={!title.trim() || !publicKey.trim() || saving}>
              {saving ? 'Adding…' : 'Add key'}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
