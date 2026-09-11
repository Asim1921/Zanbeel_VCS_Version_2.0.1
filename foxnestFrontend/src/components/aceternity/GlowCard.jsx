import React, { useRef, useState } from 'react'
import { cn } from '../../lib/utils'

/**
 * A card whose border lights up where the cursor is — the Aceternity
 * "border-beam" idea, done with two stacked radial gradients so it costs one
 * repaint rather than an animation loop.
 *
 * Use it for things the user is meant to act on. Ordinary content should use
 * Card, so that a lit edge keeps meaning "this one".
 */
export default function GlowCard({ children, className, intensity = 0.55, ...props }) {
  const ref = useRef(null)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const [lit, setLit] = useState(false)

  const track = (event) => {
    if (!ref.current) return
    const rect = ref.current.getBoundingClientRect()
    setPos({ x: event.clientX - rect.left, y: event.clientY - rect.top })
  }

  return (
    <div
      ref={ref}
      onMouseMove={track}
      onMouseEnter={() => setLit(true)}
      onMouseLeave={() => setLit(false)}
      className={cn(
        'group relative rounded-[var(--radius-card)] p-px transition-transform duration-300',
        'hover:-translate-y-0.5',
        className
      )}
      {...props}
    >
      {/* the lit border lives in the 1px padding ring */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-[var(--radius-card)] transition-opacity duration-300"
        style={{
          opacity: lit ? 1 : 0,
          background: `radial-gradient(16rem circle at ${pos.x}px ${pos.y}px, rgba(59,157,255,${intensity}), transparent 42%)`,
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-[var(--radius-card)] border border-border"
      />
      <div className="relative h-full rounded-[calc(var(--radius-card)-1px)] bg-surface bg-gradient-to-b from-white/[0.04] to-white/[0.01]">
        {children}
      </div>
    </div>
  )
}
