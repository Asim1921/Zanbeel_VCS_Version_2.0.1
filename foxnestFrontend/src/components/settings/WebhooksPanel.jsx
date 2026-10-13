import React, { useCallback, useEffect, useMemo, useState } from 'react'
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
  FiRotateCw,
  FiCopy,
  FiCheck,
  FiRepeat,
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
 * asks when a pipeline does not run, and it is unanswerable without it. Attempts
 * are grouped by delivery, and any delivery can be sent again.
 *
 * Every webhook is signed. A secret is generated when none is given and shown
 * once, like an access token, because the server cannot show it again.
 */

const FALLBACK_EVENTS = [
  'push',
  'commit.pending',
  'commit.reviewed',
  'branch.created',
  'branch.deleted',
  'branch.merged',
  'pull_request.opened',
  'pull_request.updated',
  'pull_request.merged',
  'pull_request.closed',
  'pull_request.reviewed',
  'tag.created',
  'release.published',
  'repository.created',
  'status.reported',
]

const when = (iso) => {
  const parsed = api.parseServerTimestamp(iso)
  return parsed ? parsed.toLocaleString() : ''
}

/** Rows are attempts; a delivery is all the attempts that share a delivery id. */
function groupDeliveries(rows) {
  const groups = []
  const byId = new Map()
  for (const row of rows) {
    const key = row.delivery_id || `row-${row.id}`
    if (!byId.has(key)) {
      const group = { key, attempts: [] }
      byId.set(key, group)
      groups.push(group)
    }
    byId.get(key).attempts.push(row)
  }
  for (const group of groups) {
    group.attempts.sort((a, b) => a.attempt - b.attempt)
    group.last = group.attempts[group.attempts.length - 1]
  }
  return groups
}

function prettyBody(payload) {
  try {
    return JSON.stringify(JSON.parse(payload), null, 2)
  } catch {
    return payload
  }
}

function DeliveryGroup({ group, onRedeliver, busy }) {
  const [open, setOpen] = useState(false)
  const { last, attempts } = group
  return (
    <div className="border-b border-border last:border-0" data-testid="delivery-group">
      <div className="flex w-full flex-wrap items-center gap-3 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
        >
          {open ? (
            <FiChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
          ) : (
            <FiChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
          )}
          {last.success ? (
            <FiCheckCircle className="h-4 w-4 shrink-0 text-success-fg" />
          ) : (
            <FiXCircle className="h-4 w-4 shrink-0 text-danger-fg" />
          )}
          <span className="ref shrink-0">{last.event}</span>
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">
            {when(attempts[0].created_at)}
            {last.redelivery_of && ' · redelivery'}
          </span>
          <span className="shrink-0 font-mono text-[11px] text-muted">
            {last.status_code ?? 'no response'}
            {last.duration_ms != null && ` · ${last.duration_ms}ms`}
            {attempts.length > 1 && ` · ${attempts.length} attempts`}
          </span>
        </button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onRedeliver(last)}
          disabled={busy}
          className="flex shrink-0 items-center gap-1.5"
          title="Send this delivery again"
        >
          <FiRepeat className="h-3.5 w-3.5" />
          Redeliver
        </Button>
      </div>

      {open && (
        <div className="space-y-3 bg-black/20 px-3 py-3">
          <p className="font-mono text-[11px] text-muted">
            Delivery {last.delivery_id || '(from before delivery ids)'}
            {last.redelivery_of && ` — repeats ${last.redelivery_of}`}
          </p>
          {attempts.map((attempt) => (
            <div key={attempt.id} className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
              <span className="text-muted">try {attempt.attempt}</span>
              <span className={attempt.success ? 'text-success-fg' : 'text-danger-fg'}>
                {attempt.status_code ?? 'no response'}
              </span>
              {attempt.error && <span className="break-all text-danger-fg">{attempt.error}</span>}
            </div>
          ))}
          <div>
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted">
              Signed body
            </p>
            <pre className="max-h-52 overflow-auto rounded bg-black/40 p-2 font-mono text-[11px] text-ink-soft">
              {prettyBody(last.payload)}
            </pre>
          </div>
          {last.response_body && (
            <div>
              <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted">
                Response
              </p>
              <pre className="max-h-32 overflow-auto rounded bg-black/40 p-2 font-mono text-[11px] text-ink-soft">
                {last.response_body}
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
  const [validEvents, setValidEvents] = useState(FALLBACK_EVENTS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [events, setEvents] = useState([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState(null)

  const [revealed, setRevealed] = useState(null) // { url, secret, warning }
  const [copied, setCopied] = useState(false)

  const [expanded, setExpanded] = useState(null)
  const [deliveries, setDeliveries] = useState({})
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listWebhooks(repositoryId)
      setHooks(response.webhooks || [])
      if (response.valid_events?.length) setValidEvents(response.valid_events)
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

  const closeCreate = () => {
    setCreateOpen(false)
    setFormError(null)
  }

  const handleCreate = async () => {
    if (!url.trim()) return
    try {
      setSaving(true)
      setFormError(null)
      const response = await api.createWebhook({
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
      if (response.secret_generated) {
        setRevealed({ url: response.webhook?.url, secret: response.secret, warning: response.warning })
      } else if (response.warning) {
        setNotice(response.warning)
      }
      await load()
    } catch (err) {
      // In the dialog, next to the URL the server refused.
      setFormError(err.message || 'Failed to create webhook')
    } finally {
      setSaving(false)
    }
  }

  const handleRotate = async (hook) => {
    const ok = window.confirm(
      hook.signed
        ? `Replace the signing secret for ${hook.url}? Deliveries are signed with the new one straight away, so the receiver must be updated.`
        : `Give ${hook.url} a signing secret? Its deliveries are unsigned today.`
    )
    if (!ok) return
    try {
      setBusy(`rotate:${hook.id}`)
      const response = await api.rotateWebhookSecret(hook.id)
      setRevealed({ url: hook.url, secret: response.secret })
      await load()
    } catch (err) {
      setError(err.message || 'Failed to rotate the secret')
    } finally {
      setBusy(null)
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

  const loadDeliveries = async (hookId) => {
    try {
      const response = await api.listWebhookDeliveries(hookId, 60)
      setDeliveries((current) => ({ ...current, [hookId]: response.deliveries || [] }))
    } catch (err) {
      setError(err.message || 'Failed to load deliveries')
    }
  }

  const report = (hook, delivery, verb) => {
    if (delivery?.success) {
      setNotice(`${verb}: ${hook.url} answered ${delivery.status_code} in ${delivery.duration_ms}ms.`)
      setError(null)
    } else {
      setNotice(null)
      setError(`${verb} failed: ${delivery?.error || `HTTP ${delivery?.status_code ?? 'no response'}`}`)
    }
  }

  const handlePing = async (hook) => {
    try {
      setBusy(`ping:${hook.id}`)
      setNotice(null)
      setError(null)
      const response = await api.pingWebhook(hook.id)
      report(hook, response.delivery, 'Ping')
      if (expanded === hook.id) await loadDeliveries(hook.id)
    } catch (err) {
      setError(err.message || 'Ping failed')
    } finally {
      setBusy(null)
    }
  }

  const handleRedeliver = async (hook, delivery) => {
    try {
      setBusy(`redeliver:${hook.id}`)
      const response = await api.redeliverWebhookDelivery(hook.id, delivery.id)
      report(hook, response.delivery, 'Redelivery')
      await loadDeliveries(hook.id)
    } catch (err) {
      setError(err.message || 'Redelivery failed')
    } finally {
      setBusy(null)
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

  const grouped = useMemo(() => {
    const out = {}
    for (const [hookId, rows] of Object.entries(deliveries)) out[hookId] = groupDeliveries(rows)
    return out
  }, [deliveries])

  const copySecret = async () => {
    try {
      await navigator.clipboard.writeText(revealed.secret)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* the value is on screen to select by hand */
    }
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
              ? 'POST a signed JSON payload to an external service when something happens in this repository — every push, merge and branch change, however it was made.'
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
        <div className="space-y-3" data-testid="webhook-list">
          {hooks.map((hook) => (
            <GlassCard key={hook.id} hover={false} className="overflow-hidden" data-testid="webhook-row">
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
                    {hook.signed ? (
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
                        unsigned — generate a secret
                      </span>
                    )}
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px] text-muted">
                    {hook.events.includes('*') ? 'all events' : hook.events.join(', ')}
                  </p>
                </div>

                <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handlePing(hook)}
                    disabled={busy === `ping:${hook.id}`}
                    className="flex items-center gap-1.5"
                  >
                    <FiZap className={`h-3.5 w-3.5 ${busy === `ping:${hook.id}` ? 'animate-pulse' : ''}`} />
                    {busy === `ping:${hook.id}` ? 'Pinging…' : 'Ping'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleRotate(hook)}
                    disabled={busy === `rotate:${hook.id}`}
                    className="flex items-center gap-1.5"
                    title={hook.signed ? 'Replace the signing secret' : 'Give this webhook a signing secret'}
                  >
                    <FiRotateCw className="h-3.5 w-3.5" />
                    {hook.signed ? 'Rotate secret' : 'Generate secret'}
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
                  {!grouped[hook.id] ? (
                    <p className="px-4 py-4 text-sm text-muted">Loading…</p>
                  ) : grouped[hook.id].length === 0 ? (
                    <p className="px-4 py-4 text-sm text-muted">
                      Nothing delivered yet. Use Ping to send a test.
                    </p>
                  ) : (
                    grouped[hook.id].map((group) => (
                      <DeliveryGroup
                        key={group.key}
                        group={group}
                        busy={busy === `redeliver:${hook.id}`}
                        onRedeliver={(delivery) => handleRedeliver(hook, delivery)}
                      />
                    ))
                  )}
                </div>
              )}
            </GlassCard>
          ))}
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={closeCreate}
        title="Add a webhook"
        subtitle="Zanbeel will POST a signed JSON payload to this URL."
      >
        <div className="space-y-5">
          <Input
            label="Payload URL"
            placeholder="https://ci.example.com/zanbeel-hook"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            hint="Addresses on this server, the private network or the cloud metadata service are refused unless an administrator allows them."
          />

          <Input
            label="Secret (optional)"
            type="password"
            placeholder="Leave empty to have one generated"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            hint="At least 16 characters. Each delivery carries X-Zanbeel-Signature-256: sha256=<HMAC-SHA256 of the body>."
          />

          <div className="space-y-2">
            <span className="text-[13px] font-medium text-ink-soft">Events</span>
            <p className="text-xs text-muted">
              Leave everything unchecked to receive all events. CI usually wants{' '}
              <code className="ref">push</code> and <code className="ref">pull_request.updated</code>.
            </p>
            <div className="grid max-h-56 grid-cols-1 gap-1.5 overflow-y-auto pr-1 sm:grid-cols-2">
              {validEvents.map((event) => (
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
            <Button onClick={handleCreate} disabled={!url.trim() || saving}>
              {saving ? 'Adding…' : 'Add webhook'}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal
        open={Boolean(revealed)}
        onClose={() => setRevealed(null)}
        title="Copy the signing secret now"
        subtitle="The server cannot show it again. The receiver needs it to verify deliveries."
      >
        <div className="space-y-4">
          <div className="flex items-center gap-2 rounded-lg border border-border bg-cream-mid p-3">
            <code data-testid="webhook-secret" className="min-w-0 flex-1 break-all font-mono text-[13px] text-ink">
              {revealed?.secret}
            </code>
            <Button variant="secondary" size="sm" onClick={copySecret} className="shrink-0">
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
          <div className="rounded-lg border border-border bg-white/[0.02] p-3 text-xs text-muted">
            The receiver computes HMAC-SHA256 of the raw request body with this secret and
            compares it with <code className="ref">X-Zanbeel-Signature-256</code>. The body carries
            its own <code className="ref">delivery_id</code> and <code className="ref">delivered_at</code>,
            so a replayed delivery can be refused.
          </div>
          {revealed?.warning && <p className="text-xs text-warning-fg">{revealed.warning}</p>}
          <div className="flex justify-end">
            <Button onClick={() => setRevealed(null)}>I have saved it</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
