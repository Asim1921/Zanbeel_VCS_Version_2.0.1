import { FiGitCommit, FiUsers, FiFolder, FiArchive, FiTrendingUp, FiActivity, FiWifiOff, FiArrowUpRight } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import { BentoGrid, BentoGridItem } from '../components/aceternity/BentoGrid'
import SpotlightCard from '../components/react-bits/SpotlightCard'
import StatTile from '../components/aceternity/StatTile'
import ActivityFeed from '../components/ActivityFeed'
import FadeContent from '../components/react-bits/FadeContent'
import { useActivities, useDashboardStats, useRepositories, useServerHealth } from '../hooks/useApi'
import { getSessionUser } from '../utils/session'
import React, { useMemo } from 'react'
import { cn } from '../lib/utils'

const iconMap = { FiUsers, FiFolder, FiGitCommit, FiArchive }

/** Map dashboard stats → app tabs */
function resolveStatTarget(stat, isAdmin) {
  const key = (stat.icon || '').toLowerCase()
  const name = (stat.name || '').toLowerCase()

  if (key.includes('users') || name.includes('user') || name.includes('account')) {
    return isAdmin ? 'users-management' : null
  }
  if (key.includes('archive') || name.includes('archiv')) {
    return isAdmin ? 'archive' : 'repositories'
  }
  if (key.includes('commit') || name.includes('commit')) {
    return 'repositories'
  }
  if (key.includes('folder') || name.includes('repositor')) {
    return 'repositories'
  }
  return 'repositories'
}

const Dashboard = ({ setActiveTab, isAdmin: isAdminProp }) => {
  const sessionUser = getSessionUser()
  const isPrivileged = typeof isAdminProp === 'boolean' ? isAdminProp : sessionUser.isAdmin
  const welcomeTitle = isPrivileged
    ? 'Admin Overview'
    : `Welcome, ${sessionUser.username || 'Developer'}`
  const welcomeSubtitle = isPrivileged
    ? 'Monitor users, repositories, and activity across the entire platform.'
    : 'Track your repositories and development activity in one place.'

  const { data: dashboardData, loading: dashboardLoading, error: dashboardError } = useDashboardStats()
  const { data: repositories, loading: reposLoading } = useRepositories()
  const { data: serverHealth } = useServerHealth()
  const { data: activityData, loading: activityLoading } = useActivities(10)

  const stats = useMemo(() => dashboardData?.stats || [], [dashboardData])
  const topRepositories = repositories?.slice(0, 3) || []
  const activities = activityData?.activities || []

  const goTo = (tab) => {
    if (!tab || typeof setActiveTab !== 'function') return
    setActiveTab(tab)
  }

  if (dashboardLoading) {
    return (
      <div className="space-y-6">
        <div className="animate-pulse rounded-2xl border border-border bg-surface p-12 text-center">
          <div className="mx-auto h-8 w-48 rounded bg-cream-deep" />
          <div className="mx-auto mt-4 h-4 w-72 rounded bg-cream-mid" />
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <GlassCard key={i} className="animate-pulse p-6" hover={false}>
              <div className="flex items-center">
                <div className="mr-4 h-12 w-12 rounded-xl bg-cream-deep" />
                <div className="flex-1 space-y-2">
                  <div className="h-4 rounded bg-cream-mid" />
                  <div className="h-6 rounded bg-cream-deep" />
                </div>
              </div>
            </GlassCard>
          ))}
        </div>
      </div>
    )
  }

  if (dashboardError) {
    return (
      <GlassCard className="p-12 text-center" hover={false}>
        <h1 className="text-3xl font-semibold text-ink">Connection error</h1>
        <div className="mt-4 flex items-center justify-center gap-2 text-danger-fg">
          <FiWifiOff className="h-5 w-5" />
          <p>Unable to connect to server: {dashboardError}</p>
        </div>
      </GlassCard>
    )
  }

  return (
    <FadeContent className="space-y-6">
      <PageHeader
        title={welcomeTitle}
        subtitle={welcomeSubtitle}
        actions={
          serverHealth?.status === 'connected' ? (
            <Badge variant="success">Connected</Badge>
          ) : (
            <Badge variant="danger">Server offline</Badge>
          )
        }
      />

      <BentoGrid>
        {stats.map((stat) => {
          const Icon = iconMap[stat.icon] || FiFolder
          const target = resolveStatTarget(stat, isPrivileged)
          const clickable = Boolean(target)

          return (
            <BentoGridItem
              key={stat.name}
              className={cn(
                clickable &&
                  'cursor-pointer transition-all duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-[0_1px_0_rgba(255,255,255,0.05)_inset,0_18px_44px_rgba(0,0,0,0.6)] focus-within:ring-2 focus-within:ring-brand/30'
              )}
            >
              <button
                type="button"
                disabled={!clickable}
                onClick={() => goTo(target)}
                className={cn(
                  'relative w-full text-left outline-none',
                  !clickable && 'cursor-default'
                )}
                aria-label={
                  clickable ? `Open ${stat.name}` : undefined
                }
              >
                <StatTile
                  label={stat.name}
                  value={stat.value}
                  icon={Icon}
                  tone="accent"
                  hint={
                    stat.change && stat.change !== '+0'
                      ? `${stat.change} since last period`
                      : undefined
                  }
                  className="border-0 bg-transparent p-0 hover:border-0"
                />
              </button>
            </BentoGridItem>
          )
        })}
      </BentoGrid>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <SpotlightCard className="p-6">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="flex items-center text-lg font-semibold tracking-tight text-ink">
              <FiActivity className="mr-2 h-5 w-5 text-muted" />
              Recent activity
            </h3>
            {/* The activity tab is admin-only; App bounces everyone else back to
                the dashboard, so offering the link to a developer is a dead end. */}
            {isPrivileged && activities.length > 0 && (
              <button
                type="button"
                onClick={() => goTo('activity')}
                className="text-xs font-medium text-ink-soft underline-offset-2 hover:text-ink hover:underline"
              >
                View all
              </button>
            )}
          </div>
          {/* The endpoint returns `user` and `repository`; ActivityFeed reads
              `username` and `repository_name`, so without this the dashboard feed
              rendered every row with no author and no repository. */}
          <ActivityFeed
            activities={activities.map((a) => ({
              ...a,
              username: a.user || a.username,
              repository_name: a.repository || a.repository_name,
            }))}
            loading={activityLoading}
            emptyHint="No activity recorded yet."
          />
        </SpotlightCard>

        <SpotlightCard className="p-6">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="flex items-center text-lg font-semibold tracking-tight text-ink">
              <FiTrendingUp className="mr-2 h-5 w-5 text-muted" />
              Top repositories
            </h3>
            <div className="flex items-center gap-2">
              {reposLoading && <Badge variant="info">Loading…</Badge>}
              <button
                type="button"
                onClick={() => goTo('repositories')}
                className="text-xs font-medium text-ink-soft underline-offset-2 hover:text-ink hover:underline"
              >
                View all
              </button>
            </div>
          </div>
          <div className="space-y-3">
            {topRepositories.length > 0 ? (
              topRepositories.map((repo, index) => (
                <button
                  key={index}
                  type="button"
                  onClick={() => goTo('repositories')}
                  className="w-full rounded-xl border border-border bg-white/[0.03] p-4 text-left transition hover:border-border-strong hover:bg-white/[0.06]"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <h4 className="mb-1 font-medium text-ink">{repo.name}</h4>
                      <p className="mb-2 line-clamp-2 text-sm text-muted">{repo.description}</p>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
                        <span>{repo.language}</span>
                        <span>{repo.commits} commits</span>
                        <span>{repo.contributors} contributors</span>
                      </div>
                    </div>
                    <div className="text-right">
                      <Badge variant="success">Active</Badge>
                      <p className="mt-1 text-xs text-muted">{repo.lastUpdate}</p>
                    </div>
                  </div>
                </button>
              ))
            ) : (
              <button
                type="button"
                onClick={() => goTo('repositories')}
                className="w-full py-8 text-center text-muted transition hover:text-ink"
              >
                <FiFolder className="mx-auto mb-3 h-10 w-10 opacity-50" />
                <p>No repositories found</p>
                <p className="mt-1 text-sm">
                  {serverHealth?.status !== 'connected'
                    ? 'Connect to server to view repositories'
                    : 'Create your first repository to get started'}
                </p>
              </button>
            )}
          </div>
        </SpotlightCard>
      </div>
    </FadeContent>
  )
}

export default Dashboard
