import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  FiArrowLeft, FiLoader, FiAlertCircle, FiCheck, FiFolder, FiGitCommit,
  FiGitPullRequest, FiMapPin, FiLink, FiCalendar, FiEdit2, FiUpload, FiTrash2,
  FiLock, FiUser, FiSettings,
} from 'react-icons/fi'
import api from '../utils/api'
import Avatar from '../components/Avatar'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import { cn } from '../lib/utils'

/**
 * A person: who they are, what they own, and what they have been doing.
 *
 * The same page serves your own profile and someone else's. The editing controls are
 * the only difference, because the repository list is already filtered server-side to
 * what the viewer may see — a profile must not become a way to enumerate private
 * repositories by owner.
 */

const TABS = [
  { id: 'repositories', label: 'Repositories', icon: FiFolder },
  { id: 'commits', label: 'Commits', icon: FiGitCommit },
  { id: 'settings', label: 'Settings', icon: FiSettings, selfOnly: true },
]

function Stat({ icon: Icon, value, label }) {
  return (
    <div className="flex items-center gap-2 rounded-xl border border-border bg-white/[0.03] px-3 py-2">
      <Icon className="h-4 w-4 shrink-0 text-muted" />
      <div className="min-w-0">
        <p className="text-[15px] font-semibold leading-tight text-ink">{value}</p>
        <p className="truncate text-[11px] leading-tight text-muted">{label}</p>
      </div>
    </div>
  )
}

function EditProfile({ user, onSaved, onError }) {
  const [form, setForm] = useState({
    full_name: user.full_name || '',
    bio: user.bio || '',
    location: user.location || '',
    website: user.website || '',
    email: user.email || '',
  })
  const [saving, setSaving] = useState(false)

  const field = (key, value) => setForm((prev) => ({ ...prev, [key]: value }))

  const save = async () => {
    setSaving(true)
    onError('')
    try {
      await api.updateOwnProfile(form)
      onSaved('Profile updated.')
    } catch (err) {
      onError(err.message || 'Could not save your profile')
    } finally {
      setSaving(false)
    }
  }

  const input = 'w-full rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[13px] text-ink outline-none focus:border-accent/60'

  return (
    <GlassCard className="p-4" hover={false}>
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
        <FiEdit2 className="h-4 w-4 text-muted" /> Edit profile
      </h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-[11.5px] text-muted">
          Full name
          <input className={cn(input, 'mt-1')} value={form.full_name}
                 onChange={(e) => field('full_name', e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted">
          Email
          <input className={cn(input, 'mt-1')} value={form.email}
                 onChange={(e) => field('email', e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted">
          Location
          <input className={cn(input, 'mt-1')} value={form.location}
                 onChange={(e) => field('location', e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted">
          Website
          <input className={cn(input, 'mt-1')} placeholder="https://…" value={form.website}
                 onChange={(e) => field('website', e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted sm:col-span-2">
          Bio
          <textarea rows={3} className={cn(input, 'mt-1')} value={form.bio}
                    onChange={(e) => field('bio', e.target.value)} />
          <span className="mt-1 block text-right text-[10.5px]">
            {form.bio.length} / 1000
          </span>
        </label>
      </div>
      <Button type="button" onClick={save} disabled={saving} className="mt-2">
        {saving ? 'Saving…' : 'Save changes'}
      </Button>
    </GlassCard>
  )
}

function ChangePassword({ onSaved, onError }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [saving, setSaving] = useState(false)

  const input = 'w-full rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[13px] text-ink outline-none focus:border-accent/60'

  const submit = async () => {
    onError('')
    if (next !== confirm) {
      onError('The new passwords do not match.')
      return
    }
    if (next.length < 8) {
      onError('A password must be at least 8 characters.')
      return
    }
    setSaving(true)
    try {
      await api.changePassword(current, next)
      onSaved('Password changed.')
      setCurrent(''); setNext(''); setConfirm('')
    } catch (err) {
      onError(err.message || 'Could not change your password')
    } finally {
      setSaving(false)
    }
  }

  return (
    <GlassCard className="p-4" hover={false}>
      <h3 className="mb-1 flex items-center gap-2 text-sm font-semibold text-ink">
        <FiLock className="h-4 w-4 text-muted" /> Change password
      </h3>
      <p className="mb-3 text-[11.5px] text-muted">
        Your current password is required, so someone who finds an open session cannot
        lock you out of your own account.
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-[11.5px] text-muted">
          Current
          <input type="password" className={cn(input, 'mt-1')} value={current}
                 onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted">
          New
          <input type="password" className={cn(input, 'mt-1')} value={next}
                 onChange={(e) => setNext(e.target.value)} />
        </label>
        <label className="text-[11.5px] text-muted">
          Confirm new
          <input type="password" className={cn(input, 'mt-1')} value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} />
        </label>
      </div>
      <Button type="button" onClick={submit}
              disabled={saving || !current || !next} className="mt-3">
        {saving ? 'Changing…' : 'Change password'}
      </Button>
    </GlassCard>
  )
}

export default function Profile({ username, onBack, onOpenRepo }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [tab, setTab] = useState('repositories')
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [avatarKey, setAvatarKey] = useState(0)
  const fileInput = useRef(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = username ? await api.getUserProfile(username) : await api.getOwnProfile()
      setData(res)
    } catch (err) {
      setError(err.message || 'Could not load this profile')
    } finally {
      setLoading(false)
    }
  }, [username])

  useEffect(() => { load() }, [load])

  const announce = async (message) => {
    setNotice(message)
    setError('')
    await load()
  }

  const pickAvatar = async (file) => {
    if (!file) return
    setAvatarBusy(true)
    setError('')
    try {
      await api.uploadAvatar(file)
      // The <img> URL does not change when the image does, so it is remounted to
      // avoid showing the cached previous picture.
      setAvatarKey((k) => k + 1)
      await announce('Profile picture updated.')
    } catch (err) {
      setError(err.message || 'Could not upload that picture')
    } finally {
      setAvatarBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const removeAvatar = async () => {
    setAvatarBusy(true)
    try {
      await api.deleteAvatar()
      setAvatarKey((k) => k + 1)
      await announce('Profile picture removed.')
    } catch (err) {
      setError(err.message || 'Could not remove your picture')
    } finally {
      setAvatarBusy(false)
    }
  }

  if (loading) {
    return (
      <GlassCard className="p-6" hover={false}>
        <p className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
          <FiLoader className="h-4 w-4 animate-spin" /> Loading profile…
        </p>
      </GlassCard>
    )
  }

  if (error && !data) {
    return (
      <GlassCard className="p-6" hover={false}>
        <p className="flex items-start gap-2 py-8 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
      </GlassCard>
    )
  }

  const user = data?.user || {}
  const stats = data?.stats || {}
  const repositories = data?.repositories || []
  const commits = data?.recent_commits || []
  const isSelf = !!user.is_self
  const visibleTabs = TABS.filter((t) => !t.selfOnly || isSelf)

  return (
    <div className="space-y-4">
      {onBack && (
        <button type="button" onClick={onBack}
                className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
          <FiArrowLeft className="h-4 w-4" /> Back
        </button>
      )}

      {error && (
        <p className="flex items-start gap-2 rounded-xl border border-danger-fg/25 bg-danger-fg/10 px-4 py-3 text-sm text-danger-fg">
          <FiAlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {error}
        </p>
      )}
      {notice && !error && (
        <p className="flex items-start gap-2 rounded-xl border border-success-fg/25 bg-success-fg/10 px-4 py-3 text-sm text-success-fg">
          <FiCheck className="mt-0.5 h-4 w-4 shrink-0" /> {notice}
        </p>
      )}

      <GlassCard className="p-5" hover={false}>
        <div className="flex flex-wrap items-start gap-4">
          <div className="relative">
            <Avatar key={avatarKey} username={user.username} fullName={user.full_name}
                    hasAvatar={user.has_avatar} size="xl" />
            {isSelf && (
              <div className="mt-2 flex justify-center gap-1">
                <button type="button" title="Upload a picture" disabled={avatarBusy}
                        onClick={() => fileInput.current?.click()}
                        className="rounded-lg border border-border p-1.5 text-muted hover:text-ink disabled:opacity-50">
                  <FiUpload className="h-3.5 w-3.5" />
                </button>
                {user.has_avatar && (
                  <button type="button" title="Remove picture" disabled={avatarBusy}
                          onClick={removeAvatar}
                          className="rounded-lg border border-border p-1.5 text-muted hover:text-danger-fg disabled:opacity-50">
                    <FiTrash2 className="h-3.5 w-3.5" />
                  </button>
                )}
                <input ref={fileInput} type="file" accept="image/*" className="hidden"
                       onChange={(e) => pickAvatar(e.target.files?.[0])} />
              </div>
            )}
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold text-ink">
                {user.full_name || user.username}
              </h1>
              <span className="font-mono text-[12px] text-muted">@{user.username}</span>
              <Badge variant={user.role === 'admin' ? 'accent' : 'default'}>
                {user.role}
              </Badge>
              {!user.is_active && <Badge variant="danger">inactive</Badge>}
            </div>

            {user.bio && (
              <p className="mt-2 max-w-2xl whitespace-pre-wrap text-[13px] leading-relaxed text-ink-soft">
                {user.bio}
              </p>
            )}

            <div className="mt-2 flex flex-wrap items-center gap-3 text-[11.5px] text-muted">
              {user.location && (
                <span className="inline-flex items-center gap-1">
                  <FiMapPin className="h-3 w-3" /> {user.location}
                </span>
              )}
              {user.website && (
                <a href={user.website} target="_blank" rel="noreferrer"
                   className="inline-flex items-center gap-1 text-accent hover:underline">
                  <FiLink className="h-3 w-3" /> {user.website.replace(/^https?:\/\//, '')}
                </a>
              )}
              {user.email && (
                <span className="inline-flex items-center gap-1">
                  <FiUser className="h-3 w-3" /> {user.email}
                </span>
              )}
              {user.joined_at && (
                <span className="inline-flex items-center gap-1">
                  <FiCalendar className="h-3 w-3" /> joined {user.joined_at.slice(0, 10)}
                </span>
              )}
            </div>
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            <Stat icon={FiFolder} value={stats.repositories ?? 0} label="repositories" />
            <Stat icon={FiGitCommit} value={stats.commits ?? 0} label="commits" />
            <Stat icon={FiGitPullRequest} value={stats.pull_requests ?? 0} label="pull requests" />
          </div>
        </div>
      </GlassCard>

      <div className="flex flex-wrap gap-1 border-b border-border">
        {visibleTabs.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" onClick={() => setTab(id)}
                  className={cn(
                    'inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] transition',
                    tab === id ? 'border-accent text-ink'
                               : 'border-transparent text-muted hover:text-ink'
                  )}>
            <Icon className="h-3.5 w-3.5" /> {label}
          </button>
        ))}
      </div>

      {tab === 'repositories' && (
        <GlassCard className="p-4" hover={false}>
          {!repositories.length ? (
            <p className="py-10 text-center text-sm text-muted">
              {isSelf
                ? 'You do not own any repositories yet.'
                : 'No repositories you can see.'}
            </p>
          ) : (
            <ul className="space-y-2">
              {repositories.map((repo) => (
                <li key={repo.id}
                    onClick={onOpenRepo ? () => onOpenRepo(repo) : undefined}
                    className={cn(
                      'rounded-xl border border-border bg-white/[0.03] p-3',
                      onOpenRepo && 'cursor-pointer transition hover:border-border-strong hover:bg-white/[0.06]'
                    )}>
                  <div className="flex flex-wrap items-center gap-2">
                    <FiFolder className="h-3.5 w-3.5 text-muted" />
                    <span className="text-[13.5px] font-medium text-ink">{repo.name}</span>
                    {repo.is_archived && <Badge variant="warning">archived</Badge>}
                    <span className="ml-auto text-[11px] text-muted">
                      {repo.updated_at ? repo.updated_at.slice(0, 10) : ''}
                    </span>
                  </div>
                  {repo.description && (
                    <p className="mt-1 text-[12.5px] text-ink-soft">{repo.description}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </GlassCard>
      )}

      {tab === 'commits' && (
        <GlassCard className="p-4" hover={false}>
          {stats.commits > 0 && commits.length === 0 && (
            <p className="mb-3 text-[11.5px] text-muted">
              {stats.commits} commit(s) authored, none in repositories you can see.
            </p>
          )}
          {!commits.length ? (
            <p className="py-10 text-center text-sm text-muted">No commits to show.</p>
          ) : (
            <ol className="space-y-1.5">
              {commits.map((commit) => (
                <li key={commit.id}
                    className="flex flex-wrap items-baseline gap-2 rounded-lg border border-border bg-white/[0.03] px-3 py-2 text-[12.5px]">
                  <span className="font-mono text-[11px] text-accent">{commit.short_id}</span>
                  <span className="text-ink">{commit.message}</span>
                  {commit.repository_name && (
                    <span className="text-[11px] text-muted">in {commit.repository_name}</span>
                  )}
                  <span className="ml-auto text-[11px] text-muted">
                    {(commit.timestamp || '').slice(0, 10)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </GlassCard>
      )}

      {tab === 'settings' && isSelf && (
        <div className="space-y-4">
          <EditProfile user={user} onSaved={announce} onError={setError} />
          <ChangePassword onSaved={announce} onError={setError} />
        </div>
      )}
    </div>
  )
}
