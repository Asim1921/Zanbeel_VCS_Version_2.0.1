import { useState, useEffect } from 'react'
import api from '../utils/api'
import { getSessionUser } from '../utils/session'

export const useApiData = (apiCall, dependencies = []) => {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        setError(null)
        const result = await apiCall()
        setData(result)
      } catch (err) {
        setError(err.message)
        console.error('API call failed:', err)
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, dependencies)

  const refetch = async () => {
    try {
      setLoading(true)
      setError(null)
      const result = await apiCall()
      setData(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return { data, loading, error, refetch }
}

export const useRepositories = (username = null) => {
  return useApiData(
    async () => {
      const sessionUser = getSessionUser()
      const effectiveUsername = username || sessionUser.username

      if (username) {
        const response = await api.listRepositories(username)
        return api.transformRepositoryData(response.repositories || [])
      } else if (sessionUser.isAdmin) {
        const response = await api.listAllRepositories()
        return api.transformRepositoryData(response.repositories || [])
      } else if (effectiveUsername) {
        // Backend returns all repos accessible to this user (owned + committed + permissioned)
        const response = await api.listRepositories(effectiveUsername)
        return api.transformRepositoryData(response.repositories || [])
      } else {
        return []
      }
    },
    [username]
  )
}

export const useUsers = () => {
  return useApiData(
    async () => {
      const response = await api.getUsers()
      const users = response?.users || []
      return users.map((user, index) => ({
        id: user.id ?? index + 1,
        name: user.full_name || user.username,
        email: user.email || '',
        username: user.username,
        avatar: (user.username || '?').slice(0, 2).toUpperCase(),
        role: user.role || 'developer',
        joinDate: user.created_at || '',
        totalCommits: user.total_commits ?? 0,
        activeRepos: user.active_repos ?? 0,
        lastActive: user.last_active || '',
        repositories: user.repositories || []
      }))
    },
    []
  )
}

export const useDashboardStats = () => {
  return useApiData(
    async () => {
      const stats = await api.getDashboardStats()
      const sessionUser = getSessionUser()
      const isPrivileged = sessionUser.isAdmin
      const accountLabel = isPrivileged ? 'Total Users' : 'My Account'
      const reposLabel = isPrivileged ? 'Active Repositories' : 'My Repositories'
      const commitsLabel = isPrivileged ? 'Total Commits' : 'My Commits'
      const archivedLabel = isPrivileged ? 'Archived Projects' : 'My Archived Repositories'
      
      // Transform to match the expected format
      return {
        stats: [
          {
            name: accountLabel,
            value: stats.totalUsers.toString(),
            change: '+0',
            changeType: 'increase',
            icon: 'FiUsers',
            color: 'from-blue-400 to-blue-600'
          },
          {
            name: reposLabel,
            value: stats.totalRepos.toString(),
            change: '+0',
            changeType: 'increase',
            icon: 'FiFolder',
            color: 'from-green-400 to-green-600'
          },
          {
            name: commitsLabel,
            value: stats.totalCommits.toString(),
            change: '+0',
            changeType: 'increase',
            icon: 'FiGitCommit',
            color: 'from-blue-500 to-blue-700'
          },
          {
            name: archivedLabel,
            value: stats.archivedProjects.toString(),
            change: '+0',
            changeType: 'increase',
            icon: 'FiArchive',
            color: 'from-orange-400 to-orange-600'
          }
        ],
        recentActivity: stats.recentActivity || []
      }
    },
    []
  )
}

export const useServerHealth = () => {
  return useApiData(
    async () => {
      try {
        await api.healthCheck()
        return { status: 'connected', message: 'Server is running' }
      } catch (error) {
        return { status: 'disconnected', message: error.message }
      }
    },
    []
  )
}

/** Recent platform activity. Cheap enough to poll while a dashboard is open. */
export const useActivities = (limit = 12) =>
  useApiData(() => api.getActivities(limit), [limit])
