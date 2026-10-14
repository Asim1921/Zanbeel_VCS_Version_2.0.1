/**
 * Drive branches, releases and tags, and generated documentation in a real browser.
 *
 *   Run through the orchestrator, which starts the backend and this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_repository_management_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Phases (E2E_PHASE):
 *   lead  a deep link to the Branches tab through sign-in; the list (distance, PR, badges);
 *         create, rename, delete and restore a branch through dialogs; a branch in use
 *         cannot be deleted; a branch name opens its code; draft a release with generated
 *         notes and preview; notes are rendered without raw HTML; tags, a tag's release,
 *         a zip download; the Docs tab: rendered Markdown, links between documents, Back,
 *         a Word file; generating from the browser and seeing it committed
 *   dev   no management buttons; may create a branch and draft a release; generating
 *         keeps the documents and says they were not committed because of review
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_PHASE, SCREENSHOT_DIR, E2E_REPO,
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
const REPO = env('E2E_REPO')

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
const context = await browser.newContext({ viewport: { width: 1400, height: 950 }, acceptDownloads: true })
const page = await context.newPage()

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

const gone = async (locator, timeout = 15000) => {
  try {
    await locator.first().waitFor({ state: 'detached', timeout })
    return true
  } catch {
    return false
  }
}

const url = () => new URL(page.url())
const row = (name) => page.locator(`[data-testid="branch-row"][data-branch="${name}"]`)
const dialog = () => page.getByRole('dialog')

async function signIn() {
  await page.getByPlaceholder('Enter username').fill(USER)
  await page.getByPlaceholder('Enter password').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
}

async function open(tab, extra = '') {
  await page.goto(`${FRONTEND}/repositories/${REPO}?tab=${tab}${extra}`, { waitUntil: 'domcontentloaded' })
}

// --- lead ----------------------------------------------------------------------------

async function leadBranches() {
  console.log('\nUI  the Branches tab')
  await open('branches')
  await signIn()
  check('a link to the Branches tab lands there after signing in', await visible(page.getByTestId('branches-tab'), 30000))
  check('...and the tab is marked as selected',
    (await page.getByRole('tab', { name: 'Branches' }).getAttribute('aria-selected')) === 'true')
  const login = row('feature/login')
  check('feature/login is listed with its distance from main', await visible(login))
  const distance = (await login.getByTestId('branch-distance').getAttribute('title')) || ''
  // main moved on twice after the branch: the login fix and the generated docs.
  check('...2 behind and 2 ahead', /2 commit\(s\) behind and 2 ahead of main/.test(distance), distance)
  check('...with its last commit', await visible(login.getByText('Polish login')))
  check('...and the open pull request that uses it', await visible(login.getByRole('link', { name: /#\d+/ })))
  check('main is shown as the default', await visible(row('main').getByText('Default')))
  const del = login.getByRole('button', { name: 'Delete feature/login' })
  check('a branch an open pull request uses cannot be deleted', await del.isDisabled())
  const why = await login.locator('span[title^="Used by open pull request"]').count()
  check('...and says why', why === 1)
  await shot('branches')

  console.log('\nUI  create, rename, delete and restore a branch')
  await page.getByRole('button', { name: 'New branch' }).click()
  await dialog().getByLabel('Branch name').fill('ui/topic')
  await dialog().getByRole('button', { name: 'Create branch' }).click()
  check('a new branch appears in the list', await visible(row('ui/topic')))
  check('...with a notice', await visible(page.getByTestId('branch-notice').getByText('Created ui/topic')))

  await row('ui/topic').getByRole('button', { name: 'Rename ui/topic' }).click()
  const name = dialog().getByLabel('New name')
  await name.fill('ui/renamed')
  await dialog().getByRole('button', { name: 'Rename' }).click()
  check('a branch with a slash is renamed', await visible(row('ui/renamed')) && await gone(row('ui/topic')))

  await row('ui/renamed').getByRole('button', { name: 'Delete ui/renamed' }).click()
  await dialog().getByRole('button', { name: 'Delete branch' }).click()
  check('...and deleted', await gone(row('ui/renamed')))
  await shot('deleted')
  await page.getByTestId('branch-notice').getByRole('button', { name: 'Restore' }).click()
  check('...and restored from the notice', await visible(row('ui/renamed')))

  console.log('\nUI  a branch name opens its code')
  await row('feature/login').getByRole('button', { name: 'feature/login', exact: true }).click()
  check('the Code tab opens on that branch',
    url().searchParams.get('branch') === 'feature/login' && !url().searchParams.get('tab'), page.url())
  check('...showing its files', await visible(page.getByText('login.py')))
  await page.goBack()
  check('Back returns to the Branches tab', await visible(page.getByTestId('branches-tab')))
}

async function leadReleases() {
  console.log('\nUI  releases')
  await open('releases')
  const tab = page.getByTestId('releases-tab')
  check('the Releases tab lists the existing release', await visible(tab.locator('[data-testid="release-card"][data-version="1.0.0"]')))
  const first = tab.locator('[data-testid="release-card"][data-version="1.0.0"]')
  check('notes are rendered as Markdown', await visible(first.locator('h2', { hasText: 'Highlights' })))
  check('...but raw HTML in them is not', (await first.locator('img, script, iframe').count()) === 0)
  check('...and a javascript: link goes nowhere', (await first.locator('a[href^="javascript"]').count()) === 0)

  await tab.getByRole('button', { name: 'Draft a new release' }).click()
  await dialog().getByLabel('Version').fill('1.1.0')
  await dialog().getByRole('button', { name: 'Generate notes' }).click()
  const notes = dialog().getByLabel('Release notes')
  let drafted = ''
  for (let i = 0; i < 40 && !drafted.includes("What's changed"); i += 1) {
    drafted = await notes.inputValue()
    if (!drafted.includes("What's changed")) await page.waitForTimeout(250)
  }
  check('notes are drafted from what changed since 1.0.0', drafted.includes('Fix the login redirect'), drafted)
  await dialog().getByRole('button', { name: 'preview' }).click()
  check('...and preview as Markdown', await visible(page.getByTestId('notes-preview').locator('h2', { hasText: "What's changed" })))
  await shot('release-draft')
  await dialog().getByRole('button', { name: 'Publish release' }).click()
  const card = tab.locator('[data-testid="release-card"][data-version="1.1.0"]')
  check('the release is published', await visible(card))
  check('...marked Latest', await visible(card.getByText('Latest')))

  const download = page.waitForEvent('download', { timeout: 20000 }).catch(() => null)
  await card.getByRole('button', { name: 'Source code (zip)' }).click()
  const file = await download
  check('its source code downloads as a zip', Boolean(file) && /\.zip$/.test(file.suggestedFilename()), file?.suggestedFilename())

  console.log('\nUI  tags')
  await tab.getByRole('button', { name: /^Tags/ }).click()
  check('the Tags view is in the address', url().searchParams.get('tab') === 'tags', page.url())
  const v1 = tab.locator('[data-testid="tag-row"][data-tag="v1.0.0"]')
  check('a tag shows the release built on it', await visible(v1.getByText('Release 1.0.0')))
  check('...and cannot be deleted while it does', await v1.getByRole('button', { name: 'Delete v1.0.0' }).isDisabled())
  await shot('tags')
}

async function leadDocs() {
  console.log('\nUI  documentation')
  await open('docs')
  const content = page.getByTestId('docs-content')
  check('the Docs tab opens the index', await visible(content.locator('h1'), 20000))
  check('...and says where the documents came from',
    await visible(page.getByTestId('docs-provenance').getByText(/Committed to main/)))
  await content.getByRole('link', { name: 'Project Docs' }).click()
  check('a link between documents opens the other one',
    url().searchParams.get('doc') === 'project/index.md', page.url())
  await page.getByTestId('docs-tree').getByRole('button', { name: 'README.md' }).click()
  check('the tree opens a document', await visible(content.locator('h1', { hasText: 'README.md' })))
  check('...rendered, not as raw Markdown', (await content.locator('pre').count()) === 0)
  await shot('docs-readme')
  await page.goBack()
  check('Back returns to the previous document', url().searchParams.get('doc') === 'project/index.md', page.url())
  await page.getByTestId('docs-tree').getByRole('button', { name: 'README.docx' }).click()
  check('a Word file offers a download instead', await visible(content.getByRole('button', { name: 'Download it' })))

  console.log('\nUI  generating from the browser')
  await page.getByRole('button', { name: 'Regenerate' }).click()
  await dialog().getByRole('button', { name: 'Clear' }).click()
  await dialog().getByLabel('README.md').check()
  await dialog().getByRole('button', { name: 'Start' }).click()
  const provenance = page.getByTestId('docs-provenance')
  let text = ''
  for (let i = 0; i < 60; i += 1) {
    text = (await provenance.innerText().catch(() => '')) || ''
    if (/just now/.test(text) && /Committed to main/.test(text)) break
    await page.waitForTimeout(500)
  }
  check('a generation started here finishes and is committed', /just now/.test(text) && /Committed to main/.test(text), text)
  await shot('docs-regenerated')
}

// --- developer ---------------------------------------------------------------------------

async function devPhase() {
  console.log('\nUI  what a developer is offered')
  await open('branches')
  await signIn()
  check('the branch list loads for a developer', await visible(row('feature/login'), 30000))
  check('...with no management buttons', (await page.locator('[data-testid="branch-row"] button[aria-label^="Delete "]').count()) === 0)
  check('...but a developer may create a branch', await visible(page.getByRole('button', { name: 'New branch' })))
  await open('releases')
  check('a developer may draft a release', await visible(page.getByRole('button', { name: 'Draft a new release' })))
  check('...but not delete one', (await page.getByRole('button', { name: /^Delete 1\./ }).count()) === 0)

  console.log('\nUI  a developer generates documentation')
  await open('docs')
  await page.getByRole('button', { name: 'Regenerate' }).click()
  await dialog().getByRole('button', { name: 'Clear' }).click()
  await dialog().getByLabel('Architecture').check()
  await dialog().getByRole('button', { name: 'Start' }).click()
  check('the documents are kept and the page says why they were not committed',
    await visible(page.getByTestId('docs-not-committed').getByText(/review/), 60000))
  await shot('dev-docs')
}

try {
  if (PHASE === 'lead') {
    await leadBranches()
    await leadReleases()
    await leadDocs()
  } else {
    await devPhase()
  }
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
