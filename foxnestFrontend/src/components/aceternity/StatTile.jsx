import React from 'react'
import { cn } from '../../lib/utils'

/**
 * One figure, its label, and optionally a trend or sparkline.
 *
 * The number is set in the display face at a size that lets it be read across
 * the room, because on a dashboard the figure is the content and the label is
 * the caption — not the other way round.
 */
export default function StatTile({
  label,
  value,
  hint,
  icon: Icon,
  tone = 'default',
  series,
  className,
}) {
  const tones = {
    default: 'text-ink',
    accent: 'text-accent',
    success: 'text-success-fg',
    warning: 'text-warning-fg',
    danger: 'text-danger-fg',
  }

  return (
    <div
      className={cn(
        // No overflow-hidden here on purpose. It would clip to the *rounded*
        // border box, and callers that supply their own padding (Dashboard nests
        // this in a BentoGridItem and passes p-0) leave the label sitting in the
        // corner, where the radius shaves the first letter off. Nothing in this
        // component overflows its box, so the clip only ever did harm.
        'group relative rounded-[var(--radius-card)] border border-border',
        'bg-surface bg-gradient-to-b from-white/[0.035] to-white/[0.01] p-5',
        'transition-all duration-300 hover:border-border-strong',
        className
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[11px] uppercase leading-none tracking-[0.11em] text-muted">
          {label}
        </p>
        {Icon && (
          <Icon
            className={cn(
              'h-4 w-4 shrink-0 opacity-60 transition-opacity group-hover:opacity-100',
              tones[tone] || tones.default
            )}
          />
        )}
      </div>

      <p
        className={cn(
          'mt-3 font-display text-[2rem] font-light leading-none tracking-tight tabular-nums',
          tones[tone] || tones.default
        )}
      >
        {value}
      </p>

      {hint && <p className="mt-2 text-xs text-muted">{hint}</p>}

      {Array.isArray(series) && series.length > 1 && <Sparkline series={series} tone={tone} />}
    </div>
  )
}

/** A bare trend line — no axes, because at this size they would be noise. */
function Sparkline({ series, tone }) {
  const width = 120
  const height = 28
  const max = Math.max(...series)
  const min = Math.min(...series)
  const span = max - min || 1
  const step = width / (series.length - 1)

  const points = series.map((value, i) => {
    const x = i * step
    const y = height - ((value - min) / span) * (height - 4) - 2
    return [x, y]
  })

  const path = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const area = `${path} L${width},${height} L0,${height} Z`
  const stroke =
    tone === 'success' ? '#2ad19a' : tone === 'danger' ? '#ff6070' : tone === 'warning' ? '#f5b445' : '#3b9dff'
  const [lastX, lastY] = points[points.length - 1]

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="mt-3 h-7 w-full"
      preserveAspectRatio="none"
      aria-hidden
    >
      <path d={area} fill={stroke} fillOpacity="0.1" />
      <path d={path} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx={lastX} cy={lastY} r="2.4" fill={stroke} />
    </svg>
  )
}
