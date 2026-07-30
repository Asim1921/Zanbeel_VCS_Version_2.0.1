import React,{ useState } from 'react'
import { FiUser, FiGitCommit, FiFolder, FiCalendar, FiMail, FiWifi, FiWifiOff } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import { useUsers, useServerHealth } from '../hooks/useApi'
import { API_SERVER_URL } from '../config'

const Users = () => {
  const [selectedUser, setSelectedUser] = useState(null)
  const { data: users, loading: usersLoading, error: usersError } = useUsers()
  const { data: serverHealth } = useServerHealth()

  // Loading state
  if (usersLoading) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white mb-2">Users</h1>
            <p className="text-white/70">Loading user data...</p>
          </div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
          {[...Array(6)].map((_, i) => (
            <GlassCard key={i} className="p-6 animate-pulse">
              <div className="flex items-center space-x-4 mb-4">
                <div className="w-12 h-12 bg-white/20 rounded-full"></div>
                <div className="flex-1">
                  <div className="h-4 bg-white/20 rounded mb-2"></div>
                  <div className="h-3 bg-white/20 rounded"></div>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="h-16 bg-white/20 rounded"></div>
                <div className="h-16 bg-white/20 rounded"></div>
              </div>
            </GlassCard>
          ))}
        </div>
      </div>
    )
  }

  // Error state
  if (usersError) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white mb-2">Users</h1>
            <div className="flex items-center space-x-2">
              <FiWifiOff className="w-5 h-5 text-red-400" />
              <p className="text-red-300">Error loading user data: {usersError}</p>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const handleUserClick = (user) => {
    setSelectedUser(selectedUser?.id === user.id ? null : user)
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Users</h1>
          <p className="text-white/70">View all users and their repository contributions</p>
        </div>
        <div className="flex items-center space-x-3">
          <Button variant="primary">
            <FiUser className="w-4 h-4 mr-2" />
            Add User
          </Button>
          {/* Server Status Indicator */}
          <div className="flex items-center space-x-2">
            {serverHealth?.status === 'connected' ? (
              <Badge variant="success" className="flex items-center">
                <FiWifi className="w-3 h-3 mr-1" />
                Connected
              </Badge>
            ) : (
              <Badge variant="danger" className="flex items-center">
                <FiWifiOff className="w-3 h-3 mr-1" />
                Offline
              </Badge>
            )}
          </div>
        </div>
      </div>

      {/* Users Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
        {users && users.length > 0 ? (
          users.map((user) => (
            <GlassCard 
              key={user.id} 
              className={`p-6 cursor-pointer transition-all duration-300 ${
                selectedUser?.id === user.id ? 'ring-2 ring-blue-400 bg-white/15' : ''
              }`}
              onClick={() => handleUserClick(user)}
            >
              {/* User Header */}
              <div className="flex items-center space-x-4 mb-4">
                <div className="w-12 h-12 bg-gradient-to-br from-blue-400 to-blue-600 rounded-full flex items-center justify-center flex-shrink-0">
                  <span className="text-white font-semibold text-lg">{user.avatar}</span>
                </div>
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-white truncate">{user.name}</h3>
                  <p className="text-sm text-white/70 truncate">@{user.username}</p>
                  <Badge variant="info" className="mt-1">{user.role}</Badge>
                </div>
              </div>

              {/* User Stats */}
              <div className="grid grid-cols-2 gap-4 mb-4">
                <div className="text-center p-3 rounded-lg bg-white/10">
                  <div className="flex items-center justify-center mb-1">
                    <FiGitCommit className="w-4 h-4 text-green-400 mr-1" />
                    <span className="text-lg font-semibold text-white">{user.totalCommits}</span>
                  </div>
                  <p className="text-xs text-white/70">Total Commits</p>
                </div>
                <div className="text-center p-3 rounded-lg bg-white/10">
                  <div className="flex items-center justify-center mb-1">
                    <FiFolder className="w-4 h-4 text-blue-400 mr-1" />
                    <span className="text-lg font-semibold text-white">{user.activeRepos}</span>
                  </div>
                  <p className="text-xs text-white/70">Active Repos</p>
                </div>
              </div>

              {/* User Info */}
              <div className="space-y-2 text-sm text-white/70">
                <div className="flex items-center">
                  <FiMail className="w-4 h-4 mr-2" />
                  <span className="truncate">{user.email}</span>
                </div>
                <div className="flex items-center">
                  <FiCalendar className="w-4 h-4 mr-2" />
                  <span>Joined {new Date(user.joinDate).toLocaleDateString()}</span>
                </div>
                <div className="flex items-center">
                  <div className="w-2 h-2 bg-green-400 rounded-full mr-2"></div>
                  <span>Last active {user.lastActive}</span>
                </div>
              </div>
            </GlassCard>
          ))
        ) : (
          <div className="col-span-full text-center py-12">
            <FiUser className="w-16 h-16 mx-auto mb-4 text-white/30" />
            <h3 className="text-xl font-semibold text-white mb-2">No Users Found</h3>
            <p className="text-white/70 mb-4">
              {serverHealth?.status !== 'connected' 
                ? 'Connect to the server to view users' 
                : 'No users have been added to the system yet'
              }
            </p>
            {serverHealth?.status !== 'connected' && (
              <div className="text-sm text-orange-300 bg-orange-500/10 p-3 rounded-lg border border-orange-400/20 max-w-md mx-auto">
                <strong>Note:</strong> Please ensure the zanbeel server is running on {API_SERVER_URL}
              </div>
            )}
          </div>
        )}
      </div>

      {/* User Details Modal */}
      {selectedUser && (
        <GlassCard className="p-6 mt-6">
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-xl font-semibold text-white">
              {selectedUser.name}'s Repositories
            </h2>
            <Button 
              variant="ghost" 
              size="sm"
              onClick={() => setSelectedUser(null)}
            >
              Close
            </Button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {selectedUser.repositories && selectedUser.repositories.length > 0 ? (
              selectedUser.repositories.map((repo, index) => (
                <div 
                  key={index} 
                  className="p-4 rounded-xl bg-white/10 hover:bg-white/15 transition-colors cursor-pointer border border-white/10"
                >
                  <div className="flex items-start justify-between mb-2">
                    <h4 className="font-medium text-white text-sm">{repo.name}</h4>
                    <Badge variant="default">{repo.commits}</Badge>
                  </div>
                  <div className="space-y-1 text-xs text-white/70">
                    <div className="flex items-center">
                      <div className="w-2 h-2 bg-blue-400 rounded-full mr-2"></div>
                      <span>{repo.language}</span>
                    </div>
                    <p>Last commit: {repo.lastCommit}</p>
                  </div>
                </div>
              ))
            ) : (
              <div className="col-span-full text-center py-8 text-white/70">
                <FiFolder className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>No repositories found for this user</p>
              </div>
            )}
          </div>
        </GlassCard>
      )}
    </div>
  )
}

export default Users
