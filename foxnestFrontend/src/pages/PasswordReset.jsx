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
  const [otp, setOtp] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [sending, setSending] = useState(false)
  // Set once a code has been emailed; until then there is nothing to verify against.
  const [codeSentTo, setCodeSentTo] = useState('')

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

  const handleSendCode = async () => {
    setError('')
    setSuccess('')

    if (!selectedUser) {
      setError('Please select a user')
      return
    }

    setSending(true)
    try {
      const response = await api.requestPasswordResetOtp(selectedUser)
      if (response.success) {
        setCodeSentTo(response.sent_to || '')
        setOtp('')
        setSuccess(response.message || 'A verification code has been emailed.')
      } else {
        setError(response.message || 'Failed to send the verification code')
      }
    } catch (err) {
      setError(err.message || 'Could not send the verification code')
    } finally {
      setSending(false)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setSuccess('')

    if (!selectedUser) {
      setError('Please select a user')
      return
    }

    if (!codeSentTo) {
      setError('Send a verification code first')
      return
    }

    if (!/^\d{6}$/.test(otp.trim())) {
      setError('Enter the 6-digit code from the email')
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
      const response = await api.resetPassword(selectedUser, newPassword, otp.trim())
      if (response.success) {
        setSuccess(`Password reset successfully for ${selectedUser}`)
        setNewPassword('')
        setConfirmPassword('')
        setSelectedUser('')
        setOtp('')
        setCodeSentTo('')
      } else {
        setError(response.message || 'Failed to reset password')
      }
    } catch (err) {
      // The server explains why (wrong code, expired, too many attempts) -- show that
      // rather than a generic message the admin cannot act on.
      setError(err.message || 'An error occurred while resetting password')
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
              onChange={(e) => {
                setSelectedUser(e.target.value)
                // A code is bound to one account, so changing the account voids it.
                setCodeSentTo('')
                setOtp('')
              }}
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

          <div className="space-y-1.5">
            <Button
              type="button"
              onClick={handleSendCode}
              disabled={sending || !selectedUser}
              className="w-full"
            >
              {sending ? 'Sending code…' : codeSentTo ? 'Resend verification code' : 'Send verification code'}
            </Button>
            <p className="text-xs text-muted">
              {codeSentTo
                ? `A 6-digit code was emailed to ${codeSentTo}. Enter it below to confirm the reset.`
                : 'A 6-digit code is emailed to the account holder before the password can be changed.'}
            </p>
          </div>

          <label className="block space-y-1.5">
            <span className="text-sm font-medium text-ink">Verification code</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, ''))}
              disabled={!codeSentTo}
              className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm tracking-[0.4em] text-ink outline-none placeholder:tracking-normal placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10 disabled:opacity-50"
              placeholder="6-digit code"
            />
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

          <Button type="submit" className="w-full" disabled={submitting || !codeSentTo}>
            {submitting ? 'Resetting…' : 'Reset password'}
          </Button>
        </form>
      </GlassCard>
    </FadeContent>
  )
}
