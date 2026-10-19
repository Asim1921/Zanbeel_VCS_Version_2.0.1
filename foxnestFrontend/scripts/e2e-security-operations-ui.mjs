/**
 * Drive sign-in limits, backups, restore drills and schema health in a real browser.
 *
 *   Run through the orchestrator, which starts the backend and this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_security_operations_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Phases (E2E_PHASE):
 *   signin  the login form, throttled: the wait is said in words, and the right password
 *           is refused too until it passes
 *   admin   Operations: a locked account and a throttled address are shown and cleared;
 *           a backup is taken as a job and followed to "verified" with its restore drill;
 *           a drill and a re-verification from their buttons; the reports; the schema
 *           revision and the upgrade that built it
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_PHASE, SCREENSHOT_DIR,
 * optional E2E_BROWSER and E2E_HEADED=1.
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
const context = await browser.newContext({ viewport: { width: 1400, height: 950 } })
const page = await context.newPage()

const consoleErrors = []
page.on('pageerror', (err) => consoleErrors.push(String(err)))
page.on('dialog', (d) => d.accept())

let step = 0
const shot = async (label) => {
  step += 1
  await page.screenshot({ path: join(SHOTS, `${PHASE}-${String(step).padStart(2, '0')}-${label}.png`), fullPage: true })
}

const visible = async (locator, timeout = 15000) => {
  try {
    await locator.first().waitFor({ state: 'visible', timeout })
    return true
  } catch {
    return false
  }
}

const gone = async (locator, timeout = 15000) => {
  try {
    await locator.first().waitFor({ state: 'detached', timeout })
    return true
  } catch {
    return false
  }
}

async function signIn(user = USER, password = PASSWORD) {
  await page.getByPlaceholder('Enter username').fill(user)
  await page.getByPlaceholder('Enter password').fill(password)
  await page.getByRole('button', { name: /sign in/i }).click()
}

const tab = (name) => page.getByRole('button', { name, exact: true })

// --- signed out ------------------------------------------------------------------------

async function signinPhase() {
  console.log('\nUI  the login form, throttled')
  await page.goto(`${FRONTEND}/`, { waitUntil: 'domcontentloaded' })
  await visible(page.getByPlaceholder('Enter username'), 30000)
  let text = ''
  for (let i = 0; i < 6; i += 1) {
    await signIn(USER, `wrong-password-${i}`)
    await page.waitForTimeout(700)
    text = await page.locator('body').innerText()
  }
  check('six wrong passwords: the form says how long to wait', /Try again in \d+ (seconds|minutes)/.test(text),
    text.slice(0, 300))
  await shot('throttled')
  await signIn(USER, PASSWORD)
  await page.waitForTimeout(900)
  text = await page.locator('body').innerText()
  check('...and the right password is refused too until it passes', /Try again in/.test(text) &&
    (await page.getByPlaceholder('Enter password').count()) === 1, text.slice(0, 300))
}

// --- admin -------------------------------------------------------------------------------

async function adminPhase() {
  console.log('\nUI  Operations: sign-in security')
  await page.goto(`${FRONTEND}/operations`, { waitUntil: 'domcontentloaded' })
  await signIn()
  check('a link to Operations lands there after signing in',
    await visible(page.getByRole('heading', { name: 'Operations' }), 30000))
  check('the locked account is listed, with its lock', await visible(page.getByText(/Locked · \d+ min left/)))
  check('...and the throttled address', await visible(page.getByText('Address throttled')))
  check('the policy is stated', await visible(page.getByText(/lock the account/)))
  await shot('sign-in-security')

  await page.getByRole('button', { name: 'Unlock' }).first().click()
  check('Unlock clears the lock', await visible(page.getByText(/The lock on lead cleared/)))
  check('...and the row goes', await gone(page.getByText(/Locked · \d+ min left/)))
  await page.getByRole('button', { name: 'Clear address' }).first().click()
  check('Clear address clears the throttle', await visible(page.getByText(/The limit on .* cleared/)))
  await shot('cleared')

  console.log('\nUI  Operations: backups')
  await tab('Backups').click()
  check('with no backup yet, the page says so', await visible(page.getByText('There is no verified backup of this server.')))
  check('...and that nothing takes backups on a schedule', await visible(page.getByText(/Nothing takes backups on a schedule/)))
  await shot('no-backups')
  await page.getByRole('button', { name: 'Back up now' }).click()
  check('Back up now starts a job', await visible(page.getByRole('button', { name: 'A job is running…' }), 10000))
  check('...followed to a verified backup', await visible(page.getByText('Verified', { exact: true }), 120000))
  check('...and its restore drill passing', await visible(page.getByText('Restore drill passed'), 120000))
  check('the result is announced', await visible(page.getByText(/Backup verified — .* restore drill passed/), 30000))
  check('the stale warning is gone', await gone(page.getByText('There is no verified backup of this server.')))
  await shot('backed-up')

  await page.getByRole('button', { name: 'Test restore' }).first().click()
  check('Test restore runs a drill from its button',
    await visible(page.getByText(/Restore drill passed: .* restored into a scratch directory/), 120000))
  await page.getByRole('button', { name: 'Re-verify' }).first().click()
  check('Re-verify reads every blob back', await visible(page.getByText(/Verified: every blob in/), 120000))
  await page.getByRole('button', { name: 'Reports' }).first().click()
  check('the reports open', await visible(page.getByText(/branch heads checked: \d+/)))
  check('...with no problems', await visible(page.getByText('No problems found.')))
  await shot('reports')

  console.log('\nUI  Operations: schema')
  await tab('Schema').click()
  check('the schema is at its revision and matches the models',
    await visible(page.getByText(/The schema is at 0003_security_and_operations and matches the models/)))
  check('the upgrade that built it is listed', await visible(page.getByText(/no revision → 0003_security_and_operations/)))
  await shot('schema')

  check('the at-a-glance backup card shows a fresh backup', await visible(page.getByText(/< 1 h ago/)))
  check('no page errors', consoleErrors.length === 0, consoleErrors.join(' | '))
}

try {
  if (PHASE === 'signin') await signinPhase()
  else if (PHASE === 'admin') await adminPhase()
  else check(`known phase ${PHASE}`, false)
} catch (err) {
  check('the browser run completed', false, err?.stack || String(err))
  await shot('error').catch(() => {})
} finally {
  await browser.close()
}

process.exit(failures ? 1 : 0)
