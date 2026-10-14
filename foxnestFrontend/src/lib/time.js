/**
 * Server times are naive UTC ("2026-10-13T09:30:00"). `new Date()` alone reads such a
 * string as local time, which put every timestamp off by the reader's UTC offset.
 */

export function parseServerTime(value) {
  if (!value) return null
  if (typeof value !== 'string') return new Date(value)
  const normalized = value.replace(' ', 'T')
  const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(normalized)
  const parsed = new Date(hasZone ? normalized : `${normalized}Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function absoluteTime(value) {
  const when = parseServerTime(value)
  if (!when) return ''
  return when.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function relativeTime(value) {
  const when = parseServerTime(value)
  if (!when) return ''
  const seconds = Math.round((Date.now() - when.getTime()) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return when.toLocaleDateString()
}
