/**
 * Lane assignment for a commit graph — the `git log --graph` rails.
 *
 * The input is the server's ordering, which guarantees a commit appears before its
 * parents. That guarantee is what makes a single pass possible: by the time a commit
 * is reached, every child that wanted to reach it has already run and reserved a lane
 * for it, so the commit's column is simply the lane holding its name.
 *
 * Lanes are never compacted. Reusing a freed column would make rails jump sideways for
 * reasons that have nothing to do with the history, and a graph that moves under you is
 * harder to read than one that is a little wider than it needs to be.
 */

/** A lane is "reserved" for the commit id it is waiting to draw, or null when free. */
function claimLane(lanes, commitId) {
  const existing = lanes.indexOf(commitId)
  if (existing !== -1) return existing
  const free = lanes.indexOf(null)
  if (free !== -1) {
    lanes[free] = commitId
    return free
  }
  lanes.push(commitId)
  return lanes.length - 1
}

/**
 * Turn an ordered commit list into rows carrying a lane and the links leaving them.
 *
 * Each row's `links` describe what is drawn between it and the row below: `{ from, to }`
 * lane indices. A link where from === to is a straight rail; anything else is a curve,
 * which is exactly where a branch left or rejoined.
 */
export function assignLanes(commits) {
  const rows = []
  const lanes = []
  const present = new Set((commits || []).map((c) => c.id))
  let maxLane = 0

  ;(commits || []).forEach((commit, index) => {
    // The lanes as they stood when this row began. A rail only enters this row if its
    // lane was already occupied, which is what distinguishes a branch passing through
    // from a lane this row is about to open.
    const incoming = lanes.slice()

    // Every lane already waiting for this commit converges here. The leftmost becomes
    // the commit's own lane; the rest are merges arriving from the right.
    const waiting = []
    lanes.forEach((value, laneIndex) => {
      if (value === commit.id) waiting.push(laneIndex)
    })

    let lane
    // Lanes arriving from above that end at this commit. Their rails reach the top of
    // this row and have to curve into the node, or a branch appears to stop in mid-air
    // instead of visibly rejoining.
    let converging = []
    if (waiting.length) {
      lane = waiting[0]
      converging = waiting.slice(1)
      // The others have delivered their child and are done.
      converging.forEach((laneIndex) => { lanes[laneIndex] = null })
    } else {
      // No child reached this commit: it is a tip, so it opens a lane of its own.
      lane = claimLane(lanes, commit.id)
    }

    // Reserve lanes for the parents. The first parent continues this commit's lane,
    // which is what keeps a branch's mainline visually straight.
    const parents = commit.parents || []
    const parentLanes = []
    if (parents.length === 0) {
      lanes[lane] = null
    } else {
      parents.forEach((parentId, position) => {
        const known = present.has(parentId)
        if (position === 0) {
          // Keep the column even when the parent was cut by the limit, so the rail
          // runs off the bottom rather than stopping as though history ended.
          lanes[lane] = known || commit.truncated_parents?.includes(parentId)
            ? parentId
            : null
          parentLanes.push(lane)
        } else {
          const already = lanes.indexOf(parentId)
          parentLanes.push(already !== -1 ? already : claimLane(lanes, parentId))
        }
      })
    }

    // Links leaving this row: this commit down to each parent's lane, plus every other
    // occupied lane carrying straight through.
    const links = []
    parentLanes.forEach((target) => links.push({ from: lane, to: target }))

    // Rails already running when this row began keep running -- including through a
    // lane that this row's merge also targets. A lane can be carrying one branch down
    // the page and receive a merge's second parent in the same row; suppressing the
    // pass-through there made the branch appear to stop at the merge it flowed past.
    incoming.forEach((value, laneIndex) => {
      if (value == null) return
      if (laneIndex === lane) return
      if (converging.includes(laneIndex)) return
      if (lanes[laneIndex] == null) return
      links.push({ from: laneIndex, to: laneIndex })
    })

    maxLane = Math.max(maxLane, lane, ...lanes.map((v, i) => (v == null ? 0 : i)))
    rows.push({ commit, lane, links, converging, row: index })
  })

  // A lane that ends at a commit was drawn as a straight pass-through by the row
  // above, because that row could not yet know which column its target would land in.
  // Redirect those links now that every commit has a lane, so a rejoining branch
  // visibly curves into the commit it rejoins instead of stopping above it.
  rows.forEach((current, index) => {
    if (!current.converging.length || index === 0) return
    const above = rows[index - 1]
    current.converging.forEach((laneIndex) => {
      const link = above.links.find((l) => l.from === laneIndex && l.to === laneIndex)
      if (link) link.to = current.lane
      else above.links.push({ from: laneIndex, to: current.lane })
    })
  })

  return { rows, laneCount: maxLane + 1 }
}

/**
 * A stable colour index per lane.
 *
 * Tied to the lane rather than the branch because a lane is what the eye actually
 * follows down the page; branches come and go within one.
 */
export function laneColour(lane) {
  return lane % 8
}

/** Branch and tag chips for a commit, in the order they should be shown. */
export function refChips(commit) {
  const chips = []
  ;(commit.branch_heads || []).forEach((head) => {
    chips.push({ kind: head.is_default ? 'default' : 'branch', label: head.name })
  })
  ;(commit.tags || []).forEach((tag) => chips.push({ kind: 'tag', label: tag }))
  return chips
}
