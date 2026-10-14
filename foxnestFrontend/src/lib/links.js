/**
 * Where things live in the web client, as URLs.
 *
 * The client used to keep its place in component state only, so nothing could be
 * linked to, bookmarked, or reached with Back. Every page that points somewhere builds
 * the address here, so a search result, a notification and an activity row all agree
 * on what "this commit" or "this file at line 42" looks like.
 *
 * Branch names contain slashes, so branch, path, line, commit and tab are query
 * parameters rather than path segments.
 */

const enc = encodeURIComponent

function query(params = {}) {
  const p = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '' && value !== false) p.set(key, String(value))
  }
  const text = p.toString()
  return text ? `?${text}` : ''
}

export const repositoryLink = (repoId, params = {}) => `/repositories/${enc(repoId)}${query(params)}`

export const fileLink = (repoId, path, { branch = null, line = null } = {}) =>
  repositoryLink(repoId, { branch, path, line })

export const commitLink = (repoId, commitId) => repositoryLink(repoId, { commit: commitId })

export const pullRequestLink = (repoId, prId) => `/repositories/${enc(repoId)}/pulls/${enc(prId)}`

export const repositoryActivityLink = (repoId) => repositoryLink(repoId, { tab: 'activity' })

/** A repository tab: branches, releases, tags, docs, ... with its own parameters. */
export const repositoryTabLink = (repoId, tab, params = {}) => repositoryLink(repoId, { tab, ...params })

export const searchLink = (params = {}) => `/search${query(params)}`

export const activityLink = (params = {}) => `/activity${query(params)}`

export const profileLink = (username) => (username ? `/profile/${enc(username)}` : '/profile')

/** Where a notification's payload.target points, or null. */
export function targetLink(target, payload = {}) {
  const t = target || {}
  const repoId = t.repository_id || payload.repository_id
  switch (t.kind) {
    case 'pull_request':
      return repoId && t.pr_id != null ? pullRequestLink(repoId, t.pr_id) : null
    case 'commit':
      return repoId && t.commit_id ? commitLink(repoId, t.commit_id) : null
    case 'repository':
      return repoId ? repositoryLink(repoId) : null
    case 'review_queue':
      return '/pending-approvals'
    case 'settings':
      return `/settings${query({ tab: t.tab })}`
    case 'issue':
      return null // issues open in a dialog on the repositories page; see Header
    default:
      break
  }
  // Notifications from before targets existed still carry ids worth following.
  if (repoId && (payload.pull_request_id ?? payload.pr_id) != null) {
    return pullRequestLink(repoId, payload.pull_request_id ?? payload.pr_id)
  }
  if (repoId && payload.commit_id) return commitLink(repoId, payload.commit_id)
  return null
}

/** Where an activity row is about, or null. */
export function activityTargetLink(item) {
  if (!item?.repository_id) return null
  const repoId = item.repository_id
  if (item.target_type === 'pull_request' && item.target_id) return pullRequestLink(repoId, item.target_id)
  // A tag or release row carries a commit too, but the row is about the tag or release.
  if (item.target_type === 'release') return repositoryTabLink(repoId, 'releases')
  if (item.target_type === 'tag') return repositoryTabLink(repoId, 'tags')
  if (item.activity_type === 'generate_project_docs' || item.activity_type === 'generate_docs') {
    return repositoryTabLink(repoId, 'docs')
  }
  if (item.commit_id && item.activity_type !== 'branch_deleted') return commitLink(repoId, item.commit_id)
  if (item.target_type === 'branch' && item.target_id && item.activity_type !== 'branch_deleted') {
    return repositoryLink(repoId, { branch: item.target_id })
  }
  return repositoryLink(repoId)
}

/** Where Open goes. An issue opens in the repositories page's issue dialog. */
export function openTarget(navigate, notification) {
  const payload = notification?.payload || {}
  const target = payload.target || {}
  if (target.kind === 'issue' || (!target.kind && payload.issue_number && payload.repository_id)) {
    navigate('/repositories')
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent('foxnest:open-issue', {
        detail: { repository_id: target.repository_id || payload.repository_id,
                  issue_number: target.issue_number ?? payload.issue_number },
      }))
    }, 50)
    return true
  }
  const link = targetLink(target, payload)
  if (!link) return false
  navigate(link)
  return true
}

export function hasTarget(notification) {
  const payload = notification?.payload || {}
  return Boolean(targetLink(payload.target, payload) || payload.target?.kind === 'issue'
    || (payload.issue_number && payload.repository_id))
}
