/**
 * Drive the credentials screens in a real browser, the way a person would.
 *
 *   Run through the orchestrator, which starts the backend and this dev server:
 *     ..\venv\Scripts\python.exe tools/e2e_credentials_ui.py      (from server/)
 *
 * Needs playwright-core and an installed Chrome; nothing is downloaded:
 *     npm i --no-save playwright-core
 *
 * Two phases (E2E_PHASE), so the orchestrator can use what phase 1 created -- sign in
 * with the token and the key from outside the browser -- before phase 2 looks at the
 * result and takes it all away again.
 *
 *   1  create a read-only token (one-time reveal), add a read-only SSH key, see a bad key refused
 *   2  see the key's live sign-in, remove the key, revoke the token, read the notifications,
 *      and as an administrator revoke everything another user holds
 *
 * Inputs (env): FRONTEND_URL, E2E_USER, E2E_PASSWORD, E2E_PHASE, SCREENSHOT_DIR,
 * E2E_PUBKEY and E2E_FINGERPRINT (phase 1), E2E_OTHER_USER (phase 2), optional
 * E2E_BROWSER (chrome | msedge) and E2E_HEADED=1.
 *
 * Prints PASS/FAIL per check (and `TOKEN fxp_...` for the orchestrator) and exits
 * non-zero on any failure.
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
const page = await (await browser.newContext({ viewport: { width: 1400, height: 950 } })).newPage()

const consoleErrors = []
page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()) })
page.on('pageerror', (err) => consoleErrors.push(String(err)))
// Revoke / Remove ask with window.confirm; say yes, as the person would.
page.on('dialog', (d) => d.accept())

let step = 0
const shot = async (label) => {
  step += 1
  await page.screenshot({ path: join(SHOTS, `p${PHASE}-${String(step).padStart(2, '0')}-${label}.png`) })
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

const dialog = () => page.getByRole('dialog').last()
const nav = (name) => page.getByRole('button', { name, exact: true }).first().click()
const tab = (name) => page.getByRole('button', { name, exact: true }).first().click()
const tokenRow = (name) => page.getByTestId('token-row').filter({ hasText: name })
const keyRow = (title) => page.getByTestId('ssh-key-row').filter({ hasText: title })

async function signIn() {
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('Enter username').fill(USER)
  await page.getByPlaceholder('Enter password').fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  check(`signs in as ${USER}`, await visible(page.getByRole('button', { name: 'Settings', exact: true }), 30000))
}

async function phaseOne() {
  console.log('\nUI  access tokens')
  await nav('Settings')
  check('the Settings page opens on access tokens', await visible(page.getByText('Personal access tokens')))
  await page.getByRole('button', { name: 'New token' }).click()
  check('the new-token dialog opens', await visible(dialog().getByText('New access token')))
  await dialog().getByPlaceholder('ci-runner').fill('ui-ci')
  check('read only is the default access', await dialog().getByRole('radio', { name: /Read only/ }).isChecked())
  check('expiry defaults to 90 days', (await page.locator('#token-expiry').inputValue()) === '90')
  await page.locator('#token-expiry').selectOption('30')
  await shot('new-token')
  await dialog().getByRole('button', { name: 'Create token' }).click()

  const reveal = page.getByTestId('new-token-value')
  check('the token is revealed once', await visible(reveal))
  const token = ((await reveal.innerText()) || '').trim()
  check('...and it is an fxp_ token', token.startsWith('fxp_'), token)
  console.log(`TOKEN ${token}`)
  check('the reveal shows how a build agent uses it',
    await visible(dialog().getByText('FOXNEST_TOKEN=<token> fox pull')))
  await shot('reveal')
  await dialog().getByRole('button', { name: 'I have saved it' }).click()
  check('the token is listed, active and read only',
    await visible(tokenRow('ui-ci').filter({ hasText: 'read only' }).filter({ hasText: 'Active' })))
  check('...without its value anywhere on the page', !(await page.content()).includes(token))

  console.log('\nUI  SSH keys')
  await tab('SSH keys')
  check('the SSH keys tab opens', await visible(page.getByText('Sign in from the CLI with a key')))
  await page.getByRole('button', { name: 'Add key' }).first().click()
  const keyBox = dialog().getByPlaceholder(/ssh-ed25519 AAAA/)
  await dialog().getByPlaceholder('work-laptop').fill('ui-laptop')
  await keyBox.fill('-----BEGIN OPENSSH PRIVATE KEY----- b3BlbnNzaC1rZXktdjEAAAA -----END OPENSSH PRIVATE KEY-----')
  await dialog().getByRole('radio', { name: /Read only/ }).check()
  await dialog().getByRole('button', { name: 'Add key' }).click()
  check('a pasted private key is refused, in the dialog',
    await visible(dialog().getByText(/private key/i).filter({ hasText: /Paste the public key/i })))
  await shot('private-key-refused')

  await keyBox.fill(env('E2E_PUBKEY'))
  await dialog().getByRole('button', { name: 'Add key' }).click()
  const row = keyRow('ui-laptop')
  check('the key is listed', await visible(row))
  check('...as read only', await visible(row.getByText('read only')))
  check('...with the fingerprint ssh-keygen reports', await visible(row.getByText(env('E2E_FINGERPRINT'))))
  await shot('key-added')
}

async function phaseTwo() {
  console.log('\nUI  what phase 1 made, in use')
  await nav('Settings')
  await tab('SSH keys')
  const row = keyRow('ui-laptop')
  check('the key shows its live sign-in', await visible(row.getByText('1 active sign-in')))
  await shot('key-in-use')
  await row.getByRole('button', { name: 'Remove' }).click()
  check('removing the key takes it off the list', await gone(row))

  await tab('Access tokens')
  const token = tokenRow('ui-ci')
  check('the token shows it has been used', await visible(token.getByText(/last used (just now|\d+m ago)/)))
  await token.getByRole('button', { name: 'Revoke' }).click()
  check('revoking the token takes it off the list', await gone(token))
  await page.getByLabel('Show revoked and expired').check()
  check('...and it is still there, marked Revoked, with history shown',
    await visible(tokenRow('ui-ci').getByText('Revoked')))

  console.log('\nUI  notifications')
  await nav('Notifications')
  check('the owner was told about the new token', await visible(page.getByText('New access token: ui-ci')))
  check('...and about the new key', await visible(page.getByText('New SSH key: ui-laptop')))
  await shot('notifications')

  console.log('\nUI  an administrator revokes another user\'s credentials')
  const other = env('E2E_OTHER_USER')
  await nav('Operations')
  await tab('Credentials')
  const holder = page.getByTestId('credential-holder').filter({ hasText: other })
  check(`${other}'s token and key are listed`, await visible(holder.getByText('dev-ci')) &&
    await visible(holder.getByText('dev-laptop')))
  await shot('credentials-overview')
  await holder.getByRole('button', { name: 'Revoke all' }).click()
  check('Revoke all reports what it did',
    await visible(page.getByText(`${other}: 1 token(s) revoked, 1 SSH key(s) removed.`)))
  check(`${other} no longer holds anything`, await gone(holder))
  await shot('revoked-all')
}

try {
  await signIn()
  if (PHASE === '1') await phaseOne()
  else await phaseTwo()
  // The private key pasted in phase 1 is refused on purpose. The browser logs that as a
  // failed resource load and api.js logs every failed request; both are the refusal
  // working. Anything else is a real error.
  const expected = /favicon|DevTools|Failed to load resource|That looks like a private key/i
  const relevant = consoleErrors.filter((e) => !expected.test(e))
  check('no errors in the browser console', relevant.length === 0, relevant.join('\n'))
} catch (err) {
  await shot('failure').catch(() => {})
  check('the UI run completed without an unexpected error', false, err?.stack || err)
} finally {
  await browser.close()
}

process.exit(failures ? 1 : 0)
