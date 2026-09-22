import React, { useState } from 'react'
import { FiLock, FiUser, FiEye, FiEyeOff, FiMail } from 'react-icons/fi'
import Button from '../components/ui/Button'
import GlassCard from '../components/ui/GlassCard'
import BackgroundBeams from '../components/aceternity/BackgroundBeams'
import DotBackground from '../components/aceternity/DotBackground'
import MovingBorder from '../components/aceternity/MovingBorder'
import BlurText from '../components/react-bits/BlurText'
import FadeContent from '../components/react-bits/FadeContent'
import BrandLogo from '../components/ui/BrandLogo'
import api from '../utils/api'

const fieldClass =
  'flex items-center gap-2 rounded-xl border border-border bg-white/[0.04] px-3.5 transition-all duration-200 ' +
  'hover:border-border-strong focus-within:border-accent/70 focus-within:bg-white/[0.06] focus-within:ring-4 focus-within:ring-accent/10'
const inputClass =
  'w-full bg-transparent py-2.5 text-sm text-ink outline-none placeholder:text-muted'
const labelClass = 'mb-2 block text-[13px] font-medium text-ink-soft'

const Login = ({ onLoginSuccess }) => {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [forgotMode, setForgotMode] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [otp, setOtp] = useState('')
  // Only after a code has been sent is there anything to verify against.
  const [otpSent, setOtpSent] = useState(false)
  const [registerMode, setRegisterMode] = useState(false)
  const [registrationForm, setRegistrationForm] = useState({
    username: '',
    password: '',
    email: '',
    full_name: '',
    requested_role: 'developer',
    requested_team_lead_username: '',
  })
  const [error, setError] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [showRegisterPassword, setShowRegisterPassword] = useState(false)
  const [showNewPassword, setShowNewPassword] = useState(false)

  const handleSubmit = async (event) => {
    event.preventDefault()
    setError('')
    setSuccessMessage('')
    if (!username.trim() || !password) {
      setError('Username and password are required')
      return
    }
    try {
      setLoading(true)
      const response = await api.login(username.trim(), password)
      if (response.success) onLoginSuccess(response.user)
      else setError('Login failed')
    } catch (err) {
      setError(err.message || 'Unable to login')
    } finally {
      setLoading(false)
    }
  }

  const handleSendResetCode = async (event) => {
    if (event) event.preventDefault()
    setError('')
    setSuccessMessage('')
    if (!forgotEmail.trim()) {
      setError('Enter the email address on your account')
      return
    }
    try {
      setLoading(true)
      const response = await api.forgotPassword(forgotEmail.trim())
      // The reply is deliberately the same whether or not the address is registered,
      // so there is nothing here to branch on.
      setOtpSent(true)
      setSuccessMessage(response.message || 'If that email has an account, a code has been sent.')
    } catch (err) {
      setError(err.message || 'Unable to send a reset code')
    } finally {
      setLoading(false)
    }
  }

  const handleResetWithOtp = async (event) => {
    event.preventDefault()
    setError('')
    setSuccessMessage('')
    if (!/^\d{6}$/.test(otp.trim())) {
      setError('Enter the 6-digit code from the email')
      return
    }
    if (newPassword.length < 8) {
      setError('New password must be at least 8 characters')
      return
    }
    try {
      setLoading(true)
      const response = await api.resetPasswordWithOtp(forgotEmail.trim(), otp.trim(), newPassword)
      if (response.success) {
        setSuccessMessage(response.message || 'Password updated. You can now sign in.')
        setForgotMode(false)
        setOtpSent(false)
        setForgotEmail('')
        setOtp('')
        setNewPassword('')
        setPassword('')
      } else setError('Password reset failed')
    } catch (err) {
      setError(err.message || 'Unable to reset password')
    } finally {
      setLoading(false)
    }
  }

  const handleRegisterRequest = async (event) => {
    event.preventDefault()
    setError('')
    setSuccessMessage('')
    if (!registrationForm.username.trim() || !registrationForm.password) {
      setError('Username and password are required')
      return
    }
    if (registrationForm.password.length < 8) {
      setError('Password must be at least 8 characters')
      return
    }
    try {
      setLoading(true)
      const response = await api.requestRegistration({
        username: registrationForm.username.trim(),
        password: registrationForm.password,
        email: registrationForm.email || null,
        full_name: registrationForm.full_name || null,
        requested_role: registrationForm.requested_role,
        requested_team_lead_username: registrationForm.requested_team_lead_username || null,
      })
      if (response.success) {
        setSuccessMessage('Registration request submitted. Please wait for admin approval.')
        setRegisterMode(false)
        setRegistrationForm({
          username: '',
          password: '',
          email: '',
          full_name: '',
          requested_role: 'developer',
          requested_team_lead_username: '',
        })
      } else setError('Unable to submit registration request')
    } catch (err) {
      setError(err.message || 'Unable to submit registration request')
    } finally {
      setLoading(false)
    }
  }

  const modeTitle = registerMode
    ? 'Request access'
    : forgotMode
      ? 'Reset password'
      : 'Welcome back'

  return (
    <div className="app-canvas relative flex min-h-screen items-center justify-center overflow-hidden p-6">
      <DotBackground />
      <BackgroundBeams className="opacity-40" />
      <FadeContent className="relative z-10 w-full max-w-[26rem]">
        <div className="mb-9 text-center">
          <div className="relative mx-auto mb-6 w-fit">
            <div
              className="animate-glow-pulse absolute inset-0 rounded-3xl bg-accent/30 blur-2xl"
              aria-hidden
            />
            <BrandLogo
              size={64}
              className="relative justify-center"
              imgClassName="ring-1 ring-accent/40"
            />
          </div>
          <h1 className="font-display text-[2.6rem] font-light leading-[1.05] tracking-tight md:text-[3rem]">
            <span className="text-gradient">
              <BlurText text="Zanbeel" />
            </span>
          </h1>
          <p className="mx-auto mt-4 max-w-[19rem] text-[15px] leading-relaxed text-ink-soft">
            Version control that keeps your history honest.
          </p>
          <div className="mt-7 flex items-center justify-center gap-3">
            <span className="h-px w-10 bg-gradient-to-r from-transparent to-border-strong" />
            <span className="font-mono text-[10.5px] uppercase tracking-[0.22em] text-accent">
              {modeTitle}
            </span>
            <span className="h-px w-10 bg-gradient-to-l from-transparent to-border-strong" />
          </div>
        </div>

        <GlassCard className="panel-float-lg hairline-top p-8" hover={false}>
          <form
            onSubmit={
              registerMode
                ? handleRegisterRequest
                : forgotMode
                  ? (otpSent ? handleResetWithOtp : handleSendResetCode)
                  : handleSubmit
            }
            className="space-y-4"
          >
            {/* Resetting is keyed on the email alone -- asking for a username as well
                would only be another thing to get wrong. */}
            {!forgotMode && (
              <div>
                <label className={labelClass}>Username</label>
                <div className={fieldClass}>
                  <FiUser className="text-muted" />
                  <input
                    type="text"
                    value={registerMode ? registrationForm.username : username}
                    onChange={(e) => {
                      if (registerMode) {
                        setRegistrationForm({ ...registrationForm, username: e.target.value })
                      } else setUsername(e.target.value)
                    }}
                    className={inputClass}
                    placeholder="Enter username"
                    autoComplete="username"
                  />
                </div>
              </div>
            )}

            {!forgotMode && !registerMode && (
              <>
                <div>
                  <label className={labelClass}>Password</label>
                  <div className={fieldClass}>
                    <FiLock className="text-muted" />
                    <input
                      type={showPassword ? 'text' : 'password'}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className={inputClass}
                      placeholder="Enter password"
                      autoComplete="current-password"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((v) => !v)}
                      className="rounded-lg p-1.5 text-muted transition hover:bg-white/[0.07] hover:text-ink"
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                      title={showPassword ? 'Hide password' : 'Show password'}
                    >
                      {showPassword ? <FiEyeOff className="h-4 w-4" /> : <FiEye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>
                <button
                  type="button"
                  className="w-full rounded-xl border border-border bg-white/[0.04] py-2 text-sm font-medium text-ink-soft transition hover:border-border-strong hover:bg-white/[0.07] hover:text-ink"
                  onClick={() => {
                    setForgotMode(true)
                    setRegisterMode(false)
                    setOtpSent(false)
                    setOtp('')
                    setNewPassword('')
                    setError('')
                    setSuccessMessage('')
                  }}
                >
                  Forgot password?
                </button>
              </>
            )}

            {registerMode && (
              <>
                <div>
                  <label className={labelClass}>Password</label>
                  <div className={fieldClass}>
                    <FiLock className="text-muted" />
                    <input
                      type={showRegisterPassword ? 'text' : 'password'}
                      value={registrationForm.password}
                      onChange={(e) =>
                        setRegistrationForm({ ...registrationForm, password: e.target.value })
                      }
                      className={inputClass}
                      placeholder="Minimum 8 characters"
                      autoComplete="new-password"
                    />
                    <button
                      type="button"
                      onClick={() => setShowRegisterPassword((v) => !v)}
                      className="rounded-lg p-1.5 text-muted transition hover:bg-white/[0.07] hover:text-ink"
                      aria-label={showRegisterPassword ? 'Hide password' : 'Show password'}
                    >
                      {showRegisterPassword ? <FiEyeOff className="h-4 w-4" /> : <FiEye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>
                <div>
                  <label className={labelClass}>Email (optional)</label>
                  <input
                    type="email"
                    value={registrationForm.email}
                    onChange={(e) =>
                      setRegistrationForm({ ...registrationForm, email: e.target.value })
                    }
                    className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10"
                    placeholder="name@company.com"
                  />
                </div>
                <div>
                  <label className={labelClass}>Full name (optional)</label>
                  <input
                    type="text"
                    value={registrationForm.full_name}
                    onChange={(e) =>
                      setRegistrationForm({ ...registrationForm, full_name: e.target.value })
                    }
                    className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10"
                    placeholder="Your full name"
                  />
                </div>
                <div>
                  <label className={labelClass}>Requested role</label>
                  <select
                    value={registrationForm.requested_role}
                    onChange={(e) =>
                      setRegistrationForm({ ...registrationForm, requested_role: e.target.value })
                    }
                    className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none focus:border-ink focus:ring-2 focus:ring-ink/10"
                  >
                    <option value="developer">Developer</option>
                    <option value="team_lead">Team Lead</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Team lead username (optional)</label>
                  <input
                    type="text"
                    value={registrationForm.requested_team_lead_username}
                    onChange={(e) =>
                      setRegistrationForm({
                        ...registrationForm,
                        requested_team_lead_username: e.target.value,
                      })
                    }
                    className="w-full rounded-xl border border-border bg-cream-mid px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-ink focus:ring-2 focus:ring-ink/10"
                    placeholder="team lead username"
                  />
                </div>
              </>
            )}

            {forgotMode && (
              <>
                <div>
                  <label className={labelClass}>Email address</label>
                  <div className={fieldClass}>
                    <FiMail className="text-muted" />
                    <input
                      type="email"
                      value={forgotEmail}
                      onChange={(e) => {
                        setForgotEmail(e.target.value)
                        // A code belongs to one address; changing it voids the step.
                        setOtpSent(false)
                        setOtp('')
                      }}
                      className={inputClass}
                      placeholder="name@company.com"
                      autoComplete="email"
                      autoFocus
                    />
                  </div>
                  {!otpSent && (
                    <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
                      We'll email a 6-digit code to this address if it has an account.
                    </p>
                  )}
                </div>

                {otpSent && (
                  <>
                    <div>
                      <label className={labelClass}>6-digit code</label>
                      <div className={fieldClass}>
                        <FiLock className="text-muted" />
                        <input
                          type="text"
                          inputMode="numeric"
                          autoComplete="one-time-code"
                          maxLength={6}
                          value={otp}
                          onChange={(e) => setOtp(e.target.value.replace(/\D/g, ''))}
                          className={`${inputClass} tracking-[0.4em]`}
                          placeholder="000000"
                        />
                      </div>
                    </div>
                    <div>
                      <label className={labelClass}>New password</label>
                      <div className={fieldClass}>
                        <FiLock className="text-muted" />
                        <input
                          type={showNewPassword ? 'text' : 'password'}
                          value={newPassword}
                          onChange={(e) => setNewPassword(e.target.value)}
                          className={inputClass}
                          placeholder="Minimum 8 characters"
                          autoComplete="new-password"
                        />
                        <button
                          type="button"
                          onClick={() => setShowNewPassword((v) => !v)}
                          className="rounded-lg p-1.5 text-muted transition hover:bg-white/[0.07] hover:text-ink"
                          aria-label={showNewPassword ? 'Hide password' : 'Show password'}
                        >
                          {showNewPassword ? <FiEyeOff className="h-4 w-4" /> : <FiEye className="h-4 w-4" />}
                        </button>
                      </div>
                    </div>
                    <button
                      type="button"
                      className="w-full text-[13px] text-muted hover:text-ink"
                      onClick={handleSendResetCode}
                      disabled={loading}
                    >
                      Didn't get it? Send another code
                    </button>
                  </>
                )}
              </>
            )}

            {error && <p className="text-sm text-danger-fg">{error}</p>}
            {successMessage && <p className="text-sm text-success-fg">{successMessage}</p>}

            {!registerMode && !forgotMode ? (
              <MovingBorder type="submit" disabled={loading} containerClassName="w-full" className="w-full">
                {loading ? 'Please wait…' : 'Sign in'}
              </MovingBorder>
            ) : (
              <Button type="submit" className="w-full" disabled={loading}>
                {loading
                  ? 'Please wait…'
                  : registerMode
                    ? 'Submit registration request'
                    : otpSent
                      ? 'Reset password'
                      : 'Send reset code'}
              </Button>
            )}

            {forgotMode && (
              <button
                type="button"
                className="w-full text-sm text-muted hover:text-ink"
                onClick={() => {
                  setForgotMode(false)
                  setRegisterMode(false)
                  setOtpSent(false)
                  setError('')
                  setSuccessMessage('')
                  setPassword('')
                  setNewPassword('')
                  setForgotEmail('')
                  setOtp('')
                }}
              >
                Back to sign in
              </button>
            )}

            <button
              type="button"
              className="w-full text-sm text-muted hover:text-ink"
              onClick={() => {
                setRegisterMode(!registerMode)
                setForgotMode(false)
                setError('')
                setSuccessMessage('')
              }}
            >
              {registerMode ? 'Back to sign in' : 'New user? Request account approval'}
            </button>
          </form>
        </GlassCard>
      </FadeContent>
    </div>
  )
}

export default Login
