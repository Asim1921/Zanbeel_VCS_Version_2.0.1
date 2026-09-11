import React from 'react'
import { cn } from '../../lib/utils'
import Card from './Card'

export default function EmptyState({ icon: Icon, title, description, action, className }) {
  return (
    <Card
      hover={false}
      className={cn('flex flex-col items-center justify-center px-6 py-16 text-center', className)}
    >
      {Icon && (
        <div className="relative mb-5">
          <div
            className="animate-glow-pulse absolute inset-0 rounded-2xl bg-accent/25 blur-xl"
            aria-hidden
          />
          <div className="relative flex h-14 w-14 items-center justify-center rounded-2xl border border-border-strong bg-white/[0.04] text-accent">
            <Icon className="h-6 w-6" />
          </div>
        </div>
      )}
      <h3 className="font-display text-xl font-medium tracking-tight text-ink">{title}</h3>
      {description && (
        <p className="mt-2 max-w-md text-sm leading-relaxed text-muted">{description}</p>
      )}
      {action && <div className="mt-6">{action}</div>}
    </Card>
  )
}
