import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  FiTag, FiPackage, FiPlus, FiTrash2, FiRefreshCw, FiAlertTriangle, FiCheckCircle,
  FiDownload, FiEdit3, FiZap,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import Markdown from '../Markdown'
import api from '../../utils/api'
import { downloadAuthed } from '../../utils/authedDownload'
import { absoluteTime, relativeTime } from '../../lib/time'
import { cn } from '../../lib/utils'

/**
 * Releases and the tags under them.
 *
 * One tab with two views, because a release is a tag with notes: kept apart, people
 * create a release whose tag they cannot find, or delete a tag without seeing the
 * release built on it (the server now refuses that, and the tag row says why).
 *
 * Notes are Markdown, drafted on request from the pull requests and commits since the
 * previous release, and can be corrected afterwards. Every release and tag downloads
 * as a zip of the files it names.
 */

const fieldClass =
  'w-full rounded-xl border border-border bg-cream-mid px-3 py-2.5 text-sm text-ink outline-none focus:border-accent/70'

function safeName(text) {
  return String(text || 'source').replace(/[^A-Za-z0-9._-]+/g, '-')
}

function NotesEditor({ value, onChange, onDraft, drafting }) {
  const [mode, setMode] = useState('write')
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1">
        <span className="mr-auto text-[13px] font-medium text-ink-soft">Notes</span>
        {onDraft && (
          <Button variant="ghost" size="sm" onClick={onDraft} loading={drafting} title="Draft notes from the pull requests and commits since the previous release">
            <FiZap className="h-3.5 w-3.5" /> Generate notes
          </Button>
        )}
        {['write', 'preview'].map((m) => (
          <button key={m} type="button" onClick={() => setMode(m)}
            className={cn('rounded-lg border px-2.5 py-1 text-xs capitalize',
              mode === m ? 'border-accent/60 bg-accent/15 text-ink' : 'border-border text-ink-soft hover:text-ink')}>
            {m}
          </button>
        ))}
      </div>
      {mode === 'write' ? (
        <textarea rows={9} value={value} onChange={(e) => onChange(e.target.value)}
          placeholder="What changed in this version. Markdown is supported."
          aria-label="Release notes"
          className={cn(fieldClass, 'resize-y font-mono text-[12.5px] leading-relaxed')} />
      ) : (
        <div className="min-h-[12rem] rounded-xl border border-border bg-cream-mid px-4 py-3" data-testid="notes-preview">
          {value.trim() ? <Markdown>{value}</Markdown> : <p className="text-sm text-muted">Nothing to preview.</p>}
        </div>
      )}
    </div>
  )
}

function TargetPicker({ branches, value, onChange, label = 'Target' }) {
  const isBranch = branches.includes(value)
  return (
    <div className="grid grid-cols-[1fr_1fr] gap-2">
      <label className="space-y-1.5">
        <span className="text-[13px] font-medium text-ink-soft">{label}</span>
        <select className={fieldClass} value={isBranch ? value : '__commit__'}
          onChange={(e) => onChange(e.target.value === '__commit__' ? '' : e.target.value)}>
          {branches.map((b) => <option key={b} value={b}>{b}</option>)}
          <option value="__commit__">A commit…</option>
        </select>
      </label>
      {!isBranch && (
        <label className="space-y-1.5">
          <span className="text-[13px] font-medium text-ink-soft">Commit id</span>
          <input className={fieldClass} value={value} placeholder="7+ characters"
            onChange={(e) => onChange(e.target.value)} />
        </label>
      )}
    </div>
  )
}

export default function ReleasesTab({ repo, view = 'releases', onViewChange, onOpenCommit, branches = [], defaultBranch }) {
  const repoId = repo.id
  const [releases, setReleases] = useState([])
  const [tags, setTags] = useState([])
  const [permissions, setPermissions] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [drafting, setDrafting] = useState(false)

  const [newRelease, setNewRelease] = useState(null)
  const [editing, setEditing] = useState(null)
  const [withdrawing, setWithdrawing] = useState(null)
  const [newTag, setNewTag] = useState(null)
  const [deletingTag, setDeletingTag] = useState(null)

  const branchNames = useMemo(() => branches.map((b) => b.name || b), [branches])

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const [r, t] = await Promise.all([api.listReleases(repoId), api.listTags(repoId)])
      setReleases(r.releases || [])
      setTags(t.tags || [])
      setPermissions(r.permissions || t.permissions || {})
    } catch (err) {
      setError(err.message || 'Could not load releases and tags')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => { load() }, [load])

  const canWrite = Boolean(permissions.can_write)
  const canManage = Boolean(permissions.can_manage)

  const run = async (fn, success) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      setNotice(success)
      await load()
      return true
    } catch (err) {
      setError(err.message || 'That did not work')
      return false
    } finally {
      setBusy(false)
    }
  }

  const download = async (ref, label) => {
    try {
      setError(null)
      await downloadAuthed(api.sourceZipUrl(repoId, ref), `${safeName(repo.name)}-${safeName(label || ref)}.zip`)
    } catch (err) {
      setError(err.message || 'Download failed')
    }
  }

  // --- releases --------------------------------------------------------------------

  const tagsWithoutRelease = tags.filter((t) => !t.release)
  const releaseTagName = (draft) => (draft.tag.trim() || (draft.version.trim()
    ? (draft.version.trim().startsWith('v') ? draft.version.trim() : `v${draft.version.trim()}`) : ''))
  const tagExists = (name) => tags.some((t) => t.name === name)

  const draftNotes = async (draft, setDraft) => {
    const tagName = releaseTagName(draft)
    const target = tagExists(tagName) ? tagName : (draft.target || defaultBranch)
    if (draft.notes.trim() && !window.confirm('Replace the notes you have written with a draft?')) return
    try {
      setDrafting(true)
      const response = await api.draftReleaseNotes(repoId, { target })
      setDraft((d) => ({ ...d, notes: response.notes || '' }))
    } catch (err) {
      setError(err.message || 'Could not draft notes')
    } finally {
      setDrafting(false)
    }
  }

  const publish = async () => {
    const draft = newRelease
    const tagName = releaseTagName(draft)
    const ok = await run(() => api.createRelease(repoId, {
      version: draft.version.trim(),
      tag: draft.tag.trim() || null,
      target: tagExists(tagName) ? null : (draft.target || null),
      title: draft.title.trim() || null,
      notes: draft.notes.trim() || null,
    }), `Published ${draft.version.trim()}`)
    if (ok) setNewRelease(null)
  }

  const saveEdit = async () => {
    const ok = await run(() => api.updateRelease(repoId, editing.version, {
      title: editing.title, notes: editing.notes,
    }), `Updated ${editing.version}`)
    if (ok) setEditing(null)
  }

  const withdraw = async () => {
    const release = withdrawing
    const ok = await run(() => api.deleteRelease(repoId, release.version),
      `Deleted release ${release.version}. Its tag ${release.tag} is still there.`)
    if (ok) setWithdrawing(null)
  }

  // --- tags ------------------------------------------------------------------------

  const createTag = async () => {
    const ok = await run(() => api.createTag(repoId, {
      name: newTag.name.trim(), target: newTag.target || null, message: newTag.message.trim() || null,
    }), `Created tag ${newTag.name.trim()}`)
    if (ok) setNewTag(null)
  }

  const removeTag = async () => {
    const tag = deletingTag
    const ok = await run(() => api.deleteTag(repoId, tag.name), `Deleted tag ${tag.name}`)
    if (ok) setDeletingTag(null)
  }

  // --- rendering ---------------------------------------------------------------------

  const commitButton = (commit, commitId) => (commitId ? (
    <button type="button" onClick={() => onOpenCommit?.(commitId)} className="font-mono text-accent hover:underline"
      title={commit?.message || commitId}>
      {String(commitId).slice(0, 8)}
    </button>
  ) : null)

  const views = [
    { id: 'releases', label: 'Releases', icon: FiPackage, count: releases.length },
    { id: 'tags', label: 'Tags', icon: FiTag, count: tags.length },
  ]

  return (
    <div className="space-y-4" data-testid="releases-tab">
      <div className="flex flex-wrap items-center gap-2">
        {views.map(({ id, label, icon: Icon, count }) => (
          <button key={id} type="button" onClick={() => onViewChange?.(id)}
            className={cn('flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors',
              view === id ? 'border-accent/50 bg-accent/10 text-ink'
                : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink')}>
            <Icon className="h-3.5 w-3.5" /> {label}
            <span className="font-mono text-[11px] text-muted">{count}</span>
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} className="!px-2" title="Refresh" aria-label="Refresh releases">
            <FiRefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
          </Button>
          {canWrite && view === 'releases' && (
            <Button size="sm" onClick={() => setNewRelease({ version: '', tag: '', target: defaultBranch || '', title: '', notes: '' })}>
              <FiPlus className="h-4 w-4" /> Draft a new release
            </Button>
          )}
          {canWrite && view === 'tags' && (
            <Button size="sm" onClick={() => setNewTag({ name: '', target: defaultBranch || '', message: '' })}>
              <FiPlus className="h-4 w-4" /> New tag
            </Button>
          )}
        </div>
      </div>

      {error && (
        <GlassCard hover={false} className="flex items-start gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg" role="alert">
          <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}
      {notice && (
        <GlassCard hover={false} className="flex items-center gap-2 border-success-fg/25 bg-success-bg p-3 text-sm text-success-fg" data-testid="release-notice">
          <FiCheckCircle className="h-4 w-4 shrink-0" /> <span className="min-w-0 break-words">{notice}</span>
        </GlassCard>
      )}

      {loading && !releases.length && !tags.length ? (
        <div className="space-y-2">{[0, 1].map((i) => <div key={i} className="h-24 animate-pulse rounded-xl bg-white/[0.04]" />)}</div>
      ) : view === 'releases' ? (
        releases.length === 0 ? (
          <EmptyState icon={FiPackage} title="No releases yet"
            description="A release names a version people can refer to, with notes on what changed and a download of its files."
            action={canWrite ? <Button onClick={() => setNewRelease({ version: '', tag: '', target: defaultBranch || '', title: '', notes: '' })}>Draft a release</Button> : null} />
        ) : (
          <div className="space-y-3">
            {releases.map((r) => (
              <GlassCard key={r.version} hover={false} className="p-5" data-testid="release-card" data-version={r.version}>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-display text-lg font-medium tracking-tight text-ink">{r.title || r.version}</h3>
                  {r.latest && <Badge variant="success">Latest</Badge>}
                  {r.prerelease && <Badge variant="warning">Pre-release</Badge>}
                  <div className="ml-auto flex items-center gap-1">
                    {canWrite && (
                      <Button variant="ghost" size="sm" className="!px-2" title="Edit" aria-label={`Edit ${r.version}`}
                        onClick={() => setEditing({ version: r.version, title: r.title || '', notes: r.notes || '' })}>
                        <FiEdit3 className="h-3.5 w-3.5" />
                      </Button>
                    )}
                    {canManage && (
                      <Button variant="danger" size="sm" className="!px-2" title="Delete" aria-label={`Delete ${r.version}`}
                        onClick={() => setWithdrawing(r)}>
                        <FiTrash2 className="h-3.5 w-3.5" />
                      </Button>
                    )}
                  </div>
                </div>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-muted">
                  <span className="ref">{r.tag}</span>
                  {commitButton(r.commit, r.commit_id)}
                  <span>
                    {r.created_by ? `${r.created_by} published ` : 'Published '}
                    <span title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</span>
                  </span>
                </p>
                <div className="mt-3 border-t border-border pt-3">
                  {r.notes ? <Markdown>{r.notes}</Markdown> : <p className="text-sm text-muted">No notes.</p>}
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-3">
                  <span className="text-[11px] uppercase tracking-wider text-muted">Assets</span>
                  <Button variant="secondary" size="sm" onClick={() => download(r.tag, r.tag)}>
                    <FiDownload className="h-3.5 w-3.5" /> Source code (zip)
                  </Button>
                </div>
              </GlassCard>
            ))}
          </div>
        )
      ) : tags.length === 0 ? (
        <EmptyState icon={FiTag} title="No tags" description="A tag names a commit so it can be found again, built from, or downloaded."
          action={canWrite ? <Button onClick={() => setNewTag({ name: '', target: defaultBranch || '', message: '' })}>Create a tag</Button> : null} />
      ) : (
        <GlassCard hover={false} className="p-0">
          {tags.map((t) => (
            <div key={t.name} data-testid="tag-row" data-tag={t.name}
              className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3 last:border-b-0">
              <FiTag className="h-4 w-4 shrink-0 text-accent" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[13px] text-ink">{t.name}</span>
                  {t.release && (
                    <button type="button" onClick={() => onViewChange?.('releases')}>
                      <Badge variant="accent">Release {t.release}</Badge>
                    </button>
                  )}
                </div>
                <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
                  {commitButton(t.commit, t.commit_id)}
                  {t.message && <span className="max-w-[28rem] truncate text-ink-soft">{t.message}</span>}
                  <span>
                    {t.created_by ? `${t.created_by} · ` : ''}
                    <span title={absoluteTime(t.created_at)}>{relativeTime(t.created_at)}</span>
                  </span>
                </p>
              </div>
              <Button variant="ghost" size="sm" className="!px-2" title="Download the files at this tag"
                aria-label={`Download ${t.name}`} onClick={() => download(t.name, t.name)}>
                <FiDownload className="h-3.5 w-3.5" />
              </Button>
              {canManage && (
                <span title={t.release ? `Release ${t.release} is built on this tag; delete the release first` : 'Delete tag'}>
                  <Button variant="danger" size="sm" className="!px-2" disabled={Boolean(t.release) || busy}
                    aria-label={`Delete ${t.name}`} onClick={() => setDeletingTag(t)}>
                    <FiTrash2 className="h-3.5 w-3.5" />
                  </Button>
                </span>
              )}
            </div>
          ))}
        </GlassCard>
      )}

      <Modal open={Boolean(newRelease)} onClose={() => setNewRelease(null)} panelClassName="max-w-2xl"
        title="Draft a new release" subtitle="A release names a version. Its tag is created for you unless you pick one that exists.">
        {newRelease && (() => {
          const tagName = releaseTagName(newRelease)
          const existing = tagExists(tagName)
          return (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Input label="Version" value={newRelease.version} autoFocus placeholder="1.2.0"
                  hint="Semantic version: 1.2.0, or 2.0.0-rc.1 for a pre-release"
                  onChange={(e) => setNewRelease({ ...newRelease, version: e.target.value })} />
                <label className="block space-y-1.5">
                  <span className="text-[13px] font-medium text-ink-soft">Tag</span>
                  <input className={fieldClass} list="release-tag-options" value={newRelease.tag}
                    placeholder={tagName || 'v1.2.0'} aria-label="Tag"
                    onChange={(e) => setNewRelease({ ...newRelease, tag: e.target.value })} />
                  <datalist id="release-tag-options">
                    {tagsWithoutRelease.map((t) => <option key={t.name} value={t.name} />)}
                  </datalist>
                  <span className="text-xs text-muted">
                    {tagName ? (existing ? `Uses the existing tag ${tagName}.` : `Creates the tag ${tagName}.`) : ''}
                  </span>
                </label>
              </div>
              {!existing && (
                <TargetPicker branches={branchNames} value={newRelease.target}
                  label="Tag the tip of" onChange={(target) => setNewRelease({ ...newRelease, target })} />
              )}
              <Input label="Title (optional)" value={newRelease.title} placeholder={newRelease.version || 'Search and automation'}
                onChange={(e) => setNewRelease({ ...newRelease, title: e.target.value })} />
              <NotesEditor value={newRelease.notes} drafting={drafting}
                onChange={(notes) => setNewRelease({ ...newRelease, notes })}
                onDraft={() => draftNotes(newRelease, setNewRelease)} />
              <div className="flex justify-end gap-2">
                <Button variant="secondary" onClick={() => setNewRelease(null)}>Cancel</Button>
                <Button onClick={publish} loading={busy} disabled={!newRelease.version.trim() || (!existing && !newRelease.target)}>
                  Publish release
                </Button>
              </div>
            </div>
          )
        })()}
      </Modal>

      <Modal open={Boolean(editing)} onClose={() => setEditing(null)} panelClassName="max-w-2xl"
        title={`Edit ${editing?.version || ''}`} subtitle="The version and its tag stay as they are; they are what people refer to.">
        {editing && (
          <div className="space-y-4">
            <Input label="Title" value={editing.title} onChange={(e) => setEditing({ ...editing, title: e.target.value })} />
            <NotesEditor value={editing.notes} onChange={(notes) => setEditing({ ...editing, notes })} />
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
              <Button onClick={saveEdit} loading={busy}>Save</Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={Boolean(withdrawing)} onClose={() => setWithdrawing(null)} title={`Delete release ${withdrawing?.version || ''}?`}
        subtitle={`The tag ${withdrawing?.tag || ''} and its commit stay; only the release and its notes go.`}>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setWithdrawing(null)}>Cancel</Button>
          <Button variant="danger" onClick={withdraw} loading={busy}>Delete release</Button>
        </div>
      </Modal>

      <Modal open={Boolean(newTag)} onClose={() => setNewTag(null)} title="New tag"
        subtitle="Names a commit, permanently, so it can be found, built from and downloaded.">
        {newTag && (
          <div className="space-y-4">
            <Input label="Tag name" value={newTag.name} autoFocus placeholder="v1.2.0"
              onChange={(e) => setNewTag({ ...newTag, name: e.target.value })} />
            <TargetPicker branches={branchNames} value={newTag.target} label="Tag the tip of"
              onChange={(target) => setNewTag({ ...newTag, target })} />
            <Input label="Message (optional)" value={newTag.message} placeholder="What this tag marks"
              onChange={(e) => setNewTag({ ...newTag, message: e.target.value })} />
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setNewTag(null)}>Cancel</Button>
              <Button onClick={createTag} loading={busy} disabled={!newTag.name.trim() || !newTag.target.trim()}>Create tag</Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={Boolean(deletingTag)} onClose={() => setDeletingTag(null)} title={`Delete tag ${deletingTag?.name || ''}?`}
        subtitle="The commit stays; only its name goes. Anyone who refers to this tag will no longer find it.">
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDeletingTag(null)}>Cancel</Button>
          <Button variant="danger" onClick={removeTag} loading={busy}>Delete tag</Button>
        </div>
      </Modal>
    </div>
  )
}
