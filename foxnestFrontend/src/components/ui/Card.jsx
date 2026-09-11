import React, { useRef, useState } from 'react'
import { cn } from '../../lib/utils'

/**
 * Panels on a near-black ground cannot rely on drop shadow — a black shadow on
 * black is invisible. Separation comes from a hairline border, a very slight
 * top-lit gradient, and (optionally) a cursor-tracked spotlight, which is the
 * Aceternity idea that actually earns its keep here: it tells you which card
 * you are on without adding a border colour that competes with state.
 */
export default function Card({
  children,
  className = '',
  hover = true,
  spotlight = false,
  ...props
}) {
  const ref = useRef(null)
  const [pos, setPos] = useState({ x: -1000, y: -1000 })
  const [lit, setLit] = useState(false)

  const track = (event) => {
    if (!spotlight || !ref.current) return
    const rect = ref.current.getBoundingClientRect()
    setPos({ x: event.clientX - rect.left, y: event.clientY - rect.top })
  }

  return (
    <div
      ref={ref}
      onMouseMove={track}
      onMouseEnter={() => spotlight && setLit(true)}
      onMouseLeave={() => spotlight && setLit(false)}
      className={cn(
        'relative overflow-hidden rounded-[var(--radius-card)] border border-border text-ink',
        'bg-surface bg-gradient-to-b from-white/[0.035] to-white/[0.012]',
        'shadow-[0_1px_0_rgba(255,255,255,0.03)_inset,0_8px_24px_rgba(0,0,0,0.5)]',
        hover &&
          'transition-all duration-300 hover:-translate-y-0.5 hover:border-border-strong ' +
            'hover:shadow-[0_1px_0_rgba(255,255,255,0.05)_inset,0_18px_44px_rgba(0,0,0,0.6)]',
        className
      )}
      {...props}
    >
      {spotlight && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 transition-opacity duration-300"
          style={{
            opacity: lit ? 1 : 0,
            background: `radial-gradient(18rem circle at ${pos.x}px ${pos.y}px, rgba(59,157,255,0.10), transparent 45%)`,
          }}
        />
      )}
      {/* display:contents so children participate in the Card's own layout.
          With a plain wrapper div, a caller passing `flex` to className styled
          the wrapper instead of the children, and everything silently stacked
          vertically. The spotlight overlay above is positioned against the Card
          root and precedes the children in DOM order, so it still paints
          underneath them without needing this element as a layer. */}
      <div className="contents">{children}</div>
    </div>
  )
}
