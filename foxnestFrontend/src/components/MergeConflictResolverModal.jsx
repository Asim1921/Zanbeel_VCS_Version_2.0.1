import React, { useEffect, useMemo, useState } from 'react'
import { FiChevronLeft, FiLoader, FiRefreshCw, FiSave, FiX } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import { ModalOverlay } from './ui/Modal'
import api from '../utils/api'

const decodeB64 = (b64) => {
  if (!b64) return ''
  try {
    return atob(b64)
  } catch {
    return ''
  }
}

const MergeConflictResolverModal = ({ repo, prId, sessionId, expectedHeadCommitId, onClose, onResolved }) => {
  const repoId = repo?.id
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [bundle, setBundle] = useState(null)
  const [activePath, setActivePath] = useState(null)
  const [resolvedByPath, setResolvedByPath] = useState({})
  const [submitting, setSubmitting] = useState(false)

  const files = bundle?.files || []

  const activeFile = useMemo(() => files.find((f) => f.path === activePath) || null, [files, activePath])

  const load = async () => {
    if (!repoId || !prId || !sessionId) return
    try {
      setLoading(true)
      setError(null)
      const res = await api.getMergeConflicts(repoId, prId, sessionId)
      setBundle(res)
      const first = (res.files || [])[0]?.path || null
      setActivePath((prev) => prev || first)

      const initial = {}
      ;(res.files || []).forEach((f) => {
        if (f.is_binary) {
          initial[f.path] = null
        } else {
          initial[f.path] = decodeB64(f.suggested_b64 || f.ours_b64 || '')
        }
      })
      setResolvedByPath((prev) => (Object.keys(prev).length ? prev : initial))
    } catch (err) {
      setError(err.message || 'Unable to load merge conflicts')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId, prId, sessionId])

  const chooseSide = (path, side) => {
    const f = files.find((x) => x.path === path)
    if (!f) return
    if (f.is_binary) {
      // For binary, we still allow choosing ours/theirs by copying their bytes.
      const chosenB64 = side === 'theirs' ? f.theirs_b64 : f.ours_b64
      setResolvedByPath((prev) => ({ ...prev, [path]: decodeB64(chosenB64) }))
      return
    }
    const chosenB64 = side === 'theirs' ? f.theirs_b64 : f.ours_b64
    setResolvedByPath((prev) => ({ ...prev, [path]: decodeB64(chosenB64) }))
  }

  const handleSubmit = async () => {
    if (!repoId || !prId || !sessionId) return
    try {
      setSubmitting(true)
      setError(null)
      const resolutions = {}
      for (const f of files) {
        const text = resolvedByPath[f.path]
        if (text == null) continue
        try {
          resolutions[f.path] = btoa(text)
        } catch {
          resolutions[f.path] = btoa(unescape(encodeURIComponent(text)))
        }
      }

      await api.resolveMergeConflicts(repoId, prId, sessionId, {
        resolutions,
        expected_head_commit_id: expectedHeadCommitId || null,
      })
      onResolved?.()
      onClose?.()
    } catch (err) {
      setError(err.message || 'Failed to resolve merge conflicts')
    } finally {
      setSubmitting(false)
    }
  }

  const handleAbort = async () => {
    if (!repoId || !prId || !sessionId) return
    try {
      setSubmitting(true)
      setError(null)
      await api.abortMergeConflicts(repoId, prId, sessionId, expectedHeadCommitId || null)
      onClose?.()
    } catch (err) {
      setError(err.message || 'Failed to abort merge conflict session')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="relative max-h-[90vh] w-full max-w-6xl overflow-y-auto">
        <GlassCard className="p-6" hover={false}>
          <div className="flex items-start justify-between gap-4 mb-5">
            <div>
              <p className="text-xs uppercase tracking-[0.16em] text-muted">Resolve merge conflicts</p>
              <h2 className="text-xl font-semibold text-ink">{repo?.name} • PR #{prId}</h2>
              <p className="text-sm text-muted">Choose ours/theirs or edit the resolved content, then finalize merge.</p>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={load} disabled={loading || submitting}>
                <FiRefreshCw className="w-4 h-4 mr-2" />
                Refresh
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose} disabled={submitting}>
                <FiX className="w-4 h-4 mr-2" />
                Close
              </Button>
            </div>
          </div>

          {error && (
            <div className="mb-4 rounded-lg border border-danger-fg/20 bg-danger-bg px-3 py-2 text-sm text-danger-fg">
              {error}
            </div>
          )}

          {loading ? (
            <div className="py-10 text-ink-soft flex items-center justify-center">
              <FiLoader className="w-5 h-5 animate-spin mr-2" /> Loading conflicts...
            </div>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
              <div className="xl:col-span-1 rounded-xl border border-border bg-cream-mid/70 p-4 max-h-[64vh] overflow-auto">
                <div className="flex items-center justify-between mb-2">
                  <p className="text-ink font-medium">Files</p>
                  <p className="text-xs text-muted">{files.length}</p>
                </div>
                <div className="space-y-2">
                  {files.map((f) => (
                    <button
                      key={f.path}
                      type="button"
                      onClick={() => setActivePath(f.path)}
                      className={`w-full text-left rounded-lg border px-3 py-2 transition-colors ${
                        activePath === f.path ? 'border-ink/40 bg-cream-mid' : 'border-border bg-cream-mid/70 hover:bg-cream-mid'
                      }`}
                    >
                      <p className="text-xs text-ink font-mono truncate">{f.path}</p>
                      <p className="text-[11px] text-muted mt-1">{f.is_binary ? 'binary' : 'text'}</p>
                    </button>
                  ))}
                </div>
              </div>

              <div className="xl:col-span-2 rounded-xl border border-border bg-cream-mid/70 p-4 max-h-[64vh] overflow-auto">
                {!activeFile ? (
                  <div className="text-muted text-sm">Select a file to resolve.</div>
                ) : (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-ink font-medium font-mono truncate">{activeFile.path}</p>
                        <p className="text-xs text-muted">
                          {bundle?.session?.source_branch} → {bundle?.session?.target_branch}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button variant="ghost" size="sm" className="border border-border" onClick={() => chooseSide(activeFile.path, 'ours')} disabled={submitting}>
                          Use ours
                        </Button>
                        <Button variant="ghost" size="sm" className="border border-border" onClick={() => chooseSide(activeFile.path, 'theirs')} disabled={submitting}>
                          Use theirs
                        </Button>
                      </div>
                    </div>

                    {activeFile.is_binary ? (
                      <div className="text-sm text-ink-soft rounded-lg border border-border bg-cream-mid p-3">
                        Binary conflict: choose “ours” or “theirs”.
                      </div>
                    ) : (
                      <textarea
                        value={resolvedByPath[activeFile.path] ?? ''}
                        onChange={(e) => setResolvedByPath((prev) => ({ ...prev, [activeFile.path]: e.target.value }))}
                        className="w-full min-h-[40vh] bg-cream-deep border border-border rounded px-3 py-2 text-sm text-ink font-mono"
                      />
                    )}

                    <div className="flex items-center justify-between gap-2 pt-2">
                      <Button variant="ghost" size="sm" className="border border-danger-fg/20 text-danger-fg" onClick={handleAbort} disabled={submitting}>
                        <FiChevronLeft className="w-4 h-4 mr-2" />
                        Abort
                      </Button>
                      <Button variant="primary" size="sm" onClick={handleSubmit} disabled={submitting}>
                        {submitting ? <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Saving...</> : <><FiSave className="w-4 h-4 mr-2" />Finalize merge</>}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </GlassCard>
      </div>
    </ModalOverlay>
  )
}

export default MergeConflictResolverModal

