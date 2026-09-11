import React from 'react'
import { cn } from '../../lib/utils'

/**
 * State is carried by a coloured dot as well as the tint, so it still reads when
 * skimmed quickly or by someone who does not separate these hues easily.
 */
const variants = {
  default: 'bg-white/[0.06] text-ink-soft border-border',
  success: 'bg-success-fg/10 text-success-fg border-success-fg/25',
  warning: 'bg-warning-fg/10 text-warning-fg border-warning-fg/25',
  danger: 'bg-danger-fg/10 text-danger-fg border-danger-fg/25',
  info: 'bg-info-fg/10 text-info-fg border-info-fg/25',
  accent: 'bg-accent/12 text-accent border-accent/30',
}

const dots = {
  success: 'bg-success-fg',
  warning: 'bg-warning-fg',
  danger: 'bg-danger-fg',
  info: 'bg-info-fg',
  accent: 'bg-accent',
}

export default function Badge({ children, variant = 'default', dot, className = '' }) {
  const showDot = dot ?? variant !== 'default'
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1',
        'whitespace-nowrap text-[11px] font-medium tracking-wide',
        variants[variant] || variants.default,
        className
      )}
    >
      {showDot && dots[variant] && (
        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dots[variant])} aria-hidden />
      )}
      {children}
    </span>
  )
}
