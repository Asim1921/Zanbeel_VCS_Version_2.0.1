import { API_BASE_URL } from '../config'
import { getSessionToken } from './session'

export async function authedFetch(absoluteUrl, { responseType = 'blob' } = {}) {
  const token = getSessionToken()
  if (!token) throw new Error('Authentication required')
  const res = await fetch(absoluteUrl, { headers: { Authorization: `Bearer ${token}` } })
  if (!res.ok) {
    let detail = ''
    try {
      const j = await res.json()
      detail = j?.detail
        ? ` - ${typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)}`
        : ''
    } catch {
      // ignore
    }
    throw new Error(`HTTP ${res.status}${detail}`)
  }
  if (responseType === 'text') return await res.text()
  return await res.blob()
}

export async function downloadAuthed(absoluteUrl, filename = 'download') {
  const blob = await authedFetch(absoluteUrl, { responseType: 'blob' })
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
}

export async function openAuthedInNewTab(absoluteUrl) {
  const blob = await authedFetch(absoluteUrl, { responseType: 'blob' })
  const objectUrl = URL.createObjectURL(blob)
  window.open(objectUrl, '_blank', 'noopener,noreferrer')
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
}

export function buildFileDownloadUrl(repoId, filePath, branchName = null) {
  const params = new URLSearchParams({ path: filePath })
  if (branchName) params.set('branch', branchName)
  return `${API_BASE_URL}/repository/${repoId}/file/download?${params.toString()}`
}
