import { FiGitCommit, FiUsers, FiFolder, FiArchive, FiTrendingUp, FiActivity, FiWifi, FiWifiOff } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import { useDashboardStats, useRepositories, useServerHealth } from '../hooks/useApi'
import { API_SERVER_URL } from '../config'
import { getSessionUser } from '../utils/session'
import React from 'react'

const Dashboard = () => {
  const sessionUser = getSessionUser()
  const isPrivileged = sessionUser.isAdmin
  const welcomeTitle = isPrivileged ? 'Admin Overview' : `Welcome, ${sessionUser.username || 'Developer'}`
  const welcomeSubtitle = isPrivileged
    ? 'Monitor users, repositories, and activity across the entire platform.'
    : 'Track your repositories and development activity in one place.'

  const { data: dashboardData, loading: dashboardLoading, error: dashboardError } = useDashboardStats()
  const { data: repositories, loading: reposLoading } = useRepositories()
  const { data: serverHealth } = useServerHealth()

  // Loading state
  if (dashboardLoading) {
    return (
      <div className="space-y-6">
        <div className="mb-8 relative overflow-hidden rounded-2xl">
          <div className="absolute inset-0 bg-gradient-to-r from-purple-600/30 via-pink-600/30 to-purple-600/30 backdrop-blur-xl"></div>
          <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-white/5"></div>
          <div className="relative border border-white/20 rounded-2xl p-12 text-center animate-pulse">
            <h1 className="text-5xl font-bold text-white/50 mb-4">Loading...</h1>
            <p className="text-white/40 text-lg">Connecting to your development hub...</p>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {[...Array(4)].map((_, i) => (
            <GlassCard key={i} className="p-6 animate-pulse">
              <div className="flex items-center">
                <div className="w-12 h-12 bg-white/20 rounded-xl mr-4"></div>
                <div className="flex-1">
                  <div className="h-4 bg-white/20 rounded mb-2"></div>
                  <div className="h-6 bg-white/20 rounded"></div>
                </div>
              </div>
            </GlassCard>
          ))}
        </div>
      </div>
    )
  }

  // Error state
  if (dashboardError) {
    return (
      <div className="space-y-6">
        <div className="mb-8 relative overflow-hidden rounded-2xl">
          <div className="absolute inset-0 bg-gradient-to-r from-red-600/30 via-orange-600/30 to-red-600/30 backdrop-blur-xl"></div>
          <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-white/5"></div>
          <div className="relative border border-white/20 rounded-2xl p-12 text-center">
            <h1 className="text-5xl font-bold bg-gradient-to-r from-red-300 to-orange-300 bg-clip-text text-transparent mb-4">
              Connection Error
            </h1>
            <div className="flex items-center justify-center space-x-2">
              <FiWifiOff className="w-6 h-6 text-red-400" />
              <p className="text-red-300 text-lg">Unable to connect to server: {dashboardError}</p>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const stats = dashboardData?.stats || []
  const iconMap = {
    FiUsers,
    FiFolder,
    FiGitCommit,
    FiArchive
  }

  // Get top repositories (first 3)
  const topRepositories = repositories?.slice(0, 3) || []

  return (
    <div className="space-y-6">
      {/* Welcome Section with Gradient Background */}
      <div className="mb-8 relative overflow-hidden rounded-2xl">
        <div className="absolute inset-0 bg-gradient-to-r from-purple-600/30 via-pink-600/30 to-purple-600/30 backdrop-blur-xl"></div>
        <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-white/5"></div>
        <div className="relative border border-white/20 rounded-2xl p-12 text-center">
          <div className="mb-4">
            <h1 className="text-5xl font-bold bg-gradient-to-r from-purple-300 via-pink-300 to-purple-300 bg-clip-text text-transparent mb-4">
              {welcomeTitle}
            </h1>
            <p className="text-white/80 text-lg max-w-2xl mx-auto">
              {welcomeSubtitle}
            </p>
          </div>

          {/* Server Status - Centered */}
          <div className="flex items-center justify-center space-x-2 mt-6">
            {serverHealth?.status === 'connected' ? (
              <>
                <div className="w-2 h-2 bg-green-400 rounded-full animate-pulse"></div>
                <Badge variant="success">Connected to Server</Badge>
              </>
            ) : (
              <>
                <div className="w-2 h-2 bg-red-400 rounded-full animate-pulse"></div>
                <Badge variant="danger">Server Offline</Badge>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {stats.map((stat) => {
          const Icon = iconMap[stat.icon] || FiFolder
          return (
            <GlassCard key={stat.name} className="p-6">
              <div className="flex items-center">
                <div className={`flex-shrink-0 p-3 rounded-xl bg-gradient-to-r ${stat.color}`}>
                  <Icon className="w-6 h-6 text-white" />
                </div>
                <div className="ml-4 flex-1">
                  <p className="text-sm font-medium text-white/70">{stat.name}</p>
                  <div className="flex items-baseline">
                    <p className="text-2xl font-semibold text-white">{stat.value}</p>
                    {stat.change !== '+0' && (
                      <span className="ml-2 text-sm font-medium text-green-400">
                        {stat.change}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </GlassCard>
          )
        })}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Server Status Card */}
        <GlassCard className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-white flex items-center">
              <FiActivity className="w-5 h-5 mr-2" />
              Server Status
            </h3>
            <Badge variant={serverHealth?.status === 'connected' ? 'success' : 'danger'}>
              {serverHealth?.status === 'connected' ? 'Online' : 'Offline'}
            </Badge>
          </div>
          <div className="space-y-4">
            <div className="flex items-center space-x-3 p-3 rounded-xl bg-white/5">
              {serverHealth?.status === 'connected' ? (
                <FiWifi className="w-8 h-8 text-green-400" />
              ) : (
                <FiWifiOff className="w-8 h-8 text-red-400" />
              )}
              <div className="flex-1">
                <p className="text-sm text-white">
                  {serverHealth?.status === 'connected' ? 'Connected to zanbeel Server' : 'Unable to connect to server'}
                </p>
                <p className="text-xs text-white/50 mt-1">{serverHealth?.message}</p>
              </div>
            </div>
            {serverHealth?.status !== 'connected' && (
              <div className="text-sm text-orange-300 bg-orange-500/10 p-3 rounded-lg border border-orange-400/20">
                <strong>Note:</strong> Please ensure the zanbeel server is running on {API_SERVER_URL}
              </div>
            )}
          </div>
        </GlassCard>

        {/* Top Repositories */}
        <GlassCard className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-white flex items-center">
              <FiTrendingUp className="w-5 h-5 mr-2" />
              Top Repositories
            </h3>
            {reposLoading && <Badge variant="info">Loading...</Badge>}
          </div>
          <div className="space-y-4">
            {topRepositories.length > 0 ? (
              topRepositories.map((repo, index) => (
                <div key={index} className="p-4 rounded-xl bg-white/5 hover:bg-white/10 transition-colors cursor-pointer">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <h4 className="font-medium text-white mb-1">{repo.name}</h4>
                      <p className="text-sm text-white/70 mb-2">{repo.description}</p>
                      <div className="flex items-center space-x-4 text-xs text-white/50">
                        <span className="flex items-center">
                          <div className="w-2 h-2 bg-blue-400 rounded-full mr-1"></div>
                          {repo.language}
                        </span>
                        <span>{repo.commits} commits</span>
                        <span>{repo.contributors} contributors</span>
                      </div>
                    </div>
                    <div className="text-right">
                      <Badge variant="success">Active</Badge>
                      <p className="text-xs text-white/50 mt-1">{repo.lastUpdate}</p>
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <div className="text-center py-8 text-white/70">
                <FiFolder className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>No repositories found</p>
                <p className="text-sm text-white/50 mt-1">
                  {serverHealth?.status !== 'connected' 
                    ? 'Connect to server to view repositories' 
                    : 'Create your first repository to get started'
                  }
                </p>
              </div>
            )}
          </div>
        </GlassCard>
      </div>
    </div>
  )
}

export default Dashboard
