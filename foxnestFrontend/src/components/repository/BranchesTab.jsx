import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  FiGitBranch, FiEdit3, FiTrash2, FiStar, FiCrosshair, FiRefreshCw, FiAlertTriangle,
  FiCheckCircle, FiPlus, FiSearch, FiShield, FiLock, FiGitPullRequest, FiRotateCcw,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'
import { pullRequestLink } from '../../lib/links'
import { absoluteTime, relativeTime } from '../../lib/time'
import { cn } from '../../lib/utils'

/**
 * Every branch, as a list worth reading: what was last committed to it and by whom, how
 * far it has drifted from the default branch, whether it is protected, and which pull
 * requests use it -- with the operations the caller is allowed, and only those.
 *
 * The server says what the caller may do (`permissions`) and what each branch accepts
 * (`protection.permits`), so a button is never offered for an action that will be
 * refused. A deleted branch can be restored from the notice that confirms it.
 */

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'stale', label: 'Stale' },
]

const MODE_BADGE = {
  protected: { variant: 'warning', label: 'Protected', icon: FiShield },
  frozen: { variant: 'info', label: 'Frozen', icon: FiLock },
  archived: { variant: 'default', label: 'Archived', icon: FiLock },
}

const selectClass =
  'w-full rounded-xl border border-border bg-cream-mid px-3 py-2.5 text-sm text-ink outline-none focus:border-accent/70'

function Distance({ ahead, behind, scale, compared }) {
  if (ahead == null || behind == null) {
    return <span className="text-[11px] text-muted" title="History too long to compare">—</span>
  }
  const width = (n) => `${Math.max(n ? 6 : 0, Math.round((n / Math.max(scale, 1)) * 100))}%`
  return (
    <div
      className="flex w-36 shrink-0 items-center gap-1.5"
      title={`${behind} commit(s) behind and ${ahead} ahead of ${compared}`}
      data-testid="branch-distance"
    >
      <span className="w-7 text-right font-mono text-[11px] text-muted">{behind}</span>
      <div className="flex h-1.5 flex-1">
        <div className="flex flex-1 justify-end overflow-hidden rounded-l-full bg-white/[0.05]">
          <div className="h-full rounded-l-full bg-muted/60" style={{ width: width(behind) }} />
        </div>
        <div className="w-px bg-border-strong" />
        <div className="flex-1 overflow-hidden rounded-r-full bg-white/[0.05]">
          <div className="h-full rounded-r-full bg-accent/70" style={{ width: width(ahead) }} />
        </div>
      </div>
      <span className="w-7 font-mono text-[11px] text-muted">{ahead}</span>
    </div>
  )
}

export default function BranchesTab({ repo, onOpenBranch, onOpenCommit, onChanged }) {
  const repoId = repo.id
  const [data, setData] = useState({ branches: [], permissions: {}, default_branch: null })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')

  const [creating, setCreating] = useState(null)
  const [tags, setTags] = useState([])
  const [renaming, setRenaming] = useState(null)
  const [moving, setMoving] = useState(null)
  const [deleting, setDeleting] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.getBranchesDetailed(repoId)
      setData({
        branches: response.branches || [],
        permissions: response.permissions || {},
        default_branch: response.default_branch,
      })
    } catch (err) {
      setError(err.message || 'Could not load branches')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => { load() }, [load])

  const run = async (fn, success) => {
    setBusy(true)
    setError(null)
    try {
      const result = await fn()
      setNotice(typeof success === 'function' ? success(result) : { text: success })
      await load()
      onChanged?.()
      return true
    } catch (err) {
      setError(err.message || 'That did not work')
      return false
    } finally {
      setBusy(false)
    }
  }

  const { branches, permissions } = data
  const canWrite = Boolean(permissions.can_write)
  const canManage = Boolean(permissions.can_manage)

  const scale = useMemo(
    () => Math.max(1, ...branches.flatMap((b) => [b.ahead || 0, b.behind || 0])),
    [branches]
  )

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return branches.filter((b) => {
      if (q && !b.name.toLowerCase().includes(q)) return false
      if (filter === 'active' && b.stale) return false
      if (filter === 'stale' && !b.stale) return false
      return true
    })
  }, [branches, filter, query])

  const defaultRow = visible.find((b) => b.is_default)
  const others = visible.filter((b) => !b.is_default)

  // --- actions --------------------------------------------------------------------

  const openCreate = async () => {
    setCreating({ name: '', kind: 'branch', source: data.default_branch || '' })
    try {
      const response = await api.listTags(repoId)
      setTags(response.tags || [])
    } catch {
      setTags([])
    }
  }

  const submitCreate = async () => {
    const { name, kind, source } = creating
    const from = kind === 'branch' ? { fromBranch: source }
      : kind === 'tag' ? { fromTag: source } : { fromCommit: source.trim() }
    const ok = await run(() => api.createBranch(repoId, { name: name.trim(), ...from }),
      `Created ${name.trim()} from ${kind === 'commit' ? source.trim().slice(0, 12) : source}`)
    if (ok) setCreating(null)
  }

  const submitRename = async () => {
    const from = renaming.branch.name
    const to = renaming.newName.trim()
    const ok = await run(() => api.renameBranch(repoId, from, to), (result) => ({
      text: `Renamed ${from} to ${to}` + (result?.retargeted_pull_requests?.length
        ? `; ${result.retargeted_pull_requests.length} open pull request(s) now use the new name`
        : ''),
    }))
    if (ok) setRenaming(null)
  }

  const openMove = async (branch) => {
    setMoving({ branch, commitId: branch.head_commit_id || '', commits: [] })
    try {
      const response = await api.getCommits(repoId, false, branch.name)
      setMoving((m) => (m ? { ...m, commits: response.commits || [] } : m))
    } catch {
      // The commit id field still works.
    }
  }

  const submitMove = async () => {
    const { branch, commitId } = moving
    const ok = await run(
      () => api.moveBranchHead(repoId, branch.name, commitId.trim(), branch.head_commit_id),
      `Moved ${branch.name} to ${commitId.trim().slice(0, 12)}`
    )
    if (ok) setMoving(null)
  }

  const submitDelete = async () => {
    const branch = deleting
    const ok = await run(() => api.deleteBranch(repoId, branch.name), (result) => ({
      text: `Deleted ${branch.name}`,
      restore: { name: branch.name, head: result?.branch?.head_commit_id || branch.head_commit_id },
    }))
    if (ok) setDeleting(null)
  }

  const restore = (entry) =>
    run(() => api.createBranch(repoId, { name: entry.name, fromCommit: entry.head }),
      `Restored ${entry.name} at ${String(entry.head).slice(0, 12)}`)

  const setDefault = (branch) =>
    run(() => api.setDefaultBranch(repoId, branch.name), `${branch.name} is now the default branch`)

  // --- rendering ----------------------------------------------------------------------

  const deleteBlock = (b) => {
    if (b.is_default) return 'The default branch cannot be deleted'
    if (!b.protection?.permits?.delete) return `${b.name} is ${b.protection?.mode}; it cannot be deleted`
    if (b.pull_requests?.length) {
      return `Used by open pull request ${b.pull_requests.map((p) => `#${p.id}`).join(', ')}`
    }
    return null
  }

  const renderRow = (b) => {
    const mode = MODE_BADGE[b.protection?.mode]
    const blockedDelete = deleteBlock(b)
    const blockedRename = b.protection?.permits?.rename === false ? `${b.name} is ${b.protection.mode}` : null
    const blockedMove = b.protection?.permits?.update === false && b.protection?.permits?.force_update === false
      ? `${b.name} is ${b.protection.mode}` : null
    return (
      <div
        key={b.name}
        data-testid="branch-row"
        data-branch={b.name}
        className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3 last:border-b-0"
      >
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <FiGitBranch className="h-3.5 w-3.5 shrink-0 text-accent" />
            <button
              type="button"
              onClick={() => onOpenBranch?.(b.name)}
              className="truncate font-mono text-[13px] text-ink hover:text-accent hover:underline"
              title={`Browse ${b.name}`}
            >
              {b.name}
            </button>
            {b.is_default && <Badge variant="success">Default</Badge>}
            {mode && (
              <Badge variant={mode.variant} className="gap-1" dot={false}>
                <mode.icon className="h-3 w-3" /> {mode.label}
              </Badge>
            )}
            {b.stale && !b.is_default && <Badge>Stale</Badge>}
          </div>
          {b.head_commit && (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
              <button
                type="button"
                onClick={() => onOpenCommit?.(b.head_commit.id)}
                className="font-mono text-accent hover:underline"
              >
                {b.head_commit.short_id}
              </button>
              <span className="max-w-[28rem] truncate text-ink-soft">{b.head_commit.message}</span>
              <span>
                {b.head_commit.author} · <span title={absoluteTime(b.head_commit.timestamp)}>{relativeTime(b.head_commit.timestamp)}</span>
              </span>
            </p>
          )}
        </div>

        {!b.is_default && (
          <Distance ahead={b.ahead} behind={b.behind} scale={scale} compared={b.compared_to} />
        )}

        {(b.pull_requests || []).filter((p) => p.role === 'source').slice(0, 2).map((p) => (
          <Link
            key={p.id}
            to={pullRequestLink(p.repository_id || repoId, p.id)}
            className="flex shrink-0 items-center gap-1 rounded-full border border-success-fg/30 bg-success-fg/10 px-2.5 py-1 text-[11px] text-success-fg hover:border-success-fg/60"
            title={p.title}
          >
            <FiGitPullRequest className="h-3 w-3" /> #{p.id}
          </Link>
        ))}

        {canManage && (
          <div className="flex shrink-0 items-center gap-1">
            {!b.is_default && (
              <Button variant="ghost" size="sm" className="!px-2" disabled={busy}
                onClick={() => setDefault(b)} title="Make this the default branch" aria-label={`Make ${b.name} the default branch`}>
                <FiStar className="h-3.5 w-3.5" />
              </Button>
            )}
            <Button variant="ghost" size="sm" className="!px-2" disabled={busy || Boolean(blockedMove)}
              onClick={() => openMove(b)} title={blockedMove || 'Move the branch to another commit'} aria-label={`Move ${b.name}`}>
              <FiCrosshair className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" className="!px-2" disabled={busy || Boolean(blockedRename)}
              onClick={() => setRenaming({ branch: b, newName: b.name })} title={blockedRename || 'Rename'} aria-label={`Rename ${b.name}`}>
              <FiEdit3 className="h-3.5 w-3.5" />
            </Button>
            <span title={blockedDelete || 'Delete'}>
              <Button variant="danger" size="sm" className="!px-2" disabled={busy || Boolean(blockedDelete)}
                onClick={() => setDeleting(b)} aria-label={`Delete ${b.name}`}>
                <FiTrash2 className="h-3.5 w-3.5" />
              </Button>
            </span>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-4" data-testid="branches-tab">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <FiSearch className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Find a branch…"
            aria-label="Find a branch"
            className="w-full rounded-xl border border-border bg-white/[0.04] py-2 pl-9 pr-3 text-sm text-ink outline-none focus:border-accent/70"
          />
        </div>
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setFilter(f.id)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[12px] transition-colors',
                filter === f.id
                  ? 'border-accent/50 bg-accent/10 text-ink'
                  : 'border-border text-ink-soft hover:border-border-strong hover:text-ink'
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
        <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2" aria-label="Refresh branches">
          <FiRefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
        </Button>
        {canWrite && (
          <Button size="sm" onClick={openCreate} disabled={busy}>
            <FiPlus className="h-4 w-4" /> New branch
          </Button>
        )}
      </div>

      {error && (
        <GlassCard hover={false} className="flex items-start gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg" role="alert">
          <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard hover={false} className="flex flex-wrap items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg" data-testid="branch-notice">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 flex-1 break-words">{notice.text}</span>
          {notice.restore && canWrite && (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => restore(notice.restore)}>
              <FiRotateCcw className="h-3.5 w-3.5" /> Restore
            </Button>
          )}
        </GlassCard>
      )}

      {loading && !branches.length ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => <div key={i} className="h-16 animate-pulse rounded-xl bg-white/[0.04]" />)}
        </div>
      ) : !branches.length ? (
        <EmptyState icon={FiGitBranch} title="No branches" description="Push to this repository to create its first branch." />
      ) : (
        <>
          {defaultRow && (
            <section>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">Default branch</h3>
              <GlassCard hover={false} className="p-0">{renderRow(defaultRow)}</GlassCard>
            </section>
          )}
          <section>
            <h3 className="mb-2 flex items-baseline gap-2 text-[11px] font-medium uppercase tracking-wider text-muted">
              {filter === 'stale' ? 'Stale branches' : filter === 'active' ? 'Active branches' : 'Branches'}
              <span className="font-mono normal-case tracking-normal">{others.length}</span>
              {data.default_branch && (
                <span className="ml-auto normal-case tracking-normal">behind | ahead of {data.default_branch}</span>
              )}
            </h3>
            {others.length ? (
              <GlassCard hover={false} className="p-0">{others.map(renderRow)}</GlassCard>
            ) : (
              <p className="rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
                {query ? `No branch matches “${query}”.` : 'No other branches.'}
              </p>
            )}
          </section>
        </>
      )}

      <Modal open={Boolean(creating)} onClose={() => setCreating(null)} title="New branch"
        subtitle="A branch is a name for a line of work. It starts at the commit you choose.">
        {creating && (
          <div className="space-y-4">
            <Input label="Branch name" value={creating.name} autoFocus placeholder="feature/search"
              onChange={(e) => setCreating({ ...creating, name: e.target.value })} />
            <div className="grid grid-cols-[8rem_1fr] gap-2">
              <label className="space-y-1.5">
                <span className="text-[13px] font-medium text-ink-soft">Start from</span>
                <select className={selectClass} value={creating.kind}
                  onChange={(e) => setCreating({
                    ...creating, kind: e.target.value,
                    source: e.target.value === 'branch' ? (data.default_branch || '')
                      : e.target.value === 'tag' ? (tags[0]?.name || '') : '',
                  })}>
                  <option value="branch">Branch</option>
                  <option value="tag" disabled={!tags.length}>Tag</option>
                  <option value="commit">Commit</option>
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-[13px] font-medium text-ink-soft">
                  {creating.kind === 'commit' ? 'Commit id' : creating.kind === 'tag' ? 'Tag' : 'Branch'}
                </span>
                {creating.kind === 'commit' ? (
                  <input className={selectClass} value={creating.source} placeholder="full id or 7+ characters"
                    onChange={(e) => setCreating({ ...creating, source: e.target.value })} />
                ) : (
                  <select className={selectClass} value={creating.source}
                    onChange={(e) => setCreating({ ...creating, source: e.target.value })}>
                    {(creating.kind === 'tag' ? tags.map((t) => t.name) : branches.map((b) => b.name)).map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                )}
              </label>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setCreating(null)}>Cancel</Button>
              <Button onClick={submitCreate} loading={busy}
                disabled={!creating.name.trim() || !String(creating.source).trim()}>
                Create branch
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={Boolean(renaming)} onClose={() => setRenaming(null)} title={`Rename ${renaming?.branch?.name || ''}`}
        subtitle="Open pull requests follow the new name. Anyone with the old name checked out will need to switch to the new one.">
        {renaming && (
          <div className="space-y-4">
            <Input label="New name" value={renaming.newName} autoFocus
              onChange={(e) => setRenaming({ ...renaming, newName: e.target.value })} />
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setRenaming(null)}>Cancel</Button>
              <Button onClick={submitRename} loading={busy}
                disabled={!renaming.newName.trim() || renaming.newName.trim() === renaming.branch.name}>
                Rename
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={Boolean(moving)} onClose={() => setMoving(null)} title={`Move ${moving?.branch?.name || ''}`}
        subtitle="Points the branch at another commit of this repository. Nothing is deleted, but commits after the new tip stop being part of this branch. Protection rules still apply.">
        {moving && (
          <div className="space-y-4">
            {moving.commits.length > 0 && (
              <label className="block space-y-1.5">
                <span className="text-[13px] font-medium text-ink-soft">Pick a commit on this branch</span>
                <select className={selectClass} value={moving.commitId}
                  onChange={(e) => setMoving({ ...moving, commitId: e.target.value })}>
                  {moving.commits.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.id.slice(0, 10)} · {(c.message || '').split('\n')[0].slice(0, 60)}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <Input label="Commit id" value={moving.commitId} placeholder="full commit id"
              onChange={(e) => setMoving({ ...moving, commitId: e.target.value })} />
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setMoving(null)}>Cancel</Button>
              <Button onClick={submitMove} loading={busy}
                disabled={!moving.commitId.trim() || moving.commitId.trim() === moving.branch.head_commit_id}>
                Move branch
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={Boolean(deleting)} onClose={() => setDeleting(null)} title={`Delete ${deleting?.name || ''}?`}
        subtitle="The commits stay in the repository. You can restore the branch from the notice that follows.">
        {deleting && (
          <div className="space-y-4">
            {deleting.ahead > 0 && (
              <p className="flex items-start gap-2 rounded-lg border border-warning-fg/30 bg-warning-bg p-3 text-sm text-warning-fg">
                <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                {deleting.ahead} commit(s) on this branch are not on {deleting.compared_to}.
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={submitDelete} loading={busy}>Delete branch</Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
