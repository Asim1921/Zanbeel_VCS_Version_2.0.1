import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  FiArrowLeft, FiCode, FiGitCommit, FiUsers, FiFileText, FiDownload,
  FiLoader, FiAlertCircle, FiGitBranch, FiShield, FiShare2, FiActivity, FiPackage, FiBookOpen,
} from 'react-icons/fi'
import api from '../utils/api'
import { downloadAuthed } from '../utils/authedDownload'
import ActivityBrowser from '../components/ActivityBrowser'
import FileTree from '../components/FileTree'
import { ancestorsOf, normalizePath } from '../lib/fileTree'
import CodeView from '../components/CodeView'
import BranchProtectionPanel from '../components/BranchProtectionPanel'
import CommitGraph from '../components/CommitGraph'
import CommitDetail from '../components/CommitDetail'
import BranchesTab from '../components/repository/BranchesTab'
import ReleasesTab from '../components/repository/ReleasesTab'
import DocsTab from '../components/repository/DocsTab'
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
 *
 * What is open -- branch, file, line, commit, tab, document -- is held in the address,
 * not in component state, so any of it can be linked to (search results and
 * notifications do) and Back steps through it.
 *
 * Branches, releases and tags, and the generated documentation were reachable only
 * from the CLI or from a dialog behind a "Manage" button; they are tabs here now.
 */


const TABS = [
  { id: 'code', label: 'Code', icon: FiCode },
  { id: 'commits', label: 'Commits', icon: FiGitCommit },
  { id: 'branches', label: 'Branches', icon: FiGitBranch },
  // Tags are a view of the Releases tab: a release is a tag with notes.
  { id: 'releases', label: 'Releases', icon: FiPackage, also: ['tags'] },
  { id: 'docs', label: 'Docs', icon: FiBookOpen },
  { id: 'graph', label: 'Graph', icon: FiShare2 },
  { id: 'contributors', label: 'Contributors', icon: FiUsers },
  { id: 'activity', label: 'Activity', icon: FiActivity },
  { id: 'protection', label: 'Protection', icon: FiShield },
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
  const [searchParams, setSearchParams] = useSearchParams()
  const urlBranch = searchParams.get('branch')
  const selectedFile = searchParams.get('path')
  const tab = searchParams.get('tab') || 'code'
  const openCommitId = searchParams.get('commit')
  const line = Number(searchParams.get('line')) || null
  const docPath = searchParams.get('doc')

  // Change what is open. A new file, commit or tab is a step Back can return from;
  // pointing at a line within the open file replaces the address instead.
  const go = useCallback((changes, { replace = false } = {}) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      for (const [key, value] of Object.entries(changes)) {
        if (value === null || value === undefined || value === '') next.delete(key)
        else next.set(key, String(value))
      }
      return next
    }, { replace })
  }, [setSearchParams])

  const [branches, setBranches] = useState([])
  const [defaultBranch, setDefaultBranch] = useState(null)
  // Until the branch list arrives the default branch is unknown; fetching the tree
  // before then loaded it twice and made a linked file flicker.
  const [branchesLoaded, setBranchesLoaded] = useState(false)
  const branch = urlBranch || defaultBranch
  const [files, setFiles] = useState({})
  const [filesLoading, setFilesLoading] = useState(true)
  const [filesError, setFilesError] = useState('')

  const [expanded, setExpanded] = useState(() => new Set())
  const setTab = (id) => go({ tab: id === 'code' ? null : id, commit: null, doc: null })
  // Bumped when a branch is created, renamed or deleted, so the selector follows.
  const [branchesVersion, setBranchesVersion] = useState(0)
  const [counts, setCounts] = useState({ tags: null, releases: null })

  // 'code' | 'diff' | 'blame' -- which rendering of the selected file is showing.
  const [fileMode, setFileMode] = useState('code')
  const [blame, setBlame] = useState(null)
  const [diffRows, setDiffRows] = useState(null)
  const [diffStats, setDiffStats] = useState(null)
  const [paneLoading, setPaneLoading] = useState(false)
  const [paneError, setPaneError] = useState('')
  const [paneNote, setPaneNote] = useState('')

  const [commits, setCommits] = useState([])
  // The graph spans every branch, so it is loaded once rather than per branch.
  const [graph, setGraph] = useState(null)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState('')
  const [selectedGraphCommit, setSelectedGraphCommit] = useState(null)
  // The commit whose changes are open, in the address; Back returns to the tab it was
  // opened from.
  const setOpenCommitId = (id) => go({ commit: id })
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
        setDefaultBranch(preferred?.name || null)
      } catch {
        if (alive) setDefaultBranch(null)
      } finally {
        if (alive) setBranchesLoaded(true)
      }
    }
    load()
    return () => { alive = false }
  }, [repo.id, branchesVersion])

  // A branch renamed or deleted from the Branches tab: stop pointing at it.
  useEffect(() => {
    if (!branchesLoaded || !urlBranch || !branches.length) return
    if (!branches.some((b) => (b.name || b) === urlBranch)) go({ branch: null }, { replace: true })
  }, [branches, branchesLoaded, urlBranch, go])

  // How many tags and releases, for the summary under the name.
  useEffect(() => {
    let alive = true
    Promise.all([
      api.listTags(repo.id).then((r) => (r.tags || []).length).catch(() => null),
      api.listReleases(repo.id).then((r) => (r.releases || []).length).catch(() => null),
    ]).then(([tags, releases]) => { if (alive) setCounts({ tags, releases }) })
    return () => { alive = false }
  }, [repo.id, tab])

  // One request per branch brings the whole tree *with* content, so opening a
  // file is instant and needs no further round trip.
  useEffect(() => {
    if (!branch && !branchesLoaded) return undefined
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
  }, [repo.id, branch, branchesLoaded])

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

  useEffect(() => {
    if (tab !== 'graph' || graph) return undefined
    let alive = true
    setGraphLoading(true)
    setGraphError('')
    const load = async () => {
      try {
        // No branch filter: the point of the graph is the branches next to each other.
        const response = await api.getCommitGraph(repo.id, { limit: 300 })
        if (alive) setGraph(response)
      } catch (err) {
        if (alive) setGraphError(err.message || 'Could not load the commit graph')
      } finally {
        if (alive) setGraphLoading(false)
      }
    }
    load()
    return () => { alive = false }
  }, [tab, repo.id, graph])

  // A different branch has different history.
  useEffect(() => {
    setCommits([])
  }, [branch])

  // Whatever opened the file -- a click, a link, Back -- starts it fresh, with its
  // folders unfolded so the tree shows where it is.
  useEffect(() => {
    setFileMode('code')
    setBlame(null)
    setDiffRows(null)
    setDiffStats(null)
    setPaneError('')
    setPaneNote('')
    if (selectedFile) {
      setExpanded((prev) => new Set([...prev, ...ancestorsOf(normalizePath(selectedFile))]))
    }
  }, [selectedFile, branch])

  const paths = useMemo(() => Object.keys(files).sort(), [files])
  const currentFile = selectedFile ? files[selectedFile] : null
  const missingFile = Boolean(selectedFile && !filesLoading && !filesError && !currentFile)

  const openFile = useCallback((path) => {
    go({ path, line: null, tab: null, commit: null })
  }, [go])

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
          <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted" data-testid="repo-summary">
            {repo.owner ? <span>{repo.owner} ·</span> : null}
            <span>{filesLoading ? '…' : paths.length} files ·</span>
            <button type="button" className="hover:text-ink hover:underline" onClick={() => setTab('branches')}>
              {branches.length} {branches.length === 1 ? 'branch' : 'branches'}
            </button>
            {counts.tags !== null && (
              <>
                <span>·</span>
                <button type="button" className="hover:text-ink hover:underline" onClick={() => setTab('tags')}>
                  {counts.tags} {counts.tags === 1 ? 'tag' : 'tags'}
                </button>
              </>
            )}
            {counts.releases !== null && (
              <>
                <span>·</span>
                <button type="button" className="hover:text-ink hover:underline" onClick={() => setTab('releases')}>
                  {counts.releases} {counts.releases === 1 ? 'release' : 'releases'}
                </button>
              </>
            )}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <FiGitBranch className="h-4 w-4 text-muted" />
          <select
            value={branch || ''}
            onChange={(e) => go({ branch: e.target.value, path: null, line: null, commit: null })}
            aria-label="Branch"
            className="rounded-xl border border-border bg-cream-mid px-3 py-1.5 text-sm text-ink outline-none focus:border-ink"
          >
            {branches.map((b) => (
              <option key={b.name || b} value={b.name || b}>
                {(b.name || b)}{b.is_default ? ' (default)' : ''}
              </option>
            ))}
          </select>
          {branch && (
            <button
              type="button"
              title={`Download the files on ${branch} as a zip`}
              onClick={() => downloadAuthed(api.sourceZipUrl(repo.id, branch),
                `${repo.name}-${branch.replace(/[^A-Za-z0-9._-]+/g, '-')}.zip`).catch(() => {})}
              className="flex items-center gap-1.5 rounded-xl border border-border bg-white/[0.04] px-3 py-1.5 text-sm text-ink-soft transition hover:border-border-strong hover:text-ink"
            >
              <FiDownload className="h-4 w-4" /> ZIP
            </button>
          )}
        </div>
      </div>

      <div className="flex gap-1 overflow-x-auto border-b border-border" role="tablist">
        {TABS.map((entry) => {
          const Icon = entry.icon
          const active = tab === entry.id || (entry.also || []).includes(tab)
          return (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => setTab(entry.id)}
            className={cn(
              'flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm transition',
              active
                ? 'border-accent text-ink'
                : 'border-transparent text-muted hover:text-ink'
            )}
          >
            <Icon className="h-4 w-4" /> {entry.label}
          </button>
          )
        })}
      </div>

      {openCommitId ? (
        <CommitDetail
          repo={repo}
          commitId={openCommitId}
          onBack={() => setOpenCommitId(null)}
          onOpenCommit={(id) => setOpenCommitId(id)}
        />
      ) : (
      <>

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
            ) : missingFile ? (
              <div className="px-4 py-20 text-center" data-testid="file-missing">
                <FiAlertCircle className="mx-auto mb-3 h-7 w-7 text-warning-fg" />
                <p className="text-sm text-muted">
                  <span className="font-mono text-ink">{selectedFile}</span> is not on {branch ? `'${branch}'` : 'this branch'}.
                </p>
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
                      highlightLine={line}
                      onLineClick={(number) => go({ line: number }, { replace: true })}
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
                <li
                  key={commit.id}
                  onClick={() => setOpenCommitId(commit.id)}
                  className="cursor-pointer rounded-xl border border-border bg-white/[0.03] p-3 transition hover:border-border-strong hover:bg-white/[0.06]"
                >
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

      {tab === 'graph' && (
        <GlassCard className="p-4" hover={false}>
          {graphLoading ? (
            <p className="flex items-center justify-center gap-2 py-12 text-sm text-muted">
              <FiLoader className="h-4 w-4 animate-spin" /> Building the graph…
            </p>
          ) : graphError ? (
            <p className="flex items-start gap-2 py-8 text-sm text-danger-fg">
              <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {graphError}
            </p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-baseline gap-2">
                <h3 className="text-sm font-semibold text-ink">Commit graph</h3>
                <span className="text-[11.5px] text-muted">
                  {graph?.returned ?? 0} of {graph?.total ?? 0} commits across{' '}
                  {(graph?.branches || []).length} branch(es)
                </span>
                {graph?.truncated && (
                  <span className="text-[11.5px] text-warning-fg">
                    — showing the most recent {graph?.limit}; older history is not drawn
                  </span>
                )}
              </div>
              <CommitGraph
                commits={graph?.commits || []}
                selectedCommitId={selectedGraphCommit?.id}
                onSelectCommit={setSelectedGraphCommit}
              />
              {selectedGraphCommit && (
                <div className="mt-4 rounded-xl border border-border bg-white/[0.03] p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[11px] text-accent">
                      {selectedGraphCommit.short_id}
                    </span>
                    {selectedGraphCommit.is_merge && <Badge variant="info">merge</Badge>}
                    <span className="text-xs text-muted">
                      {selectedGraphCommit.author} · {selectedGraphCommit.timestamp}
                    </span>
                    <button
                      type="button"
                      onClick={() => setOpenCommitId(selectedGraphCommit.id)}
                      className="ml-auto rounded-lg border border-accent/40 bg-accent/10 px-2 py-0.5 text-[11px] text-ink hover:bg-accent/20"
                    >
                      View changes
                    </button>
                    <button
                      type="button"
                      onClick={() => setSelectedGraphCommit(null)}
                      className="text-[11px] text-muted hover:text-ink"
                    >
                      close
                    </button>
                  </div>
                  <p className="mt-1 whitespace-pre-wrap break-words text-[13px] text-ink-soft">
                    {selectedGraphCommit.message}
                  </p>
                  {selectedGraphCommit.parents?.length > 0 && (
                    <p className="mt-2 font-mono text-[10.5px] text-muted">
                      parents: {selectedGraphCommit.parents.map((p) => p.slice(0, 8)).join(', ')}
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </GlassCard>
      )}

      {tab === 'protection' && <BranchProtectionPanel repo={repo} />}

      {tab === 'activity' && <ActivityBrowser repoId={repo.id} />}

      {tab === 'branches' && (
        <BranchesTab
          repo={repo}
          onOpenBranch={(name) => go({ branch: name, tab: null, path: null, line: null, commit: null })}
          onOpenCommit={(id) => setOpenCommitId(id)}
          onChanged={() => setBranchesVersion((v) => v + 1)}
        />
      )}

      {(tab === 'releases' || tab === 'tags') && (
        <ReleasesTab
          repo={repo}
          view={tab}
          onViewChange={(view) => setTab(view)}
          onOpenCommit={(id) => setOpenCommitId(id)}
          branches={branches}
          defaultBranch={defaultBranch}
        />
      )}

      {tab === 'docs' && (
        <DocsTab
          repo={repo}
          docPath={docPath}
          onOpenDoc={(path) => go({ doc: path })}
          onOpenCommit={(id) => setOpenCommitId(id)}
        />
      )}

      </>
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
