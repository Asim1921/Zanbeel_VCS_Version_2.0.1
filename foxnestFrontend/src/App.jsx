import React, { useEffect, useMemo, useState } from 'react'
import Layout from './components/layout/Layout'
import Dashboard from './pages/Dashboard'
import UsersManagement from './pages/UsersManagement'
import Repositories from './pages/Repositories'
import RepositoryDetail from './pages/RepositoryDetail'
import PullRequestDetail from './pages/PullRequestDetail'
import Profile from './pages/Profile'
import Notifications from './pages/Notifications'
import Archive from './pages/Archive'
import PendingApprovals from './pages/PendingApprovals'
import PendingRepositories from './pages/PendingRepositories'
import PendingUserRegistrations from './pages/PendingUserRegistrations'
import PasswordReset from './pages/PasswordReset'
import Login from './pages/Login'
import Settings from './pages/Settings'
import Search from './pages/Search'
import Activity from './pages/Activity'
import Operations from './pages/Operations'
import BrandLogo from './components/ui/BrandLogo'
import api from './utils/api'
import { clearSessionUser, getSessionToken, setSessionUser } from './utils/session'

function App() {
  const [activeTab, setActiveTab] = useState('dashboard')
  // The repository browser lives inside the Repositories tab rather than as a tab of
  // its own, so Back returns to the list and no permission list needs a new entry.
  const [browsingRepo, setBrowsingRepo] = useState(null)
  // { repo, prId } while a pull request is open for review. Held here rather than
  // inside Repositories so the review survives the list re-rendering underneath it.
  const [reviewing, setReviewing] = useState(null)
  // Whose profile is open. null means your own; a username means someone else's.
  const [profileUser, setProfileUser] = useState(null)
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
    // Tabs everyone has, whatever their role. Keeping them in one list rather than
    // repeating them in both branches is what stops a new page being reachable for
    // an administrator and silently bounced to the dashboard for everyone else.
    const commonTabs = [
      'dashboard',
      'repositories',
      'profile',
      'notifications',
      'settings',
      'search',
    ]
    const adminTabs = [
      'users-management',
      'pending-approvals',
      'pending-repositories',
      'pending-user-registrations',
      'password-reset',
      'archive',
      'activity',
      'operations',
    ]
    const allowedTabs = isAdmin ? [...commonTabs, ...adminTabs] : commonTabs

    if (!allowedTabs.includes(activeTab)) {
      setActiveTab('dashboard')
    }
    // Leaving the tab closes the browser, so returning to it lands on the list.
    if (activeTab !== 'repositories') {
      setBrowsingRepo(null)
    }
    // Same for a profile: coming back to the tab should show your own, not whoever
    // you were last looking at.
    if (activeTab !== 'profile') {
      setProfileUser(null)
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
      <div className="app-canvas flex min-h-screen items-center justify-center">
        <div className="rounded-2xl border border-border bg-surface px-8 py-7 text-center shadow-[0_1px_0_rgba(255,255,255,0.03)_inset,0_8px_24px_rgba(0,0,0,0.5)]">
          <BrandLogo size={48} className="mx-auto mb-3 justify-center" />
          <p className="text-xl font-semibold tracking-tight text-ink">Zanbeel</p>
          <p className="mt-2 text-sm text-muted">Checking session…</p>
        </div>
      </div>
    )
  }

  if (!currentUser) {
    return <Login onLoginSuccess={handleLoginSuccess} />
  }

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
      case 'users-management':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <UsersManagement />
      case 'repositories':
        if (reviewing) {
          return (
            <PullRequestDetail
              repo={reviewing.repo}
              prId={reviewing.prId}
              onBack={() => setReviewing(null)}
            />
          )
        }
        return browsingRepo ? (
          <RepositoryDetail repo={browsingRepo} onBack={() => setBrowsingRepo(null)} />
        ) : (
          <Repositories onBrowseRepo={setBrowsingRepo} onReviewPullRequest={setReviewing} />
        )
      case 'profile':
        return (
          <Profile
            username={profileUser}
            onBack={profileUser ? () => setProfileUser(null) : undefined}
            onOpenRepo={(repo) => {
              setBrowsingRepo(repo)
              setActiveTab('repositories')
            }}
          />
        )
      case 'notifications':
        return <Notifications />
      case 'settings':
        return <Settings isAdmin={isAdmin} />
      case 'search':
        return <Search />
      case 'activity':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <Activity />
      case 'operations':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <Operations />
      case 'pending-approvals':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <PendingApprovals />
      case 'pending-repositories':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <PendingRepositories />
      case 'pending-user-registrations':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <PendingUserRegistrations />
      case 'password-reset':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <PasswordReset />
      case 'archive':
        if (!isAdmin) return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
        return <Archive />
      default:
        return <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />
    }
  }

  return (
    <div className="app-canvas relative min-h-screen">
      <Layout
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        currentUser={currentUser}
        onLogout={handleLogout}
      >
        {renderContent()}
      </Layout>
    </div>
  )
}

export default App
