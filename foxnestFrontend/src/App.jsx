import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
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
import { profileLink, pullRequestLink, repositoryLink } from './lib/links'

// Pages everyone has, whatever their role. One list for both branches is what stops a
// new page being reachable for an administrator and bounced to the dashboard for
// everyone else.
const COMMON_TABS = ['dashboard', 'repositories', 'profile', 'notifications', 'settings', 'search', 'activity']
const ADMIN_TABS = [
  'users-management', 'pending-approvals', 'pending-repositories', 'pending-user-registrations',
  'password-reset', 'archive', 'operations',
]

/**
 * A repository or pull request named by the URL. The list page hands over the
 * repository it already has; a link from elsewhere (search, a notification, a pasted
 * address) carries only the id, so the repository is fetched.
 */
function useRepository(repoId, known) {
  const [state, setState] = useState(() => ({
    repo: known && known.id === repoId ? known : null, error: null,
  }))
  useEffect(() => {
    if (!repoId) return undefined
    if (known && known.id === repoId) {
      setState({ repo: known, error: null })
      return undefined
    }
    let alive = true
    setState({ repo: null, error: null })
    api.getRepository(repoId)
      .then((response) => { if (alive) setState({ repo: response?.repository || response, error: null }) })
      .catch((err) => { if (alive) setState({ repo: null, error: err.message || 'Repository not found' }) })
    return () => { alive = false }
  }, [repoId, known])
  return state
}

function RouteMessage({ error }) {
  return (
    <div className="py-20 text-center text-sm text-muted">
      {error ? `Cannot open this page: ${error}` : 'Loading…'}
    </div>
  )
}

function RepositoryRoute({ repoId, prId, known }) {
  const navigate = useNavigate()
  const { repo, error } = useRepository(repoId, known)
  if (!repo) return <RouteMessage error={error} />
  if (prId) {
    return (
      <PullRequestDetail
        key={`${repo.id}:${prId}`}
        repo={repo}
        prId={Number(prId)}
        onBack={() => navigate(repositoryLink(repo.id))}
      />
    )
  }
  // Keyed by repository: going from one repository to another (a search result, a
  // notification) must start fresh, not carry over the last one's tree and history.
  return <RepositoryDetail key={repo.id} repo={repo} onBack={() => navigate('/repositories')} />
}

function App() {
  const location = useLocation()
  const navigate = useNavigate()
  // The page is the URL's first segment; the rest names what is open within it.
  const segments = useMemo(
    () => location.pathname.split('/').filter(Boolean).map((s) => decodeURIComponent(s)),
    [location.pathname]
  )
  const activeTab = segments[0] || 'dashboard'
  const setActiveTab = useCallback(
    (tab) => navigate(tab && tab !== 'dashboard' ? `/${tab}` : '/'),
    [navigate]
  )
  // The repository the list page opened, so it is not fetched a second time.
  const [knownRepo, setKnownRepo] = useState(null)
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
    if (!currentUser) return
    const allowed = isAdmin ? [...COMMON_TABS, ...ADMIN_TABS] : COMMON_TABS
    if (!allowed.includes(activeTab)) navigate('/', { replace: true })
  }, [activeTab, isAdmin, currentUser, navigate])

  const handleLogout = () => {
    clearSessionUser()
    setCurrentUser(null)
    navigate('/')
  }

  // Signing in keeps the address that was asked for, so a link opened while signed
  // out lands where it pointed.
  const handleLoginSuccess = (user) => {
    setCurrentUser(user)
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

  const dashboard = <Dashboard setActiveTab={setActiveTab} isAdmin={isAdmin} />

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return dashboard
      case 'users-management':
        return isAdmin ? <UsersManagement /> : dashboard
      case 'repositories':
        if (segments[1]) {
          return (
            <RepositoryRoute
              repoId={segments[1]}
              prId={segments[2] === 'pulls' ? segments[3] : null}
              known={knownRepo}
            />
          )
        }
        return (
          <Repositories
            onBrowseRepo={(repo, params = {}) => {
              setKnownRepo(repo)
              navigate(repositoryLink(repo.id, params))
            }}
            onReviewPullRequest={({ repo, prId }) => {
              setKnownRepo(repo)
              navigate(pullRequestLink(repo.id, prId))
            }}
          />
        )
      case 'profile':
        return (
          <Profile
            username={segments[1] || null}
            onBack={segments[1] ? () => navigate(profileLink(null)) : undefined}
            onOpenRepo={(repo) => {
              setKnownRepo(repo)
              navigate(repositoryLink(repo.id))
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
        return <Activity isAdmin={isAdmin} />
      case 'operations':
        return isAdmin ? <Operations /> : dashboard
      case 'pending-approvals':
        return isAdmin ? <PendingApprovals /> : dashboard
      case 'pending-repositories':
        return isAdmin ? <PendingRepositories /> : dashboard
      case 'pending-user-registrations':
        return isAdmin ? <PendingUserRegistrations /> : dashboard
      case 'password-reset':
        return isAdmin ? <PasswordReset /> : dashboard
      case 'archive':
        return isAdmin ? <Archive /> : dashboard
      default:
        return dashboard
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
