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
 * points at `fox ssh login` for the rest. Saying so plainly avoids the obvious
 * confusion of "why is there no Sign in with SSH button here".
 */

function relativeTime(iso) {
  if (!iso) return 'never used'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 'never used'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return 'used just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `used ${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `used ${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `used ${days}d ago`
  return `used ${new Date(iso).toLocaleDateString()}`
}

export default function SSHKeysPanel() {
  const [keys, setKeys] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [addOpen, setAddOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listSSHKeys()
      setKeys(response.keys || [])
    } catch (err) {
      setError(err.message || 'Failed to load SSH keys')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleAdd = async () => {
    if (!title.trim() || !publicKey.trim()) return
    try {
      setSaving(true)
      setError(null)
      await api.addSSHKey({ title: title.trim(), publicKey: publicKey.trim() })
      setAddOpen(false)
      setTitle('')
      setPublicKey('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to add the key')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (key) => {
    const confirmed = window.confirm(
      `Remove "${key.title}"? Any machine signing in with it loses access immediately.`
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
            signs it.
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
          description="Add your public key, then sign in from the CLI with: fox ssh login"
          action={<Button onClick={() => setAddOpen(true)}>Add a key</Button>}
        />
      ) : (
        <div className="space-y-2">
          {keys.map((key) => (
            <GlassCard key={key.id} hover={false} className="flex flex-wrap items-center gap-4 p-4">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-accent">
                <FiKey className="h-4 w-4" />
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{key.title}</span>
                  <Badge variant="default">{key.key_type}</Badge>
                </div>
                <p className="mt-1 truncate font-mono text-[11px] text-muted">
                  {key.fingerprint}
                </p>
                <p className="mt-0.5 text-xs text-muted">{relativeTime(key.last_used_at)}</p>
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
          sign-in itself happens in the CLI, not here.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-black/30 p-3 font-mono text-[12px] text-ink-soft">
{`ssh-keygen -t ed25519          # if you do not have a key yet
fox ssh add work-laptop        # uploads ~/.ssh/id_ed25519.pub
fox ssh login                  # signs a challenge, saves a session`}
        </pre>
      </GlassCard>

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
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
              <code className="ref">ssh-rsa</code> or <code className="ref">ecdsa-sha2-…</code>.
            </p>
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={() => setAddOpen(false)}>
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
