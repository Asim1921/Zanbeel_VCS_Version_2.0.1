/**
 * Drive the conflict resolver in a real browser, the way a person would.
 *
 *   Run through the orchestrator, which starts the backend and this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_merge_conflicts_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_REPO, E2E_PR, SCREENSHOT_DIR,
 * optional E2E_BROWSER (chrome | msedge) and E2E_HEADED=1.
 *
 * Prints PASS/FAIL per check and exits non-zero on any failure. The orchestrator then
 * checks on the server that what was clicked is what was committed.
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
const REPO = env('E2E_REPO')
const PR = env('E2E_PR')
const SHOTS = env('SCREENSHOT_DIR')
const URDU_RESOLVED = 'سلام دنیا اور دوستو\nخوش آمدید ✓\n'

mkdirSync(SHOTS, { recursive: true })

let failures = 0
let step = 0
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
const page = await (await browser.newContext({ viewport: { width: 1500, height: 1000 } })).newPage()

const consoleErrors = []
page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()) })
page.on('pageerror', (err) => consoleErrors.push(String(err)))

const shot = async (label) => {
  step += 1
  await page.screenshot({ path: join(SHOTS, `${String(step).padStart(2, '0')}-${label}.png`), fullPage: false })
}

const visible = async (locator, timeout = 15000) => {
  try {
    await locator.first().waitFor({ state: 'visible', timeout })
    return true
  } catch {
    return false
  }
}

const dialog = () => page.getByRole('dialog').last()
const resolver = () => page.getByRole('dialog').filter({ hasText: 'Resolve conflicts' }).last()

try {
  console.log('\nUI  sign in and reach the pull request')
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('Enter username').fill(USER)
  await page.getByPlaceholder('Enter password').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  check('signs in', await visible(page.getByText('Repositories'), 30000))

  await page.getByText('Repositories', { exact: true }).first().click()
  // The page renders both a grid and a list; only one of them is shown.
  const repoName = page.locator(`:text-is("${REPO}"):visible`)
  check('the repository is listed', await visible(repoName, 30000))
  await repoName.first().click()
  await page.getByRole('button', { name: 'Pull Requests' }).first().click()
  check('the PR list shows the conflict badge',
    await visible(dialog().getByText(/conflicting files?/), 30000))
  check('...and offers Resolve conflicts on the row',
    await visible(dialog().getByRole('button', { name: /Resolve conflicts|Continue resolving/ })))
  await shot('pr-list')

  await dialog().getByRole('button', { name: 'Review' }).first().click()
  console.log('\nUI  the pull request page')
  check('says the branch has conflicts that must be resolved',
    await visible(page.getByText('This branch has conflicts that must be resolved'), 30000))
  check('lists the conflicting files', await visible(page.getByText('app.py', { exact: true })))
  check('Merge is disabled while conflicted',
    await page.getByRole('button', { name: 'Merge pull request' }).isDisabled())
  await shot('pr-detail-conflicted')

  console.log('\nUI  the resolver')
  await page.getByRole('button', { name: /^(Resolve conflicts|Continue resolving)$/ }).last().click()
  check('opens with progress shown', await visible(resolver().getByText(/0 of 3 resolved/), 30000))
  await shot('resolver-open')

  // app.py: a conflict block with per-block choices.
  await resolver().getByRole('button', { name: /app\.py/ }).first().click()
  check('shows the conflict block for line 5', await visible(resolver().getByText(/Conflict 1 · line 5/)))
  const markResolved = resolver().getByRole('button', { name: 'Mark as resolved' })
  check('Mark as resolved is disabled while markers remain', await markResolved.isDisabled())
  const editor = resolver().locator('textarea')
  const before = await editor.inputValue()
  check('the editor shows labelled diff3 markers',
    before.includes('<<<<<<< main') && before.includes('||||||| base') && before.includes('>>>>>>> feature'),
    before)
  check('the clean edit on line 9 is already merged', before.includes('line 9 feature'))
  await resolver().getByRole('button', { name: 'Accept both' }).click()
  const after = await editor.inputValue()
  check('Accept both replaces the block with both sides',
    !after.includes('<<<<<<<') && after.includes('line 5 main\nline 5 feature\n'), after)
  await shot('app-accept-both')
  await markResolved.click()
  check('app.py is marked resolved', await visible(resolver().getByText(/1 of 3 resolved/)))

  // i18n/ur.txt: non-ASCII text must arrive and leave intact.
  await resolver().getByRole('button', { name: /ur\.txt/ }).first().click()
  const urdu = await editor.inputValue()
  check('Urdu text is shown correctly (not garbled)',
    urdu.includes('سلام دنیا') && urdu.includes('سلام دوستو') && !urdu.includes('Ø'), urdu)
  await editor.fill(URDU_RESOLVED)
  await shot('urdu-edited')
  await resolver().getByRole('button', { name: 'Mark as resolved' }).click()
  check('ur.txt is marked resolved', await visible(resolver().getByText(/2 of 3 resolved/)))

  // notes.txt: edited on one side, deleted on the other.
  await resolver().getByRole('button', { name: /notes\.txt/ }).first().click()
  check('explains which side deleted the file', await visible(resolver().getByText(/deleted this file/)))
  await resolver().getByRole('button', { name: 'Delete the file' }).click()
  check('the file is shown as going to be deleted',
    await visible(resolver().getByText('This file will be deleted by the merge.')))
  await resolver().getByRole('button', { name: 'Mark as resolved' }).click()
  check('all three files are resolved', await visible(resolver().getByText(/3 of 3 resolved/)))
  await shot('all-resolved')

  // Saved on the server: close and come back.
  await resolver().getByRole('button', { name: 'Close' }).click()
  await page.getByRole('button', { name: 'Continue resolving' }).last().click()
  check('decisions survive closing and reopening', await visible(resolver().getByText(/3 of 3 resolved/), 30000))
  check('the commit says where it goes',
    await visible(resolver().getByText(/Commits a merge of 'main' into 'feature'/)))

  const commit = resolver().getByRole('button', { name: 'Commit merge' })
  check('Commit merge is enabled', await commit.isEnabled())
  await commit.click()

  console.log('\nUI  after the resolution')
  check('reports that approvals are stale',
    await visible(page.getByText(/Approvals given before this commit are stale/), 30000))
  check('the conflict panel is gone',
    !(await page.getByText('This branch has conflicts that must be resolved').isVisible()))
  check('the stale approval is shown', await visible(page.getByText(/went stale when the branch moved/)))
  check('Merge waits for a fresh approval',
    await page.getByRole('button', { name: 'Merge pull request' }).isDisabled())
  await shot('after-resolution')

  const relevant = consoleErrors.filter((e) => !/favicon|DevTools/i.test(e))
  check('no errors in the browser console', relevant.length === 0, relevant.join('\n'))
} catch (err) {
  await shot('failure').catch(() => {})
  check('the UI run completed without an unexpected error', false, err?.stack || err)
} finally {
  await browser.close()
}

console.log(`\nscreenshots: ${SHOTS}`)
process.exit(failures ? 1 : 0)
