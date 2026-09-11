import React, { useMemo, useState, useEffect } from 'react'
import {
  FiUser, FiGitCommit, FiFolder, FiCalendar, FiMail, FiWifiOff, FiEdit3, FiTrash2,
  FiSave, FiX, FiShield, FiUserPlus, FiClock, FiActivity, FiLogIn, FiArrowLeft,
  FiList, FiTrendingUp, FiChevronRight
} from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import api from '../utils/api'
import { API_SERVER_URL } from '../config.js'
import { getSessionToken } from '../utils/session'

const formatRelativeTime = (iso) => {
  if (!iso) return 'Never'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'Never'
  const diffMs = Date.now() - date.getTime()
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days === 1) return 'Yesterday'
  if (days < 30) return `${days}d ago`
  return date.toLocaleDateString()
}

const formatDateTime = (iso) => {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

const statusBadge = (status) => {
  if (status === 'active') return { label: 'Active', variant: 'success' }
  if (status === 'idle') return { label: 'Idle', variant: 'warning' }
  return { label: 'Never used', variant: 'danger' }
}

const scoreBadge = (score) => {
  if (score === 'high') return { label: 'High', variant: 'success' }
  if (score === 'medium') return { label: 'Medium', variant: 'warning' }
  return { label: 'Low', variant: 'danger' }
}

const DETAIL_TABS = [
  { id: 'overview', label: 'Overview', icon: FiTrendingUp },
  { id: 'activity', label: 'User Activity', icon: FiActivity },
  { id: 'history', label: 'User History', icon: FiList },
  { id: 'permissions', label: 'Permissions', icon: FiShield },
]

const UsersManagement = () => {
  const token = getSessionToken()
  const authHeaders = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json'
  }

  const [users, setUsers] = useState([])
  const [engagementSummary, setEngagementSummary] = useState({
    active: 0,
    idle: 0,
    never_used: 0,
    total: 0,
  })
  const [statusFilter, setStatusFilter] = useState('all')
  const [repositories, setRepositories] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedUser, setSelectedUser] = useState(null)
  const [detailTab, setDetailTab] = useState('overview')
  const [userDetail, setUserDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [showAddUserModal, setShowAddUserModal] = useState(false)
  const [showPermissionModal, setShowPermissionModal] = useState(false)
  const [showEditPermissionModal, setShowEditPermissionModal] = useState(false)
  const [newUser, setNewUser] = useState({
    username: '',
    email: '',
    password: '',
    full_name: '',
    role: 'developer',
    team_lead_id: null
  })
  const [permissionForm, setPermissionForm] = useState({
    username: '',
    repo_id: '',
    permission_level: 'read'
  })
  const [editPermissionForm, setEditPermissionForm] = useState({
    username: '',
    repo_id: '',
    permission_level: 'read'
  })

  useEffect(() => {
    fetchData()
  }, [])

  const fetchData = async () => {
    try {
      setLoading(true)
      setError(null)

      let engagementLoaded = false
      try {
        const engagement = await api.getUsersEngagement()
        if (engagement?.success) {
          setUsers(engagement.users || [])
          setEngagementSummary(engagement.summary || {
            active: 0,
            idle: 0,
            never_used: 0,
            total: (engagement.users || []).length,
          })
          engagementLoaded = true
        }
      } catch (engErr) {
        console.warn('Engagement API unavailable, falling back to /users', engErr)
      }

      if (!engagementLoaded) {
        const usersResponse = await fetch(`${API_SERVER_URL}/api/users`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        const usersData = await usersResponse.json()
        if (usersData.success) {
          setUsers(usersData.users || [])
          setEngagementSummary({
            active: 0,
            idle: 0,
            never_used: 0,
            total: (usersData.users || []).length,
          })
        }
      }

      const reposResponse = await api.listAllRepositories()
      if (reposResponse.success) {
        setRepositories(reposResponse.repositories)
      }
    } catch (err) {
      setError(`Error connecting to server: ${err.message}`)
      console.error('Error fetching data:', err)
    } finally {
      setLoading(false)
    }
  }

  const filteredUsers = useMemo(() => {
    if (statusFilter === 'all') return users
    return users.filter((u) => (u.status || 'never_used') === statusFilter)
  }, [users, statusFilter])

  const openUserDetail = async (user, tab = 'overview') => {
    setSelectedUser(user)
    setDetailTab(tab)
    setDetailLoading(true)
    setUserDetail(null)
    try {
      const [detail, permRes] = await Promise.all([
        api.getUserDetail(user.username),
        fetch(`${API_SERVER_URL}/api/permissions/user/${user.username}`, {
          headers: { Authorization: `Bearer ${token}` },
        }).then((r) => r.json()).catch(() => ({ success: false, permissions: [] })),
      ])
      setUserDetail({
        ...(detail || {}),
        permissions: permRes?.success ? (permRes.permissions || []) : [],
      })
      if (detail?.user) {
        setSelectedUser({ ...user, ...detail.user })
      }
    } catch (err) {
      console.error('Error loading user detail:', err)
      setError(`Failed to load user detail: ${err.message}`)
    } finally {
      setDetailLoading(false)
    }
  }

  const closeUserDetail = () => {
    setSelectedUser(null)
    setUserDetail(null)
    setDetailTab('overview')
  }

  const handleAddUser = async () => {
    try {
      if (!newUser.username) {
        alert('Username is required')
        return
      }
      if (!newUser.password || newUser.password.length < 8) {
        alert('Password is required and must be at least 8 characters')
        return
      }
      if (newUser.role === 'developer' && !newUser.team_lead_id) {
        alert('Please select a team lead for this developer')
        return
      }

      const response = await fetch(`${API_SERVER_URL}/api/users/create`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify(newUser)
      })

      if (response.ok) {
        await fetchData()
        setShowAddUserModal(false)
        setNewUser({
          username: '',
          email: '',
          password: '',
          full_name: '',
          role: 'developer',
          team_lead_id: null
        })
      } else {
        const errorData = await response.json()
        alert(`Error: ${errorData.detail || 'Failed to create user'}`)
      }
    } catch (error) {
      console.error('Error adding user:', error)
      alert('Failed to add user. Please try again.')
    }
  }

  const handleAddPermission = async () => {
    try {
      const response = await fetch(`${API_SERVER_URL}/api/permissions/create`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify(permissionForm)
      })

      if (response.ok) {
        alert('Permission granted successfully!')
        setShowPermissionModal(false)
        setPermissionForm({ username: '', repo_id: '', permission_level: 'read' })
        if (selectedUser?.username === permissionForm.username) {
          await openUserDetail(selectedUser, 'permissions')
        }
      }
    } catch (error) {
      console.error('Error adding permission:', error)
    }
  }

  const handleRevokePermission = async (username, repoId) => {
    if (!confirm('Are you sure you want to revoke this permission?')) return

    try {
      const response = await fetch(
        `${API_SERVER_URL}/api/permissions/revoke?username=${username}&repo_id=${repoId}`,
        {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` }
        }
      )

      if (response.ok) {
        alert('Permission revoked successfully!')
        if (selectedUser) {
          await openUserDetail(selectedUser, 'permissions')
        }
      }
    } catch (error) {
      console.error('Error revoking permission:', error)
    }
  }

  const handleEditPermission = (permission) => {
    setEditPermissionForm({
      username: selectedUser.username,
      repo_id: permission.repository_id,
      permission_level: permission.permission_level
    })
    setShowEditPermissionModal(true)
  }

  const handleUpdatePermission = async () => {
    try {
      const response = await fetch(`${API_SERVER_URL}/api/permissions/update`, {
        method: 'PUT',
        headers: authHeaders,
        body: JSON.stringify(editPermissionForm)
      })

      if (response.ok) {
        alert('Permission updated successfully!')
        setShowEditPermissionModal(false)
        if (selectedUser) {
          await openUserDetail(selectedUser, 'permissions')
        }
      }
    } catch (error) {
      console.error('Error updating permission:', error)
    }
  }

  const handleDeleteUser = async (username) => {
    if (!confirm(`Are you sure you want to delete user "${username}"? This action cannot be undone.`)) return

    try {
      const response = await fetch(`${API_SERVER_URL}/api/users/${username}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` }
      })
      const data = await response.json()

      if (response.ok) {
        alert('User deleted successfully!')
        await fetchData()
        if (selectedUser?.username === username) {
          closeUserDetail()
        }
      } else {
        alert(data.detail || 'Failed to delete user')
      }
    } catch (error) {
      console.error('Error deleting user:', error)
      alert('Error deleting user')
    }
  }

  const getPermissionBadgeVariant = (level) => {
    switch (level) {
      case 'admin': return 'danger'
      case 'team_lead': return 'warning'
      case 'write': return 'info'
      case 'read': return 'default'
      default: return 'default'
    }
  }

  const profile = userDetail?.user || selectedUser
  const st = statusBadge(profile?.status || 'never_used')
  const sc = scoreBadge(profile?.activity_score || 'low')

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center h-64">
          <FiUser className="w-8 h-8 animate-pulse text-info-fg" />
          <span className="ml-2 text-ink-soft">Loading users...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-ink mb-2">Users & Permissions</h1>
          <p className="text-ink-soft">
            {selectedUser
              ? `Profile for @${selectedUser.username}`
              : 'Click a username to open activity, history, and permissions'}
          </p>
        </div>
        <div className="flex items-center space-x-3">
          {selectedUser && (
            <Button variant="secondary" onClick={closeUserDetail} className="flex items-center space-x-2">
              <FiArrowLeft className="w-4 h-4" />
              <span>All users</span>
            </Button>
          )}
          <Button
            variant="primary"
            onClick={() => setShowAddUserModal(true)}
            className="flex items-center space-x-2"
          >
            <FiUserPlus className="w-4 h-4" />
            <span>Add User</span>
          </Button>
          <Button
            variant="secondary"
            onClick={() => setShowPermissionModal(true)}
            className="flex items-center space-x-2"
          >
            <FiShield className="w-4 h-4" />
            <span>Grant Permission</span>
          </Button>
        </div>
      </div>

      {error && (
        <GlassCard className="p-4 border-danger-fg/20 bg-danger-bg">
          <div className="flex items-center space-x-2 text-danger-fg">
            <FiWifiOff className="w-5 h-5" />
            <span>{error}</span>
          </div>
        </GlassCard>
      )}

      {!selectedUser && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <GlassCard className="p-4">
              <p className="text-xs text-muted uppercase tracking-wide">Total</p>
              <p className="text-2xl font-semibold text-ink mt-1">{engagementSummary.total}</p>
            </GlassCard>
            <GlassCard className="p-4 border-success-fg/20">
              <p className="text-xs text-success-fg/80 uppercase tracking-wide">Active (7d)</p>
              <p className="text-2xl font-semibold text-success-fg mt-1">{engagementSummary.active}</p>
            </GlassCard>
            <GlassCard className="p-4 border-warning-fg/20">
              <p className="text-xs text-warning-fg/80 uppercase tracking-wide">Idle</p>
              <p className="text-2xl font-semibold text-warning-fg mt-1">{engagementSummary.idle}</p>
            </GlassCard>
            <GlassCard className="p-4 border-danger-fg/20">
              <p className="text-xs text-danger-fg/80 uppercase tracking-wide">Never used</p>
              <p className="text-2xl font-semibold text-danger-fg mt-1">{engagementSummary.never_used}</p>
            </GlassCard>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {[
              { id: 'all', label: 'All' },
              { id: 'active', label: 'Active' },
              { id: 'idle', label: 'Idle' },
              { id: 'never_used', label: 'Never used' },
            ].map((f) => (
              <Button
                key={f.id}
                size="sm"
                variant={statusFilter === f.id ? 'primary' : 'secondary'}
                onClick={() => setStatusFilter(f.id)}
              >
                {f.label}
              </Button>
            ))}
            <span className="text-sm text-muted ml-2">
              Showing {filteredUsers.length} user{filteredUsers.length === 1 ? '' : 's'}
            </span>
          </div>

          <GlassCard className="overflow-hidden p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-border text-xs uppercase tracking-wide text-muted">
                    <th className="px-4 py-3 font-medium">User</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Last login</th>
                    <th className="px-4 py-3 font-medium">Last work</th>
                    <th className="px-4 py-3 font-medium">Commits 7d/30d</th>
                    <th className="px-4 py-3 font-medium">Score</th>
                    <th className="px-4 py-3 font-medium">Role</th>
                    <th className="px-4 py-3 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredUsers.map((user) => {
                    const rowStatus = statusBadge(user.status || 'never_used')
                    const rowScore = scoreBadge(user.activity_score || 'low')
                    return (
                      <tr
                        key={user.id}
                        className="border-b border-white/5 hover:bg-cream-mid transition-colors"
                      >
                        <td className="px-4 py-3">
                          <button
                            type="button"
                            onClick={() => openUserDetail(user)}
                            className="text-left group"
                          >
                            <div className="flex items-center gap-3">
                              <div className="w-9 h-9 rounded-full bg-gradient-to-br from-glow to-accent flex items-center justify-center shrink-0">
                                <span className="text-ink text-sm font-semibold">
                                  {(user.full_name || user.username || '?').charAt(0).toUpperCase()}
                                </span>
                              </div>
                              <div>
                                <p className="text-ink font-medium group-hover:text-info-fg transition-colors">
                                  {user.full_name || user.username}
                                </p>
                                <p className="text-xs text-info-fg/90 group-hover:underline">@{user.username}</p>
                              </div>
                            </div>
                          </button>
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant={rowStatus.variant}>{rowStatus.label}</Badge>
                        </td>
                        <td className="px-4 py-3 text-sm text-ink-soft">{formatRelativeTime(user.last_login_at)}</td>
                        <td className="px-4 py-3 text-sm text-ink-soft">{formatRelativeTime(user.last_work_at)}</td>
                        <td className="px-4 py-3 text-sm text-ink-soft">
                          {user.commits_7d ?? 0} / {user.commits_30d ?? 0}
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant={rowScore.variant}>{rowScore.label}</Badge>
                        </td>
                        <td className="px-4 py-3 text-sm text-ink-soft capitalize">
                          {(user.role || 'developer').replace('_', ' ')}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => openUserDetail(user)}
                              title="Open profile"
                            >
                              <FiChevronRight className="w-4 h-4" />
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="text-danger-fg hover:text-danger-fg"
                              onClick={() => handleDeleteUser(user.username)}
                              title="Delete user"
                            >
                              <FiTrash2 className="w-4 h-4" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            {filteredUsers.length === 0 && (
              <div className="p-8 text-center text-muted">No users match this filter.</div>
            )}
          </GlassCard>
        </>
      )}

      {selectedUser && (
        <GlassCard className="p-0 overflow-hidden min-h-[560px]">
          <div className="flex flex-col lg:flex-row min-h-[560px]">
            {/* Left tabs */}
            <aside className="lg:w-56 shrink-0 border-b lg:border-b-0 lg:border-r border-border bg-cream-mid">
              <div className="p-4 border-b border-border">
                <div className="flex items-center gap-3">
                  <div className="w-11 h-11 rounded-full bg-gradient-to-br from-glow to-accent flex items-center justify-center">
                    <span className="text-ink font-semibold">
                      {(profile?.full_name || profile?.username || '?').charAt(0).toUpperCase()}
                    </span>
                  </div>
                  <div className="min-w-0">
                    <p className="text-ink font-semibold truncate">{profile?.full_name || profile?.username}</p>
                    <p className="text-xs text-muted truncate">@{profile?.username}</p>
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Badge variant={st.variant}>{st.label}</Badge>
                  <Badge variant={sc.variant}>Score: {sc.label}</Badge>
                </div>
              </div>
              <nav className="p-2 space-y-1">
                {DETAIL_TABS.map((tab) => {
                  const Icon = tab.icon
                  const active = detailTab === tab.id
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setDetailTab(tab.id)}
                      className={`w-full flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                        active
                          ? 'bg-info-fg/20 text-info-fg border border-info-fg/30'
                          : 'text-ink-soft hover:bg-cream-mid hover:text-ink'
                      }`}
                    >
                      <Icon className="w-4 h-4 shrink-0" />
                      <span>{tab.label}</span>
                    </button>
                  )
                })}
              </nav>
            </aside>

            {/* Right content */}
            <div className="flex-1 p-6">
              {detailLoading && (
                <div className="flex items-center justify-center h-64 text-muted">
                  <FiActivity className="w-6 h-6 animate-pulse mr-2" />
                  Loading profile...
                </div>
              )}

              {!detailLoading && detailTab === 'overview' && profile && (
                <div className="space-y-6">
                  <div>
                    <h2 className="text-xl font-bold text-ink">Overview</h2>
                    <p className="text-sm text-muted mt-1">Engagement and performance snapshot</p>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiLogIn /> Last login</div>
                      <p className="text-ink text-lg font-semibold mt-2">{formatRelativeTime(profile.last_login_at)}</p>
                      <p className="text-xs text-muted mt-1">{formatDateTime(profile.last_login_at)}</p>
                    </div>
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiClock /> Last work</div>
                      <p className="text-ink text-lg font-semibold mt-2">{formatRelativeTime(profile.last_work_at)}</p>
                      <p className="text-xs text-muted mt-1">{formatDateTime(profile.last_work_at)}</p>
                    </div>
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiGitCommit /> Commits</div>
                      <p className="text-ink text-lg font-semibold mt-2">
                        {profile.commits_7d ?? 0} <span className="text-muted text-sm">/ 7d</span>
                        {' · '}
                        {profile.commits_30d ?? 0} <span className="text-muted text-sm">/ 30d</span>
                      </p>
                    </div>
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiActivity /> Pending waits</div>
                      <p className="text-ink text-lg font-semibold mt-2">{profile.pending_waits ?? 0}</p>
                    </div>
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiFolder /> Repos owned</div>
                      <p className="text-ink text-lg font-semibold mt-2">{profile.repos_owned ?? 0}</p>
                    </div>
                    <div className="rounded-xl bg-cream-mid/70 border border-border p-4">
                      <div className="flex items-center gap-2 text-muted text-xs uppercase"><FiTrendingUp /> Activity score</div>
                      <div className="mt-2"><Badge variant={sc.variant}>{sc.label}</Badge></div>
                    </div>
                  </div>
                  <div className="rounded-xl bg-cream-mid/70 border border-border p-4 space-y-2 text-sm text-ink-soft">
                    {profile.email && (
                      <div className="flex items-center gap-2"><FiMail className="w-4 h-4" />{profile.email}</div>
                    )}
                    <div className="flex items-center gap-2">
                      <FiCalendar className="w-4 h-4" />
                      Joined {profile.created_at ? new Date(profile.created_at).toLocaleDateString() : '—'}
                    </div>
                    <div className="flex items-center gap-2">
                      <FiShield className="w-4 h-4" />
                      Role: {(profile.role || 'developer').replace('_', ' ')}
                    </div>
                    {profile.team_lead_name && (
                      <div className="flex items-center gap-2">
                        <FiUser className="w-4 h-4" />
                        Team lead: {profile.team_lead_name}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {!detailLoading && detailTab === 'activity' && (
                <div className="space-y-4">
                  <div>
                    <h2 className="text-xl font-bold text-ink">User Activity</h2>
                    <p className="text-sm text-muted mt-1">System actions recorded for this user</p>
                  </div>
                  {(userDetail?.activity || []).length === 0 ? (
                    <div className="text-center py-16 text-muted">
                      <FiActivity className="w-10 h-10 mx-auto mb-3 opacity-40" />
                      No activity recorded yet.
                    </div>
                  ) : (
                    <div className="space-y-2 max-h-[520px] overflow-y-auto pr-1">
                      {(userDetail?.activity || []).map((item) => (
                        <div
                          key={`act-${item.id}`}
                          className="rounded-xl border border-border bg-cream-mid/70 px-4 py-3"
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <p className="text-ink font-medium text-sm">
                                {(item.type || 'activity').replace(/_/g, ' ')}
                              </p>
                              <p className="text-muted text-sm mt-1">{item.description || '—'}</p>
                              {item.repository && (
                                <p className="text-xs text-info-fg/80 mt-1">Repo: {item.repository}</p>
                              )}
                            </div>
                            <span className="text-xs text-muted whitespace-nowrap">
                              {formatRelativeTime(item.created_at)}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {!detailLoading && detailTab === 'history' && (
                <div className="space-y-4">
                  <div>
                    <h2 className="text-xl font-bold text-ink">User History</h2>
                    <p className="text-sm text-muted mt-1">
                      Commits, repository creation, requests, and related events
                    </p>
                  </div>
                  {(userDetail?.history || []).length === 0 ? (
                    <div className="text-center py-16 text-muted">
                      <FiList className="w-10 h-10 mx-auto mb-3 opacity-40" />
                      No history found for this user.
                    </div>
                  ) : (
                    <div className="space-y-2 max-h-[520px] overflow-y-auto pr-1">
                      {(userDetail?.history || []).map((item, idx) => (
                        <div
                          key={`hist-${idx}-${item.kind}-${item.created_at}`}
                          className="rounded-xl border border-border bg-cream-mid/70 px-4 py-3"
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <div className="flex items-center gap-2 flex-wrap">
                                <Badge variant="info">{(item.kind || 'event').replace(/_/g, ' ')}</Badge>
                                {item.status && <Badge variant="default">{item.status}</Badge>}
                              </div>
                              <p className="text-ink font-medium text-sm mt-2">{item.title}</p>
                              {item.detail && (
                                <p className="text-muted text-sm mt-1">{item.detail}</p>
                              )}
                              {item.repository && (
                                <p className="text-xs text-info-fg/80 mt-1">Repo: {item.repository}</p>
                              )}
                            </div>
                            <div className="text-right shrink-0">
                              <p className="text-xs text-muted">{formatRelativeTime(item.created_at)}</p>
                              <p className="text-[11px] text-muted mt-1">{formatDateTime(item.created_at)}</p>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {!detailLoading && detailTab === 'permissions' && (
                <div className="space-y-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h2 className="text-xl font-bold text-ink">Permissions</h2>
                      <p className="text-sm text-muted mt-1">Repository access for this user</p>
                    </div>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        setPermissionForm({
                          username: selectedUser.username,
                          repo_id: '',
                          permission_level: 'read',
                        })
                        setShowPermissionModal(true)
                      }}
                    >
                      <FiShield className="w-4 h-4 mr-2" />
                      Grant
                    </Button>
                  </div>
                  {(userDetail?.permissions || []).length > 0 ? (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      {(userDetail?.permissions || []).map((perm, index) => (
                        <div
                          key={index}
                          className="p-4 rounded-xl bg-cream-mid border border-border"
                        >
                          <div className="flex items-start justify-between mb-3">
                            <div className="flex-1">
                              <h4 className="font-medium text-ink text-sm mb-1">
                                {perm.repository_name}
                              </h4>
                              <Badge variant={getPermissionBadgeVariant(perm.permission_level)}>
                                {perm.permission_level}
                              </Badge>
                            </div>
                            <div className="flex items-center space-x-1">
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleEditPermission(perm)}
                                className="text-info-fg hover:text-info-fg"
                                title="Edit permission"
                              >
                                <FiEdit3 className="w-4 h-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleRevokePermission(selectedUser.username, perm.repository_id)}
                                className="text-danger-fg hover:text-danger-fg"
                                title="Revoke permission"
                              >
                                <FiTrash2 className="w-4 h-4" />
                              </Button>
                            </div>
                          </div>
                          <div className="space-y-1 text-xs text-ink-soft">
                            {perm.granted_by && <p>Granted by: {perm.granted_by}</p>}
                            {perm.granted_at && (
                              <p>Date: {new Date(perm.granted_at).toLocaleDateString()}</p>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-center py-16 text-muted">
                      <FiShield className="w-10 h-10 mx-auto mb-3 opacity-40" />
                      No permissions granted yet
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </GlassCard>
      )}

      {/* Add User Modal */}
      {showAddUserModal && (
        <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <GlassCard className="max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-ink">Add New User</h2>
              <Button variant="ghost" size="sm" onClick={() => setShowAddUserModal(false)}>
                <FiX className="w-5 h-5" />
              </Button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Username *</label>
                <input
                  type="text"
                  value={newUser.username}
                  onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
                  placeholder="johndoe"
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink placeholder:text-muted focus:outline-none focus:border-ink"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Email</label>
                <input
                  type="email"
                  value={newUser.email}
                  onChange={(e) => setNewUser({ ...newUser, email: e.target.value })}
                  placeholder="john@example.com"
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink placeholder:text-muted focus:outline-none focus:border-ink"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Password *</label>
                <input
                  type="password"
                  value={newUser.password}
                  onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
                  placeholder="At least 8 characters"
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink placeholder:text-muted focus:outline-none focus:border-ink"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Full Name</label>
                <input
                  type="text"
                  value={newUser.full_name}
                  onChange={(e) => setNewUser({ ...newUser, full_name: e.target.value })}
                  placeholder="John Doe"
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink placeholder:text-muted focus:outline-none focus:border-ink"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Role *</label>
                <select
                  value={newUser.role}
                  onChange={(e) => setNewUser({ ...newUser, role: e.target.value, team_lead_id: null })}
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                >
                  <option value="developer">Developer</option>
                  <option value="team_lead">Team Lead</option>
                </select>
              </div>
              {newUser.role === 'developer' && (
                <div>
                  <label className="block text-sm font-medium text-ink-soft mb-2">Assign Team Lead *</label>
                  <select
                    value={newUser.team_lead_id || ''}
                    onChange={(e) => setNewUser({ ...newUser, team_lead_id: parseInt(e.target.value) || null })}
                    className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                  >
                    <option value="">Select a team lead</option>
                    {users.filter((user) => user.role === 'team_lead').map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.username} - {user.full_name || 'No name'}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="flex items-center space-x-3 pt-2">
                <Button
                  variant="primary"
                  onClick={handleAddUser}
                  disabled={!newUser.username || !newUser.password || (newUser.role === 'developer' && !newUser.team_lead_id)}
                  className="flex-1"
                >
                  <FiSave className="w-4 h-4 mr-2" />
                  Add User
                </Button>
                <Button variant="secondary" onClick={() => setShowAddUserModal(false)} className="flex-1">
                  Cancel
                </Button>
              </div>
            </div>
          </GlassCard>
        </div>
      )}

      {/* Grant Permission Modal */}
      {showPermissionModal && (
        <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <GlassCard className="max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-ink">Grant Repository Permission</h2>
              <Button variant="ghost" size="sm" onClick={() => setShowPermissionModal(false)}>
                <FiX className="w-5 h-5" />
              </Button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Username *</label>
                <select
                  value={permissionForm.username}
                  onChange={(e) => setPermissionForm({ ...permissionForm, username: e.target.value })}
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                >
                  <option value="">Select a user</option>
                  {users.map((user) => (
                    <option key={user.id} value={user.username}>{user.username}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Repository *</label>
                <select
                  value={permissionForm.repo_id}
                  onChange={(e) => setPermissionForm({ ...permissionForm, repo_id: e.target.value })}
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                >
                  <option value="">Select a repository</option>
                  {repositories.map((repo) => (
                    <option key={repo.id} value={repo.id}>{repo.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Permission Level *</label>
                <select
                  value={permissionForm.permission_level}
                  onChange={(e) => setPermissionForm({ ...permissionForm, permission_level: e.target.value })}
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                >
                  <option value="read">Read - View only</option>
                  <option value="write">Write - Can push changes (requires approval)</option>
                  <option value="team_lead">Team Lead - Can approve changes</option>
                  <option value="admin">Admin - Full access</option>
                </select>
              </div>
              <div className="flex items-center space-x-3 pt-2">
                <Button
                  variant="primary"
                  onClick={handleAddPermission}
                  disabled={!permissionForm.username || !permissionForm.repo_id}
                  className="flex-1"
                >
                  <FiShield className="w-4 h-4 mr-2" />
                  Grant Permission
                </Button>
                <Button variant="secondary" onClick={() => setShowPermissionModal(false)} className="flex-1">
                  Cancel
                </Button>
              </div>
            </div>
          </GlassCard>
        </div>
      )}

      {/* Edit Permission Modal */}
      {showEditPermissionModal && (
        <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <GlassCard className="max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-ink">Edit Permission</h2>
              <Button variant="ghost" size="sm" onClick={() => setShowEditPermissionModal(false)}>
                <FiX className="w-5 h-5" />
              </Button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">User</label>
                <input
                  type="text"
                  value={editPermissionForm.username}
                  disabled
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-muted cursor-not-allowed"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Repository</label>
                <input
                  type="text"
                  value={repositories.find((r) => r.id === editPermissionForm.repo_id)?.name || ''}
                  disabled
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-muted cursor-not-allowed"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-ink-soft mb-2">Permission Level *</label>
                <select
                  value={editPermissionForm.permission_level}
                  onChange={(e) => setEditPermissionForm({ ...editPermissionForm, permission_level: e.target.value })}
                  className="w-full px-3 py-2 bg-cream-mid border border-border rounded-md text-ink focus:outline-none focus:border-ink"
                >
                  <option value="read">Read - View only</option>
                  <option value="write">Write - Can push changes (requires approval)</option>
                  <option value="team_lead">Team Lead - Can approve changes</option>
                  <option value="admin">Admin - Full access</option>
                </select>
              </div>
              <div className="flex items-center space-x-3 pt-2">
                <Button variant="primary" onClick={handleUpdatePermission} className="flex-1">
                  <FiSave className="w-4 h-4 mr-2" />
                  Update Permission
                </Button>
                <Button variant="secondary" onClick={() => setShowEditPermissionModal(false)} className="flex-1">
                  Cancel
                </Button>
              </div>
            </div>
          </GlassCard>
        </div>
      )}
    </div>
  )
}

export default UsersManagement
