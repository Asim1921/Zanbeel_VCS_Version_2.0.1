import React, { useCallback, useEffect, useState } from 'react'
import {
  FiSend,
  FiPlus,
  FiTrash2,
  FiZap,
  FiAlertTriangle,
  FiRefreshCw,
  FiCheckCircle,
  FiXCircle,
  FiChevronDown,
  FiChevronRight,
  FiLock,
  FiUnlock,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Webhooks for one repository, or the server-wide set when repositoryId is null.
 *
 * The delivery log is the point of this screen as much as the configuration is:
 * "did the integration actually receive the push" is the first thing anyone
 * asks when a pipeline does not run, and it is unanswerable without it.
 */

const ALL_EVENTS = [
  'push',
  'commit.pending',
  'commit.reviewed',
  'branch.created',
  'branch.merged',
  'pull_request.opened',
  'pull_request.merged',
  'pull_request.closed',
  'pull_request.reviewed',
  'tag.created',
  'release.published',
  'repository.created',
  'status.reported',
]

function DeliveryRow({ delivery }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-white/[0.03]"
      >
        {open ? (
          <FiChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
        ) : (
          <FiChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
        )}
        {delivery.success ? (
          <FiCheckCircle className="h-4 w-4 shrink-0 text-success-fg" />
        ) : (
          <FiXCircle className="h-4 w-4 shrink-0 text-danger-fg" />
        )}
        <span className="ref shrink-0">{delivery.event}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">
          {delivery.created_at?.slice(0, 19).replace('T', ' ')}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-muted">
          {delivery.status_code ?? 'no response'}
          {delivery.duration_ms != null && ` · ${delivery.duration_ms}ms`}
          {delivery.attempt > 1 && ` · try ${delivery.attempt}`}
        </span>
      </button>

      {open && (
        <div className="space-y-3 bg-black/20 px-3 py-3">
          {delivery.error && (
            <div>
              <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-danger-fg">
                Error
              </p>
              <pre className="overflow-x-auto rounded bg-black/40 p-2 font-mono text-[11px] text-danger-fg">
                {delivery.error}
              </pre>
            </div>
          )}
          <div>
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted">
              Request body
            </p>
            <pre className="max-h-52 overflow-auto rounded bg-black/40 p-2 font-mono text-[11px] text-ink-soft">
              {(() => {
                try {
                  return JSON.stringify(JSON.parse(delivery.payload), null, 2)
                } catch {
                  return delivery.payload
                }
              })()}
            </pre>
          </div>
          {delivery.response_body && (
            <div>
              <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted">
                Response
              </p>
              <pre className="max-h-32 overflow-auto rounded bg-black/40 p-2 font-mono text-[11px] text-ink-soft">
                {delivery.response_body}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function WebhooksPanel({ repositoryId = null }) {
  const [hooks, setHooks] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [events, setEvents] = useState([])
  const [saving, setSaving] = useState(false)

  const [expanded, setExpanded] = useState(null)
  const [deliveries, setDeliveries] = useState({})
  const [pinging, setPinging] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listWebhooks(repositoryId)
      setHooks(response.webhooks || [])
    } catch (err) {
      setError(err.message || 'Failed to load webhooks')
    } finally {
      setLoading(false)
    }
  }, [repositoryId])

  useEffect(() => {
    load()
  }, [load])

  const toggleEvent = (event) => {
    setEvents((current) =>
      current.includes(event) ? current.filter((e) => e !== event) : [...current, event]
    )
  }

  const handleCreate = async () => {
    if (!url.trim()) return
    try {
      setSaving(true)
      setError(null)
      await api.createWebhook({
        url: url.trim(),
        repository_id: repositoryId,
        secret: secret.trim() || null,
        // An empty selection means "everything", which is what the server
        // stores as "*" — sending [] would be indistinguishable from a mistake.
        events: events.length ? events : null,
      })
      setCreateOpen(false)
      setUrl('')
      setSecret('')
      setEvents([])
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create webhook')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (hook) => {
    if (!window.confirm(`Delete the webhook for ${hook.url}? Its delivery history goes too.`)) return
    try {
      await api.deleteWebhook(hook.id)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to delete webhook')
    }
  }

  const handleToggleActive = async (hook) => {
    try {
      await api.updateWebhook(hook.id, { active: !hook.active })
      await load()
    } catch (err) {
      setError(err.message || 'Failed to update webhook')
    }
  }

  const handlePing = async (hook) => {
    try {
      setPinging(hook.id)
      setNotice(null)
      setError(null)
      const response = await api.pingWebhook(hook.id)
      const delivery = response.delivery
      if (delivery?.success) {
        setNotice(`${hook.url} answered ${delivery.status_code} in ${delivery.duration_ms}ms.`)
      } else {
        setError(
          `Ping failed: ${delivery?.error || `HTTP ${delivery?.status_code ?? 'no response'}`}`
        )
      }
      if (expanded === hook.id) await loadDeliveries(hook.id)
    } catch (err) {
      setError(err.message || 'Ping failed')
    } finally {
      setPinging(null)
    }
  }

  const loadDeliveries = async (hookId) => {
    try {
      const response = await api.listWebhookDeliveries(hookId)
      setDeliveries((current) => ({ ...current, [hookId]: response.deliveries || [] }))
    } catch (err) {
      setError(err.message || 'Failed to load deliveries')
    }
  }

  const toggleExpanded = async (hookId) => {
    if (expanded === hookId) {
      setExpanded(null)
      return
    }
    setExpanded(hookId)
    if (!deliveries[hookId]) await loadDeliveries(hookId)
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-light tracking-tight text-ink">
            Webhooks
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            {repositoryId
              ? 'POST a signed JSON payload to an external service when something happens in this repository.'
              : 'Server-wide hooks fire for every repository — useful for an audit sink or a chat relay.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          <Button onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
            <FiPlus className="h-4 w-4" />
            Add webhook
          </Button>
        </div>
      </div>

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

      {loading ? (
        <div className="space-y-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-20 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : hooks.length === 0 ? (
        <EmptyState
          icon={FiSend}
          title="No webhooks yet"
          description="Without one, an external system can only find out something happened by polling."
          action={<Button onClick={() => setCreateOpen(true)}>Add a webhook</Button>}
        />
      ) : (
        <div className="space-y-3">
          {hooks.map((hook) => (
            <GlassCard key={hook.id} hover={false} className="overflow-hidden">
              <div className="flex flex-wrap items-center gap-3 p-4">
                <div
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${
                    hook.active
                      ? 'border-accent/30 bg-accent/10 text-accent'
                      : 'border-border bg-surface text-muted'
                  }`}
                >
                  <FiSend className="h-4 w-4" />
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-mono text-[13px] text-ink">{hook.url}</span>
                    {hook.active ? (
                      <Badge variant="success">Active</Badge>
                    ) : (
                      <Badge variant="default">Paused</Badge>
                    )}
                    {hook.has_secret ? (
                      <span
                        className="flex items-center gap-1 text-[11px] text-success-fg"
                        title="Deliveries are signed with HMAC-SHA256"
                      >
                        <FiLock className="h-3 w-3" />
                        signed
                      </span>
                    ) : (
                      <span
                        className="flex items-center gap-1 text-[11px] text-warning-fg"
                        title="Without a secret a receiver cannot tell a real delivery from a forged one"
                      >
                        <FiUnlock className="h-3 w-3" />
                        unsigned
                      </span>
                    )}
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px] text-muted">
                    {hook.events.includes('*') ? 'all events' : hook.events.join(', ')}
                  </p>
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handlePing(hook)}
                    disabled={pinging === hook.id}
                    className="flex items-center gap-1.5"
                  >
                    <FiZap className={`h-3.5 w-3.5 ${pinging === hook.id ? 'animate-pulse' : ''}`} />
                    {pinging === hook.id ? 'Pinging…' : 'Ping'}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => handleToggleActive(hook)}>
                    {hook.active ? 'Pause' : 'Resume'}
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => handleDelete(hook)}
                    className="!px-2"
                    title="Delete"
                  >
                    <FiTrash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>

              <button
                type="button"
                onClick={() => toggleExpanded(hook.id)}
                className="flex w-full items-center gap-2 border-t border-border px-4 py-2 text-left text-xs text-ink-soft transition-colors hover:bg-white/[0.03]"
              >
                {expanded === hook.id ? (
                  <FiChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <FiChevronRight className="h-3.5 w-3.5" />
                )}
                Recent deliveries
              </button>

              {expanded === hook.id && (
                <div className="border-t border-border">
                  {!deliveries[hook.id] ? (
                    <p className="px-4 py-4 text-sm text-muted">Loading…</p>
                  ) : deliveries[hook.id].length === 0 ? (
                    <p className="px-4 py-4 text-sm text-muted">
                      Nothing delivered yet. Use Ping to send a test.
                    </p>
                  ) : (
                    deliveries[hook.id].map((d) => <DeliveryRow key={d.id} delivery={d} />)
                  )}
                </div>
              )}
            </GlassCard>
          ))}
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Add a webhook"
        subtitle="Zanbeel will POST a JSON payload to this URL."
      >
        <div className="space-y-5">
          <Input
            label="Payload URL"
            placeholder="https://example.com/zanbeel-hook"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />

          <Input
            label="Secret (optional)"
            type="password"
            placeholder="Used to sign each delivery"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            hint="Deliveries are signed with HMAC-SHA256 in X-Zanbeel-Signature-256. Without a secret the receiver cannot tell a real delivery from a forged one."
          />

          <div className="space-y-2">
            <span className="text-[13px] font-medium text-ink-soft">Events</span>
            <p className="text-xs text-muted">
              Leave everything unchecked to receive all events.
            </p>
            <div className="grid max-h-56 grid-cols-1 gap-1.5 overflow-y-auto pr-1 sm:grid-cols-2">
              {ALL_EVENTS.map((event) => (
                <label
                  key={event}
                  className="flex cursor-pointer items-center gap-2 rounded-lg border border-border bg-white/[0.02] px-2.5 py-1.5 transition-colors hover:border-border-strong"
                >
                  <input
                    type="checkbox"
                    checked={events.includes(event)}
                    onChange={() => toggleEvent(event)}
                    className="h-3.5 w-3.5 accent-[#3b9dff]"
                  />
                  <span className="truncate font-mono text-[12px] text-ink-soft">{event}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={!url.trim() || saving}>
              {saving ? 'Adding…' : 'Add webhook'}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
