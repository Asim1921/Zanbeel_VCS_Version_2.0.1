import { FiUser, FiFolder, FiArchive, FiHome, FiMenu, FiX, FiShield, FiClock, FiKey } from 'react-icons/fi'
import React from 'react'

const Sidebar = ({ isOpen, setIsOpen, activeTab, setActiveTab, currentUser }) => {
  const isAdmin = ['team_lead', 'admin'].includes((currentUser?.role || '').toLowerCase())

  const commonNavigation = [
    {
      name: 'Dashboard',
      id: 'dashboard',
      icon: FiHome,
      description: 'Overview of your activities'
    },
    {
      name: 'Repositories',
      id: 'repositories',
      icon: FiFolder,
      description: 'Browse all repositories'
    }
  ]

  const adminNavigation = [
    {
      name: 'Users & Permissions',
      id: 'users-management',
      icon: FiShield,
      description: 'Manage users and permissions'
    },
    {
      name: 'Password Reset',
      id: 'password-reset',
      icon: FiKey,
      description: 'Reset user passwords'
    },
    {
      name: 'Pending Approvals',
      id: 'pending-approvals',
      icon: FiClock,
      description: 'Review pending commits'
    },
    {
      name: 'Pending Repositories',
      id: 'pending-repositories',
      icon: FiFolder,
      description: 'Review pending repository requests'
    },
    {
      name: 'Pending Users',
      id: 'pending-user-registrations',
      icon: FiUser,
      description: 'Review account requests'
    },
    {
      name: 'Archive',
      id: 'archive',
      icon: FiArchive,
      description: 'Archived projects'
    }
  ]

  const navigation = isAdmin ? [...commonNavigation, ...adminNavigation] : commonNavigation

  return (
    <>
      {/* Desktop Sidebar */}
      <div className={`fixed top-0 left-0 z-30 h-full transition-all duration-300 ${
        isOpen ? 'w-64' : 'w-20'
      } hidden lg:block`}>
        <div className="h-full bg-white/10 backdrop-blur-xl border-r border-white/20">
          {/* Logo */}
          <div className="flex items-center justify-center h-16 border-b border-white/20">
            <div className="flex items-center space-x-3">
              <div className="w-8 h-8 bg-gradient-to-br from-purple-400 to-pink-400 rounded-lg flex items-center justify-center">
                <span className="text-white font-bold text-sm">Z</span>
              </div>
              {isOpen && (
                <span className="text-white font-semibold text-xl">zanbeel</span>
              )}
            </div>
          </div>

          {/* Navigation */}
          <nav className="mt-8 px-4 space-y-2">
            {navigation.map((item) => {
              const Icon = item.icon
              const isActive = activeTab === item.id
              
              return (
                <button
                  key={item.id}
                  onClick={() => setActiveTab(item.id)}
                  className={`w-full flex items-center px-3 py-3 rounded-xl transition-all duration-200 group ${
                    isActive
                      ? 'bg-white/20 text-white backdrop-blur-sm border border-white/30'
                      : 'text-white/70 hover:text-white hover:bg-white/10'
                  }`}
                  title={!isOpen ? item.name : ''}
                >
                  <Icon className={`w-5 h-5 ${isOpen ? 'mr-3' : 'mx-auto'}`} />
                  {isOpen && (
                    <div className="flex-1 text-left">
                      <div className="font-medium">{item.name}</div>
                      <div className="text-xs text-white/50 mt-0.5">{item.description}</div>
                    </div>
                  )}
                </button>
              )
            })}
          </nav>

          {/* Toggle Button */}
          <button
            onClick={() => setIsOpen(!isOpen)}
            className="absolute bottom-6 left-4 right-4 flex items-center justify-center px-3 py-2 rounded-xl bg-white/10 text-white/70 hover:text-white hover:bg-white/20 transition-all duration-200"
          >
            <FiMenu className={`w-5 h-5 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
            {isOpen && <span className="ml-2 text-sm">Collapse</span>}
          </button>
        </div>
      </div>

      {/* Mobile Sidebar */}
      <div className={`fixed top-0 left-0 z-40 h-full w-64 transform transition-transform duration-300 lg:hidden ${
        isOpen ? 'translate-x-0' : '-translate-x-full'
      }`}>
        <div className="h-full bg-white/10 backdrop-blur-xl border-r border-white/20">
          {/* Header */}
          <div className="flex items-center justify-between h-16 px-4 border-b border-white/20">
            <div className="flex items-center space-x-3">
              <div className="w-8 h-8 bg-gradient-to-br from-purple-400 to-pink-400 rounded-lg flex items-center justify-center">
                <span className="text-white font-bold text-sm">F</span>
              </div>
              <span className="text-white font-semibold text-xl">FoxNest</span>
            </div>
            <button
              onClick={() => setIsOpen(false)}
              className="p-2 rounded-lg text-white/70 hover:text-white hover:bg-white/10 transition-colors"
            >
              <FiX className="w-5 h-5" />
            </button>
          </div>

          {/* Navigation */}
          <nav className="mt-8 px-4 space-y-2">
            {navigation.map((item) => {
              const Icon = item.icon
              const isActive = activeTab === item.id
              
              return (
                <button
                  key={item.id}
                  onClick={() => {
                    setActiveTab(item.id)
                    setIsOpen(false)
                  }}
                  className={`w-full flex items-center px-3 py-3 rounded-xl transition-all duration-200 ${
                    isActive
                      ? 'bg-white/20 text-white backdrop-blur-sm border border-white/30'
                      : 'text-white/70 hover:text-white hover:bg-white/10'
                  }`}
                >
                  <Icon className="w-5 h-5 mr-3" />
                  <div className="flex-1 text-left">
                    <div className="font-medium">{item.name}</div>
                    <div className="text-xs text-white/50 mt-0.5">{item.description}</div>
                  </div>
                </button>
              )
            })}
          </nav>
        </div>
      </div>
    </>
  )
}

export default Sidebar
