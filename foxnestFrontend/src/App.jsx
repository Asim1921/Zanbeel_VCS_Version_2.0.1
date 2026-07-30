import React,{ useEffect, useMemo, useState } from 'react'
import Layout from './components/layout/Layout'
import Dashboard from './pages/Dashboard'
import UsersManagement from './pages/UsersManagement'
import Repositories from './pages/Repositories'
import Archive from './pages/Archive'
import PendingApprovals from './pages/PendingApprovals'
import PendingRepositories from './pages/PendingRepositories'
import PendingUserRegistrations from './pages/PendingUserRegistrations'
import PasswordReset from './pages/PasswordReset'
import Login from './pages/Login'
import api from './utils/api'
import { clearSessionUser, getSessionToken, setSessionUser } from './utils/session'

function App() {
  const [activeTab, setActiveTab] = useState('dashboard')
  const [currentUser, setCurrentUser] = useState(null)
  const [authLoading, setAuthLoading] = useState(true)

  useEffect(() => {
    const bootstrapAuth = async () => {
      const token = getSessionToken()
      if (!token) {
        setAuthLoading(false)
        return
      }

      try {
        const response = await api.getCurrentUser()
        if (response.success) {
          setCurrentUser(response.user)
          setSessionUser({ username: response.user.username, role: response.user.role, token })
        } else {
          clearSessionUser()
        }
      } catch {
        clearSessionUser()
      } finally {
        setAuthLoading(false)
      }
    }

    bootstrapAuth()
  }, [])

  const isAdmin = useMemo(
    () => ['team_lead', 'admin'].includes((currentUser?.role || '').toLowerCase()),
    [currentUser]
  )

  useEffect(() => {
    const allowedTabs = isAdmin
      ? ['dashboard', 'users-management', 'repositories', 'pending-approvals', 'pending-repositories', 'pending-user-registrations', 'password-reset', 'archive']
      : ['dashboard', 'repositories']

    if (!allowedTabs.includes(activeTab)) {
      setActiveTab('dashboard')
    }
  }, [activeTab, isAdmin])

  const handleLogout = () => {
    clearSessionUser()
    setCurrentUser(null)
    setActiveTab('dashboard')
  }

  const handleLoginSuccess = (user) => {
    setCurrentUser(user)
    setActiveTab('dashboard')
  }

  if (authLoading) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center text-white">
        Checking session...
      </div>
    )
  }

  if (!currentUser) {
    return <Login onLoginSuccess={handleLoginSuccess} />
  }

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return <Dashboard />
      case 'users-management':
        if (!isAdmin) return <Dashboard />
        return <UsersManagement />
      case 'repositories':
        return <Repositories />
      case 'pending-approvals':
        if (!isAdmin) return <Dashboard />
        return <PendingApprovals />
      case 'pending-repositories':
        if (!isAdmin) return <Dashboard />
        return <PendingRepositories />
      case 'pending-user-registrations':
        if (!isAdmin) return <Dashboard />
        return <PendingUserRegistrations />
      case 'password-reset':
        if (!isAdmin) return <Dashboard />
        return <PasswordReset />
      case 'archive':
        if (!isAdmin) return <Dashboard />
        return <Archive />
      default:
        return <Dashboard />
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-gray-900 to-slate-800">
      {/* Professional gradient background with subtle animated elements */}
      <div className="absolute inset-0 overflow-hidden">
        <div className="absolute -inset-10 opacity-30">
          <div className="absolute top-0 -left-4 w-72 h-72 bg-blue-900 rounded-full mix-blend-multiply filter blur-xl opacity-50 animate-blob"></div>
          <div className="absolute top-0 -right-4 w-72 h-72 bg-slate-700 rounded-full mix-blend-multiply filter blur-xl opacity-50 animate-blob animation-delay-2000"></div>
          <div className="absolute -bottom-8 left-20 w-72 h-72 bg-gray-800 rounded-full mix-blend-multiply filter blur-xl opacity-50 animate-blob animation-delay-4000"></div>
        </div>
      </div>

      <Layout activeTab={activeTab} setActiveTab={setActiveTab} currentUser={currentUser} onLogout={handleLogout}>
        {renderContent()}
      </Layout>
    </div>
  )
}

export default App
