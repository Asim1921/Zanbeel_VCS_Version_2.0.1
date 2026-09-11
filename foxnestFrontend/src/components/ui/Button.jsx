import React from 'react'
import { cn } from '../../lib/utils'

/**
 * On a dark ground a filled button reads as a block of colour, so the primary
 * action is lit rather than filled: a glowing ring plus a soft halo, which is
 * what the reference uses for its one call to action. Everything else stays
 * quiet so a screen never has two things shouting.
 */
const variants = {
  primary:
    'bg-accent text-[#04070e] font-semibold hover:brightness-110 ' +
    'shadow-[0_0_0_1px_rgba(59,157,255,0.5),0_6px_22px_-6px_rgba(59,157,255,0.75)] ' +
    'hover:shadow-[0_0_0_1px_rgba(59,157,255,0.7),0_8px_30px_-6px_rgba(59,157,255,0.95)] ' +
    'focus-visible:ring-accent/50',

  // The outlined pill from the reference: hairline border, glass fill, glow on hover.
  outline:
    'text-ink border border-border-strong bg-white/[0.03] backdrop-blur-sm ' +
    'hover:border-accent/60 hover:bg-accent/[0.08] hover:text-white ' +
    'hover:shadow-[0_0_24px_-8px_rgba(59,157,255,0.8)] focus-visible:ring-accent/40',

  secondary:
    'bg-white/[0.05] text-ink border border-border hover:bg-white/[0.09] ' +
    'hover:border-border-strong focus-visible:ring-ink/20',

  ghost:
    'text-ink-soft hover:text-ink hover:bg-white/[0.06] focus-visible:ring-ink/15',

  danger:
    'bg-danger-fg/12 text-danger-fg border border-danger-fg/25 hover:bg-danger-fg/20 ' +
    'hover:border-danger-fg/45 focus-visible:ring-danger-fg/35',

  success:
    'bg-success-fg/12 text-success-fg border border-success-fg/25 hover:bg-success-fg/20 ' +
    'hover:border-success-fg/45 focus-visible:ring-success-fg/35',

  brand:
    'bg-accent text-[#04070e] font-semibold hover:brightness-110 ' +
    'shadow-[0_0_0_1px_rgba(59,157,255,0.5),0_6px_22px_-6px_rgba(59,157,255,0.75)] ' +
    'focus-visible:ring-accent/50',
}

const sizes = {
  sm: 'px-3.5 py-1.5 text-xs',
  md: 'px-5 py-2 text-sm',
  lg: 'px-6 py-2.5 text-sm',
}

export default function Button({
  children,
  variant = 'primary',
  size = 'md',
  className = '',
  disabled = false,
  loading = false,
  ...props
}) {
  return (
    <button
      className={cn(
        'relative inline-flex items-center justify-center gap-2 rounded-full font-medium',
        'transition-all duration-200 ease-out active:scale-[0.98]',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-cream',
        variants[variant] || variants.primary,
        sizes[size] || sizes.md,
        (disabled || loading) && 'pointer-events-none opacity-45',
        className
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading && (
        <span
          className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
          aria-hidden
        />
      )}
      {children}
    </button>
  )
}
