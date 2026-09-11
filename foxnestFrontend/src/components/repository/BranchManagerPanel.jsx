import React, { useCallback, useEffect, useState } from 'react'
import {
  FiGitBranch,
  FiEdit3,
  FiTrash2,
  FiStar,
  FiCrosshair,
  FiRefreshCw,
  FiAlertTriangle,
  FiCheckCircle,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Rename, delete, set-default and move-head for branches.
 *
 * These four have existed server-side all along and the web client only ever
 * listed branches. Each is destructive in a different way, so each confirms in
 * terms of what it will actually do rather than a generic "are you sure".
 */

export default function BranchManagerPanel({ repoId, canManage = false }) {
  const [branches, setBranches] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(null)

  const [renaming, setRenaming] = useState(null)
  const [newName, setNewName] = useState('')
  const [moving, setMoving] = useState(null)
  const [commitId, setCommitId] = useState('')
  const [commits, setCommits] = useState([])

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.getBranches(repoId)
      setBranches(response.branches || [])
    } catch (err) {
      setError(err.message || 'Failed to load branches')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => {
    load()
  }, [load])

  const act = async (label, fn) => {
    try {
      setBusy(label)
      setError(null)
      setNotice(null)
      await fn()
      setNotice(label)
      await load()
    } catch (err) {
      setError(err.message || 'That did not work')
    } finally {
      setBusy(null)
    }
  }

  const handleRename = async () => {
    if (!newName.trim()) return
    const from = renaming.name
    setRenaming(null)
    await act(`Renamed ${from} to ${newName.trim()}`, () =>
      api.renameBranch(repoId, from, newName.trim())
    )
    setNewName('')
  }

  const handleDelete = (branch) => {
    const ok = window.confirm(
      `Delete branch "${branch.name}"?\n\n` +
        'The commits stay in the repository, but nothing will point at them from ' +
        'this branch any more.'
    )
    if (!ok) return
    act(`Deleted ${branch.name}`, () => api.deleteBranch(repoId, branch.name))
  }

  const handleSetDefault = (branch) => {
    const ok = window.confirm(
      `Make "${branch.name}" the default branch?\n\n` +
        'Clones and pushes with no branch named will use it, and branch protection ' +
        'treats the default branch as protected.'
    )
    if (!ok) return
    act(`${branch.name} is now the default`, () => api.setDefaultBranch(repoId, branch.name))
  }

  const openMove = async (branch) => {
    setMoving(branch)
    setCommitId(branch.head_commit_id || '')
    try {
      const response = await api.getCommits(repoId, false, branch.name)
      setCommits(response.commits || [])
    } catch {
      setCommits([])
    }
  }

  const handleMove = async () => {
    if (!commitId.trim()) return
    const branch = moving
    setMoving(null)
    await act(`Moved ${branch.name} to ${commitId.trim().slice(0, 12)}`, () =>
      api.moveBranchHead(repoId, branch.name, commitId.trim())
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg font-light tracking-tight text-ink">Branches</h3>
          <p className="mt-1 text-sm text-muted">
            Rename, delete, change the default, or move a branch to a different commit.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
          <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard className="flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg">
          <FiCheckCircle className="h-4 w-4 shrink-0" />
          <span>{notice}</span>
        </GlassCard>
      )}

      {loading ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-14 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : branches.length === 0 ? (
        <EmptyState icon={FiGitBranch} title="No branches" description="This repository has no branches yet." />
      ) : (
        <div className="space-y-2">
          {branches.map((b) => {
            const name = b.name || b
            const isDefault = Boolean(b.is_default)
            return (
              <GlassCard key={name} hover={false} className="flex flex-wrap items-center gap-3 p-3.5">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-accent">
                  <FiGitBranch className="h-3.5 w-3.5" />
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-mono text-[13px] text-ink">{name}</span>
                    {isDefault && <Badge variant="success">Default</Badge>}
                  </div>
                  {b.head_commit_id && (
                    <p className="mt-0.5 font-mono text-[11px] text-muted">
                      head {String(b.head_commit_id).slice(0, 12)}
                    </p>
                  )}
                </div>

                {canManage && (
                  <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                    {!isDefault && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleSetDefault(b)}
                        disabled={Boolean(busy)}
                        title="Make this the default branch"
                        className="!px-2"
                      >
                        <FiStar className="h-3.5 w-3.5" />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openMove(b)}
                      disabled={Boolean(busy)}
                      title="Move the branch to another commit"
                      className="!px-2"
                    >
                      <FiCrosshair className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setRenaming(b)
                        setNewName(name)
                      }}
                      disabled={Boolean(busy)}
                      title="Rename"
                      className="!px-2"
                    >
                      <FiEdit3 className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => handleDelete(b)}
                      disabled={Boolean(busy) || isDefault}
                      title={isDefault ? 'The default branch cannot be deleted' : 'Delete'}
                      className="!px-2"
                    >
                      <FiTrash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                )}
              </GlassCard>
            )
          })}
        </div>
      )}

      <Modal
        open={Boolean(renaming)}
        onClose={() => setRenaming(null)}
        title={`Rename ${renaming?.name || ''}`}
        subtitle="Anyone tracking the old name will need to update their remote."
      >
        <div className="space-y-4">
          <Input
            label="New name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="feature/new-name"
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setRenaming(null)}>
              Cancel
            </Button>
            <Button onClick={handleRename} disabled={!newName.trim()}>
              Rename
            </Button>
          </div>
        </div>
      </Modal>

      <Modal
        open={Boolean(moving)}
        onClose={() => setMoving(null)}
        title={`Move ${moving?.name || ''}`}
        subtitle="Points the branch at a different commit. Nothing is deleted, but work after the new head stops being reachable from this branch."
      >
        <div className="space-y-4">
          {commits.length > 0 && (
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-soft">
                Pick a commit
              </label>
              <select
                value={commitId}
                onChange={(e) => setCommitId(e.target.value)}
                className="w-full rounded-lg border border-border bg-cream-mid px-3 py-2 text-[13px] text-ink focus:border-accent focus:outline-none"
              >
                <option value="">— choose —</option>
                {commits.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id.slice(0, 10)} · {(c.message || '').slice(0, 52)}
                  </option>
                ))}
              </select>
            </div>
          )}
          <Input
            label="Commit id"
            value={commitId}
            onChange={(e) => setCommitId(e.target.value)}
            placeholder="full commit id"
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setMoving(null)}>
              Cancel
            </Button>
            <Button onClick={handleMove} disabled={!commitId.trim()}>
              Move branch
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
