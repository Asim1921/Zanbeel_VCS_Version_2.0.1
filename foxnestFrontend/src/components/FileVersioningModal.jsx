import React, { useEffect, useMemo, useState } from 'react'
import { FiClock, FiGitCommit, FiLoader, FiRotateCcw, FiX } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import Badge from './ui/Badge'
import api from '../utils/api'

const FileVersioningModal = ({ repo, branch, filePath, onClose, onRollbackComplete }) => {
  const [versions, setVersions] = useState([])
  const [selectedVersion, setSelectedVersion] = useState(null)
  const [compareResult, setCompareResult] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingCompare, setLoadingCompare] = useState(false)
  const [rollingBack, setRollingBack] = useState(false)
  const [error, setError] = useState(null)
  const [showConfirm, setShowConfirm] = useState(false)
  const [historyScope, setHistoryScope] = useState('all')
  const [historyCursor, setHistoryCursor] = useState(null)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [loadingMoreHistory, setLoadingMoreHistory] = useState(false)

  const latestVersion = versions[0] || null
  const canRollback = selectedVersion && latestVersion && selectedVersion.commit_id !== latestVersion.commit_id
  const displayedVersions = useMemo(() => versions, [versions])

  useEffect(() => {
    let mounted = true

    const loadHistory = async () => {
      try {
        setLoading(true)
        setError(null)
        const scopeBranch = historyScope === 'branch' ? branch : null
        const response = await api.getFileHistory(repo.id, filePath, scopeBranch, 100, true, null)
        if (!mounted) return

        const items = response.versions || []
        setVersions(items)
        setHistoryCursor(response.next_cursor || null)
        setHasMoreHistory(!!response.has_more)
        if (items.length > 1) {
          setSelectedVersion(items[1])
        } else if (items.length > 0) {
          setSelectedVersion(items[0])
        }
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to load file history')
      } finally {
        if (mounted) setLoading(false)
      }
    }

    if (repo?.id && filePath) loadHistory()

    return () => {
      mounted = false
    }
  }, [repo?.id, filePath, branch, historyScope])

  const loadOlderVersions = async () => {
    if (!historyCursor || !hasMoreHistory || loadingMoreHistory) return
    try {
      setLoadingMoreHistory(true)
      const scopeBranch = historyScope === 'branch' ? branch : null
      const response = await api.getFileHistory(repo.id, filePath, scopeBranch, 100, true, historyCursor)
      const nextItems = response.versions || []
      setVersions(prev => [...prev, ...nextItems])
      setHistoryCursor(response.next_cursor || null)
      setHasMoreHistory(!!response.has_more)
    } catch (err) {
      setError(err.message || 'Failed to load older versions')
    } finally {
      setLoadingMoreHistory(false)
    }
  }

  useEffect(() => {
    let mounted = true

    const loadCompare = async () => {
      if (!latestVersion || !selectedVersion) {
        setCompareResult(null)
        return
      }

      try {
        setLoadingCompare(true)
        const response = await api.compareCommits(
          repo.id,
          selectedVersion.commit_id,
          latestVersion.commit_id,
          filePath
        )
        if (!mounted) return

        const fileEntry = (response.files || [])[0] || null
        setCompareResult(fileEntry)
      } catch (err) {
        if (mounted) setError(err.message || 'Failed to compare versions')
      } finally {
        if (mounted) setLoadingCompare(false)
      }
    }

    loadCompare()

    return () => {
      mounted = false
    }
  }, [repo?.id, filePath, latestVersion?.commit_id, selectedVersion?.commit_id])

  const compareRows = useMemo(() => {
    return compareResult?.diff?.rows || []
  }, [compareResult])

  const handleRollback = async () => {
    if (!selectedVersion || !canRollback) return

    try {
      setRollingBack(true)
      setError(null)
      const response = await api.rollbackFile(repo.id, {
        path: filePath,
        target_commit_id: selectedVersion.commit_id,
        branch,
        expected_head_commit_id: latestVersion?.commit_id || null,
        summary: `rollback(file): ${filePath} to ${selectedVersion.commit_id.slice(0, 8)}`
      })

      setShowConfirm(false)
      if (onRollbackComplete) {
        onRollbackComplete(response)
      }
      onClose()
    } catch (err) {
      if (err?.code === 'HEAD_MISMATCH') {
        setError('Branch head changed since you opened this dialog. Refresh history and retry.')
      } else {
        setError(err.message || 'Rollback failed')
      }
    } finally {
      setRollingBack(false)
    }
  }

  const rowClass = (column, rowType) => {
    if (column === 'previous') {
      if (rowType === 'removed' || rowType === 'changed') return 'bg-danger-fg/15 text-danger-fg'
      if (rowType === 'same') return 'text-ink-soft'
      return 'text-muted'
    }

    if (rowType === 'added' || rowType === 'changed') return 'bg-success-fg/15 text-success-fg'
    if (rowType === 'same') return 'text-ink-soft'
    return 'text-muted'
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm p-4">
      <div className="absolute inset-0" onClick={onClose} />
      <div className="relative w-full max-w-7xl">
        <GlassCard className="p-6" hover={false}>
          <div className="flex items-start justify-between gap-4 mb-5">
            <div>
              <p className="text-xs uppercase tracking-[0.16em] text-muted">File Versioning</p>
              <h2 className="text-xl font-semibold text-ink">{filePath}</h2>
              <p className="text-sm text-muted">Repository: {repo?.name} {branch ? `• Branch: ${branch}` : ''}</p>
              <p className="text-xs text-success-fg/85 mt-1">Switching versions is safe. Restores create a new immutable version entry.</p>
              <div className="mt-3 inline-flex rounded-lg border border-border overflow-hidden">
                <button
                  type="button"
                  onClick={() => setHistoryScope('all')}
                  className={`px-3 py-1.5 text-xs transition-colors ${historyScope === 'all' ? 'bg-info-fg/30 text-info-fg' : 'bg-cream-mid/70 text-ink-soft hover:bg-cream-mid'}`}
                >
                  Full Repository History
                </button>
                <button
                  type="button"
                  onClick={() => setHistoryScope('branch')}
                  className={`px-3 py-1.5 text-xs transition-colors ${historyScope === 'branch' ? 'bg-info-fg/30 text-info-fg' : 'bg-cream-mid/70 text-ink-soft hover:bg-cream-mid'}`}
                  disabled={!branch}
                  title={branch ? `Only commits reachable from ${branch}` : 'No branch selected'}
                >
                  Branch History Only
                </button>
              </div>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose}>
              <FiX className="w-4 h-4 mr-2" />
              Close
            </Button>
          </div>

          {error && (
            <div className="mb-4 rounded-lg border border-danger-fg/20 bg-danger-bg px-3 py-2 text-sm text-danger-fg">
              {error}
            </div>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-14 text-ink-soft">
              <FiLoader className="w-5 h-5 animate-spin mr-2" />
              Loading file history...
            </div>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
              <div className="xl:col-span-3 rounded-xl border border-border bg-cream-mid/70 p-3 max-h-[64vh] overflow-y-auto">
                <div className="flex items-center justify-between mb-2 gap-2">
                  <p className="text-xs uppercase text-muted tracking-wider">Versions</p>
                  <p className="text-[11px] text-muted">
                    Loaded {versions.length}
                  </p>
                </div>
                <div className="space-y-2">
                  {displayedVersions.map((version) => {
                    const isSelected = selectedVersion?.commit_id === version.commit_id
                    const isActive = latestVersion?.commit_id === version.commit_id
                    return (
                      <button
                        key={version.commit_id}
                        type="button"
                        onClick={() => setSelectedVersion(version)}
                        className={`w-full text-left rounded-lg border px-3 py-2 transition-colors ${
                          isSelected
                            ? 'border-info-fg/50 bg-info-bg'
                            : 'border-border bg-cream-mid/70 hover:bg-cream-mid'
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs text-ink-soft">v{version.version_number}</span>
                          {isActive && (
                            <Badge variant="success" className="text-[10px]">CURRENT ACTIVE</Badge>
                          )}
                        </div>
                        <p className="text-ink text-sm font-medium mt-1 line-clamp-2">{version.message}</p>
                        <p className="text-xs text-muted mt-1">{version.author || 'Unknown'}</p>
                        <p className="text-xs text-muted mt-1 inline-flex items-center gap-1">
                          <FiClock className="w-3 h-3" />
                          {api.formatDate(version.timestamp)}
                        </p>
                        <p className="text-[11px] text-muted mt-1 uppercase tracking-wide">{version.change_type || 'modified'}</p>
                        {version?.lineage?.renamed && (
                          <p className="text-[11px] text-warning-fg/90 mt-1">
                            Renamed path: {version.observed_path}
                          </p>
                        )}
                        <p className="text-xs text-muted font-mono mt-1">{version.commit_id.slice(0, 12)}</p>
                      </button>
                    )
                  })}

                  {versions.length === 0 && (
                    <p className="text-sm text-muted">No version history found for this file.</p>
                  )}

                  {hasMoreHistory && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full text-info-fg hover:text-ink border border-info-fg/30"
                      onClick={loadOlderVersions}
                      disabled={loadingMoreHistory}
                    >
                      {loadingMoreHistory ? 'Loading...' : 'Load older versions'}
                    </Button>
                  )}
                </div>
              </div>

              <div className="xl:col-span-9">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-sm text-ink-soft flex items-center gap-2">
                    <FiGitCommit className="w-4 h-4" />
                    <span>
                      Comparing <span className="text-ink">{selectedVersion?.commit_id?.slice(0, 8) || '—'}</span> → <span className="text-ink">{latestVersion?.commit_id?.slice(0, 8) || '—'}</span>
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => setShowConfirm(true)}
                      disabled={!canRollback || rollingBack}
                    >
                      <FiRotateCcw className="w-4 h-4 mr-2" />
                      Switch to Previous Version and Save
                    </Button>
                  </div>
                </div>

                {loadingCompare ? (
                  <div className="flex items-center justify-center py-14 text-ink-soft border border-border rounded-xl bg-cream-mid/70">
                    <FiLoader className="w-5 h-5 animate-spin mr-2" />
                    Building visual diff...
                  </div>
                ) : compareResult?.is_binary ? (
                  <div className="rounded-xl border border-warning-fg/20 bg-warning-bg p-4 text-warning-fg text-sm">
                    Binary or oversized file detected. Inline text diff is not available for this comparison.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                    <div className="rounded-xl border border-danger-fg/20 bg-danger-fg/5 overflow-hidden">
                      <div className="px-3 py-2 text-xs font-semibold text-danger-fg border-b border-danger-fg/20">Previous (red)</div>
                      <div className="max-h-[56vh] overflow-auto font-mono text-xs">
                        {compareRows.map((row, idx) => (
                          <pre key={`prev-${idx}`} className={`px-3 py-1 whitespace-pre-wrap break-words ${rowClass('previous', row.type)}`}>
                            {row.previous || ' '}
                          </pre>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-xl border border-success-fg/20 bg-success-fg/5 overflow-hidden">
                      <div className="px-3 py-2 text-xs font-semibold text-success-fg border-b border-success-fg/20">Current (green)</div>
                      <div className="max-h-[56vh] overflow-auto font-mono text-xs">
                        {compareRows.map((row, idx) => (
                          <pre key={`curr-${idx}`} className={`px-3 py-1 whitespace-pre-wrap break-words ${rowClass('current', row.type)}`}>
                            {row.current || ' '}
                          </pre>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {showConfirm && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm p-4">
              <GlassCard className="p-5 max-w-md w-full" hover={false}>
                <h3 className="text-lg font-semibold text-ink mb-2">Confirm rollback</h3>
                <p className="text-sm text-ink-soft mb-4">
                  Switch <span className="text-ink font-medium">{filePath}</span> to historical commit <span className="text-ink font-mono">{selectedVersion?.commit_id?.slice(0, 12)}</span> and save as current?
                </p>
                <p className="text-xs text-ink/55 mb-5">
                  This creates a new commit so restore is reversible and all prior versions remain accessible.
                </p>
                <div className="flex justify-end gap-2">
                  <Button variant="secondary" size="sm" onClick={() => setShowConfirm(false)} disabled={rollingBack}>
                    Cancel
                  </Button>
                  <Button variant="danger" size="sm" onClick={handleRollback} disabled={rollingBack}>
                    {rollingBack ? (
                      <>
                        <FiLoader className="w-4 h-4 mr-2 animate-spin" />
                        Rolling back...
                      </>
                    ) : (
                      <>
                        <FiRotateCcw className="w-4 h-4 mr-2" />
                        Switch and Save
                      </>
                    )}
                  </Button>
                </div>
              </GlassCard>
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  )
}

export default FileVersioningModal
