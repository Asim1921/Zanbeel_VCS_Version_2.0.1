/**
 * Every page the app can render must also be a tab it is allowed to open.
 *
 *   node scripts/verify-navigation.mjs
 *
 * App.jsx holds the same knowledge twice: a switch that renders a page for a tab, and
 * an allowedTabs whitelist that bounces anything unrecognised back to the dashboard.
 * Adding a page to the switch and forgetting the whitelist produces the most confusing
 * failure the app can have — the page exists, the sidebar links to it, clicking it
 * silently returns you to the dashboard, and nothing errors. That is exactly what
 * happened to Profile and Notifications.
 *
 * This compares the two lists so the next page cannot ship half-reachable.
 */

import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'src', 'App.jsx'), 'utf8')

let failures = 0
const fail = (message) => { failures += 1; console.log(`  FAIL  ${message}`) }
const pass = (message) => console.log(`  PASS  ${message}`)

/** Tab ids the render switch knows how to draw. */
const rendered = new Set(
  [...source.matchAll(/case\s+'([a-z0-9-]+)'\s*:/gi)].map((m) => m[1])
)

/** Tab ids the guard will permit, from both the common and admin lists. */
const listed = new Set()
for (const name of ['commonTabs', 'adminTabs']) {
  const block = source.match(new RegExp(`const ${name}\\s*=\\s*\\[([^\\]]*)\\]`, 's'))
  if (!block) {
    fail(`${name} not found in App.jsx — has the guard been restructured?`)
    continue
  }
  for (const m of block[1].matchAll(/'([a-z0-9-]+)'/gi)) listed.add(m[1])
}

console.log('navigation: rendered pages vs allowed tabs\n')

if (rendered.size === 0) fail('no case labels found — the switch could not be parsed')
else pass(`${rendered.size} page(s) in the render switch`)

if (listed.size === 0) fail('no tabs found in the allow lists')
else pass(`${listed.size} tab(s) in the allow lists`)

const unreachable = [...rendered].filter((tab) => !listed.has(tab))
if (unreachable.length) {
  fail(`these pages render but are not allowed, so they bounce to the dashboard: ${unreachable.join(', ')}`)
} else {
  pass('every rendered page is an allowed tab')
}

// The reverse is only a warning: a tab may legitimately be allowed before its page
// exists, and the default case renders the dashboard for it.
const undrawn = [...listed].filter((tab) => !rendered.has(tab))
if (undrawn.length) {
  console.log(`  note  allowed but not in the switch (falls through to the default): ${undrawn.join(', ')}`)
}

// The pages this work added, named explicitly: a generic check passes vacuously if
// the parse silently finds nothing.
for (const tab of ['profile', 'notifications']) {
  if (rendered.has(tab) && listed.has(tab)) pass(`'${tab}' is both rendered and allowed`)
  else fail(`'${tab}' is not reachable (rendered: ${rendered.has(tab)}, allowed: ${listed.has(tab)})`)
}

console.log()
if (failures) {
  console.log(`${failures} check(s) failed`)
  process.exit(1)
}
console.log('all checks passed')
