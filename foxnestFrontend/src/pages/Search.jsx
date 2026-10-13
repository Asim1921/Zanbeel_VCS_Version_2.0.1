import React, { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  FiSearch,
  FiFolder,
  FiGitCommit,
  FiCode,
  FiFile,
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
import { commitLink, fileLink, repositoryLink } from '../lib/links'
import { cn } from '../lib/utils'

/**
 * Search across code, commits, repositories and file paths.
 *
 * Four modes rather than one blended list, because the useful filters differ: an
 * owner means nothing for code, a branch nothing for repositories.
 *
 * The search is held in the address -- ?type=code&q=... -- so it survives a refresh,
 * Back returns to it from a result, and it can be sent to someone. Every result opens
 * what it found: the repository, the commit, or the file at the matching line.
 */

const MODES = [
  { id: 'code', label: 'Code', icon: FiCode, placeholder: 'A string to find in the files…' },
  { id: 'commits', label: 'Commits', icon: FiGitCommit, placeholder: 'Words from a commit message, or a commit id…' },
  { id: 'repositories', label: 'Repositories', icon: FiFolder, placeholder: 'Name, description or owner…' },
  { id: 'files', label: 'Files', icon: FiFile, placeholder: 'Part of a path, e.g. server routes…' },
]

const inputClass =
  'rounded-lg border border-border bg-cream-mid px-3 py-1.5 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none'

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

function CodeFileResult({ file, branch, everywhere }) {
  const [open, setOpen] = useState(true)
  return (
    <GlassCard hover={false} className="overflow-hidden" data-testid="code-result">
      <div className="flex w-full items-center gap-2 px-4 py-2.5">
        <button type="button" onClick={() => setOpen((v) => !v)} className="text-muted hover:text-ink"
                aria-label={open ? 'Collapse' : 'Expand'}>
          {open ? <FiChevronDown className="h-3.5 w-3.5" /> : <FiChevronRight className="h-3.5 w-3.5" />}
        </button>
        <FiCode className="h-3.5 w-3.5 shrink-0 text-accent" />
        {everywhere && file.repository_name && (
          <Link to={repositoryLink(file.repository_id)} className="shrink-0 text-[13px] text-ink-soft hover:underline">
            {file.repository_name}
          </Link>
        )}
        <Link
          to={fileLink(file.repository_id, file.path, { branch })}
          className="min-w-0 flex-1 truncate font-mono text-[13px] text-ink hover:underline"
        >
          {file.path}
        </Link>
        <Badge variant="default">{file.match_count}</Badge>
      </div>

      {open && (
        <div className="border-t border-border">
          {file.matches.map((m, i) => (
            <Link
              key={i}
              to={fileLink(file.repository_id, file.path, { branch, line: m.line })}
              data-testid="code-match"
              className="flex gap-3 overflow-x-auto border-b border-border/50 px-4 py-1.5 last:border-0 hover:bg-white/[0.04]"
            >
              <span className="w-12 shrink-0 select-none text-right font-mono text-[11px] text-muted">{m.line}</span>
              <code className="font-mono text-[12px] leading-relaxed text-ink-soft">
                <MatchLine text={m.text} start={m.start} end={m.end} />
              </code>
            </Link>
          ))}
        </div>
      )}
    </GlassCard>
  )
}

const when = (iso) => api.parseServerTimestamp(iso)?.toLocaleString() || ''

export default function Search() {
  const [params, setParams] = useSearchParams()
  const mode = MODES.some((m) => m.id === params.get('type')) ? params.get('type') : 'code'
  const asked = {
    q: params.get('q') || '',
    owner: params.get('owner') || '',
    repo: params.get('repo') || '',
    branch: params.get('branch') || '',
    path: params.get('path') || '',
    regex: params.get('regex') === '1',
    caseSensitive: params.get('case') === '1',
  }

  // The form edits a draft; Search puts it in the address, which runs it.
  const [draft, setDraft] = useState(asked)
  const [repos, setRepos] = useState([])
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const inputRef = useRef(null)
  const key = params.toString()

  useEffect(() => {
    api.listRepositories().then((r) => setRepos(r.repositories || [])).catch(() => setRepos([]))
  }, [])

  useEffect(() => {
    setDraft(asked)
    inputRef.current?.focus()
    // Only when the address changes (a search, Back, a link), not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  useEffect(() => {
    let alive = true
    const ready =
      (mode === 'repositories' && (asked.q || asked.owner)) ||
      (mode === 'commits' && (asked.q || asked.owner)) ||
      ((mode === 'code' || mode === 'files') && asked.q)
    if (!ready) {
      setResult(null)
      setError(null)
      return undefined
    }
    const run = async () => {
      try {
        setLoading(true)
        setError(null)
        let response
        if (mode === 'repositories') {
          response = await api.searchRepositories({ q: asked.q, owner: asked.owner || null })
        } else if (mode === 'commits') {
          response = await api.searchCommits({ q: asked.q, repositoryId: asked.repo || null, author: asked.owner || null })
        } else if (mode === 'files') {
          response = await api.searchPaths({ q: asked.q, repositoryId: asked.repo || null, branch: asked.branch || null })
        } else {
          response = await api.searchCode({
            repositoryId: asked.repo || null, q: asked.q, branch: asked.branch || null,
            path: asked.path || null, regex: asked.regex, caseSensitive: asked.caseSensitive,
          })
        }
        if (alive) setResult(response)
      } catch (err) {
        if (alive) {
          setError(err.message || 'Search failed')
          setResult(null)
        }
      } finally {
        if (alive) setLoading(false)
      }
    }
    run()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const submit = (next = draft, nextMode = mode) => {
    if (nextMode === 'code' && next.regex && !next.repo) {
      setError('A regular expression searches one repository at a time: choose a repository.')
      return
    }
    const p = new URLSearchParams()
    p.set('type', nextMode)
    if (next.q.trim()) p.set('q', next.q.trim())
    if (next.owner.trim() && (nextMode === 'repositories' || nextMode === 'commits')) p.set('owner', next.owner.trim())
    if (next.repo && nextMode !== 'repositories') p.set('repo', next.repo)
    if (next.branch.trim() && next.repo && (nextMode === 'code' || nextMode === 'files')) p.set('branch', next.branch.trim())
    if (next.path.trim() && nextMode === 'code') p.set('path', next.path.trim())
    if (next.regex && nextMode === 'code') p.set('regex', '1')
    if (next.caseSensitive && nextMode === 'code') p.set('case', '1')
    setParams(p)
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter') submit()
  }

  const active = MODES.find((m) => m.id === mode)
  const set = (field) => (e) => setDraft((d) => ({ ...d, [field]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const everywhere = !asked.repo
  const ran = Boolean(result || error)

  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Find things"
        title="Search"
        subtitle="Look across the code, commit history, repositories and file paths you can read."
      />

      <div className="mb-5 flex flex-wrap gap-2" role="tablist">
        {MODES.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={mode === id}
            onClick={() => submit({ ...draft, regex: id === 'code' ? draft.regex : false }, id)}
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
              aria-label="Search for"
              value={draft.q}
              onChange={set('q')}
              onKeyDown={onKeyDown}
              placeholder={active.placeholder}
              className="w-full rounded-lg border border-border bg-cream-mid py-2 pl-9 pr-3 text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
            />
          </div>
          <Button onClick={() => submit()} disabled={loading} className="shrink-0">
            {loading ? 'Searching…' : 'Search'}
          </Button>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {(mode === 'repositories' || mode === 'commits') && (
            <input
              aria-label={mode === 'repositories' ? 'Owner' : 'Author'}
              value={draft.owner}
              onChange={set('owner')}
              onKeyDown={onKeyDown}
              placeholder={mode === 'repositories' ? 'Owner (optional)' : 'Author (optional)'}
              className={inputClass}
            />
          )}

          {mode !== 'repositories' && (
            <select aria-label="Repository" value={draft.repo} onChange={set('repo')} className={inputClass}>
              <option value="">All repositories</option>
              {repos.map((r) => (
                <option key={r.id} value={r.id}>{r.name}</option>
              ))}
            </select>
          )}

          {(mode === 'code' || mode === 'files') && draft.repo && (
            <input aria-label="Branch" value={draft.branch} onChange={set('branch')} onKeyDown={onKeyDown}
                   placeholder="Branch (default)" className={inputClass} />
          )}
          {mode === 'code' && (
            <input aria-label="Path contains" value={draft.path} onChange={set('path')} onKeyDown={onKeyDown}
                   placeholder="Path contains…" className={inputClass} />
          )}
        </div>

        {mode === 'code' && (
          <div className="flex flex-wrap items-center gap-4 pt-1">
            <label className={cn('flex items-center gap-2 text-[13px]', draft.repo ? 'cursor-pointer text-ink-soft' : 'text-muted')}
                   title={draft.repo ? '' : 'Choose a repository to search with a regular expression'}>
              <input type="checkbox" checked={draft.regex} onChange={set('regex')} disabled={!draft.repo}
                     className="h-3.5 w-3.5 accent-[#3b9dff]" />
              Regular expression
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-[13px] text-ink-soft">
              <input type="checkbox" checked={draft.caseSensitive} onChange={set('caseSensitive')}
                     className="h-3.5 w-3.5 accent-[#3b9dff]" />
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

      {!loading && result && (
        <div data-testid="search-results">
          {mode === 'repositories' && (
            result.repositories?.length ? (
              <div className="space-y-2">
                <p className="mb-2 text-xs text-muted">
                  {result.total} repositor{result.total === 1 ? 'y' : 'ies'}
                  {result.truncated && ' (showing the first page)'}
                </p>
                {result.repositories.map((r) => (
                  <Link key={r.id} to={repositoryLink(r.id)} className="block">
                    <GlassCard className="flex flex-wrap items-center gap-3 p-4">
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
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState icon={FiFolder} title="No repositories matched" description="Try a shorter term, or clear the owner filter." />
            )
          )}

          {mode === 'commits' && (
            result.commits?.length ? (
              <div className="space-y-2">
                <p className="mb-2 text-xs text-muted">
                  {result.total} commit{result.total === 1 ? '' : 's'}
                  {result.truncated && ` (showing the newest ${result.commits.length})`}
                </p>
                {result.commits.map((c) => (
                  <Link key={c.id} to={commitLink(c.repository_id, c.id)} className="block" data-testid="commit-result">
                    <GlassCard className="flex flex-wrap items-start gap-3 p-4">
                      <FiGitCommit className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                      <div className="min-w-0 flex-1">
                        <p className="break-words text-[13px] text-ink">{String(c.message || '').split('\n')[0]}</p>
                        <p className="mt-1 flex flex-wrap items-center gap-x-3 font-mono text-[11px] text-muted">
                          <span className="ref">{c.id.slice(0, 12)}</span>
                          <span>{c.repository_name}</span>
                          <span>{c.author}</span>
                          {c.created_at && <span>{when(c.created_at)}</span>}
                        </p>
                      </div>
                    </GlassCard>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState icon={FiGitCommit} title="No commits matched" description="Commit search looks at messages, and at ids from 7 characters." />
            )
          )}

          {mode === 'files' && (
            result.files?.length ? (
              <div className="space-y-1">
                <p className="mb-2 text-xs text-muted">
                  {result.total} file{result.total === 1 ? '' : 's'}{result.truncated && ' (showing the best matches)'}
                </p>
                {result.files.map((f) => (
                  <Link key={`${f.repository_id}:${f.path}`} to={fileLink(f.repository_id, f.path, { branch: f.branch })}
                        data-testid="file-result"
                        className="flex items-center gap-2 rounded-lg px-3 py-2 hover:bg-white/[0.04]">
                    <FiFile className="h-3.5 w-3.5 shrink-0 text-accent" />
                    {everywhere && <span className="shrink-0 text-[13px] text-ink-soft">{f.repository_name}</span>}
                    <span className="min-w-0 truncate font-mono text-[13px] text-ink">{f.path}</span>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState icon={FiFile} title="No files matched" description="Every word must appear somewhere in the path." />
            )
          )}

          {mode === 'code' && (
            <>
              {(result.repositories_not_indexed || []).length > 0 && (
                <p className="mb-2 text-xs text-warning-fg">
                  Still being indexed, so not searched yet: {result.repositories_not_indexed.join(', ')}.
                </p>
              )}
              {result.results?.length ? (
                <div className="space-y-2">
                  <p className="mb-2 text-xs text-muted">
                    {result.total_matches} match{result.total_matches === 1 ? '' : 'es'} in{' '}
                    {result.results.length} file{result.results.length === 1 ? '' : 's'}
                    {everywhere ? ` across ${result.repositories_searched} repositor${result.repositories_searched === 1 ? 'y' : 'ies'}` : ''}
                    {result.files_skipped > 0 && `, ${result.files_skipped} skipped (binary or large)`}
                    {result.timed_out ? ' · stopped at the time limit — narrow the search' : result.truncated ? ' · more not shown' : ''}
                  </p>
                  {result.results.map((f) => (
                    <CodeFileResult
                      key={`${f.repository_id || result.repository_id}:${f.path}`}
                      file={{ ...f, repository_id: f.repository_id || result.repository_id }}
                      branch={asked.repo ? asked.branch || null : null}
                      everywhere={everywhere}
                    />
                  ))}
                </div>
              ) : (
                <EmptyState
                  icon={FiCode}
                  title="Nothing found in the files"
                  description={
                    result.timed_out
                      ? 'The search stopped at its time limit before finding anything. Try a simpler pattern or a path filter.'
                      : everywhere
                        ? `Searched the default branch of ${result.repositories_searched} repositor${result.repositories_searched === 1 ? 'y' : 'ies'}.`
                        : result.commit_id
                          ? `Scanned ${result.files_scanned} file(s) at ${String(result.commit_id).slice(0, 12)}.`
                          : 'That branch has no commits yet.'
                  }
                />
              )}
            </>
          )}
        </div>
      )}

      {!loading && !ran && (
        <EmptyState
          icon={FiSearch}
          title="Search across everything you can read"
          description="Code search covers the default branch of every repository; pick one to search a branch or with a regular expression."
        />
      )}
    </div>
  )
}
