import React from 'react'
import Spotlight from '../aceternity/Spotlight'
import { cn } from '../../lib/utils'

export default function SpotlightCard({
  children,
  className,
  hover = true,
  ...props
}) {
  return (
    <Spotlight
      className={cn(
        'rounded-[var(--radius-card)] border border-border bg-surface',
        'shadow-[0_1px_0_rgba(255,255,255,0.03)_inset,0_8px_24px_rgba(0,0,0,0.5)]',
        hover &&
          'transition-all duration-300 hover:-translate-y-0.5 hover:shadow-[0_1px_0_rgba(255,255,255,0.05)_inset,0_18px_44px_rgba(0,0,0,0.6)]',
        className
      )}
      spotlightColor="rgba(17, 17, 17, 0.05)"
      {...props}
    >
      <div className="relative z-10 h-full">{children}</div>
    </Spotlight>
  )
}
