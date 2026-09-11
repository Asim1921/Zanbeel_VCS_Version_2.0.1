import React, { useEffect, useState } from 'react'
import { FiUserPlus, FiCheck, FiX, FiRefreshCw } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import EmptyState from '../components/ui/EmptyState'
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
          <FiUserPlus className="w-8 h-8 animate-pulse text-info-fg" />
          <span className="ml-2 text-ink-soft">Loading pending user registrations...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <PageHeader
        eyebrow={requests.length ? `${requests.length} awaiting review` : 'Queue clear'}
        title="Pending User Registrations"
        subtitle="Approve or reject self-registration requests."
        actions={
          <Button variant="secondary" onClick={fetchRequests} className="flex items-center gap-2">
            <FiRefreshCw className="w-4 h-4" />
            <span>Refresh</span>
          </Button>
        }
      />

      {error && (
        <GlassCard className="p-4 mb-6 border-danger-fg/20 bg-danger-bg text-danger-fg">
          {error}
        </GlassCard>
      )}

      {requests.length === 0 ? (
        <EmptyState
          icon={FiUserPlus}
          title="No requests waiting"
          description="Self-registration requests will appear here as soon as somebody signs up."
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {requests.map((request) => (
            <GlassCard
              key={request.id}
              className={`p-6 cursor-pointer transition-all ${selectedRequest?.id === request.id ? 'border-info-fg/25 bg-info-bg' : 'hover:border-info-fg/20'}`}
              onClick={() => handleSelect(request)}
            >
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-lg font-semibold text-ink">{request.username}</h3>
                  <Badge variant="warning">Pending</Badge>
                </div>
                <p className="text-ink-soft text-sm">{request.full_name || 'No full name provided'}</p>
                <p className="text-muted text-sm">{request.email || 'No email provided'}</p>
                <p className="text-muted text-sm">Requested role: {request.requested_role}</p>
                {request.requested_team_lead_username && (
                  <p className="text-muted text-sm">Requested team lead: {request.requested_team_lead_username}</p>
                )}
                <p className="text-muted text-xs">Requested at: {new Date(request.created_at).toLocaleString()}</p>

                {selectedRequest?.id === request.id && (
                  <div
                    className="space-y-3 pt-2 border-t border-border"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <div>
                      <label className="block text-sm text-ink-soft mb-1">Assign role on approval</label>
                      <select
                        value={reviewRole}
                        onChange={(e) => setReviewRole(e.target.value)}
                        onClick={(event) => event.stopPropagation()}
                        className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink"
                      >
                        <option value="developer">Developer</option>
                        <option value="team_lead">Team Lead</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-sm text-ink-soft mb-1">Review comment (optional)</label>
                      <textarea
                        value={reviewComment}
                        onChange={(e) => setReviewComment(e.target.value)}
                        onClick={(event) => event.stopPropagation()}
                        rows={3}
                        className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink"
                        placeholder="Comment for applicant"
                      />
                    </div>
                    <div className="flex items-center space-x-2">
                      <Button onClick={(event) => { event.stopPropagation(); handleReview('approve') }} className="flex items-center space-x-1 bg-success-bg text-success-fg hover:bg-success-fg/30">
                        <FiCheck className="w-4 h-4" />
                        <span>Approve</span>
                      </Button>
                      <Button onClick={(event) => { event.stopPropagation(); handleReview('reject') }} className="flex items-center space-x-1 bg-danger-fg/20 text-danger-fg hover:bg-danger-fg/30">
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
