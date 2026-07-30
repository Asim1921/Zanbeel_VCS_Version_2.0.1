import { FiMenu, FiLogOut, FiBell, FiCheck, FiChevronDown, FiChevronUp, FiExternalLink, FiDownload } from 'react-icons/fi'
import React, { useEffect, useMemo, useState } from 'react'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import api from '../../utils/api'

/** Legacy rows stored the full CLI guide in `body`; keep commands only in the downloadable .txt. */
function getNotificationDisplayBody(n) {
  const t = (n?.type || '').toLowerCase()
  const p = n?.payload || {}
  if (t === 'issue_access_request_approved' && p.cli_setup_guide) {
    const lines = ['✅ REPOSITORY ACCESS GRANTED', '']
    if (p.repository_name) lines.push(`Repository: ${p.repository_name}`)
    if (p.repository_owner) lines.push(`Owner: @${p.repository_owner}`)
    lines.push('Permission Level: Developer (write access)')
    if (n.created_at) {
      try {
        lines.push(`Granted At: ${new Date(n.created_at).toLocaleString()}`)
      } catch {
        // ignore invalid date
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
  const role = currentUser?.role === 'team_lead' ? 'Admin' : 'User'

  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [notifications, setNotifications] = useState([])
  const [notifError, setNotifError] = useState(null)
  const [notifLoading, setNotifLoading] = useState(false)
  const [expandedNotifIds, setExpandedNotifIds] = useState(() => new Set())

  const unreadCount = useMemo(
    () => (notifications || []).filter(n => !n.is_read).length,
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
    setExpandedNotifIds(prev => {
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
          // Ensure the repositories page is mounted before sending the open-issue event.
          if (typeof setActiveTab === 'function' && activeTab !== 'repositories') {
            setActiveTab('repositories')
            setTimeout(() => {
              window.dispatchEvent(new CustomEvent('foxnest:open-issue', { detail: { repository_id: repositoryId, issue_number: issueNumber } }))
            }, 50)
          } else {
            window.dispatchEvent(new CustomEvent('foxnest:open-issue', { detail: { repository_id: repositoryId, issue_number: issueNumber } }))
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
        // Notification payloads may include backend-style "/api/..." URLs.
        // The frontend API client already prefixes "/api", so strip to avoid "/api/api/...".
        const normalizedUrl = (url || '').startsWith('/api/') ? (url || '').slice(4) : url
        await api.request(normalizedUrl, {
          method: 'POST',
          body: { comment: '' }
        })
        await loadNotifications()
        try {
          window.dispatchEvent(new CustomEvent('foxnest:access-requests-updated'))
        } catch {
          // ignore
        }
        return
      }

      // REVIEW or unknown: open as best-effort in a new tab.
      if (typeof window !== 'undefined') {
        window.open(url, '_blank', 'noopener,noreferrer')
      }
    } catch (err) {
      setNotifError(err?.message || 'Action failed')
    }
  }

  useEffect(() => {
    // Poll lightly to keep the bell fresh without hammering the server.
    loadNotifications()
    const t = setInterval(loadNotifications, 30000)
    return () => clearInterval(t)
  }, [currentUser?.username])

  return (
    <header className="sticky top-0 z-10">
      {/* Gradient background */}
      <div className="absolute inset-0 bg-gradient-to-r from-purple-900/40 via-pink-900/40 to-purple-900/40 backdrop-blur-xl"></div>
      <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-transparent"></div>
      <div className="absolute bottom-0 inset-x-0 h-px bg-gradient-to-r from-transparent via-purple-400/50 to-transparent"></div>
      
      {/* Content */}
      <div className="relative flex items-center justify-between h-16 px-6">
        {/* Left side */}
        <div className="flex items-center space-x-4">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="p-2 rounded-lg text-white/70 hover:text-white hover:bg-white/10 transition-colors lg:hidden"
          >
            <FiMenu className="w-5 h-5" />
          </button>
          
          {/* Title with gradient */}
          <div className="hidden md:block">
            <h2 className="text-lg font-bold bg-gradient-to-r from-purple-300 via-pink-300 to-purple-300 bg-clip-text text-transparent">
              Zanbeel: Version Control System
            </h2>
          </div>
        </div>

        {/* Right side */}
        <div className="flex items-center space-x-4">
          <div className="relative">
            <button
              type="button"
              onClick={() => setNotificationsOpen(true)}
              className="relative p-2 rounded-lg text-white/70 hover:text-white hover:bg-white/10 transition-colors"
              title="Notifications"
            >
              <FiBell className="w-5 h-5" />
              {unreadCount > 0 && (
                <span className="absolute -top-1 -right-1 min-w-5 h-5 px-1 rounded-full bg-pink-500 text-white text-[11px] flex items-center justify-center">
                  {unreadCount > 99 ? '99+' : unreadCount}
                </span>
              )}
            </button>
          </div>
          {/* User Avatar */}
          <div className="relative">
            <button className="flex items-center space-x-3 p-2 rounded-xl bg-gradient-to-r from-purple-500/20 to-pink-500/20 hover:from-purple-500/30 hover:to-pink-500/30 border border-purple-400/30 transition-all">
              <div className="w-8 h-8 bg-gradient-to-br from-purple-400 to-pink-400 rounded-full flex items-center justify-center shadow-lg">
                <span className="text-white font-bold text-sm">{displayName.charAt(0).toUpperCase()}</span>
              </div>
              <span className="hidden sm:block text-white font-medium">{displayName}</span>
              <span className="hidden sm:block text-xs text-white/60">{role}</span>
            </button>
          </div>
          <button
            onClick={onLogout}
            className="p-2 rounded-lg text-white/70 hover:text-white hover:bg-white/10 transition-colors"
            title="Sign out"
          >
            <FiLogOut className="w-5 h-5" />
          </button>
        </div>
      </div>

      {notificationsOpen && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 backdrop-blur-sm p-4">
          <div className="absolute inset-0" onClick={() => setNotificationsOpen(false)} />
          <GlassCard className="relative mt-16 w-full max-w-2xl p-5" hover={false}>
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <p className="text-xs uppercase tracking-[0.16em] text-white/50">Notifications</p>
                <p className="text-white/80 text-sm">Mentions, comments, and status changes.</p>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={loadNotifications} disabled={notifLoading}>
                  Refresh
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setNotificationsOpen(false)}>
                  Close
                </Button>
              </div>
            </div>

            {notifError && (
              <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
                {notifError}
              </div>
            )}

            <div className="max-h-[60vh] overflow-auto space-y-2">
              {notifLoading ? (
                <div className="py-10 text-white/70 text-center">Loading…</div>
              ) : notifications.length === 0 ? (
                <p className="text-white/50 text-sm">No notifications.</p>
              ) : (
                notifications.map((n) => (
                  <div key={n.id} className={`rounded-lg border p-3 ${n.is_read ? 'border-white/10 bg-white/5' : 'border-pink-400/30 bg-pink-500/10'}`}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-white font-medium truncate">{n.title || n.type}</p>
                        {(() => {
                          const bodyText = getNotificationDisplayBody(n)
                          return bodyText ? (
                            <p className="text-white/70 text-sm mt-1 whitespace-pre-wrap">{bodyText}</p>
                          ) : null
                        })()}

                        {n?.payload?.cli_setup_guide && (
                          <div className="mt-3">
                            <button
                              type="button"
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
                              className="inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-emerald-900/40 bg-gradient-to-r from-emerald-500 via-teal-500 to-cyan-600 hover:from-emerald-400 hover:via-teal-400 hover:to-cyan-500 border border-emerald-300/30 transition-all"
                              title="Download full FoxNest CLI quick start as a text file"
                            >
                              <FiDownload className="w-4 h-4 shrink-0" />
                              Download CLI quick start (.txt)
                            </button>
                            <p className="text-[11px] text-white/45 mt-1.5">Step-by-step commands are in the file only.</p>
                          </div>
                        )}

                        <p className="text-xs text-white/40 mt-2">{n.created_at ? new Date(n.created_at).toLocaleString() : ''}</p>

                        {(n?.payload?.repository_id && n?.payload?.issue_number) && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => handleNotificationAction({ type: 'OPEN_ISSUE' }, n)}
                              className="inline-flex items-center gap-1 text-xs text-white/90 border border-white/20 rounded px-2 py-1 hover:bg-white/10"
                              title="Open issue"
                            >
                              <FiExternalLink className="w-3.5 h-3.5" />
                              Open issue #{n.payload.issue_number}
                            </button>
                          </div>
                        )}

                        {(() => {
                          const otherActions = (Array.isArray(n?.payload?.actions) ? n.payload.actions : []).filter(
                            (a) => String(a?.type || '').toUpperCase() !== 'DOWNLOAD_TXT'
                          )
                          if (otherActions.length === 0) return null
                          return (
                            <div className="mt-2">
                              <button
                                type="button"
                                onClick={() => toggleExpanded(n.id)}
                                className="inline-flex items-center gap-1 text-xs text-white/80 border border-white/20 rounded px-2 py-1 hover:bg-white/10"
                                title="Show details"
                              >
                                {expandedNotifIds.has(n.id) ? <FiChevronUp className="w-3.5 h-3.5" /> : <FiChevronDown className="w-3.5 h-3.5" />}
                                Details
                              </button>
                            </div>
                          )
                        })()}

                        {expandedNotifIds.has(n.id) && (
                          <div className="mt-3 space-y-3">
                            {Array.isArray(n?.payload?.actions) && (
                              <div className="flex flex-wrap gap-2">
                                {n.payload.actions
                                  .filter((action) => String(action?.type || '').toUpperCase() !== 'DOWNLOAD_TXT')
                                  .map((action, idx) => (
                                    <button
                                      key={`${n.id}_action_${idx}`}
                                      type="button"
                                      onClick={() => handleNotificationAction(action, n)}
                                      className="inline-flex items-center gap-1 text-xs text-white/90 border border-white/20 rounded px-2 py-1 hover:bg-white/10"
                                      title={action?.url || ''}
                                    >
                                      <FiExternalLink className="w-3.5 h-3.5" />
                                      {action?.type || 'OPEN'}
                                    </button>
                                  ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                      {!n.is_read && (
                        <button
                          type="button"
                          onClick={async () => {
                            try {
                              await api.markNotificationsRead([n.id])
                              setNotifications(prev => prev.map(x => (x.id === n.id ? { ...x, is_read: true } : x)))
                            } catch {
                              // ignore
                            }
                          }}
                          className="shrink-0 inline-flex items-center gap-1 text-xs text-white/80 border border-white/20 rounded px-2 py-1 hover:bg-white/10"
                          title="Mark as read"
                        >
                          <FiCheck className="w-3.5 h-3.5" />
                          Read
                        </button>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </GlassCard>
        </div>
      )}
    </header>
  )
}

export default Header
