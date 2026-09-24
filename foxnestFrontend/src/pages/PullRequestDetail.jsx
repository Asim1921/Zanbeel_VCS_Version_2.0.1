import React, { useCallback, useEffect, useState } from 'react'
import {
  FiArrowLeft, FiGitPullRequest, FiFileText, FiGitCommit, FiMessageSquare,
  FiCheck, FiX, FiAlertCircle, FiLoader, FiShield, FiUsers,
} from 'react-icons/fi'
import api from '../utils/api'
import { getSessionUsername } from '../utils/session'
import ReviewDiff from '../components/ReviewDiff'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import { cn } from '../lib/utils'

/**
 * Reviewing a pull request.
 *
 * A page rather than a dialog: a review means reading a long diff, arguing on
 * specific lines and then deciding, and a dialog gives none of those room. The merge
 * panel deliberately lists every blocker rather than greying out a button, because
 * "why can I not merge this" is the question people actually have.
 */

const TABS = [
  { id: 'conversation', label: 'Conversation', icon: FiMessageSquare },
  { id: 'files', label: 'Files changed', icon: FiFileText },
  { id: 'commits', label: 'Commits', icon: FiGitCommit },
]

const STATE_LABEL = {
  approved: { text: 'approved', variant: 'success' },
  changes_requested: { text: 'requested changes', variant: 'danger' },
  commented: { text: 'commented', variant: 'default' },
}

function MergePanel({ summary, onMerge, merging, canAct }) {
  if (!summary) return null
  const ok = summary.can_merge
  const owners = summary.code_owners || {}

  return (
    <GlassCard className="p-4" hover={false}>
      <div className="mb-3 flex items-center gap-2">
        <FiShield className={cn('h-4 w-4', ok ? 'text-success-fg' : 'text-warning-fg')} />
        <h3 className="text-sm font-semibold text-ink">
          {ok ? 'Ready to merge' : 'Cannot merge yet'}
        </h3>
      </div>

      {!ok && (
        <ul className="mb-3 space-y-1.5">
          {(summary.blockers || []).map((blocker, i) => (
            <li key={i} className="flex items-start gap-2 text-[12.5px] text-warning-fg">
              <FiAlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {blocker}
            </li>
          ))}
        </ul>
      )}

      <dl className="mb-3 grid grid-cols-2 gap-2 text-[11.5px]">
        <div>
          <dt className="text-muted">Approvals</dt>
          <dd className="text-ink">
            {(summary.approvals || []).length} of {summary.required_approvals} required
          </dd>
        </div>
        {(summary.stale_approvals || []).length > 0 && (
          <div>
            <dt className="text-muted">Stale</dt>
            <dd className="text-warning-fg">
              {summary.stale_approvals.length} went stale when the branch moved
            </dd>
          </div>
        )}
        {owners.enabled && (
          <div className="col-span-2">
            <dt className="flex items-center gap-1 text-muted">
              <FiUsers className="h-3 w-3" /> Code owners
            </dt>
            <dd className="text-ink">
              {owners.missing?.length
                ? `waiting on ${owners.missing.join(', ')}`
                : 'all required owners have approved'}
            </dd>
            {owners.unknown_owners?.length > 0 && (
              <dd className="text-warning-fg">
                CODEOWNERS names unknown user(s): {owners.unknown_owners.join(', ')}
              </dd>
            )}
          </div>
        )}
      </dl>

      <Button
        type="button"
        disabled={!ok || merging || !canAct}
        onClick={onMerge}
        className="w-full"
      >
        {merging ? 'Merging…' : 'Merge pull request'}
      </Button>
    </GlassCard>
  )
}

function ReviewBox({ onSubmit, busy, isAuthor }) {
  const [state, setState] = useState('approved')
  const [body, setBody] = useState('')

  return (
    <GlassCard className="p-4" hover={false}>
      <h3 className="mb-3 text-sm font-semibold text-ink">Submit a review</h3>
      {isAuthor && (
        <p className="mb-2 text-[11.5px] text-muted">
          This is your pull request. Approving your own work does not count toward the gate.
        </p>
      )}
      <textarea
        rows={3}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Leave a note for the author…"
        className="mb-2 w-full rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
      />
      <div className="flex flex-wrap items-center gap-2">
        {['approved', 'changes_requested', 'commented'].map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setState(option)}
            className={cn(
              'rounded-lg border px-2.5 py-1 text-xs transition',
              state === option
                ? 'border-accent/60 bg-accent/15 text-ink'
                : 'border-border text-ink-soft hover:border-border-strong hover:text-ink'
            )}
          >
            {STATE_LABEL[option].text}
          </button>
        ))}
        <Button
          type="button"
          disabled={busy}
          onClick={async () => { await onSubmit(state, body); setBody('') }}
          className="!px-3 !py-1 !text-xs"
        >
          Submit
        </Button>
      </div>
    </GlassCard>
  )
}

export default function PullRequestDetail({ repo, prId, onBack }) {
  const [pr, setPr] = useState(null)
  const [summary, setSummary] = useState(null)
  const [reviews, setReviews] = useState([])
  const [bundle, setBundle] = useState(null)
  const [commits, setCommits] = useState([])

  const [tab, setTab] = useState('conversation')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [merging, setMerging] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const me = getSessionUsername()

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [detail, reviewData, files] = await Promise.all([
        api.getPullRequest(repo.id, prId),
        api.listReviews(repo.id, prId),
        api.getPullRequestFiles(repo.id, prId).catch(() => null),
      ])
      setPr(detail.pull_request || detail)
      setReviews(reviewData.reviews || [])
      setSummary(reviewData)
      setBundle(files)
    } catch (err) {
      setError(err.message || 'Could not load this pull request')
    } finally {
      setLoading(false)
    }
  }, [repo.id, prId])

  useEffect(() => { load() }, [load])

  // Commits are only needed when that tab is opened.
  useEffect(() => {
    if (tab !== 'commits' || commits.length || !pr) return
    api.getCommits(repo.id, false, pr.source_branch)
      .then((res) => setCommits(res.commits || []))
      .catch(() => {})
  }, [tab, commits.length, pr, repo.id])

  const act = async (fn, successMessage) => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await fn()
      if (successMessage) setNotice(successMessage)
      await load()
    } catch (err) {
      setError(err.message || 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  const submitReview = (state, body) =>
    act(() => api.submitReview(repo.id, prId, state, body), 'Review submitted.')

  const addComment = (filePath, line, side, body) =>
    act(() => api.addPullRequestComment(repo.id, prId, {
      file_path: filePath, line, side, body,
    }))

  const replyToComment = (parentId, body) =>
    act(() => api.addPullRequestComment(repo.id, prId, { in_reply_to_id: parentId, body }))

  const resolveComment = (commentId, resolved) =>
    act(() => api.resolvePullRequestComment(repo.id, prId, commentId, resolved))

  const deleteComment = (commentId) =>
    act(() => api.deletePullRequestComment(repo.id, prId, commentId))

  const merge = async () => {
    setMerging(true)
    setError('')
    try {
      await api.mergePullRequest(repo.id, prId, summary?.source_head_commit_id || null)
      setNotice('Merged.')
      await load()
    } catch (err) {
      setError(err.message || 'Merge failed')
    } finally {
      setMerging(false)
    }
  }

  if (loading) {
    return (
      <GlassCard className="p-6" hover={false}>
        <p className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
          <FiLoader className="h-4 w-4 animate-spin" /> Loading pull request…
        </p>
      </GlassCard>
    )
  }

  const status = pr?.status || 'open'
  const statusVariant = status === 'merged' ? 'accent' : status === 'closed' ? 'danger' : 'success'
  const isAuthor = pr?.created_by === me || pr?.author === me
  const commentCount = bundle?.comments?.total ?? 0

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink"
        >
          <FiArrowLeft className="h-4 w-4" /> Back
        </button>
        <FiGitPullRequest className="h-4 w-4 text-muted" />
        <h1 className="text-lg font-semibold text-ink">{pr?.title}</h1>
        <Badge variant={statusVariant}>{status}</Badge>
        <span className="font-mono text-[11.5px] text-muted">
          {pr?.source_branch} → {pr?.target_branch}
        </span>
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-xl border border-danger-fg/25 bg-danger-fg/10 px-4 py-3 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
      )}
      {notice && !error && (
        <p className="flex items-start gap-2 rounded-xl border border-success-fg/25 bg-success-fg/10 px-4 py-3 text-sm text-success-fg">
          <FiCheck className="mt-0.5 h-4 w-4 shrink-0" /> {notice}
        </p>
      )}

      <div className="flex flex-wrap gap-1 border-b border-border">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              'inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] transition',
              tab === id
                ? 'border-accent text-ink'
                : 'border-transparent text-muted hover:text-ink'
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
            {id === 'files' && bundle?.totals?.files ? (
              <span className="ml-1 rounded-full bg-white/[0.08] px-1.5 text-[10px] text-ink-soft">
                {bundle.totals.files}
              </span>
            ) : null}
            {id === 'conversation' && commentCount ? (
              <span className="ml-1 rounded-full bg-white/[0.08] px-1.5 text-[10px] text-ink-soft">
                {commentCount}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {tab === 'conversation' && (
        <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
          <div className="space-y-4">
            <GlassCard className="p-4" hover={false}>
              <h3 className="mb-2 text-sm font-semibold text-ink">Description</h3>
              <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-soft">
                {pr?.description || 'No description given.'}
              </p>
            </GlassCard>

            <GlassCard className="p-4" hover={false}>
              <h3 className="mb-3 text-sm font-semibold text-ink">Reviews</h3>
              {!reviews.length ? (
                <p className="py-6 text-center text-sm text-muted">No reviews yet.</p>
              ) : (
                <ol className="space-y-2">
                  {reviews.map((review) => {
                    const label = STATE_LABEL[review.state] || STATE_LABEL.commented
                    return (
                      <li
                        key={review.id}
                        className="rounded-lg border border-border bg-white/[0.03] p-3"
                      >
                        <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
                          <span className="font-semibold text-ink">{review.reviewer}</span>
                          <Badge variant={label.variant}>{label.text}</Badge>
                          <span className="ml-auto font-mono text-[10.5px] text-muted">
                            {(review.submitted_at || review.created_at || '')
                              .slice(0, 16).replace('T', ' ')}
                          </span>
                        </div>
                        {review.body && (
                          <p className="mt-1.5 whitespace-pre-wrap text-[12.5px] text-ink-soft">
                            {review.body}
                          </p>
                        )}
                      </li>
                    )
                  })}
                </ol>
              )}
            </GlassCard>

            {status === 'open' && (
              <ReviewBox onSubmit={submitReview} busy={busy} isAuthor={isAuthor} />
            )}
          </div>

          <div className="space-y-4">
            {status === 'open' && (
              <MergePanel
                summary={summary}
                onMerge={merge}
                merging={merging}
                canAct={status === 'open'}
              />
            )}
            {status !== 'open' && (
              <GlassCard className="p-4" hover={false}>
                <p className="text-sm text-muted">
                  This pull request is {status}.
                  {pr?.merge_commit_id && (
                    <> Merge commit{' '}
                      <span className="font-mono text-ink">
                        {String(pr.merge_commit_id).slice(0, 12)}
                      </span>.
                    </>
                  )}
                </p>
              </GlassCard>
            )}
          </div>
        </div>
      )}

      {tab === 'files' && (
        <GlassCard className="p-4" hover={false}>
          {bundle?.comments?.outdated > 0 && (
            <p className="mb-3 text-[11.5px] text-warning-fg">
              {bundle.comments.outdated} comment(s) refer to lines that have changed since
              they were written, and are collapsed as outdated.
            </p>
          )}
          <ReviewDiff
            bundle={bundle}
            onComment={addComment}
            onReply={replyToComment}
            onResolve={resolveComment}
            onDelete={deleteComment}
            currentUser={me}
            busy={busy}
          />
        </GlassCard>
      )}

      {tab === 'commits' && (
        <GlassCard className="p-4" hover={false}>
          {!commits.length ? (
            <p className="py-10 text-center text-sm text-muted">No commits to show.</p>
          ) : (
            <ol className="space-y-1.5">
              {commits.map((commit) => (
                <li
                  key={commit.id}
                  className="flex flex-wrap items-baseline gap-2 rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[12.5px]"
                >
                  <span className="font-mono text-[11px] text-accent">
                    {String(commit.id).slice(0, 8)}
                  </span>
                  <span className="text-ink">{commit.message}</span>
                  <span className="ml-auto text-[11px] text-muted">{commit.author}</span>
                </li>
              ))}
            </ol>
          )}
        </GlassCard>
      )}
    </div>
  )
}
