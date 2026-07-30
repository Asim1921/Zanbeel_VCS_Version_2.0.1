import React, { useState, useEffect } from 'react'
import { FiClock, FiCheck, FiX, FiFolder, FiUser, FiFileText, FiRefreshCw } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
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
          <FiClock className="w-8 h-8 animate-pulse text-yellow-400" />
          <span className="ml-2 text-gray-300">Loading pending repositories...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <FiClock className="w-6 h-6 text-yellow-400" />
          <div>
            <h1 className="text-2xl font-bold text-white">Pending Repository Requests</h1>
            <p className="text-white/70">Review and approve repository creation requests</p>
          </div>
          <Badge variant="warning" className="text-yellow-400 border-yellow-400/30">
            {pendingRepos.length}
          </Badge>
        </div>
        <Button
          variant="secondary"
          onClick={fetchPendingRepositories}
          className="flex items-center space-x-2"
        >
          <FiRefreshCw className="w-4 h-4" />
          <span>Refresh</span>
        </Button>
      </div>

      {/* Reviewer Username Input */}
      <GlassCard className="p-4 bg-blue-500/10 border-blue-500/30">
        <div className="flex items-center space-x-4">
          <FiUser className="w-5 h-5 text-blue-400" />
          <div className="flex-1">
            <label className="block text-sm font-medium text-white/70 mb-2">
              Your Username (required for approvals/rejections)
            </label>
            <input
              type="text"
              value={reviewerUsername}
              onChange={(e) => setReviewerUsername(e.target.value)}
              placeholder="Enter your username"
              className="w-full px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white placeholder-gray-400 focus:outline-none focus:border-blue-500"
            />
          </div>
        </div>
      </GlassCard>

      {/* Search by Team Lead */}
      <GlassCard className="p-4 bg-purple-500/10 border-purple-500/30">
        <div className="flex items-center space-x-4">
          <FiUser className="w-5 h-5 text-purple-400" />
          <div className="flex-1">
            <label className="block text-sm font-medium text-white/70 mb-2">
              Filter by Team Lead
            </label>
            <div className="flex space-x-2">
              <input
                type="text"
                value={searchTeamLead}
                onChange={(e) => handleSearchChange(e.target.value)}
                placeholder="Enter team lead username (leave empty for all)"
                className="flex-1 px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white placeholder-gray-400 focus:outline-none focus:border-purple-500"
              />
              <Button
                variant="primary"
                onClick={handleSearchClick}
                className="px-4"
              >
                Search
              </Button>
              {searchTeamLead && (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setSearchTeamLead('')
                    setTimeout(fetchPendingRepositories, 100)
                  }}
                  className="px-4"
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
        <GlassCard className="p-4 border-red-500/30 bg-red-500/10">
          <div className="flex items-center space-x-2 text-red-400">
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
                  ? 'border-yellow-500/50 bg-yellow-500/10'
                  : 'hover:border-yellow-500/30'
              }`}
              onClick={() => handleRepoClick(repo)}
            >
              <div className="space-y-4">
                {/* Repository Header */}
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-2">
                      <FiFolder className="w-5 h-5 text-yellow-400" />
                      <h3 className="font-semibold text-white">
                        {repo.repo_name}
                      </h3>
                    </div>
                    <div className="flex items-center space-x-4 text-sm text-white/70">
                      <div className="flex items-center space-x-1">
                        <FiUser className="w-4 h-4" />
                        <span>Requested by: {repo.requested_by}</span>
                      </div>
                    </div>
                    {repo.requested_by_full_name && (
                      <div className="mt-1 text-xs text-white/50">
                        {repo.requested_by_full_name}
                      </div>
                    )}
                    {repo.owner && (
                      <div className="mt-2 text-xs text-purple-400 flex items-center space-x-1">
                        <FiUser className="w-3 h-3" />
                        <span>Will be owned by: {repo.owner} ({repo.owner_full_name || 'Team Lead'})</span>
                      </div>
                    )}
                  </div>
                  <Badge variant="warning" className="text-yellow-400 border-yellow-400/30">
                    Pending
                  </Badge>
                </div>

                {/* Repository Description */}
                {repo.description && (
                  <div className="bg-black/30 p-3 rounded-md">
                    <div className="flex items-start space-x-2">
                      <FiFileText className="w-4 h-4 text-blue-400 mt-0.5" />
                      <div className="flex-1">
                        <p className="text-sm text-white/70">{repo.description}</p>
                      </div>
                    </div>
                  </div>
                )}

                {/* Repository Info */}
                <div className="space-y-2 text-sm text-white/70">
                  <div className="flex items-center">
                    <FiClock className="w-4 h-4 mr-2" />
                    <span>
                      Requested {new Date(repo.created_at).toLocaleString()}
                    </span>
                  </div>
                  <div className="text-xs text-white/50 font-mono bg-black/30 p-2 rounded">
                    ID: {repo.id}
                  </div>
                </div>

                {/* Actions (shown when selected) */}
                {selectedRepo?.id === repo.id && (
                  <div
                    className="pt-4 border-t border-white/10 space-y-3"
                    onClick={(e) => e.stopPropagation()}
                    onFocusCapture={(e) => e.stopPropagation()}
                  >
                    <div>
                      <label className="block text-sm font-medium text-white/70 mb-2">
                        Review Comment (optional)
                      </label>
                      <textarea
                        value={reviewComment}
                        onChange={(e) => setReviewComment(e.target.value)}
                        placeholder="Add a comment about this repository request..."
                        rows={3}
                        className="w-full px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white placeholder-gray-400 focus:outline-none focus:border-blue-500 resize-none"
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
                        className="flex-1 flex items-center justify-center space-x-1 bg-green-600/20 text-green-400 hover:bg-green-600/30"
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
                        className="flex-1 flex items-center justify-center space-x-1 bg-red-600/20 text-red-400 hover:bg-red-600/30"
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
        <div className="text-center py-12">
          <FiCheck className="w-16 h-16 text-green-600 mx-auto mb-4" />
          <h3 className="text-xl font-semibold text-white mb-2">All Caught Up!</h3>
          <p className="text-white/70">
            There are no pending repository requests awaiting approval at this time.
          </p>
        </div>
      )}
    </div>
  )
}

export default PendingRepositories
