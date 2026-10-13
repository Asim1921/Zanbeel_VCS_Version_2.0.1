/**
 * Drive search, the activity log and notifications in a real browser.
 *
 *   Run through the orchestrator, which starts the backend and this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_discovery_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Phases (E2E_PHASE):
 *   lead  open a deep link while signed out and land on it after signing in; open a code
 *         match at its line; Back to the results; reload a file link; commit and file
 *         results; "/" to search from anywhere; filter the activity log and open a row;
 *         a repository's activity tab; open a notification; the search index panel
 *   dev   search shows only what dev can read; the activity page is there for a
 *         developer and scoped; a notification opens the approved commit; a link to a
 *         repository dev cannot read says so
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_PHASE, SCREENSHOT_DIR,
 * E2E_BILLING, E2E_PAYROLL (repository ids), optional E2E_BROWSER and E2E_HEADED=1.
 */

import { mkdirSync } from 'fs'
import { join } from 'path'

let chromium
try {
  ({ chromium } = await import('playwright-core'))
} catch {
  console.log('playwright-core is not installed: npm i --no-save playwright-core')
  process.exit(2)
}

const env = (name) => {
  const value = process.env[name]
  if (!value) {
    console.log(`missing ${name}`)
    process.exit(2)
  }
  return value
}

const FRONTEND = env('FRONTEND_URL')
const USER = env('E2E_USER')
const PASSWORD = env('E2E_PASSWORD')
const PHASE = env('E2E_PHASE')
const SHOTS = env('SCREENSHOT_DIR')
const BILLING = env('E2E_BILLING')
const PAYROLL = env('E2E_PAYROLL')

mkdirSync(SHOTS, { recursive: true })

let failures = 0
const check = (name, ok, detail = '') => {
  if (ok) console.log(`  PASS  ${name}`)
  else {
    failures += 1
    console.log(`  FAIL  ${name}${detail ? `\n        ${String(detail).slice(0, 400)}` : ''}`)
  }
  return ok
}

const browser = await chromium.launch({
  channel: process.env.E2E_BROWSER || 'chrome',
  headless: process.env.E2E_HEADED !== '1',
})
const page = await (await browser.newContext({ viewport: { width: 1400, height: 950 } })).newPage()

const consoleErrors = []
page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()) })
page.on('pageerror', (err) => consoleErrors.push(String(err)))
page.on('dialog', (d) => d.accept())

let step = 0
const shot = async (label) => {
  step += 1
  await page.screenshot({ path: join(SHOTS, `${PHASE}-${String(step).padStart(2, '0')}-${label}.png`) })
}

const visible = async (locator, timeout = 15000) => {
  try {
    await locator.first().waitFor({ state: 'visible', timeout })
    return true
  } catch {
    return false
  }
}

const url = () => new URL(page.url())
const nav = (name) => page.getByRole('button', { name, exact: true }).first().click()

async function signIn() {
  await page.getByPlaceholder('Enter username').fill(USER)
  await page.getByPlaceholder('Enter password').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
}

// --- lead ----------------------------------------------------------------------------

async function phaseLead() {
  console.log('\nUI  a deep link survives signing in')
  await page.goto(`${FRONTEND}/search?type=code&q=parse_invoice`, { waitUntil: 'domcontentloaded' })
  await signIn()
  const results = page.getByTestId('code-result')
  check('signing in lands on the search the link asked for', await visible(results, 30000))
  check('...which covers both repositories', await visible(results.filter({ hasText: 'billing' })) &&
    await visible(results.filter({ hasText: 'payroll' })))
  check('the search box holds the query', (await page.getByLabel('Search for').inputValue()) === 'parse_invoice')
  await shot('search-results')

  console.log('\nUI  a result opens what it found')
  await results.filter({ hasText: 'billing' }).getByTestId('code-match').first().click()
  const marked = page.locator('tr[data-highlighted="true"]')
  check('a code match opens the file with its line highlighted', await visible(marked, 30000))
  check('...at the line the match was on', (await marked.first().getAttribute('id')) === 'L1')
  check('...and the address says which file and line',
    url().pathname === `/repositories/${BILLING}` && url().searchParams.get('path') === 'src/invoice.py' &&
    url().searchParams.get('line') === '1', page.url())
  await shot('file-at-line')

  await page.reload({ waitUntil: 'domcontentloaded' })
  check('reloading keeps the file and the line', await visible(page.locator('tr[data-highlighted="true"]'), 30000))

  await page.goBack()
  check('Back returns to the search results', await visible(page.getByTestId('code-result'), 30000) &&
    url().pathname === '/search')

  await page.getByRole('tab', { name: 'Commits' }).click()
  await page.getByLabel('Search for').fill('100%')
  await page.getByLabel('Search for').press('Enter')
  const commit = page.getByTestId('commit-result')
  check('commit search reads "100%" literally', await visible(commit.filter({ hasText: 'Bill at 100% rate' })))
  await commit.first().click()
  check('a commit result opens the commit', await visible(page.getByText('Bill at 100% rate').first(), 30000) &&
    Boolean(url().searchParams.get('commit')))
  await shot('commit-opened')

  await page.goBack()
  await page.getByRole('tab', { name: 'Files' }).click()
  await page.getByLabel('Search for').fill('invoice')
  await page.getByLabel('Search for').press('Enter')
  const file = page.getByTestId('file-result').filter({ hasText: 'src/invoice.py' })
  check('go to file finds the path', await visible(file))
  await file.first().click()
  check('...and opens it', await visible(page.getByText('def parse_invoice').first(), 30000))

  console.log('\nUI  search from anywhere')
  await nav('Dashboard')
  await page.keyboard.press('/')
  const quick = page.getByLabel('Search code')
  check('"/" focuses the header search', await quick.evaluate((el) => el === document.activeElement))
  await quick.fill('RoundedCents')
  await quick.press('Enter')
  check('Enter searches the code', await visible(page.getByTestId('code-result').filter({ hasText: 'billing' }), 30000))

  console.log('\nUI  the activity log')
  await nav('Activity')
  const count = page.getByTestId('activity-count')
  check('the activity page shows a total from the server', await visible(count.filter({ hasText: /\d+ events?/ }), 30000))
  await page.getByLabel('Filter by event type').selectOption('approve_commit')
  check('filtering by type narrows it, on the server', await visible(count.filter({ hasText: /^1 event$/ }), 15000),
    await count.innerText().catch(() => ''))
  check('...and the filter is in the address', url().searchParams.get('type') === 'approve_commit')
  const row = page.getByTestId('activity-row').first()
  await row.getByRole('button').first().click()
  check('a row shows how the person was signed in', await visible(row.getByText(/password sign-in/)))
  await shot('activity-filtered')
  await row.getByTestId('activity-open').click()
  check('Open goes to the commit the row is about', await visible(page.getByText('Developer fix').first(), 30000) &&
    Boolean(url().searchParams.get('commit')))
  await page.goBack()
  check('Back returns to the filtered log', url().searchParams.get('type') === 'approve_commit' &&
    await visible(count.filter({ hasText: /^1 event$/ })))

  console.log('\nUI  a repository\'s own activity')
  await page.goto(`${FRONTEND}/repositories/${BILLING}?tab=activity`, { waitUntil: 'domcontentloaded' })
  check('the repository page has an activity tab, reachable by link',
    await visible(page.getByTestId('activity-browser'), 30000) &&
    await visible(page.getByTestId('activity-row').filter({ hasText: 'Approved' })))

  console.log('\nUI  notifications go somewhere')
  await nav('Notifications')
  const open = page.locator('li').filter({ hasText: 'mentioned you' }).getByTestId('notification-open').first()
  check('a mention has an Open button', await visible(open, 30000))
  await open.click()
  check('...which opens the pull request', await visible(page.getByTestId('pr-checks').or(page.getByText('Rounding from dev')), 30000) &&
    /\/repositories\/[^/]+\/pulls\/\d+/.test(url().pathname), page.url())
  await shot('notification-opened-pr')

  console.log('\nUI  the search index')
  await nav('Operations')
  await page.getByRole('button', { name: 'Search index', exact: true }).click()
  check('administrators see the index, up to date', await visible(page.getByTestId('search-index-panel').getByText('Up to date'), 30000))
  await shot('search-index')
}

// --- dev -----------------------------------------------------------------------------

async function phaseDev() {
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' })
  await signIn()
  check(`signs in as ${USER}`, await visible(page.getByRole('button', { name: 'Settings', exact: true }), 30000))

  console.log('\nUI  dev sees only what dev can read')
  await nav('Search')
  await page.getByLabel('Search for').fill('parse_invoice')
  await page.getByLabel('Search for').press('Enter')
  const results = page.getByTestId('code-result')
  check('code search finds the billing match', await visible(results.filter({ hasText: 'billing' }), 30000))
  check('...and nothing from payroll', (await results.filter({ hasText: 'payroll' }).count()) === 0)

  await nav('Activity')
  check('developers have the activity page now', await visible(page.getByTestId('activity-row'), 30000))
  check('...without payroll in it', !(await page.getByTestId('activity-browser').innerText()).includes('payroll'))

  await nav('Notifications')
  const approved = page.locator('li').filter({ hasText: 'Your commit was approved' })
  check('dev was told the queued commit was approved', await visible(approved, 30000))
  await approved.getByTestId('notification-open').click()
  check('...and Open shows that commit', await visible(page.getByText('Developer fix').first(), 30000))
  await shot('dev-approved-commit')

  await page.goto(`${FRONTEND}/repositories/${PAYROLL}`, { waitUntil: 'domcontentloaded' })
  check('a link to a repository dev cannot read says so', await visible(page.getByText(/Cannot open this page/), 30000))
}

try {
  if (PHASE === 'lead') await phaseLead()
  else await phaseDev()
  const expected = /favicon|DevTools|Failed to load resource|not allowed|not found|status of 40[34]/i
  const relevant = consoleErrors.filter((e) => !expected.test(e))
  check('no errors in the browser console', relevant.length === 0, relevant.join('\n'))
} catch (err) {
  await shot('failure').catch(() => {})
  check('the UI run completed without an unexpected error', false, err?.stack || err)
} finally {
  await browser.close()
}

process.exit(failures ? 1 : 0)
