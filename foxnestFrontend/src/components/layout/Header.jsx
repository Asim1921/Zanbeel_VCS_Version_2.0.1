import { FiMenu, FiLogOut, FiBell, FiCheck, FiChevronDown, FiChevronUp, FiExternalLink, FiDownload } from 'react-icons/fi'
import React, { useEffect, useMemo, useState } from 'react'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Modal from '../ui/Modal'
import api from '../../utils/api'

function getNotificationDisplayBody(n) {
  const t = (n?.type || '').toLowerCase()
  const p = n?.payload || {}
  if (t === 'issue_access_request_approved' && p.cli_setup_guide) {
    const lines = ['REPOSITORY ACCESS GRANTED', '']
    if (p.repository_name) lines.push(`Repository: ${p.repository_name}`)
    if (p.repository_owner) lines.push(`Owner: @${p.repository_owner}`)
    lines.push('Permission Level: Developer (write access)')
    if (n.created_at) {
      try {
        lines.push(`Granted At: ${new Date(n.created_at).toLocaleString()}`)
      } catch {
        // ignore
      }
    }
    if (p.repository_id) lines.push(`Repository ID: ${p.repository_id}`)
    if (p.issue_number != null && p.issue_number !== '') lines.push(`Issue: #${p.issue_number}`)
    return lines.join('\n')
  }
  return n?.body || ''
}

const Header = ({ sidebarOpen, setSidebarOpen, activeTab, setActiveTab, currentUser, onLogout }) => {
  const displayName = currentUser?.full_name || currentUser?.username || 'User'
  const role = currentUser?.role === 'team_lead' || currentUser?.role === 'admin' ? 'Admin' : 'User'

  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [notifications, setNotifications] = useState([])
  const [notifError, setNotifError] = useState(null)
  const [notifLoading, setNotifLoading] = useState(false)
  const [expandedNotifIds, setExpandedNotifIds] = useState(() => new Set())

  const unreadCount = useMemo(
    () => (notifications || []).filter((n) => !n.is_read).length,
    [notifications]
  )

  const loadNotifications = async () => {
    try {
      setNotifLoading(true)
      setNotifError(null)
      const res = await api.listNotifications({ unreadOnly: false, limit: 50 })
      setNotifications(res.notifications || [])
    } catch (err) {
      setNotifError(err.message || 'Unable to load notifications')
    } finally {
      setNotifLoading(false)
    }
  }

  const toggleExpanded = (id) => {
    setExpandedNotifIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleNotificationAction = async (action, notif) => {
    const type = (action?.type || '').toUpperCase()
    const url = action?.url
    const actionPayload = action?.payload || {}

    if (type === 'OPEN_ISSUE') {
      const repositoryId = actionPayload.repository_id || notif?.payload?.repository_id
      const issueNumber = actionPayload.issue_number || notif?.payload?.issue_number
      if (repositoryId && issueNumber) {
        try {
          if (typeof setActiveTab === 'function' && activeTab !== 'repositories') {
            setActiveTab('repositories')
            setTimeout(() => {
              window.dispatchEvent(
                new CustomEvent('foxnest:open-issue', {
                  detail: { repository_id: repositoryId, issue_number: issueNumber },
                })
              )
            }, 50)
          } else {
            window.dispatchEvent(
              new CustomEvent('foxnest:open-issue', {
                detail: { repository_id: repositoryId, issue_number: issueNumber },
              })
            )
          }
          setNotificationsOpen(false)
        } catch {
          // ignore
        }
      }
      return
    }

    if (type === 'DOWNLOAD_TXT') {
      const text = actionPayload.text || notif?.payload?.cli_setup_guide || ''
      const filename = actionPayload.filename || 'foxnest-cli-quickstart.txt'
      try {
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
        const objectUrl = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = objectUrl
        a.download = filename
        document.body.appendChild(a)
        a.click()
        a.remove()
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
      } catch {
        // ignore
      }
      return
    }

    if (!url) return

    try {
      if (type === 'APPROVE' || type === 'DENY') {
        const ok = window.confirm(`${type === 'APPROVE' ? 'Approve' : 'Deny'} this access request?`)
        if (!ok) return
        const normalizedUrl = (url || '').startsWith('/api/') ? (url || '').slice(4) : url
        await api.request(normalizedUrl, {
          method: 'POST',
          body: { comment: '' },
        })
        await loadNotifications()
        try {
          window.dispatchEvent(new CustomEvent('foxnest:access-requests-updated'))
        } catch {
          // ignore
        }
        return
      }

      if (typeof window !== 'undefined') {
        window.open(url, '_blank', 'noopener,noreferrer')
      }
    } catch (err) {
      setNotifError(err?.message || 'Action failed')
    }
  }

  useEffect(() => {
    loadNotifications()
    const t = setInterval(loadNotifications, 30000)
    return () => clearInterval(t)
  }, [currentUser?.username])

  return (
    <header className="sticky top-0 z-10 px-3 pt-2 sm:px-4 sm:pt-3 md:px-6">
      <div className="panel-float relative flex h-14 items-center justify-between gap-2 px-3 sm:px-4 md:h-16 md:px-5">
        <div className="flex min-w-0 items-center gap-2 sm:gap-3">
          <button
            type="button"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="shrink-0 rounded-full p-2 text-ink-soft transition hover:bg-cream-deep hover:text-ink lg:hidden"
            aria-label="Open menu"
          >
            <FiMenu className="h-5 w-5" />
          </button>
          <div className="flex min-w-0 items-center gap-2 sm:gap-3">
            <img
              src="/zanbeel-logo.png"
              alt=""
              className="hidden h-8 w-8 shrink-0 rounded-full object-cover ring-1 ring-black/5 sm:block"
              aria-hidden
            />
            <h2 className="truncate text-sm font-semibold tracking-tight text-ink sm:text-[15px]">
              Zanbeel Workspace
            </h2>
            <span className="hidden items-center gap-1.5 rounded-full bg-success-bg px-2.5 py-1 text-[11px] font-medium text-success-fg sm:inline-flex">
              <span className="h-1.5 w-1.5 rounded-full bg-success-fg" />
              Live
            </span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          <button
            type="button"
            onClick={() => setNotificationsOpen(true)}
            className="relative rounded-full border border-border bg-surface p-2 text-ink-soft transition hover:bg-cream-mid hover:text-ink"
            title="Notifications"
          >
            <FiBell className="h-4 w-4" />
            {unreadCount > 0 && (
              <span className="absolute -top-1 -right-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-white">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>

          <div className="flex items-center gap-2 rounded-full border border-border bg-cream-mid/80 py-1 pr-2 pl-1 sm:pr-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-accent text-white">
              <span className="text-xs font-semibold">{displayName.charAt(0).toUpperCase()}</span>
            </div>
            <div className="hidden min-w-0 md:block">
              <p className="max-w-[120px] truncate text-sm font-medium leading-tight text-ink">{displayName}</p>
              <p className="text-[11px] leading-tight text-muted">{role}</p>
            </div>
          </div>

          <Button variant="primary" size="sm" onClick={onLogout} title="Sign out" className="!px-3">
            <FiLogOut className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Sign out</span>
          </Button>
        </div>
      </div>

      {/* The panel used to hand-roll its own overlay with `bg-ink/40`. After the
          dark-theme flip `ink` is near-white, so the scrim *lightened* the page
          instead of dimming it and the unbacked container let the dashboard show
          straight through the text. Modal already solves the scrim, the panel
          background, stacking, Escape and the scroll lock, so use it. */}
      <Modal
        open={notificationsOpen}
        onClose={() => setNotificationsOpen(false)}
        title="Notifications"
        subtitle="Mentions, comments, and status changes."
        panelClassName="max-w-2xl"
      >
        <div>
            <div className="mb-4 flex justify-end">
              <Button variant="ghost" size="sm" onClick={loadNotifications} disabled={notifLoading}>
                Refresh
              </Button>
            </div>

            {notifError && (
              <div className="mb-3 rounded-xl border border-danger-fg/20 bg-danger-bg px-3 py-2 text-sm text-danger-fg">
                {notifError}
              </div>
            )}

            <div className="max-h-[60vh] space-y-2 overflow-auto">
              {notifLoading ? (
                <div className="py-10 text-center text-muted">Loading…</div>
              ) : notifications.length === 0 ? (
                <p className="text-sm text-muted">No notifications.</p>
              ) : (
                notifications.map((n) => (
                  <div
                    key={n.id}
                    className={`rounded-xl border p-3 ${
                      n.is_read
                        ? 'border-border bg-cream-mid/50'
                        : 'border-border-strong bg-cream-deep'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate font-medium text-ink">{n.title || n.type}</p>
                        {(() => {
                          const bodyText = getNotificationDisplayBody(n)
                          return bodyText ? (
                            <p className="mt-1 whitespace-pre-wrap text-sm text-ink-soft">{bodyText}</p>
                          ) : null
                        })()}

                        {n?.payload?.cli_setup_guide && (
                          <div className="mt-3">
                            <Button
                              size="sm"
                              onClick={() =>
                                handleNotificationAction(
                                  {
                                    type: 'DOWNLOAD_TXT',
                                    payload: {
                                      filename: `${n?.payload?.repository_name || 'foxnest'}-foxnest-cli-quickstart.txt`,
                                      text: n.payload.cli_setup_guide,
                                    },
                                  },
                                  n
                                )
                              }
                            >
                              <FiDownload className="h-4 w-4" />
                              Download CLI quick start
                            </Button>
                            <p className="mt-1.5 text-[11px] text-muted">
                              Step-by-step commands are in the file only.
                            </p>
                          </div>
                        )}

                        <p className="mt-2 text-xs text-muted">
                          {n.created_at ? new Date(n.created_at).toLocaleString() : ''}
                        </p>

                        {n?.payload?.repository_id && n?.payload?.issue_number && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => handleNotificationAction({ type: 'OPEN_ISSUE' }, n)}
                              className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs text-ink hover:bg-cream-mid"
                            >
                              <FiExternalLink className="h-3.5 w-3.5" />
                              Open issue #{n.payload.issue_number}
                            </button>
                          </div>
                        )}

                        {(() => {
                          const otherActions = (
                            Array.isArray(n?.payload?.actions) ? n.payload.actions : []
                          ).filter((a) => String(a?.type || '').toUpperCase() !== 'DOWNLOAD_TXT')
                          if (otherActions.length === 0) return null
                          return (
                            <div className="mt-2">
                              <button
                                type="button"
                                onClick={() => toggleExpanded(n.id)}
                                className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs text-ink-soft hover:bg-cream-mid"
                              >
                                {expandedNotifIds.has(n.id) ? (
                                  <FiChevronUp className="h-3.5 w-3.5" />
                                ) : (
                                  <FiChevronDown className="h-3.5 w-3.5" />
                                )}
                                Details
                              </button>
                            </div>
                          )
                        })()}

                        {expandedNotifIds.has(n.id) && Array.isArray(n?.payload?.actions) && (
                          <div className="mt-3 flex flex-wrap gap-2">
                            {n.payload.actions
                              .filter((action) => String(action?.type || '').toUpperCase() !== 'DOWNLOAD_TXT')
                              .map((action, idx) => (
                                <button
                                  key={`${n.id}_action_${idx}`}
                                  type="button"
                                  onClick={() => handleNotificationAction(action, n)}
                                  className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs text-ink hover:bg-cream-mid"
                                >
                                  <FiExternalLink className="h-3.5 w-3.5" />
                                  {action?.type || 'OPEN'}
                                </button>
                              ))}
                          </div>
                        )}
                      </div>
                      {!n.is_read && (
                        <button
                          type="button"
                          onClick={async () => {
                            try {
                              await api.markNotificationsRead([n.id])
                              setNotifications((prev) =>
                                prev.map((x) => (x.id === n.id ? { ...x, is_read: true } : x))
                              )
                            } catch {
                              // ignore
                            }
                          }}
                          className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs text-ink-soft hover:bg-cream-mid"
                        >
                          <FiCheck className="h-3.5 w-3.5" />
                          Read
                        </button>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
        </div>
      </Modal>
    </header>
  )
}

export default Header
