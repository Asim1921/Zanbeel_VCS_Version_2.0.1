import React, { useEffect, useState } from 'react'
import { FiGitBranch, FiLoader, FiRefreshCw, FiCopy, FiUploadCloud, FiGitMerge, FiSettings } from 'react-icons/fi'
import Button from './ui/Button'
import api from '../utils/api'

const SCOPE_OPTIONS = ['read', 'write', 'manage', 'team_lead']

const BranchActionsPanel = ({
  repo,
  branches,
  selectedBranch,
  selectedFilePath,
  canManage,
  onBranchesChanged,
  onOpenPullRequests,
}) => {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  const [newBranchName, setNewBranchName] = useState('')
  const [fromBranch, setFromBranch] = useState(selectedBranch || '')
  const [mergeSource, setMergeSource] = useState('')
  const [mergeTarget, setMergeTarget] = useState(selectedBranch || '')
  const [publishSource, setPublishSource] = useState('')
  const [publishTarget, setPublishTarget] = useState(selectedBranch || '')
  const [copySource, setCopySource] = useState('')
  const [copyTarget, setCopyTarget] = useState(selectedBranch || '')
  const [copyPath, setCopyPath] = useState(selectedFilePath || '')

  const [policy, setPolicy] = useState(null)
  const [policyDraft, setPolicyDraft] = useState(null)
  const [yourScope, setYourScope] = useState('read')

  useEffect(() => {
    setFromBranch(selectedBranch || '')
    setMergeTarget(selectedBranch || '')
    setPublishTarget(selectedBranch || '')
    setCopyTarget(selectedBranch || '')
    setCopyPath(selectedFilePath || '')
  }, [selectedBranch, selectedFilePath])

  useEffect(() => {
    if (!expanded || !repo?.id) return
    let cancelled = false
    api.getBranchPolicy(repo.id).then((res) => {
      if (cancelled || !res?.success) return
      setPolicy(res.policy)
      setPolicyDraft(res.policy)
      setYourScope(res.your_scope || 'read')
    }).catch(() => {})
    return () => { cancelled = true }
  }, [expanded, repo?.id])

  const branchNames = (branches || []).map((b) => b.name || b)
  const headFor = (name) => {
    const b = (branches || []).find((x) => (x.name || x) === name)
    return b?.head_commit_id || b?.head || null
  }

  const run = async (fn) => {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      await fn()
    } catch (e) {
      setError(e.message || 'Operation failed')
    } finally {
      setBusy(false)
    }
  }

  const handleCreateBranch = () => run(async () => {
    if (!newBranchName.trim()) {
      setError('Enter a branch name')
      return
    }
    const res = await api.createBranch(repo.id, {
      name: newBranchName.trim(),
      fromBranch: fromBranch || undefined,
    })
    if (!res?.success) {
      setError(res?.detail || 'Create branch failed')
      return
    }
    setMessage(`Created branch ${newBranchName.trim()}`)
    setNewBranchName('')
    onBranchesChanged?.()
  })

  const handleMerge = () => run(async () => {
    if (!mergeSource || !mergeTarget) {
      setError('Select source and target branches')
      return
    }
    try {
      const res = await api.mergeBranches(repo.id, {
        sourceBranch: mergeSource,
        targetBranch: mergeTarget,
        expectedHeadCommitId: headFor(mergeTarget),
      })
      if (res?.success) {
        setMessage(`Merged ${mergeSource} into ${mergeTarget}`)
        onBranchesChanged?.()
      }
    } catch (e) {
      if (e.status === 409 && e.data?.pull_request_id) {
        setError(
          `Merge conflicts (${(e.data.conflicts || []).length} files). Open Pull Requests to resolve (PR #${e.data.pull_request_id}).`
        )
        onOpenPullRequests?.()
      } else {
        throw e
      }
    }
  })

  const handlePublish = () => run(async () => {
    if (!publishSource || !publishTarget) {
      setError('Select source and target branches')
      return
    }
    const res = await api.publishBranch(repo.id, {
      sourceBranch: publishSource,
      targetBranch: publishTarget,
      expectedHeadCommitId: headFor(publishTarget),
    })
    if (res?.success) {
      setMessage(
        res.status === 'already_up_to_date'
          ? `${publishTarget} is already up to date with ${publishSource}`
          : `Published ${publishSource} → ${publishTarget} (fast-forward)`
      )
      onBranchesChanged?.()
    }
  })

  const handleCopyFiles = () => run(async () => {
    const paths = copyPath.split(/[\n,]+/).map((p) => p.trim()).filter(Boolean)
    if (!copySource || !copyTarget || paths.length === 0) {
      setError('Source branch, target branch, and at least one path are required')
      return
    }
    const res = await api.copyFilesFromBranch(repo.id, {
      sourceBranch: copySource,
      targetBranch: copyTarget,
      paths,
      expectedHeadCommitId: headFor(copyTarget),
    })
    if (res?.success) {
      setMessage(`Updated ${paths.length} path(s) on ${copyTarget} from ${copySource}`)
      onBranchesChanged?.()
    }
  })

  const handleSavePolicy = () => run(async () => {
    if (!policyDraft) return
    const res = await api.updateBranchPolicy(repo.id, policyDraft)
    if (res?.success) {
      setPolicy(res.policy)
      setPolicyDraft(res.policy)
      setMessage('Branch policy saved')
    }
  })

  if (!repo?.id) return null

  return (
    <div className="mb-4 rounded-lg border border-white/10 bg-white/5 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-white/5 transition-colors"
      >
        <span className="text-sm font-medium text-white flex items-center gap-2">
          <FiGitBranch className="w-4 h-4" />
          Branch actions
          <span className="text-white/40 font-normal">(scope: {yourScope})</span>
        </span>
        <span className="text-white/50 text-xs">{expanded ? 'Hide' : 'Show'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-5 border-t border-white/10 pt-4">
          {error && (
            <p className="text-sm text-red-300 bg-red-500/10 border border-red-400/20 rounded p-2">{error}</p>
          )}
          {message && (
            <p className="text-sm text-green-300 bg-green-500/10 border border-green-400/20 rounded p-2">{message}</p>
          )}

          <section>
            <h4 className="text-xs uppercase tracking-wider text-white/50 mb-2">Create branch from branch</h4>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <input
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                placeholder="New branch name"
                value={newBranchName}
                onChange={(e) => setNewBranchName(e.target.value)}
              />
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={fromBranch}
                onChange={(e) => setFromBranch(e.target.value)}
              >
                <option value="">From current tip</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <Button size="sm" onClick={handleCreateBranch} disabled={busy}>
                {busy ? <FiLoader className="animate-spin" /> : 'Create'}
              </Button>
            </div>
          </section>

          <section>
            <h4 className="text-xs uppercase tracking-wider text-white/50 mb-2 flex items-center gap-1">
              <FiGitMerge className="w-3.5 h-3.5" /> Merge into branch
            </h4>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={mergeSource}
                onChange={(e) => setMergeSource(e.target.value)}
              >
                <option value="">Source</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={mergeTarget}
                onChange={(e) => setMergeTarget(e.target.value)}
              >
                <option value="">Target</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <Button size="sm" onClick={handleMerge} disabled={busy}>Merge</Button>
            </div>
          </section>

          <section>
            <h4 className="text-xs uppercase tracking-wider text-white/50 mb-2 flex items-center gap-1">
              <FiUploadCloud className="w-3.5 h-3.5" /> Push branch → branch (fast-forward)
            </h4>
            <p className="text-xs text-white/40 mb-2">Like git push origin SRC:DST when target can fast-forward.</p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={publishSource}
                onChange={(e) => setPublishSource(e.target.value)}
              >
                <option value="">Source (local history)</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={publishTarget}
                onChange={(e) => setPublishTarget(e.target.value)}
              >
                <option value="">Target on server</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <Button size="sm" onClick={handlePublish} disabled={busy}>Publish</Button>
            </div>
          </section>

          <section>
            <h4 className="text-xs uppercase tracking-wider text-white/50 mb-2 flex items-center gap-1">
              <FiCopy className="w-3.5 h-3.5" /> Checkout files from branch (server)
            </h4>
            <p className="text-xs text-white/40 mb-2">Copies paths from source onto target branch (new commit). CLI: fox checkout-files</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-2">
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={copySource}
                onChange={(e) => setCopySource(e.target.value)}
              >
                <option value="">From branch</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
              <select
                className="px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                value={copyTarget}
                onChange={(e) => setCopyTarget(e.target.value)}
              >
                <option value="">Onto branch</option>
                {branchNames.map((n) => (
                  <option key={n} value={n} className="bg-slate-100 text-slate-900">{n}</option>
                ))}
              </select>
            </div>
            <textarea
              className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm mb-2 min-h-[60px]"
              placeholder="Paths (one per line or comma-separated)"
              value={copyPath}
              onChange={(e) => setCopyPath(e.target.value)}
            />
            <Button size="sm" onClick={handleCopyFiles} disabled={busy}>Apply paths</Button>
          </section>

          {canManage && policyDraft && (
            <section>
              <h4 className="text-xs uppercase tracking-wider text-white/50 mb-2 flex items-center gap-1">
                <FiSettings className="w-3.5 h-3.5" /> Branching rules
              </h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                {[
                  ['create_branch_min_scope', 'Create branch'],
                  ['push_min_scope', 'Push'],
                  ['merge_min_scope', 'Merge'],
                  ['publish_min_scope', 'Publish / push refspec'],
                  ['copy_files_min_scope', 'Copy files'],
                  ['default_branch_push_min_scope', 'Protected / default push'],
                ].map(([key, label]) => (
                  <label key={key} className="flex flex-col gap-1 text-white/70">
                    {label}
                    <select
                      className="px-2 py-1.5 bg-white/10 border border-white/20 rounded text-white"
                      value={policyDraft[key] || 'write'}
                      onChange={(e) => setPolicyDraft({ ...policyDraft, [key]: e.target.value })}
                    >
                      {SCOPE_OPTIONS.map((s) => (
                        <option key={s} value={s} className="bg-slate-100 text-slate-900">{s}</option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <input
                className="mt-2 w-full px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm"
                placeholder="Extra protected branches (comma-separated)"
                value={(policyDraft.protected_branches || []).join(', ')}
                onChange={(e) => setPolicyDraft({
                  ...policyDraft,
                  protected_branches: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                })}
              />
              <Button size="sm" className="mt-2" onClick={handleSavePolicy} disabled={busy}>
                Save branching rules
              </Button>
            </section>
          )}

          <p className="text-xs text-white/40 flex items-center gap-1">
            <FiRefreshCw className="w-3 h-3" />
            Local CLI: fox push SRC:DST · fox checkout-files --from BRANCH path
          </p>
        </div>
      )}
    </div>
  )
}

export default BranchActionsPanel
