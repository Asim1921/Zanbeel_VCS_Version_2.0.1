import React from 'react'
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
  resolve_merge_conflicts: FiGitMerge,
  update_branch_policy: FiShield,
  archive_repository: FiArchive,
  gc: FiTrash2,
  register_user: FiUserPlus,
}

const TONES = {
  push_commit: 'text-accent',
  merge_branch: 'text-success-fg',
  create_pull_request: 'text-info-fg',
  review_pull_request: 'text-success-fg',
  archive_repository: 'text-warning-fg',
  gc: 'text-danger-fg',
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
        return (
          <li key={item.id ?? index} className="relative flex gap-3 rounded-lg px-1 py-2">
            <span
              className={cn(
                'relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
                'border border-border bg-surface',
                tone
              )}
            >
              <Icon className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0 flex-1 pt-0.5">
              <p className="text-[13px] leading-snug text-ink-soft">
                {item.username && (
                  <span className="font-medium text-ink">{item.username}</span>
                )}{' '}
                {item.description || type.replace(/_/g, ' ')}
              </p>
              <p className="mt-1 flex items-center gap-2 font-mono text-[10.5px] text-muted">
                <span>{relativeTime(item.created_at)}</span>
                {item.repository_name && (
                  <>
                    <span aria-hidden>·</span>
                    <span className="truncate">{item.repository_name}</span>
                  </>
                )}
              </p>
            </div>
          </li>
        )
      })}
    </ol>
  )
}
