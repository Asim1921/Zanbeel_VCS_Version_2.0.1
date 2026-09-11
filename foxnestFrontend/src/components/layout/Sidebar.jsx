import {
  FiUser,
  FiFolder,
  FiArchive,
  FiHome,
  FiX,
  FiShield,
  FiClock,
  FiKey,
  FiSettings,
  FiSearch,
  FiActivity,
  FiHardDrive,
  FiChevronLeft,
  FiChevronRight,
} from 'react-icons/fi'
import React, { useEffect, useState } from 'react'
import { cn } from '../../lib/utils'
import BrandLogo from '../ui/BrandLogo'

// One quiet treatment for every icon; the accent is spent only on the active
// item, so colour in the rail always means "you are here" rather than decoration.
const IDLE_ICON = 'bg-white/[0.05] text-ink-soft border border-border'
const ACTIVE_ICON = 'bg-accent text-[#04070e] shadow-[0_0_18px_-4px_rgba(59,157,255,0.9)]'

const Sidebar = ({ isOpen, setIsOpen, activeTab, setActiveTab, currentUser }) => {
  const isAdmin = ['team_lead', 'admin'].includes((currentUser?.role || '').toLowerCase())
  const [hoveredId, setHoveredId] = useState(null)

  const commonNavigation = [
    { name: 'Dashboard', id: 'dashboard', icon: FiHome, description: 'Overview of your activities' },
    { name: 'Repositories', id: 'repositories', icon: FiFolder, description: 'Browse all repositories' },
    { name: 'Search', id: 'search', icon: FiSearch, description: 'Find repositories, commits and code' },
    { name: 'Settings', id: 'settings', icon: FiSettings, description: 'Access tokens and SSH keys' },
  ]

  const adminNavigation = [
    { name: 'Activity', id: 'activity', icon: FiActivity, description: 'Audit log of everything that happened' },
    { name: 'Users & Permissions', id: 'users-management', icon: FiShield, description: 'Manage users and permissions' },
    { name: 'Password Reset', id: 'password-reset', icon: FiKey, description: 'Reset user passwords' },
    { name: 'Pending Approvals', id: 'pending-approvals', icon: FiClock, description: 'Review pending commits' },
    { name: 'Pending Repositories', id: 'pending-repositories', icon: FiFolder, description: 'Review pending repository requests' },
    { name: 'Pending Users', id: 'pending-user-registrations', icon: FiUser, description: 'Review account requests' },
    { name: 'Operations', id: 'operations', icon: FiHardDrive, description: 'Lockouts, backups and schema health' },
    { name: 'Archive', id: 'archive', icon: FiArchive, description: 'Archived projects' },
  ]

  const navigation = isAdmin ? [...commonNavigation, ...adminNavigation] : commonNavigation

  // Keep mobile drawer closed when switching to small screens
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)')
    const apply = (e) => {
      if (!e.matches) setSidebarOpen(false)
    }
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [])

  const renderNavItem = (item, index, { mode }) => {
    const Icon = item.icon
    const isActive = activeTab === item.id
    const collapsed = mode === 'collapsed'
    const mobile = mode === 'mobile'

    if (collapsed) {
      return (
        <div key={item.id} className="relative flex justify-center">
          <button
            type="button"
            onClick={() => setActiveTab(item.id)}
            onMouseEnter={() => setHoveredId(item.id)}
            onMouseLeave={() => setHoveredId(null)}
            onFocus={() => setHoveredId(item.id)}
            onBlur={() => setHoveredId(null)}
            aria-label={item.name}
            aria-current={isActive ? 'page' : undefined}
            className={cn(
              'flex h-11 w-11 items-center justify-center rounded-2xl transition-all duration-200',
              isActive
                ? ACTIVE_ICON
                : cn(IDLE_ICON, 'hover:border-border-strong hover:text-ink')
            )}
          >
            <Icon className="h-[18px] w-[18px]" />
          </button>

          {hoveredId === item.id && (
            <div
              role="tooltip"
              className="pointer-events-none absolute left-[calc(100%+10px)] top-1/2 z-50 -translate-y-1/2 whitespace-nowrap rounded-xl border border-border bg-surface px-3 py-2 text-xs font-medium text-ink shadow-[0_12px_32px_rgba(0,0,0,0.7)]"
            >
              <div className="font-semibold">{item.name}</div>
              <div className="mt-0.5 text-[11px] font-normal text-muted">{item.description}</div>
            </div>
          )}
        </div>
      )
    }

    return (
      <button
        key={item.id}
        type="button"
        onClick={() => {
          setActiveTab(item.id)
          if (mobile) setIsOpen(false)
        }}
        aria-current={isActive ? 'page' : undefined}
        className={cn(
          'group flex w-full items-center gap-3 rounded-2xl border px-3 py-2.5 text-left transition-all duration-200',
          isActive
            ? 'border-accent/35 bg-accent/[0.10] shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]'
            : 'border-transparent hover:border-border hover:bg-white/[0.04]'
        )}
      >
        <span
          className={cn(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition',
            isActive ? ACTIVE_ICON : IDLE_ICON
          )}
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div
            className={cn(
              'text-[13.5px] font-medium leading-tight',
              isActive ? 'text-ink' : 'text-ink-soft'
            )}
          >
            {item.name}
          </div>
        </div>
        <FiChevronRight
          className={cn(
            'h-4 w-4 shrink-0 transition',
            isActive ? 'text-accent opacity-100' : 'text-muted opacity-0 group-hover:opacity-100'
          )}
        />
      </button>
    )
  }

  return (
    <>
      {/* Desktop rail */}
      <aside
        className={cn(
          'fixed top-0 left-0 z-30 hidden h-dvh p-2 transition-[width] duration-300 ease-out sm:p-3 lg:block',
          isOpen ? 'w-[280px]' : 'w-[84px]'
        )}
      >
        <div className="panel-float flex h-full flex-col overflow-visible">
          {/* Brand */}
          <div
            className={cn(
              'flex shrink-0 items-center border-b border-border/80',
              isOpen ? 'h-16 px-4' : 'h-16 justify-center px-2'
            )}
          >
            <BrandLogo
              size={isOpen ? 40 : 34}
              withWordmark={isOpen}
              subtitle={isOpen ? 'Version control' : undefined}
              className={cn(!isOpen && 'justify-center')}
            />
          </div>

          {isOpen && (
            <p className="shrink-0 px-5 pt-4 pb-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              Navigate
            </p>
          )}

          <nav
            className={cn(
              'flex-1 overflow-y-auto overflow-x-visible py-2',
              isOpen ? 'space-y-1.5 px-3' : 'flex flex-col items-center gap-2 px-2 pt-3'
            )}
          >
            {navigation.map((item, index) =>
              renderNavItem(item, index, { mode: isOpen ? 'expanded' : 'collapsed' })
            )}
          </nav>

          <div className={cn('shrink-0 border-t border-border/80 p-3', !isOpen && 'px-2')}>
            <button
              type="button"
              onClick={() => setIsOpen(!isOpen)}
              aria-label={isOpen ? 'Collapse sidebar' : 'Expand sidebar'}
              className={cn(
                'flex items-center justify-center gap-2 rounded-2xl border border-border bg-cream-mid text-ink-soft transition hover:bg-cream-deep hover:text-ink',
                isOpen ? 'h-10 w-full px-3 text-sm' : 'mx-auto h-11 w-11'
              )}
            >
              {isOpen ? (
                <>
                  <FiChevronLeft className="h-4 w-4" />
                  <span>Collapse</span>
                </>
              ) : (
                <FiChevronRight className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </aside>

      {/* Mobile drawer */}
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 w-[min(100vw-3rem,288px)] transform p-3 transition-transform duration-300 ease-out lg:hidden',
          isOpen ? 'translate-x-0' : '-translate-x-full pointer-events-none'
        )}
        aria-hidden={!isOpen}
      >
        <div className="panel-float-lg flex h-full flex-col overflow-hidden">
          <div className="flex h-16 shrink-0 items-center justify-between gap-2 border-b border-border/80 px-4">
            <BrandLogo size={40} withWordmark subtitle="Version control" />
            <button
              type="button"
              onClick={() => setIsOpen(false)}
              className="rounded-full p-2 text-muted hover:bg-cream-deep hover:text-ink"
              aria-label="Close menu"
            >
              <FiX className="h-5 w-5" />
            </button>
          </div>

          <p className="px-5 pt-4 pb-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            Navigate
          </p>

          <nav className="flex-1 space-y-1.5 overflow-y-auto px-3 pb-6">
            {navigation.map((item, index) =>
              renderNavItem(item, index, { mode: 'mobile' })
            )}
          </nav>
        </div>
      </aside>
    </>
  )
}

export default Sidebar
