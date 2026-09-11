import React, { useState, useEffect } from 'react'
import api from '../utils/api'
import GlassCard from '../components/ui/GlassCard'
import Button from '../components/ui/Button'
import PageHeader from '../components/ui/PageHeader'
import FadeContent from '../components/react-bits/FadeContent'

export default function PasswordReset() {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedUser, setSelectedUser] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const fetchUsers = async () => {
      try {
        const response = await api.getUsers()
        if (response.success) {
          setUsers(response.users || [])
        }
      } catch (err) {
        console.error('Failed to fetch users:', err)
        setError('Failed to load users')
      } finally {
        setLoading(false)
      }
    }

    fetchUsers()
  }, [])

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setSuccess('')

    if (!selectedUser) {
      setError('Please select a user')
      return
    }

    if (!newPassword) {
      setError('Please enter a new password')
      return
    }

    if (newPassword !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    if (newPassword.length < 6) {
      setError('Password must be at least 6 characters')
      return
    }

    setSubmitting(true)
    try {
      const response = await api.resetPassword(selectedUser, newPassword)
      if (response.success) {
        setSuccess(`Password reset successfully for ${selectedUser}`)
        setNewPassword('')
        setConfirmPassword('')
        setSelectedUser('')
      } else {
        setError(response.message || 'Failed to reset password')
      }
    } catch (err) {
      setError('An error occurred while resetting password')
      console.error(err)
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <p className="text-muted">Loading users…</p>
      </div>
    )
  }

  return (
    <FadeContent className="mx-auto max-w-xl">
      <PageHeader
        title="Password Reset"
        subtitle="Set a new password for any user account."
      />

      <GlassCard className="p-6" hover={false}>
        {error && (
          <div className="mb-4 rounded-xl border border-danger-fg/20 bg-danger-bg px-4 py-3 text-sm text-danger-fg">
            {error}
          </div>
        )}

        {success && (
          <div className="mb-4 rounded-xl border border-success-fg/20 bg-success-bg px-4 py-3 text-sm text-success-fg">
            {success}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium text-ink">Select user</span>
            <select
              value={selectedUser}
              onChange={(e) => setSelectedUser(e.target.value)}
              className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none focus:border-ink focus:ring-2 focus:ring-ink/10"
            >
              <option value="">— Choose a user —</option>
              {users.map((user) => (
                <option key={user.id} value={user.username}>
                  {user.username} ({user.role})
                </option>
              ))}
            </select>
          </label>

          <label className="block space-y-1.5">
            <span className="text-sm font-medium text-ink">New password</span>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10"
              placeholder="Enter new password"
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-sm font-medium text-ink">Confirm password</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10"
              placeholder="Confirm new password"
            />
          </label>

          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? 'Resetting…' : 'Reset password'}
          </Button>
        </form>
      </GlassCard>
    </FadeContent>
  )
}
