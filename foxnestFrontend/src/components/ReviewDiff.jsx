import React, { useMemo, useState } from 'react'
import { FiPlus, FiMessageSquare, FiCheck, FiClock, FiChevronDown, FiChevronRight } from 'react-icons/fi'
import Button from './ui/Button'
import { cn } from '../lib/utils'

/**
 * A reviewable diff: the side-by-side rows, with discussion attached to the lines.
 *
 * The comment anchor is the line number the server put on each row, not the row's
 * position in the table. Counting rows in the client would drift the moment a diff
 * is truncated or a row pads an uneven replacement, and a drifted anchor silently
 * files a comment against the wrong code.
 */

const GUTTER =
  'select-none border-r border-border px-2 text-right font-mono text-[10.5px] text-muted w-[1%] whitespace-nowrap align-top'

const ROW_TINT = {
  added: 'bg-success-fg/[0.07]',
  removed: 'bg-danger-fg/[0.07]',
  changed: 'bg-warning-fg/[0.06]',
  same: '',
}

function Thread({ thread, onReply, onResolve, onDelete, currentUser, busy }) {
  const [replying, setReplying] = useState(false)
  const [draft, setDraft] = useState('')
  const [collapsed, setCollapsed] = useState(thread.resolved || thread.outdated)

  const replies = thread.replies || []

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className="flex w-full items-center gap-2 rounded-lg border border-border bg-white/[0.02] px-3 py-1.5 text-left text-[11.5px] text-muted hover:text-ink"
      >
        <FiChevronRight className="h-3 w-3 shrink-0" />
        <span className="font-medium text-ink-soft">{thread.author}</span>
        <span className="truncate">{thread.body}</span>
        {thread.resolved && <span className="ml-auto shrink-0 text-success-fg">resolved</span>}
        {thread.outdated && !thread.resolved && (
          <span className="ml-auto shrink-0 text-warning-fg">outdated</span>
        )}
      </button>
    )
  }

  const renderOne = (comment, isReply) => (
    <div
      key={comment.id}
      className={cn('rounded-lg border border-border bg-white/[0.03] p-3', isReply && 'ml-5')}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="font-semibold text-ink">{comment.author}</span>
        <span className="text-muted">
          {(comment.created_at || '').slice(0, 16).replace('T', ' ')}
        </span>
        {comment.outdated && (
          <span className="inline-flex items-center gap-1 rounded-full border border-warning-fg/30 bg-warning-fg/10 px-1.5 py-0.5 text-[10px] text-warning-fg">
            <FiClock className="h-2.5 w-2.5" /> outdated
          </span>
        )}
        {currentUser && comment.author === currentUser && (
          <button
            type="button"
            onClick={() => onDelete(comment.id)}
            className="ml-auto text-[10.5px] text-muted hover:text-danger-fg"
          >
            delete
          </button>
        )}
      </div>
      <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-soft">
        {comment.body}
      </p>
    </div>
  )

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => setCollapsed(true)}
        className="flex items-center gap-1 text-[10.5px] text-muted hover:text-ink"
      >
        <FiChevronDown className="h-3 w-3" /> collapse
      </button>
      {renderOne(thread, false)}
      {replies.map((reply) => renderOne(reply, true))}

      <div className="ml-5 flex flex-wrap items-center gap-2">
        {!replying && (
          <button
            type="button"
            onClick={() => setReplying(true)}
            className="text-[11.5px] text-muted hover:text-ink"
          >
            Reply
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => onResolve(thread.id, !thread.resolved)}
          className={cn(
            'inline-flex items-center gap-1 text-[11.5px] disabled:opacity-50',
            thread.resolved ? 'text-muted hover:text-ink' : 'text-success-fg hover:brightness-125'
          )}
        >
          <FiCheck className="h-3 w-3" />
          {thread.resolved ? 'Unresolve' : 'Resolve'}
        </button>
      </div>

      {replying && (
        <div className="ml-5 space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="Reply…"
            className="w-full rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
          />
          <div className="flex gap-2">
            <Button
              type="button"
              disabled={!draft.trim() || busy}
              onClick={async () => {
                await onReply(thread.id, draft.trim())
                setDraft('')
                setReplying(false)
              }}
              className="!px-3 !py-1 !text-xs"
            >
              Reply
            </Button>
            <button
              type="button"
              onClick={() => { setReplying(false); setDraft('') }}
              className="text-[11.5px] text-muted hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function CommentComposer({ onSubmit, onCancel, busy }) {
  const [draft, setDraft] = useState('')
  return (
    <div className="space-y-2 rounded-lg border border-accent/30 bg-accent/[0.05] p-3">
      <textarea
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={3}
        placeholder="Comment on this line…"
        className="w-full rounded-lg border border-border bg-white/[0.04] px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
      />
      <div className="flex gap-2">
        <Button
          type="button"
          disabled={!draft.trim() || busy}
          onClick={async () => { await onSubmit(draft.trim()); setDraft('') }}
          className="!px-3 !py-1 !text-xs"
        >
          Comment
        </Button>
        <button type="button" onClick={onCancel} className="text-[11.5px] text-muted hover:text-ink">
          Cancel
        </button>
      </div>
    </div>
  )
}

function FileDiff({ file, onComment, onReply, onResolve, onDelete, currentUser, busy }) {
  const [open, setOpen] = useState(true)
  // Which line is being commented on, as "side:line" so the two sides never collide.
  const [composing, setComposing] = useState(null)

  const threadsByAnchor = useMemo(() => {
    const map = {}
    for (const thread of file.threads || []) {
      const key = `${thread.side}:${thread.line}`
      ;(map[key] = map[key] || []).push(thread)
    }
    return map
  }, [file.threads])

  const rows = file.diff?.rows || []
  const stats = file.diff?.stats || { added: 0, removed: 0 }

  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 bg-white/[0.04] px-3 py-2 text-left"
      >
        {open ? <FiChevronDown className="h-3.5 w-3.5 text-muted" /> : <FiChevronRight className="h-3.5 w-3.5 text-muted" />}
        <span className="font-mono text-[12px] text-ink">{file.file_path}</span>
        <span className="rounded border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
          {file.status}
        </span>
        {file.owners?.length > 0 && (
          <span className="text-[11px] text-muted">owners: {file.owners.join(', ')}</span>
        )}
        <span className="ml-auto flex items-center gap-2 text-[11px]">
          {file.threads?.length > 0 && (
            <span className="inline-flex items-center gap-1 text-info-fg">
              <FiMessageSquare className="h-3 w-3" /> {file.threads.length}
            </span>
          )}
          <span className="text-success-fg">+{stats.added}</span>
          <span className="text-danger-fg">-{stats.removed}</span>
        </span>
      </button>

      {open && (
        <div className="overflow-x-auto">
          {file.is_binary ? (
            <p className="px-4 py-6 text-center text-sm text-muted">Binary file — not shown.</p>
          ) : (
            <table className="w-full border-collapse font-mono text-[12px] leading-[1.55]">
              <tbody>
                {rows.map((row, index) => {
                  // A comment belongs to the side that has a line here: the new side
                  // where there is one, the old side for a pure deletion.
                  const side = row.current_line != null ? 'new' : 'old'
                  const line = row.current_line ?? row.previous_line
                  const anchor = line != null ? `${side}:${line}` : null
                  const threads = anchor ? threadsByAnchor[anchor] || [] : []

                  return (
                    <React.Fragment key={index}>
                      <tr className={cn('group', ROW_TINT[row.type])}>
                        <td className={GUTTER}>{row.previous_line ?? ''}</td>
                        <td className={GUTTER}>{row.current_line ?? ''}</td>
                        <td className="w-[1%] px-1 align-top">
                          {anchor && (
                            <button
                              type="button"
                              title="Comment on this line"
                              onClick={() => setComposing(composing === anchor ? null : anchor)}
                              className="opacity-0 transition group-hover:opacity-100 focus:opacity-100"
                            >
                              <FiPlus className="h-3 w-3 text-accent" />
                            </button>
                          )}
                        </td>
                        <td className="whitespace-pre-wrap break-words px-2 align-top text-ink">
                          {row.type === 'removed' || row.type === 'changed'
                            ? row.previous || ' '
                            : row.current || ' '}
                          {row.type === 'changed' && row.current !== row.previous && (
                            <span className="block text-success-fg">{row.current || ' '}</span>
                          )}
                        </td>
                      </tr>

                      {(threads.length > 0 || composing === anchor) && (
                        <tr>
                          <td colSpan={4} className="bg-white/[0.02] px-4 py-3">
                            <div className="space-y-3">
                              {threads.map((thread) => (
                                <Thread
                                  key={thread.id}
                                  thread={thread}
                                  onReply={onReply}
                                  onResolve={onResolve}
                                  onDelete={onDelete}
                                  currentUser={currentUser}
                                  busy={busy}
                                />
                              ))}
                              {composing === anchor && (
                                <CommentComposer
                                  busy={busy}
                                  onCancel={() => setComposing(null)}
                                  onSubmit={async (body) => {
                                    await onComment(file.file_path, line, side, body)
                                    setComposing(null)
                                  }}
                                />
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  )
                })}
              </tbody>
            </table>
          )}
          {file.diff?.truncated && (
            <p className="border-t border-border px-4 py-2 text-[11.5px] text-warning-fg">
              Diff truncated — this file is too large to show in full.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default function ReviewDiff({ bundle, onComment, onReply, onResolve, onDelete, currentUser, busy }) {
  const files = bundle?.files || []
  if (!files.length) {
    return (
      <p className="py-16 text-center text-sm text-muted">
        No file changes between these branches.
      </p>
    )
  }
  return (
    <div className="space-y-3">
      {files.map((file) => (
        <FileDiff
          key={file.file_path}
          file={file}
          onComment={onComment}
          onReply={onReply}
          onResolve={onResolve}
          onDelete={onDelete}
          currentUser={currentUser}
          busy={busy}
        />
      ))}
    </div>
  )
}
