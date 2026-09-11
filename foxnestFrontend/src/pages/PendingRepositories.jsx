import React, { useState, useEffect } from 'react'
import { FiClock, FiCheck, FiX, FiFolder, FiUser, FiFileText, FiRefreshCw } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import PageHeader from '../components/ui/PageHeader'
import EmptyState from '../components/ui/EmptyState'
import { API_SERVER_URL } from '../config.js'
import { getSessionToken, getSessionUsername } from '../utils/session'

const PendingRepositories = () => {
  const token = getSessionToken()
  const currentUsername = getSessionUsername()
  const [pendingRepos, setPendingRepos] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedRepo, setSelectedRepo] = useState(null)
  const [reviewComment, setReviewComment] = useState('')
  const [reviewerUsername, setReviewerUsername] = useState(currentUsername || '')
  const [searchTeamLead, setSearchTeamLead] = useState('')
  const isReviewerReady = reviewerUsername.trim().length > 0

  useEffect(() => {
    fetchPendingRepositories()
  }, [])

  const fetchPendingRepositories = async () => {
    try {
      setLoading(true)
      setError(null)

      const url = searchTeamLead 
        ? `${API_SERVER_URL}/api/pending-repositories?status=pending&team_lead_username=${searchTeamLead}`
        : `${API_SERVER_URL}/api/pending-repositories?status=pending`
      
      const response = await fetch(url, {
        headers: {
          Authorization: `Bearer ${token}`
        }
      })
      const data = await response.json()

      if (data.success) {
        setPendingRepos(data.pending_repositories)
      } else {
        setError('Failed to fetch pending repositories')
      }
    } catch (err) {
      setError(`Error connecting to server: ${err.message}`)
      console.error('Error fetching pending repositories:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleSearchChange = (value) => {
    setSearchTeamLead(value)
  }

  const handleSearchClick = () => {
    fetchPendingRepositories()
  }

  const handleReview = async (repoId, action) => {
    if (!reviewerUsername) {
      alert('Please enter your username')
      return
    }

    if (!confirm(`Are you sure you want to ${action} this repository request?`)) {
      return
    }

    try {
      const response = await fetch(`${API_SERVER_URL}/api/pending-repositories/${repoId}/review`, {
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
        alert(`Repository request ${action}d successfully!`)
        setSelectedRepo(null)
        setReviewComment('')
        fetchPendingRepositories()
      } else {
        alert(`Failed to ${action} repository request`)
      }
    } catch (error) {
      console.error(`Error ${action}ing repository:`, error)
      alert(`Error ${action}ing repository request`)
    }
  }

  const handleRepoClick = (repo) => {
    if (selectedRepo?.id === repo.id) {
      setSelectedRepo(null)
      setReviewComment('')
    } else {
      setSelectedRepo(repo)
      setReviewComment('')

      if (!isReviewerReady) {
        const suggestedReviewer = repo.owner || ''
        if (suggestedReviewer) {
          setReviewerUsername(suggestedReviewer)
        }
      }
    }
  }

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center h-64">
          <FiClock className="w-8 h-8 animate-pulse text-warning-fg" />
          <span className="ml-2 text-ink-soft">Loading pending repositories...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      <PageHeader
        eyebrow={
          pendingRepos.length ? `${pendingRepos.length} awaiting review` : 'Queue clear'
        }
        title="Pending Repository Requests"
        subtitle="Review and approve repository creation requests."
        actions={
          <Button
            variant="secondary"
            onClick={fetchPendingRepositories}
            className="flex items-center gap-2"
          >
            <FiRefreshCw className="w-4 h-4" />
            <span>Refresh</span>
          </Button>
        }
      />

      {/* One bar for both inputs — see PendingApprovals, same reviewing task. */}
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
                    setTimeout(fetchPendingRepositories, 100)
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

      {/* Connection Status */}
      {error && (
        <GlassCard className="p-4 border-danger-fg/20 bg-danger-bg">
          <div className="flex items-center space-x-2 text-danger-fg">
            <span>⚠️</span>
            <span>{error}</span>
          </div>
        </GlassCard>
      )}

      {/* Pending Repositories Grid */}
      {pendingRepos.length > 0 ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {pendingRepos.map((repo) => (
            <GlassCard
              key={repo.id}
              className={`p-6 cursor-pointer transition-all duration-300 hover:scale-105 ${
                selectedRepo?.id === repo.id
                  ? 'border-warning-fg/50 bg-warning-bg'
                  : 'hover:border-warning-fg/20'
              }`}
              onClick={() => handleRepoClick(repo)}
            >
              <div className="space-y-4">
                {/* Repository Header */}
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-2">
                      <FiFolder className="w-5 h-5 text-warning-fg" />
                      <h3 className="font-semibold text-ink">
                        {repo.repo_name}
                      </h3>
                    </div>
                    <div className="flex items-center space-x-4 text-sm text-ink-soft">
                      <div className="flex items-center space-x-1">
                        <FiUser className="w-4 h-4" />
                        <span>Requested by: {repo.requested_by}</span>
                      </div>
                    </div>
                    {repo.requested_by_full_name && (
                      <div className="mt-1 text-xs text-muted">
                        {repo.requested_by_full_name}
                      </div>
                    )}
                    {repo.owner && (
                      <div className="mt-2 text-xs text-ink flex items-center space-x-1">
                        <FiUser className="w-3 h-3" />
                        <span>Will be owned by: {repo.owner} ({repo.owner_full_name || 'Team Lead'})</span>
                      </div>
                    )}
                  </div>
                  <Badge variant="warning" className="text-warning-fg border-warning-fg/30">
                    Pending
                  </Badge>
                </div>

                {/* Repository Description */}
                {repo.description && (
                  <div className="bg-cream-deep p-3 rounded-md">
                    <div className="flex items-start space-x-2">
                      <FiFileText className="w-4 h-4 text-info-fg mt-0.5" />
                      <div className="flex-1">
                        <p className="text-sm text-ink-soft">{repo.description}</p>
                      </div>
                    </div>
                  </div>
                )}

                {/* Repository Info */}
                <div className="space-y-2 text-sm text-ink-soft">
                  <div className="flex items-center">
                    <FiClock className="w-4 h-4 mr-2" />
                    <span>
                      Requested {new Date(repo.created_at).toLocaleString()}
                    </span>
                  </div>
                  <div className="text-xs text-muted font-mono bg-cream-deep p-2 rounded">
                    ID: {repo.id}
                  </div>
                </div>

                {/* Actions (shown when selected) */}
                {selectedRepo?.id === repo.id && (
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
                        placeholder="Add a comment about this repository request..."
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
                          handleReview(repo.id, 'approve')
                        }}
                        disabled={!isReviewerReady}
                        className="flex-1 flex items-center justify-center space-x-1 bg-success-bg text-success-fg hover:bg-success-fg/30"
                        title={isReviewerReady ? 'Approve this repository' : 'Enter your username above to enable'}
                      >
                        <FiCheck className="w-4 h-4" />
                        <span>Approve & Create</span>
                      </Button>
                      <Button
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation()
                          handleReview(repo.id, 'reject')
                        }}
                        disabled={!isReviewerReady}
                        className="flex-1 flex items-center justify-center space-x-1 bg-danger-fg/20 text-danger-fg hover:bg-danger-fg/30"
                        title={isReviewerReady ? 'Reject this request' : 'Enter your username above to enable'}
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
          description="There are no pending repository requests awaiting approval at this time."
        />
      )}
    </div>
  )
}

export default PendingRepositories
