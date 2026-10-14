import React, { useState } from 'react'
import { Link } from 'react-router-dom'
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
  FiXCircle,
  FiActivity,
  FiChevronDown,
  FiFileText,
  FiKey,
  FiSend,
  FiFilter,
  FiTag,
  FiArrowRight,
} from 'react-icons/fi'
import { activityTargetLink, profileLink } from '../lib/links'
import { absoluteTime, relativeTime } from '../lib/time'
import { cn } from '../lib/utils'

/**
 * The activity log as a timeline: events in order, newest first.
 *
 * Rows now carry what they are about -- the branch, the commit before and after, the
 * pull request -- and how the person was signed in, so each row links to the thing
 * it describes. Rows written before that existed show their description only.
 */

const ICONS = {
  code: FiGitCommit,
  review: FiCheckCircle,
  repository: FiFolder,
  settings: FiShield,
  access: FiKey,
}

const TYPE_ICONS = {
  push_commit: FiGitCommit,
  branch_created: FiGitBranch,
  branch_deleted: FiTrash2,
  branch_renamed: FiGitBranch,
  branch_force_updated: FiGitBranch,
  merge_branch: FiGitMerge,
  merge_pull_request: FiGitMerge,
  resolve_merge_conflicts: FiGitMerge,
  reject_commit: FiXCircle,
  close_pull_request: FiXCircle,
  archive_repository: FiArchive,
  delete_repository: FiTrash2,
  gc: FiTrash2,
  generate_docs: FiFileText,
  generate_project_docs: FiFileText,
  webhook_created: FiSend,
  webhook_updated: FiSend,
  webhook_deleted: FiSend,
  webhook_secret_rotated: FiSend,
  push_rule_created: FiFilter,
  push_rule_updated: FiFilter,
  push_rule_deleted: FiFilter,
  tag_created: FiTag,
  tag_deleted: FiTag,
  release_created: FiTag,
  create_user: FiUserPlus,
}

const TONES = {
  code: 'text-accent',
  review: 'text-success-fg',
  repository: 'text-ink-soft',
  settings: 'text-warning-fg',
  access: 'text-info-fg',
}

const DANGER = new Set(['branch_deleted', 'branch_force_updated', 'delete_repository', 'credentials_revoked',
  'rollback_branch', 'delete_commit', 'gc'])

function credentialText(credential) {
  if (!credential) return null
  if (credential === 'session') return 'password sign-in'
  const [kind, ...rest] = credential.split(':')
  const name = rest.join(':')
  if (kind === 'token') return `access token “${name}”`
  if (kind === 'ssh_key') return `SSH key “${name}”`
  return credential
}

function Detail({ label, children, mono }) {
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 text-muted">{label}</dt>
      <dd className={cn('min-w-0 flex-1 break-words text-ink', mono && 'break-all font-mono text-[11px]')}>
        {children}
      </dd>
    </div>
  )
}

export default function ActivityFeed({ activities = [], loading, emptyHint, className, showRepository = true }) {
  // One row open at a time: the panels are tall enough that several open at once
  // turns the timeline back into a wall of text.
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
    <ol className={cn('relative space-y-1', className)} data-testid="activity-feed">
      <span
        className="absolute bottom-4 left-[15px] top-4 w-px bg-gradient-to-b from-border via-border to-transparent"
        aria-hidden
      />
      {activities.map((item, index) => {
        const type = item.activity_type || item.type || ''
        const Icon = TYPE_ICONS[type] || ICONS[item.category] || FiActivity
        const tone = DANGER.has(type) ? 'text-danger-fg' : TONES[item.category] || 'text-ink-soft'
        const rowId = item.id ?? `idx-${index}`
        const isOpen = openId === rowId
        const username = item.username || item.user
        const repository = item.repository_name || item.repository
        const label = item.label || type.replace(/_/g, ' ')
        const description = item.description || label
        const ref = item.payload?.ref
        const link = activityTargetLink(item)
        const credential = credentialText(item.credential)

        return (
          <li key={rowId} className="relative" data-testid="activity-row">
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
                <span className="mt-1 flex flex-wrap items-center gap-x-2 font-mono text-[10.5px] text-muted">
                  <span title={absoluteTime(item.created_at)}>{relativeTime(item.created_at)}</span>
                  <span aria-hidden>·</span>
                  <span>{label}</span>
                  {showRepository && repository && (
                    <>
                      <span aria-hidden>·</span>
                      <span className="truncate">{repository}</span>
                    </>
                  )}
                  {credential && item.credential !== 'session' && (
                    <>
                      <span aria-hidden>·</span>
                      <span className="truncate">via {credential}</span>
                    </>
                  )}
                </span>
              </span>
              <FiChevronDown
                className={cn('mt-1.5 h-3.5 w-3.5 shrink-0 text-muted transition-transform', isOpen && 'rotate-180')}
                aria-hidden
              />
            </button>

            {isOpen && (
              <div className="mb-2 ml-11 mr-1 rounded-xl border border-border bg-white/[0.03] p-3">
                <dl className="space-y-2 text-[12px]">
                  <Detail label="Event">{label}</Detail>
                  {username && (
                    <Detail label="By">
                      <Link to={profileLink(username)} className="text-accent hover:underline">{username}</Link>
                      {credential && <span className="ml-1.5 text-muted">with {credential}</span>}
                    </Detail>
                  )}
                  {repository && <Detail label="Repository">{repository}</Detail>}
                  {item.created_at && (
                    <Detail label="When">
                      {absoluteTime(item.created_at)}
                      <span className="ml-1.5 text-muted">({relativeTime(item.created_at)})</span>
                    </Detail>
                  )}
                  {ref?.branch && <Detail label="Branch">{ref.renamed_from ? `${ref.renamed_from} → ${ref.branch}` : ref.branch}</Detail>}
                  {ref && (ref.before || ref.after) && (
                    <Detail label="Moved" mono>
                      {(ref.before || 'none').slice(0, 12)} → {(ref.after || 'none').slice(0, 12)}
                      {ref.operation && <span className="ml-1.5 font-sans text-muted">({ref.operation.replace('_', ' ')})</span>}
                      {ref.created && <span className="ml-1.5 font-sans text-muted">— new branch</span>}
                    </Detail>
                  )}
                  {item.commit_id && !ref && <Detail label="Commit" mono>{item.commit_id}</Detail>}
                  {item.client_ip && <Detail label="From" mono>{item.client_ip}</Detail>}
                  {item.payload?.system && <Detail label="Note">Made by the server on the owner's behalf.</Detail>}
                  {item.description && (
                    <div className="flex gap-2 border-t border-border pt-2">
                      <dt className="w-24 shrink-0 text-muted">Details</dt>
                      <dd className="min-w-0 flex-1 whitespace-pre-wrap break-words text-ink-soft">{item.description}</dd>
                    </div>
                  )}
                </dl>
                {link && (
                  <Link
                    to={link}
                    className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-[12px] text-ink hover:bg-accent/20"
                    data-testid="activity-open"
                  >
                    Open <FiArrowRight className="h-3 w-3" />
                  </Link>
                )}
              </div>
            )}
          </li>
        )
      })}
    </ol>
  )
}
