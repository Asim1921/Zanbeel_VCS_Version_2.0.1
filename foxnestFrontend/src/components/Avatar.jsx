import React, { useState } from 'react'
import api from '../utils/api'
import { cn } from '../lib/utils'

/**
 * A user's picture, or their initials.
 *
 * The image is fetched from the API like everything else, so it can 404 for a user
 * who has not set one — the initials are the normal case, not an error state, and
 * falling back silently is what keeps a missing avatar from looking broken.
 */

const SIZES = {
  sm: 'h-8 w-8 text-xs',
  md: 'h-12 w-12 text-sm',
  lg: 'h-20 w-20 text-xl',
  xl: 'h-28 w-28 text-3xl',
}

export default function Avatar({ username, fullName, hasAvatar = true, size = 'md', className }) {
  const [failed, setFailed] = useState(false)
  const label = (fullName || username || '?').trim()
  const initials = label
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join('') || '?'

  const shared = cn(
    'shrink-0 rounded-full object-cover ring-1 ring-border',
    SIZES[size] || SIZES.md,
    className
  )

  if (!username || !hasAvatar || failed) {
    return (
      <div
        className={cn(shared, 'flex items-center justify-center bg-accent font-semibold text-[#04070e]')}
        aria-label={label}
      >
        {initials}
      </div>
    )
  }

  return (
    <img
      src={api.avatarUrl(username)}
      alt={label}
      className={shared}
      onError={() => setFailed(true)}
    />
  )
}
