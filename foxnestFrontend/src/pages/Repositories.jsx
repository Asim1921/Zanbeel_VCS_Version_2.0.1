import React, { useState, useEffect, useCallback } from 'react'
import { useLocalStorage } from '../hooks/useCustomHooks'
import { FiFolder, FiGitCommit, FiUsers, FiStar, FiEye, FiGitBranch, FiClock, FiArchive, FiEdit3, FiTrash2, FiWifi, FiWifiOff, FiLoader, FiCode, FiMessageSquare, FiSearch, FiX, FiCheck, FiFileText, FiRotateCcw, FiGitPullRequest, FiHash, FiSettings, FiChevronLeft, FiChevronRight } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import Modal, { ModalOverlay } from '../components/ui/Modal'
import CodeEditor from '../components/CodeEditor'
import CommitHistoryModal from '../components/CommitHistoryModal'
import FileVersioningModal from '../components/FileVersioningModal'
import BranchActionsPanel from '../components/BranchActionsPanel'
import PullRequestsModal from '../components/PullRequestsModal'
import IssuesModal from '../components/IssuesModal'
import AutomationModal from '../components/repository/AutomationModal'
import { useRepositories, useServerHealth } from '../hooks/useApi'
import api from '../utils/api'
import { API_SERVER_URL } from '../config'
import { getSessionUser, getSessionToken } from '../utils/session'

/** Icon-only buttons are square. The size="sm" default pads for text, which made
 *  the six card actions total 308px inside a 302px card and wrap by six pixels. */
const ICON_ACTION = '!px-2'
const REPOS_PAGE_SIZE = 5

const Repositories = ({ onBrowseRepo }) => {
  const sessionUser = getSessionUser()
  const currentUsername = sessionUser.username
  const hasAuthToken = !!getSessionToken()
  const isAdmin = sessionUser.isAdmin

  const [selectedRepo, setSelectedRepo] = useState(null)
  // List is the default: it shows owner, commits, branches, size and updated-at at a
  // glance, which is what people scan for. Remembered per browser so the choice sticks.
  const [viewMode, setViewMode] = useLocalStorage('zanbeel_repo_view', 'list')
  const [currentPage, setCurrentPage] = useState(1)
  const [repositories, setRepositories] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [deleteModal, setDeleteModal] = useState({ isOpen: false, repo: null })
  const [deleting, setDeleting] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorRepo, setEditorRepo] = useState(null)
  const [commitModalRepo, setCommitModalRepo] = useState(null)
  const [hoveredRepoId, setHoveredRepoId] = useState(null)
  const [repoComments, setRepoComments] = useState({})
  const [commentModalRepo, setCommentModalRepo] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [renameModal, setRenameModal] = useState({ isOpen: false, repo: null })
  const [renamingValue, setRenamingValue] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [archiveSuccess, setArchiveSuccess] = useState(false)
  const [repoCommitHistory, setRepoCommitHistory] = useState({})
  const [commitHistoryModalRepo, setCommitHistoryModalRepo] = useState(null)
  const [docsInfo, setDocsInfo] = useState(null)
  const [docsLoading, setDocsLoading] = useState(false)
  const [docsError, setDocsError] = useState(null)
  const [branches, setBranches] = useState([])
  const [selectedBranch, setSelectedBranch] = useState(null)
  const [branchesLoading, setBranchesLoading] = useState(false)
  const [filesLoading, setFilesLoading] = useState(false)
  const [repoFiles, setRepoFiles] = useState([])
  const [versioningTarget, setVersioningTarget] = useState(null)
  const [filePreview, setFilePreview] = useState(null)
  const [filePreviewLoading, setFilePreviewLoading] = useState(false)
  const [filePreviewError, setFilePreviewError] = useState(null)
  const [pullRequestsModalRepo, setPullRequestsModalRepo] = useState(null)
  const [automationRepo, setAutomationRepo] = useState(null)
  const [issuesModalRepo, setIssuesModalRepo] = useState(null)
  const [starBusyId, setStarBusyId] = useState(null)
  const [repoContributors, setRepoContributors] = useState([])
  const [contributorsLoading, setContributorsLoading] = useState(false)
  const [contributorsError, setContributorsError] = useState(null)

  useEffect(() => {
    let cancelled = false

    const buildFallbackContributors = async (repo) => {
      const byName = new Map()
      if (repo.owner) {
        byName.set(repo.owner.toLowerCase(), {
          id: `owner-${repo.owner}`,
          username: repo.owner,
          full_name: null,
          roles: ['owner'],
        })
      }
      try {
        const commitsResponse = await api.getCommits(repo.id)
        const commits = commitsResponse?.commits || []
        for (const commit of commits) {
          const author = commit.author
          if (!author) continue
          const key = author.toLowerCase()
          const existing = byName.get(key)
          if (existing) {
            if (!existing.roles.includes('commit_author')) existing.roles.push('commit_author')
          } else {
            byName.set(key, {
              id: `author-${author}`,
              username: author,
              full_name: null,
              roles: ['commit_author'],
            })
          }
        }
      } catch {
        // Keep owner-only fallback if commits cannot be loaded.
      }
      return Array.from(byName.values())
    }

    const loadContributors = async () => {
      if (!selectedRepo?.id) {
        setRepoContributors([])
        setContributorsError(null)
        setContributorsLoading(false)
        return
      }

      try {
        setContributorsLoading(true)
        setContributorsError(null)
        const response = await api.getRepositoryContributors(selectedRepo.id)
        if (cancelled) return
        if (response?.success) {
          setRepoContributors(response.contributors || [])
        } else {
          const fallback = await buildFallbackContributors(selectedRepo)
          if (cancelled) return
          setRepoContributors(fallback)
          if (!fallback.length) {
            setContributorsError(response?.error || 'Failed to load contributors')
          }
        }
      } catch (err) {
        if (cancelled) return
        const fallback = await buildFallbackContributors(selectedRepo)
        if (cancelled) return
        setRepoContributors(fallback)
        if (!fallback.length) {
          setContributorsError(err.message || 'Failed to load contributors')
        } else {
          setContributorsError(null)
        }
      } finally {
        if (!cancelled) setContributorsLoading(false)
      }
    }

    loadContributors()
    return () => { cancelled = true }
  }, [selectedRepo?.id])

  // Branch operations change the branch list, so the panel needs a way to refresh it.
  const reloadBranches = useCallback(async () => {
    if (!selectedRepo?.id) return
    try {
      const response = await api.getBranches(selectedRepo.id)
      if (response?.success) setBranches(response.branches || [])
    } catch (err) {
      console.error('Error reloading branches:', err)
    }
  }, [selectedRepo?.id])

  useEffect(() => {
    fetchRepositories()
  }, [])

  useEffect(() => {
    if (repositories.length > 0 && Object.keys(repoComments).length === 0) {
      console.log('Re-fetching comments for repositories:', repositories.length)
      fetchAllRepoComments(repositories)
    }
  }, [repositories])

  useEffect(() => {
    const fetchDocs = async () => {
      if (!selectedRepo?.id) {
        setDocsInfo(null)
        setDocsError(null)
        return
      }

      try {
        setDocsInfo(null)
        setDocsLoading(true)
        setDocsError(null)
        const response = await api.getRepositoryDocs(selectedRepo.id, selectedBranch)
        if (response.success) {
          setDocsInfo(response)
        } else {
          setDocsInfo(null)
          setDocsError(response.error || 'No documentation generated yet.')
        }
      } catch (err) {
        setDocsInfo(null)
        setDocsError(err.message || 'No documentation generated yet.')
      } finally {
        setDocsLoading(false)
      }
    }

    fetchDocs()
  }, [selectedRepo?.id, selectedBranch])

  useEffect(() => {
    const fetchBranchesAndFiles = async () => {
      if (!selectedRepo?.id) {
        setBranches([])
        setSelectedBranch(null)
        setRepoFiles([])
        setFilesLoading(false)
        return
      }

      try {
        // Fetch branches for this repository
        setBranchesLoading(true)
        const branchesResponse = await api.getBranches(selectedRepo.id)
        if (branchesResponse.success) {
          const list = branchesResponse.branches || []
          setBranches(list)
          const defaultBranch =
            list.find(b => b.is_default) ||
            list.find(b => b.name === 'main') ||
            list[0]
          setSelectedBranch(defaultBranch?.name || null)
        }
      } catch (err) {
        console.error('Error fetching branches:', err)
        setBranches([])
        setSelectedBranch(null)
      } finally {
        setBranchesLoading(false)
      }
    }

    fetchBranchesAndFiles()
  }, [selectedRepo?.id])

  useEffect(() => {
    const fetchFilesForBranch = async () => {
      if (!selectedRepo?.id) return

      try {
        setFilesLoading(true)
        const filesResponse = await api.getRepositoryFiles(selectedRepo.id, selectedBranch)
        if (filesResponse.success) {
          // Transform files
          const filesList = []
          const filesDict = filesResponse.files || {}
          const fileEntries = Array.isArray(filesDict)
            ? filesDict.map((item, idx) => {
                if (typeof item === 'string') return [item, {}]
                const key = item?.path || item?.name || `file_${idx}`
                return [key, item || {}]
              })
            : Object.entries(filesDict)

          // Add individual files
          fileEntries.forEach(([pathName, file]) => {
            const fileName = (pathName || '').split('/').pop() || pathName || 'unknown'
            filesList.push({
              name: fileName,
              path: pathName,
              type: 'file',
              size: file?.size ? api.formatSize(file.size) : '0 B'
            })
          });

          // Add folders
          (filesResponse.folders || []).forEach(folder => {
            const fileCount = fileEntries.filter(([pathName]) => (pathName || '').startsWith(folder + '/')).length
            filesList.push({
              name: folder,
              type: 'folder',
              files: fileCount
            })
          });

          // Update selected repo with files
          setRepoFiles(filesList)
        }
      } catch (err) {
        console.error('Error fetching files:', err)
        setRepoFiles([])
      } finally {
        setFilesLoading(false)
      }
    }

    fetchFilesForBranch()
  }, [selectedRepo?.id, selectedBranch])

  const fetchAllRepoComments = async (repos) => {
    try {
      const commentsMap = {}
      
      // Create promises for all repositories to fetch commit messages in parallel
      const commentPromises = repos.map(async (repo) => {
        const localMap = {}
        try {
          // Fetch actual commits to get commit messages
          const response = await api.getCommits(repo.id, false)
          
          if (response.success && Array.isArray(response.commits)) {
            // Collect all commit messages
            const messages = []
            response.commits.forEach(commit => {
              if (commit.message && commit.message.trim()) {
                // Format timestamp like in commit history
                let date = 'Unknown date'
                const parsed = api.parseServerTimestamp(commit.timestamp)
                if (parsed) {
                  date = parsed.toLocaleString('en-US', {
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                    hour12: true
                  })
                }
                const formattedMsg = `📝 ${commit.author}: ${commit.message}\n   └─ ${date}`
                messages.push(formattedMsg)
              }
            })
            
            // Store messages if any exist
            if (messages.length > 0) {
              localMap[repo.id] = messages.join('\n\n')
            }
          }
          return localMap
        } catch (err) {
          console.error(`Error fetching commits for repo ${repo.id}:`, err)
          return localMap
        }
      })
      
      // Wait for all promises to resolve
      const results = await Promise.all(commentPromises)
      
      // Merge all results into single comments map
      const finalCommentsMap = {}
      results.forEach(result => {
        Object.assign(finalCommentsMap, result)
      })
      
      console.log('Commit messages loaded for repos:', Object.keys(finalCommentsMap))
      setRepoComments(finalCommentsMap)
    } catch (err) {
      console.error('Error fetching all commit messages:', err)
    }
  }

  const fetchCommitHistoryForRepo = async (repo) => {
    try {
      const response = await api.getCommits(repo.id, false)
      if (response.success && Array.isArray(response.commits)) {
        const historyLines = []
        response.commits.forEach(commit => {
          let date = 'Unknown'
          const parsed = api.parseServerTimestamp(commit.timestamp)
          if (parsed) {
            date = parsed.toLocaleString('en-US', {
              year: 'numeric',
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
              hour12: true
            })
          }
          const filesCount = commit.files ? commit.files.length : 0
          const formatted = `Commit: ${commit.id.slice(0, 16)}\nAuthor: ${commit.author}\nDate: ${date}\nMessage: ${commit.message}\nFiles: ${filesCount}`
          historyLines.push(formatted)
        })
        setRepoCommitHistory(prev => ({
          ...prev,
          [repo.id]: historyLines.join('\n\n' + '='.repeat(40) + '\n\n')
        }))
        setCommitHistoryModalRepo(repo)
      }
    } catch (err) {
      console.error('Error fetching commit history:', err)
    }
  }

  const handleStarToggle = async (e, repo) => {
    e.stopPropagation()
    if (!hasAuthToken) {
      alert('Sign in to star repositories.')
      return
    }
    try {
      setStarBusyId(repo.id)
      const res = repo.starredByMe
        ? await api.unstarRepository(repo.id)
        : await api.starRepository(repo.id)
      if (res.success) {
        setRepositories((prev) =>
          prev.map((r) =>
            r.id === repo.id
              ? { ...r, stars: res.star_count, starredByMe: res.starred }
              : r
          )
        )
        setSelectedRepo((sr) =>
          sr?.id === repo.id
            ? { ...sr, stars: res.star_count, starredByMe: res.starred }
            : sr
        )
      }
    } catch (err) {
      console.error('Star toggle failed:', err)
      alert(err.message || 'Could not update star')
    } finally {
      setStarBusyId(null)
    }
  }

  const fetchRepositories = async () => {
    try {
      setLoading(true)
      setError(null)
      
      const response = isAdmin
        ? await api.listAllRepositories()
        : await api.listRepositories(currentUsername)
      
      if (response.success) {
        // Filter only non-archived repositories (backend already scopes to accessible repos)
        const activeRepos = response.repositories.filter(repo => repo.is_archived !== true)
        
        // Transform server data to match our component expectations
        const transformedRepos = api.transformRepositoryData(activeRepos)
        
        setRepositories(transformedRepos)
      } else {
        setError('Failed to fetch repositories')
      }
    } catch (err) {
      setError(`Error connecting to server: ${err.message}`)
      console.error('Error fetching repositories:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleArchive = async (repoId) => {
    try {
      setDeleting(true)
      const response = await api.archiveRepository(repoId, 'Archived from web interface', currentUsername)
      
      if (response.success) {
        // Remove from local state (since it's moved to archived)
        setRepositories(repositories.filter(r => r.id !== repoId))
        
        // Close selected repo if it was the archived one
        if (selectedRepo?.id === repoId) {
          setSelectedRepo(null)
        }
        
        // Show success message
        setArchiveSuccess(true)
        setTimeout(() => setArchiveSuccess(false), 5000)
      }
    } catch (err) {
      console.error('Error archiving repository:', err)
      alert(`Failed to archive repository: ${err.message}`)
    } finally {
      setDeleting(false)
    }
  }

  const handleEditRepo = (e, repo) => {
    e.stopPropagation()
    setRenameModal({ isOpen: true, repo })
    setRenamingValue(repo.name)
  }

  const handleRenameRepository = async () => {
    if (!renamingValue.trim() || !renameModal.repo) return
    
    try {
      setRenaming(true)
      
      // Update the repository name in the state directly
      setRepositories(repositories.map(repo => 
        repo.id === renameModal.repo.id 
          ? { ...repo, name: renamingValue.trim() }
          : repo
      ))
      
      // Update selected repo if it's the one being renamed
      if (selectedRepo?.id === renameModal.repo.id) {
        setSelectedRepo(prev => ({ ...prev, name: renamingValue.trim() }))
      }
      
      // Close the modal
      setRenameModal({ isOpen: false, repo: null })
      setRenamingValue('')
    } catch (err) {
      console.error('Error renaming repository:', err)
      alert(`Failed to rename repository: ${err.message}`)
    } finally {
      setRenaming(false)
    }
  }

  const handleRenameCancel = () => {
    setRenameModal({ isOpen: false, repo: null })
    setRenamingValue('')
  }

  const handleOpenEditor = (e, repo) => {
    e.stopPropagation()
    setEditorRepo(repo)
    setEditorOpen(true)
  }

  const handleOpenFilePreview = async (file) => {
    if (!selectedRepo?.id || !file?.path) return

    try {
      setFilePreviewLoading(true)
      setFilePreviewError(null)
      setFilePreview({
        path: file.path,
        content: '',
        is_binary: false,
        size: file.size,
        hash: null,
        mime_type: null
      })

      const response = await api.getRepositoryFile(selectedRepo.id, file.path, selectedBranch)
      if (response?.success && response?.file) {
        setFilePreview(response.file)
      } else {
        setFilePreviewError('Unable to load file content.')
      }
    } catch (err) {
      setFilePreviewError(err.message || 'Unable to load file content.')
    } finally {
      setFilePreviewLoading(false)
    }
  }

  const handleDeleteClick = (e, repo) => {
    e.stopPropagation()
    setDeleteModal({ isOpen: true, repo })
  }

  const handleDeleteConfirm = async () => {
    if (!deleteModal.repo) return
    
    try {
      setDeleting(true)
      const response = await api.deleteRepository(deleteModal.repo.id, currentUsername)
      
      if (response.success) {
        // Remove from local state
        setRepositories(repositories.filter(r => r.id !== deleteModal.repo.id))
        
        // Close selected repo if it was the deleted one
        if (selectedRepo?.id === deleteModal.repo.id) {
          setSelectedRepo(null)
        }
        
        // Close modal
        setDeleteModal({ isOpen: false, repo: null })
      }
    } catch (err) {
      console.error('Error deleting repository:', err)
      alert(`Failed to delete repository: ${err.message}`)
    } finally {
      setDeleting(false)
    }
  }

  const handleDeleteCancel = () => {
    setDeleteModal({ isOpen: false, repo: null })
  }

  const handleRepoClick = (repo) => {
    setSelectedRepo(selectedRepo?.id === repo.id ? null : repo)
  }

  const handleViewCommits = (e, repo) => {
    e.stopPropagation()
    fetchCommitHistoryForRepo(repo)
  }

  const handleViewComments = (e, repo) => {
    e.stopPropagation()
    if (repoComments[repo.id]) {
      setCommentModalRepo(repo)
    }
  }

  const handleRepoMouseEnter = (repo) => {
    setHoveredRepoId(repo.id)
  }

  const handleRepoMouseLeave = () => {
    setHoveredRepoId(null)
  }

  const filteredRepos = repositories.filter(repo => {
    const isActive = repo.status === 'active'
    if (!searchQuery.trim()) return isActive
    
    const query = searchQuery.toLowerCase()
    const matchesName = repo.name && repo.name.toLowerCase().includes(query)
    const matchesDescription = repo.description && repo.description.toLowerCase().includes(query)
    const matchesOwner = repo.owner && repo.owner.toLowerCase().includes(query)
    
    return isActive && (matchesName || matchesDescription || matchesOwner)
  })

  const totalPages = Math.max(1, Math.ceil(filteredRepos.length / REPOS_PAGE_SIZE))
  const safePage = Math.min(currentPage, totalPages)
  const pageStart = (safePage - 1) * REPOS_PAGE_SIZE
  const paginatedRepos = filteredRepos.slice(pageStart, pageStart + REPOS_PAGE_SIZE)
  const rangeStart = filteredRepos.length === 0 ? 0 : pageStart + 1
  const rangeEnd = Math.min(pageStart + REPOS_PAGE_SIZE, filteredRepos.length)

  useEffect(() => {
    setCurrentPage(1)
  }, [searchQuery, viewMode])

  useEffect(() => {
    if (currentPage > totalPages) setCurrentPage(totalPages)
  }, [currentPage, totalPages])

  const goToPage = (page) => {
    const next = Math.min(Math.max(1, page), totalPages)
    setCurrentPage(next)
  }

  const canManageRepo = (repo) => {
    if (isAdmin) return true
    if (repo.current_user_can_manage) return true
    return !!currentUsername && repo.owner === currentUsername
  }

  const canWriteRepo = (repo) => {
    if (isAdmin) return true
    if (repo.current_user_can_write) return true
    return !!currentUsername && repo.owner === currentUsername
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <FiLoader className="w-8 h-8 text-ink animate-spin" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center py-12">
        <p className="text-danger-fg mb-4">{error}</p>
        <Button onClick={fetchRepositories}>Retry</Button>
      </div>
    )
  }

  return (
    <div className="space-y-6 min-w-0 w-full overflow-x-hidden">
      {/* Archive Success Notification */}
      {archiveSuccess && (
        <div className="bg-success-bg border border-success-fg/60 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <FiCheck className="w-5 h-5 text-success-fg" />
            <p className="text-success-fg">Repository archived successfully! View it in the <span className="font-semibold">Archive</span> section.</p>
          </div>
          <button 
            onClick={() => setArchiveSuccess(false)}
            className="text-success-fg hover:text-success-fg"
          >
            <FiX className="w-5 h-5" />
          </button>
        </div>
      )}

      {!currentUsername && (
        <div className="bg-warning-bg border border-warning-fg/30 rounded-lg p-4">
          <p className="text-warning-fg text-sm">
            Session username is not set. Set localStorage key <span className="font-semibold">foxnest_username</span> to enable owner actions.
          </p>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-ink mb-2 md:text-4xl">Repositories</h1>
          <p className="text-muted">Browse and manage all your repositories</p>
        </div>
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1 bg-cream-mid rounded-lg p-1">
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 rounded text-sm transition-colors ${
                viewMode === 'grid' ? 'bg-cream-deep text-ink' : 'text-ink-soft hover:text-ink'
              }`}
            >
              Grid
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`p-2 rounded text-sm transition-colors ${
                viewMode === 'list' ? 'bg-cream-deep text-ink' : 'text-ink-soft hover:text-ink'
              }`}
            >
              List
            </button>
          </div>
        </div>
      </div>

      {/* Filter, placed under the heading it belongs to. It used to render
          above the page title, so it read as a detached global search bar
          rather than a filter for the list below it. */}
      <div className="relative">
        <FiSearch className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
        <input
          type="text"
          placeholder="Filter these repositories by name, description or owner…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full rounded-lg border border-border bg-cream-mid py-2 pl-9 pr-9 text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
        />
        {searchQuery && (
          <button
            onClick={() => setSearchQuery('')}
            aria-label="Clear filter"
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-ink-soft transition-colors hover:bg-white/[0.07] hover:text-ink"
          >
            <FiX className="h-4 w-4" />
          </button>
        )}
      </div>

      {/* Repository Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiFolder className="w-8 h-8 text-info-fg mr-3" />
            <div>
              <p className="text-2xl font-semibold text-ink">{filteredRepos.length}</p>
              <p className="text-sm text-ink-soft">Active Repos</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiGitCommit className="w-8 h-8 text-success-fg mr-3" />
            <div>
              <p className="text-2xl font-semibold text-ink">
                {repositories.reduce((sum, repo) => sum + repo.commits, 0)}
              </p>
              <p className="text-sm text-ink-soft">Total Commits</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiUsers className="w-8 h-8 text-ink mr-3" />
            <div>
              <p className="text-2xl font-semibold text-ink">
                {repositories.length
                  ? Math.max(...repositories.map((repo) => repo.contributors || 0))
                  : 0}
              </p>
              <p className="text-sm text-ink-soft">Max Contributors</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiStar className="w-8 h-8 text-warning-fg mr-3" />
            <div>
              <p className="text-2xl font-semibold text-ink">
                {repositories.reduce((sum, repo) => sum + repo.stars, 0)}
              </p>
              <p className="text-sm text-ink-soft">Total Stars</p>
            </div>
          </div>
        </GlassCard>
      </div>

      {/* Repositories Grid/List */}
      {viewMode === 'grid' ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
          {paginatedRepos.map((repo) => (
            <div key={repo.id} className="relative">
              <GlassCard
                className={`p-6 cursor-pointer transition-all duration-300 ${
                  selectedRepo?.id === repo.id ? 'ring-2 ring-ink bg-cream-mid' : ''
                }`}
                onClick={() => handleRepoClick(repo)}
                onMouseEnter={() => handleRepoMouseEnter(repo)}
                onMouseLeave={handleRepoMouseLeave}
              >
                {/* Repository Header */}
                <div className="mb-4">
                  <div className="flex flex-col gap-2 mb-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 min-w-0">
                        <h3 className="font-semibold text-ink text-base leading-tight mb-1" title={repo.name}>{repo.name}</h3>
                        <Badge variant={repo.visibility === 'public' ? 'success' : 'warning'} className="w-fit">
                          {repo.visibility}
                        </Badge>
                      </div>
                      <button
                        type="button"
                        onClick={(e) => handleStarToggle(e, repo)}
                        disabled={starBusyId === repo.id || !hasAuthToken}
                        className={`shrink-0 rounded-lg p-2 border transition-colors disabled:opacity-40 ${
                          repo.starredByMe
                            ? 'border-warning-fg/50 bg-warning-fg/20 text-warning-fg'
                            : 'border-border bg-cream-mid/70 text-ink-soft hover:bg-cream-mid hover:border-warning-fg/30'
                        }`}
                        title={
                          !hasAuthToken
                            ? 'Sign in to star'
                            : repo.starredByMe
                              ? 'Remove your star'
                              : 'Star this repository'
                        }
                        aria-label={repo.starredByMe ? 'Unstar' : 'Star'}
                      >
                        <FiStar className={`w-5 h-5 ${repo.starredByMe ? 'fill-current' : ''}`} />
                      </button>
                    </div>
                    {/* gap, not space-x: these wrap, and space-x leaves wrapped
                        rows touching. ICON_ACTION keeps the buttons square so all
                        six fit on one line inside a card. */}
                    <div className="flex flex-wrap items-center justify-start gap-1.5">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={(e) => { e.stopPropagation(); if (repoComments[repo.id]) setCommentModalRepo(repo) }}
                        title={repoComments[repo.id] ? "View commit messages" : "No commit messages"}
                        disabled={!repoComments[repo.id]}
                        className={`${ICON_ACTION} ${repoComments[repo.id] ? 'bg-success-bg hover:bg-success-bg border border-success-fg/25 hover:border-success-fg/40' : 'bg-cream-mid border border-border cursor-not-allowed opacity-50'}`}
                      >
                        <FiMessageSquare className={`w-4 h-4 ${repoComments[repo.id] ? 'text-success-fg' : 'text-muted'}`} />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleViewCommits(e, repo) }} title="View commit history" className={`${ICON_ACTION} bg-cream-deep hover:bg-cream-mid border border-border-strong hover:border-ink`}>
                        <FiGitCommit className="w-4 h-4 text-ink" />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={(e) => handleOpenEditor(e, repo)} title="Open in Editor" className={ICON_ACTION}>
                        <FiCode className="w-4 h-4 text-info-fg" />
                      </Button>
                      {(canWriteRepo(repo) || canManageRepo(repo)) ? (
                        <>
                          {canWriteRepo(repo) && (
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleEditRepo(e, repo) }} title="Edit Repository" className={ICON_ACTION}>
                              <FiEdit3 className="w-4 h-4" />
                            </Button>
                          )}
                          {canManageRepo(repo) && (
                            <>
                              <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleArchive(repo.id) }} title="Archive Repository" className={ICON_ACTION}>
                                <FiArchive className="w-4 h-4" />
                              </Button>
                              <Button variant="ghost" size="sm" onClick={(e) => handleDeleteClick(e, repo)} title="Delete Repository" className={ICON_ACTION}>
                                <FiTrash2 className="w-4 h-4 text-danger-fg" />
                              </Button>
                            </>
                          )}
                        </>
                      ) : (
                        <div className="text-xs text-muted px-2">Read-only</div>
                      )}
                    </div>
                  </div>
                </div>
                <p className="text-sm text-ink-soft break-words mb-4">{repo.description}</p>
                <div className="flex items-center space-x-2 mb-4">
                  {repo.language && repo.language !== 'Unknown' && (
                    <div className="flex items-center space-x-1">
                      <div className="w-3 h-3 rounded-full" style={{ backgroundColor: repo.languageColor }}></div>
                      <span className="text-sm text-ink-soft">{repo.language}</span>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1">
                    {repo.tags.slice(0, 2).map((tag, index) => (
                      <Badge key={index} variant="default" className="text-xs">{tag}</Badge>
                    ))}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <div className="flex items-center space-x-2 text-sm text-ink-soft">
                    <FiGitCommit className="w-4 h-4" />
                    <span>{repo.commits} {repo.commits === 1 ? 'Commit' : 'Commits'}</span>
                  </div>
                  <div className="flex items-center space-x-2 text-sm text-ink-soft">
                    <FiUsers className="w-4 h-4" />
                    <span className="underline-offset-2 hover:underline" title="Click card to view contributor names">
                      {repo.contributors} {repo.contributors === 1 ? 'Contributor' : 'Contributors'}
                    </span>
                  </div>
                  <div className="flex items-center space-x-2 text-sm text-ink-soft">
                    <button
                      type="button"
                      onClick={(e) => handleStarToggle(e, repo)}
                      disabled={starBusyId === repo.id || !hasAuthToken}
                      className={`flex items-center space-x-1 rounded-md px-1.5 py-0.5 -ml-1 transition-colors disabled:opacity-50 ${
                        repo.starredByMe ? 'text-warning-fg bg-warning-fg/15' : 'hover:bg-cream-mid text-ink-soft'
                      }`}
                      title={
                        !hasAuthToken
                          ? 'Sign in to star'
                          : repo.starredByMe
                            ? 'Remove your star'
                            : 'Star this repository'
                      }
                    >
                      <FiStar className={`w-4 h-4 shrink-0 ${repo.starredByMe ? 'fill-current' : ''}`} />
                      <span>
                        {repo.stars} {repo.stars === 1 ? 'Star' : 'Stars'}
                      </span>
                    </button>
                  </div>
                  <div className="flex items-center space-x-2 text-sm text-ink-soft">
                    <FiGitBranch className="w-4 h-4" />
                    <span>{repo.branches} {repo.branches === 1 ? 'Branch' : 'Branches'}</span>
                  </div>
                </div>
                <div className="flex items-center justify-between text-xs text-muted">
                  <div className="flex items-center space-x-1">
                    <FiClock className="w-3 h-3" />
                    <span>Updated {repo.lastUpdate}</span>
                  </div>
                  <span>{repo.size}</span>
                </div>
              </GlassCard>
            </div>
          ))}
        </div>
      ) : (
        /* ── Table / List View ── */
        <>
          {/* Compact stacked cards below lg */}
          <div className="space-y-3 lg:hidden">
            {paginatedRepos.map((repo) => (
              <GlassCard
                key={repo.id}
                className={`p-4 cursor-pointer transition-colors ${
                  selectedRepo?.id === repo.id ? 'ring-2 ring-ink bg-cream-mid' : ''
                }`}
                onClick={() => handleRepoClick(repo)}
              >
                <div className="flex items-start justify-between gap-3 mb-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <FiFolder className="w-4 h-4 text-ink shrink-0" />
                      <h3 className="font-medium text-ink truncate" title={repo.name}>{repo.name}</h3>
                      <Badge variant={repo.visibility === 'public' ? 'success' : 'warning'} className="text-xs shrink-0">
                        {repo.visibility}
                      </Badge>
                    </div>
                    <p className="mt-1 text-xs text-muted truncate">
                      {repo.owner || '—'} · Updated {repo.lastUpdate}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => handleStarToggle(e, repo)}
                    disabled={starBusyId === repo.id || !hasAuthToken}
                    className={`shrink-0 inline-flex items-center gap-1 rounded-md px-2 py-1 text-sm disabled:opacity-50 ${
                      repo.starredByMe ? 'text-warning-fg bg-warning-fg/15' : 'text-ink-soft hover:bg-cream-mid'
                    }`}
                    title={!hasAuthToken ? 'Sign in to star' : repo.starredByMe ? 'Unstar' : 'Star'}
                  >
                    <FiStar className={`w-3.5 h-3.5 ${repo.starredByMe ? 'fill-current' : ''}`} />
                    {repo.stars}
                  </button>
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-ink-soft mb-3">
                  <div className="flex items-center gap-1.5">
                    <FiGitCommit className="w-3.5 h-3.5 shrink-0" />
                    <span>{repo.commits} commits</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <FiGitBranch className="w-3.5 h-3.5 shrink-0" />
                    <span>{repo.branches} branches</span>
                  </div>
                  <div className="flex items-center gap-1.5" title="Click card to view contributor names">
                    <FiUsers className="w-3.5 h-3.5 shrink-0" />
                    <span>{repo.contributors} contributors</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <FiClock className="w-3.5 h-3.5 shrink-0" />
                    <span>{repo.size}</span>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={(e) => { e.stopPropagation(); if (repoComments[repo.id]) setCommentModalRepo(repo) }}
                    title={repoComments[repo.id] ? 'View commit messages' : 'No commit messages'}
                    disabled={!repoComments[repo.id]}
                    className={`!rounded-lg !px-2 !py-1.5 ${repoComments[repo.id] ? 'bg-success-bg border border-success-fg/25' : 'opacity-40 cursor-not-allowed'}`}
                  >
                    <FiMessageSquare className={`w-3.5 h-3.5 ${repoComments[repo.id] ? 'text-success-fg' : 'text-muted'}`} />
                  </Button>
                  <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleViewCommits(e, repo) }} title="Commit history" className="!rounded-lg !px-2 !py-1.5 bg-cream-deep border border-border-strong">
                    <FiGitCommit className="w-3.5 h-3.5 text-ink" />
                  </Button>
                  <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleOpenEditor(e, repo) }} title="Open in Editor" className="!rounded-lg !px-2 !py-1.5">
                    <FiCode className="w-3.5 h-3.5 text-info-fg" />
                  </Button>
                  {canWriteRepo(repo) && (
                    <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleEditRepo(e, repo) }} title="Edit" className="!rounded-lg !px-2 !py-1.5">
                      <FiEdit3 className="w-3.5 h-3.5" />
                    </Button>
                  )}
                  {canManageRepo(repo) && (
                    <>
                      <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleArchive(repo.id) }} title="Archive" className="!rounded-lg !px-2 !py-1.5">
                        <FiArchive className="w-3.5 h-3.5" />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleDeleteClick(e, repo) }} title="Delete" className="!rounded-lg !px-2 !py-1.5">
                        <FiTrash2 className="w-3.5 h-3.5 text-danger-fg" />
                      </Button>
                    </>
                  )}
                </div>
              </GlassCard>
            ))}
          </div>

          {/* Desktop table: scroll + sticky name/actions */}
          <GlassCard className="hidden lg:block overflow-hidden">
            <div className="overflow-x-auto overscroll-x-contain">
              <table className="w-full min-w-[880px] text-sm">
                <thead>
                  <tr className="border-b border-border text-left bg-surface">
                    <th className="sticky left-0 z-20 bg-surface px-3 py-3 text-muted font-medium min-w-[200px]">Repository</th>
                    <th className="px-3 py-3 text-muted font-medium whitespace-nowrap">Owner</th>
                    <th className="hidden xl:table-cell px-3 py-3 text-muted font-medium whitespace-nowrap">Language</th>
                    <th className="px-2 py-3 text-muted font-medium text-center whitespace-nowrap" title="Commits">
                      <FiGitCommit className="inline w-4 h-4" />
                      <span className="hidden 2xl:inline ml-1">Commits</span>
                    </th>
                    <th className="px-2 py-3 text-muted font-medium text-center whitespace-nowrap" title="Branches">
                      <FiGitBranch className="inline w-4 h-4" />
                      <span className="hidden 2xl:inline ml-1">Branches</span>
                    </th>
                    <th className="px-2 py-3 text-muted font-medium text-center whitespace-nowrap" title="Stars">
                      <FiStar className="inline w-4 h-4" />
                      <span className="hidden 2xl:inline ml-1">Stars</span>
                    </th>
                    <th className="px-2 py-3 text-muted font-medium text-center whitespace-nowrap" title="Contributors">
                      <FiUsers className="inline w-4 h-4" />
                      <span className="hidden 2xl:inline ml-1">Contributors</span>
                    </th>
                    <th className="hidden xl:table-cell px-3 py-3 text-muted font-medium whitespace-nowrap">Size</th>
                    <th className="hidden xl:table-cell px-3 py-3 text-muted font-medium whitespace-nowrap">Updated</th>
                    <th className="sticky right-0 z-20 bg-surface px-2 py-3 text-muted font-medium text-center min-w-[168px]">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {paginatedRepos.map((repo, idx) => {
                    const rowBg = selectedRepo?.id === repo.id
                      ? 'bg-cream-mid'
                      : idx % 2 === 0
                        ? 'bg-cream-mid/40'
                        : 'bg-surface'
                    return (
                      <tr
                        key={repo.id}
                        className={`border-b border-border/60 transition-colors cursor-pointer hover:bg-cream-mid group ${
                          selectedRepo?.id === repo.id ? 'bg-cream-mid' : idx % 2 === 0 ? 'bg-cream-mid/40' : ''
                        }`}
                        onClick={() => handleRepoClick(repo)}
                      >
                        <td className={`sticky left-0 z-10 px-3 py-3 ${rowBg} group-hover:bg-cream-mid`}>
                          <div className="flex items-center gap-2 min-w-0 max-w-[260px]">
                            <FiFolder className="w-4 h-4 text-ink shrink-0" />
                            <span className="font-medium text-ink truncate" title={repo.name}>{repo.name}</span>
                            <Badge variant={repo.visibility === 'public' ? 'success' : 'warning'} className="text-[10px] shrink-0">
                              {repo.visibility}
                            </Badge>
                          </div>
                        </td>
                        <td className="px-3 py-3 text-ink-soft whitespace-nowrap">{repo.owner || '—'}</td>
                        <td className="hidden xl:table-cell px-3 py-3">
                          {repo.language && repo.language !== 'Unknown' ? (
                            <div className="flex items-center gap-1 min-w-0">
                              <div className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: repo.languageColor }}></div>
                              <span className="text-ink-soft truncate max-w-[100px]">{repo.language}</span>
                            </div>
                          ) : <span className="text-muted">—</span>}
                        </td>
                        <td className="px-2 py-3 text-center text-ink-soft">{repo.commits}</td>
                        <td className="px-2 py-3 text-center text-ink-soft">{repo.branches}</td>
                        <td className="px-2 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            onClick={(e) => handleStarToggle(e, repo)}
                            disabled={starBusyId === repo.id || !hasAuthToken}
                            className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-sm disabled:opacity-50 ${
                              repo.starredByMe ? 'text-warning-fg bg-warning-fg/15' : 'text-ink-soft hover:bg-cream-mid'
                            }`}
                            title={!hasAuthToken ? 'Sign in to star' : repo.starredByMe ? 'Remove your star' : 'Star'}
                          >
                            <FiStar className={`w-3.5 h-3.5 ${repo.starredByMe ? 'fill-current' : ''}`} />
                            {repo.stars}
                          </button>
                        </td>
                        <td className="px-2 py-3 text-center text-ink-soft" title="Click row to view contributor names">
                          {repo.contributors}
                        </td>
                        <td className="hidden xl:table-cell px-3 py-3 text-muted whitespace-nowrap">{repo.size}</td>
                        <td className="hidden xl:table-cell px-3 py-3 text-muted text-xs whitespace-nowrap">{repo.lastUpdate}</td>
                        <td className={`sticky right-0 z-10 px-2 py-3 ${rowBg} group-hover:bg-cream-mid`}>
                          <div className="flex items-center justify-end gap-0.5 flex-nowrap" onClick={(e) => e.stopPropagation()}>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={(e) => { e.stopPropagation(); if (repoComments[repo.id]) setCommentModalRepo(repo) }}
                              title={repoComments[repo.id] ? 'View commit messages' : 'No commit messages'}
                              disabled={!repoComments[repo.id]}
                              className={`!rounded-lg !px-1.5 !py-1.5 ${repoComments[repo.id] ? 'bg-success-bg border border-success-fg/25' : 'opacity-40 cursor-not-allowed'}`}
                            >
                              <FiMessageSquare className={`w-3.5 h-3.5 ${repoComments[repo.id] ? 'text-success-fg' : 'text-muted'}`} />
                            </Button>
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleViewCommits(e, repo) }} title="Commit history" className="!rounded-lg !px-1.5 !py-1.5 bg-cream-deep border border-border-strong">
                              <FiGitCommit className="w-3.5 h-3.5 text-ink" />
                            </Button>
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleOpenEditor(e, repo) }} title="Open in Editor" className="!rounded-lg !px-1.5 !py-1.5">
                              <FiCode className="w-3.5 h-3.5 text-info-fg" />
                            </Button>
                            {canWriteRepo(repo) && (
                              <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleEditRepo(e, repo) }} title="Edit" className="!rounded-lg !px-1.5 !py-1.5">
                                <FiEdit3 className="w-3.5 h-3.5" />
                              </Button>
                            )}
                            {canManageRepo(repo) && (
                              <>
                                <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleArchive(repo.id) }} title="Archive" className="!rounded-lg !px-1.5 !py-1.5">
                                  <FiArchive className="w-3.5 h-3.5" />
                                </Button>
                                <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleDeleteClick(e, repo) }} title="Delete" className="!rounded-lg !px-1.5 !py-1.5">
                                  <FiTrash2 className="w-3.5 h-3.5 text-danger-fg" />
                                </Button>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </GlassCard>
        </>
      )}

      {filteredRepos.length > 0 && (
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-lg border border-border bg-surface px-4 py-3">
          <p className="text-sm text-ink-soft">
            Showing <span className="font-medium text-ink">{rangeStart}–{rangeEnd}</span> of{' '}
            <span className="font-medium text-ink">{filteredRepos.length}</span> repositories
          </p>
          <div className="flex items-center gap-2 flex-wrap">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => goToPage(safePage - 1)}
              disabled={safePage <= 1}
              className="!rounded-lg border border-border"
              aria-label="Previous page"
            >
              <FiChevronLeft className="w-4 h-4" />
              <span className="hidden sm:inline">Previous</span>
            </Button>
            <div className="flex items-center gap-1">
              {Array.from({ length: totalPages }, (_, i) => i + 1).map((page) => {
                const showPage =
                  totalPages <= 7 ||
                  page === 1 ||
                  page === totalPages ||
                  Math.abs(page - safePage) <= 1
                const showEllipsisBefore = page === safePage - 2 && safePage > 3 && totalPages > 7
                const showEllipsisAfter = page === safePage + 2 && safePage < totalPages - 2 && totalPages > 7

                if (showEllipsisBefore || showEllipsisAfter) {
                  return (
                    <span key={`ellipsis-${page}`} className="px-1 text-muted text-sm">
                      …
                    </span>
                  )
                }
                if (!showPage) return null

                return (
                  <button
                    key={page}
                    type="button"
                    onClick={() => goToPage(page)}
                    aria-label={`Page ${page}`}
                    aria-current={page === safePage ? 'page' : undefined}
                    className={`min-w-8 h-8 rounded-lg text-sm font-medium transition-colors ${
                      page === safePage
                        ? 'bg-ink text-cream'
                        : 'text-ink-soft hover:bg-cream-mid border border-transparent hover:border-border'
                    }`}
                  >
                    {page}
                  </button>
                )
              })}
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => goToPage(safePage + 1)}
              disabled={safePage >= totalPages}
              className="!rounded-lg border border-border"
              aria-label="Next page"
            >
              <span className="hidden sm:inline">Next</span>
              <FiChevronRight className="w-4 h-4" />
            </Button>
          </div>
        </div>
      )}

      {/* Repository Details Modal */}
      <Modal
        open={!!selectedRepo}
        onClose={() => setSelectedRepo(null)}
        title={selectedRepo?.name || 'Repository'}
        subtitle={
          selectedRepo
            ? `${selectedRepo.owner || 'Unknown owner'} · ${selectedRepo.visibility} · Updated ${selectedRepo.lastUpdate}`
            : undefined
        }
        panelClassName="max-w-5xl"
      >
        {selectedRepo && (
          <>
          <div className="mb-5 flex flex-wrap gap-2">
            {/* The primary action: the dialog summarises, the page is where you read code. */}
            {typeof onBrowseRepo === 'function' && (
              <button
                type="button"
                onClick={() => onBrowseRepo(selectedRepo)}
                className="inline-flex items-center gap-2 rounded-full border border-accent/50 bg-accent/15 px-3.5 py-1.5 text-[13px] text-ink transition-colors hover:border-accent"
              >
                <FiCode className="h-3.5 w-3.5" />
                Browse code
              </button>
            )}
            <button
              type="button"
              onClick={(e) => handleStarToggle(e, selectedRepo)}
              disabled={starBusyId === selectedRepo.id || !hasAuthToken}
              title={
                !hasAuthToken
                  ? 'Sign in to star'
                  : selectedRepo.starredByMe
                    ? 'Remove your star'
                    : 'Star repository'
              }
              className={`inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors disabled:opacity-40 ${
                selectedRepo.starredByMe
                  ? 'border-warning-fg/40 bg-warning-fg/10 text-warning-fg'
                  : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink'
              }`}
            >
              <FiStar className={`h-3.5 w-3.5 ${selectedRepo.starredByMe ? 'fill-current' : ''}`} />
              {selectedRepo.stars ?? 0} {selectedRepo.stars === 1 ? 'Star' : 'Stars'}
            </button>
            <button
              type="button"
              onClick={() => setPullRequestsModalRepo(selectedRepo)}
              className="inline-flex items-center gap-2 rounded-full border border-border bg-white/[0.02] px-3.5 py-1.5 text-[13px] text-ink-soft transition-colors hover:border-border-strong hover:text-ink"
            >
              <FiGitPullRequest className="h-3.5 w-3.5" />
              Pull Requests
            </button>
            <button
              type="button"
              onClick={() => setIssuesModalRepo(selectedRepo)}
              className="inline-flex items-center gap-2 rounded-full border border-border bg-white/[0.02] px-3.5 py-1.5 text-[13px] text-ink-soft transition-colors hover:border-border-strong hover:text-ink"
            >
              <FiHash className="h-3.5 w-3.5" />
              Issues
            </button>
            <button
              type="button"
              onClick={() => setAutomationRepo(selectedRepo)}
              title="Branches, releases, docs, webhooks, checks and push rules"
              className="inline-flex items-center gap-2 rounded-full border border-border bg-white/[0.02] px-3.5 py-1.5 text-[13px] text-ink-soft transition-colors hover:border-border-strong hover:text-ink"
            >
              <FiSettings className="h-3.5 w-3.5" />
              Manage
            </button>
          </div>

          <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-2">
            {/* Repository Info */}
            <div className="rounded-xl border border-border bg-white/[0.02] p-4">
              <h3 className="mb-3 font-display text-sm font-medium tracking-tight text-ink">Repository information</h3>
              <div className="space-y-2.5 text-sm">
                {selectedRepo.language && (
                  <div className="flex justify-between">
                    <span className="text-ink-soft">Language:</span>
                    <div className="flex items-center space-x-1">
                      <div 
                        className="w-3 h-3 rounded-full" 
                        style={{ backgroundColor: selectedRepo.languageColor }}
                      ></div>
                      <span className="text-ink">{selectedRepo.language}</span>
                    </div>
                  </div>
                )}
                <div className="flex justify-between">
                  <span className="text-ink-soft">Size:</span>
                  <span className="text-ink">{selectedRepo.size}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-ink-soft">Visibility:</span>
                  <Badge variant={selectedRepo.visibility === 'public' ? 'success' : 'warning'}>
                    {selectedRepo.visibility}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-ink-soft">Last Update:</span>
                  <span className="text-ink">{selectedRepo.lastUpdate}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-ink-soft">Owner:</span>
                  <span className="text-ink">{selectedRepo.owner || '—'}</span>
                </div>
              </div>

              <div className="mt-5 border-t border-border pt-4">
                <h3 className="mb-3 flex items-center font-display text-sm font-medium tracking-tight text-ink">
                  <FiUsers className="w-4 h-4 mr-2" />
                  Contributors
                  {!contributorsLoading && (
                    <span className="ml-2 text-sm font-normal text-muted">
                      ({repoContributors.length})
                    </span>
                  )}
                </h3>
                {contributorsLoading ? (
                  <div className="flex items-center gap-2 text-sm text-ink-soft">
                    <FiLoader className="w-4 h-4 animate-spin" />
                    Loading contributors…
                  </div>
                ) : contributorsError ? (
                  <p className="text-sm text-danger-fg">{contributorsError}</p>
                ) : repoContributors.length === 0 ? (
                  <p className="text-sm text-muted">No contributors found for this repository.</p>
                ) : (
                  <ul className="space-y-2">
                    {repoContributors.map((person) => {
                      const displayName = person.full_name?.trim() || person.username
                      const roleLabel = (person.roles || [])
                        .map((role) => ({
                          owner: 'Owner',
                          commit_author: 'Author',
                          collaborator: 'Collaborator',
                          issue_participant: 'Issues',
                        }[role] || role))
                        .join(' · ')
                      return (
                        <li
                          key={person.id || person.username}
                          className="flex items-start justify-between gap-3 rounded-lg border border-border bg-white/[0.03] px-3 py-2"
                        >
                          <div className="min-w-0">
                            <p className="font-medium text-ink truncate">{displayName}</p>
                            {person.full_name?.trim() && person.username !== displayName && (
                              <p className="text-xs text-muted truncate">@{person.username}</p>
                            )}
                          </div>
                          {roleLabel && (
                            <span className="shrink-0 text-xs text-ink-soft text-right">{roleLabel}</span>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            </div>

            {/* Files Structure */}
            <div className="rounded-xl border border-border bg-white/[0.02] p-4">
              <div className="mb-4 flex items-center justify-between">
                <h3 className="font-display text-sm font-medium tracking-tight text-ink">Files & folders</h3>
                {branchesLoading && (
                  <FiLoader className="w-4 h-4 text-muted animate-spin" />
                )}
              </div>

              {/* Branch Selector */}
              {branches.length > 0 && (
                <div className="mb-4 rounded-lg border border-border bg-white/[0.03] p-3">
                  <label className="block text-sm text-ink-soft mb-2">
                    <FiGitBranch className="w-4 h-4 mr-2 inline" />
                    Branch
                  </label>
                  <select 
                    value={selectedBranch || ''} 
                    onChange={(e) => setSelectedBranch(e.target.value)}
                    className="w-full rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-ink transition-colors focus:border-accent focus:outline-none"
                    disabled={filesLoading}
                  >
                    {branches.map((branch, index) => (
                      <option
                        key={index}
                        value={branch.name || branch}
                        className="bg-surface text-ink"
                      >
                        {branch.name || branch}
                        {branch.is_default ? ' (default)' : ''}
                      </option>
                    ))}
                  </select>
                  {selectedBranch && selectedBranch.startsWith('rollback_') && (
                    <p className="text-xs text-warning-fg mt-2 bg-warning-bg p-2 rounded border border-warning-fg/20">
                      📌 Rollback branch: This shows the state at a specific commit
                    </p>
                  )}

                  <BranchActionsPanel
                    repo={selectedRepo}
                    branches={branches}
                    selectedBranch={selectedBranch}
                    selectedFilePath={null}
                    canManage={isAdmin}
                    onBranchesChanged={reloadBranches}
                    onOpenPullRequests={() => setPullRequestsModalRepo(selectedRepo)}
                  />
                </div>
              )}

              {filesLoading && (
                <div className="flex items-center justify-center py-8 text-muted">
                  <FiLoader className="w-4 h-4 animate-spin mr-2" />
                  Loading files...
                </div>
              )}

              {!filesLoading && (
                <div className="space-y-2 max-h-64 overflow-y-auto">
                  {repoFiles && repoFiles.length > 0 ? (
                    repoFiles.map((file, index) => (
                      <div key={index} className="flex items-center justify-between rounded-lg border border-transparent bg-white/[0.03] p-2 transition-colors hover:border-border hover:bg-white/[0.05]">
                        <div className="flex items-center space-x-2">
                          {file.type === 'folder' ? (
                            <FiFolder className="w-4 h-4 text-info-fg" />
                          ) : (
                            <div className="w-4 h-4 bg-cream-deep rounded"></div>
                          )}
                          {file.type === 'file' && file.path ? (
                            <button
                              type="button"
                              onClick={() => handleOpenFilePreview(file)}
                              className="text-ink text-sm hover:text-info-fg underline-offset-2 hover:underline text-left"
                              title={`Open ${file.path}`}
                            >
                              {file.name}
                            </button>
                          ) : (
                            <span className="text-ink text-sm">{file.name}</span>
                          )}
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="text-muted text-xs">
                            {file.type === 'folder' ? `${file.files} files` : file.size}
                          </span>
                          {file.type === 'file' && file.path && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-info-fg hover:text-ink"
                              onClick={() => setVersioningTarget({ repo: selectedRepo, path: file.path })}
                              title="View file history, diff and rollback"
                            >
                              <FiRotateCcw className="w-3.5 h-3.5" />
                            </Button>
                          )}
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="text-muted text-sm">No files available</p>
                  )}
                </div>
              )}
            </div>

            {/* Documentation */}
            <div className="rounded-xl border border-border bg-white/[0.02] p-4 lg:col-span-2">
              <h3 className="mb-3 font-display text-sm font-medium tracking-tight text-ink">Documentation</h3>
              {docsLoading ? (
                <div className="flex items-center space-x-2 text-ink-soft">
                  <FiLoader className="w-4 h-4 animate-spin" />
                  <span className="text-sm">Checking docs...</span>
                </div>
              ) : docsInfo?.success ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between text-sm text-ink-soft">
                    <span>Files:</span>
                    <span className="text-ink">{docsInfo.files?.length || 0}</span>
                  </div>
                  {docsInfo.metadata?.generated_at && (
                    <div className="flex items-center justify-between text-sm text-ink-soft">
                      <span>Generated:</span>
                      <span className="text-ink">{new Date(docsInfo.metadata.generated_at).toLocaleString()}</span>
                    </div>
                  )}
                  <p className="text-xs text-muted">
                    &quot;Open overview&quot; opens the main index only. Use the file list or ZIP to get all {docsInfo.files?.length || 0} files.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="bg-info-fg/20 hover:bg-info-fg/40 border border-info-fg/25 hover:border-info-fg"
                      onClick={() => window.open(`${API_SERVER_URL}${docsInfo.index_url}`, '_blank', 'noopener,noreferrer')}
                    >
                      <FiFileText className="w-4 h-4 mr-2" />
                      Open overview (index)
                    </Button>
                    <a
                      href={`${API_SERVER_URL}${docsInfo.archive_url || api.getRepositoryDocsArchivePath(selectedRepo.id)}`}
                      download
                      className="inline-flex items-center justify-center rounded-lg text-sm px-3 py-1.5 bg-success-fg/20 hover:bg-success-fg/35 border border-success-fg/50 text-ink transition-colors"
                    >
                      <FiFileText className="w-4 h-4 mr-2" />
                      Download all ({docsInfo.files?.length || 0} files, ZIP)
                    </a>
                  </div>
                  {Array.isArray(docsInfo.files) && docsInfo.files.length > 0 && (
                    <div className="max-h-40 space-y-1 overflow-y-auto rounded-lg border border-border bg-white/[0.03] p-2">
                      {docsInfo.files.map((f) => (
                        <div key={f.path} className="flex items-center justify-between gap-2 text-xs">
                          <a
                            href={`${API_SERVER_URL}${f.url}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-info-fg hover:text-info-fg truncate"
                            title={f.path}
                          >
                            {f.path}
                          </a>
                          <span className="text-muted shrink-0">{api.formatSize(f.size)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-sm text-muted">
                  <p>No documentation generated yet.</p>
                  <p className="mt-1">Run <span className="text-ink">fox generate-docs</span> to create docs.</p>
                  {docsError && <p className="mt-1 text-muted">{docsError}</p>}
                </div>
              )}
            </div>
          </div>
          </>
        )}
      </Modal>

      {/* Delete Confirmation Modal */}
      {deleteModal.isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-md w-full mx-4">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-ink mb-2 flex items-center">
                <FiTrash2 className="w-6 h-6 text-danger-fg mr-2" />
                Delete Repository
              </h3>
              <p className="text-ink-soft">
                Are you sure you want to permanently delete this repository?
              </p>
            </div>

            <div className="bg-danger-bg border border-danger-fg/20 rounded-lg p-4 mb-6">
              <p className="text-ink font-semibold mb-1">{deleteModal.repo?.name}</p>
              <p className="text-ink-soft text-sm mb-3">{deleteModal.repo?.description}</p>
              <div className="flex items-center space-x-4 text-xs text-muted">
                <span className="flex items-center">
                  <FiGitCommit className="w-3 h-3 mr-1" />
                  {deleteModal.repo?.commits} commits
                </span>
                <span className="flex items-center">
                  <FiUsers className="w-3 h-3 mr-1" />
                  Owner: {deleteModal.repo?.owner}
                </span>
              </div>
            </div>

            <div className="bg-warning-bg border border-warning-fg/20 rounded-lg p-3 mb-6">
              <p className="text-warning-fg text-sm font-medium flex items-start">
                <span className="mr-2">⚠️</span>
                <span>
                  This action cannot be undone. All commits, files, and related data will be permanently deleted.
                </span>
              </p>
            </div>

            <div className="flex space-x-3">
              <Button
                variant="secondary"
                className="flex-1"
                onClick={handleDeleteCancel}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                className="flex-1 bg-danger-fg hover:bg-danger-fg"
                onClick={handleDeleteConfirm}
                disabled={deleting}
              >
                {deleting ? (
                  <>
                    <FiLoader className="w-4 h-4 mr-2 animate-spin" />
                    Deleting...
                  </>
                ) : (
                  <>
                    <FiTrash2 className="w-4 h-4 mr-2" />
                    Delete Permanently
                  </>
                )}
              </Button>
            </div>
          </GlassCard>
        </div>
      )}

      {/* Code Editor Modal */}
      {editorOpen && editorRepo && (
        <CodeEditor
          repoId={editorRepo.id}
          repoName={editorRepo.name}
          onClose={() => {
            setEditorOpen(false)
            setEditorRepo(null)
          }}
        />
      )}

      {commitModalRepo && (
        <CommitHistoryModal
          repo={commitModalRepo}
          branch={selectedBranch}
          onClose={() => setCommitModalRepo(null)}
          onRollbackComplete={() => {
            setCommitModalRepo(null)
            // re-trigger file list reload by toggling selectedRepo
            if (selectedRepo) {
              const r = selectedRepo
              setSelectedRepo(null)
              setTimeout(() => setSelectedRepo(r), 0)
            }
          }}
        />
      )}

      {/* Commit History Modal */}
      {commitHistoryModalRepo && repoCommitHistory[commitHistoryModalRepo.id] && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-3xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-ink mb-2 flex items-center">
                <FiGitCommit className="w-6 h-6 text-ink mr-2" />
                Commit History - {commitHistoryModalRepo.name}
              </h3>
              <p className="text-ink-soft text-sm">All commits for this repository</p>
            </div>

            <div className="bg-cream-deep border border-border rounded-lg p-4 mb-6 overflow-x-auto">
              <p className="text-ink leading-relaxed whitespace-pre-wrap font-mono text-sm break-words">
                {repoCommitHistory[commitHistoryModalRepo.id]}
              </p>
            </div>

            <div className="flex justify-end space-x-3">
              <Button
                variant="secondary"
                onClick={() => setCommitHistoryModalRepo(null)}
              >
                Close
              </Button>
            </div>
          </GlassCard>
        </div>
      )}

      {/* Commit Messages Modal */}
      {commentModalRepo && repoComments[commentModalRepo.id] && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-3xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-ink mb-2 flex items-center">
                <FiMessageSquare className="w-6 h-6 text-success-fg mr-2" />
                Commit Messages - {commentModalRepo.name}
              </h3>
              <p className="text-ink-soft text-sm">All commit messages for this repository</p>
            </div>

            <div className="bg-success-bg border border-success-fg/20 rounded-lg p-4 mb-6">
              <p className="text-ink leading-relaxed whitespace-pre-wrap font-mono text-sm">
                {repoComments[commentModalRepo.id]}
              </p>
            </div>

            <div className="flex justify-end space-x-3">
              <Button
                variant="secondary"
                onClick={() => setCommentModalRepo(null)}
              >
                Close
              </Button>
            </div>
          </GlassCard>
        </div>
      )}

      {versioningTarget && selectedRepo && (
        <FileVersioningModal
          repo={selectedRepo}
          branch={selectedBranch}
          filePath={versioningTarget.path}
          onClose={() => setVersioningTarget(null)}
          onRollbackComplete={async () => {
            fetchRepositories()

            if (selectedRepo?.id) {
              try {
                setFilesLoading(true)
                const filesResponse = await api.getRepositoryFiles(selectedRepo.id, selectedBranch)
                if (filesResponse.success) {
                  const filesList = []
                  const filesDict = filesResponse.files || {}
                  const fileEntries = Array.isArray(filesDict)
                    ? filesDict.map((item, idx) => {
                        if (typeof item === 'string') return [item, {}]
                        const key = item?.path || item?.name || `file_${idx}`
                        return [key, item || {}]
                      })
                    : Object.entries(filesDict)

                  fileEntries.forEach(([pathName, file]) => {
                    const fileName = (pathName || '').split('/').pop() || pathName || 'unknown'
                    filesList.push({
                      name: fileName,
                      path: pathName,
                      type: 'file',
                      size: file?.size ? api.formatSize(file.size) : '0 B'
                    })
                  })

                  ;(filesResponse.folders || []).forEach(folder => {
                    const fileCount = fileEntries.filter(([pathName]) => (pathName || '').startsWith(folder + '/')).length
                    filesList.push({
                      name: folder,
                      type: 'folder',
                      files: fileCount
                    })
                  })

                  setRepoFiles(filesList)
                }
              } catch (err) {
                console.error('Error refreshing files after restore:', err)
              } finally {
                setFilesLoading(false)
              }
            }

            setVersioningTarget(null)
          }}
        />
      )}

      {filePreview && selectedRepo && (
        <ModalOverlay onClose={() => { setFilePreview(null); setFilePreviewError(null) }}>
          <GlassCard className="relative max-h-[90vh] w-full max-w-4xl overflow-y-auto p-5" hover={false}>
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <p className="text-xs uppercase tracking-[0.16em] text-muted">File Preview</p>
                <h3 className="text-lg font-semibold text-ink break-all">{filePreview.path}</h3>
                <p className="text-xs text-muted mt-1">Repository: {selectedRepo.name} {selectedBranch ? `• Branch: ${selectedBranch}` : ''}</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => { setFilePreview(null); setFilePreviewError(null) }}
              >
                <FiX className="w-4 h-4 mr-2" />
                Close
              </Button>
            </div>

            {filePreviewError && (
              <div className="mb-3 rounded-lg border border-danger-fg/20 bg-danger-bg px-3 py-2 text-sm text-danger-fg">
                {filePreviewError}
              </div>
            )}

            {filePreviewLoading ? (
              <div className="flex items-center justify-center py-12 text-ink-soft">
                <FiLoader className="w-5 h-5 mr-2 animate-spin" />
                Loading file content...
              </div>
            ) : filePreview.is_binary ? (
              <div className="rounded-lg border border-warning-fg/20 bg-warning-bg px-4 py-3 text-sm text-warning-fg">
                This file is binary or non-text. Use download if you want the raw file.
              </div>
            ) : (
              <div className="rounded-lg border border-border bg-cream-deep max-h-[65vh] overflow-auto">
                <pre className="p-4 text-xs text-ink font-mono whitespace-pre-wrap break-words">
                  {filePreview.content || ''}
                </pre>
              </div>
            )}
          </GlassCard>
        </ModalOverlay>
      )}

      {pullRequestsModalRepo && (
        <PullRequestsModal
          repo={pullRequestsModalRepo}
          onClose={() => setPullRequestsModalRepo(null)}
        />
      )}

      {issuesModalRepo && (
        <IssuesModal
          repo={issuesModalRepo}
          onClose={() => setIssuesModalRepo(null)}
        />
      )}

      {automationRepo && (
        <AutomationModal
          open
          repo={automationRepo}
          /* Status checks hang off a commit, so pass the tip of whatever branch
             is being viewed rather than the repository's default. */
          headCommitId={
            (branches || []).find((b) => (b.name || b) === selectedBranch)?.head_commit_id ||
            automationRepo.head_commit_id ||
            null
          }
          canManage={isAdmin || automationRepo.owner === currentUsername}
          onClose={() => setAutomationRepo(null)}
        />
      )}

      {/* Rename Repository Modal */}
      {renameModal.isOpen && renameModal.repo && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-md w-full mx-4">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-ink mb-2 flex items-center">
                <FiEdit3 className="w-6 h-6 text-ink mr-2" />
                Rename Repository
              </h3>
              <p className="text-ink-soft text-sm">
                Change the name of "{renameModal.repo.name}"
              </p>
            </div>

            <div className="mb-6">
              <input
                type="text"
                value={renamingValue}
                onChange={(e) => setRenamingValue(e.target.value)}
                placeholder="Enter new repository name"
                className="w-full px-4 py-2 bg-cream-mid border border-border rounded-lg text-ink placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-ink/20 focus:border-transparent"
                autoFocus
              />
              <p className="text-xs text-muted mt-2">
                The repository name will be updated everywhere in the system
              </p>
            </div>

            <div className="flex space-x-3">
              <Button
                variant="secondary"
                className="flex-1"
                onClick={handleRenameCancel}
                disabled={renaming}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                className="flex-1"
                onClick={handleRenameRepository}
                disabled={renaming || !renamingValue.trim()}
              >
                {renaming ? (
                  <>
                    <FiLoader className="w-4 h-4 mr-2 animate-spin" />
                    Renaming...
                  </>
                ) : (
                  <>
                    <FiEdit3 className="w-4 h-4 mr-2" />
                    Rename
                  </>
                )}
              </Button>
            </div>
          </GlassCard>
        </div>
      )}
    </div>
  )
}

export default Repositories
