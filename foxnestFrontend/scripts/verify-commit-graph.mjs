/**
 * Invariant checks for the commit graph lane assignment.
 *
 * Plain node, no test runner: the project has no JS test setup, and adding one to
 * cover a single pure function would be a larger decision than the function deserves.
 *
 *   node scripts/verify-commit-graph.mjs
 *
 * The cases below are the shapes that actually broke it. In particular `passThroughMerge`
 * is the one real history had and a hand-written fixture did not: a lane carrying one
 * branch down the page while the same row's merge targets that lane. Suppressing the
 * pass-through there made a branch appear to stop at a merge it merely flowed past.
 */

import { assignLanes } from '../src/lib/commitGraph.js'

let failures = 0

function check(name, ok, detail = '') {
  if (ok) {
    console.log(`  PASS  ${name}`)
  } else {
    failures += 1
    console.log(`  FAIL  ${name}${detail ? `  ${detail}` : ''}`)
  }
}

/** Every rail leaving a row must land on a lane the next row uses or carries. */
function danglingRails(rows) {
  const bad = []
  rows.forEach((row, index) => {
    const next = rows[index + 1]
    if (!next) return
    const valid = new Set([next.lane, ...next.links.map((l) => l.from)])
    row.links.forEach((link) => {
      if (!valid.has(link.to)) bad.push(`${row.commit.id}: ${link.from}->${link.to}`)
    })
  })
  return bad
}

/** Every parent present in the graph must be reachable by a rail from its child. */
function unreachableParents(rows) {
  const byId = Object.fromEntries(rows.map((r) => [r.commit.id, r]))
  const bad = []
  rows.forEach((row) => {
    ;(row.commit.parents || []).forEach((parentId) => {
      const parent = byId[parentId]
      if (!parent) return
      const reachable = row.links.some((l) => l.to === parent.lane)
        || rows.slice(row.row, parent.row).some((r) =>
          r.links.some((l) => l.to === parent.lane))
      if (!reachable) bad.push(`${row.commit.id} -> ${parentId}`)
    })
  })
  return bad
}

const CASES = {
  linear: [
    { id: 'c3', parents: ['c2'] },
    { id: 'c2', parents: ['c1'] },
    { id: 'c1', parents: [] },
  ],

  branchAndMerge: [
    { id: 'm1', parents: ['c2', 'f2'] },
    { id: 'f2', parents: ['f1'] },
    { id: 'c2', parents: ['c1'] },
    { id: 'f1', parents: ['c1'] },
    { id: 'c1', parents: [] },
  ],

  // A third branch still open, which must keep its own lane to the bottom.
  openSideBranch: [
    { id: 'm1', parents: ['c2', 'f2'] },
    { id: 't1', parents: ['f1'] },
    { id: 'f2', parents: ['f1'] },
    { id: 'c2', parents: ['c1'] },
    { id: 'f1', parents: ['c1'] },
    { id: 'c1', parents: [] },
  ],

  // The regression from real history: at row `mergeB`, lane 1 is carrying `sideTip`'s
  // rail down to `shared`, and the merge also targets lane 1 for its second parent.
  passThroughMerge: [
    { id: 'mergeA', parents: ['mainB', 'sideTip'] },
    { id: 'sideTip', parents: ['shared'] },
    { id: 'mainB', parents: ['mainA', 'shared'] },
    { id: 'shared', parents: ['root'] },
    { id: 'mainA', parents: ['root'] },
    { id: 'root', parents: [] },
  ],

  // Two roots that never meet: both lanes run to the bottom independently.
  unrelatedHistories: [
    { id: 'a2', parents: ['a1'] },
    { id: 'b2', parents: ['b1'] },
    { id: 'a1', parents: [] },
    { id: 'b1', parents: [] },
  ],

  // A parent cut by the limit: the lane must stay open so the rail runs off the bottom.
  truncated: [
    { id: 'x2', parents: ['x1'], truncated_parents: ['x1'] },
  ],
}

console.log('commit graph lane assignment\n')

for (const [name, commits] of Object.entries(CASES)) {
  const { rows, laneCount } = assignLanes(commits)
  check(`${name}: every commit gets a row`, rows.length === commits.length,
    `${rows.length} of ${commits.length}`)
  check(`${name}: no dangling rails`, danglingRails(rows).length === 0,
    danglingRails(rows).join(', '))
  check(`${name}: every parent is reachable`, unreachableParents(rows).length === 0,
    unreachableParents(rows).join(', '))
  check(`${name}: lane count is positive`, laneCount >= 1, String(laneCount))
}

// Shape assertions that a generic invariant would not catch.
{
  const { rows, laneCount } = assignLanes(CASES.linear)
  check('linear history uses a single lane', laneCount === 1, String(laneCount))
  check('linear history keeps every commit in lane 0',
    rows.every((r) => r.lane === 0), JSON.stringify(rows.map((r) => r.lane)))
}

{
  const { rows } = assignLanes(CASES.branchAndMerge)
  const merge = rows.find((r) => r.commit.id === 'm1')
  check('a merge emits a rail to each parent lane', merge.links.length >= 2,
    JSON.stringify(merge.links))
  const lanes = new Set(merge.links.map((l) => l.to))
  check('a merge forks into two different lanes', lanes.size === 2,
    JSON.stringify([...lanes]))
}

{
  const { rows } = assignLanes(CASES.passThroughMerge)
  const mergeB = rows.find((r) => r.commit.id === 'mainB')
  const sideLane = rows.find((r) => r.commit.id === 'sideTip').lane
  check('a lane passing through a merge keeps its rail',
    mergeB.links.some((l) => l.from === sideLane && l.to === sideLane),
    JSON.stringify(mergeB.links))
}

{
  const { rows } = assignLanes(CASES.unrelatedHistories)
  check('unrelated histories do not share a lane',
    rows.find((r) => r.commit.id === 'a1').lane
      !== rows.find((r) => r.commit.id === 'b1').lane,
    JSON.stringify(rows.map((r) => [r.commit.id, r.lane])))
}

console.log()
if (failures) {
  console.log(`${failures} check(s) failed`)
  process.exit(1)
}
console.log('all checks passed')
