import React from 'react'
import { cn } from '../../lib/utils'

export default function Input({
  className = '',
  label,
  hint,
  error,
  id,
  icon: Icon,
  ...props
}) {
  const inputId = id || props.name
  return (
    <label className="block space-y-1.5">
      {label && <span className="text-[13px] font-medium text-ink-soft">{label}</span>}
      <span className="relative block">
        {Icon && (
          <Icon className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
        )}
        <input
          id={inputId}
          className={cn(
            'w-full rounded-xl border border-border bg-white/[0.04] px-3.5 py-2.5 text-sm text-ink',
            'placeholder:text-muted outline-none transition-all duration-200',
            'hover:border-border-strong',
            'focus:border-accent/70 focus:bg-white/[0.06] focus:ring-4 focus:ring-accent/10',
            Icon && 'pl-10',
            error && 'border-danger-fg/50 focus:border-danger-fg focus:ring-danger-fg/15',
            className
          )}
          {...props}
        />
      </span>
      {(hint || error) && (
        <span className={cn('text-xs', error ? 'text-danger-fg' : 'text-muted')}>
          {error || hint}
        </span>
      )}
    </label>
  )
}
