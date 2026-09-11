import React, { useState, useEffect } from 'react'
import { FiClock, FiCheck, FiX, FiGitCommit, FiUser, FiFolder, FiMessageSquare, FiRefreshCw, FiAlertTriangle } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import PageHeader from '../components/ui/PageHeader'
import EmptyState from '../components/ui/EmptyState'
import api from '../utils/api'
import { API_SERVER_URL } from '../config.js'
import { getSessionToken, getSessionUsername } from '../utils/session'

const PendingApprovals = () => {
  const token = getSessionToken()
  const currentUsername = getSessionUsername()
  const [pendingCommits, setPendingCommits] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedCommit, setSelectedCommit] = useState(null)
  const [reviewComment, setReviewComment] = useState('')
  const [reviewerUsername, setReviewerUsername] = useState(currentUsername || '')
  const [searchTeamLead, setSearchTeamLead] = useState('')
  const isReviewerReady = reviewerUsername.trim().length > 0

  useEffect(() => {
    fetchPendingCommits()
  }, [])

  const fetchPendingCommits = async () => {
    try {
      setLoading(true)
      setError(null)

      const url = searchTeamLead 
        ? `${API_SERVER_URL}/api/pending-commits?status=pending&team_lead_username=${searchTeamLead}`
        : `${API_SERVER_URL}/api/pending-commits?status=pending`
      
      const response = await fetch(url, {
        headers: {
          Authorization: `Bearer ${token}`
        }
      })
      const data = await response.json()

      if (data.success) {
        setPendingCommits(data.pending_commits)
      } else {
        setError('Failed to fetch pending commits')
      }
    } catch (err) {
      setError(`Error connecting to server: ${err.message}`)
      console.error('Error fetching pending commits:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleSearchChange = (value) => {
    setSearchTeamLead(value)
  }

  const handleSearchClick = () => {
    fetchPendingCommits()
  }

  const handleReview = async (commitId, action) => {
    if (!reviewerUsername) {
      alert('Please enter your username')
      return
    }

    if (!confirm(`Are you sure you want to ${action} this commit?`)) {
      return
    }

    try {
      const response = await fetch(`${API_SERVER_URL}/api/pending-commits/${commitId}/review`, {
        method: 'POST',
        headers: { 
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          reviewer_username: reviewerUsername,
          action: action,
          comment: reviewComment || undefined
        })
      })

      const data = await response.json()

      if (data.success) {
        alert(`Commit ${action}d successfully!`)
        setSelectedCommit(null)
        setReviewComment('')
        fetchPendingCommits()
      } else {
        alert(`Failed to ${action} commit`)
      }
    } catch (error) {
      console.error(`Error ${action}ing commit:`, error)
      alert(`Error ${action}ing commit`)
    }
  }

  const handleCommitClick = (commit) => {
    if (selectedCommit?.id === commit.id) {
      setSelectedCommit(null)
      setReviewComment('')
    } else {
      setSelectedCommit(commit)
      setReviewComment('')

      if (!isReviewerReady) {
        const suggestedReviewer = commit.team_lead_name || commit.reviewer_hint || ''
        if (suggestedReviewer) {
          setReviewerUsername(suggestedReviewer)
        }
      }
    }
  }

  const formatServerTime = (value) => {
    if (!value) return 'Unknown time'
    const hasTimezone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value)
    const normalized = value.includes('T') ? value : value.replace(' ', 'T')
    const isoValue = hasTimezone ? normalized : `${normalized}Z`
    const date = new Date(isoValue)
    if (Number.isNaN(date.getTime())) {
      return value
    }
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Karachi',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
      timeZoneName: 'short'
    }).format(date)
  }

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center h-64">
          <FiClock className="w-8 h-8 animate-pulse text-warning-fg" />
          <span className="ml-2 text-ink-soft">Loading pending commits...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      <PageHeader
        eyebrow={
          pendingCommits.length
            ? `${pendingCommits.length} awaiting review`
            : 'Queue clear'
        }
        title="Pending Approvals"
        subtitle="Review and approve commits awaiting team lead approval."
        actions={
          <Button
            variant="secondary"
            onClick={fetchPendingCommits}
            className="flex items-center gap-2"
          >
            <FiRefreshCw className="w-4 h-4" />
            <span>Refresh</span>
          </Button>
        }
      />

      {/* Both inputs belong to the same task, so they share one bar rather than
          stacking as two full-width panels for one field each. */}
      <GlassCard className="p-4">
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="mb-2 flex items-center gap-2 text-sm font-medium text-ink-soft">
              <FiUser className="h-4 w-4 text-accent" />
              Your username
              <span className="text-xs font-normal text-muted">
                (required to approve or reject)
              </span>
            </label>
            <input
              type="text"
              value={reviewerUsername}
              onChange={(e) => setReviewerUsername(e.target.value)}
              placeholder="Enter your username"
              className="w-full rounded-lg border border-border bg-cream-mid px-3 py-2 text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-2 flex items-center gap-2 text-sm font-medium text-ink-soft">
              <FiUser className="h-4 w-4 text-muted" />
              Filter by team lead
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={searchTeamLead}
                onChange={(e) => handleSearchChange(e.target.value)}
                placeholder="Leave empty for all"
                className="min-w-0 flex-1 rounded-lg border border-border bg-cream-mid px-3 py-2 text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
              />
              <Button variant="primary" onClick={handleSearchClick} className="shrink-0 px-4">
                Search
              </Button>
              {searchTeamLead && (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setSearchTeamLead('')
                    setTimeout(fetchPendingCommits, 100)
                  }}
                  className="shrink-0 px-4"
                >
                  Clear
                </Button>
              )}
            </div>
          </div>
        </div>
      </GlassCard>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/20 bg-danger-bg p-4 text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </GlassCard>
      )}

      {/* Pending Commits Grid */}
      {pendingCommits.length > 0 ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {pendingCommits.map((commit) => (
            <GlassCard
              key={commit.id}
              className={`p-6 cursor-pointer transition-all duration-300 hover:scale-105 ${
                selectedCommit?.id === commit.id
                  ? 'border-warning-fg/50 bg-warning-bg'
                  : 'hover:border-warning-fg/20'
              }`}
              onClick={() => handleCommitClick(commit)}
            >
              <div className="space-y-4">
                {/* Commit Header */}
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-2">
                      <FiGitCommit className="w-5 h-5 text-warning-fg" />
                      <h3 className="font-semibold text-ink">
                        {commit.message}
                      </h3>
                    </div>
                    <div className="flex items-center space-x-4 text-sm text-ink-soft">
                      <div className="flex items-center space-x-1">
                        <FiUser className="w-4 h-4" />
                        <span>{commit.author}</span>
                      </div>
                      <div className="flex items-center space-x-1">
                        <FiFolder className="w-4 h-4" />
                        <span>{commit.repository_name}</span>
                      </div>
                    </div>
                    {commit.team_lead_name && (
                      <div className="mt-2 text-xs text-ink flex items-center space-x-1">
                        <FiUser className="w-3 h-3" />
                        <span>Assigned Team Lead: {commit.team_lead_name}</span>
                      </div>
                    )}
                  </div>
                  <Badge variant="warning" className="text-warning-fg border-warning-fg/30">
                    Pending
                  </Badge>
                </div>

                {/* Commit Info */}
                <div className="space-y-2 text-sm text-ink-soft">
                  <div className="flex items-center">
                    <FiClock className="w-4 h-4 mr-2" />
                    <span>
                      Submitted {formatServerTime(commit.created_at)}
                    </span>
                  </div>
                  <div className="text-xs text-muted font-mono bg-cream-deep p-2 rounded">
                    ID: {commit.id.substring(0, 8)}...
                  </div>
                </div>

                {/* Actions (shown when selected) */}
                {selectedCommit?.id === commit.id && (
                  <div
                    className="pt-4 border-t border-border space-y-3"
                    onClick={(e) => e.stopPropagation()}
                    onFocusCapture={(e) => e.stopPropagation()}
                  >
                    <div>
                      <label className="block text-sm font-medium text-ink-soft mb-2">
                        Review Comment (optional)
                      </label>
                      <textarea
                        value={reviewComment}
                        onChange={(e) => setReviewComment(e.target.value)}
                        placeholder="Add a comment about this commit..."
                        rows={3}
                        className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink placeholder:text-muted focus:outline-none focus:border-ink resize-none"
                        onClick={(e) => e.stopPropagation()}
                        onFocus={(e) => e.stopPropagation()}
                      />
                    </div>
                    <div className="flex items-center space-x-2">
                      <Button
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation()
                          handleReview(commit.id, 'approve')
                        }}
                        disabled={!isReviewerReady}
                        className="flex-1 flex items-center justify-center space-x-1 bg-success-bg text-success-fg hover:bg-success-fg/30"
                        title={isReviewerReady ? 'Approve this commit' : 'Enter your username above to enable'}
                      >
                        <FiCheck className="w-4 h-4" />
                        <span>Approve & Merge</span>
                      </Button>
                      <Button
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation()
                          handleReview(commit.id, 'reject')
                        }}
                        disabled={!isReviewerReady}
                        className="flex-1 flex items-center justify-center space-x-1 bg-danger-fg/20 text-danger-fg hover:bg-danger-fg/30"
                        title={isReviewerReady ? 'Reject this commit' : 'Enter your username above to enable'}
                      >
                        <FiX className="w-4 h-4" />
                        <span>Reject</span>
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            </GlassCard>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={FiCheck}
          title="All caught up"
          description="There are no pending commits awaiting approval at this time."
        />
      )}
    </div>
  )
}

export default PendingApprovals
