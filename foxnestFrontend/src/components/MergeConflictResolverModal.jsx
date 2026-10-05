import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  FiAlertTriangle, FiCheck, FiChevronLeft, FiFile, FiGitMerge, FiLoader, FiRefreshCw,
  FiRotateCcw, FiTrash2, FiX,
} from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import Badge from './ui/Badge'
import { ModalOverlay } from './ui/Modal'
import api from '../utils/api'
import {
  applyChoice, applyChoiceToAll, b64Size, b64ToText, parseConflictBlocks, textToB64,
} from '../lib/conflictText'

/**
 * Resolving merge conflicts, modelled on GitHub's conflict editor.
 *
 * Each file is edited with its conflict blocks inline, offering "accept target / source /
 * both" per block, and is then marked resolved -- which saves the decision on the server,
 * so closing this dialog loses nothing and someone else can pick up where you stopped.
 * "Commit merge" is enabled once every file is resolved.
 *
 * For a pull request the commit goes to the pull request's own branch (a merge of the
 * target into it), exactly as GitHub does; the pull request then merges normally after
 * review. For a direct branch merge it goes to the target.
 *
 * Content is decoded as UTF-8 (see lib/conflictText.js); atob/btoa corrupted every
 * non-ASCII character this dialog used to show or save.
 */

const KIND_LABEL = {
  content: 'Both edited',
  add_add: 'Both added',
  modify_delete: 'Edited / deleted',
  binary: 'Binary',
}

function initialDraft(file) {
  const saved = file.resolution === 'content' && file.resolved_b64 != null
  const draft = { deleted: file.resolution === 'delete', choice: null, keepMarkers: false, dirty: false }
  if (file.is_binary && file.kind !== 'modify_delete') {
    if (saved) {
      draft.choice = file.resolved_b64 === file.ours_b64 ? 'ours'
        : file.resolved_b64 === file.theirs_b64 ? 'theirs' : null
    }
    return { ...draft, text: null }
  }
  const source = saved ? file.resolved_b64 : file.suggested_b64
  const text = file.is_binary ? null : (b64ToText(source) ?? '')
  return { ...draft, text, keepMarkers: !!file.resolved_has_markers }
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const MergeConflictResolverModal = ({ repo, prId, sessionId: initialSessionId, onClose, onResolved }) => {
  // A session with no pull request came from a direct branch merge. Same sides, different
  // endpoints, so the dialog picks the pair rather than being duplicated.
  const isBranchSession = prId == null
  const repoId = repo?.id

  const [sessionId, setSessionId] = useState(initialSessionId)
  const [bundle, setBundle] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [resolved, setResolved] = useState({})
  const [activePath, setActivePath] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(null) // what is in flight: a path, 'commit', 'abort', 'recompute'
  const [error, setError] = useState(null)
  const [stale, setStale] = useState(null)
  const [showVersions, setShowVersions] = useState(false)

  const session = bundle?.session
  const files = useMemo(() => bundle?.files || [], [bundle])
  const target = session?.target_branch || 'target'
  const source = session?.source_branch || 'source'

  const load = useCallback(async (id = sessionId) => {
    if (!repoId || !id) return
    setLoading(true)
    setError(null)
    try {
      const res = isBranchSession
        ? await api.getBranchMergeConflicts(repoId, id)
        : await api.getMergeConflicts(repoId, prId, id)
      setBundle(res)
      setStale(res.session?.status === 'open' ? res.session?.stale_reason || null
        : `This session is ${res.session?.status}.`)
      const nextDrafts = {}
      const nextResolved = {}
      ;(res.files || []).forEach((f) => {
        nextDrafts[f.path] = initialDraft(f)
        nextResolved[f.path] = !!f.resolved
      })
      setDrafts(nextDrafts)
      setResolved(nextResolved)
      setActivePath((prev) => ((res.files || []).some((f) => f.path === prev) ? prev : res.files?.[0]?.path || null))
    } catch (err) {
      setError(err.message || 'Unable to load merge conflicts')
    } finally {
      setLoading(false)
    }
  }, [repoId, prId, isBranchSession, sessionId])

  useEffect(() => {
    load(initialSessionId)
    // Reload only when the dialog is pointed at a different session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId, prId, initialSessionId])

  const activeFile = files.find((f) => f.path === activePath) || null
  const draft = activeFile ? drafts[activeFile.path] : null
  const blocks = useMemo(
    () => (draft?.text != null && !draft.deleted ? parseConflictBlocks(draft.text) : []),
    [draft?.text, draft?.deleted]
  )

  const resolvedCount = files.filter((f) => resolved[f.path] && !drafts[f.path]?.dirty).length
  const anyDirty = files.some((f) => drafts[f.path]?.dirty)
  const allResolved = files.length > 0 && resolvedCount === files.length

  const updateDraft = (path, patch) => {
    setDrafts((prev) => ({ ...prev, [path]: { ...prev[path], ...patch, dirty: true } }))
    // Editing a resolved file un-resolves it until it is marked again, as on GitHub.
    setResolved((prev) => ({ ...prev, [path]: false }))
  }

  const handleFailure = (err, fallback) => {
    if (err?.code === 'SESSION_STALE' || err?.code === 'SESSION_CLOSED') {
      setStale(err.message)
    } else {
      setError(err?.message || fallback)
    }
  }

  const save = async (file, payload) => {
    setBusy(file.path)
    setError(null)
    try {
      const res = isBranchSession
        ? await api.saveBranchMergeResolution(repoId, sessionId, payload)
        : await api.saveMergeConflictResolution(repoId, prId, sessionId, payload)
      const isNowResolved = !payload.clear
      setResolved((prev) => ({ ...prev, [file.path]: isNowResolved }))
      setDrafts((prev) => ({ ...prev, [file.path]: { ...prev[file.path], dirty: false } }))
      if (isNowResolved) {
        // Move on to the next file that still needs a decision.
        const next = files.find((f) => f.path !== file.path && !(resolved[f.path] && !drafts[f.path]?.dirty))
        if (next) setActivePath(next.path)
      }
      return res
    } catch (err) {
      handleFailure(err, 'Could not save that decision')
      return null
    } finally {
      setBusy(null)
    }
  }

  const markResolved = (file) => {
    const d = drafts[file.path]
    if (d.deleted) return save(file, { path: file.path, delete: true })
    if (file.is_binary) {
      const b64 = file.kind === 'modify_delete'
        ? file.suggested_b64
        : (d.choice === 'theirs' ? file.theirs_b64 : file.ours_b64)
      return save(file, { path: file.path, content_b64: b64 })
    }
    return save(file, { path: file.path, content_b64: textToB64(d.text) })
  }

  const markUnresolved = (file) => save(file, { path: file.path, clear: true })

  const commit = async () => {
    setBusy('commit')
    setError(null)
    try {
      const payload = {
        expected_head_commit_id: session?.lands_on === 'source'
          ? session?.source_head_commit_id : session?.target_head_commit_id,
        allow_markers: files.some((f) => drafts[f.path]?.keepMarkers),
      }
      const result = isBranchSession
        ? await api.resolveBranchMergeConflicts(repoId, sessionId, payload)
        : await api.resolveMergeConflicts(repoId, prId, sessionId, payload)
      onResolved?.(result)
      onClose?.()
    } catch (err) {
      handleFailure(err, 'Could not commit the merge')
    } finally {
      setBusy(null)
    }
  }

  const recompute = async () => {
    setBusy('recompute')
    setError(null)
    try {
      const res = isBranchSession
        ? await api.startBranchMergeConflictSession(repoId, source, target)
        : await api.startMergeConflictSession(repoId, prId)
      const nextId = res?.session?.id
      setSessionId(nextId)
      await load(nextId)
    } catch (err) {
      setError(err.message || 'Could not recompute the conflicts')
    } finally {
      setBusy(null)
    }
  }

  const abort = async () => {
    if (!window.confirm('Discard this resolution session? Saved decisions are dropped; neither branch changes.')) return
    setBusy('abort')
    try {
      if (isBranchSession) await api.abortBranchMergeConflicts(repoId, sessionId)
      else await api.abortMergeConflicts(repoId, prId, sessionId)
      onClose?.()
    } catch (err) {
      handleFailure(err, 'Could not discard the session')
    } finally {
      setBusy(null)
    }
  }

  const requestClose = () => {
    if (anyDirty && !window.confirm('Some edits are not marked as resolved yet and will be lost. Close anyway?')) return
    onClose?.()
  }

  const workable = !stale && !loading

  return (
    <ModalOverlay onClose={requestClose}>
      <div className="relative max-h-[94vh] w-full max-w-7xl overflow-y-auto">
        <GlassCard className="p-5" hover={false}>
          {/* Header */}
          <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs uppercase tracking-[0.16em] text-muted">Resolve conflicts</p>
              <h2 className="text-lg font-semibold text-ink">
                {repo?.name}{isBranchSession ? '' : ` · PR #${prId}`}
              </h2>
              <p className="font-mono text-xs text-muted">
                {source} → {target}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {files.length > 0 && (
                <Badge variant={allResolved ? 'success' : 'warning'}>
                  {resolvedCount} of {files.length} resolved
                </Badge>
              )}
              <Button variant="ghost" size="sm" onClick={requestClose} disabled={busy === 'commit'}>
                <FiX className="h-4 w-4" /> Close
              </Button>
            </div>
          </div>

          {stale && (
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warning-fg/40 bg-warning-fg/10 px-3 py-2 text-sm text-warning-fg">
              <span className="flex items-center gap-2">
                <FiAlertTriangle className="h-4 w-4 shrink-0" />
                {stale} Recompute to see the conflicts as they are now.
              </span>
              <Button size="sm" variant="secondary" onClick={recompute} loading={busy === 'recompute'}>
                <FiRefreshCw className="h-3.5 w-3.5" /> Recompute
              </Button>
            </div>
          )}

          {error && (
            <div className="mb-4 rounded-lg border border-danger-fg/20 bg-danger-bg px-3 py-2 text-sm text-danger-fg">
              {error}
            </div>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-16 text-ink-soft">
              <FiLoader className="mr-2 h-5 w-5 animate-spin" /> Loading conflicts…
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
              {/* File list */}
              <div className="max-h-[68vh] overflow-auto rounded-xl border border-border bg-cream-mid/70 p-3">
                <p className="mb-2 px-1 text-xs font-medium uppercase tracking-wider text-muted">
                  Conflicting files
                </p>
                <div className="space-y-1.5">
                  {files.map((f) => {
                    const isDone = resolved[f.path] && !drafts[f.path]?.dirty
                    const open = drafts[f.path]?.text != null && !drafts[f.path]?.deleted
                      ? parseConflictBlocks(drafts[f.path].text).length : null
                    return (
                      <button
                        key={f.path}
                        type="button"
                        onClick={() => setActivePath(f.path)}
                        className={`w-full rounded-lg border px-2.5 py-2 text-left transition-colors ${
                          activePath === f.path ? 'border-accent/50 bg-accent/[0.08]' : 'border-border hover:bg-white/[0.04]'
                        }`}
                      >
                        <p className="flex items-center gap-1.5 truncate font-mono text-xs text-ink">
                          {isDone
                            ? <FiCheck className="h-3.5 w-3.5 shrink-0 text-success-fg" />
                            : <FiFile className="h-3.5 w-3.5 shrink-0 text-warning-fg" />}
                          <span className="truncate">{f.path}</span>
                        </p>
                        <p className="mt-1 text-[11px] text-muted">
                          {KIND_LABEL[f.kind] || f.kind}
                          {drafts[f.path]?.deleted ? ' · will be deleted' : ''}
                          {!isDone && open ? ` · ${open} conflict${open === 1 ? '' : 's'}` : ''}
                          {drafts[f.path]?.dirty ? ' · unsaved' : ''}
                        </p>
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* Editor */}
              <div className="min-w-0 rounded-xl border border-border bg-cream-mid/70 p-4">
                {!activeFile || !draft ? (
                  <p className="text-sm text-muted">Select a file to resolve.</p>
                ) : (
                  <FileEditor
                    file={activeFile}
                    draft={draft}
                    blocks={blocks}
                    target={target}
                    source={source}
                    isResolved={resolved[activeFile.path] && !draft.dirty}
                    busy={busy === activeFile.path}
                    disabled={!workable || !!busy}
                    showVersions={showVersions}
                    onToggleVersions={() => setShowVersions((v) => !v)}
                    onChange={(patch) => updateDraft(activeFile.path, patch)}
                    onMarkResolved={() => markResolved(activeFile)}
                    onMarkUnresolved={() => markUnresolved(activeFile)}
                  />
                )}
              </div>
            </div>
          )}

          {/* Commit */}
          {!loading && files.length > 0 && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
              <Button variant="danger" size="sm" onClick={abort} disabled={!!busy || !!stale}>
                <FiChevronLeft className="h-4 w-4" /> Discard session
              </Button>
              <div className="flex flex-wrap items-center gap-3">
                <p className="max-w-xl text-right text-xs text-muted">
                  {session?.lands_on === 'source'
                    ? `Commits a merge of '${target}' into '${source}'. The pull request stays open; approvals given before this commit will need renewing.`
                    : `Merges '${source}' into '${target}'.`}
                </p>
                <Button onClick={commit} disabled={!allResolved || anyDirty || !workable} loading={busy === 'commit'}>
                  <FiGitMerge className="h-4 w-4" /> Commit merge
                </Button>
              </div>
            </div>
          )}
        </GlassCard>
      </div>
    </ModalOverlay>
  )
}

function FileEditor({
  file, draft, blocks, target, source, isResolved, busy, disabled, showVersions,
  onToggleVersions, onChange, onMarkResolved, onMarkUnresolved,
}) {
  const isModifyDelete = file.kind === 'modify_delete'
  const isBinaryChoice = file.is_binary && !isModifyDelete
  const hasMarkers = blocks.length > 0
  const canMark = !disabled && (
    draft.deleted
    || (isBinaryChoice ? !!draft.choice : (file.is_binary || !hasMarkers || draft.keepMarkers))
  )

  const resetText = () => onChange({ text: b64ToText(file.suggested_b64) ?? '', deleted: false, keepMarkers: false })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-sm font-medium text-ink">{file.path}</p>
          <p className="mt-0.5 text-xs text-muted">
            {KIND_LABEL[file.kind] || file.kind}
            {isResolved && <span className="ml-2 text-success-fg">· resolved{file.resolved_by ? ` by ${file.resolved_by}` : ''}</span>}
          </p>
        </div>
        {!file.is_binary && !draft.deleted && (
          <div className="flex flex-wrap items-center gap-1.5">
            <Button variant="secondary" size="sm" disabled={disabled || !hasMarkers}
              onClick={() => onChange({ text: applyChoiceToAll(draft.text, 'ours') })}>
              Accept all from {target}
            </Button>
            <Button variant="secondary" size="sm" disabled={disabled || !hasMarkers}
              onClick={() => onChange({ text: applyChoiceToAll(draft.text, 'theirs') })}>
              Accept all from {source}
            </Button>
            <Button variant="ghost" size="sm" disabled={disabled} onClick={resetText} title="Start again from the suggested merge">
              <FiRotateCcw className="h-3.5 w-3.5" /> Reset
            </Button>
          </div>
        )}
      </div>

      {isModifyDelete && (
        <div className="rounded-lg border border-border bg-white/[0.03] p-3 text-sm text-ink-soft">
          <p className="mb-2">
            <span className="font-mono text-ink">{file.deleted_in === 'target' ? target : source}</span> deleted this file;{' '}
            <span className="font-mono text-ink">{file.deleted_in === 'target' ? source : target}</span> changed it.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant={!draft.deleted ? 'outline' : 'ghost'} disabled={disabled}
              onClick={() => onChange({ deleted: false })}>
              <FiFile className="h-3.5 w-3.5" /> Keep the file
            </Button>
            <Button size="sm" variant={draft.deleted ? 'danger' : 'ghost'} disabled={disabled}
              onClick={() => onChange({ deleted: true })}>
              <FiTrash2 className="h-3.5 w-3.5" /> Delete the file
            </Button>
          </div>
        </div>
      )}

      {isBinaryChoice && (
        <div className="grid gap-2 sm:grid-cols-2">
          {[['ours', target, file.ours_b64], ['theirs', source, file.theirs_b64]].map(([side, branch, b64]) => (
            <button
              key={side}
              type="button"
              disabled={disabled}
              onClick={() => onChange({ choice: side })}
              className={`rounded-lg border p-3 text-left text-sm transition ${
                draft.choice === side ? 'border-accent/60 bg-accent/[0.08] text-ink' : 'border-border text-ink-soft hover:bg-white/[0.04]'
              }`}
            >
              <p className="font-medium">Use the {branch} version</p>
              <p className="mt-1 text-xs text-muted">{formatBytes(b64Size(b64))} · binary file</p>
            </button>
          ))}
        </div>
      )}

      {draft.deleted && (
        <p className="rounded-lg border border-danger-fg/25 bg-danger-fg/10 px-3 py-2 text-sm text-danger-fg">
          This file will be deleted by the merge.
        </p>
      )}

      {!file.is_binary && !draft.deleted && (
        <>
          {blocks.length > 0 && (
            <div className="space-y-1.5">
              {blocks.map((block) => (
                <div key={`${block.index}-${block.start}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warning-fg/30 bg-warning-fg/[0.06] px-3 py-1.5 text-xs">
                  <span className="text-warning-fg">Conflict {block.index + 1} · line {block.line}</span>
                  <span className="flex flex-wrap gap-1">
                    {[['ours', `Accept ${target}`], ['theirs', `Accept ${source}`], ['both', 'Accept both']].map(([choice, label]) => (
                      <button key={choice} type="button" disabled={disabled}
                        onClick={() => onChange({ text: applyChoice(draft.text, block, choice) })}
                        className="rounded border border-border px-2 py-0.5 text-ink-soft transition hover:border-accent/50 hover:text-ink disabled:opacity-40">
                        {label}
                      </button>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          )}

          <textarea
            value={draft.text ?? ''}
            onChange={(e) => onChange({ text: e.target.value })}
            disabled={disabled}
            spellCheck={false}
            className="min-h-[44vh] w-full rounded-lg border border-border bg-cream-deep px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink"
          />

          <button type="button" onClick={onToggleVersions} className="text-xs text-accent hover:underline">
            {showVersions ? 'Hide' : 'Compare'} the {target}, base and {source} versions
          </button>
          {showVersions && (
            <div className="grid gap-2 lg:grid-cols-3">
              {[[target, file.ours_b64], ['base', file.base_b64], [source, file.theirs_b64]].map(([label, b64]) => (
                <div key={label} className="min-w-0 rounded-lg border border-border">
                  <p className="border-b border-border px-2 py-1 font-mono text-[11px] text-muted">{label}</p>
                  <pre className="max-h-64 overflow-auto px-2 py-1.5 font-mono text-[11.5px] text-ink-soft">
                    {b64 == null ? '(file does not exist here)' : (b64ToText(b64) ?? '(binary)')}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {file.is_binary && isModifyDelete && !draft.deleted && (
        <p className="text-xs text-muted">Binary file: keeping it keeps the changed version.</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <div className="text-xs">
          {!file.is_binary && !draft.deleted && hasMarkers ? (
            <label className="flex items-center gap-2 text-warning-fg">
              <input type="checkbox" checked={draft.keepMarkers} disabled={disabled}
                onChange={(e) => onChange({ keepMarkers: e.target.checked })} />
              {blocks.length} conflict{blocks.length === 1 ? '' : 's'} left. Tick only if this file really contains marker text.
            </label>
          ) : (
            <span className="text-muted">{draft.dirty ? 'Unsaved changes' : (isResolved ? 'Saved' : 'Not resolved yet')}</span>
          )}
        </div>
        <div className="flex gap-2">
          {isResolved && (
            <Button variant="ghost" size="sm" disabled={disabled} onClick={onMarkUnresolved}>Mark as unresolved</Button>
          )}
          {!isResolved && (
            <Button size="sm" disabled={!canMark} loading={busy} onClick={onMarkResolved}>
              <FiCheck className="h-4 w-4" /> Mark as resolved
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

export default MergeConflictResolverModal
