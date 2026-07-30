import React, { useEffect, useMemo, useState } from 'react'
import { FiGitBranch, FiGitCommit, FiLoader, FiPlus, FiRefreshCw, FiX } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import Badge from './ui/Badge'
import api from '../utils/api'

const PullRequestsModal = ({ repo, onClose }) => {
  const [pullRequests, setPullRequests] = useState([])
  const [branches, setBranches] = useState([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [mergingId, setMergingId] = useState(null)
  const [error, setError] = useState(null)
  const [mergeConflict, setMergeConflict] = useState(null)

  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [sourceBranch, setSourceBranch] = useState('')
  const [targetBranch, setTargetBranch] = useState('')

  const branchHeadByName = useMemo(() => {
    const map = {}
    branches.forEach((b) => {
      map[b.name] = b.head_commit_id || null
    })
    return map
  }, [branches])

  const loadData = async () => {
    if (!repo?.id) return
    try {
      setLoading(true)
      setError(null)
      setMergeConflict(null)

      const [prsResponse, branchesResponse] = await Promise.all([
        api.listPullRequests(repo.id),
        api.getBranches(repo.id),
      ])

      const prs = prsResponse.pull_requests || []
      const bs = branchesResponse.branches || []

      setPullRequests(prs)
      setBranches(bs)

      const mainBranch = bs.find((b) => b.name === 'main')
      setTargetBranch(mainBranch?.name || bs[0]?.name || '')
      setSourceBranch(bs.find((b) => b.name !== (mainBranch?.name || bs[0]?.name))?.name || bs[0]?.name || '')
    } catch (err) {
      setError(err.message || 'Unable to load pull requests')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [repo?.id])

  const handleCreatePr = async () => {
    if (!title.trim() || !sourceBranch || !targetBranch) return
    try {
      setSubmitting(true)
      setError(null)
      await api.createPullRequest(repo.id, {
        title: title.trim(),
        description: description.trim() || null,
        source_branch: sourceBranch,
        target_branch: targetBranch,
      })
      setTitle('')
      setDescription('')
      await loadData()
    } catch (err) {
      setError(err.message || 'Failed to create pull request')
    } finally {
      setSubmitting(false)
    }
  }

  const handleMergePr = async (pr) => {
    try {
      setMergingId(pr.id)
      setError(null)
      setMergeConflict(null)
      const expectedHead = branchHeadByName[pr.target_branch] || null
      await api.mergePullRequest(repo.id, pr.id, expectedHead)
      await loadData()
    } catch (err) {
      const data = err?.data || {}
      if (data?.code === 'MERGE_CONFLICT') {
        setMergeConflict({
          prId: pr.id,
          source: data.source_branch || pr.source_branch,
          target: data.target_branch || pr.target_branch,
          conflicts: data.conflicts || [],
          message: data.message || err.message,
        })
      } else if (err?.code === 'HEAD_MISMATCH') {
        setError('Target branch head changed. Pull request list has been refreshed, please retry merge.')
        await loadData()
      } else {
        setError(err.message || 'Merge failed')
      }
    } finally {
      setMergingId(null)
    }
  }

  const handleClosePr = async (prId) => {
    try {
      setError(null)
      await api.closePullRequest(repo.id, prId)
      await loadData()
    } catch (err) {
      setError(err.message || 'Failed to close pull request')
    }
  }

  const statusBadge = (status) => {
    if (status === 'open') return <Badge variant="success" className="text-[11px]">OPEN</Badge>
    if (status === 'merged') return <Badge variant="info" className="text-[11px]">MERGED</Badge>
    return <Badge variant="default" className="text-[11px]">{(status || 'unknown').toUpperCase()}</Badge>
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="absolute inset-0" onClick={onClose} />
      <div className="relative w-full max-w-5xl">
        <GlassCard className="p-6" hover={false}>
          <div className="flex items-start justify-between gap-4 mb-5">
            <div>
              <p className="text-xs uppercase tracking-[0.16em] text-white/50">Pull Requests</p>
              <h2 className="text-xl font-semibold text-white">{repo?.name}</h2>
              <p className="text-sm text-white/60">Merge branches with conflict-aware feedback.</p>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={loadData}>
                <FiRefreshCw className="w-4 h-4 mr-2" />
                Refresh
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose}>
                <FiX className="w-4 h-4 mr-2" />
                Close
              </Button>
            </div>
          </div>

          {error && (
            <div className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
              {error}
            </div>
          )}

          {mergeConflict && (
            <div className="mb-4 rounded-xl border border-orange-400/40 bg-orange-500/10 p-4">
              <p className="text-orange-200 font-semibold mb-1">Merge conflict detected (PR #{mergeConflict.prId})</p>
              <p className="text-orange-100/90 text-sm mb-2">
                {mergeConflict.source} → {mergeConflict.target}: {mergeConflict.message}
              </p>
              <div className="max-h-28 overflow-auto rounded-lg border border-orange-400/30 bg-black/20 p-2">
                {(mergeConflict.conflicts || []).map((path) => (
                  <p key={path} className="text-xs text-orange-100 font-mono py-0.5">{path}</p>
                ))}
              </div>
              <p className="text-xs text-orange-100/80 mt-2">Resolve these file conflicts on branches, push fixes, then retry merge.</p>
            </div>
          )}

          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
            <div className="xl:col-span-1 rounded-xl border border-white/10 bg-white/5 p-4">
              <h3 className="text-white font-medium mb-3 flex items-center gap-2"><FiPlus className="w-4 h-4" />New Pull Request</h3>
              <div className="space-y-3">
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="PR title"
                  className="w-full bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                />
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Description (optional)"
                  className="w-full min-h-20 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                />
                <div>
                  <label className="text-xs text-white/60">Source branch</label>
                  <select value={sourceBranch} onChange={(e) => setSourceBranch(e.target.value)} className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white">
                    {branches.map((b) => <option className="bg-slate-100 text-slate-900" key={`src-${b.name}`} value={b.name}>{b.name}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-white/60">Target branch</label>
                  <select value={targetBranch} onChange={(e) => setTargetBranch(e.target.value)} className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white">
                    {branches.map((b) => <option className="bg-slate-100 text-slate-900" key={`tgt-${b.name}`} value={b.name}>{b.name}</option>)}
                  </select>
                </div>
                <Button variant="primary" size="sm" className="w-full" onClick={handleCreatePr} disabled={submitting || !title.trim() || !sourceBranch || !targetBranch || sourceBranch === targetBranch}>
                  {submitting ? <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Creating...</> : <><FiPlus className="w-4 h-4 mr-2" />Create PR</>}
                </Button>
              </div>
            </div>

            <div className="xl:col-span-2 rounded-xl border border-white/10 bg-white/5 p-4 max-h-[64vh] overflow-auto">
              <h3 className="text-white font-medium mb-3">Open and historical pull requests</h3>

              {loading ? (
                <div className="py-10 text-white/70 flex items-center justify-center"><FiLoader className="w-5 h-5 animate-spin mr-2" />Loading pull requests...</div>
              ) : pullRequests.length === 0 ? (
                <p className="text-white/50 text-sm">No pull requests yet.</p>
              ) : (
                <div className="space-y-3">
                  {pullRequests.map((pr) => (
                    <div key={pr.id} className="rounded-lg border border-white/10 bg-white/5 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-white font-medium">#{pr.id} {pr.title}</p>
                          <p className="text-white/60 text-xs mt-1 flex items-center gap-1">
                            <FiGitBranch className="w-3.5 h-3.5" />
                            {pr.source_branch} → {pr.target_branch}
                          </p>
                          {pr.description && <p className="text-white/70 text-sm mt-2">{pr.description}</p>}
                          <p className="text-white/45 text-xs mt-2 inline-flex items-center gap-1">
                            <FiGitCommit className="w-3.5 h-3.5" />
                            Created by {pr.created_by || 'unknown'}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          {statusBadge(pr.status)}
                          {pr.status === 'open' && (
                            <>
                              <Button variant="ghost" size="sm" className="text-blue-200 border border-blue-400/30" disabled={mergingId === pr.id} onClick={() => handleMergePr(pr)}>
                                {mergingId === pr.id ? <><FiLoader className="w-4 h-4 mr-1 animate-spin" />Merging</> : 'Merge'}
                              </Button>
                              <Button variant="ghost" size="sm" className="text-white/80 border border-white/20" onClick={() => handleClosePr(pr.id)}>
                                Close
                              </Button>
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </GlassCard>
      </div>
    </div>
  )
}

export default PullRequestsModal
