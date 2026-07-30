import React, { useState, useEffect } from 'react'
import { FiArchive, FiRefreshCw, FiTrash2, FiDownload, FiClock, FiFolder, FiGitCommit, FiUsers, FiLoader, FiEdit3, FiUpload, FiCheck, FiX, FiFileText, FiCode } from 'react-icons/fi'
import GlassCard from '../components/ui/GlassCard'
import Badge from '../components/ui/Badge'
import Button from '../components/ui/Button'
import CodeEditor from '../components/CodeEditor'
import api from '../utils/api'
import { API_SERVER_URL } from '../config.js'
import { getSessionToken, getSessionUser } from '../utils/session'

const Archive = () => {
  const sessionUser = getSessionUser()
  const token = getSessionToken()
  const currentUsername = sessionUser.username
  const isAdmin = sessionUser.isAdmin

  const [selectedRepo, setSelectedRepo] = useState(null)
  const [repositories, setRepositories] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editingRepo, setEditingRepo] = useState(null)
  const [editForm, setEditForm] = useState({
    g1_coordinator: '',
    tested: false
  })
  const [uploadingManual, setUploadingManual] = useState(false)
  const [deleteModal, setDeleteModal] = useState({ isOpen: false, repo: null })
  const [deleting, setDeleting] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorRepo, setEditorRepo] = useState(null)

  useEffect(() => {
    fetchRepositories()
  }, [])

  const fetchRepositories = async () => {
    try {
      setLoading(true)
      setError(null)

      // Fetch all repositories
      const response = await api.listAllRepositories()
      
      if (response.success) {
        // Filter only archived repositories
        const archivedRepos = response.repositories.filter(repo => repo.is_archived === true)
        
        // Transform server data to match our component expectations
        const transformedRepos = archivedRepos.map((repo, index) => ({
          id: repo.id,
          name: repo.name,
          description: repo.description || `Repository owned by ${repo.owner}`,
          language: repo.language || 'Unknown',
          languageColor: '#6c757d',
          commits: repo.commits?.length || 0,
          contributors: 1,
          stars: 0,
          watchers: 0,
          branches: 1,
          size: repo.size ? `${(repo.size / 1024).toFixed(2)} KB` : '0 KB',
          lastUpdate: api.formatDate(repo.created_at),
          status: 'archived',
          visibility: repo.is_public ? 'public' : 'private',
          tags: getTags(repo.name),
          owner: repo.owner,
          createdAt: repo.created_at,
          head: repo.head,
          is_archived: true,
          archived_at: repo.archived_at,
          archivedDate: repo.archived_at ? new Date(repo.archived_at).toISOString().split('T')[0] : 'Unknown',
          archivedBy: repo.owner,
          archived_reason: repo.archived_reason,
          reason: repo.archived_reason || getArchiveReason(repo.name),
          g1_coordinator: repo.g1_coordinator,
          tested: repo.tested || false,
          instruction_manual_path: repo.instruction_manual_path,
          instruction_manual_filename: repo.instruction_manual_filename,
          has_instruction_manual: repo.has_instruction_manual
        }))
        
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

  const getArchiveReason = (repoName) => {
    const reasons = {
      'legacy-system': 'Project completed and no longer maintained',
      'old-mobile-prototype': 'Replaced by React Native implementation',
      'experimental-ui': 'Merged into main design system',
      'temp-data-migration': 'Migration completed successfully'
    }
    return reasons[repoName] || 'Archived for cleanup'
  }

  const getTags = (repoName) => {
    const tagMap = {
      'legacy-system': ['python', 'legacy', 'completed'],
      'old-mobile-prototype': ['flutter', 'prototype', 'replaced'],
      'experimental-ui': ['javascript', 'ui', 'experimental'],
      'temp-data-migration': ['sql', 'migration', 'temporary']
    }
    return tagMap[repoName] || ['archived']
  }



  // Use server data if available, otherwise fallback to hardcoded data
  const displayRepositories = repositories.length > 0 ? repositories : repositories

  const handleRestore = (repoId) => {
    console.log('Restoring repository:', repoId)
    // Handle restore logic here
  }

  const handleOpenEditor = (e, repo) => {
    e.stopPropagation()
    setEditorRepo(repo)
    setEditorOpen(true)
  }

  const handleDeleteClick = (e, repo) => {
    e.stopPropagation()
    setDeleteModal({ isOpen: true, repo })
  }

  const handleDelete = (repoId) => {
    const repo = repositories.find(r => r.id === repoId)
    if (repo) {
      setDeleteModal({ isOpen: true, repo })
    }
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

  const handleEditRepo = (repo) => {
    setEditingRepo(repo.id)
    setEditForm({
      g1_coordinator: repo.g1_coordinator || '',
      tested: repo.tested || false
    })
  }

  const handleSaveEdit = async () => {
    try {
      const actorParam = currentUsername ? `?actor_username=${encodeURIComponent(currentUsername)}` : ''
      const response = await fetch(`${API_SERVER_URL}/api/repository/${editingRepo}/details${actorParam}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(editForm)
      })
      
      if (response.ok) {
        // Refresh repositories to show updated data
        await fetchRepositories()
        setEditingRepo(null)
        setEditForm({ g1_coordinator: '', tested: false })
      }
    } catch (error) {
      console.error('Error updating repository:', error)
    }
  }

  const handleCancelEdit = () => {
    setEditingRepo(null)
    setEditForm({ g1_coordinator: '', tested: false })
  }

  const handleFileUpload = async (event, repoId) => {
    const file = event.target.files[0]
    if (!file || file.type !== 'application/pdf') {
      alert('Please select a PDF file')
      return
    }

    setUploadingManual(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const actorParam = currentUsername ? `?actor_username=${encodeURIComponent(currentUsername)}` : ''

      const response = await fetch(`${API_SERVER_URL}/api/repository/${repoId}/upload-manual${actorParam}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
        body: formData
      })

      if (response.ok) {
        // Refresh repositories to show updated data
        await fetchRepositories()
      } else {
        alert('Failed to upload instruction manual')
      }
    } catch (error) {
      console.error('Error uploading file:', error)
      alert('Error uploading file')
    } finally {
      setUploadingManual(false)
    }
  }

  const handleDownloadManual = async (repoId) => {
    try {
      const response = await fetch(`${API_SERVER_URL}/api/repository/${repoId}/download-manual`, {
        headers: {
          Authorization: `Bearer ${token}`,
        }
      })
      if (response.ok) {
        const blob = await response.blob()
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.style.display = 'none'
        a.href = url
        a.download = 'instruction_manual.pdf'
        document.body.appendChild(a)
        a.click()
        window.URL.revokeObjectURL(url)
      } else {
        alert('No instruction manual found for this repository')
      }
    } catch (error) {
      console.error('Error downloading manual:', error)
      alert('Error downloading instruction manual')
    }
  }

  const handleExport = (repoId) => {
    console.log('Exporting repository:', repoId)
    // Handle export logic here
  }

  const handleRepoClick = (repo) => {
    setSelectedRepo(selectedRepo?.id === repo.id ? null : repo)
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

  const handleRefresh = () => {
    fetchRepositories()
  }

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center h-64">
          <FiLoader className="w-8 h-8 animate-spin text-blue-400" />
          <span className="ml-2 text-gray-300">Loading repositories...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <FiArchive className="w-6 h-6 text-orange-400" />
          <h1 className="text-2xl font-bold text-white">Archived Repositories</h1>
          <Badge variant="secondary" className="text-orange-400 border-orange-400/30">
            {displayRepositories.length}
          </Badge>
        </div>
        <div className="flex items-center space-x-3">
          <Button
            variant="secondary"
            onClick={handleRefresh}
            className="flex items-center space-x-2"
          >
            <FiRefreshCw className="w-4 h-4" />
            <span>Refresh</span>
          </Button>
        </div>
      </div>

      {/* Connection Status */}
      {error && (
        <GlassCard className="p-4 border-red-500/30 bg-red-500/10">
          <div className="flex items-center space-x-2 text-red-400">
            <span>⚠️</span>
            <span>{error}</span>
            <span className="text-sm text-gray-400">(Showing fallback data)</span>
          </div>
        </GlassCard>
      )}

      {!currentUsername && (
        <GlassCard className="p-4 border-yellow-500/30 bg-yellow-500/10">
          <div className="text-yellow-300 text-sm">
            Session username is not set. Set localStorage key <span className="font-semibold">foxnest_username</span> to enable owner actions.
          </div>
        </GlassCard>
      )}

      {/* Success indicator when connected */}
      {!error && repositories.length > 0 && (
        <GlassCard className="p-4 border-green-500/30 bg-green-500/10">
          <div className="flex items-center space-x-2 text-green-400">
            <span>✅</span>
            <span>Connected to zanbeel Server - Showing live data</span>
          </div>
        </GlassCard>
      )}

      {/* Repository Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {displayRepositories.map((repo) => (
          <GlassCard
            key={repo.id}
            className={`p-6 cursor-pointer transition-all duration-300 hover:scale-105 ${
              selectedRepo?.id === repo.id
                ? 'border-orange-500/50 bg-orange-500/10'
                : 'hover:border-orange-500/30'
            }`}
            onClick={() => handleRepoClick(repo)}
          >
            <div className="space-y-4">
              {/* Header */}
              <div className="flex items-start justify-between">
                <div className="flex items-center space-x-2">
                  <FiFolder className="w-5 h-5 text-orange-400" />
                  <h3 className="text-lg font-semibold text-white truncate">
                    {repo.name}
                  </h3>
                </div>
                <FiArchive className="w-4 h-4 text-orange-400 flex-shrink-0" />
              </div>

              {/* Description */}
              <p className="text-gray-300 text-sm line-clamp-2">
                {repo.description}
              </p>

              {/* Language */}
              <div className="flex items-center space-x-2">
                <div
                  className="w-3 h-3 rounded-full"
                  style={{ backgroundColor: repo.languageColor }}
                />
                <span className="text-sm text-gray-400">{repo.language}</span>
              </div>

              {/* Stats */}
              <div className="flex items-center space-x-4 text-sm text-gray-400">
                <div className="flex items-center space-x-1">
                  <FiGitCommit className="w-4 h-4" />
                  <span>{repo.commits}</span>
                </div>
                <div className="flex items-center space-x-1">
                  <FiUsers className="w-4 h-4" />
                  <span>{repo.contributors || 1}</span>
                </div>
                <div className="flex items-center space-x-1">
                  <FiClock className="w-4 h-4" />
                  <span>{repo.lastUpdate}</span>
                </div>
              </div>

              {/* Archive Info */}
              <div className="pt-2 border-t border-gray-700">
                <div className="text-xs text-gray-500 space-y-1">
                  <div>Archived: {repo.archivedDate}</div>
                  <div>By: {repo.archivedBy}</div>
                  <div className="text-orange-400">{repo.reason}</div>
                  {repo.g1_coordinator && (
                    <div className="text-blue-400">Team Lead: {repo.g1_coordinator}</div>
                  )}
                  <div className="flex items-center space-x-2">
                    <span>Tested:</span>
                    <Badge 
                      variant={repo.tested ? "success" : "secondary"}
                      className={`text-xs ${repo.tested ? 'bg-green-600/20 text-green-400' : 'bg-gray-600/20 text-gray-400'}`}
                    >
                      {repo.tested ? 'Yes' : 'No'}
                    </Badge>
                  </div>
                  {repo.has_instruction_manual && (
                    <div className="text-purple-400">📄 Manual Available</div>
                  )}
                </div>
              </div>

              {/* Tags */}
              <div className="flex flex-wrap gap-1">
                {repo.tags?.map((tag, index) => (
                  <Badge
                    key={index}
                    variant="secondary"
                    className="text-xs bg-gray-700/50 text-gray-300"
                  >
                    {tag}
                  </Badge>
                ))}
              </div>

              {/* Actions */}
              {selectedRepo?.id === repo.id && (
                <div className="flex items-center space-x-2 pt-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={(e) => handleOpenEditor(e, repo)}
                    className="flex items-center space-x-1"
                    title="Open in Editor"
                  >
                    <FiCode className="w-3 h-3 text-blue-400" />
                    <span>Open Editor</span>
                  </Button>
                  {canWriteRepo(repo) && (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={(e) => {
                        e.stopPropagation()
                        handleEditRepo(repo)
                      }}
                      className="flex items-center space-x-1"
                    >
                      <FiEdit3 className="w-3 h-3" />
                      <span>Edit</span>
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={(e) => {
                      e.stopPropagation()
                      handleRestore(repo.id)
                    }}
                    className="flex items-center space-x-1"
                  >
                    <FiRefreshCw className="w-3 h-3" />
                    <span>Restore</span>
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={(e) => {
                      e.stopPropagation()
                      handleExport(repo.id)
                    }}
                    className="flex items-center space-x-1"
                  >
                    <FiDownload className="w-3 h-3" />
                    <span>Export</span>
                  </Button>
                  {canManageRepo(repo) ? (
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDelete(repo.id)
                      }}
                      className="flex items-center space-x-1 bg-red-600/20 text-red-400 hover:bg-red-600/30"
                    >
                      <FiTrash2 className="w-3 h-3" />
                      <span>Delete</span>
                    </Button>
                  ) : (
                    <div className="text-xs text-white/50 px-2 py-1">Read-only (not owner)</div>
                  )}
                </div>
              )}
            </div>
          </GlassCard>
        ))}
      </div>

      {/* Empty State */}
      {displayRepositories.length === 0 && !loading && (
        <div className="text-center py-12">
          <FiArchive className="w-16 h-16 text-gray-600 mx-auto mb-4" />
          <h3 className="text-xl font-semibold text-gray-400 mb-2">No Archived Repositories</h3>
          <p className="text-gray-500">
            Archived repositories will appear here when you archive projects that are no longer active.
          </p>
        </div>
      )}

      {/* Repository Details Modal/Panel */}
      {selectedRepo && (
        <GlassCard className="p-6 border-orange-500/30">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-white">{selectedRepo.name}</h2>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedRepo(null)}
                className="text-gray-400 hover:text-white"
              >
                ✕
              </Button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Repository Info */}
              <div className="space-y-3">
                <h3 className="text-lg font-semibold text-orange-400">Repository Details</h3>
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-400">Owner:</span>
                    <span className="text-white">{selectedRepo.owner}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Language:</span>
                    <span className="text-white">{selectedRepo.language}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Size:</span>
                    <span className="text-white">{selectedRepo.size}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Commits:</span>
                    <span className="text-white">{selectedRepo.commits}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Contributors:</span>
                    <span className="text-white">{selectedRepo.contributors}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Last Update:</span>
                    <span className="text-white">{selectedRepo.lastUpdate}</span>
                  </div>
                </div>
              </div>

              {/* Archive Info */}
              <div className="space-y-3">
                <h3 className="text-lg font-semibold text-orange-400">Archive Information</h3>
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-400">Archived Date:</span>
                    <span className="text-white">{selectedRepo.archivedDate}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Archived By:</span>
                    <span className="text-white">{selectedRepo.archivedBy}</span>
                  </div>
                  <div className="mt-3">
                    <span className="text-gray-400 block mb-1">Reason:</span>
                    <span className="text-orange-400 text-sm">{selectedRepo.reason}</span>
                  </div>
                </div>
              </div>
            </div>

            {/* G1 Coordinator and Testing Section */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* G1 Coordinator */}
              <div className="space-y-3">
                <h3 className="text-lg font-semibold text-blue-400">Team Lead</h3>
                {editingRepo === selectedRepo.id ? (
                  <div className="space-y-2">
                    <input
                      type="text"
                      value={editForm.g1_coordinator}
                      onChange={(e) => setEditForm({...editForm, g1_coordinator: e.target.value})}
                      placeholder="Enter Team Lead name"
                      className="w-full px-3 py-2 bg-gray-800/50 border border-gray-600 rounded-md text-white placeholder-gray-400 focus:outline-none focus:border-blue-500"
                    />
                    <div className="flex items-center space-x-2">
                      <Button
                        size="sm"
                        onClick={handleSaveEdit}
                        className="flex items-center space-x-1 bg-green-600/20 text-green-400 hover:bg-green-600/30"
                      >
                        <FiCheck className="w-3 h-3" />
                        <span>Save</span>
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={handleCancelEdit}
                        className="flex items-center space-x-1"
                      >
                        <FiX className="w-3 h-3" />
                        <span>Cancel</span>
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="flex justify-between">
                      <span className="text-gray-400">Name:</span>
                      <span className="text-white">{selectedRepo.g1_coordinator || 'Not assigned'}</span>
                    </div>
                    {!canWriteRepo(selectedRepo) && (
                      <div className="text-xs text-white/50">Only owner/admin can edit.</div>
                    )}
                  </div>
                )}
              </div>

              {/* Testing Status */}
              <div className="space-y-3">
                <h3 className="text-lg font-semibold text-green-400">Testing Status</h3>
                {editingRepo === selectedRepo.id ? (
                  <div className="space-y-2">
                    <label className="flex items-center space-x-2">
                      <input
                        type="checkbox"
                        checked={editForm.tested}
                        onChange={(e) => setEditForm({...editForm, tested: e.target.checked})}
                        className="rounded bg-gray-800 border-gray-600 text-green-600 focus:ring-green-500"
                      />
                      <span className="text-gray-300">Mark as tested</span>
                    </label>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="flex justify-between">
                      <span className="text-gray-400">Status:</span>
                      <Badge 
                        variant={selectedRepo.tested ? "success" : "secondary"}
                        className={`${selectedRepo.tested ? 'bg-green-600/20 text-green-400' : 'bg-gray-600/20 text-gray-400'}`}
                      >
                        {selectedRepo.tested ? 'Tested' : 'Not tested'}
                      </Badge>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Instruction Manual Section */}
            <div className="space-y-3">
              <h3 className="text-lg font-semibold text-purple-400">Instruction Manual</h3>
              <div className="space-y-2">
                {selectedRepo.has_instruction_manual ? (
                  <div className="flex items-center justify-between p-3 bg-gray-800/30 rounded-md border border-gray-600">
                    <div className="flex items-center space-x-2">
                      <FiFileText className="w-4 h-4 text-purple-400" />
                      <span className="text-gray-300">{selectedRepo.instruction_manual_filename}</span>
                    </div>
                    <div className="flex items-center space-x-2">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => handleDownloadManual(selectedRepo.id)}
                        className="flex items-center space-x-1"
                      >
                        <FiDownload className="w-3 h-3" />
                        <span>Download</span>
                      </Button>
                      {canWriteRepo(selectedRepo) && (
                      <label className="cursor-pointer">
                        <Button
                          size="sm"
                          variant="secondary"
                          as="span"
                          className="flex items-center space-x-1"
                          disabled={uploadingManual}
                        >
                          <FiUpload className="w-3 h-3" />
                          <span>{uploadingManual ? 'Uploading...' : 'Replace'}</span>
                        </Button>
                        <input
                          type="file"
                          accept=".pdf"
                          onChange={(e) => handleFileUpload(e, selectedRepo.id)}
                          className="hidden"
                          disabled={uploadingManual}
                        />
                      </label>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between p-3 bg-gray-800/30 rounded-md border border-gray-600 border-dashed">
                    <span className="text-gray-400">No instruction manual uploaded</span>
                    {canWriteRepo(selectedRepo) && (
                    <label className="cursor-pointer">
                      <Button
                        size="sm"
                        variant="primary"
                        as="span"
                        className="flex items-center space-x-1"
                        disabled={uploadingManual}
                      >
                        <FiUpload className="w-3 h-3" />
                        <span>{uploadingManual ? 'Uploading...' : 'Upload PDF'}</span>
                      </Button>
                      <input
                        type="file"
                        accept=".pdf"
                        onChange={(e) => handleFileUpload(e, selectedRepo.id)}
                        className="hidden"
                        disabled={uploadingManual}
                      />
                    </label>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Description */}
            <div className="space-y-2">
              <h3 className="text-lg font-semibold text-orange-400">Description</h3>
              <p className="text-gray-300">{selectedRepo.description}</p>
            </div>

            {/* Tags */}
            <div className="space-y-2">
              <h3 className="text-lg font-semibold text-orange-400">Tags</h3>
              <div className="flex flex-wrap gap-2">
                {selectedRepo.tags?.map((tag, index) => (
                  <Badge
                    key={index}
                    variant="secondary"
                    className="bg-gray-700/50 text-gray-300"
                  >
                    {tag}
                  </Badge>
                ))}
              </div>
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
                Delete Archived Repository
              </h3>
              <p className="text-white/70">
                Are you sure you want to permanently delete this archived repository?
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
                <span className="flex items-center">
                  <FiArchive className="w-3 h-3 mr-1" />
                  Archived
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
                variant="danger"
                className="flex-1 bg-red-600/20 text-red-400 hover:bg-red-600/30"
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
    </div>
  )
}

export default Archive