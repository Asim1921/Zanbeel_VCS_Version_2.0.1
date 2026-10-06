import React, { useCallback, useEffect, useState } from 'react'
import {
  FiBell, FiCheck, FiCheckCircle, FiLoader, FiAlertCircle, FiRotateCcw,
  FiGitPullRequest, FiGitMerge, FiMessageSquare, FiUserCheck, FiFolder, FiInbox,
  FiKey, FiTerminal, FiSlash,
} from 'react-icons/fi'
import api from '../utils/api'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import { cn } from '../lib/utils'

/**
 * Everything the system has told this person, in one place.
 *
 * The bell shows the last few; this is where you go through them. Opening one marks
 * it read, and marking it unread again is available on every row — otherwise people
 * avoid opening the things they mean to come back to.
 */

const PAGE = 30

const ICONS = {
  pr_review: FiGitPullRequest,
  pr_review_requested: FiUserCheck,
  pr_merged: FiGitMerge,
  pr_comment: FiMessageSquare,
  issue_comment: FiMessageSquare,
  issue_assigned: FiUserCheck,
  access_granted: FiFolder,
  credential_token_created: FiKey,
  credential_ssh_key_added: FiTerminal,
  credentials_revoked: FiSlash,
}

const LABELS = {
  pr_review: 'review',
  pr_review_requested: 'review requested',
  pr_merged: 'merged',
  pr_comment: 'comment',
  credential_token_created: 'new access token',
  credential_ssh_key_added: 'new SSH key',
  credentials_revoked: 'credentials revoked',
}

const prettyType = (type) =>
  LABELS[type] || (type || '').replace(/_/g, ' ')

function Row({ notification, onRead, onUnread, busy }) {
  const [open, setOpen] = useState(false)
  const Icon = ICONS[notification.type] || FiBell
  const unread = !notification.is_read

  const toggle = () => {
    const next = !open
    setOpen(next)
    // Opening is what marks it read, which matches how people actually treat a list
    // like this: the ones they looked at are the ones they dealt with.
    if (next && unread) onRead(notification.id)
  }

  return (
    <li className={cn(
      'rounded-xl border transition',
      unread ? 'border-accent/30 bg-accent/[0.05]' : 'border-border bg-white/[0.03]'
    )}>
      <div className="flex items-start gap-2 p-3">
        <button type="button" onClick={toggle} className="flex min-w-0 flex-1 items-start gap-2 text-left">
          <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', unread ? 'text-accent' : 'text-muted')} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              {unread && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />}
              <p className={cn('text-[13px]', unread ? 'font-semibold text-ink' : 'text-ink-soft')}>
                {notification.title || prettyType(notification.type)}
              </p>
              <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                {prettyType(notification.type)}
              </span>
              <span className="ml-auto shrink-0 font-mono text-[10.5px] text-muted">
                {(notification.created_at || '').slice(0, 16).replace('T', ' ')}
              </span>
            </div>
            {notification.body && (
              <p className={cn(
                'mt-0.5 text-[12.5px] text-muted',
                open ? 'whitespace-pre-wrap' : 'truncate'
              )}>
                {notification.body}
              </p>
            )}
          </div>
        </button>

        {!unread && (
          <button type="button" title="Mark as unread" disabled={busy}
                  onClick={() => onUnread(notification.id)}
                  className="shrink-0 rounded-lg border border-border p-1.5 text-muted hover:text-ink disabled:opacity-50">
            <FiRotateCcw className="h-3 w-3" />
          </button>
        )}
      </div>

      {open && notification.payload && Object.keys(notification.payload).length > 0 && (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 border-t border-border px-3 py-2 text-[11px] sm:grid-cols-3">
          {Object.entries(notification.payload).map(([key, value]) => (
            <div key={key} className="min-w-0">
              <dt className="text-muted">{key.replace(/_/g, ' ')}</dt>
              <dd className="truncate font-mono text-ink-soft">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  )
}

export default function Notifications() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [type, setType] = useState(null)
  const [offset, setOffset] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await api.listNotifications({ unreadOnly, type, limit: PAGE, offset }))
    } catch (err) {
      setError(err.message || 'Could not load your notifications')
    } finally {
      setLoading(false)
    }
  }, [unreadOnly, type, offset])

  useEffect(() => { load() }, [load])

  const act = async (fn) => {
    setBusy(true)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err.message || 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  const notifications = data?.notifications || []
  const total = data?.total ?? 0
  const unread = data?.unread ?? 0
  const types = data?.types || []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <FiBell className="h-5 w-5 text-muted" />
        <h1 className="text-lg font-semibold text-ink">Notifications</h1>
        {unread > 0 && (
          <span className="rounded-full bg-accent px-2 py-0.5 text-[11px] font-semibold text-[#04070e]">
            {unread} unread
          </span>
        )}
        <Button type="button" variant="secondary" disabled={busy || unread === 0}
                onClick={() => act(() => api.markAllNotificationsRead())}
                className="ml-auto !px-3 !py-1 !text-xs">
          <FiCheckCircle className="mr-1 h-3.5 w-3.5" /> Mark all read
        </Button>
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-xl border border-danger-fg/25 bg-danger-fg/10 px-4 py-3 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <button type="button"
                onClick={() => { setUnreadOnly(false); setOffset(0) }}
                className={cn('rounded-lg border px-2.5 py-1 text-xs transition',
                  !unreadOnly ? 'border-accent/60 bg-accent/15 text-ink'
                              : 'border-border text-ink-soft hover:text-ink')}>
          All
        </button>
        <button type="button"
                onClick={() => { setUnreadOnly(true); setOffset(0) }}
                className={cn('rounded-lg border px-2.5 py-1 text-xs transition',
                  unreadOnly ? 'border-accent/60 bg-accent/15 text-ink'
                             : 'border-border text-ink-soft hover:text-ink')}>
          Unread
        </button>
        <span className="mx-1 h-4 w-px bg-border" />
        <button type="button"
                onClick={() => { setType(null); setOffset(0) }}
                className={cn('rounded-lg border px-2.5 py-1 text-xs transition',
                  !type ? 'border-accent/60 bg-accent/15 text-ink'
                        : 'border-border text-ink-soft hover:text-ink')}>
          Every kind
        </button>
        {types.map((name) => (
          <button key={name} type="button"
                  onClick={() => { setType(name); setOffset(0) }}
                  className={cn('rounded-lg border px-2.5 py-1 text-xs transition',
                    type === name ? 'border-accent/60 bg-accent/15 text-ink'
                                  : 'border-border text-ink-soft hover:text-ink')}>
            {prettyType(name)}
          </button>
        ))}
      </div>

      <GlassCard className="p-4" hover={false}>
        {loading ? (
          <p className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
            <FiLoader className="h-4 w-4 animate-spin" /> Loading…
          </p>
        ) : !notifications.length ? (
          <div className="py-16 text-center">
            <FiInbox className="mx-auto mb-2 h-6 w-6 text-muted" />
            <p className="text-sm text-muted">
              {unreadOnly ? 'Nothing unread.' : 'No notifications yet.'}
            </p>
          </div>
        ) : (
          <>
            <ul className="space-y-2">
              {notifications.map((n) => (
                <Row key={n.id} notification={n} busy={busy}
                     onRead={(id) => act(() => api.markNotificationsRead([id]))}
                     onUnread={(id) => act(() => api.markNotificationUnread(id))} />
              ))}
            </ul>

            {total > PAGE && (
              <div className="mt-3 flex items-center justify-between text-[11.5px] text-muted">
                <span>
                  {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
                </span>
                <div className="flex gap-1.5">
                  <button type="button" disabled={offset === 0}
                          onClick={() => setOffset(Math.max(0, offset - PAGE))}
                          className="rounded-lg border border-border px-2 py-1 hover:text-ink disabled:opacity-40">
                    Newer
                  </button>
                  <button type="button" disabled={offset + PAGE >= total}
                          onClick={() => setOffset(offset + PAGE)}
                          className="rounded-lg border border-border px-2 py-1 hover:text-ink disabled:opacity-40">
                    Older
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </GlassCard>

      <p className="flex items-center gap-1.5 text-[11px] text-muted">
        <FiCheck className="h-3 w-3" />
        Opening a notification marks it read. Use the arrow on any row to put it back.
      </p>
    </div>
  )
}
