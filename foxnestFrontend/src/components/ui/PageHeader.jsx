import React from 'react'
import { cn } from '../../lib/utils'
import BlurText from '../react-bits/BlurText'

export default function PageHeader({
  title,
  subtitle,
  actions,
  eyebrow,
  className,
  animateTitle = true,
}) {
  return (
    <div
      className={cn(
        'mb-8 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between',
        className
      )}
    >
      <div className="min-w-0">
        {eyebrow && (
          <p className="mb-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent">
            {eyebrow}
          </p>
        )}
        <h1 className="font-display text-[1.9rem] font-light tracking-tight text-ink md:text-[2.25rem]">
          {animateTitle && typeof title === 'string' ? <BlurText text={title} /> : title}
        </h1>
        {subtitle && (
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted md:text-[15px]">
            {subtitle}
          </p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
