import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  FiBookOpen, FiFile, FiRefreshCw, FiAlertTriangle, FiDownload, FiCpu, FiCheckCircle,
  FiClock, FiLoader, FiXCircle,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import EmptyState from '../ui/EmptyState'
import FileTree from '../FileTree'
import Markdown from '../Markdown'
import api from '../../utils/api'
import { authedFetch, downloadAuthed } from '../../utils/authedDownload'
import { ancestorsOf } from '../../lib/fileTree'
import { absoluteTime, relativeTime } from '../../lib/time'
import { cn } from '../../lib/utils'

/**
 * The documentation the server generates, read in place.
 *
 * Markdown is rendered (it used to be shown as raw text in a <pre>), links between the
 * documents move between them, and the open document is in the address, so it can be
 * linked to. The panel says what the documents were generated from, whether the
 * default branch has moved on since, and -- when they were not committed -- why.
 *
 * Generation runs in the background on the server for minutes; while it does, this
 * polls its status and shows each document's progress, instead of announcing success
 * the moment the job was queued.
 */

const POLL_MS = 3000
const TEXT_TYPES = /\.(md|markdown|txt|json|csv|ya?ml|xml|html?|css|js|py|sql)$/i

function StepBadge({ status }) {
  if (status === 'done') return <FiCheckCircle className="h-3.5 w-3.5 text-success-fg" />
  if (status === 'generating') return <FiLoader className="h-3.5 w-3.5 animate-spin text-accent" />
  if (status === 'error') return <FiXCircle className="h-3.5 w-3.5 text-danger-fg" />
  if (status === 'skipped') return <FiXCircle className="h-3.5 w-3.5 text-muted" />
  return <FiClock className="h-3.5 w-3.5 text-muted" />
}

export default function DocsTab({ repo, docPath, onOpenDoc, onOpenCommit }) {
  const repoId = repo.id
  const [listing, setListing] = useState(null)
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [content, setContent] = useState({ path: null, text: '', loading: false, error: null })
  const [expanded, setExpanded] = useState(() => new Set())
  const [dialog, setDialog] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const wasRunning = useRef(false)

  const loadListing = useCallback(async () => {
    try {
      setError(null)
      setListing(await api.getRepositoryDocs(repoId))
    } catch (err) {
      setError(err.message || 'Could not load the documentation')
    }
  }, [repoId])

  const loadStatus = useCallback(async () => {
    try {
      const next = await api.getDocsStatus(repoId)
      setStatus(next)
      return next
    } catch (err) {
      setError(err.message || 'Could not load the generation status')
      return null
    }
  }, [repoId])

  useEffect(() => {
    let alive = true
    setLoading(true)
    Promise.all([loadListing(), loadStatus()]).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [loadListing, loadStatus])

  const running = ['queued', 'running', 'processing'].includes(status?.status)

  // While a job runs, follow it; when it ends, show what it produced.
  useEffect(() => {
    if (!running) {
      if (wasRunning.current) {
        wasRunning.current = false
        loadListing()
      }
      return undefined
    }
    wasRunning.current = true
    const timer = setInterval(loadStatus, POLL_MS)
    return () => clearInterval(timer)
  }, [running, loadStatus, loadListing])

  const files = useMemo(() => (listing?.files || []).map((f) => f.path), [listing])
  const sizes = useMemo(() => Object.fromEntries((listing?.files || []).map((f) => [f.path, f.size])), [listing])

  // No document named: open the index, as a documentation site would.
  const selected = useMemo(() => {
    if (docPath) return docPath
    for (const candidate of ['index.md', 'project/index.md', 'project/README.md']) {
      if (files.includes(candidate)) return candidate
    }
    return files.find((p) => p.toLowerCase().endsWith('.md')) || files[0] || null
  }, [docPath, files])

  useEffect(() => {
    if (selected) setExpanded((prev) => new Set([...prev, ...ancestorsOf(selected)]))
  }, [selected])

  useEffect(() => {
    if (!selected || !files.includes(selected) || !TEXT_TYPES.test(selected)) {
      setContent({ path: selected, text: '', loading: false, error: null })
      return undefined
    }
    let alive = true
    setContent({ path: selected, text: '', loading: true, error: null })
    authedFetch(api.docFileUrl(repoId, selected), { responseType: 'text' })
      .then((text) => { if (alive) setContent({ path: selected, text, loading: false, error: null }) })
      .catch((err) => { if (alive) setContent({ path: selected, text: '', loading: false, error: err.message }) })
    return () => { alive = false }
  }, [repoId, selected, files])

  const permissions = status?.permissions || listing?.permissions || {}
  const last = listing?.last_generation || status?.last_generation
  const upToDate = listing?.up_to_date ?? status?.up_to_date
  const defaultBranch = listing?.default_branch || status?.default_branch
  const available = status?.available_documents || []

  const openGenerate = () => setDialog({
    selected: new Set(available.map((d) => d.key)),
    commit: true,
  })

  const submitGenerate = async () => {
    try {
      setSubmitting(true)
      setError(null)
      const all = dialog.selected.size === available.length
      await api.generateProjectDocs(repoId, {
        selectedDocs: all ? [] : [...dialog.selected],
        commit: dialog.commit,
      })
      setDialog(null)
      const next = await loadStatus()
      // A short job can finish before the first poll, and then nothing was "running"
      // for the effect above to notice ending.
      if (!['queued', 'running', 'processing'].includes(next?.status)) await loadListing()
    } catch (err) {
      setError(err.message || 'Could not start generation')
    } finally {
      setSubmitting(false)
    }
  }

  const downloadFile = async (path) => {
    try {
      await downloadAuthed(api.docFileUrl(repoId, path), path.split('/').pop())
    } catch (err) {
      setError(err.message || 'Download failed')
    }
  }

  const downloadAll = async () => {
    try {
      await downloadAuthed(api.docsArchiveUrl(repoId), `${repo.name}-documentation.zip`)
    } catch (err) {
      setError(err.message || 'Download failed')
    }
  }

  const job = status || {}
  const progress = job.docs_progress || []
  const hasDocs = files.length > 0

  return (
    <div className="space-y-4" data-testid="docs-tab">
      <GlassCard hover={false} className="p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="flex items-center gap-2 font-display text-lg font-light tracking-tight text-ink">
              <FiBookOpen className="h-4 w-4 text-accent" /> Generated documentation
              {upToDate === true && <Badge variant="success">Up to date</Badge>}
              {upToDate === false && <Badge variant="warning">{defaultBranch || 'The default branch'} has changed since</Badge>}
            </h3>
            {last ? (
              <div className="mt-1 space-y-0.5 text-[12.5px] text-muted" data-testid="docs-provenance">
                <p>
                  Generated from{' '}
                  <button type="button" className="font-mono text-accent hover:underline"
                    onClick={() => onOpenCommit?.(last.source_commit)}>
                    {String(last.source_commit || '').slice(0, 8)}
                  </button>{' '}
                  on <span className="font-mono text-ink-soft">{last.source_branch}</span>
                  {last.generated_by ? ` by ${last.generated_by}` : ''}{' '}
                  <span title={absoluteTime(last.generated_at)}>{relativeTime(last.generated_at)}</span>
                  {last.llm_model ? ` · ${last.llm_model}` : ''}
                </p>
                {last.committed ? (
                  <p>
                    Committed to {last.source_branch} as{' '}
                    <button type="button" className="font-mono text-accent hover:underline"
                      onClick={() => onOpenCommit?.(last.commit_id)}>
                      {String(last.commit_id || '').slice(0, 8)}
                    </button>
                  </p>
                ) : last.commit_skipped_reason ? (
                  <p className="text-warning-fg" data-testid="docs-not-committed">Not committed: {last.commit_skipped_reason}</p>
                ) : null}
              </div>
            ) : (
              <p className="mt-1 text-[12.5px] text-muted">
                Written by the server&apos;s documentation generator from the default branch.
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button variant="ghost" size="sm" className="!px-2" title="Refresh" aria-label="Refresh documentation"
              onClick={() => { loadListing(); loadStatus() }}>
              <FiRefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
            </Button>
            {hasDocs && (
              <Button variant="secondary" size="sm" onClick={downloadAll}>
                <FiDownload className="h-3.5 w-3.5" /> Download all (.zip)
              </Button>
            )}
            {permissions.can_generate && (
              <Button size="sm" onClick={openGenerate} disabled={running || !available.length}>
                <FiCpu className={cn('h-4 w-4', running && 'animate-pulse')} />
                {running ? 'Generating…' : hasDocs ? 'Regenerate' : 'Generate'}
              </Button>
            )}
          </div>
        </div>

        {running && (
          <div className="mt-4 rounded-xl border border-accent/30 bg-accent/[0.06] p-3" data-testid="docs-job">
            <p className="flex items-center gap-2 text-sm text-ink">
              <FiLoader className="h-4 w-4 animate-spin text-accent" />
              {job.status === 'queued' ? 'Queued' : 'Generating'}
              {job.requested_by ? ` for ${job.requested_by}` : ''}
              {job.started_at ? <span className="text-muted">· started {relativeTime(job.started_at)}</span> : null}
            </p>
            {progress.length > 0 && (
              <ul className="mt-2 grid gap-1 sm:grid-cols-2">
                {progress.map((step) => (
                  <li key={step.name} className="flex items-center gap-2 text-[12.5px] text-ink-soft">
                    <StepBadge status={step.status} /> {step.name}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[11.5px] text-muted">This can take several minutes. You can leave this page; it carries on.</p>
          </div>
        )}
        {job.status === 'failed' && (
          <p className="mt-4 flex items-start gap-2 rounded-xl border border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg" data-testid="docs-job-failed">
            <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>The last generation failed: {job.error || 'unknown error'}</span>
          </p>
        )}
        {job.status === 'completed' && job.result?.commit && !job.result.commit.committed && !last?.commit_skipped_reason && (
          <p className="mt-3 text-[12.5px] text-warning-fg">Not committed: {job.result.commit.reason}</p>
        )}
      </GlassCard>

      {error && (
        <GlassCard hover={false} className="flex items-start gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg" role="alert">
          <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      {loading && !listing ? (
        <div className="h-48 animate-pulse rounded-xl bg-white/[0.04]" />
      ) : !hasDocs ? (
        <EmptyState icon={FiBookOpen} title="No documentation yet"
          description={permissions.can_generate
            ? 'Generate it to have the server read the default branch and write a README, architecture notes, API reference and more.'
            : 'Nobody has generated documentation for this repository yet. Someone with write access can.'}
          action={permissions.can_generate && !running && available.length
            ? <Button onClick={openGenerate}>Generate documentation</Button> : null} />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(220px,280px)_1fr]">
          <GlassCard hover={false} className="max-h-[70vh] overflow-y-auto p-2" data-testid="docs-tree">
            <FileTree paths={files} selected={selected} onSelect={(path) => onOpenDoc?.(path)}
              expandedPaths={expanded} onExpandedChange={setExpanded} />
          </GlassCard>
          <GlassCard hover={false} className="min-w-0 p-0">
            {!selected ? null : !files.includes(selected) ? (
              <div className="px-4 py-16 text-center text-sm text-muted">
                <span className="font-mono text-ink">{selected}</span> is not among the generated documents.
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
                  <FiFile className="h-3.5 w-3.5 text-accent" />
                  <span className="min-w-0 truncate font-mono text-[12px] text-ink" data-testid="docs-open-path">{selected}</span>
                  <span className="font-mono text-[11px] text-muted">{api.formatSize(sizes[selected])}</span>
                  <Button variant="ghost" size="sm" className="ml-auto" onClick={() => downloadFile(selected)}>
                    <FiDownload className="h-3.5 w-3.5" /> Download
                  </Button>
                </div>
                <div className="max-h-[66vh] overflow-auto px-5 py-4" data-testid="docs-content">
                  {!TEXT_TYPES.test(selected) ? (
                    <div className="py-12 text-center text-sm text-muted">
                      <p>This is a {selected.split('.').pop().toUpperCase()} file, which the browser cannot show here.</p>
                      <Button variant="secondary" size="sm" className="mt-3" onClick={() => downloadFile(selected)}>
                        <FiDownload className="h-3.5 w-3.5" /> Download it
                      </Button>
                    </div>
                  ) : content.loading ? (
                    <div className="space-y-2">{[0, 1, 2, 3].map((i) => <div key={i} className="h-3 animate-pulse rounded bg-white/[0.06]" />)}</div>
                  ) : content.error ? (
                    <p className="text-sm text-danger-fg">Could not load this file: {content.error}</p>
                  ) : /\.(md|markdown)$/i.test(selected) ? (
                    <Markdown basePath={selected} onNavigate={(path) => onOpenDoc?.(path)}>{content.text}</Markdown>
                  ) : (
                    <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-ink-soft">{content.text}</pre>
                  )}
                </div>
              </>
            )}
          </GlassCard>
        </div>
      )}

      <Modal open={Boolean(dialog)} onClose={() => setDialog(null)} title="Generate documentation"
        subtitle={`The server reads ${defaultBranch || 'the default branch'} and writes the documents you choose. It takes several minutes.`}>
        {dialog && (
          <div className="space-y-4">
            <div>
              <div className="mb-2 flex items-center">
                <span className="text-[13px] font-medium text-ink-soft">Documents</span>
                <button type="button" className="ml-auto text-xs text-accent hover:underline"
                  onClick={() => setDialog({
                    ...dialog,
                    selected: dialog.selected.size === available.length ? new Set() : new Set(available.map((d) => d.key)),
                  })}>
                  {dialog.selected.size === available.length ? 'Clear' : 'Select all'}
                </button>
              </div>
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {available.map((doc) => (
                  <label key={doc.key} className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-ink-soft hover:border-border-strong">
                    <input type="checkbox" checked={dialog.selected.has(doc.key)}
                      onChange={(e) => {
                        const next = new Set(dialog.selected)
                        if (e.target.checked) next.add(doc.key)
                        else next.delete(doc.key)
                        setDialog({ ...dialog, selected: next })
                      }} />
                    {doc.name}
                  </label>
                ))}
              </div>
            </div>
            <label className="flex items-start gap-2 rounded-lg border border-border px-3 py-2.5 text-sm text-ink-soft">
              <input type="checkbox" className="mt-0.5" checked={dialog.commit}
                onChange={(e) => setDialog({ ...dialog, commit: e.target.checked })} />
              <span>
                Commit them to <span className="font-mono text-ink">{defaultBranch || 'the default branch'}</span> under docs/
                <span className="mt-0.5 block text-xs text-muted">
                  Only if you could push that change yourself: protection and push rules apply, and a
                  developer&apos;s changes wait for review, so theirs are kept here instead.
                </span>
              </span>
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setDialog(null)}>Cancel</Button>
              <Button onClick={submitGenerate} loading={submitting} disabled={!dialog.selected.size}>Start</Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
