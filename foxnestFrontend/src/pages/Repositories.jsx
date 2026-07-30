import React, { useState, useEffect } from 'react'
import { FiFolder, FiGitCommit, FiUsers, FiStar, FiEye, FiGitBranch, FiClock, FiArchive, FiEdit3, FiTrash2, FiWifi, FiWifiOff, FiLoader, FiCode, FiMessageSquare, FiSearch, FiX, FiCheck, FiFileText, FiRotateCcw, FiGitPullRequest, FiHash } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import CodeEditor from '../components/CodeEditor'
import CommitHistoryModal from '../components/CommitHistoryModal'
import FileVersioningModal from '../components/FileVersioningModal'
import PullRequestsModal from '../components/PullRequestsModal'
import IssuesModal from '../components/IssuesModal'
import { useRepositories, useServerHealth } from '../hooks/useApi'
import api from '../utils/api'
import { API_SERVER_URL } from '../config'
import { getSessionUser, getSessionToken } from '../utils/session'

const Repositories = () => {
  const sessionUser = getSessionUser()
  const currentUsername = sessionUser.username
  const hasAuthToken = !!getSessionToken()
  const isAdmin = sessionUser.isAdmin

  const [selectedRepo, setSelectedRepo] = useState(null)
  const [viewMode, setViewMode] = useState('grid') // 'grid' or 'list'
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
  const [issuesModalRepo, setIssuesModalRepo] = useState(null)
  const [starBusyId, setStarBusyId] = useState(null)

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
        <FiLoader className="w-8 h-8 text-white animate-spin" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center py-12">
        <p className="text-red-400 mb-4">{error}</p>
        <Button onClick={fetchRepositories}>Retry</Button>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Archive Success Notification */}
      {archiveSuccess && (
        <div className="bg-green-500/20 border border-green-500/60 rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <FiCheck className="w-5 h-5 text-green-400" />
            <p className="text-green-300">Repository archived successfully! View it in the <span className="font-semibold">Archive</span> section.</p>
          </div>
          <button 
            onClick={() => setArchiveSuccess(false)}
            className="text-green-300 hover:text-green-200"
          >
            <FiX className="w-5 h-5" />
          </button>
        </div>
      )}

      {!currentUsername && (
        <div className="bg-yellow-500/20 border border-yellow-500/60 rounded-lg p-4">
          <p className="text-yellow-200 text-sm">
            Session username is not set. Set localStorage key <span className="font-semibold">foxnest_username</span> to enable owner actions.
          </p>
        </div>
      )}

      {/* Search Bar */}
      <div className="relative">
        <div className="flex items-center bg-white/10 border border-white/20 rounded-xl px-4 py-2 focus-within:border-purple-400 transition-colors">
          <FiSearch className="w-5 h-5 text-white/50 mr-3" />
          <input
            type="text"
            placeholder="Search repositories by name, description, or owner..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="flex-1 bg-transparent text-white placeholder-white/50 outline-none"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="ml-2 p-1 hover:bg-white/10 rounded transition-colors"
            >
              <FiX className="w-4 h-4 text-white/70" />
            </button>
          )}
        </div>
      </div>

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Repositories</h1>
          <p className="text-white/70">Browse and manage all your repositories</p>
        </div>
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1 bg-white/10 rounded-lg p-1">
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 rounded text-sm transition-colors ${
                viewMode === 'grid' ? 'bg-white/20 text-white' : 'text-white/70 hover:text-white'
              }`}
            >
              Grid
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`p-2 rounded text-sm transition-colors ${
                viewMode === 'list' ? 'bg-white/20 text-white' : 'text-white/70 hover:text-white'
              }`}
            >
              List
            </button>
          </div>
        </div>
      </div>

      {/* Repository Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiFolder className="w-8 h-8 text-blue-400 mr-3" />
            <div>
              <p className="text-2xl font-semibold text-white">{filteredRepos.length}</p>
              <p className="text-sm text-white/70">Active Repos</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiGitCommit className="w-8 h-8 text-green-400 mr-3" />
            <div>
              <p className="text-2xl font-semibold text-white">
                {repositories.reduce((sum, repo) => sum + repo.commits, 0)}
              </p>
              <p className="text-sm text-white/70">Total Commits</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiUsers className="w-8 h-8 text-purple-400 mr-3" />
            <div>
              <p className="text-2xl font-semibold text-white">
                {repositories.length
                  ? Math.max(...repositories.map((repo) => repo.contributors || 0))
                  : 0}
              </p>
              <p className="text-sm text-white/70">Max Contributors</p>
            </div>
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <div className="flex items-center">
            <FiStar className="w-8 h-8 text-yellow-400 mr-3" />
            <div>
              <p className="text-2xl font-semibold text-white">
                {repositories.reduce((sum, repo) => sum + repo.stars, 0)}
              </p>
              <p className="text-sm text-white/70">Total Stars</p>
            </div>
          </div>
        </GlassCard>
      </div>

      {/* Repositories Grid/List */}
      {viewMode === 'grid' ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
          {filteredRepos.map((repo) => (
            <div key={repo.id} className="relative">
              <GlassCard
                className={`p-6 cursor-pointer transition-all duration-300 ${
                  selectedRepo?.id === repo.id ? 'ring-2 ring-purple-400 bg-white/15' : ''
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
                        <h3 className="font-semibold text-white text-base leading-tight mb-1" title={repo.name}>{repo.name}</h3>
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
                            ? 'border-amber-400/50 bg-amber-500/20 text-amber-300'
                            : 'border-white/15 bg-white/5 text-white/70 hover:bg-white/10 hover:border-amber-400/30'
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
                    <div className="flex items-center justify-start space-x-2 flex-wrap">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={(e) => { e.stopPropagation(); if (repoComments[repo.id]) setCommentModalRepo(repo) }}
                        title={repoComments[repo.id] ? "View commit messages" : "No commit messages"}
                        disabled={!repoComments[repo.id]}
                        className={repoComments[repo.id] ? 'bg-green-500/20 hover:bg-green-500/40 border border-green-500/50 hover:border-green-400' : 'bg-gray-500/10 border border-gray-500/30 cursor-not-allowed opacity-50'}
                      >
                        <FiMessageSquare className={`w-4 h-4 ${repoComments[repo.id] ? 'text-green-400' : 'text-gray-500'}`} />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleViewCommits(e, repo) }} title="View commit history" className="bg-purple-500/20 hover:bg-purple-500/40 border border-purple-500/50 hover:border-purple-400">
                        <FiGitCommit className="w-4 h-4 text-purple-400" />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={(e) => handleOpenEditor(e, repo)} title="Open in Editor">
                        <FiCode className="w-4 h-4 text-blue-400" />
                      </Button>
                      {(canWriteRepo(repo) || canManageRepo(repo)) ? (
                        <>
                          {canWriteRepo(repo) && (
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleEditRepo(e, repo) }} title="Edit Repository">
                              <FiEdit3 className="w-4 h-4" />
                            </Button>
                          )}
                          {canManageRepo(repo) && (
                            <>
                              <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleArchive(repo.id) }} title="Archive Repository">
                                <FiArchive className="w-4 h-4" />
                              </Button>
                              <Button variant="ghost" size="sm" onClick={(e) => handleDeleteClick(e, repo)} title="Delete Repository">
                                <FiTrash2 className="w-4 h-4 text-red-400" />
                              </Button>
                            </>
                          )}
                        </>
                      ) : (
                        <div className="text-xs text-white/50 px-2">Read-only</div>
                      )}
                    </div>
                  </div>
                </div>
                <p className="text-sm text-white/70 break-words mb-4">{repo.description}</p>
                <div className="flex items-center space-x-2 mb-4">
                  {repo.language && repo.language !== 'Unknown' && (
                    <div className="flex items-center space-x-1">
                      <div className="w-3 h-3 rounded-full" style={{ backgroundColor: repo.languageColor }}></div>
                      <span className="text-sm text-white/70">{repo.language}</span>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1">
                    {repo.tags.slice(0, 2).map((tag, index) => (
                      <Badge key={index} variant="default" className="text-xs">{tag}</Badge>
                    ))}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <div className="flex items-center space-x-2 text-sm text-white/70">
                    <FiGitCommit className="w-4 h-4" />
                    <span>{repo.commits} {repo.commits === 1 ? 'Commit' : 'Commits'}</span>
                  </div>
                  <div className="flex items-center space-x-2 text-sm text-white/70">
                    <FiUsers className="w-4 h-4" />
                    <span>{repo.contributors} {repo.contributors === 1 ? 'Contributor' : 'Contributors'}</span>
                  </div>
                  <div className="flex items-center space-x-2 text-sm text-white/70">
                    <button
                      type="button"
                      onClick={(e) => handleStarToggle(e, repo)}
                      disabled={starBusyId === repo.id || !hasAuthToken}
                      className={`flex items-center space-x-1 rounded-md px-1.5 py-0.5 -ml-1 transition-colors disabled:opacity-50 ${
                        repo.starredByMe ? 'text-amber-300 bg-amber-500/15' : 'hover:bg-white/10 text-white/70'
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
                  <div className="flex items-center space-x-2 text-sm text-white/70">
                    <FiGitBranch className="w-4 h-4" />
                    <span>{repo.branches} {repo.branches === 1 ? 'Branch' : 'Branches'}</span>
                  </div>
                </div>
                <div className="flex items-center justify-between text-xs text-white/50">
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
        <GlassCard className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10 text-left">
                  <th className="px-4 py-3 text-white/60 font-medium">Repository</th>
                  <th className="px-4 py-3 text-white/60 font-medium">Owner</th>
                  <th className="px-4 py-3 text-white/60 font-medium">Language</th>
                  <th className="px-4 py-3 text-white/60 font-medium text-center">
                    <FiGitCommit className="inline w-4 h-4 mr-1" />Commits
                  </th>
                  <th className="px-4 py-3 text-white/60 font-medium text-center">
                    <FiGitBranch className="inline w-4 h-4 mr-1" />Branches
                  </th>
                  <th className="px-4 py-3 text-white/60 font-medium text-center">
                    <FiStar className="inline w-4 h-4 mr-1" />Stars
                  </th>
                  <th className="px-4 py-3 text-white/60 font-medium text-center">
                    <FiUsers className="inline w-4 h-4 mr-1" />Contributors
                  </th>
                  <th className="px-4 py-3 text-white/60 font-medium">Size</th>
                  <th className="px-4 py-3 text-white/60 font-medium">Updated</th>
                  <th className="px-4 py-3 text-white/60 font-medium text-center">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredRepos.map((repo, idx) => (
                  <tr
                    key={repo.id}
                    className={`border-b border-white/5 transition-colors cursor-pointer hover:bg-white/10 ${
                      selectedRepo?.id === repo.id ? 'bg-white/10' : idx % 2 === 0 ? 'bg-white/[0.02]' : ''
                    }`}
                    onClick={() => handleRepoClick(repo)}
                  >
                    {/* Name + visibility */}
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <FiFolder className="w-4 h-4 text-purple-400 shrink-0" />
                        <span className="font-medium text-white truncate max-w-[180px]" title={repo.name}>{repo.name}</span>
                        <Badge variant={repo.visibility === 'public' ? 'success' : 'warning'} className="text-xs shrink-0">
                          {repo.visibility}
                        </Badge>
                      </div>
                    </td>
                    {/* Owner */}
                    <td className="px-4 py-3 text-white/70 whitespace-nowrap">{repo.owner || '—'}</td>
                    {/* Language */}
                    <td className="px-4 py-3">
                      {repo.language && repo.language !== 'Unknown' ? (
                        <div className="flex items-center gap-1">
                          <div className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: repo.languageColor }}></div>
                          <span className="text-white/70">{repo.language}</span>
                        </div>
                      ) : <span className="text-white/30">—</span>}
                    </td>
                    {/* Commits */}
                    <td className="px-4 py-3 text-center text-white/70">{repo.commits}</td>
                    {/* Branches */}
                    <td className="px-4 py-3 text-center text-white/70">{repo.branches}</td>
                    {/* Stars */}
                    <td className="px-4 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        onClick={(e) => handleStarToggle(e, repo)}
                        disabled={starBusyId === repo.id || !hasAuthToken}
                        className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-sm disabled:opacity-50 ${
                          repo.starredByMe ? 'text-amber-300 bg-amber-500/15' : 'text-white/70 hover:bg-white/10'
                        }`}
                        title={!hasAuthToken ? 'Sign in to star' : repo.starredByMe ? 'Remove your star' : 'Star'}
                      >
                        <FiStar className={`w-3.5 h-3.5 ${repo.starredByMe ? 'fill-current' : ''}`} />
                        {repo.stars}
                      </button>
                    </td>
                    {/* Contributors */}
                    <td className="px-4 py-3 text-center text-white/70">{repo.contributors}</td>
                    {/* Size */}
                    <td className="px-4 py-3 text-white/60 whitespace-nowrap">{repo.size}</td>
                    {/* Updated */}
                    <td className="px-4 py-3 text-white/50 whitespace-nowrap text-xs">{repo.lastUpdate}</td>
                    {/* Actions */}
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-center gap-1" onClick={(e) => e.stopPropagation()}>
                        <Button
                          variant="ghost" size="sm"
                          onClick={(e) => { e.stopPropagation(); if (repoComments[repo.id]) setCommentModalRepo(repo) }}
                          title={repoComments[repo.id] ? "View commit messages" : "No commit messages"}
                          disabled={!repoComments[repo.id]}
                          className={repoComments[repo.id] ? 'bg-green-500/20 hover:bg-green-500/40 border border-green-500/50' : 'opacity-40 cursor-not-allowed'}
                        >
                          <FiMessageSquare className={`w-3.5 h-3.5 ${repoComments[repo.id] ? 'text-green-400' : 'text-gray-500'}`} />
                        </Button>
                        <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleViewCommits(e, repo) }} title="Commit history" className="bg-purple-500/20 hover:bg-purple-500/40 border border-purple-500/50">
                          <FiGitCommit className="w-3.5 h-3.5 text-purple-400" />
                        </Button>
                        <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleOpenEditor(e, repo) }} title="Open in Editor">
                          <FiCode className="w-3.5 h-3.5 text-blue-400" />
                        </Button>
                        {canWriteRepo(repo) && (
                          <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleEditRepo(e, repo) }} title="Edit">
                            <FiEdit3 className="w-3.5 h-3.5" />
                          </Button>
                        )}
                        {canManageRepo(repo) && (
                          <>
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleArchive(repo.id) }} title="Archive">
                              <FiArchive className="w-3.5 h-3.5" />
                            </Button>
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); handleDeleteClick(e, repo) }} title="Delete">
                              <FiTrash2 className="w-3.5 h-3.5 text-red-400" />
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </GlassCard>
      )}

      {/* Repository Details Modal */}
      {selectedRepo && (
        <GlassCard className="p-6 mt-6">
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-xl font-semibold text-white flex items-center">
              <FiFolder className="w-5 h-5 mr-2" />
              {selectedRepo.name}
            </h2>
            <div className="flex items-center gap-2 flex-wrap">
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => handleStarToggle(e, selectedRepo)}
                disabled={starBusyId === selectedRepo.id || !hasAuthToken}
                title={
                  !hasAuthToken
                    ? 'Sign in to star'
                    : selectedRepo.starredByMe
                      ? 'Remove your star'
                      : 'Star repository'
                }
                className={selectedRepo.starredByMe ? 'text-amber-300 border border-amber-500/40' : ''}
              >
                <FiStar className={`w-4 h-4 mr-2 ${selectedRepo.starredByMe ? 'fill-current' : ''}`} />
                {selectedRepo.stars ?? 0}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setPullRequestsModalRepo(selectedRepo)}
                title="Open pull requests"
              >
                <FiGitPullRequest className="w-4 h-4 mr-2" />
                Pull Requests
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setIssuesModalRepo(selectedRepo)}
                title="Open issues"
              >
                <FiHash className="w-4 h-4 mr-2" />
                Issues
              </Button>
              <Button 
                variant="ghost" 
                size="sm"
                onClick={() => setSelectedRepo(null)}
              >
                Close
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Repository Info */}
            <div>
              <h3 className="text-lg font-medium text-white mb-4">Repository Information</h3>
              <div className="space-y-3">
                {selectedRepo.language && (
                  <div className="flex justify-between">
                    <span className="text-white/70">Language:</span>
                    <div className="flex items-center space-x-1">
                      <div 
                        className="w-3 h-3 rounded-full" 
                        style={{ backgroundColor: selectedRepo.languageColor }}
                      ></div>
                      <span className="text-white">{selectedRepo.language}</span>
                    </div>
                  </div>
                )}
                <div className="flex justify-between">
                  <span className="text-white/70">Size:</span>
                  <span className="text-white">{selectedRepo.size}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-white/70">Visibility:</span>
                  <Badge variant={selectedRepo.visibility === 'public' ? 'success' : 'warning'}>
                    {selectedRepo.visibility}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-white/70">Last Update:</span>
                  <span className="text-white">{selectedRepo.lastUpdate}</span>
                </div>
              </div>
            </div>

            {/* Files Structure */}
            <div>
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-medium text-white">Files & Folders</h3>
                {branchesLoading && (
                  <FiLoader className="w-4 h-4 text-white/50 animate-spin" />
                )}
              </div>

              {/* Branch Selector */}
              {branches.length > 0 && (
                <div className="mb-4 p-3 rounded-lg bg-white/5 border border-white/10">
                  <label className="block text-sm text-white/70 mb-2">
                    <FiGitBranch className="w-4 h-4 mr-2 inline" />
                    Branch
                  </label>
                  <select 
                    value={selectedBranch || ''} 
                    onChange={(e) => setSelectedBranch(e.target.value)}
                    className="w-full px-3 py-2 bg-white/10 border border-white/20 rounded text-white text-sm focus:border-purple-400 focus:outline-none transition-colors"
                    disabled={filesLoading}
                  >
                    {branches.map((branch, index) => (
                      <option
                        key={index}
                        value={branch.name || branch}
                        className="bg-slate-100 text-slate-900"
                      >
                        {branch.name || branch}
                        {branch.is_default ? ' (default)' : ''}
                      </option>
                    ))}
                  </select>
                  {selectedBranch && selectedBranch.startsWith('rollback_') && (
                    <p className="text-xs text-orange-300 mt-2 bg-orange-500/10 p-2 rounded border border-orange-400/20">
                      📌 Rollback branch: This shows the state at a specific commit
                    </p>
                  )}
                </div>
              )}

              {filesLoading && (
                <div className="flex items-center justify-center py-8 text-white/50">
                  <FiLoader className="w-4 h-4 animate-spin mr-2" />
                  Loading files...
                </div>
              )}

              {!filesLoading && (
                <div className="space-y-2 max-h-64 overflow-y-auto">
                  {repoFiles && repoFiles.length > 0 ? (
                    repoFiles.map((file, index) => (
                      <div key={index} className="flex items-center justify-between p-2 rounded-lg bg-white/10 hover:bg-white/15 transition-colors">
                        <div className="flex items-center space-x-2">
                          {file.type === 'folder' ? (
                            <FiFolder className="w-4 h-4 text-blue-400" />
                          ) : (
                            <div className="w-4 h-4 bg-white/30 rounded"></div>
                          )}
                          {file.type === 'file' && file.path ? (
                            <button
                              type="button"
                              onClick={() => handleOpenFilePreview(file)}
                              className="text-white text-sm hover:text-blue-200 underline-offset-2 hover:underline text-left"
                              title={`Open ${file.path}`}
                            >
                              {file.name}
                            </button>
                          ) : (
                            <span className="text-white text-sm">{file.name}</span>
                          )}
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="text-white/50 text-xs">
                            {file.type === 'folder' ? `${file.files} files` : file.size}
                          </span>
                          {file.type === 'file' && file.path && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-blue-200 hover:text-white"
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
                    <p className="text-white/50 text-sm">No files available</p>
                  )}
                </div>
              )}
            </div>

            {/* Documentation */}
            <div className="mt-6">
              <h3 className="text-lg font-medium text-white mb-3">Documentation</h3>
              {docsLoading ? (
                <div className="flex items-center space-x-2 text-white/70">
                  <FiLoader className="w-4 h-4 animate-spin" />
                  <span className="text-sm">Checking docs...</span>
                </div>
              ) : docsInfo?.success ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between text-sm text-white/70">
                    <span>Files:</span>
                    <span className="text-white">{docsInfo.files?.length || 0}</span>
                  </div>
                  {docsInfo.metadata?.generated_at && (
                    <div className="flex items-center justify-between text-sm text-white/70">
                      <span>Generated:</span>
                      <span className="text-white">{new Date(docsInfo.metadata.generated_at).toLocaleString()}</span>
                    </div>
                  )}
                  <p className="text-xs text-white/50">
                    &quot;Open overview&quot; opens the main index only. Use the file list or ZIP to get all {docsInfo.files?.length || 0} files.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="bg-blue-500/20 hover:bg-blue-500/40 border border-blue-500/50 hover:border-blue-400"
                      onClick={() => window.open(`${API_SERVER_URL}${docsInfo.index_url}`, '_blank', 'noopener,noreferrer')}
                    >
                      <FiFileText className="w-4 h-4 mr-2" />
                      Open overview (index)
                    </Button>
                    <a
                      href={`${API_SERVER_URL}${docsInfo.archive_url || api.getRepositoryDocsArchivePath(selectedRepo.id)}`}
                      download
                      className="inline-flex items-center justify-center rounded-lg text-sm px-3 py-1.5 bg-emerald-500/20 hover:bg-emerald-500/35 border border-emerald-500/50 text-white transition-colors"
                    >
                      <FiFileText className="w-4 h-4 mr-2" />
                      Download all ({docsInfo.files?.length || 0} files, ZIP)
                    </a>
                  </div>
                  {Array.isArray(docsInfo.files) && docsInfo.files.length > 0 && (
                    <div className="max-h-40 overflow-y-auto rounded-lg border border-white/10 bg-white/5 p-2 space-y-1">
                      {docsInfo.files.map((f) => (
                        <div key={f.path} className="flex items-center justify-between gap-2 text-xs">
                          <a
                            href={`${API_SERVER_URL}${f.url}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-300 hover:text-blue-100 truncate"
                            title={f.path}
                          >
                            {f.path}
                          </a>
                          <span className="text-white/40 shrink-0">{api.formatSize(f.size)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-sm text-white/60">
                  <p>No documentation generated yet.</p>
                  <p className="mt-1">Run <span className="text-white">fox generate-docs</span> to create docs.</p>
                  {docsError && <p className="mt-1 text-white/40">{docsError}</p>}
                </div>
              )}
            </div>
          </div>
        </GlassCard>
      )}

      {/* Delete Confirmation Modal */}
      {deleteModal.isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-md w-full mx-4">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-white mb-2 flex items-center">
                <FiTrash2 className="w-6 h-6 text-red-400 mr-2" />
                Delete Repository
              </h3>
              <p className="text-white/70">
                Are you sure you want to permanently delete this repository?
              </p>
            </div>

            <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 mb-6">
              <p className="text-white font-semibold mb-1">{deleteModal.repo?.name}</p>
              <p className="text-white/70 text-sm mb-3">{deleteModal.repo?.description}</p>
              <div className="flex items-center space-x-4 text-xs text-white/60">
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

            <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 mb-6">
              <p className="text-yellow-200 text-sm font-medium flex items-start">
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
                className="flex-1 bg-red-500 hover:bg-red-600"
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
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-3xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-white mb-2 flex items-center">
                <FiGitCommit className="w-6 h-6 text-purple-400 mr-2" />
                Commit History - {commitHistoryModalRepo.name}
              </h3>
              <p className="text-white/70 text-sm">All commits for this repository</p>
            </div>

            <div className="bg-purple-500/10 border border-purple-500/30 rounded-lg p-4 mb-6 overflow-x-auto">
              <p className="text-white leading-relaxed whitespace-pre-wrap font-mono text-sm break-words">
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
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-3xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-white mb-2 flex items-center">
                <FiMessageSquare className="w-6 h-6 text-green-400 mr-2" />
                Commit Messages - {commentModalRepo.name}
              </h3>
              <p className="text-white/70 text-sm">All commit messages for this repository</p>
            </div>

            <div className="bg-green-500/10 border border-green-500/30 rounded-lg p-4 mb-6">
              <p className="text-white leading-relaxed whitespace-pre-wrap font-mono text-sm">
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
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="absolute inset-0" onClick={() => { setFilePreview(null); setFilePreviewError(null) }} />
          <GlassCard className="relative p-5 w-full max-w-4xl" hover={false}>
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <p className="text-xs uppercase tracking-[0.16em] text-white/50">File Preview</p>
                <h3 className="text-lg font-semibold text-white break-all">{filePreview.path}</h3>
                <p className="text-xs text-white/60 mt-1">Repository: {selectedRepo.name} {selectedBranch ? `• Branch: ${selectedBranch}` : ''}</p>
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
              <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
                {filePreviewError}
              </div>
            )}

            {filePreviewLoading ? (
              <div className="flex items-center justify-center py-12 text-white/70">
                <FiLoader className="w-5 h-5 mr-2 animate-spin" />
                Loading file content...
              </div>
            ) : filePreview.is_binary ? (
              <div className="rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-sm text-yellow-100">
                This file is binary or non-text. Use download if you want the raw file.
              </div>
            ) : (
              <div className="rounded-lg border border-white/10 bg-black/30 max-h-[65vh] overflow-auto">
                <pre className="p-4 text-xs text-white/90 font-mono whitespace-pre-wrap break-words">
                  {filePreview.content || ''}
                </pre>
              </div>
            )}
          </GlassCard>
        </div>
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

      {/* Rename Repository Modal */}
      {renameModal.isOpen && renameModal.repo && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <GlassCard className="p-6 max-w-md w-full mx-4">
            <div className="mb-4">
              <h3 className="text-xl font-bold text-white mb-2 flex items-center">
                <FiEdit3 className="w-6 h-6 text-purple-400 mr-2" />
                Rename Repository
              </h3>
              <p className="text-white/70 text-sm">
                Change the name of "{renameModal.repo.name}"
              </p>
            </div>

            <div className="mb-6">
              <input
                type="text"
                value={renamingValue}
                onChange={(e) => setRenamingValue(e.target.value)}
                placeholder="Enter new repository name"
                className="w-full px-4 py-2 bg-white/10 border border-white/20 rounded-lg text-white placeholder-white/50 focus:outline-none focus:ring-2 focus:ring-purple-400 focus:border-transparent"
                autoFocus
              />
              <p className="text-xs text-white/50 mt-2">
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
