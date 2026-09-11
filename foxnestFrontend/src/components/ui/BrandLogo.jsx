import React from 'react'
import { cn } from '../../lib/utils'

/**
 * Zanbeel brand mark — yellow circular logo with Z / circuit motif.
 * Uses the official asset from /zanbeel-logo.png
 */
export default function BrandLogo({
  size = 36,
  className,
  imgClassName,
  withWordmark = false,
  wordmarkClassName,
  subtitle,
}) {
  return (
    <div className={cn('flex items-center gap-3', className)}>
      <img
        src="/zanbeel-logo.png"
        alt="Zanbeel"
        width={size}
        height={size}
        className={cn(
          'shrink-0 rounded-full object-cover ring-1 ring-white/10 shadow-[0_4px_16px_-4px_rgba(0,0,0,0.8)]',
          imgClassName
        )}
        style={{ width: size, height: size }}
        draggable={false}
      />
      {withWordmark && (
        <div className="min-w-0">
          <div
            className={cn(
              'truncate text-[15px] font-semibold tracking-tight text-ink',
              wordmarkClassName
            )}
          >
            Zanbeel
          </div>
          {subtitle && <div className="truncate text-[11px] text-muted">{subtitle}</div>}
        </div>
      )}
    </div>
  )
}
