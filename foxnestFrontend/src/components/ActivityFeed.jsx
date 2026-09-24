import React, { useState } from 'react'
import {
  FiGitCommit,
  FiGitMerge,
  FiGitBranch,
  FiFolder,
  FiUserPlus,
  FiShield,
  FiTrash2,
  FiArchive,
  FiCheckCircle,
  FiActivity,
  FiChevronDown,
  FiFileText,
  FiKey,
} from 'react-icons/fi'
import { cn } from '../lib/utils'

/**
 * The platform activity feed.
 *
 * The API has served this for a long time and nothing ever rendered it, so
 * administrators had no way to see who did what. Laid out as a timeline rather
 * than a table: these are events in order, and the eye should follow the thread.
 */

const ICONS = {
  push_commit: FiGitCommit,
  pending_commit: FiGitCommit,
  merge_branch: FiGitMerge,
  publish_branch: FiGitBranch,
  copy_files: FiGitBranch,
  create_repository: FiFolder,
  request_repository: FiFolder,
  fork_repository: FiGitBranch,
  create_pull_request: FiGitMerge,
  review_pull_request: FiCheckCircle,
  merge_pull_request: FiGitMerge,
  resolve_merge_conflicts: FiGitMerge,
  update_branch_policy: FiShield,
  archive_repository: FiArchive,
  gc: FiTrash2,
  register_user: FiUserPlus,
  generate_docs: FiFileText,
  generate_project_docs: FiFileText,
  grant_access_to_issue: FiKey,
  password_reset: FiKey,
  password_reset_otp_sent: FiKey,
}

const TONES = {
  push_commit: 'text-accent',
  merge_branch: 'text-success-fg',
  create_pull_request: 'text-info-fg',
  review_pull_request: 'text-success-fg',
  merge_pull_request: 'text-success-fg',
  archive_repository: 'text-warning-fg',
  gc: 'text-danger-fg',
}

const TYPE_LABELS = {
  push_commit: 'Commit pushed',
  pending_commit: 'Commit submitted for review',
  merge_branch: 'Branch merged',
  publish_branch: 'Branch published',
  copy_files: 'Files copied between branches',
  create_repository: 'Repository created',
  request_repository: 'Repository requested',
  fork_repository: 'Repository forked',
  create_pull_request: 'Pull request opened',
  review_pull_request: 'Pull request reviewed',
  merge_pull_request: 'Pull request merged',
  resolve_merge_conflicts: 'Merge conflicts resolved',
  generate_docs: 'Documentation generated',
  generate_project_docs: 'Project documentation generated',
  grant_access_to_issue: 'Issue access granted',
  update_branch_policy: 'Branch policy updated',
  archive_repository: 'Repository archived',
  gc: 'Garbage collection',
  register_user: 'User registered',
  password_reset: 'Password reset',
  password_reset_otp_sent: 'Password reset code sent',
  cherry_pick: 'Commit cherry-picked',
  revert: 'Commit reverted',
  rebase: 'Branch rebased',
}

function humanType(type) {
  return TYPE_LABELS[type] || (type ? type.replace(/_/g, ' ') : 'Activity')
}

/**
 * Pull the identifiers already embedded in a description into named fields.
 *
 * The server writes descriptions like "Pushed commit to 'resetPass' from aa3fe0cc
 * to 272690889b6dce81: ..." -- the useful parts are in there, but the row truncates
 * them. Surfacing them separately means a full commit id can be read and copied
 * without another request, since the feed endpoint returns no structured detail.
 */
function extractDetails(description) {
  const text = description || ''
  const details = []

  const quoted = [...text.matchAll(/'([^']{1,80})'/g)].map((m) => m[1])
  if (quoted.length === 1) {
    details.push({ label: 'Branch', value: quoted[0] })
  } else if (quoted.length > 1) {
    details.push({ label: 'Branches', value: quoted.join(' → ') })
  }

  // 8+ hex characters is a commit id here; shorter runs are too easy to hit by chance.
  const hashes = [...new Set([...text.matchAll(/\b[0-9a-f]{8,40}\b/g)].map((m) => m[0]))]
  if (hashes.length === 1) {
    details.push({ label: 'Commit', value: hashes[0], mono: true })
  } else if (hashes.length >= 2) {
    details.push({ label: 'From', value: hashes[0], mono: true })
    details.push({ label: 'To', value: hashes[1], mono: true })
  }

  const pr = text.match(/\bPR #(\d+)/i) || text.match(/\bpull request #(\d+)/i)
  if (pr) details.push({ label: 'Pull request', value: `#${pr[1]}` })

  return details
}

function absoluteTime(iso) {
  if (!iso) return ''
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return ''
  return when.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function relativeTime(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
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

export default function ActivityFeed({ activities = [], loading, emptyHint, className }) {
  // Which row is expanded. One at a time: these panels are tall enough that several
  // open at once turns the timeline back into a wall of text.
  const [openId, setOpenId] = useState(null)

  if (loading) {
    return (
      <div className={cn('space-y-3', className)}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex gap-3">
            <div className="h-8 w-8 shrink-0 animate-pulse rounded-lg bg-white/[0.06]" />
            <div className="flex-1 space-y-2 py-1">
              <div className="h-3 w-3/4 animate-pulse rounded bg-white/[0.06]" />
              <div className="h-2.5 w-1/3 animate-pulse rounded bg-white/[0.04]" />
            </div>
          </div>
        ))}
      </div>
    )
  }

  if (!activities.length) {
    return (
      <div className={cn('py-10 text-center', className)}>
        <FiActivity className="mx-auto mb-3 h-6 w-6 text-muted" />
        <p className="text-sm text-muted">{emptyHint || 'Nothing has happened yet.'}</p>
      </div>
    )
  }

  return (
    <ol className={cn('relative space-y-1', className)}>
      {/* the thread the timeline hangs from */}
      <span
        className="absolute bottom-4 left-[15px] top-4 w-px bg-gradient-to-b from-border via-border to-transparent"
        aria-hidden
      />
      {activities.map((item, index) => {
        const type = item.activity_type || item.type || ''
        const Icon = ICONS[type] || FiActivity
        const tone = TONES[type] || 'text-ink-soft'
        // Fall back to the index so rows without an id still toggle independently.
        const rowId = item.id ?? `idx-${index}`
        const isOpen = openId === rowId
        const username = item.username || item.user
        const repository = item.repository_name || item.repository
        const description = item.description || humanType(type)
        const details = isOpen ? extractDetails(item.description) : []

        return (
          <li key={rowId} className="relative">
            <button
              type="button"
              onClick={() => setOpenId(isOpen ? null : rowId)}
              aria-expanded={isOpen}
              className={cn(
                'flex w-full gap-3 rounded-lg px-1 py-2 text-left transition',
                'hover:bg-white/[0.04] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                isOpen && 'bg-white/[0.04]'
              )}
            >
              <span
                className={cn(
                  'relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
                  'border border-border bg-surface',
                  tone
                )}
              >
                <Icon className="h-3.5 w-3.5" />
              </span>
              <span className="min-w-0 flex-1 pt-0.5">
                <span className="block text-[13px] leading-snug text-ink-soft">
                  {username && <span className="font-medium text-ink">{username}</span>}{' '}
                  <span className={cn(!isOpen && 'line-clamp-2')}>{description}</span>
                </span>
                <span className="mt-1 flex items-center gap-2 font-mono text-[10.5px] text-muted">
                  <span>{relativeTime(item.created_at)}</span>
                  {repository && (
                    <>
                      <span aria-hidden>·</span>
                      <span className="truncate">{repository}</span>
                    </>
                  )}
                </span>
              </span>
              <FiChevronDown
                className={cn(
                  'mt-1.5 h-3.5 w-3.5 shrink-0 text-muted transition-transform',
                  isOpen && 'rotate-180'
                )}
                aria-hidden
              />
            </button>

            {isOpen && (
              <div className="ml-11 mr-1 mb-2 rounded-xl border border-border bg-white/[0.03] p-3">
                <dl className="space-y-2 text-[12px]">
                  <div className="flex gap-2">
                    <dt className="w-20 shrink-0 text-muted">Event</dt>
                    <dd className="min-w-0 flex-1 text-ink">{humanType(type)}</dd>
                  </div>
                  {username && (
                    <div className="flex gap-2">
                      <dt className="w-20 shrink-0 text-muted">By</dt>
                      <dd className="min-w-0 flex-1 text-ink">{username}</dd>
                    </div>
                  )}
                  {repository && (
                    <div className="flex gap-2">
                      <dt className="w-20 shrink-0 text-muted">Repository</dt>
                      <dd className="min-w-0 flex-1 break-words text-ink">{repository}</dd>
                    </div>
                  )}
                  {item.created_at && (
                    <div className="flex gap-2">
                      <dt className="w-20 shrink-0 text-muted">When</dt>
                      <dd className="min-w-0 flex-1 text-ink">
                        {absoluteTime(item.created_at)}
                        <span className="ml-1.5 text-muted">({relativeTime(item.created_at)})</span>
                      </dd>
                    </div>
                  )}
                  {details.map((detail) => (
                    <div key={detail.label} className="flex gap-2">
                      <dt className="w-20 shrink-0 text-muted">{detail.label}</dt>
                      <dd
                        className={cn(
                          'min-w-0 flex-1 break-all text-ink',
                          detail.mono && 'font-mono text-[11px]'
                        )}
                      >
                        {detail.value}
                      </dd>
                    </div>
                  ))}
                  {item.description && (
                    <div className="flex gap-2 border-t border-border pt-2">
                      <dt className="w-20 shrink-0 text-muted">Details</dt>
                      <dd className="min-w-0 flex-1 whitespace-pre-wrap break-words text-ink-soft">
                        {item.description}
                      </dd>
                    </div>
                  )}
                </dl>
              </div>
            )}
          </li>
        )
      })}
    </ol>
  )
}
