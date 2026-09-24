import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  FiSearch,
  FiFolder,
  FiGitCommit,
  FiCode,
  FiAlertTriangle,
  FiChevronRight,
  FiChevronDown,
} from 'react-icons/fi'
import PageHeader from '../components/ui/PageHeader'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import Badge from '../components/ui/Badge'
import EmptyState from '../components/ui/EmptyState'
import api from '../utils/api'
import { cn } from '../lib/utils'

/**
 * Search across repositories, commits and code.
 *
 * Three modes rather than one blended result list, because the useful filters
 * differ completely: an owner filter means nothing for code, and a branch means
 * nothing for repositories. Mixing them would produce a form where most fields
 * are inert most of the time.
 */

const MODES = [
  { id: 'repositories', label: 'Repositories', icon: FiFolder, placeholder: 'Name, description or owner…' },
  { id: 'commits', label: 'Commits', icon: FiGitCommit, placeholder: 'Words from a commit message…' },
  { id: 'code', label: 'Code', icon: FiCode, placeholder: 'A string to find in the files…' },
]

/** Highlight the matched span without using dangerouslySetInnerHTML. */
function MatchLine({ text, start, end }) {
  if (start == null || end == null || end <= start) {
    return <span className="whitespace-pre">{text}</span>
  }
  return (
    <span className="whitespace-pre">
      {text.slice(0, start)}
      <mark className="rounded-sm bg-accent/25 px-0.5 text-ink">{text.slice(start, end)}</mark>
      {text.slice(end)}
    </span>
  )
}

function CodeFileResult({ file }) {
  const [open, setOpen] = useState(true)
  return (
    <GlassCard hover={false} className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left transition-colors hover:bg-white/[0.03]"
      >
        {open ? (
          <FiChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
        ) : (
          <FiChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />
        )}
        <FiCode className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate font-mono text-[13px] text-ink">
          {file.path}
        </span>
        <Badge variant="default">{file.match_count}</Badge>
      </button>

      {open && (
        <div className="border-t border-border">
          {file.matches.map((m, i) => (
            <div
              key={i}
              className="flex gap-3 overflow-x-auto border-b border-border/50 px-4 py-1.5 last:border-0 hover:bg-white/[0.02]"
            >
              <span className="w-12 shrink-0 select-none text-right font-mono text-[11px] text-muted">
                {m.line}
              </span>
              <code className="font-mono text-[12px] leading-relaxed text-ink-soft">
                <MatchLine text={m.text} start={m.start} end={m.end} />
              </code>
            </div>
          ))}
        </div>
      )}
    </GlassCard>
  )
}

export default function Search() {
  const [mode, setMode] = useState('repositories')
  const [query, setQuery] = useState('')
  const [owner, setOwner] = useState('')
  const [repoId, setRepoId] = useState('')
  const [branch, setBranch] = useState('')
  const [pathFilter, setPathFilter] = useState('')
  const [regex, setRegex] = useState(false)
  const [caseSensitive, setCaseSensitive] = useState(false)

  const [repos, setRepos] = useState([])
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [ran, setRan] = useState(false)
  const inputRef = useRef(null)

  useEffect(() => {
    api
      .listRepositories()
      .then((r) => setRepos(r.repositories || []))
      .catch(() => setRepos([]))
  }, [])

  useEffect(() => {
    inputRef.current?.focus()
  }, [mode])

  const run = useCallback(async () => {
    // Code search needs a repository; the others are happy with filters alone.
    if (mode === 'code' && (!repoId || !query.trim())) {
      setError('Code search needs a repository and something to look for.')
      return
    }
    if (mode === 'repositories' && !query.trim() && !owner.trim()) {
      setError('Enter something to search for, or filter by owner.')
      return
    }
    if (mode === 'commits' && !query.trim() && !owner.trim()) {
      setError('Enter words from a commit message, or filter by author.')
      return
    }

    try {
      setLoading(true)
      setError(null)
      let response
      if (mode === 'repositories') {
        response = await api.searchRepositories({ q: query.trim(), owner: owner.trim() || null })
      } else if (mode === 'commits') {
        response = await api.searchCommits({
          q: query.trim(),
          repositoryId: repoId || null,
          author: owner.trim() || null,
        })
      } else {
        response = await api.searchCode({
          repositoryId: repoId,
          q: query.trim(),
          branch: branch.trim() || null,
          path: pathFilter.trim() || null,
          regex,
          caseSensitive,
        })
      }
      setResult(response)
      setRan(true)
    } catch (err) {
      setError(err.message || 'Search failed')
      setResult(null)
      setRan(true)
    } finally {
      setLoading(false)
    }
  }, [mode, query, owner, repoId, branch, pathFilter, regex, caseSensitive])

  const onKeyDown = (e) => {
    if (e.key === 'Enter') run()
  }

  const active = MODES.find((m) => m.id === mode)

  const switchMode = (id) => {
    setMode(id)
    setResult(null)
    setRan(false)
    setError(null)
  }

  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Find things"
        title="Search"
        subtitle="Look across repositories, commit messages and the contents of tracked files."
      />

      <div className="mb-5 flex flex-wrap gap-2">
        {MODES.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => switchMode(id)}
            className={cn(
              'flex items-center gap-2 rounded-full border px-4 py-1.5 text-sm transition-colors',
              mode === id
                ? 'border-accent/50 bg-accent/10 text-ink'
                : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink'
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      <GlassCard hover={false} className="mb-6 space-y-3 p-4">
        <div className="flex flex-wrap gap-2">
          <div className="relative min-w-0 flex-1">
            <FiSearch className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={active.placeholder}
              className="w-full rounded-lg border border-border bg-cream-mid py-2 pl-9 pr-3 text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
            />
          </div>
          <Button onClick={run} disabled={loading} className="shrink-0">
            {loading ? 'Searching…' : 'Search'}
          </Button>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {(mode === 'repositories' || mode === 'commits') && (
            <input
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={mode === 'repositories' ? 'Owner (optional)' : 'Author (optional)'}
              className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none"
            />
          )}

          {(mode === 'commits' || mode === 'code') && (
            <select
              value={repoId}
              onChange={(e) => setRepoId(e.target.value)}
              className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none"
            >
              <option value="">
                {mode === 'code' ? '— pick a repository —' : 'All repositories'}
              </option>
              {repos.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          )}

          {mode === 'code' && (
            <>
              <input
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Branch (default)"
                className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none"
              />
              <input
                value={pathFilter}
                onChange={(e) => setPathFilter(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Path contains…"
                className="rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none"
              />
            </>
          )}
        </div>

        {mode === 'code' && (
          <div className="flex flex-wrap gap-4 pt-1">
            <label className="flex cursor-pointer items-center gap-2 text-[13px] text-ink-soft">
              <input
                type="checkbox"
                checked={regex}
                onChange={(e) => setRegex(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#3b9dff]"
              />
              Regular expression
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-[13px] text-ink-soft">
              <input
                type="checkbox"
                checked={caseSensitive}
                onChange={(e) => setCaseSensitive(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#3b9dff]"
              />
              Match case
            </label>
          </div>
        )}
      </GlassCard>

      {error && (
        <GlassCard className="mb-5 flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-14 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      )}

      {!loading && ran && result && (
        <>
          {/* --- repositories --- */}
          {mode === 'repositories' && (
            result.repositories?.length ? (
              <div className="space-y-2">
                <p className="mb-2 text-xs text-muted">
                  {result.total} repositor{result.total === 1 ? 'y' : 'ies'}
                  {result.truncated && ' (showing the first page)'}
                </p>
                {result.repositories.map((r) => (
                  <GlassCard key={r.id} hover={false} className="flex flex-wrap items-center gap-3 p-4">
                    <FiFolder className="h-4 w-4 shrink-0 text-accent" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-medium text-ink">{r.name}</span>
                        {r.is_archived && <Badge variant="warning">Archived</Badge>}
                      </div>
                      <p className="mt-0.5 truncate text-xs text-muted">
                        {r.description || 'No description'} · owned by {r.owner || 'unknown'}
                      </p>
                    </div>
                    <span className="ref shrink-0">{r.id}</span>
                  </GlassCard>
                ))}
              </div>
            ) : (
              <EmptyState icon={FiFolder} title="No repositories matched" description="Try a shorter term, or clear the owner filter." />
            )
          )}

          {/* --- commits --- */}
          {mode === 'commits' && (
            result.commits?.length ? (
              <div className="space-y-2">
                <p className="mb-2 text-xs text-muted">
                  {result.total} commit{result.total === 1 ? '' : 's'}
                  {result.truncated && ' (showing the first page)'}
                </p>
                {result.commits.map((c) => (
                  <GlassCard key={c.id} hover={false} className="flex flex-wrap items-start gap-3 p-4">
                    <FiGitCommit className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                    <div className="min-w-0 flex-1">
                      <p className="break-words text-[13px] text-ink">{c.message}</p>
                      <p className="mt-1 flex flex-wrap items-center gap-x-3 font-mono text-[11px] text-muted">
                        <span className="ref">{c.id.slice(0, 12)}</span>
                        <span>{c.repository_name}</span>
                        <span>{c.author}</span>
                        {c.created_at && <span>{new Date(c.created_at).toLocaleString()}</span>}
                      </p>
                    </div>
                  </GlassCard>
                ))}
              </div>
            ) : (
              <EmptyState icon={FiGitCommit} title="No commits matched" description="Commit search looks at messages only." />
            )
          )}

          {/* --- code --- */}
          {mode === 'code' && (
            result.results?.length ? (
              <div className="space-y-2">
                <p className="mb-2 text-xs text-muted">
                  {result.total_matches} match{result.total_matches === 1 ? '' : 'es'} in{' '}
                  {result.results.length} file{result.results.length === 1 ? '' : 's'} ·{' '}
                  {result.files_scanned} scanned
                  {result.files_skipped > 0 && `, ${result.files_skipped} skipped (binary or large)`}
                  {result.truncated && ' · truncated'}
                </p>
                {result.results.map((f) => (
                  <CodeFileResult key={f.path} file={f} />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={FiCode}
                title="Nothing found in the files"
                description={
                  result.commit_id
                    ? `Scanned ${result.files_scanned} file(s) at ${String(result.commit_id).slice(0, 12)}.`
                    : 'That branch has no commits yet.'
                }
              />
            )
          )}
        </>
      )}

      {!loading && !ran && (
        <EmptyState
          icon={FiSearch}
          title="Search across everything you can read"
          description="Repositories and commits search the whole server. Code search runs against one repository at a branch tip."
        />
      )}
    </div>
  )
}
