import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  FiArrowLeft, FiCode, FiGitCommit, FiUsers, FiFileText,
  FiLoader, FiAlertCircle, FiGitBranch,
} from 'react-icons/fi'
import api from '../utils/api'
import FileTree from '../components/FileTree'
import { ancestorsOf, normalizePath } from '../lib/fileTree'
import CodeView from '../components/CodeView'
import { authorClass, blameSummary } from '../lib/blame'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import FadeContent from '../components/react-bits/FadeContent'
import { cn } from '../lib/utils'

/**
 * Browsing a repository: tree on the left, file on the right.
 *
 * A page rather than a dialog because this carries a tree, source, a diff and a
 * blame view at once, and a dialog cannot give any of them room.
 */


const TABS = [
  { id: 'code', label: 'Code', icon: FiCode },
  { id: 'commits', label: 'Commits', icon: FiGitCommit },
  { id: 'contributors', label: 'Contributors', icon: FiUsers },
]

function Breadcrumbs({ path, onNavigate }) {
  if (!path) return null
  const parts = normalizePath(path).split('/')
  return (
    <div className="flex flex-wrap items-center gap-1 font-mono text-[11.5px] text-muted">
      {parts.map((part, index) => {
        const isLast = index === parts.length - 1
        return (
          <React.Fragment key={index}>
            {index > 0 && <span aria-hidden>/</span>}
            {isLast ? (
              <span className="text-ink">{part}</span>
            ) : (
              <button
                type="button"
                className="hover:text-ink hover:underline"
                onClick={() => onNavigate(parts.slice(0, index + 1).join('/'))}
              >
                {part}
              </button>
            )}
          </React.Fragment>
        )
      })}
    </div>
  )
}

export default function RepositoryDetail({ repo, onBack }) {
  const [branches, setBranches] = useState([])
  const [branch, setBranch] = useState(null)
  const [files, setFiles] = useState({})
  const [filesLoading, setFilesLoading] = useState(true)
  const [filesError, setFilesError] = useState('')

  const [selectedFile, setSelectedFile] = useState(null)
  const [expanded, setExpanded] = useState(() => new Set())
  const [tab, setTab] = useState('code')

  // 'code' | 'diff' | 'blame' -- which rendering of the selected file is showing.
  const [fileMode, setFileMode] = useState('code')
  const [blame, setBlame] = useState(null)
  const [diffRows, setDiffRows] = useState(null)
  const [diffStats, setDiffStats] = useState(null)
  const [paneLoading, setPaneLoading] = useState(false)
  const [paneError, setPaneError] = useState('')
  const [paneNote, setPaneNote] = useState('')

  const [commits, setCommits] = useState([])
  const [commitsLoading, setCommitsLoading] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const response = await api.getBranches(repo.id)
        if (!alive) return
        const list = response?.branches || []
        setBranches(list)
        const preferred =
          list.find((b) => b.is_default) || list.find((b) => b.name === 'main') || list[0]
        setBranch(preferred?.name || null)
      } catch {
        if (alive) setBranch(null)
      }
    }
    load()
    return () => { alive = false }
  }, [repo.id])

  // One request per branch brings the whole tree *with* content, so opening a
  // file is instant and needs no further round trip.
  useEffect(() => {
    let alive = true
    setFilesLoading(true)
    setFilesError('')
    const load = async () => {
      try {
        const response = await api.getRepositoryFiles(repo.id, branch, { includeContent: true })
        if (!alive) return
        setFiles(response?.files || {})
      } catch (err) {
        if (alive) setFilesError(err.message || 'Could not load files')
      } finally {
        if (alive) setFilesLoading(false)
      }
    }
    load()
    return () => { alive = false }
  }, [repo.id, branch])

  useEffect(() => {
    if (tab !== 'commits' || commits.length) return undefined
    let alive = true
    setCommitsLoading(true)
    const load = async () => {
      try {
        const response = await api.getCommits(repo.id, false, branch)
        if (alive) setCommits(response?.commits || [])
      } catch {
        if (alive) setCommits([])
      } finally {
        if (alive) setCommitsLoading(false)
      }
    }
    load()
    return () => { alive = false }
  }, [tab, repo.id, branch, commits.length])

  const paths = useMemo(() => Object.keys(files).sort(), [files])
  const currentFile = selectedFile ? files[selectedFile] : null

  const openFile = useCallback((path) => {
    setSelectedFile(path)
    setFileMode('code')
    setBlame(null)
    setDiffRows(null)
    setDiffStats(null)
    setPaneError('')
    setPaneNote('')
    setTab('code')
  }, [])

  const revealFolder = (folderPath) => {
    setExpanded((prev) => new Set([...prev, ...ancestorsOf(`${folderPath}/x`), folderPath]))
  }

  const showBlame = async () => {
    if (!selectedFile) return
    setFileMode('blame')
    setPaneError('')
    setPaneNote('')
    if (blame) return
    setPaneLoading(true)
    try {
      const response = await api.blameFile(repo.id, selectedFile, { branch })
      if (response?.binary) {
        setPaneNote(response.message || 'Blame is not available for binary files.')
      } else {
        setBlame(response)
        if (response?.truncated_history) {
          setPaneNote('History is deeper than the blame limit; the oldest lines are approximate.')
        }
      }
    } catch (err) {
      setPaneError(
        err.status === 413
          ? 'This file is too large to blame.'
          : err.message || 'Could not load blame'
      )
    } finally {
      setPaneLoading(false)
    }
  }

  const showDiff = async () => {
    if (!selectedFile) return
    setFileMode('diff')
    setPaneError('')
    setPaneNote('')
    if (diffRows) return
    setPaneLoading(true)
    try {
      let list = commits
      if (!list.length) {
        const response = await api.getCommits(repo.id, false, branch)
        list = response?.commits || []
        setCommits(list)
      }
      if (list.length < 2) {
        setPaneNote('This is the first commit on the branch, so there is nothing to compare against.')
        setDiffRows([])
        return
      }
      // Compare the whole commit and pick the file out client-side: a single-file
      // compare depends on the stored path spelling, and older history has both.
      const response = await api.compareCommits(repo.id, list[1].id, list[0].id)
      const wanted = normalizePath(selectedFile)
      const entry = (response?.files || []).find((f) => normalizePath(f.file_path) === wanted)
      if (!entry) {
        setPaneNote('This file did not change in the most recent commit.')
        setDiffRows([])
      } else if (entry.is_binary) {
        setPaneNote('This file is binary, so there is no line-level diff.')
        setDiffRows([])
      } else {
        setDiffRows(entry.diff?.rows || [])
        setDiffStats(entry.diff?.stats || null)
      }
    } catch (err) {
      setPaneError(err.message || 'Could not load the diff')
    } finally {
      setPaneLoading(false)
    }
  }

  const summary = useMemo(() => (blame ? blameSummary(blame) : []), [blame])

  const repoContributors = useMemo(() => {
    const tally = new Map()
    for (const commit of commits) {
      const author = commit.author || 'unknown'
      tally.set(author, (tally.get(author) || 0) + 1)
    }
    const total = commits.length || 1
    return [...tally.entries()]
      .map(([author, count]) => ({ author, count, percent: Math.round((count / total) * 100) }))
      .sort((a, b) => b.count - a.count)
  }, [commits])

  const modeLabel = { code: 'Code', diff: 'Show differences', blame: 'Blame' }

  return (
    <FadeContent className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1.5 rounded-xl border border-border bg-white/[0.04] px-3 py-1.5 text-sm text-ink-soft transition hover:border-border-strong hover:text-ink"
        >
          <FiArrowLeft className="h-4 w-4" /> Repositories
        </button>
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-ink">{repo.name}</h1>
          <p className="truncate text-xs text-muted">
            {repo.owner ? `${repo.owner} · ` : ''}{paths.length} files
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <FiGitBranch className="h-4 w-4 text-muted" />
          <select
            value={branch || ''}
            onChange={(e) => {
              setBranch(e.target.value)
              setSelectedFile(null)
              setCommits([])
            }}
            className="rounded-xl border border-border bg-cream-mid px-3 py-1.5 text-sm text-ink outline-none focus:border-ink"
          >
            {branches.map((b) => (
              <option key={b.name || b} value={b.name || b}>
                {(b.name || b)}{b.is_default ? ' (default)' : ''}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {TABS.map((entry) => {
          const Icon = entry.icon
          return (
          <button
            key={entry.id}
            type="button"
            onClick={() => setTab(entry.id)}
            className={cn(
              'flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition',
              tab === entry.id
                ? 'border-accent text-ink'
                : 'border-transparent text-muted hover:text-ink'
            )}
          >
            <Icon className="h-4 w-4" /> {entry.label}
          </button>
          )
        })}
      </div>

      {tab === 'code' && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(220px,300px)_1fr]">
          <GlassCard className="max-h-[70vh] overflow-y-auto p-2" hover={false}>
            {filesLoading ? (
              <p className="px-2 py-6 text-center text-sm text-muted">Loading files…</p>
            ) : filesError ? (
              <p className="px-2 py-6 text-center text-sm text-danger-fg">{filesError}</p>
            ) : (
              <FileTree
                paths={paths}
                selected={selectedFile}
                onSelect={openFile}
                expandedPaths={expanded}
                onExpandedChange={setExpanded}
              />
            )}
          </GlassCard>

          <GlassCard className="min-w-0 p-0" hover={false}>
            {!selectedFile ? (
              <div className="px-4 py-20 text-center">
                <FiFileText className="mx-auto mb-3 h-7 w-7 text-muted" />
                <p className="text-sm text-muted">Select a file to read it.</p>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
                  <Breadcrumbs path={selectedFile} onNavigate={revealFolder} />
                  <div className="ml-auto flex items-center gap-1.5">
                    {['code', 'diff', 'blame'].map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => {
                          if (mode === 'code') {
                            setFileMode('code')
                            setPaneError('')
                            setPaneNote('')
                          } else if (mode === 'diff') {
                            showDiff()
                          } else {
                            showBlame()
                          }
                        }}
                        className={cn(
                          'rounded-lg border px-2.5 py-1 text-xs transition',
                          fileMode === mode
                            ? 'border-accent/60 bg-accent/15 text-ink'
                            : 'border-border text-ink-soft hover:border-border-strong hover:text-ink'
                        )}
                      >
                        {modeLabel[mode]}
                      </button>
                    ))}
                  </div>
                </div>

                {fileMode === 'blame' && summary.length > 0 && (
                  <div className="flex flex-wrap items-center gap-3 border-b border-border px-3 py-2">
                    <span className="text-[11px] uppercase tracking-wider text-muted">Contributions</span>
                    {summary.map((row) => (
                      <span key={row.author} className="flex items-center gap-1.5 text-xs">
                        <span className={cn('font-medium', authorClass(row.author))}>{row.author}</span>
                        <span className="text-muted">{row.percent}% · {row.count} lines</span>
                      </span>
                    ))}
                  </div>
                )}

                {fileMode === 'diff' && diffStats && (
                  <div className="flex items-center gap-3 border-b border-border px-3 py-2 text-xs">
                    <span className="text-success-fg">+{diffStats.added}</span>
                    <span className="text-danger-fg">−{diffStats.removed}</span>
                    <span className="text-muted">{diffStats.unchanged} unchanged</span>
                  </div>
                )}

                {paneError && (
                  <p className="flex items-center gap-2 border-b border-border px-3 py-2 text-sm text-danger-fg">
                    <FiAlertCircle className="h-4 w-4 shrink-0" /> {paneError}
                  </p>
                )}
                {paneNote && !paneError && (
                  <p className="border-b border-border px-3 py-2 text-sm text-muted">{paneNote}</p>
                )}

                <div className="max-h-[62vh] overflow-auto p-1">
                  {paneLoading ? (
                    <p className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
                      <FiLoader className="h-4 w-4 animate-spin" /> Loading…
                    </p>
                  ) : currentFile?.is_binary && fileMode === 'code' ? (
                    <p className="py-16 text-center text-sm text-muted">
                      Binary file — not shown.
                    </p>
                  ) : (
                    <CodeView
                      mode={fileMode}
                      content={currentFile?.content}
                      blame={blame}
                      rows={diffRows || []}
                    />
                  )}
                </div>
              </>
            )}
          </GlassCard>
        </div>
      )}

      {tab === 'commits' && (
        <GlassCard className="p-4" hover={false}>
          {commitsLoading ? (
            <p className="py-10 text-center text-sm text-muted">Loading commits…</p>
          ) : !commits.length ? (
            <p className="py-10 text-center text-sm text-muted">No commits on this branch.</p>
          ) : (
            <ol className="space-y-2">
              {commits.map((commit) => (
                <li key={commit.id} className="rounded-xl border border-border bg-white/[0.03] p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[11px] text-accent">
                      {String(commit.id).slice(0, 8)}
                    </span>
                    {(commit.parents || []).length > 1 && <Badge variant="info">merge</Badge>}
                    <span className="text-xs text-muted">
                      {commit.author} · {commit.timestamp}
                    </span>
                  </div>
                  <p className="mt-1 whitespace-pre-wrap break-words text-[13px] text-ink-soft">
                    {String(commit.message || '').split('\n')[0]}
                  </p>
                </li>
              ))}
            </ol>
          )}
        </GlassCard>
      )}

      {tab === 'contributors' && (
        <GlassCard className="p-4" hover={false}>
          {commitsLoading ? (
            <p className="py-10 text-center text-sm text-muted">Loading…</p>
          ) : !repoContributors.length ? (
            <p className="py-10 text-center text-sm text-muted">
              Open the Commits tab first to load history.
            </p>
          ) : (
            <ul className="space-y-2">
              {repoContributors.map((row) => (
                <li key={row.author} className="flex items-center gap-3">
                  <span className={cn('w-40 shrink-0 truncate text-sm font-medium', authorClass(row.author))}>
                    {row.author}
                  </span>
                  <span className="h-2 flex-1 overflow-hidden rounded-full bg-white/[0.06]">
                    <span
                      className="block h-full rounded-full bg-accent"
                      style={{ width: `${row.percent}%` }}
                    />
                  </span>
                  <span className="w-28 shrink-0 text-right text-xs text-muted">
                    {row.count} commits · {row.percent}%
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-4 border-t border-border pt-3 text-xs text-muted">
            Per-line authorship for a specific file is on the Blame view in the Code tab.
          </p>
        </GlassCard>
      )}
    </FadeContent>
  )
}
