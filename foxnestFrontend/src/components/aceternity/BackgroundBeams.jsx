import React from 'react'
import { cn } from '../../lib/utils'

/**
 * Thin light beams raking across the ground, plus the blue wash that gives the
 * page its depth. Deliberately low contrast: this sits behind content and must
 * never compete with it.
 */
export default function BackgroundBeams({ className }) {
  return (
    <div
      className={cn('pointer-events-none absolute inset-0 overflow-hidden', className)}
      aria-hidden
    >
      <div className="animate-beam-drift absolute -top-24 left-1/4 h-[420px] w-px rotate-12 bg-gradient-to-b from-transparent via-accent/35 to-transparent" />
      <div className="animate-beam-drift absolute right-1/3 top-10 h-[520px] w-px -rotate-6 bg-gradient-to-b from-transparent via-glow/25 to-transparent [animation-delay:2s]" />
      <div className="animate-beam-drift absolute bottom-0 left-1/2 h-[380px] w-px rotate-[18deg] bg-gradient-to-b from-transparent via-accent/25 to-transparent [animation-delay:4s]" />
      <div className="animate-beam-drift absolute -top-10 right-1/4 h-[300px] w-px rotate-[-14deg] bg-gradient-to-b from-transparent via-glow/20 to-transparent [animation-delay:6s]" />

      <div className="absolute inset-0 bg-[radial-gradient(ellipse_60%_45%_at_50%_0%,rgba(59,157,255,0.20),transparent_65%)]" />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_82%_18%,rgba(111,211,255,0.10),transparent_45%)]" />
      <div className="absolute inset-x-0 bottom-0 h-56 bg-gradient-to-t from-cream to-transparent" />
    </div>
  )
}
