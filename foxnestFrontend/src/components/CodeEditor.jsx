import React, { useState, useEffect } from 'react'
import { FiFolder, FiFile, FiX, FiChevronRight, FiChevronDown, FiCode, FiDownload } from 'react-icons/fi'
import GlassCard from './ui/GlassCard'
import Button from './ui/Button'
import { API_BASE_URL } from '../config'
import { downloadAuthed, buildFileDownloadUrl } from '../utils/authedDownload'
import { getSessionToken } from '../utils/session'

const CodeEditor = ({ repoId, repoName, onClose }) => {
  const [files, setFiles] = useState({})
  const [folders, setFolders] = useState([])
  const [selectedFile, setSelectedFile] = useState(null)
  const [expandedFolders, setExpandedFolders] = useState(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [loadingFile, setLoadingFile] = useState(null)

  const authedFetch = async (url, options = {}) => {
    const token = getSessionToken()
    if (!token) {
      throw new Error('Authentication required — please sign in again.')
    }
    const response = await fetch(url, {
      ...options,
      headers: {
        ...(options.headers || {}),
        Authorization: `Bearer ${token}`,
      },
    })
    return response
  }

  useEffect(() => {
    fetchRepositoryFiles()
  }, [repoId])

  const parseJsonResponse = async (response) => {
    const responseText = await response.text()
    if (!response.ok) {
      const statusLine = `HTTP ${response.status}`
      const detail = responseText ? ` - ${responseText}` : ''
      throw new Error(`${statusLine}${detail}`)
    }

    if (!responseText) {
      throw new Error('Empty response from server')
    }

    try {
      return JSON.parse(responseText)
    } catch (parseError) {
      throw new Error('Invalid JSON response from server')
    }
  }

  const fetchRepositoryFiles = async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await authedFetch(`${API_BASE_URL}/repository/${repoId}/files?include_content=false`)
      const data = await parseJsonResponse(response)

      if (data.success) {
        setFiles(data.files || {})
        setFolders(data.folders || [])
        
        // Auto-expand root folders
        const rootFolders = (data.folders || []).filter(f => !f.includes('/') || f.split('/').length === 1)
        setExpandedFolders(new Set(rootFolders))
        
        // Auto-select first file if available
        const fileNames = Object.keys(data.files || {})
        if (fileNames.length > 0) {
          handleSelectFile(fileNames[0])
        }
      } else {
        setError('Failed to load repository files')
      }
    } catch (err) {
      setError(`Error: ${err.message}`)
      console.error('Error fetching repository files:', err)
    } finally {
      setLoading(false)
    }
  }

  const fetchFileContent = async (filePath) => {
    if (!filePath) return null

    const existing = files[filePath]
    if (existing && existing.content !== undefined) {
      return existing
    }

    try {
      setLoadingFile(filePath)
      const response = await authedFetch(
        `${API_BASE_URL}/repository/${repoId}/file?path=${encodeURIComponent(filePath)}`
      )
      const data = await parseJsonResponse(response)

      if (data.success && data.file) {
        const updated = {
          ...existing,
          ...data.file,
        }
        setFiles(prev => ({
          ...prev,
          [filePath]: updated,
        }))
        return updated
      }

      throw new Error('Failed to load file content')
    } catch (err) {
      setError(`Error: ${err.message}`)
      console.error('Error fetching file content:', err)
      return null
    } finally {
      setLoadingFile(null)
    }
  }

  const handleSelectFile = (filePath) => {
    setSelectedFile(filePath)
    const fileName = filePath.split('/').pop()
    if (!isTextFile(fileName)) {
      setFiles(prev => ({
        ...prev,
        [filePath]: {
          ...prev[filePath],
          is_binary: true,
        }
      }))
      return
    }
    fetchFileContent(filePath)
  }

  const toggleFolder = (folderPath) => {
    const newExpanded = new Set(expandedFolders)
    if (newExpanded.has(folderPath)) {
      newExpanded.delete(folderPath)
    } else {
      newExpanded.add(folderPath)
    }
    setExpandedFolders(newExpanded)
  }

  const getFileIcon = (filename) => {
    const ext = filename.split('.').pop().toLowerCase()
    const iconMap = {
      'js': '📄',
      'jsx': '⚛️',
      'ts': '📘',
      'tsx': '⚛️',
      'py': '🐍',
      'json': '📋',
      'md': '📝',
      'html': '🌐',
      'css': '🎨',
      'txt': '📄',
      'jpg': '🖼️',
      'png': '🖼️',
      'gif': '🖼️',
      'pdf': '📕',
    }
    return iconMap[ext] || '📄'
  }

  const isTextFile = (filename) => {
    const ext = filename.split('.').pop().toLowerCase()
    const textExtensions = new Set([
      'js', 'jsx', 'ts', 'tsx', 'py', 'json', 'md', 'html', 'css', 'txt', 'csv', 'yml', 'yaml'
    ])
    return textExtensions.has(ext)
  }

  const buildFileTree = () => {
    const tree = {}
    const fileNames = Object.keys(files)

    // Build tree structure
    fileNames.forEach(filePath => {
      const parts = filePath.split('/')
      let current = tree

      parts.forEach((part, index) => {
        if (index === parts.length - 1) {
          // It's a file
          if (!current._files) current._files = []
          current._files.push(filePath)
        } else {
          // It's a folder
          if (!current[part]) current[part] = {}
          current = current[part]
        }
      })
    })

    return tree
  }

  const renderTreeNode = (node, path = '', level = 0) => {
    const entries = []

    // Render folders first
    Object.keys(node).forEach(key => {
      if (key === '_files') return // Skip the files array

      const folderPath = path ? `${path}/${key}` : key
      const isExpanded = expandedFolders.has(folderPath)
      const hasChildren = Object.keys(node[key]).length > 0

      entries.push(
        <div key={folderPath}>
          <div
            className="flex items-center px-2 py-1.5 hover:bg-cream-mid cursor-pointer rounded group"
            style={{ paddingLeft: `${level * 12 + 8}px` }}
            onClick={() => toggleFolder(folderPath)}
          >
            {isExpanded ? (
              <FiChevronDown className="w-4 h-4 text-muted mr-1" />
            ) : (
              <FiChevronRight className="w-4 h-4 text-muted mr-1" />
            )}
            <FiFolder className={`w-4 h-4 mr-2 ${isExpanded ? 'text-info-fg' : 'text-info-fg'}`} />
            <span className="text-sm text-ink-soft group-hover:text-ink">
              {key}
            </span>
          </div>
          {isExpanded && hasChildren && (
            <div>
              {renderTreeNode(node[key], folderPath, level + 1)}
            </div>
          )}
        </div>
      )
    })

    // Render files
    if (node._files) {
      node._files.forEach(filePath => {
        const fileName = filePath.split('/').pop()
        const isSelected = selectedFile === filePath

        entries.push(
          <div
            key={filePath}
            className={`flex items-center px-2 py-1.5 cursor-pointer rounded group transition-colors ${
              isSelected 
                ? 'bg-info-fg/20 text-ink' 
                : 'hover:bg-cream-mid text-ink-soft hover:text-ink'
            }`}
            style={{ paddingLeft: `${level * 12 + 32}px` }}
            onClick={() => handleSelectFile(filePath)}
          >
            <span className="mr-2">{getFileIcon(fileName)}</span>
            <span className="text-sm">{fileName}</span>
          </div>
        )
      })
    }

    return entries
  }

  const downloadFile = async () => {
    if (!selectedFile || !files[selectedFile]) return
    const downloadUrl = buildFileDownloadUrl(repoId, selectedFile)
    const filename = selectedFile.split('/').pop() || 'download'
    try {
      await downloadAuthed(downloadUrl, filename)
    } catch (err) {
      setError(err.message || 'Authentication required')
    }
  }

  const getLanguageFromFilename = (filename) => {
    const ext = filename.split('.').pop().toLowerCase()
    const langMap = {
      'js': 'javascript',
      'jsx': 'javascript',
      'ts': 'typescript',
      'tsx': 'typescript',
      'py': 'python',
      'json': 'json',
      'md': 'markdown',
      'html': 'html',
      'css': 'css',
      'txt': 'plaintext',
    }
    return langMap[ext] || 'plaintext'
  }

  if (loading) {
    return (
      <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm z-50 flex items-center justify-center">
        <GlassCard className="p-8">
          <div className="flex items-center space-x-3">
            <FiCode className="w-6 h-6 animate-pulse text-info-fg" />
            <span className="text-ink">Loading repository files...</span>
          </div>
        </GlassCard>
      </div>
    )
  }

  const tree = buildFileTree()

  return (
    <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="w-full h-full max-w-7xl max-h-[90vh] bg-surface rounded-lg overflow-hidden shadow-2xl border border-border">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 bg-cream-mid border-b border-border">
          <div className="flex items-center space-x-3">
            <FiCode className="w-5 h-5 text-info-fg" />
            <div>
              <h2 className="text-lg font-semibold text-ink">{repoName}</h2>
              <p className="text-xs text-muted">Repository File Explorer</p>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            {selectedFile && (
              <Button
                size="sm"
                variant="secondary"
                onClick={downloadFile}
                className="flex items-center space-x-1"
              >
                <FiDownload className="w-4 h-4" />
                <span>Download</span>
              </Button>
            )}
            <button
              onClick={onClose}
              className="p-2 hover:bg-cream-mid rounded-lg transition-colors text-ink-soft hover:text-ink"
            >
              <FiX className="w-5 h-5" />
            </button>
          </div>
        </div>

        {error ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <p className="text-danger-fg mb-2">⚠️ {error}</p>
              <Button onClick={fetchRepositoryFiles}>Retry</Button>
            </div>
          </div>
        ) : Object.keys(files).length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <FiFolder className="w-16 h-16 text-muted mx-auto mb-4" />
              <p className="text-muted">This repository is empty</p>
            </div>
          </div>
        ) : (
          <div className="flex h-[calc(100%-60px)]">
            {/* File Tree Sidebar */}
            <div className="w-64 bg-cream-mid border-r border-border overflow-y-auto">
              <div className="p-3">
                <div className="text-xs font-semibold text-muted mb-2 uppercase tracking-wider">
                  Explorer
                </div>
                <div className="space-y-0.5">
                  {renderTreeNode(tree)}
                </div>
              </div>
            </div>

            {/* Code Viewer */}
            <div className="flex-1 flex flex-col bg-surface">
              {selectedFile ? (
                <>
                  {/* File Header */}
                  <div className="px-4 py-2 bg-cream-mid border-b border-border flex items-center justify-between">
                    <div className="flex items-center space-x-2">
                      <span>{getFileIcon(selectedFile)}</span>
                      <span className="text-sm text-ink">{selectedFile}</span>
                    </div>
                    <div className="flex items-center space-x-4 text-xs text-muted">
                      {files[selectedFile]?.size && (
                        <span>{(files[selectedFile].size / 1024).toFixed(2)} KB</span>
                      )}
                      <span>{getLanguageFromFilename(selectedFile)}</span>
                    </div>
                  </div>

                  {/* File Content */}
                  <div className="flex-1 overflow-auto p-4 bg-cream">
                    {loadingFile === selectedFile || (files[selectedFile]?.content === undefined && files[selectedFile]?.is_binary !== true) ? (
                      <div className="flex items-center justify-center h-full">
                        <div className="text-center">
                          <FiCode className="w-6 h-6 animate-pulse text-info-fg mx-auto mb-3" />
                          <p className="text-muted">Loading file...</p>
                        </div>
                      </div>
                    ) : files[selectedFile]?.is_binary ? (
                      <div className="flex items-center justify-center h-full">
                        <div className="text-center">
                          <FiFile className="w-16 h-16 text-muted mx-auto mb-4" />
                          <p className="text-muted mb-2">Binary file cannot be displayed</p>
                          <Button size="sm" onClick={downloadFile}>
                            <FiDownload className="w-4 h-4 mr-2" />
                            Download File
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <pre className="text-sm text-ink font-mono whitespace-pre-wrap">
                        <code>{files[selectedFile]?.content}</code>
                      </pre>
                    )}
                  </div>
                </>
              ) : (
                <div className="flex items-center justify-center h-full">
                  <div className="text-center">
                    <FiFile className="w-16 h-16 text-muted mx-auto mb-4" />
                    <p className="text-muted">Select a file to view its contents</p>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default CodeEditor