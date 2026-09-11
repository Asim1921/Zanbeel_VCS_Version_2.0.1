import React, { useCallback, useEffect, useState } from 'react'
import {
  FiTag,
  FiPackage,
  FiPlus,
  FiTrash2,
  FiRefreshCw,
  FiAlertTriangle,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'
import { cn } from '../../lib/utils'

/**
 * Tags and releases, which existed in the API and the CLI and in no screen.
 *
 * Kept on one panel with two tabs because a release is a tag with notes: showing
 * them apart makes people create a release whose tag they cannot find, or a tag
 * they then wonder why nobody was told about.
 */

export default function ReleasesPanel({ repoId, canWrite = false }) {
  const [tab, setTab] = useState('releases')
  const [tags, setTags] = useState([])
  const [releases, setReleases] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [tagOpen, setTagOpen] = useState(false)
  const [tagName, setTagName] = useState('')
  const [tagMessage, setTagMessage] = useState('')

  const [relOpen, setRelOpen] = useState(false)
  const [version, setVersion] = useState('')
  const [title, setTitle] = useState('')
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const [t, r] = await Promise.all([
        api.listTags(repoId).catch(() => ({ tags: [] })),
        api.listReleases(repoId).catch(() => ({ releases: [] })),
      ])
      setTags(t.tags || [])
      setReleases(r.releases || [])
    } catch (err) {
      setError(err.message || 'Failed to load tags and releases')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => {
    load()
  }, [load])

  const createTag = async () => {
    if (!tagName.trim()) return
    try {
      setSaving(true)
      setError(null)
      await api.createTag(repoId, { name: tagName.trim(), message: tagMessage.trim() || null })
      setTagOpen(false)
      setTagName('')
      setTagMessage('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create the tag')
    } finally {
      setSaving(false)
    }
  }

  const createRelease = async () => {
    if (!version.trim()) return
    try {
      setSaving(true)
      setError(null)
      await api.createRelease(repoId, {
        version: version.trim(),
        title: title.trim() || null,
        notes: notes.trim() || null,
      })
      setRelOpen(false)
      setVersion('')
      setTitle('')
      setNotes('')
      await load()
    } catch (err) {
      setError(err.message || 'Failed to publish the release')
    } finally {
      setSaving(false)
    }
  }

  const removeTag = async (name) => {
    if (!window.confirm(`Delete tag "${name}"? A release built on it keeps working.`)) return
    try {
      await api.deleteTag(repoId, name)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to delete the tag')
    }
  }

  const TABS = [
    { id: 'releases', label: 'Releases', icon: FiPackage, count: releases.length },
    { id: 'tags', label: 'Tags', icon: FiTag, count: tags.length },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg font-light tracking-tight text-ink">
            Releases and tags
          </h3>
          <p className="mt-1 text-sm text-muted">
            A release is a tag with notes. Versions are validated as semantic versions.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          {canWrite && (
            <Button
              onClick={() => (tab === 'releases' ? setRelOpen(true) : setTagOpen(true))}
              className="flex items-center gap-2"
            >
              <FiPlus className="h-4 w-4" />
              {tab === 'releases' ? 'New release' : 'New tag'}
            </Button>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        {TABS.map(({ id, label, icon: Icon, count }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              'flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors',
              tab === id
                ? 'border-accent/50 bg-accent/10 text-ink'
                : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink'
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
            <span className="font-mono text-[11px] text-muted">{count}</span>
          </button>
        ))}
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      {loading ? (
        <div className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : tab === 'releases' ? (
        releases.length === 0 ? (
          <EmptyState
            icon={FiPackage}
            title="No releases yet"
            description="Publish one to mark a version people can refer to."
            action={canWrite ? <Button onClick={() => setRelOpen(true)}>Publish a release</Button> : null}
          />
        ) : (
          <div className="space-y-2">
            {releases.map((r) => (
              <GlassCard key={r.version} hover={false} className="p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <FiPackage className="h-4 w-4 shrink-0 text-accent" />
                  <span className="font-medium text-ink">{r.title || r.version}</span>
                  <Badge variant="info">{r.version}</Badge>
                  {r.tag && <span className="ref">{r.tag}</span>}
                </div>
                {r.notes && (
                  <p className="mt-2 whitespace-pre-wrap break-words text-[13px] text-ink-soft">
                    {r.notes}
                  </p>
                )}
                {r.commit_id && (
                  <p className="mt-2 font-mono text-[11px] text-muted">
                    {String(r.commit_id).slice(0, 12)}
                  </p>
                )}
              </GlassCard>
            ))}
          </div>
        )
      ) : tags.length === 0 ? (
        <EmptyState
          icon={FiTag}
          title="No tags"
          description="A tag names a commit so you can find it again."
          action={canWrite ? <Button onClick={() => setTagOpen(true)}>Create a tag</Button> : null}
        />
      ) : (
        <div className="space-y-2">
          {tags.map((t) => (
            <GlassCard key={t.name} hover={false} className="flex flex-wrap items-center gap-3 p-3.5">
              <FiTag className="h-4 w-4 shrink-0 text-accent" />
              <div className="min-w-0 flex-1">
                <span className="font-mono text-[13px] text-ink">{t.name}</span>
                {t.message && <p className="mt-0.5 truncate text-xs text-muted">{t.message}</p>}
              </div>
              {t.commit_id && (
                <span className="ref shrink-0">{String(t.commit_id).slice(0, 10)}</span>
              )}
              {canWrite && (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => removeTag(t.name)}
                  className="!px-2"
                  title="Delete tag"
                >
                  <FiTrash2 className="h-3.5 w-3.5" />
                </Button>
              )}
            </GlassCard>
          ))}
        </div>
      )}

      <Modal
        open={tagOpen}
        onClose={() => setTagOpen(false)}
        title="New tag"
        subtitle="Names the current head commit unless you say otherwise."
      >
        <div className="space-y-4">
          <Input label="Tag name" value={tagName} onChange={(e) => setTagName(e.target.value)} placeholder="v1.2.0" />
          <Input
            label="Message (optional)"
            value={tagMessage}
            onChange={(e) => setTagMessage(e.target.value)}
            placeholder="What this tag marks"
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setTagOpen(false)}>
              Cancel
            </Button>
            <Button onClick={createTag} disabled={!tagName.trim() || saving}>
              {saving ? 'Creating…' : 'Create tag'}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal
        open={relOpen}
        onClose={() => setRelOpen(false)}
        title="Publish a release"
        subtitle="The tag is created for you if it does not exist."
      >
        <div className="space-y-4">
          <Input
            label="Version"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            placeholder="1.2.0"
            hint="Semantic version. The server rejects anything else."
          />
          <Input label="Title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Search and automation" />
          <div>
            <label className="mb-1.5 block text-[13px] font-medium text-ink-soft">
              Notes (optional)
            </label>
            <textarea
              rows={5}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="What changed in this version"
              className="w-full resize-y rounded-lg border border-border bg-cream-mid px-3 py-2 text-[13px] text-ink placeholder:text-muted focus:border-accent focus:outline-none"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setRelOpen(false)}>
              Cancel
            </Button>
            <Button onClick={createRelease} disabled={!version.trim() || saving}>
              {saving ? 'Publishing…' : 'Publish release'}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
