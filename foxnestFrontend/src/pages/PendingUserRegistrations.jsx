import React, { useEffect, useState } from 'react'
import { FiUserPlus, FiCheck, FiX, FiRefreshCw } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import Badge from '../components/ui/Badge'
import api from '../utils/api'

const PendingUserRegistrations = () => {
  const [requests, setRequests] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedRequest, setSelectedRequest] = useState(null)
  const [reviewComment, setReviewComment] = useState('')
  const [reviewRole, setReviewRole] = useState('developer')

  const fetchRequests = async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listPendingUserRegistrations('pending')
      if (response.success) {
        setRequests(response.pending_registrations || [])
      } else {
        setError('Failed to load pending registration requests')
      }
    } catch (err) {
      setError(err.message || 'Failed to load pending registration requests')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchRequests()
  }, [])

  const handleSelect = (request) => {
    if (selectedRequest?.id === request.id) {
      setSelectedRequest(null)
      setReviewComment('')
      return
    }
    setSelectedRequest(request)
    setReviewComment('')
    setReviewRole(request.requested_role || 'developer')
  }

  const handleReview = async (action) => {
    if (!selectedRequest) return

    if (!window.confirm(`Are you sure you want to ${action} this registration request?`)) {
      return
    }

    try {
      const payload = {
        action,
        comment: reviewComment || undefined,
        role: reviewRole,
      }

      const response = await api.reviewPendingUserRegistration(selectedRequest.id, payload)
      if (response.success) {
        setSelectedRequest(null)
        setReviewComment('')
        await fetchRequests()
      } else {
        alert(`Failed to ${action} request`)
      }
    } catch (err) {
      alert(err.message || `Failed to ${action} request`)
    }
  }

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center h-64">
          <FiUserPlus className="w-8 h-8 animate-pulse text-blue-400" />
          <span className="ml-2 text-gray-300">Loading pending user registrations...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <FiUserPlus className="w-6 h-6 text-blue-400" />
          <div>
            <h1 className="text-2xl font-bold text-white">Pending User Registrations</h1>
            <p className="text-white/70">Approve or reject self-registration requests</p>
          </div>
          <Badge variant="info">{requests.length}</Badge>
        </div>
        <Button variant="secondary" onClick={fetchRequests} className="flex items-center space-x-2">
          <FiRefreshCw className="w-4 h-4" />
          <span>Refresh</span>
        </Button>
      </div>

      {error && (
        <GlassCard className="p-4 border-red-500/30 bg-red-500/10 text-red-300">
          {error}
        </GlassCard>
      )}

      {requests.length === 0 ? (
        <GlassCard className="p-10 text-center text-white/70">
          No pending user registration requests.
        </GlassCard>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {requests.map((request) => (
            <GlassCard
              key={request.id}
              className={`p-6 cursor-pointer transition-all ${selectedRequest?.id === request.id ? 'border-blue-500/50 bg-blue-500/10' : 'hover:border-blue-500/30'}`}
              onClick={() => handleSelect(request)}
            >
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-lg font-semibold text-white">{request.username}</h3>
                  <Badge variant="warning">Pending</Badge>
                </div>
                <p className="text-white/70 text-sm">{request.full_name || 'No full name provided'}</p>
                <p className="text-white/60 text-sm">{request.email || 'No email provided'}</p>
                <p className="text-white/60 text-sm">Requested role: {request.requested_role}</p>
                {request.requested_team_lead_username && (
                  <p className="text-white/60 text-sm">Requested team lead: {request.requested_team_lead_username}</p>
                )}
                <p className="text-white/50 text-xs">Requested at: {new Date(request.created_at).toLocaleString()}</p>

                {selectedRequest?.id === request.id && (
                  <div
                    className="space-y-3 pt-2 border-t border-white/10"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <div>
                      <label className="block text-sm text-white/70 mb-1">Assign role on approval</label>
                      <select
                        value={reviewRole}
                        onChange={(e) => setReviewRole(e.target.value)}
                        onClick={(event) => event.stopPropagation()}
                        className="w-full px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white"
                      >
                        <option value="developer">Developer</option>
                        <option value="team_lead">Team Lead</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-sm text-white/70 mb-1">Review comment (optional)</label>
                      <textarea
                        value={reviewComment}
                        onChange={(e) => setReviewComment(e.target.value)}
                        onClick={(event) => event.stopPropagation()}
                        rows={3}
                        className="w-full px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white"
                        placeholder="Comment for applicant"
                      />
                    </div>
                    <div className="flex items-center space-x-2">
                      <Button onClick={(event) => { event.stopPropagation(); handleReview('approve') }} className="flex items-center space-x-1 bg-green-600/20 text-green-300 hover:bg-green-600/30">
                        <FiCheck className="w-4 h-4" />
                        <span>Approve</span>
                      </Button>
                      <Button onClick={(event) => { event.stopPropagation(); handleReview('reject') }} className="flex items-center space-x-1 bg-red-600/20 text-red-300 hover:bg-red-600/30">
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
      )}
    </div>
  )
}

export default PendingUserRegistrations
