import { API_BASE_URL } from '../config'
import { getSessionToken, clearSessionUser, setSessionUser, getSessionUser } from './session'

class FoxNestAPI {
  constructor() {
    this.baseURL = API_BASE_URL
  }

  formatSize(bytes = 0) {
    const num = Number(bytes) || 0
    if (num === 0) return '0 KB'
    const units = ['B', 'KB', 'MB', 'GB']
    const exponent = Math.min(Math.floor(Math.log(num) / Math.log(1024)), units.length - 1)
    const value = num / Math.pow(1024, exponent)
    const rounded = exponent === 0 ? value.toFixed(0) : value.toFixed(1)
    return `${rounded} ${units[exponent]}`
  }

  async request(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`
    const token = getSessionToken()
    const timeout = options.timeout || 10000 // 10 second default timeout
    
    const config = {
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
      ...options,
    }

    if (config.body && typeof config.body === 'object') {
      config.body = JSON.stringify(config.body)
    }

    try {
      // Create abort controller for timeout
      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), timeout)
      config.signal = controller.signal
      
      const response = await fetch(url, config)
      clearTimeout(timeoutId)
      
      if (!response.ok) {
        // Only clear session on auth-specific 401 errors (login, /auth/me endpoints)
        if (response.status === 401 && (endpoint.includes('/auth/login') || endpoint.includes('/auth/me'))) {
          clearSessionUser()
        }
        const errorData = await response.json().catch(() => ({ detail: 'Network error' }))
        const detail = errorData?.detail
        const detailMessage = typeof detail === 'string' ? detail : (detail?.message || null)
        const message = detailMessage || errorData?.message || `HTTP ${response.status}`
        const error = new Error(message)
        error.status = response.status
        error.data = errorData
        error.code = (typeof detail === 'object' && detail?.code) ? detail.code : (errorData?.code || null)
        throw error
      }
      
      return await response.json()
    } catch (error) {
      if (error.name === 'AbortError') {
        console.error(`API Request timeout: ${endpoint}`)
        throw new Error('Request timed out. Server is not responding.')
      }
      console.error(`API Request failed: ${endpoint}`, error)
      throw error
    }
  }

  // Repository endpoints
  async login(username, password) {
    try {
      const response = await this.request('/auth/login', {
        method: 'POST',
        body: { username, password },
        timeout: 5000 // 5 second timeout for login
      })

      if (response?.success && response?.access_token && response?.user) {
        setSessionUser({
          username: response.user.username,
          role: response.user.role,
          token: response.access_token
        })
      }

      return response
    } catch (error) {
      if (error.name === 'AbortError') {
        throw new Error('Login request timed out. Server is not responding. Please try again.')
      }
      throw error
    }
  }

  async getCurrentUser() {
    return this.request('/auth/me')
  }

  async changePassword(currentPassword, newPassword) {
    return this.request('/auth/change-password', {
      method: 'POST',
      body: {
        current_password: currentPassword,
        new_password: newPassword
      }
    })
  }

  async forgotPassword(email) {
    return this.request('/auth/forgot-password', {
      method: 'POST',
      body: { email }
    })
  }

  async resetPasswordWithOtp(email, otp, newPassword) {
    return this.request('/auth/reset-password', {
      method: 'POST',
      body: {
        email,
        otp,
        new_password: newPassword
      }
    })
  }

  async bootstrapPassword(username, newPassword, setupKey) {
    return this.request('/auth/bootstrap-password', {
      method: 'POST',
      body: {
        username,
        new_password: newPassword,
        setup_key: setupKey
      }
    })
  }

  async requestRegistration(payload) {
    return this.request('/auth/register-request', {
      method: 'POST',
      body: payload
    })
  }

  async getUsers() {
    return this.request('/users')
  }

  async getUsersEngagement() {
    return this.request('/admin/users/engagement')
  }

  async getUserDetail(username, limit = 100) {
    return this.request(`/admin/users/${encodeURIComponent(username)}/detail?limit=${limit}`)
  }

  async requestPasswordResetOtp(username) {
    return this.request(`/admin/users/${encodeURIComponent(username)}/reset-password/request-otp`, {
      method: 'POST',
      body: {}
    })
  }

  async resetPassword(username, newPassword, otp) {
    return this.request(`/admin/users/${encodeURIComponent(username)}/reset-password`, {
      method: 'POST',
      body: {
        new_password: newPassword,
        otp
      }
    })
  }

  async listPendingUserRegistrations(status = 'pending') {
    const params = new URLSearchParams()
    if (status) params.append('status', status)
    const suffix = params.toString() ? `?${params.toString()}` : ''
    return this.request(`/admin/pending-user-registrations${suffix}`)
  }

  async reviewPendingUserRegistration(requestId, payload) {
    return this.request(`/admin/pending-user-registrations/${requestId}/review`, {
      method: 'POST',
      body: payload
    })
  }

  async createRepository(username, repoName) {
    return this.request('/repository/create', {
      method: 'POST',
      body: { username, repo_name: repoName },
    })
  }

  async listRepositories(username = null, repoName = null) {
    const params = new URLSearchParams()
    if (username) params.append('username', username)
    if (repoName) params.append('repo_name', repoName)
    const query = params.toString()
    return this.request(`/repository/list${query ? `?${query}` : ''}`)
  }

  async listAllRepositories() {
    return this.request('/repositories/all')
  }

  async getRepository(repoId) {
    return this.request(`/repository/${repoId}`)
  }

  async getRepositoryContributors(repoId) {
    return this.request(`/repository/${repoId}/contributors`)
  }

  async getCommits(repoId, full = false, branch = null) {
    const params = new URLSearchParams({ full: full.toString() })
    if (branch) params.append('branch', branch)
    return this.request(`/repository/${repoId}/commits?${params}`)
  }

  async getBranches(repoId) {
    return this.request(`/repository/${repoId}/branches`)
  }

  async starRepository(repoId) {
    return this.request(`/repository/${repoId}/star`, { method: 'POST' })
  }

  async unstarRepository(repoId) {
    return this.request(`/repository/${repoId}/star`, { method: 'DELETE' })
  }

  async listPullRequests(repoId, status = null) {
    const params = new URLSearchParams()
    if (status) params.append('status', status)
    const suffix = params.toString() ? `?${params.toString()}` : ''
    return this.request(`/repository/${repoId}/pull-requests${suffix}`)
  }

  async createPullRequest(repoId, payload) {
    return this.request(`/repository/${repoId}/pull-requests`, {
      method: 'POST',
      body: payload,
    })
  }

  async closePullRequest(repoId, prId) {
    return this.request(`/repository/${repoId}/pull-requests/${prId}/close`, {
      method: 'POST',
    })
  }

  async mergePullRequest(repoId, prId, expectedHeadCommitId = null) {
    return this.request(`/repository/${repoId}/pull-requests/${prId}/merge`, {
      method: 'POST',
      body: expectedHeadCommitId ? { expected_head_commit_id: expectedHeadCommitId } : {},
    })
  }

  // Activity feed. Implemented server-side and populated for a long time, but no
  // screen ever called it.
  async getActivities(limit = 20) {
    return this.request(`/activities?limit=${limit}`)
  }

  // Merge conflict resolution. A conflicted merge returns code MERGE_CONFLICT with a
  // resolve_url; startMergeConflictSession() parks the three sides of each conflicted
  // file so they can be reconciled by hand.
  async startMergeConflictSession(repoId, prId) {
    return this.request(`/repository/${repoId}/pull-requests/${prId}/conflicts`, {
      method: 'POST',
      body: {},
    })
  }

  async getMergeConflicts(repoId, prId, sessionId) {
    return this.request(`/repository/${repoId}/pull-requests/${prId}/conflicts/${sessionId}`)
  }

  async resolveMergeConflicts(repoId, prId, sessionId, payload) {
    return this.request(
      `/repository/${repoId}/pull-requests/${prId}/conflicts/${sessionId}/resolve`,
      { method: 'POST', body: payload, timeout: 60000 }
    )
  }

  async abortMergeConflicts(repoId, prId, sessionId, expectedHeadCommitId = null) {
    return this.request(
      `/repository/${repoId}/pull-requests/${prId}/conflicts/${sessionId}/abort`,
      {
        method: 'POST',
        body: expectedHeadCommitId ? { expected_head_commit_id: expectedHeadCommitId } : {},
      }
    )
  }

  // Branch protection and the operations it governs. The panel speaks camelCase; the
  // API speaks snake_case, so the mapping lives here rather than in the component.
  async getBranchPolicy(repoId) {
    return this.request(`/repository/${repoId}/branch-policy`)
  }

  async updateBranchPolicy(repoId, policy) {
    return this.request(`/repository/${repoId}/branch-policy`, {
      method: 'PUT',
      body: policy,
    })
  }

  async createBranch(repoId, { name, fromBranch, fromCommit } = {}) {
    return this.request(`/repository/${repoId}/branches`, {
      method: 'POST',
      body: { name, from_branch: fromBranch || null, from_commit: fromCommit || null },
    })
  }

  async mergeBranches(repoId, { sourceBranch, targetBranch, expectedHeadCommitId, dryRun } = {}) {
    return this.request(`/repository/${repoId}/branches/merge`, {
      method: 'POST',
      body: {
        source_branch: sourceBranch,
        target_branch: targetBranch,
        expected_head_commit_id: expectedHeadCommitId || null,
        dry_run: !!dryRun,
      },
      timeout: 60000,
    })
  }

  async publishBranch(repoId, { sourceBranch, targetBranch, expectedHeadCommitId } = {}) {
    return this.request(`/repository/${repoId}/branches/publish`, {
      method: 'POST',
      body: {
        source_branch: sourceBranch,
        target_branch: targetBranch,
        expected_head_commit_id: expectedHeadCommitId || null,
      },
    })
  }

  async copyFilesFromBranch(repoId, { sourceBranch, targetBranch, paths, expectedHeadCommitId } = {}) {
    return this.request(`/repository/${repoId}/branches/copy-files`, {
      method: 'POST',
      body: {
        source_branch: sourceBranch,
        target_branch: targetBranch,
        paths: paths || [],
        expected_head_commit_id: expectedHeadCommitId || null,
      },
      timeout: 60000,
    })
  }

  // Issue tracking
  async listIssues(repoId, params = {}) {
    const qs = new URLSearchParams()
    if (params.status) qs.append('status', params.status)
    if (params.search) qs.append('search', params.search)
    if (params.label) qs.append('label', params.label)
    if (params.milestone_id != null) qs.append('milestone_id', String(params.milestone_id))
    if (params.assignee) qs.append('assignee', params.assignee)
    if (params.offset != null) qs.append('offset', String(params.offset))
    if (params.limit != null) qs.append('limit', String(params.limit))
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return this.request(`/repository/${repoId}/issues${suffix}`)
  }

  async createIssue(repoId, payload) {
    return this.request(`/repository/${repoId}/issues`, {
      method: 'POST',
      body: payload
    })
  }

  async getIssue(repoId, issueNumber) {
    return this.request(`/repository/${repoId}/issues/${issueNumber}`)
  }

  async updateIssue(repoId, issueNumber, payload) {
    return this.request(`/repository/${repoId}/issues/${issueNumber}`, {
      method: 'PUT',
      body: payload
    })
  }

  async addIssueComment(repoId, issueNumber, payload) {
    return this.request(`/repository/${repoId}/issues/${issueNumber}/comments`, {
      method: 'POST',
      body: payload
    })
  }

  async watchIssue(repoId, issueNumber, watch = true) {
    return this.request(`/repository/${repoId}/issues/${issueNumber}/watch`, {
      method: 'POST',
      body: { watch: !!watch }
    })
  }

  async listIssueLabels(repoId) {
    return this.request(`/repository/${repoId}/issue-labels`)
  }

  async upsertIssueLabel(repoId, payload) {
    return this.request(`/repository/${repoId}/issue-labels`, {
      method: 'POST',
      body: payload
    })
  }

  async listMilestones(repoId, includeClosed = true) {
    const qs = new URLSearchParams()
    qs.append('include_closed', includeClosed ? 'true' : 'false')
    return this.request(`/repository/${repoId}/milestones?${qs.toString()}`)
  }

  async createMilestone(repoId, payload) {
    return this.request(`/repository/${repoId}/milestones`, {
      method: 'POST',
      body: payload
    })
  }

  // In-app notifications
  async listNotifications({ unreadOnly = false, limit = 50 } = {}) {
    const qs = new URLSearchParams()
    if (unreadOnly) qs.append('unread_only', 'true')
    if (limit != null) qs.append('limit', String(limit))
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return this.request(`/notifications${suffix}`)
  }

  async markNotificationsRead(ids = []) {
    return this.request(`/notifications/mark-read`, {
      method: 'POST',
      body: { ids }
    })
  }

  async getRepositoryFiles(repoId, branch = null, { includeContent = false } = {}) {
    const params = new URLSearchParams()
    if (branch) params.append('branch', branch)
    // A listing only needs metadata. The browser asks for content so the whole
    // tree is readable from one response instead of a request per file opened.
    params.append('include_content', includeContent ? 'true' : 'false')
    const suffix = params.toString() ? `?${params.toString()}` : ''
    return this.request(`/repository/${repoId}/files${suffix}`, {
      timeout: includeContent ? 120000 : undefined,
    })
  }

  async getRepositoryFile(repoId, path, branch = null) {
    const params = new URLSearchParams({ path })
    if (branch) params.append('branch', branch)
    return this.request(`/repository/${repoId}/file?${params.toString()}`)
  }

  async getFileHistory(repoId, path, branch = null, limit = 100, followRenames = false, cursor = null) {
    const params = new URLSearchParams({ path, limit: String(limit) })
    if (branch) params.append('branch', branch)
    if (followRenames) params.append('follow_renames', 'true')
    if (cursor) params.append('cursor', cursor)
    return this.request(`/repository/${repoId}/file-history?${params.toString()}`)
  }

  async getBranchProtection(repoId) {
    return this.request(`/repository/${repoId}/branch-protection`)
  }

  async setBranchProtection(repoId, branchPattern, mode, expectedPolicyVersion = null) {
    return this.request(`/repository/${repoId}/branch-protection`, {
      method: 'PUT',
      body: {
        branch_pattern: branchPattern,
        mode,
        expected_policy_version: expectedPolicyVersion,
      },
    })
  }

  async requestBranchUnlock(repoId, branch, { reason, operations = ['update'], minutes = 30 }) {
    return this.request(`/repository/${repoId}/branches/${encodeURIComponent(branch)}/unlock-requests`, {
      method: 'POST',
      body: { reason, operations, expires_in_minutes: minutes },
    })
  }

  async getReferenceAudit(repoId, { reference = null, limit = 100 } = {}) {
    const params = new URLSearchParams({ limit: String(limit) })
    if (reference) params.append('reference', reference)
    return this.request(`/repository/${repoId}/audit/references?${params.toString()}`)
  }

  async listImmutableReleases(repoId) {
    return this.request(`/repository/${repoId}/releases/immutable`)
  }

  async blameFile(repoId, path, { commit = null, branch = null } = {}) {
    const params = new URLSearchParams({ path })
    if (commit) params.append('commit', commit)
    else if (branch) params.append('branch', branch)
    // Blame walks the whole history of a file, so it can outrun the 10s default.
    return this.request(`/repository/${repoId}/blame?${params.toString()}`, { timeout: 60000 })
  }

  async compareCommits(repoId, fromCommit, toCommit, path = null) {
    const params = new URLSearchParams({ from_commit: fromCommit, to_commit: toCommit })
    if (path) params.append('path', path)
    return this.request(`/repository/${repoId}/compare?${params.toString()}`)
  }

  async rollbackFile(repoId, payload) {
    return this.request(`/repository/${repoId}/rollback/file`, {
      method: 'POST',
      body: payload
    })
  }

  async rollbackBranch(repoId, payload) {
    return this.request(`/repository/${repoId}/rollback/branch`, {
      method: 'POST',
      body: payload
    })
  }

  async getRepositoryDocs(repoId, branch = null) {
    const params = new URLSearchParams()
    if (branch) params.append('branch', branch)
    const suffix = params.toString() ? `?${params.toString()}` : ''
    return this.request(`/repository/${repoId}/docs${suffix}`)
  }

  /** URL to download all generated docs as ZIP (use with API_SERVER_URL prefix). */
  getRepositoryDocsArchivePath(repoId) {
    return `/api/repository/${repoId}/docs/archive.zip`
  }

  async pushCommit(repoId, commit) {
    return this.request(`/repository/${repoId}/push`, {
      method: 'POST',
      body: { commit },
    })
  }

  async pullCommits(repoId, sinceCommit = null) {
    const params = sinceCommit ? `?since_commit=${sinceCommit}` : ''
    return this.request(`/repository/${repoId}/pull${params}`)
  }

  async deleteRepository(repoId, actorUsername) {
    const params = new URLSearchParams()
    if (actorUsername) params.append('actor_username', actorUsername)

    const suffix = params.toString() ? `?${params.toString()}` : ''

    return this.request(`/repository/${repoId}${suffix}`, {
      method: 'DELETE',
    })
  }

  async archiveRepository(repoId, reason = null, actorUsername) {
    const params = new URLSearchParams()
    if (actorUsername) params.append('actor_username', actorUsername)

    const suffix = params.toString() ? `?${params.toString()}` : ''

    return this.request(`/repository/${repoId}/archive${suffix}`, {
      method: 'POST',
      body: { reason: reason || 'Archived via web interface' },
    })
  }

  // Health check
  async healthCheck() {
    return this.request('/', { method: 'GET' })
  }

  // Helper methods for data transformation
  transformRepositoryData(repositories) {
    return repositories.map(repo => {
      // Get the latest commit timestamp if commits exist
      let latestTimestamp = repo.updated_at || repo.created_at
      if (repo.commits && Array.isArray(repo.commits) && repo.commits.length > 0) {
        const latestCommit = repo.commits[0]
        latestTimestamp = latestCommit.timestamp || latestTimestamp
      }
      
      return {
        id: repo.id,
        name: repo.name,
        description: repo.description || `Repository owned by ${repo.owner}`,
        language: repo.language, // Use language from backend (null if not set)
        languageColor: '#6c757d',
        commits: repo.commits?.length || 0,
        contributors: repo.contributor_count ?? 1,
        stars: repo.star_count ?? 0,
        starredByMe: !!repo.starred_by_me,
        watchers: 0, // Not implemented in server yet
        branches: repo.branch_count ?? 1,
        size: this.formatSize(repo.size || repo.size_bytes || 0),
        lastUpdate: this.formatDate(latestTimestamp),
        status: repo.is_archived ? 'archived' : 'active',
        visibility: repo.is_public === false ? 'private' : 'public',
        tags: [],
        files: [], // Empty array to prevent undefined error
        owner: repo.owner,
        createdAt: repo.created_at,
        head: repo.head,
        is_archived: repo.is_archived || false,
        archived_at: repo.archived_at,
        archived_reason: repo.archived_reason,
        g1_coordinator: repo.g1_coordinator,
        tested: repo.tested,
        current_user_can_write: !!repo.current_user_can_write,
        current_user_can_manage: !!repo.current_user_can_manage
      }
    })
  }

  transformCommitData(commits) {
    return commits.map(commit => ({
      id: commit.id,
      message: commit.message,
      author: commit.author,
      timestamp: commit.timestamp,
      parent: commit.parent,
      files: Array.isArray(commit.files) ? commit.files : Object.keys(commit.files || {})
    }))
  }

  parseServerTimestamp(dateString) {
    if (!dateString) return null
    if (typeof dateString !== 'string') return new Date(dateString)

    const normalized = dateString.replace(' ', 'T')
    const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(normalized)
    const withTz = hasTz ? normalized : `${normalized}Z`
    const parsed = new Date(withTz)
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }

  formatDate(dateString) {
    const parsedDate = this.parseServerTimestamp(dateString)
    if (!parsedDate) return 'Unknown'

    const now = new Date()
    const diffMs = now - parsedDate
    const diffMinutes = Math.floor(diffMs / (1000 * 60))
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60))
    const diffDays = Math.floor(diffHours / 24)

    if (diffMinutes < 1) return 'just now'
    if (diffMinutes < 60) return `${diffMinutes} minute${diffMinutes === 1 ? '' : 's'} ago`
    if (diffHours < 24) return `${diffHours} hour${diffHours === 1 ? '' : 's'} ago`
    if (diffDays === 1) return '1 day ago'
    if (diffDays < 7) return `${diffDays} days ago`
    if (diffDays < 30) return `${Math.floor(diffDays / 7)} week${Math.floor(diffDays / 7) === 1 ? '' : 's'} ago`
    
    // For display of the actual date, use local timezone
    return parsedDate.toLocaleString('en-US', { 
      year: 'numeric', 
      month: '2-digit', 
      day: '2-digit', 
      hour: '2-digit', 
      minute: '2-digit', 
      hour12: true 
    })
  }

  // Get user statistics
  async getUserStats(username) {
    try {
      const reposResponse = await this.listRepositories(username)
      const repositories = reposResponse.repositories || []
      
      let totalCommits = 0
      const repoDetails = []

      for (const repo of repositories) {
        try {
          const commitsResponse = await this.getCommits(repo.id, false)
          const commits = commitsResponse.commits || []
          totalCommits += commits.length
          
          repoDetails.push({
            name: repo.name,
            commits: commits.length,
            lastCommit: commits.length > 0 ? this.formatDate(commits[0].timestamp) : 'No commits',
            language: 'Unknown' // You might want to detect this
          })
        } catch (error) {
          console.error(`Error fetching commits for repo ${repo.id}:`, error)
        }
      }

      return {
        totalCommits,
        activeRepos: repositories.length,
        repositories: repoDetails
      }
    } catch (error) {
      console.error('Error fetching user stats:', error)
      return {
        totalCommits: 0,
        activeRepos: 0,
        repositories: []
      }
    }
  }

  // Get dashboard statistics
  async getDashboardStats() {
    try {
      const sessionUser = getSessionUser()
      const isPrivileged = sessionUser.isAdmin
      const hasUsername = !!sessionUser.username

      const repoRequest = isPrivileged
        ? this.listAllRepositories()
        : (hasUsername ? this.listRepositories(sessionUser.username) : Promise.resolve({ success: true, repositories: [] }))

      // Keep admin/team lead behavior unchanged; regular users get user-scoped dashboard data.
      const [reposResult, usersResult] = await Promise.allSettled([
        repoRequest,
        isPrivileged ? this.request('/users') : Promise.resolve({ success: true, users: [] })
      ])

      const reposResponse = reposResult.status === 'fulfilled' ? reposResult.value : { success: false, repositories: [] }
      const usersResponse = usersResult.status === 'fulfilled' ? usersResult.value : { success: false, users: [] }
      
      if (!reposResponse.success) {
        return {
          totalUsers: 0,
          totalRepos: 0,
          totalCommits: 0,
          archivedProjects: 0,
          recentActivity: []
        }
      }

      // Backend already scopes repositories to what the user can access
      const allRepos = reposResponse.repositories || []
      const activeRepos = allRepos.filter(repo => !repo.is_archived)
      const archivedRepos = allRepos.filter(repo => repo.is_archived === true)
      
      // Count total commits only from active repositories
      let totalCommits = 0
      activeRepos.forEach(repo => {
        if (Array.isArray(repo.commits)) {
          totalCommits += repo.commits.length
        }
      })
      
      // For non-admin users, keep this scoped to self for a user-focused dashboard.
      const allUsers = usersResponse.success ? (usersResponse.users || []) : []
      const totalUsers = isPrivileged ? allUsers.length : (hasUsername ? 1 : 0)

      return {
        totalUsers,
        totalRepos: activeRepos.length,
        totalCommits,
        archivedProjects: archivedRepos.length,
        recentActivity: []
      }
    } catch (error) {
      console.error('Error fetching dashboard stats:', error)
      return {
        totalUsers: 0,
        totalRepos: 0,
        totalCommits: 0,
        archivedProjects: 0,
        recentActivity: []
      }
    }
  }

  // Update repository details (G1 coordinator, tested status)
  async updateRepositoryDetails(repoId, details, actorUsername) {
    try {
      const params = new URLSearchParams()
      if (actorUsername) params.append('actor_username', actorUsername)

      const suffix = params.toString() ? `?${params.toString()}` : ''

      const response = await this.request(`/repository/${repoId}/details${suffix}`, {
        method: 'PUT',
        body: details
      })
      
      return response
    } catch (error) {
      console.error('Error updating repository details:', error)
      throw error
    }
  }

  // Upload instruction manual PDF
  async uploadInstructionManual(repoId, file, actorUsername) {
    try {
      const formData = new FormData()
      formData.append('file', file)

      const params = new URLSearchParams()
      if (actorUsername) params.append('actor_username', actorUsername)

      const suffix = params.toString() ? `?${params.toString()}` : ''
      
      const response = await fetch(`${this.baseURL}/repository/${repoId}/upload-manual${suffix}`, {
        method: 'POST',
        body: formData
      })
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }
      
      return await response.json()
    } catch (error) {
      console.error('Error uploading instruction manual:', error)
      throw error
    }
  }

  // Download instruction manual PDF
  async downloadInstructionManual(repoId) {
    try {
      const response = await fetch(`${this.baseURL}/repository/${repoId}/download-manual`)
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }
      
      // Return the blob for download
      const blob = await response.blob()
      return blob
    } catch (error) {
      console.error('Error downloading instruction manual:', error)
      throw error
    }
  }

  // --- Personal access tokens ------------------------------------------------
  // The plaintext comes back only from createAccessToken, and only once; there
  // is deliberately no endpoint that can read an existing token's value.

  async listAccessTokens({ includeRevoked = false } = {}) {
    const suffix = includeRevoked ? '?include_revoked=true' : ''
    return this.request(`/tokens${suffix}`)
  }

  async createAccessToken({ name, scopes = null, expiresInDays = null }) {
    const body = { name }
    if (scopes && scopes.length) body.scopes = scopes
    if (expiresInDays) body.expires_in_days = Number(expiresInDays)
    return this.request('/tokens', { method: 'POST', body })
  }

  async revokeAccessToken(tokenId) {
    return this.request(`/tokens/${tokenId}`, { method: 'DELETE' })
  }

  // --- Webhooks --------------------------------------------------------------
  // Omitting repositoryId addresses the server-wide hooks, which is a different
  // set from any repository's own — not "all of them".

  async listWebhooks(repositoryId = null) {
    const suffix = repositoryId ? `?repository_id=${encodeURIComponent(repositoryId)}` : ''
    return this.request(`/webhooks${suffix}`)
  }

  async createWebhook(payload) {
    return this.request('/webhooks', { method: 'POST', body: payload })
  }

  async updateWebhook(webhookId, payload) {
    return this.request(`/webhooks/${webhookId}`, { method: 'PUT', body: payload })
  }

  async deleteWebhook(webhookId) {
    return this.request(`/webhooks/${webhookId}`, { method: 'DELETE' })
  }

  async pingWebhook(webhookId) {
    // A ping waits for the receiver, so it needs more than the 10s default.
    return this.request(`/webhooks/${webhookId}/ping`, { method: 'POST', timeout: 60000 })
  }

  async listWebhookDeliveries(webhookId, limit = 30) {
    return this.request(`/webhooks/${webhookId}/deliveries?limit=${limit}`)
  }

  // --- Commit status checks --------------------------------------------------

  async reportCommitStatus(repoId, commitId, payload) {
    return this.request(`/repository/${repoId}/commits/${commitId}/statuses`, {
      method: 'POST',
      body: payload,
    })
  }

  async getCommitStatuses(repoId, commitId, { history = false } = {}) {
    const suffix = history ? '?history=true' : ''
    return this.request(`/repository/${repoId}/commits/${commitId}/statuses${suffix}`)
  }

  async getRequiredChecks(repoId) {
    return this.request(`/repository/${repoId}/required-checks`)
  }

  // --- Server-side hooks (pre-receive push policy) ---------------------------

  async listServerHooks(repositoryId = null) {
    const suffix = repositoryId ? `?repository_id=${encodeURIComponent(repositoryId)}` : ''
    return this.request(`/server-hooks${suffix}`)
  }

  async createServerHook(payload) {
    return this.request('/server-hooks', { method: 'POST', body: payload })
  }

  async updateServerHook(hookId, payload) {
    return this.request(`/server-hooks/${hookId}`, { method: 'PUT', body: payload })
  }

  async deleteServerHook(hookId) {
    return this.request(`/server-hooks/${hookId}`, { method: 'DELETE' })
  }

  async testServerHook(hookId, { message = '', paths = [] } = {}) {
    return this.request(`/server-hooks/${hookId}/test`, {
      method: 'POST',
      body: { message, paths },
    })
  }

  // --- SSH keys --------------------------------------------------------------
  // A public key is public, so it is safe to list in full. The challenge/verify
  // pair is used by the CLI, not the browser: signing needs the private key.

  async listSSHKeys() {
    return this.request('/ssh-keys')
  }

  async addSSHKey({ title, publicKey }) {
    return this.request('/ssh-keys', {
      method: 'POST',
      body: { title, public_key: publicKey },
    })
  }

  async deleteSSHKey(keyId) {
    return this.request(`/ssh-keys/${keyId}`, { method: 'DELETE' })
  }

  // --- Security operations (admin) -------------------------------------------

  async getLockouts() {
    return this.request('/admin/security/lockouts')
  }

  async unlockAccount(username) {
    return this.request('/admin/security/unlock', { method: 'POST', body: { username } })
  }

  async getSchemaHealth() {
    return this.request('/admin/schema')
  }

  async listBackups(limit = 25) {
    return this.request(`/admin/backups?limit=${limit}`)
  }

  async createBackup(label = null) {
    // Copying the blob store takes minutes on a real repository, and the caller
    // waits because a backup nobody checked the result of is not a backup.
    return this.request('/admin/backups', {
      method: 'POST',
      body: label ? { label } : {},
      timeout: 600000,
    })
  }

  async verifyBackup(backupId) {
    return this.request(`/admin/backups/${backupId}/verify`, {
      method: 'POST',
      timeout: 300000,
    })
  }

  // --- Search ----------------------------------------------------------------
  // Code search is scoped to one repository: a store-wide content scan would
  // read gigabytes per query. Repository and commit search are global.

  async searchRepositories({ q = '', owner = null, archived = null, limit = 50 } = {}) {
    const p = new URLSearchParams()
    if (q) p.append('q', q)
    if (owner) p.append('owner', owner)
    if (archived !== null && archived !== undefined) p.append('archived', String(archived))
    p.append('limit', String(limit))
    return this.request(`/search/repositories?${p}`)
  }

  async searchCommits({ q = '', repositoryId = null, author = null, limit = 50 } = {}) {
    const p = new URLSearchParams()
    if (q) p.append('q', q)
    if (repositoryId) p.append('repository_id', repositoryId)
    if (author) p.append('author', author)
    p.append('limit', String(limit))
    return this.request(`/search/commits?${p}`)
  }

  // --- Branch mutation -------------------------------------------------------
  // The four operations the server has always supported and no screen exposed.

  async renameBranch(repoId, branchName, newName) {
    return this.request(`/repository/${repoId}/branches/${encodeURIComponent(branchName)}/rename`, {
      method: 'PUT',
      body: { new_name: newName },
    })
  }

  async deleteBranch(repoId, branchName) {
    return this.request(`/repository/${repoId}/branches/${encodeURIComponent(branchName)}`, {
      method: 'DELETE',
    })
  }

  async setDefaultBranch(repoId, branchName) {
    return this.request(`/repository/${repoId}/branches/${encodeURIComponent(branchName)}/default`, {
      method: 'PUT',
    })
  }

  async moveBranchHead(repoId, branchName, commitId) {
    return this.request(`/repository/${repoId}/branches/${encodeURIComponent(branchName)}/head`, {
      method: 'PUT',
      // The server's BranchHeadUpdateRequest field is head_commit_id, not commit_id.
      body: { head_commit_id: commitId },
    })
  }

  // --- Tags and releases -----------------------------------------------------

  async listTags(repoId) {
    return this.request(`/repository/${repoId}/tags`)
  }

  async createTag(repoId, { name, commitId = null, message = null }) {
    const body = { name }
    if (commitId) body.commit_id = commitId
    if (message) body.message = message
    return this.request(`/repository/${repoId}/tags`, { method: 'POST', body })
  }

  async deleteTag(repoId, tagName) {
    return this.request(`/repository/${repoId}/tags/${encodeURIComponent(tagName)}`, {
      method: 'DELETE',
    })
  }

  async listReleases(repoId) {
    return this.request(`/repository/${repoId}/releases`)
  }

  async createRelease(repoId, { version, title = null, notes = null, tag = null }) {
    const body = { version }
    if (title) body.title = title
    if (notes) body.notes = notes
    if (tag) body.tag = tag
    return this.request(`/repository/${repoId}/releases`, { method: 'POST', body })
  }

  // --- Generated documentation ----------------------------------------------

  async getDocsStatus(repoId) {
    return this.request(`/repository/${repoId}/docs-status`)
  }

  async generateProjectDocs(repoId, payload = {}) {
    // The LLM pipeline takes minutes, not seconds.
    return this.request(`/repository/${repoId}/generate-project-docs`, {
      method: 'POST',
      body: payload,
      timeout: 300000,
    })
  }

  async searchCode({ repositoryId, q, branch = null, path = null, regex = false, caseSensitive = false, limit = 200 } = {}) {
    const p = new URLSearchParams({ repository_id: repositoryId, q })
    if (branch) p.append('branch', branch)
    if (path) p.append('path', path)
    if (regex) p.append('regex', 'true')
    if (caseSensitive) p.append('case_sensitive', 'true')
    p.append('limit', String(limit))
    // Scanning real blobs takes longer than a metadata query.
    return this.request(`/search/code?${p}`, { timeout: 60000 })
  }
}

export default new FoxNestAPI()