import React, { useCallback, useEffect, useState } from 'react'
import {
  FiArrowLeft, FiLoader, FiAlertCircle, FiGitCommit, FiGitMerge,
  FiGitBranch, FiTag, FiChevronDown, FiChevronRight, FiFilePlus, FiFileMinus, FiFileText,
} from 'react-icons/fi'
import api from '../utils/api'
import GlassCard from './ui/GlassCard'
import Badge from './ui/Badge'
import { cn } from '../lib/utils'

/**
 * One commit: what it changed, and nothing else.
 *
 * Only files this commit touched are listed. Showing the whole tree would bury the
 * handful that moved, which is the entire question someone has when they click a
 * commit out of a list.
 */

const STATUS = {
  added: { label: 'added', icon: FiFilePlus, tone: 'text-success-fg border-success-fg/30 bg-success-fg/10' },
  removed: { label: 'deleted', icon: FiFileMinus, tone: 'text-danger-fg border-danger-fg/30 bg-danger-fg/10' },
  modified: { label: 'modified', icon: FiFileText, tone: 'text-info-fg border-info-fg/30 bg-info-fg/10' },
}

const GUTTER =
  'select-none border-r border-border px-2 text-right font-mono text-[10.5px] text-muted w-[1%] whitespace-nowrap align-top'

const ROW_TINT = {
  added: 'bg-success-fg/[0.07]',
  removed: 'bg-danger-fg/[0.07]',
  changed: 'bg-warning-fg/[0.06]',
  same: '',
}

function FileEntry({ file, defaultOpen, onLoadDiff }) {
  const [open, setOpen] = useState(defaultOpen)
  const [loaded, setLoaded] = useState(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')

  const meta = STATUS[file.status] || STATUS.modified
  const Icon = meta.icon
  const effective = loaded || file
  const stats = file.diff?.stats || { added: 0, removed: 0 }
  const rows = effective.diff?.rows || []
  // Left out of the response because the commit was too large to send in full.
  const omitted = file.diff_omitted && !loaded

  const loadDiff = async () => {
    setLoading(true)
    setLoadError('')
    try {
      setLoaded(await onLoadDiff(file.file_path))
    } catch (err) {
      setLoadError(err.message || 'Could not load this diff')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 bg-white/[0.04] px-3 py-2 text-left"
      >
        {open
          ? <FiChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
          : <FiChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />}
        <span className={cn('inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]', meta.tone)}>
          <Icon className="h-2.5 w-2.5" /> {meta.label}
        </span>
        <span className="truncate font-mono text-[12px] text-ink">{file.file_path}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[11px]">
          {stats.added > 0 && <span className="text-success-fg">+{stats.added}</span>}
          {stats.removed > 0 && <span className="text-danger-fg">-{stats.removed}</span>}
        </span>
      </button>

      {open && (
        <div className="overflow-x-auto">
          {file.is_binary ? (
            <p className="px-4 py-6 text-center text-sm text-muted">
              Binary file — not shown.
            </p>
          ) : omitted ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-muted">
                This commit is large, so this file was not sent with the rest.
              </p>
              {loadError && (
                <p className="mt-1 text-[12px] text-danger-fg">{loadError}</p>
              )}
              <button
                type="button"
                disabled={loading}
                onClick={loadDiff}
                className="mt-2 rounded-lg border border-accent/40 bg-accent/10 px-3 py-1 text-[12px] text-ink hover:bg-accent/20 disabled:opacity-50"
              >
                {loading ? 'Loading…' : 'Load diff'}
              </button>
            </div>
          ) : !rows.length ? (
            <p className="px-4 py-6 text-center text-sm text-muted">
              No textual changes to show.
            </p>
          ) : (
            <table className="w-full border-collapse font-mono text-[12px] leading-[1.55]">
              <tbody>
                {rows.map((row, index) => (
                  <tr key={index} className={ROW_TINT[row.type]}>
                    <td className={GUTTER}>{row.previous_line ?? ''}</td>
                    <td className={GUTTER}>{row.current_line ?? ''}</td>
                    <td className="w-[1%] select-none px-1 text-center align-top text-muted">
                      {row.type === 'added' ? '+' : row.type === 'removed' ? '-' : ''}
                    </td>
                    <td className="whitespace-pre-wrap break-words px-2 align-top text-ink">
                      {row.type === 'removed' || row.type === 'changed'
                        ? (row.previous || ' ')
                        : (row.current || ' ')}
                      {row.type === 'changed' && row.current !== row.previous && (
                        <span className="block text-success-fg">{row.current || ' '}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {effective.diff?.truncated && (
            <p className="border-t border-border px-4 py-2 text-[11.5px] text-warning-fg">
              Diff truncated — this file is too large to show in full.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default function CommitDetail({ repo, commitId, onBack, onOpenCommit }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [against, setAgainst] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.getCommitChanges(repo.id, commitId, { against })
      setData(res)
    } catch (err) {
      setError(err.message || 'Could not load this commit')
    } finally {
      setLoading(false)
    }
  }, [repo.id, commitId, against])

  useEffect(() => { load() }, [load])

  if (loading) {
    return (
      <GlassCard className="p-6" hover={false}>
        <p className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
          <FiLoader className="h-4 w-4 animate-spin" /> Loading commit…
        </p>
      </GlassCard>
    )
  }

  if (error) {
    return (
      <GlassCard className="p-6" hover={false}>
        <p className="flex items-start gap-2 py-8 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
        <button type="button" onClick={onBack} className="text-sm text-muted hover:text-ink">
          Back to commits
        </button>
      </GlassCard>
    )
  }

  const commit = data?.commit || {}
  const files = data?.files || []
  const totals = data?.totals || {}

  const summary = [
    totals.files_added ? `${totals.files_added} added` : null,
    totals.files_modified ? `${totals.files_modified} modified` : null,
    totals.files_removed ? `${totals.files_removed} deleted` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="space-y-4">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink"
      >
        <FiArrowLeft className="h-4 w-4" /> Back to commits
      </button>

      <GlassCard className="p-4" hover={false}>
        <div className="flex flex-wrap items-center gap-2">
          {commit.is_merge
            ? <FiGitMerge className="h-4 w-4 text-muted" />
            : <FiGitCommit className="h-4 w-4 text-muted" />}
          <h2 className="text-[15px] font-semibold text-ink">{commit.subject}</h2>
          {commit.is_merge && <Badge variant="info">merge</Badge>}
          {commit.is_root && <Badge variant="accent">root commit</Badge>}
        </div>

        {commit.message && commit.message !== commit.subject && (
          <p className="mt-2 whitespace-pre-wrap border-l-2 border-border pl-3 text-[12.5px] leading-relaxed text-ink-soft">
            {commit.message.split('\n').slice(1).join('\n').trim()}
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11.5px] text-muted">
          <span className="font-mono text-accent">{commit.short_id}</span>
          <span>{commit.author}</span>
          <span>{(commit.timestamp || '').slice(0, 19).replace('T', ' ')}</span>
          {(commit.branches || []).map((name) => (
            <span key={name} className="inline-flex items-center gap-1 rounded-full border border-info-fg/30 bg-info-fg/10 px-1.5 py-0.5 text-info-fg">
              <FiGitBranch className="h-2.5 w-2.5" /> {name}
            </span>
          ))}
          {(commit.tags || []).map((name) => (
            <span key={name} className="inline-flex items-center gap-1 rounded-full border border-warning-fg/30 bg-warning-fg/10 px-1.5 py-0.5 text-warning-fg">
              <FiTag className="h-2.5 w-2.5" /> {name}
            </span>
          ))}
        </div>

        {(commit.parents || []).length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
            <span>{commit.parents.length > 1 ? 'parents' : 'parent'}:</span>
            {commit.parents.map((parent) => (
              <button
                key={parent}
                type="button"
                onClick={() => onOpenCommit?.(parent)}
                className="font-mono text-accent hover:underline"
              >
                {parent.slice(0, 8)}
              </button>
            ))}
          </div>
        )}

        {commit.is_merge && (
          <div className="mt-3 rounded-lg border border-border bg-white/[0.03] p-2.5">
            <p className="text-[11.5px] text-muted">
              A merge is shown against its first parent — what this merge brought onto
              the branch. Compare against:
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {commit.parents.map((parent, index) => (
                <button
                  key={parent}
                  type="button"
                  onClick={() => setAgainst(index === 0 ? null : parent)}
                  className={cn(
                    'rounded-lg border px-2 py-0.5 font-mono text-[11px] transition',
                    (data.compared_against === parent)
                      ? 'border-accent/60 bg-accent/15 text-ink'
                      : 'border-border text-ink-soft hover:border-border-strong hover:text-ink'
                  )}
                >
                  {parent.slice(0, 8)}{index === 0 ? ' (first)' : ''}
                </button>
              ))}
            </div>
          </div>
        )}
      </GlassCard>

      <GlassCard className="p-4" hover={false}>
        <div className="mb-3 flex flex-wrap items-baseline gap-2">
          <h3 className="text-sm font-semibold text-ink">
            {totals.files || 0} file{totals.files === 1 ? '' : 's'} changed
          </h3>
          {summary && <span className="text-[11.5px] text-muted">{summary}</span>}
          {totals.files_not_loaded > 0 && (
            <span className="text-[11.5px] text-warning-fg">
              — {totals.files_not_loaded} large file(s) load on demand
            </span>
          )}
          <span className="ml-auto flex items-center gap-2 text-[12px]">
            <span className="text-success-fg">+{totals.added || 0}</span>
            <span className="text-danger-fg">-{totals.removed || 0}</span>
          </span>
        </div>

        {!files.length ? (
          <p className="py-10 text-center text-sm text-muted">
            This commit changed no files.
          </p>
        ) : (
          <div className="space-y-3">
            {files.map((file) => (
              <FileEntry
                key={file.file_path}
                file={file}
                onLoadDiff={async (filePath) => {
                  const res = await api.getCommitChanges(repo.id, commitId, {
                    against, path: filePath,
                  })
                  return (res.files || [])[0] || null
                }}
                // Open everything for a small commit; collapse a large one so the page
                // is readable before anything is expanded.
                defaultOpen={files.length <= 10}
              />
            ))}
          </div>
        )}
      </GlassCard>
    </div>
  )
}
