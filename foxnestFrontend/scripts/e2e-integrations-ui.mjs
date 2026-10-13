/**
 * Drive the Integrations screens in a real browser, the way a person would.
 *
 *   Run through the orchestrator, which starts the backend, a webhook receiver and
 *   this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_integrations_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Phases (E2E_PHASE), so the orchestrator can act from outside the browser in between:
 *
 *   token    as the CI account: make a token that can only report statuses
 *   setup    as the lead: add a signed webhook (a refused one first), ping it and send a
 *            delivery again; require ci/build, counted only from ci; add a push rule (a
 *            refused one first) and dry-run it; see the pull request blocked on the check
 *   waiting  the pull request says whose report did not count
 *   merge    the check has passed; merge the pull request
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_PHASE, E2E_REPO, SCREENSHOT_DIR,
 * E2E_RECEIVER (setup), optional E2E_BROWSER (chrome | msedge) and E2E_HEADED=1.
 *
 * Prints PASS/FAIL per check (and `TOKEN ...` / `SECRET ...` for the orchestrator) and
 * exits non-zero on any failure.
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
const REPO = env('E2E_REPO')
const SHOTS = env('SCREENSHOT_DIR')
const AWS_KEY_LINE = 'aws_key = AKIAIOSFODNN7EXAMPLE'

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

const dialog = () => page.getByRole('dialog').last()
const manager = () => page.getByRole('dialog').filter({ hasText: `Manage — ${REPO}` }).first()

async function signIn() {
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('Enter username').fill(USER)
  await page.getByPlaceholder('Enter password').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  check(`signs in as ${USER}`, await visible(page.getByRole('button', { name: 'Settings', exact: true }), 30000))
}

async function openRepository() {
  await page.getByText('Repositories', { exact: true }).first().click()
  // The page renders both a grid and a list; only one of them is shown.
  const name = page.locator(`:text-is("${REPO}"):visible`)
  check(`${REPO} is listed`, await visible(name, 30000))
  await name.first().click()
}

async function openManageTab(label) {
  if (!(await manager().isVisible().catch(() => false))) {
    await page.getByRole('button', { name: 'Manage', exact: true }).first().click()
  }
  await manager().getByRole('button', { name: label, exact: true }).click()
}

async function openPullRequest() {
  await openRepository()
  await page.getByRole('button', { name: 'Pull Requests' }).first().click()
  await dialog().getByRole('button', { name: 'Review' }).first().click()
  check('the pull request page opens', await visible(page.getByTestId('pr-checks'), 30000))
}

const requiredCheck = () => page.getByTestId('pr-checks').getByTestId('required-check').filter({ hasText: 'ci/build' })
const mergeButton = () => page.getByRole('button', { name: 'Merge pull request' })

// --- phases ------------------------------------------------------------------------

async function phaseToken() {
  console.log('\nUI  a token for CI')
  await page.getByRole('button', { name: 'Settings', exact: true }).first().click()
  await page.getByRole('button', { name: 'New token' }).click()
  await dialog().getByPlaceholder('ci-runner').fill('ui-ci-runner')
  const status = dialog().getByRole('radio', { name: /Report CI statuses/ })
  check('"Report CI statuses" is offered as an access level', await visible(status))
  await status.check()
  await shot('new-ci-token')
  await dialog().getByRole('button', { name: 'Create token' }).click()
  const reveal = page.getByTestId('new-token-value')
  check('the token is revealed once', await visible(reveal))
  const token = ((await reveal.innerText()) || '').trim()
  console.log(`TOKEN ${token}`)
  check('the reveal shows how CI reports with it', await visible(dialog().getByText(/fox status-check report ci\/build success/)))
  await shot('ci-token-reveal')
  await dialog().getByRole('button', { name: 'I have saved it' }).click()
  check('the token is listed as read & report statuses',
    await visible(page.getByTestId('token-row').filter({ hasText: 'ui-ci-runner' }).filter({ hasText: 'read & report statuses' })))
}

async function phaseSetup() {
  const receiver = env('E2E_RECEIVER')
  await openRepository()

  console.log('\nUI  webhooks')
  await openManageTab('Webhooks')
  await manager().getByRole('button', { name: 'Add webhook' }).first().click()
  const url = dialog().getByPlaceholder('https://ci.example.com/zanbeel-hook')
  await url.fill('http://169.254.169.254/latest/meta-data/')
  await dialog().getByRole('button', { name: 'Add webhook' }).click()
  check('a webhook to the metadata service is refused, in the dialog, saying why',
    await visible(dialog().getByText(/link-local address/)))
  await shot('webhook-refused')

  await url.fill(receiver)
  await dialog().getByRole('button', { name: 'Add webhook' }).click()
  const secret = page.getByTestId('webhook-secret')
  check('a secret is generated and shown once', await visible(secret))
  const value = ((await secret.innerText()) || '').trim()
  console.log(`SECRET ${value}`)
  check('...with how a receiver verifies it', await visible(dialog().getByText(/X-Zanbeel-Signature-256/)))
  await shot('webhook-secret')
  await dialog().getByRole('button', { name: 'I have saved it' }).click()
  const row = page.getByTestId('webhook-row').filter({ hasText: receiver })
  check('the webhook is listed as signed', await visible(row.getByText('signed', { exact: true })))
  check('...without its secret anywhere on the page', !(await page.content()).includes(value))

  await row.getByRole('button', { name: 'Ping' }).click()
  check('Ping reports the receiver\'s answer', await visible(page.getByText(/^Ping: .* answered 200/)))
  await row.getByRole('button', { name: 'Recent deliveries' }).click()
  const groups = row.getByTestId('delivery-group')
  check('the delivery log shows the ping', await visible(groups.filter({ hasText: 'ping' })))
  await groups.first().getByRole('button', { name: 'Redeliver' }).click()
  check('a delivery can be sent again from the log', await visible(page.getByText(/^Redelivery: .* answered 200/)))
  check('...and the log shows it as a redelivery', await visible(groups.filter({ hasText: 'redelivery' })))
  await groups.filter({ hasText: 'redelivery' }).first().getByRole('button').first().click()
  check('opening a delivery shows the signed body with its delivery id',
    await visible(row.getByText('Signed body')) && await visible(row.getByText(/"delivery_id"/)))
  await shot('webhook-deliveries')

  console.log('\nUI  required checks')
  await openManageTab('Status checks')
  const editor = page.getByTestId('required-checks-editor')
  check('the required-checks editor is shown', await visible(editor))
  await editor.getByRole('button', { name: 'Add check' }).click()
  await editor.getByLabel('Check name').last().fill('ci/build')
  await editor.getByLabel('Only counts when reported by').last().fill('ci')
  await editor.getByRole('button', { name: 'Save' }).click()
  check('saving says what merges now need', await visible(page.getByText('Merges now require: ci/build (from ci)')))
  check('the check is shown as required, from ci only',
    await visible(page.getByTestId('required-check').filter({ hasText: 'ci/build' }).filter({ hasText: 'counts only from ci' })))
  await shot('required-checks')

  console.log('\nUI  push rules')
  await openManageTab('Push rules')
  await manager().getByRole('button', { name: 'Add rule' }).first().click()
  await dialog().getByLabel('Rule name').fill('bad-policy')
  await dialog().getByLabel('External policy service', { exact: true }).fill('http://127.0.0.1:1/policy')
  await dialog().getByRole('button', { name: 'Create rule' }).click()
  check('a policy service on this server is refused, in the dialog', await visible(dialog().getByText(/loopback address/)))
  await dialog().getByLabel('External policy service', { exact: true }).fill('')
  await dialog().getByLabel('Rule name').fill('no-secrets')
  await dialog().getByLabel(/Block secrets/).check()
  await dialog().getByLabel('New branch names must match (regex)').fill('^(feature|fix)/')
  await shot('new-rule')
  await dialog().getByRole('button', { name: 'Create rule' }).click()
  const rule = page.getByTestId('push-rule-row').filter({ hasText: 'no-secrets' })
  check('the rule is listed with what it does',
    await visible(rule.getByText(/blocks secrets/)) && await visible(rule.getByText(/new branches match \^\(feature\|fix\)\//)))

  await rule.getByRole('button', { name: 'Dry run' }).click()
  await dialog().getByLabel('A file to test content rules with (path)').fill('config/settings.py')
  await dialog().getByLabel('Its content').fill(AWS_KEY_LINE)
  await dialog().getByRole('button', { name: 'Run', exact: true }).click()
  check('the dry run tests a file\'s content and would reject it',
    await visible(dialog().getByText('This push would be rejected')) && await visible(dialog().getByText(/AWS access key/)))
  await shot('dry-run')
  await dialog().getByRole('button', { name: 'Close', exact: true }).last().click()
  await page.keyboard.press('Escape')

  console.log('\nUI  the pull request, before CI')
  await page.getByRole('button', { name: 'Pull Requests' }).first().click()
  await dialog().getByRole('button', { name: 'Review' }).first().click()
  check('the pull request page shows its checks', await visible(page.getByTestId('pr-checks'), 30000))
  check('ci/build is required and has not reported', await visible(requiredCheck().getByText(/Has not reported/)))
  check('...so Merge is disabled', await mergeButton().isDisabled())
  await shot('pr-before-ci')
}

async function phaseWaiting() {
  await openPullRequest()
  check('the PR says whose report did not count',
    await visible(requiredCheck().getByText('dev reported success, but only ci counts. Waiting for ci.')))
  check('...and Merge is still disabled', await mergeButton().isDisabled())
  await shot('pr-ignored-report')
}

async function phaseMerge() {
  await openPullRequest()
  check('ci/build has passed, reported by ci', await visible(requiredCheck().getByText(/Passed.* · by ci/)))
  check('the pull request is ready to merge', await visible(page.getByText('Ready to merge')))
  await shot('pr-ready')
  await mergeButton().click()
  check('Merge goes through', await visible(page.getByText('Merged.'), 30000))
  await shot('pr-merged')
}

try {
  await signIn()
  if (PHASE === 'token') await phaseToken()
  else if (PHASE === 'setup') await phaseSetup()
  else if (PHASE === 'waiting') await phaseWaiting()
  else await phaseMerge()
  // The refused webhook and the refused rule are refused on purpose. The browser logs
  // each as a failed resource load and api.js logs every failed request; both are the
  // refusal working. Anything else is a real error.
  const expected = /favicon|DevTools|Failed to load resource|link-local|loopback/i
  const relevant = consoleErrors.filter((e) => !expected.test(e))
  check('no errors in the browser console', relevant.length === 0, relevant.join('\n'))
} catch (err) {
  await shot('failure').catch(() => {})
  check('the UI run completed without an unexpected error', false, err?.stack || err)
} finally {
  await browser.close()
}

process.exit(failures ? 1 : 0)
