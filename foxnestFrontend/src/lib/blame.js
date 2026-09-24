/**
 * Reading a blame payload: who wrote what, and in what proportion.
 *
 * Separate from the renderer so the arithmetic can be reused (and tested)
 * without pulling in a component.
 */

/** Stable, readable colour per author — the same name always gets the same hue. */
const AUTHOR_CLASSES = [
  'text-accent', 'text-success-fg', 'text-info-fg',
  'text-warning-fg', 'text-danger-fg', 'text-ink-soft',
]

export function authorClass(name) {
  if (!name) return 'text-muted'
  let hash = 0
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  return AUTHOR_CLASSES[hash % AUTHOR_CLASSES.length]
}

/** Per-author line share for a blame payload, largest contributor first. */
export function blameSummary(blame) {
  const lines = blame?.lines || []
  const meta = blame?.commits || {}
  const tally = new Map()
  for (const line of lines) {
    const author = meta[line.commit_id]?.author || 'unknown'
    tally.set(author, (tally.get(author) || 0) + 1)
  }
  const total = lines.length || 1
  return [...tally.entries()]
    .map(([author, count]) => ({ author, count, percent: Math.round((count / total) * 100) }))
    .sort((a, b) => b.count - a.count)
}
