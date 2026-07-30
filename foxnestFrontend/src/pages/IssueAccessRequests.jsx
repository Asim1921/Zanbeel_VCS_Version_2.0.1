import React, { useState, useEffect } from 'react'
import { FiClock, FiCheck, FiX, FiUser, FiFolder, FiGitBranch, FiAlertCircle, FiRefreshCw } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import { API_SERVER_URL } from '../config.js'
import { getSessionToken, getSessionUsername } from '../utils/session'

const IssueAccessRequests = () => {
  const token = getSessionToken()
  const currentUsername = getSessionUsername()
  const [requests, setRequests] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedRequest, setSelectedRequest] = useState(null)
  const [reviewComment, setReviewComment] = useState('')

  useEffect(() => {
    fetchAccessRequests()
  }, [])

  const fetchAccessRequests = async () => {
    try {
      setLoading(true)
      setError(null)

      const response = await fetch(`${API_SERVER_URL}/api/access-requests/pending`, {
        headers: {
          Authorization: `Bearer ${token}`
        }
      })
      const data = await response.json()

      if (data.success) {
        setRequests(data.requests || data.access_requests || [])
      } else {
        setError(data.detail || 'Failed to fetch access requests')
      }
    } catch (err) {
      setError(`Error connecting to server: ${err.message}`)
      console.error('Error fetching access requests:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleRefresh = () => {
    fetchAccessRequests()
  }

  const handleApprove = async (requestId) => {
    if (!confirm('Approve this repository access request?')) {
      return
    }

    try {
      const response = await fetch(`${API_SERVER_URL}/api/access-requests/${requestId}/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          comment: reviewComment
        })
      })
      const data = await response.json()

      if (data.success) {
        alert('Access request approved!')
        setReviewComment('')
        setSelectedRequest(null)
        fetchAccessRequests()
      } else {
        alert(`Failed to approve: ${data.detail || 'Unknown error'}`)
      }
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  const handleDeny = async (requestId) => {
    if (!confirm('Deny this repository access request?')) {
      return
    }

    try {
      const response = await fetch(`${API_SERVER_URL}/api/access-requests/${requestId}/deny`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          comment: reviewComment
        })
      })
      const data = await response.json()

      if (data.success) {
        alert('Access request denied.')
        setReviewComment('')
        setSelectedRequest(null)
        fetchAccessRequests()
      } else {
        alert(`Failed to deny: ${data.detail || 'Unknown error'}`)
      }
    } catch (err) {
      alert(`Error: ${err.message}`)
    }
  }

  if (!token) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <span className="text-gray-400">Please log in first</span>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <FiRefreshCw className="w-8 h-8 text-orange-400 animate-spin mr-3" />
        <span className="ml-2 text-gray-300">Loading access requests...</span>
      </div>
    )
  }

  return (
    <div className="min-h-screen p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <h1 className="text-3xl font-bold text-white flex items-center">
            <FiAlertCircle className="mr-3 text-orange-400" />
            Issue Access Requests
          </h1>
          <Button onClick={handleRefresh} variant="secondary" size="sm">
            <FiRefreshCw className="mr-2" /> Refresh
          </Button>
        </div>

        {error && (
          <div className="bg-red-900/20 border border-red-500 rounded-lg p-4 mb-6 text-red-200">
            {error}
          </div>
        )}

        {requests.length === 0 ? (
          <GlassCard className="p-8">
            <div className="text-center">
              <FiCheck className="w-12 h-12 text-green-400 mx-auto mb-4" />
              <h2 className="text-xl font-semibold text-white mb-2">All Caught Up!</h2>
              <p className="text-gray-300">There are no pending access requests at this time.</p>
            </div>
          </GlassCard>
        ) : (
          <div className="grid gap-6">
            {requests.map((request) => (
              <GlassCard 
                key={request.id}
                className={`p-6 cursor-pointer transition ${selectedRequest?.id === request.id ? 'ring-2 ring-orange-400' : ''}`}
                onClick={() => setSelectedRequest(selectedRequest?.id === request.id ? null : request)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-4 mb-3">
                      <FiUser className="w-5 h-5 text-blue-400" />
                      <span className="font-semibold text-white">@{request.requested_user.username || 'Unknown'}</span>
                      <Badge variant="warning">Pending</Badge>
                    </div>

                    <div className="grid grid-cols-2 gap-4 mt-4 text-sm">
                      <div>
                        <p className="text-gray-400">Issue Number</p>
                        <p className="text-white font-mono">#{request.issue_id}</p>
                      </div>
                      <div>
                        <p className="text-gray-400">Repository</p>
                        <p className="text-white">
                          <FiFolder className="inline mr-1" />
                          {request.repository.name || request.repository_id}
                        </p>
                      </div>
                      <div>
                        <p className="text-gray-400">Requested By</p>
                        <p className="text-white">@{request.requested_by.username || 'Unknown'}</p>
                      </div>
                      <div>
                        <p className="text-gray-400">Request Reason</p>
                        <p className="text-white">{request.request_reason || 'No reason provided'}</p>
                      </div>
                    </div>

                    {request.request_reason && (
                      <div className="mt-4">
                        <p className="text-gray-400 mb-2">Details:</p>
                        <div className="bg-black/30 rounded p-3 text-gray-200">
                          {request.request_reason}
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {selectedRequest?.id === request.id && (
                  <div className="mt-6 pt-6 border-t border-gray-700">
                    <div className="mb-4">
                      <label className="block text-sm font-medium text-gray-300 mb-2">
                        Review Comment (Optional)
                      </label>
                      <textarea
                        value={reviewComment}
                        onChange={(e) => setReviewComment(e.target.value)}
                        placeholder="Enter your comment..."
                        className="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded text-white placeholder-gray-500 focus:outline-none focus:border-orange-500"
                        rows="3"
                      />
                    </div>

                    <div className="flex gap-3 justify-end">
                      <Button 
                        variant="danger"
                        size="sm"
                        onClick={() => handleDeny(request.id)}
                      >
                        <FiX className="mr-2" /> Deny
                      </Button>
                      <Button 
                        variant="success"
                        size="sm"
                        onClick={() => handleApprove(request.id)}
                      >
                        <FiCheck className="mr-2" /> Approve
                      </Button>
                    </div>
                  </div>
                )}
              </GlassCard>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default IssueAccessRequests
