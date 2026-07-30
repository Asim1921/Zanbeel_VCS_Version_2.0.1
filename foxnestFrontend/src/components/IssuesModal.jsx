import React, { useEffect, useMemo, useState } from 'react'
import { FiX, FiRefreshCw, FiPlus, FiLoader, FiMessageSquare, FiTag, FiFlag, FiUser, FiHash, FiCheckCircle } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import Badge from './ui/Badge'
import api from '../utils/api'

const STATUS_META = {
  open: { label: 'OPEN', variant: 'success' },
  in_progress: { label: 'IN PROGRESS', variant: 'info' },
  resolved: { label: 'RESOLVED', variant: 'default' },
  closed: { label: 'CLOSED', variant: 'default' }
}

const PRIORITY_ORDER = ['critical', 'high', 'medium', 'low']

const IssuesModal = ({ repo, onClose }) => {
  const repoId = repo?.id

  const [activeTab, setActiveTab] = useState('issues') // issues | milestones

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [issues, setIssues] = useState([])
  const [totalIssues, setTotalIssues] = useState(null)
  const [selectedIssueNumber, setSelectedIssueNumber] = useState(null)
  const [selectedIssue, setSelectedIssue] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [statusFilter, setStatusFilter] = useState('open')
  const [search, setSearch] = useState('')
  const [_pageOffset, setPageOffset] = useState(0)
  const pageLimit = 50

  const [_labels, setLabels] = useState([])
  const [milestones, setMilestones] = useState([])

  // Create issue form
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [issueType, setIssueType] = useState('task')
  const [priority, setPriority] = useState('medium')
  const [assignedTo, setAssignedTo] = useState('')
  const [milestoneId, setMilestoneId] = useState('')
  const [labelsText, setLabelsText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Comment form
  const [commentBody, setCommentBody] = useState('')
  const [commentSubmitting, setCommentSubmitting] = useState(false)

  // Milestones tab
  const [milestoneTitle, setMilestoneTitle] = useState('')
  const [milestoneDescription, setMilestoneDescription] = useState('')
  const [milestoneDueDate, setMilestoneDueDate] = useState('')
  const [milestoneSubmitting, setMilestoneSubmitting] = useState(false)

  const parsedLabels = useMemo(() => {
    return labelsText
      .split(',')
      .map(s => s.trim())
      .filter(Boolean)
      .slice(0, 20)
  }, [labelsText])

  const loadMeta = async () => {
    if (!repoId) return
    try {
      const [labelsRes, milestonesRes] = await Promise.all([
        api.listIssueLabels(repoId),
        api.listMilestones(repoId, true)
      ])
      setLabels(labelsRes.labels || [])
      setMilestones(milestonesRes.milestones || [])
    } catch {
      // Non-fatal
    }
  }

  const loadIssues = async () => {
    if (!repoId) return
    try {
      setLoading(true)
      setError(null)
      setSelectedIssue(null)
      setSelectedIssueNumber(null)
      setPageOffset(0)
      const res = await api.listIssues(repoId, {
        status: statusFilter || null,
        search: search.trim() || null,
        offset: 0,
        limit: pageLimit
      })
      const list = res.issues || []
      setTotalIssues(typeof res.total === 'number' ? res.total : null)
      // Sort by priority then updated
      list.sort((a, b) => {
        const pa = PRIORITY_ORDER.indexOf(a.priority)
        const pb = PRIORITY_ORDER.indexOf(b.priority)
        if (pa !== pb) return pa - pb
        return (b.updated_at || '').localeCompare(a.updated_at || '')
      })
      setIssues(list)
      await loadMeta()
    } catch (err) {
      setError(err.message || 'Unable to load issues')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const handler = async (evt) => {
      const detail = evt?.detail || {}
      if (!detail?.repository_id || !detail?.issue_number) return
      if (!repoId || detail.repository_id !== repoId) return

      const num = Number(detail.issue_number)
      if (!Number.isFinite(num)) return

      try {
        setActiveTab('issues')
        setSelectedIssueNumber(num)
        await loadIssueDetail(num)
      } catch {
        // ignore
      }
    }

    window.addEventListener('foxnest:select-issue', handler)
    return () => window.removeEventListener('foxnest:select-issue', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId])

  const formatEvent = (e) => {
    const t = (e?.type || '').toLowerCase()
    if (t === 'issue_commit_submitted') return 'Commit submitted for review'
    if (t === 'issue_commit_approved') return 'Commit approved'
    if (t === 'issue_commit_rejected') return 'Commit rejected'
    if (t === 'issue_status_changed') return 'Status changed'
    if (t === 'issue_comment_added') return 'Comment added'
    if (t === 'issue_assigned') return 'Assignee updated'
    if (t === 'issue_created') return 'Issue created'
    return e?.type || 'Event'
  }

  const loadMoreIssues = async () => {
    if (!repoId) return
    try {
      setError(null)
      const nextOffset = issues.length
      const res = await api.listIssues(repoId, {
        status: statusFilter || null,
        search: search.trim() || null,
        offset: nextOffset,
        limit: pageLimit
      })
      const more = res.issues || []
      setTotalIssues(typeof res.total === 'number' ? res.total : totalIssues)
      setIssues(prev => [...prev, ...more])
      setPageOffset(nextOffset)
    } catch (err) {
      setError(err.message || 'Unable to load more issues')
    }
  }

  const loadIssueDetail = async (issueNumber) => {
    if (!repoId || !issueNumber) return
    try {
      setDetailLoading(true)
      setError(null)
      const res = await api.getIssue(repoId, issueNumber)
      setSelectedIssue(res.issue || null)
    } catch (err) {
      setError(err.message || 'Unable to load issue detail')
      setSelectedIssue(null)
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    loadIssues()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId])

  useEffect(() => {
    if (!repoId) return
    const t = setTimeout(() => {
      loadIssues()
    }, 250)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, search])

  const handleCreateIssue = async () => {
    if (!title.trim()) return
    try {
      setSubmitting(true)
      setError(null)
      await api.createIssue(repoId, {
        title: title.trim(),
        description: description.trim() || null,
        issue_type: issueType,
        priority,
        assigned_to: assignedTo.trim() || null,
        milestone_id: milestoneId ? Number(milestoneId) : null,
        labels: parsedLabels
      })
      setTitle('')
      setDescription('')
      setIssueType('task')
      setPriority('medium')
      setAssignedTo('')
      setMilestoneId('')
      setLabelsText('')
      await loadIssues()
    } catch (err) {
      setError(err.message || 'Failed to create issue')
    } finally {
      setSubmitting(false)
    }
  }

  const handleSelectIssue = async (issueNumber) => {
    setSelectedIssueNumber(issueNumber)
    await loadIssueDetail(issueNumber)
  }

  const statusBadge = (status) => {
    const meta = STATUS_META[status] || { label: (status || 'UNKNOWN').toUpperCase(), variant: 'default' }
    return <Badge variant={meta.variant} className="text-[11px]">{meta.label}</Badge>
  }

  const handleAddComment = async () => {
    if (!selectedIssueNumber || !commentBody.trim()) return
    try {
      setCommentSubmitting(true)
      setError(null)
      await api.addIssueComment(repoId, selectedIssueNumber, { body: commentBody.trim() })
      setCommentBody('')
      await loadIssueDetail(selectedIssueNumber)
      await loadIssues()
    } catch (err) {
      setError(err.message || 'Failed to add comment')
    } finally {
      setCommentSubmitting(false)
    }
  }

  const handleTransition = async (newStatus) => {
    if (!selectedIssueNumber) return
    try {
      setError(null)
      await api.updateIssue(repoId, selectedIssueNumber, { status: newStatus })
      await loadIssueDetail(selectedIssueNumber)
      await loadIssues()
    } catch (err) {
      setError(err.message || 'Failed to update status')
    }
  }

  const handleCreateMilestone = async () => {
    if (!milestoneTitle.trim()) return
    try {
      setMilestoneSubmitting(true)
      setError(null)
      await api.createMilestone(repoId, {
        title: milestoneTitle.trim(),
        description: milestoneDescription.trim() || null,
        due_date: milestoneDueDate ? new Date(milestoneDueDate).toISOString() : null
      })
      setMilestoneTitle('')
      setMilestoneDescription('')
      setMilestoneDueDate('')
      await loadMeta()
    } catch (err) {
      setError(err.message || 'Failed to create milestone')
    } finally {
      setMilestoneSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="absolute inset-0" onClick={onClose} />
      <div className="relative w-full max-w-6xl">
        <GlassCard className="p-6" hover={false}>
          <div className="flex items-start justify-between gap-4 mb-5">
            <div>
              <p className="text-xs uppercase tracking-[0.16em] text-white/50">Issues</p>
              <h2 className="text-xl font-semibold text-white">{repo?.name}</h2>
              <p className="text-sm text-white/60">Track work items linked to branches, commits, and PRs.</p>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-white/5 p-1">
                <button
                  type="button"
                  onClick={() => setActiveTab('issues')}
                  className={`px-3 py-1.5 rounded text-xs transition-colors ${activeTab === 'issues' ? 'bg-white/15 text-white' : 'text-white/70 hover:text-white'}`}
                >
                  Issues
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab('milestones')}
                  className={`px-3 py-1.5 rounded text-xs transition-colors ${activeTab === 'milestones' ? 'bg-white/15 text-white' : 'text-white/70 hover:text-white'}`}
                >
                  Milestones
                </button>
              </div>
              <Button variant="ghost" size="sm" onClick={loadIssues}>
                <FiRefreshCw className="w-4 h-4 mr-2" />
                Refresh
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose}>
                <FiX className="w-4 h-4 mr-2" />
                Close
              </Button>
            </div>
          </div>

          {error && (
            <div className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
              {error}
            </div>
          )}

          {activeTab === 'milestones' ? (
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
              <div className="xl:col-span-1 rounded-xl border border-white/10 bg-white/5 p-4">
                <h3 className="text-white font-medium mb-3 flex items-center gap-2"><FiPlus className="w-4 h-4" />New Milestone</h3>
                <div className="space-y-3">
                  <input
                    value={milestoneTitle}
                    onChange={(e) => setMilestoneTitle(e.target.value)}
                    placeholder="Milestone title"
                    className="w-full bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                  />
                  <textarea
                    value={milestoneDescription}
                    onChange={(e) => setMilestoneDescription(e.target.value)}
                    placeholder="Description (optional)"
                    className="w-full min-h-20 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                  />
                  <div>
                    <label className="text-xs text-white/60">Due date (optional)</label>
                    <input
                      type="date"
                      value={milestoneDueDate}
                      onChange={(e) => setMilestoneDueDate(e.target.value)}
                      className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white"
                    />
                  </div>
                  <Button variant="primary" size="sm" className="w-full" onClick={handleCreateMilestone} disabled={milestoneSubmitting || !milestoneTitle.trim()}>
                    {milestoneSubmitting ? <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Creating...</> : <><FiPlus className="w-4 h-4 mr-2" />Create milestone</>}
                  </Button>
                </div>
              </div>

              <div className="xl:col-span-2 rounded-xl border border-white/10 bg-white/5 p-4 max-h-[64vh] overflow-auto">
                <h3 className="text-white font-medium mb-3">Milestones</h3>
                {loading ? (
                  <div className="py-10 text-white/70 flex items-center justify-center"><FiLoader className="w-5 h-5 animate-spin mr-2" />Loading...</div>
                ) : milestones.length === 0 ? (
                  <p className="text-white/50 text-sm">No milestones yet.</p>
                ) : (
                  <div className="space-y-3">
                    {milestones.map((m) => (
                      <div key={m.id} className="rounded-lg border border-white/10 bg-white/5 p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <p className="text-white font-medium">{m.title}</p>
                            {m.description && <p className="text-white/70 text-sm mt-1">{m.description}</p>}
                            <p className="text-white/45 text-xs mt-2">
                              Due: {m.due_date ? new Date(m.due_date).toLocaleDateString() : '—'}
                            </p>
                          </div>
                          <div className="flex items-center gap-2">
                            <Badge variant={m.is_closed ? 'default' : 'info'} className="text-[11px]">
                              {m.is_closed ? 'CLOSED' : 'OPEN'}
                            </Badge>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
              <div className="xl:col-span-1 rounded-xl border border-white/10 bg-white/5 p-4">
                <h3 className="text-white font-medium mb-3 flex items-center gap-2"><FiPlus className="w-4 h-4" />New Issue</h3>
                <div className="space-y-3">
                  <input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder="Issue title"
                    className="w-full bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                  />
                  <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Description (supports @mentions, Fixes #123 references)"
                    className="w-full min-h-20 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                  />

                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <label className="text-xs text-white/60">Type</label>
                      <select value={issueType} onChange={(e) => setIssueType(e.target.value)} className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white">
                        {['bug', 'feature', 'task', 'question'].map(t => (
                          <option className="bg-slate-100 text-slate-900" key={t} value={t}>{t}</option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="text-xs text-white/60">Priority</label>
                      <select value={priority} onChange={(e) => setPriority(e.target.value)} className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white">
                        {PRIORITY_ORDER.map(p => (
                          <option className="bg-slate-100 text-slate-900" key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <div>
                    <label className="text-xs text-white/60">Assignee (username, optional)</label>
                    <input
                      value={assignedTo}
                      onChange={(e) => setAssignedTo(e.target.value)}
                      placeholder="e.g. alice"
                      className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                    />
                  </div>

                  <div>
                    <label className="text-xs text-white/60">Milestone (optional)</label>
                    <select value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white">
                      <option className="bg-slate-100 text-slate-900" value="">—</option>
                      {milestones.filter(m => !m.is_closed).map(m => (
                        <option className="bg-slate-100 text-slate-900" key={m.id} value={String(m.id)}>{m.title}</option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="text-xs text-white/60">Labels (comma-separated, optional)</label>
                    <input
                      value={labelsText}
                      onChange={(e) => setLabelsText(e.target.value)}
                      placeholder="e.g. backend, urgent"
                      className="w-full mt-1 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                    />
                    {parsedLabels.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {parsedLabels.map((l) => <Badge key={l} variant="default" className="text-[11px]">{l}</Badge>)}
                      </div>
                    )}
                  </div>

                  <Button
                    variant="primary"
                    size="sm"
                    className="w-full"
                    onClick={handleCreateIssue}
                    disabled={submitting || !title.trim()}
                  >
                    {submitting ? <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Creating...</> : <><FiPlus className="w-4 h-4 mr-2" />Create issue</>}
                  </Button>
                </div>
              </div>

              <div className="xl:col-span-1 rounded-xl border border-white/10 bg-white/5 p-4 max-h-[64vh] overflow-auto">
                <div className="flex items-center justify-between gap-2 mb-3">
                  <h3 className="text-white font-medium">Issues</h3>
                  <div className="flex items-center gap-2">
                    <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-white/10 border border-white/20 rounded px-2 py-1 text-xs text-white">
                      {['open', 'in_progress', 'resolved', 'closed', ''].map(s => (
                        <option className="bg-slate-100 text-slate-900" key={s || 'all'} value={s}>{s ? s : 'all'}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search issues…"
                  className="w-full mb-3 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                />

                {loading ? (
                  <div className="py-10 text-white/70 flex items-center justify-center"><FiLoader className="w-5 h-5 animate-spin mr-2" />Loading issues...</div>
                ) : issues.length === 0 ? (
                  <p className="text-white/50 text-sm">No issues found.</p>
                ) : (
                  <div className="space-y-2">
                    {issues.map((i) => (
                      <button
                        key={i.number}
                        type="button"
                        onClick={() => handleSelectIssue(i.number)}
                        className={`w-full text-left rounded-lg border px-3 py-2 transition-colors ${
                          selectedIssueNumber === i.number
                            ? 'border-purple-400/60 bg-white/10'
                            : 'border-white/10 bg-white/5 hover:bg-white/10'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="text-white font-medium truncate">
                              <span className="text-white/60 mr-2">#{i.number}</span>
                              {i.title}
                            </p>
                            <div className="mt-1 flex items-center gap-2 text-xs text-white/60 flex-wrap">
                              <span className="inline-flex items-center gap-1"><FiFlag className="w-3.5 h-3.5" />{i.priority}</span>
                              <span className="inline-flex items-center gap-1"><FiTag className="w-3.5 h-3.5" />{i.issue_type}</span>
                              {i.assigned_to && <span className="inline-flex items-center gap-1"><FiUser className="w-3.5 h-3.5" />{i.assigned_to}</span>}
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            {statusBadge(i.status)}
                            <span className="text-xs text-white/50 inline-flex items-center gap-1">
                              <FiMessageSquare className="w-3.5 h-3.5" />
                              {i.comment_count || 0}
                            </span>
                          </div>
                        </div>
                      </button>
                    ))}
                    {typeof totalIssues === 'number' && issues.length < totalIssues && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="w-full mt-2 border border-white/15"
                        onClick={loadMoreIssues}
                      >
                        Load more ({issues.length}/{totalIssues})
                      </Button>
                    )}
                  </div>
                )}
              </div>

              <div className="xl:col-span-1 rounded-xl border border-white/10 bg-white/5 p-4 max-h-[64vh] overflow-auto">
                <h3 className="text-white font-medium mb-3">Issue detail</h3>
                {!selectedIssueNumber ? (
                  <p className="text-white/50 text-sm">Select an issue to view details.</p>
                ) : detailLoading ? (
                  <div className="py-10 text-white/70 flex items-center justify-center"><FiLoader className="w-5 h-5 animate-spin mr-2" />Loading issue...</div>
                ) : !selectedIssue ? (
                  <p className="text-white/50 text-sm">Unable to load issue.</p>
                ) : (
                  <div className="space-y-4">
                    <div className="rounded-lg border border-white/10 bg-black/20 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-white font-semibold flex items-center gap-2">
                            <FiHash className="w-4 h-4 text-white/60" />
                            #{selectedIssue.number} {selectedIssue.title}
                          </p>
                          <div className="mt-2 flex items-center gap-2 flex-wrap">
                            {statusBadge(selectedIssue.status)}
                            <Badge variant="default" className="text-[11px]">{selectedIssue.issue_type}</Badge>
                            <Badge variant="default" className="text-[11px]">{selectedIssue.priority}</Badge>
                          </div>
                          <p className="text-xs text-white/50 mt-2">
                            Created by {selectedIssue.created_by || 'unknown'}
                            {selectedIssue.assigned_to ? ` • Assigned to ${selectedIssue.assigned_to}` : ''}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          {selectedIssue.status !== 'closed' && selectedIssue?.permissions?.can_close && (
                            <Button variant="ghost" size="sm" className="text-white/90 border border-white/20" onClick={() => handleTransition('closed')}>
                              <FiCheckCircle className="w-4 h-4 mr-2" />
                              Close
                            </Button>
                          )}
                          {selectedIssue.status === 'closed' && selectedIssue?.permissions?.can_reopen && (
                            <Button variant="ghost" size="sm" className="text-white/90 border border-white/20" onClick={() => handleTransition('open')}>
                              Reopen
                            </Button>
                          )}
                        </div>
                      </div>

                      {selectedIssue.description && (
                        <p className="text-white/80 text-sm mt-3 whitespace-pre-wrap">{selectedIssue.description}</p>
                      )}

                      {Array.isArray(selectedIssue.labels) && selectedIssue.labels.length > 0 && (
                        <div className="mt-3 flex flex-wrap gap-1">
                          {selectedIssue.labels.map((l) => (
                            <Badge key={l.name} variant="default" className="text-[11px]">{l.name}</Badge>
                          ))}
                        </div>
                      )}
                    </div>

                    {selectedIssue.links && (
                      <div className="rounded-lg border border-white/10 bg-black/20 p-3">
                        <p className="text-white/80 text-sm font-medium mb-2">Links</p>
                        <div className="space-y-2 text-xs text-white/70">
                          <div>
                            <p className="text-white/60 mb-1">Branches</p>
                            {(selectedIssue.links.branches || []).length === 0 ? (
                              <p className="text-white/40">—</p>
                            ) : (
                              (selectedIssue.links.branches || []).map((b) => (
                                <p key={b.name} className="font-mono text-white/80">{b.name}</p>
                              ))
                            )}
                          </div>
                          <div>
                            <p className="text-white/60 mb-1">Pull requests</p>
                            {(selectedIssue.links.pull_requests || []).length === 0 ? (
                              <p className="text-white/40">—</p>
                            ) : (
                              (selectedIssue.links.pull_requests || []).map((p) => (
                                <p key={p.id} className="text-white/80">#{p.id} {p.title || ''} <span className="text-white/50">({p.status})</span></p>
                              ))
                            )}
                          </div>
                          <div>
                            <p className="text-white/60 mb-1">Commits</p>
                            {(selectedIssue.links.commits || []).length === 0 ? (
                              <p className="text-white/40">—</p>
                            ) : (
                              (selectedIssue.links.commits || []).slice(0, 8).map((c) => (
                                <p key={c.id} className="font-mono text-white/80">{(c.id || '').slice(0, 12)} {c.message ? `— ${c.message}` : ''}</p>
                              ))
                            )}
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="rounded-lg border border-white/10 bg-black/20 p-3">
                      <p className="text-white/80 text-sm font-medium mb-2">Comments</p>
                      <div className="space-y-3 max-h-48 overflow-auto pr-1">
                        {(selectedIssue.comments || []).length === 0 ? (
                          <p className="text-white/40 text-sm">No comments yet.</p>
                        ) : (
                          (selectedIssue.comments || []).map((c) => (
                            <div key={c.id} className="rounded border border-white/10 bg-white/5 p-2">
                              <p className="text-xs text-white/50">{c.author || 'unknown'} • {c.created_at ? new Date(c.created_at).toLocaleString() : ''}</p>
                              <p className="text-sm text-white/80 whitespace-pre-wrap mt-1">{c.body}</p>
                            </div>
                          ))
                        )}
                      </div>

                      <div className="mt-3 space-y-2">
                        <textarea
                          value={commentBody}
                          onChange={(e) => setCommentBody(e.target.value)}
                          placeholder="Add a comment (supports @mentions)…"
                          className="w-full min-h-20 bg-white/10 border border-white/20 rounded px-3 py-2 text-sm text-white placeholder:text-white/40"
                        />
                        <Button variant="primary" size="sm" onClick={handleAddComment} disabled={commentSubmitting || !commentBody.trim()}>
                          {commentSubmitting ? <><FiLoader className="w-4 h-4 mr-2 animate-spin" />Posting…</> : <>Post comment</>}
                        </Button>
                      </div>
                    </div>

                    <div className="rounded-lg border border-white/10 bg-black/20 p-3">
                      <p className="text-white/80 text-sm font-medium mb-2">Activity</p>
                      <div className="space-y-2 max-h-44 overflow-auto pr-1">
                        {(selectedIssue.events || []).length === 0 ? (
                          <p className="text-white/40 text-sm">No activity recorded.</p>
                        ) : (
                          (selectedIssue.events || []).slice(-50).reverse().map((e) => (
                            <div key={e.id} className="text-xs text-white/70">
                              <span className="text-white/50">{e.created_at ? new Date(e.created_at).toLocaleString() : ''}</span>
                              <span className="mx-2 text-white/30">•</span>
                              <span className="text-white/80">{formatEvent(e)}</span>
                              {e.actor && <span className="text-white/50"> by {e.actor}</span>}
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  )
}

export default IssuesModal

