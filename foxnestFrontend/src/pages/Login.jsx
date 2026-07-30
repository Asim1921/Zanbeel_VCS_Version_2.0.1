import React, { useState } from 'react'
import { FiLock, FiUser } from 'react-icons/fi'
import Button from '../components/ui/Button'
import GlassCard from '../components/ui/GlassCard'
import api from '../utils/api'

const Login = ({ onLoginSuccess }) => {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [setupKey, setSetupKey] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [bootstrapMode, setBootstrapMode] = useState(false)
  const [registerMode, setRegisterMode] = useState(false)
  const [registrationForm, setRegistrationForm] = useState({
    username: '',
    password: '',
    email: '',
    full_name: '',
    requested_role: 'developer',
    requested_team_lead_username: ''
  })
  const [error, setError] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [loading, setLoading] = useState(false)

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
      if (response.success) {
        onLoginSuccess(response.user)
      } else {
        setError('Login failed')
      }
    } catch (err) {
      setError(err.message || 'Unable to login')
    } finally {
      setLoading(false)
    }
  }

  const handleBootstrap = async (event) => {
    event.preventDefault()
    setError('')
    setSuccessMessage('')

    if (!username.trim() || !newPassword || !setupKey) {
      setError('Username, new password and setup key are required')
      return
    }

    try {
      setLoading(true)
      const response = await api.bootstrapPassword(username.trim(), newPassword, setupKey)
      if (response.success) {
        setSuccessMessage('Password initialized. You can now sign in.')
        setBootstrapMode(false)
        setPassword('')
        setNewPassword('')
      } else {
        setError('Password setup failed')
      }
    } catch (err) {
      setError(err.message || 'Unable to initialize password')
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
          requested_team_lead_username: ''
        })
      } else {
        setError('Unable to submit registration request')
      }
    } catch (err) {
      setError(err.message || 'Unable to submit registration request')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-gray-900 to-slate-800 flex items-center justify-center p-6">
      <GlassCard className="w-full max-w-md p-8">
        <div className="text-center mb-6">
          <h1 className="text-3xl font-bold text-white mb-2">ZANBEEL Login</h1>
          <p className="text-white/70">Sign in to access your role-based dashboard</p>
        </div>

        <form onSubmit={registerMode ? handleRegisterRequest : (bootstrapMode ? handleBootstrap : handleSubmit)} className="space-y-4">
          <div>
            <label className="block text-sm text-white/70 mb-2">Username</label>
            <div className="flex items-center bg-white/10 border border-white/20 rounded-lg px-3">
              <FiUser className="text-white/60" />
              <input
                type="text"
                value={registerMode ? registrationForm.username : username}
                onChange={(e) => {
                  if (registerMode) {
                    setRegistrationForm({ ...registrationForm, username: e.target.value })
                  } else {
                    setUsername(e.target.value)
                  }
                }}
                className="w-full px-3 py-2 bg-transparent text-white outline-none"
                placeholder="Enter username"
                autoComplete="username"
              />
            </div>
          </div>

          {!bootstrapMode && !registerMode && (
          <>
            <div>
              <label className="block text-sm text-white/70 mb-2">Password</label>
              <div className="flex items-center bg-white/10 border border-white/20 rounded-lg px-3">
                <FiLock className="text-white/60" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-3 py-2 bg-transparent text-white outline-none"
                  placeholder="Enter password"
                  autoComplete="current-password"
                />
              </div>
            </div>
            <button
              type="button"
              className="w-full py-2 px-4 bg-blue-600/30 hover:bg-blue-600/50 border border-blue-500/50 text-blue-300 rounded-lg transition-all duration-200 text-sm font-medium"
              onClick={() => {
                setBootstrapMode(true)
                setRegisterMode(false)
                setError('')
                setSuccessMessage('')
              }}
            >
              🔑 Forgot Password?
            </button>
          </>
          )}

          {registerMode && (
            <>
              <div>
                <label className="block text-sm text-white/70 mb-2">Password</label>
                <div className="flex items-center bg-white/10 border border-white/20 rounded-lg px-3">
                  <FiLock className="text-white/60" />
                  <input
                    type="password"
                    value={registrationForm.password}
                    onChange={(e) => setRegistrationForm({ ...registrationForm, password: e.target.value })}
                    className="w-full px-3 py-2 bg-transparent text-white outline-none"
                    placeholder="Minimum 8 characters"
                    autoComplete="new-password"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm text-white/70 mb-2">Email (optional)</label>
                <input
                  type="email"
                  value={registrationForm.email}
                  onChange={(e) => setRegistrationForm({ ...registrationForm, email: e.target.value })}
                  className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded-lg text-white outline-none"
                  placeholder="name@company.com"
                />
              </div>

              <div>
                <label className="block text-sm text-white/70 mb-2">Full Name (optional)</label>
                <input
                  type="text"
                  value={registrationForm.full_name}
                  onChange={(e) => setRegistrationForm({ ...registrationForm, full_name: e.target.value })}
                  className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded-lg text-white outline-none"
                  placeholder="Your full name"
                />
              </div>

              <div>
                <label className="block text-sm text-white/70 mb-2">Requested Role</label>
                <select
                  value={registrationForm.requested_role}
                  onChange={(e) => setRegistrationForm({ ...registrationForm, requested_role: e.target.value })}
                  className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded-lg text-white outline-none"
                >
                  <option value="developer">Developer</option>
                  <option value="team_lead">Team Lead</option>
                </select>
              </div>

              <div>
                <label className="block text-sm text-white/70 mb-2">Requested Team Lead Username (optional)</label>
                <input
                  type="text"
                  value={registrationForm.requested_team_lead_username}
                  onChange={(e) => setRegistrationForm({ ...registrationForm, requested_team_lead_username: e.target.value })}
                  className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded-lg text-white outline-none"
                  placeholder="team lead username"
                />
              </div>
            </>
          )}

          {bootstrapMode && (
            <>
              <div>
                <label className="block text-sm text-white/70 mb-2">New Password</label>
                <div className="flex items-center bg-white/10 border border-white/20 rounded-lg px-3">
                  <FiLock className="text-white/60" />
                  <input
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    className="w-full px-3 py-2 bg-transparent text-white outline-none"
                    placeholder="Minimum 8 characters"
                  />
                </div>
              </div>
              <div>
                <label className="block text-sm text-white/70 mb-2">Setup Key</label>
                <div className="flex items-center bg-white/10 border border-white/20 rounded-lg px-3">
                  <FiLock className="text-white/60" />
                  <input
                    type="password"
                    value={setupKey}
                    onChange={(e) => setSetupKey(e.target.value)}
                    className="w-full px-3 py-2 bg-transparent text-white outline-none"
                    placeholder="Admin-provided setup key"
                  />
                </div>
              </div>
            </>
          )}

          {error && <p className="text-sm text-red-300">{error}</p>}
          {successMessage && <p className="text-sm text-green-300">{successMessage}</p>}

          <Button type="submit" className="w-full" disabled={loading}>
            {loading ? 'Please wait...' : registerMode ? 'Submit Registration Request' : bootstrapMode ? 'Initialize Password' : 'Sign In'}
          </Button>

          {bootstrapMode && (
            <button
              type="button"
              className="w-full text-sm text-white/70 hover:text-white"
              onClick={() => {
                setBootstrapMode(false)
                setRegisterMode(false)
                setError('')
                setSuccessMessage('')
                setPassword('')
                setNewPassword('')
                setSetupKey('')
              }}
            >
              ← Back to Sign In
            </button>
          )}

          <button
            type="button"
            className="w-full text-sm text-white/70 hover:text-white"
            onClick={() => {
              setRegisterMode(!registerMode)
              setBootstrapMode(false)
              setError('')
              setSuccessMessage('')
            }}
          >
            {registerMode ? 'Back to Sign In' : 'New user? Request account approval'}
          </button>
        </form>
      </GlassCard>
    </div>
  )
}

export default Login
