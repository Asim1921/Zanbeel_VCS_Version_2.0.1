import React from 'react'
import { cn } from '../../lib/utils'

export function BentoGrid({ children, className }) {
  return (
    <div
      className={cn(
        'grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4',
        className
      )}
    >
      {children}
    </div>
  )
}

export function BentoGridItem({ children, className, colSpan }) {
  return (
    <div
      className={cn(
        'rounded-[var(--radius-card)] border border-border p-5',
        'bg-surface bg-gradient-to-b from-white/[0.035] to-white/[0.01]',
        'shadow-[0_1px_0_rgba(255,255,255,0.03)_inset,0_8px_24px_rgba(0,0,0,0.5)]',
        'transition-all duration-300 hover:border-border-strong',
        colSpan === 2 && 'md:col-span-2',
        colSpan === 3 && 'lg:col-span-3',
        className
      )}
    >
      {children}
    </div>
  )
}
