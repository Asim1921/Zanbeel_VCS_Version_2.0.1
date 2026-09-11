import React from 'react'
import { cn } from '../../lib/utils'

/**
 * The atmosphere behind the whole app: a faint grid lit by two soft blue
 * sources, masked so it fades out before it reaches the content. Fixed rather
 * than absolute, so the light stays put while long pages scroll under it.
 */
export default function DotBackground({ className, fade = true }) {
  return (
    <div className={cn('pointer-events-none fixed inset-0 overflow-hidden', className)} aria-hidden>
      {/* grid */}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage:
            'linear-gradient(to right, rgba(255,255,255,0.03) 1px, transparent 1px),' +
            'linear-gradient(to bottom, rgba(255,255,255,0.03) 1px, transparent 1px)',
          backgroundSize: '56px 56px',
          maskImage: fade
            ? 'radial-gradient(ellipse 90% 60% at 50% 0%, #000 35%, transparent 100%)'
            : undefined,
          WebkitMaskImage: fade
            ? 'radial-gradient(ellipse 90% 60% at 50% 0%, #000 35%, transparent 100%)'
            : undefined,
        }}
      />
      {/* the two lights */}
      <div
        className="absolute inset-0"
        style={{
          background:
            'radial-gradient(56rem 32rem at 15% -10%, rgba(59,157,255,0.17), transparent 60%),' +
            'radial-gradient(44rem 28rem at 88% 2%, rgba(111,211,255,0.10), transparent 62%)',
        }}
      />
    </div>
  )
}
