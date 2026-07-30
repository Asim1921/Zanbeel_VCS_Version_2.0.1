import React, { useEffect, useMemo, useState } from 'react'
import { FiGitCommit, FiClock, FiUser, FiHash, FiX, FiSearch, FiLoader, FiGitBranch, FiRotateCcw, FiAlertTriangle } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Badge from './ui/Badge'
import Button from './ui/Button'
import api from '../utils/api'

const CommitHistoryModal = ({ repo, branch, onClose, onRollbackComplete }) => {
  const [commits, setCommits] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [searchTerm, setSearchTerm] = useState('')
  const [rollbackTarget, setRollbackTarget] = useState(null)
  const [rollbackLoading, setRollbackLoading] = useState(false)
  const [rollbackError, setRollbackError] = useState(null)
  const [rollbackSuccess, setRollbackSuccess] = useState(null)

  useEffect(() => {
    let mounted = true

    const fetchCommits = async () => {
      try {
        setLoading(true)
        setError(null)
        const response = await api.getCommits(repo.id, false)
        if (mounted) {
          const normalized = api.transformCommitData(response.commits || [])
          setCommits(normalized)
        }
      } catch (err) {
        console.error('Failed to load commits', err)
        if (mounted) setError('Unable to load commit history for this repository.')
      } finally {
        if (mounted) setLoading(false)
      }
    }

    if (repo?.id) fetchCommits()

    return () => {
      mounted = false
    }
  }, [repo?.id])

  const filteredCommits = useMemo(() => {
    if (!searchTerm) return commits
    const term = searchTerm.toLowerCase()
    return commits.filter(commit =>
      commit.message.toLowerCase().includes(term) ||
      commit.author.toLowerCase().includes(term) ||
      commit.id.toLowerCase().includes(term)
    )
  }, [commits, searchTerm])

  const latestCommit = commits[0]
  const headLabel = repo?.head ? `${repo.head.slice(0, 7)}…` : 'N/A'

  const handleRollback = async () => {
    if (!rollbackTarget || !branch) return
    setRollbackLoading(true)
    setRollbackError(null)
    try {
      const result = await api.rollbackBranch(repo.id, {
        branch,
        target_commit_id: rollbackTarget,
        expected_head_commit_id: latestCommit?.id || repo?.head || null,
        summary: `Rollback branch '${branch}' to commit ${rollbackTarget.slice(0, 7)}`
      })
      if (result.success) {
        setRollbackSuccess(result.new_commit_id || rollbackTarget)
        setRollbackTarget(null)
        onRollbackComplete?.()
      } else {
        setRollbackError(result.detail || result.message || 'Rollback failed')
      }
    } catch (err) {
      if (err?.code === 'HEAD_MISMATCH') {
        setRollbackError('Branch head changed since this list loaded. Refresh and retry rollback.')
      } else {
        setRollbackError(err.message || 'Rollback failed')
      }
    } finally {
      setRollbackLoading(false)
    }
  }

  const prettyDate = (timestamp) => {
    if (!timestamp) return 'Unknown'
    try {
      return api.formatDate(timestamp)
    } catch (err) {
      return 'Unknown'
    }
  }

  const renderCommitFiles = (commit) => {
    if (!commit.files || commit.files.length === 0) return null
    const displayFiles = commit.files.slice(0, 4)
    const remaining = commit.files.length - displayFiles.length

    return (
      <div className="flex flex-wrap gap-2 mt-3">
        {displayFiles.map((file, idx) => (
          <Badge key={`${commit.id}-${idx}`} variant="default" className="text-xs bg-white/10 border border-white/10">
            {file.split('/').pop()}
          </Badge>
        ))}
        {remaining > 0 && (
          <Badge variant="info" className="text-xs">+{remaining} more</Badge>
        )}
      </div>
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm overflow-y-auto py-8 px-4">
      <div className="absolute inset-0" onClick={onClose} />
      <div className="relative w-full max-w-5xl">
        <GlassCard className="p-6" hover={false}>
          {/* Header */}
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-white/50 mb-1">Commit history</p>
              <h2 className="text-2xl font-semibold text-white flex items-center gap-2">
                <FiGitCommit className="w-5 h-5 text-green-300" />
                {repo?.name}
              </h2>
              <p className="text-sm text-white/60">Explore every change made to this repository.</p>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} className="text-white/70 hover:text-white">
              <FiX className="w-4 h-4 mr-2" />
              Close
            </Button>
          </div>

          {/* Stats */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-6">
            <GlassCard className="p-4" hover={false}>
              <p className="text-xs text-white/60 mb-1">Total commits</p>
              <div className="flex items-center gap-2">
                <span className="text-2xl font-semibold text-white">{commits.length}</span>
                <Badge variant="success" className="text-[11px]">Latest {latestCommit ? prettyDate(latestCommit.timestamp) : 'N/A'}</Badge>
              </div>
            </GlassCard>
            <GlassCard className="p-4" hover={false}>
              <p className="text-xs text-white/60 mb-1">Head commit</p>
              <div className="flex items-center gap-2 text-white">
                <FiHash className="w-4 h-4 text-purple-300" />
                <span className="font-mono text-sm">{headLabel}</span>
              </div>
              {latestCommit && (
                <p className="text-xs text-white/50 mt-2 line-clamp-1">{latestCommit.message}</p>
              )}
            </GlassCard>
            <GlassCard className="p-4" hover={false}>
              <p className="text-xs text-white/60 mb-1">Repository</p>
              <div className="flex items-center gap-2 text-white">
                <FiGitBranch className="w-4 h-4 text-blue-300" />
                <span className="text-sm">{repo?.owner}</span>
              </div>
              <p className="text-xs text-white/50 mt-2">Created {repo?.lastUpdate || 'N/A'}</p>
            </GlassCard>
          </div>

          {/* Filters */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3 mt-6">
            <div className="relative flex-1">
              <FiSearch className="w-4 h-4 text-white/40 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder="Search commits by message, author, or ID"
                className="w-full bg-white/10 border border-white/10 rounded-xl text-sm text-white placeholder:text-white/40 pl-10 pr-4 py-2 focus:outline-none focus:ring-2 focus:ring-purple-400/60"
              />
            </div>
            <Badge variant="info" className="text-xs whitespace-nowrap bg-blue-500/15 text-blue-100 border border-blue-400/30">
              {filteredCommits.length} shown
            </Badge>
          </div>

          {/* Timeline */}
          <div className="mt-6 max-h-[60vh] overflow-y-auto pr-1">
            {loading && (
              <div className="flex items-center justify-center py-10 text-white/70">
                <FiLoader className="w-5 h-5 mr-2 animate-spin" />
                Loading commit history...
              </div>
            )}

            {!loading && error && (
              <div className="flex items-center justify-between bg-red-500/10 border border-red-500/30 rounded-xl px-4 py-3 text-sm text-red-200">
                <span>{error}</span>
                <Button variant="ghost" size="sm" onClick={() => setSearchTerm('')}>Dismiss</Button>
              </div>
            )}

            {!loading && !error && filteredCommits.length === 0 && (
              <div className="text-center text-white/60 py-12">
                <FiGitCommit className="w-6 h-6 mx-auto mb-3 opacity-70" />
                <p>No commits match your search.</p>
              </div>
            )}

            {!loading && !error && filteredCommits.length > 0 && (
              <div className="space-y-6">
                {filteredCommits.map((commit, index) => (
                  <div key={commit.id} className="flex items-start gap-4">
                    {/* Timeline rail */}
                    <div className="flex flex-col items-center">
                      <div className="w-3.5 h-3.5 rounded-full border-2 border-purple-200 bg-purple-500 shadow-lg shadow-purple-500/30" />
                      {index !== filteredCommits.length - 1 && (
                        <div className="flex-1 w-px bg-gradient-to-b from-purple-400/40 to-transparent" />
                      )}
                    </div>

                    {/* Card */}
                    <div className="flex-1 bg-white/5 border border-white/10 rounded-2xl p-4 hover:border-white/20 transition-colors">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="flex items-center gap-2 mb-1">
                            <Badge variant="info" className="text-[11px] font-mono bg-blue-500/15 text-blue-100 border-blue-400/30">
                              {commit.id.slice(0, 7)}
                            </Badge>
                            {repo?.head === commit.id && (
                              <Badge variant="success" className="text-[11px]">HEAD</Badge>
                            )}
                          </div>
                          <p className="text-white font-semibold leading-snug">{commit.message}</p>
                          <div className="flex flex-wrap items-center gap-3 text-xs text-white/60 mt-2">
                            <span className="inline-flex items-center gap-1">
                              <FiUser className="w-3.5 h-3.5" />
                              {commit.author}
                            </span>
                            <span className="inline-flex items-center gap-1">
                              <FiClock className="w-3.5 h-3.5" />
                              {prettyDate(commit.timestamp)}
                            </span>
                            {commit.parent && (
                              <span className="inline-flex items-center gap-1">
                                <FiHash className="w-3.5 h-3.5" />
                                Parent {commit.parent.slice(0, 7)}
                              </span>
                            )}
                            <span className="inline-flex items-center gap-1">
                              <FiGitCommit className="w-3.5 h-3.5" />
                              {commit.files?.length || 0} files
                            </span>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <Badge variant="default" className="text-xs bg-white/10 text-white px-3 py-1 border border-white/10">
                            {commit.timestamp ? new Date(commit.timestamp).toLocaleString() : 'Unknown time'}
                          </Badge>
                          {branch && repo?.head !== commit.id && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-orange-300 hover:text-orange-100 border border-orange-400/30 hover:bg-orange-500/20"
                              onClick={() => { setRollbackTarget(commit.id); setRollbackError(null) }}
                              title={`Rollback branch '${branch}' to this commit`}
                            >
                              <FiRotateCcw className="w-3.5 h-3.5 mr-1" />
                              Rollback here
                            </Button>
                          )}
                        </div>
                      </div>

                      {renderCommitFiles(commit)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Rollback success banner */}
          {rollbackSuccess && (
            <div className="mt-4 flex items-center gap-3 bg-green-500/15 border border-green-400/30 rounded-xl px-4 py-3 text-sm text-green-200">
              <FiRotateCcw className="w-4 h-4 flex-shrink-0" />
              <span>Rollback complete — new commit <span className="font-mono">{rollbackSuccess.slice(0, 7)}</span> created on branch <strong>{branch}</strong>.</span>
              <button className="ml-auto text-green-300 hover:text-white" onClick={() => setRollbackSuccess(null)}>✕</button>
            </div>
          )}
        </GlassCard>
      </div>

      {/* Rollback confirmation dialog */}
      {rollbackTarget && (
        <div className="fixed inset-0 z-60 flex items-center justify-center bg-black/70 backdrop-blur-sm">
          <div className="relative bg-slate-900 border border-orange-400/40 rounded-2xl p-6 max-w-md w-full mx-4 shadow-2xl">
            <div className="flex items-start gap-3 mb-4">
              <FiAlertTriangle className="w-6 h-6 text-orange-400 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="text-white font-semibold text-lg">Rollback branch?</h3>
                <p className="text-white/70 text-sm mt-1">
                  This will create a <strong>new commit</strong> on branch <strong className="text-orange-300">{branch}</strong> that restores all files to the state at commit <span className="font-mono text-orange-200">{rollbackTarget.slice(0, 12)}</span>.
                </p>
                <p className="text-white/50 text-xs mt-2">History is preserved — no commits will be deleted.</p>
              </div>
            </div>

            {rollbackError && (
              <div className="mb-4 bg-red-500/15 border border-red-400/30 rounded-lg px-3 py-2 text-sm text-red-300">
                {rollbackError}
              </div>
            )}

            <div className="flex justify-end gap-3">
              <Button
                variant="secondary"
                disabled={rollbackLoading}
                onClick={() => { setRollbackTarget(null); setRollbackError(null) }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                disabled={rollbackLoading}
                onClick={handleRollback}
                className="bg-orange-500 hover:bg-orange-600 text-white"
              >
                {rollbackLoading ? (
                  <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Rolling back…</>
                ) : (
                  <><FiRotateCcw className="w-4 h-4 mr-2" />Confirm Rollback</>
                )}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default CommitHistoryModal
