import React, { useMemo } from 'react'
import { FiGitCommit, FiGitMerge, FiTag, FiGitBranch } from 'react-icons/fi'
import { assignLanes, refChips } from '../lib/commitGraph'
import { cn } from '../lib/utils'

/**
 * The commit graph, drawn as rails.
 *
 * One SVG spans the whole column rather than one per row, because a rail between two
 * commits is a single curve crossing a row boundary; drawing it in halves leaves a
 * visible seam wherever a branch diverges. That means the row height here and the row
 * height in the list beside it have to be the same number, so it is one constant.
 */

const ROW_HEIGHT = 46
const LANE_WIDTH = 16
const LEFT_PAD = 12
const DOT_RADIUS = 4.5

// Eight hues that stay distinguishable on the dark ground and do not collide with the
// accent used for interactive elements.
const LANE_COLOURS = [
  '#3b9dff', '#4ade80', '#f472b6', '#fbbf24',
  '#a78bfa', '#2dd4bf', '#fb7185', '#94a3b8',
]

const colourFor = (lane) => LANE_COLOURS[lane % LANE_COLOURS.length]

const centreX = (lane) => LEFT_PAD + lane * LANE_WIDTH
const centreY = (row) => row * ROW_HEIGHT + ROW_HEIGHT / 2

/** A vertical rail, or an S-curve where a branch leaves or rejoins. */
function railPath(fromLane, toLane, fromRow) {
  const x1 = centreX(fromLane)
  const y1 = centreY(fromRow)
  const x2 = centreX(toLane)
  const y2 = centreY(fromRow + 1)
  if (fromLane === toLane) return `M ${x1} ${y1} L ${x2} ${y2}`
  const midY = (y1 + y2) / 2
  return `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`
}

function RefChip({ chip }) {
  const tone =
    chip.kind === 'tag'
      ? 'border-warning-fg/40 bg-warning-fg/10 text-warning-fg'
      : chip.kind === 'default'
        ? 'border-accent/50 bg-accent/15 text-ink'
        : 'border-info-fg/40 bg-info-fg/10 text-info-fg'
  const Icon = chip.kind === 'tag' ? FiTag : FiGitBranch
  return (
    <span className={cn(
      'inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]',
      tone
    )}>
      <Icon className="h-2.5 w-2.5" />
      {chip.label}
    </span>
  )
}

export default function CommitGraph({ commits, onSelectCommit, selectedCommitId }) {
  const { rows, laneCount } = useMemo(() => assignLanes(commits || []), [commits])

  if (!rows.length) {
    return <p className="py-12 text-center text-sm text-muted">No commits to graph.</p>
  }

  const graphWidth = LEFT_PAD * 2 + Math.max(0, laneCount - 1) * LANE_WIDTH
  const totalHeight = rows.length * ROW_HEIGHT

  return (
    <div className="relative overflow-x-auto">
      <div className="relative" style={{ minHeight: totalHeight }}>
        <svg
          className="pointer-events-none absolute left-0 top-0"
          width={graphWidth}
          height={totalHeight}
          aria-hidden
        >
          {rows.map((entry) =>
            entry.links.map((link, i) => (
              <path
                key={`${entry.row}-${i}`}
                d={railPath(link.from, link.to, entry.row)}
                fill="none"
                stroke={colourFor(link.to)}
                strokeWidth={1.6}
                strokeLinecap="round"
              />
            ))
          )}

          {rows.map((entry) => {
            const cx = centreX(entry.lane)
            const cy = centreY(entry.row)
            const colour = colourFor(entry.lane)
            const selected = entry.commit.id === selectedCommitId
            return entry.commit.is_merge ? (
              // A merge reads as a ring, so the place two lines join is identifiable
              // without following either of them.
              <circle
                key={entry.commit.id}
                cx={cx} cy={cy} r={DOT_RADIUS + 1}
                fill="#0b1020" stroke={colour}
                strokeWidth={selected ? 3 : 2}
              />
            ) : (
              <circle
                key={entry.commit.id}
                cx={cx} cy={cy} r={selected ? DOT_RADIUS + 1.5 : DOT_RADIUS}
                fill={colour}
                stroke={selected ? '#ffffff' : 'transparent'}
                strokeWidth={selected ? 1.5 : 0}
              />
            )
          })}
        </svg>

        <ol style={{ marginLeft: graphWidth + 8 }}>
          {rows.map((entry) => {
            const commit = entry.commit
            const chips = refChips(commit)
            const selected = commit.id === selectedCommitId
            return (
              <li
                key={commit.id}
                style={{ height: ROW_HEIGHT }}
                className={cn(
                  'flex items-center gap-2 rounded-lg px-2 transition',
                  selected ? 'bg-white/[0.07]' : 'hover:bg-white/[0.04]',
                  onSelectCommit && 'cursor-pointer'
                )}
                onClick={onSelectCommit ? () => onSelectCommit(commit) : undefined}
              >
                {commit.is_merge
                  ? <FiGitMerge className="h-3.5 w-3.5 shrink-0 text-muted" />
                  : <FiGitCommit className="h-3.5 w-3.5 shrink-0 text-muted" />}
                <span className="shrink-0 font-mono text-[11px] text-accent">
                  {commit.short_id}
                </span>
                {chips.map((chip) => <RefChip key={chip.kind + chip.label} chip={chip} />)}
                <span className="truncate text-[13px] text-ink">{commit.message}</span>
                <span className="ml-auto shrink-0 text-[11px] text-muted">
                  {commit.author}
                </span>
                <span className="hidden shrink-0 font-mono text-[10.5px] text-muted sm:inline">
                  {(commit.timestamp || '').slice(0, 10)}
                </span>
              </li>
            )
          })}
        </ol>
      </div>
    </div>
  )
}
