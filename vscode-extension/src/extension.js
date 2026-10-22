// Zanbeel VS Code extension.
//
// The point of this extension is narrow and worth stating plainly: make `fox`
// work in the integrated terminal without anyone installing the client on their
// machine. Everything else here exists to support that.
//
// How it works
// ------------
// VS Code lets an extension mutate the environment of the terminals it spawns
// (`ExtensionContext.environmentVariableCollection`). We write a tiny launcher
// script into the extension's global storage, point it at the bundled `fox.py`
// and a Python interpreter we resolved, then prepend that directory to PATH.
// Nothing touches the system PATH, no administrator rights are needed, and VS
// Code reverts the whole thing when the extension is uninstalled.
//
// The launcher is generated rather than shipped because the extension's own
// install directory is not reliably writable, and because the interpreter path
// is only known at runtime.
//
// The server's certificate
// ------------------------
// The Zanbeel server uses a self-signed certificate, which neither Node nor
// Python trusts by default. The extension ships a copy (certs/zanbeel.crt) and
// trusts it, alongside the usual public roots: for its own requests, and for
// `fox` in its terminals through REQUESTS_CA_BUNDLE. Nothing on the machine
// outside VS Code changes.

const vscode = require('vscode');
const cp = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const https = require('https');
const os = require('os');
const path = require('path');
const tls = require('tls');

const TOKEN_KEY = 'zanbeel.accessToken';
const USER_KEY = 'zanbeel.username';

/** Offered when signing in, unless zanbeel.serverUrl is already set. */
const DEFAULT_SERVER = 'https://192.168.15.207:8090';

/** @type {vscode.OutputChannel} */
let output;
/** @type {vscode.StatusBarItem | undefined} */
let statusItem;
/** Resolved interpreter, as argv (e.g. ['py', '-3.11'] or ['python3']). */
let pythonArgv = null;
/** CA bundle handed to the CLI as REQUESTS_CA_BUNDLE, once written. */
let caBundlePath = null;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function log(message) {
  const stamp = new Date().toISOString().slice(11, 19);
  output.appendLine(`[${stamp}] ${message}`);
}

function config() {
  return vscode.workspace.getConfiguration('zanbeel');
}

/** Run a command and resolve with its result, never rejecting. */
function run(command, args, options = {}) {
  return new Promise((resolve) => {
    cp.execFile(
      command,
      args,
      { timeout: options.timeout || 20000, cwd: options.cwd, windowsHide: true },
      (error, stdout, stderr) => {
        resolve({
          ok: !error,
          code: error && typeof error.code === 'number' ? error.code : error ? 1 : 0,
          stdout: (stdout || '').toString(),
          stderr: (stderr || '').toString(),
        });
      }
    );
  });
}

/** The bundled CLI that ships inside this extension. */
function bundledCli(context) {
  return vscode.Uri.joinPath(context.extensionUri, 'cli', 'fox.py').fsPath;
}

// ---------------------------------------------------------------------------
// The server's certificate, and requests to the server
// ---------------------------------------------------------------------------

/** The certificate file to trust: zanbeel.caCertificate, else the bundled copy. */
function caCertificateFile(context) {
  const configured = (config().get('caCertificate') || '').trim();
  if (!configured) return vscode.Uri.joinPath(context.extensionUri, 'certs', 'zanbeel.crt').fsPath;
  return configured.replace(/^~(?=$|[\\/])/, os.homedir());
}

/** That certificate as PEM text, or '' when it cannot be read. */
function caCertificatePem(context) {
  const file = caCertificateFile(context);
  try {
    const pem = fs.readFileSync(file, 'utf8');
    if (pem.includes('-----BEGIN CERTIFICATE-----')) return pem;
    log(`Not a PEM certificate, ignored: ${file}`);
  } catch (err) {
    log(`Could not read the certificate ${file}: ${err.message}`);
  }
  return '';
}

/**
 * A CA bundle for the CLI: the public roots Node ships, the server's certificate,
 * and any bundle REQUESTS_CA_BUNDLE already named. requests takes this file
 * instead of its own list, so the public roots stay in and other HTTPS sites
 * keep working from the same terminals.
 */
async function writeCaBundle(context) {
  const pem = caCertificatePem(context);
  if (!pem) return null;
  const target = vscode.Uri.joinPath(context.globalStorageUri, 'ca-bundle.pem');
  const parts = [...tls.rootCertificates, pem.trim()];
  const inherited = process.env.REQUESTS_CA_BUNDLE;
  if (inherited && path.resolve(inherited) !== target.fsPath) {
    try {
      parts.push(fs.readFileSync(inherited, 'utf8').trim());
    } catch {
      /* not readable: keep ours */
    }
  }
  await vscode.workspace.fs.createDirectory(context.globalStorageUri);
  await vscode.workspace.fs.writeFile(target, new TextEncoder().encode(parts.join('\n') + '\n'));
  return target.fsPath;
}

/**
 * A request to the Zanbeel server, trusting its certificate as well as the
 * public roots. Node's fetch() cannot be given extra CAs, hence https.request;
 * this returns the part of a fetch Response the commands use.
 */
function request(context, url, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    let target;
    try {
      target = new URL(url);
    } catch {
      reject(new Error(`not a valid address: ${url}`));
      return;
    }
    const secure = target.protocol === 'https:';
    if (!secure && target.protocol !== 'http:') {
      reject(new Error(`not an http(s) address: ${url}`));
      return;
    }
    const options = { method, headers: { Accept: 'application/json', ...headers }, timeout: 30000 };
    if (body !== undefined) options.headers['Content-Length'] = Buffer.byteLength(body);
    const pem = secure ? caCertificatePem(context) : '';
    if (pem) options.ca = [...tls.rootCertificates, pem];

    const req = (secure ? https : http).request(target, options, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        // The server is HTTPS only and answers http:// with a redirect, which would
        // turn a sign-in POST into a GET. Name the right address instead.
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          const where = new URL(res.headers.location, target);
          reject(new Error(`the server redirects to ${where.origin}; use that address`));
          return;
        }
        const text = Buffer.concat(chunks).toString('utf8');
        resolve({
          ok: res.statusCode >= 200 && res.statusCode < 300,
          status: res.statusCode,
          json: async () => JSON.parse(text),
        });
      });
      res.on('error', reject);
    });
    req.on('timeout', () => req.destroy(new Error('the server did not answer within 30 seconds')));
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

/** An error from request(), with what to do about a certificate problem. */
function describeError(context, err) {
  const certificateErrors = [
    'DEPTH_ZERO_SELF_SIGNED_CERT',
    'SELF_SIGNED_CERT_IN_CHAIN',
    'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
    'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
    'CERT_HAS_EXPIRED',
    'ERR_TLS_CERT_ALTNAME_INVALID',
  ];
  if (!certificateErrors.includes(err.code)) return err.message;
  return (
    `${err.message}. The server's certificate does not match the one this extension trusts ` +
    `(${caCertificateFile(context)}). Download the server's certificate from /zanbeel.crt ` +
    'and point the zanbeel.caCertificate setting at it.'
  );
}

// ---------------------------------------------------------------------------
// Python discovery
// ---------------------------------------------------------------------------

/**
 * Find an interpreter that can actually run the CLI.
 *
 * Candidates are tried in order and the first that reports Python 3.8+ wins.
 * `py -3.11` comes first on Windows because that is the interpreter the server
 * and CLI are developed against; the generic launchers follow.
 */
async function resolvePython() {
  const configured = (config().get('pythonPath') || '').trim();
  if (configured) {
    // Allow "py -3.11" as well as a bare path.
    const parts = configured.split(/\s+/);
    const probe = await run(parts[0], [...parts.slice(1), '--version']);
    if (probe.ok) {
      log(`Using configured interpreter: ${configured}`);
      return parts;
    }
    log(`Configured interpreter did not run: ${configured} (${probe.stderr.trim()})`);
  }

  const candidates =
    process.platform === 'win32'
      ? [['py', '-3.11'], ['py', '-3'], ['python'], ['python3']]
      : [['python3'], ['python']];

  for (const argv of candidates) {
    const probe = await run(argv[0], [...argv.slice(1), '-c', 'import sys; print(sys.version_info[0], sys.version_info[1])']);
    if (!probe.ok) continue;
    const [major, minor] = probe.stdout.trim().split(/\s+/).map(Number);
    if (major === 3 && minor >= 8) {
      log(`Detected Python ${major}.${minor} via: ${argv.join(' ')}`);
      return argv;
    }
    log(`Skipping ${argv.join(' ')} — reports Python ${major}.${minor}, need 3.8+`);
  }
  return null;
}

/** Does this interpreter have the CLI's one hard dependency? */
async function hasRequests(argv) {
  const probe = await run(argv[0], [...argv.slice(1), '-c', 'import requests']);
  return probe.ok;
}

// ---------------------------------------------------------------------------
// Launcher generation + PATH injection
// ---------------------------------------------------------------------------

/**
 * Write a `fox` launcher into global storage and return the directory holding it.
 *
 * Global storage rather than the extension directory: the latter is not
 * reliably writable (and is replaced wholesale on update), while this path is
 * ours and persists.
 */
async function writeLauncher(context, argv, cliPath) {
  const binDir = vscode.Uri.joinPath(context.globalStorageUri, 'bin');
  await vscode.workspace.fs.createDirectory(binDir);

  const interpreter = argv[0];
  const extraArgs = argv.slice(1).join(' ');
  const encoder = new TextEncoder();

  if (process.platform === 'win32') {
    // PYTHONIOENCODING guards against UnicodeEncodeError when output is piped
    // on Windows, which the CLI hits with non-ASCII commit messages.
    const cmd =
      '@echo off\r\n' +
      'setlocal\r\n' +
      'set PYTHONIOENCODING=utf-8\r\n' +
      `"${interpreter}" ${extraArgs} "${cliPath}" %*\r\n` +
      'endlocal\r\n';
    await vscode.workspace.fs.writeFile(
      vscode.Uri.joinPath(binDir, 'fox.cmd'),
      encoder.encode(cmd)
    );

    // PowerShell prefers a .ps1 when one is present on PATH; without it, a bare
    // `fox` in the default VS Code shell would not resolve the .cmd reliably.
    const ps1 =
      '$env:PYTHONIOENCODING = "utf-8"\r\n' +
      `& "${interpreter}" ${extraArgs} "${cliPath}" @args\r\n` +
      'exit $LASTEXITCODE\r\n';
    await vscode.workspace.fs.writeFile(
      vscode.Uri.joinPath(binDir, 'fox.ps1'),
      encoder.encode(ps1)
    );
  } else {
    const sh =
      '#!/bin/sh\n' +
      'PYTHONIOENCODING=utf-8\n' +
      'export PYTHONIOENCODING\n' +
      `exec "${interpreter}" ${extraArgs} "${cliPath}" "$@"\n`;
    const target = vscode.Uri.joinPath(binDir, 'fox');
    await vscode.workspace.fs.writeFile(target, encoder.encode(sh));
    try {
      require('fs').chmodSync(target.fsPath, 0o755);
    } catch (err) {
      log(`Could not mark the launcher executable: ${err}`);
    }
  }

  return binDir.fsPath;
}

/** Put `fox` on PATH for every terminal VS Code opens from now on. */
function injectPath(context, binDir) {
  const collection = context.environmentVariableCollection;
  collection.persistent = true;
  collection.description =
    "Zanbeel: adds fox to PATH, and supplies FOXNEST_TOKEN and the server's certificate";
  collection.prepend('PATH', binDir + path.delimiter);
  log(`PATH now includes: ${binDir}`);
}

/** Export the signed-in token and the configured server to terminals. */
async function injectEnvironment(context) {
  const collection = context.environmentVariableCollection;

  const token = await context.secrets.get(TOKEN_KEY);
  if (token && config().get('injectToken')) {
    collection.replace('FOXNEST_TOKEN', token);
  } else {
    collection.delete('FOXNEST_TOKEN');
  }

  const server = (config().get('serverUrl') || '').trim();
  if (server) {
    collection.replace('FOXNEST_SERVER', server.replace(/\/+$/, ''));
  } else {
    collection.delete('FOXNEST_SERVER');
  }

  if (caBundlePath) {
    collection.replace('REQUESTS_CA_BUNDLE', caBundlePath);
  } else {
    collection.delete('REQUESTS_CA_BUNDLE');
  }
}

// ---------------------------------------------------------------------------
// Repository awareness
// ---------------------------------------------------------------------------

/**
 * The workspace folder that contains a .fox repository, if any.
 *
 * The CLI now walks up to find the repository root itself, so this only decides
 * where to *start* a terminal, not how commands resolve paths.
 */
async function findRepoFolder() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const folder of folders) {
    try {
      await vscode.workspace.fs.stat(vscode.Uri.joinPath(folder.uri, '.fox', 'config.json'));
      return folder;
    } catch {
      /* not a Zanbeel repository */
    }
  }
  return null;
}

/** Run a fox command directly (not via the terminal) and capture its output. */
async function fox(context, args, cwd) {
  if (!pythonArgv) return { ok: false, stdout: '', stderr: 'No Python interpreter', code: 1 };
  const token = await context.secrets.get(TOKEN_KEY);
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  if (token && config().get('injectToken')) env.FOXNEST_TOKEN = token;
  const server = (config().get('serverUrl') || '').trim();
  if (server) env.FOXNEST_SERVER = server.replace(/\/+$/, '');
  if (caBundlePath) env.REQUESTS_CA_BUNDLE = caBundlePath;

  return new Promise((resolve) => {
    cp.execFile(
      pythonArgv[0],
      [...pythonArgv.slice(1), bundledCli(context), ...args],
      { cwd, env, timeout: 60000, windowsHide: true },
      (error, stdout, stderr) => {
        resolve({
          ok: !error,
          code: error && typeof error.code === 'number' ? error.code : error ? 1 : 0,
          stdout: (stdout || '').toString(),
          stderr: (stderr || '').toString(),
        });
      }
    );
  });
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

async function refreshStatusBar(context) {
  if (!config().get('showStatusBar')) {
    if (statusItem) statusItem.hide();
    return;
  }
  const folder = await findRepoFolder();
  if (!folder) {
    if (statusItem) statusItem.hide();
    return;
  }
  if (!statusItem) {
    statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusItem.command = 'zanbeel.status';
    context.subscriptions.push(statusItem);
  }

  const result = await fox(context, ['status'], folder.uri.fsPath);
  const match = result.stdout.match(/Current commit:\s*\S+\s*\(([^)]+)\)/);
  const branch = match ? match[1] : 'no branch';
  statusItem.text = `$(git-branch) Zanbeel: ${branch}`;
  statusItem.tooltip = 'Zanbeel status — click for details';
  statusItem.show();
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

async function commandSignIn(context) {
  const server =
    (config().get('serverUrl') || '').trim() ||
    (await vscode.window.showInputBox({
      title: 'Zanbeel server',
      prompt: 'Where is the Zanbeel server?',
      value: DEFAULT_SERVER,
      ignoreFocusOut: true,
    }));
  if (!server) return;

  const username = await vscode.window.showInputBox({
    title: 'Zanbeel sign in',
    prompt: 'Username',
    value: (await context.secrets.get(USER_KEY)) || '',
    ignoreFocusOut: true,
  });
  if (!username) return;

  const choice = await vscode.window.showQuickPick(
    [
      { label: 'Password', detail: 'Sign in with your account password', id: 'password' },
      {
        label: 'Access token',
        detail: 'Paste an existing fxp_… token (Settings → Access tokens in the web UI)',
        id: 'token',
      },
    ],
    { title: 'How would you like to sign in?', ignoreFocusOut: true }
  );
  if (!choice) return;

  let token = null;

  if (choice.id === 'token') {
    token = await vscode.window.showInputBox({
      title: 'Zanbeel access token',
      prompt: 'Paste a token beginning with fxp_',
      password: true,
      ignoreFocusOut: true,
    });
    if (!token) return;
  } else {
    const password = await vscode.window.showInputBox({
      title: 'Zanbeel sign in',
      prompt: `Password for ${username}`,
      password: true,
      ignoreFocusOut: true,
    });
    if (!password) return;

    token = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Signing in to Zanbeel…' },
      async () => {
        const base = server.replace(/\/+$/, '');
        try {
          const response = await request(context, `${base}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
          });
          const body = await response.json().catch(() => ({}));
          if (!response.ok) {
            // The server rate-limits sign-in; surface its message rather than a
            // generic failure, because "try again in 15 minutes" is actionable.
            const detail =
              typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`;
            vscode.window.showErrorMessage(`Zanbeel sign-in failed: ${detail}`);
            return null;
          }
          if (!body.access_token) return null;

          // Keep a token named after this machine rather than the password
          // session: it shows up under Settings → Access tokens, can be revoked
          // on its own, and is what Sign Out revokes. The session is discarded.
          const minted = await request(context, `${base}/api/tokens`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${body.access_token}`,
            },
            body: JSON.stringify({
              name: `VS Code on ${os.hostname()}`.slice(0, 100),
              scopes: ['repo:write'],
              expires_in_days: 90,
            }),
          });
          if (minted.ok) {
            const created = await minted.json();
            return created.token || null;
          }
          // A server from before access tokens: fall back to the session.
          if (minted.status === 404 || minted.status === 405) return body.access_token;
          const detail = (await minted.json().catch(() => ({}))).detail;
          vscode.window.showErrorMessage(
            `Signed in, but could not create an access token: ${detail || `HTTP ${minted.status}`}`
          );
          return null;
        } catch (err) {
          vscode.window.showErrorMessage(`Could not reach ${server}: ${describeError(context, err)}`);
          return null;
        }
      }
    );
    if (!token) return;
  }

  // Verify before storing, so a mistyped token fails here rather than on the
  // user's next push.
  let unreachable = null;
  const verified = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Verifying credential…' },
    async () => {
      try {
        const response = await request(context, `${server.replace(/\/+$/, '')}/api/auth/me`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) return null;
        const body = await response.json();
        return body.user ? body.user.username : null;
      } catch (err) {
        unreachable = err;
        return null;
      }
    }
  );

  if (unreachable) {
    vscode.window.showErrorMessage(`Could not reach ${server}: ${describeError(context, unreachable)}`);
    return;
  }
  if (!verified) {
    vscode.window.showErrorMessage('That credential was rejected by the server.');
    return;
  }

  await context.secrets.store(TOKEN_KEY, token);
  await context.secrets.store(USER_KEY, verified);
  if (!(config().get('serverUrl') || '').trim()) {
    await config().update('serverUrl', server.replace(/\/+$/, ''), vscode.ConfigurationTarget.Global);
  }
  await injectEnvironment(context);

  log(`Signed in as ${verified}`);
  vscode.window
    .showInformationMessage(
      `Signed in to Zanbeel as ${verified}. Open a new terminal to pick up the credential.`,
      'Open Terminal'
    )
    .then((pick) => {
      if (pick === 'Open Terminal') vscode.commands.executeCommand('zanbeel.openTerminal');
    });
}

async function commandSignOut(context) {
  // Revoke on the server first: a sign-out that leaves the token valid is only
  // a sign-out from this window. Best effort -- an unreachable server must not
  // stop someone signing out locally.
  const token = await context.secrets.get(TOKEN_KEY);
  const server = (config().get('serverUrl') || '').trim().replace(/\/+$/, '');
  if (token && token.startsWith('fxp_') && server) {
    try {
      const response = await request(context, `${server}/api/tokens/current`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      log(response.ok ? 'Revoked the access token on the server' : `Token revoke: HTTP ${response.status}`);
    } catch (err) {
      log(`Could not revoke the token on the server: ${describeError(context, err)}`);
    }
  }
  await context.secrets.delete(TOKEN_KEY);
  await context.secrets.delete(USER_KEY);
  await injectEnvironment(context);
  log('Signed out');
  vscode.window.showInformationMessage(
    'Signed out of Zanbeel. Existing terminals keep the old credential until they are closed.'
  );
}

async function commandOpenTerminal(context) {
  const folder = (await findRepoFolder()) || (vscode.workspace.workspaceFolders || [])[0];
  const terminal = vscode.window.createTerminal({
    name: 'Zanbeel',
    cwd: folder ? folder.uri.fsPath : undefined,
    iconPath: new vscode.ThemeIcon('git-branch'),
  });
  terminal.show();
  terminal.sendText('fox --version', true);
}

async function commandStatus(context) {
  const folder = await findRepoFolder();
  if (!folder) {
    vscode.window.showWarningMessage('No Zanbeel repository in this workspace.');
    return;
  }
  const result = await fox(context, ['status'], folder.uri.fsPath);
  output.show(true);
  output.appendLine('');
  output.appendLine(result.stdout || result.stderr || '(no output)');
  await refreshStatusBar(context);
}

async function commandInstallDependencies(context) {
  if (!pythonArgv) {
    vscode.window.showErrorMessage('No Python interpreter found. Set zanbeel.pythonPath.');
    return;
  }
  const terminal = vscode.window.createTerminal({ name: 'Zanbeel: install dependencies' });
  terminal.show();
  // Run through the same interpreter the launcher uses, so the packages land
  // where the CLI will look for them rather than in some other environment.
  const quoted = pythonArgv.map((part) => (part.includes(' ') ? `"${part}"` : part)).join(' ');
  terminal.sendText(`${quoted} -m pip install --user requests cryptography`, true);
}

async function commandDiagnostics(context) {
  output.show(true);
  output.appendLine('');
  output.appendLine('=== Zanbeel diagnostics ===');

  output.appendLine(`  platform          : ${process.platform}`);
  output.appendLine(`  interpreter       : ${pythonArgv ? pythonArgv.join(' ') : 'NOT FOUND'}`);

  if (pythonArgv) {
    const version = await run(pythonArgv[0], [...pythonArgv.slice(1), '--version']);
    output.appendLine(`  python version    : ${(version.stdout || version.stderr).trim()}`);
    output.appendLine(`  requests present  : ${(await hasRequests(pythonArgv)) ? 'yes' : 'NO'}`);
  }

  output.appendLine(`  bundled CLI       : ${bundledCli(context)}`);
  const binDir = vscode.Uri.joinPath(context.globalStorageUri, 'bin').fsPath;
  output.appendLine(`  launcher dir      : ${binDir}`);
  output.appendLine(`  PATH injection    : ${config().get('injectPath') ? 'on' : 'off'}`);
  output.appendLine(`  token injection   : ${config().get('injectToken') ? 'on' : 'off'}`);

  const user = await context.secrets.get(USER_KEY);
  output.appendLine(`  signed in as      : ${user || 'nobody'}`);
  const server = (config().get('serverUrl') || '').trim().replace(/\/+$/, '');
  output.appendLine(`  server            : ${server || '(from repository)'}`);

  const pem = caCertificatePem(context);
  let certificate = 'NOT READABLE';
  if (pem) {
    try {
      const cert = new crypto.X509Certificate(pem);
      certificate = `${cert.subject.replace(/\n/g, ', ')}, valid until ${cert.validTo}`;
    } catch (err) {
      certificate = `unreadable (${err.message})`;
    }
  }
  output.appendLine(`  trusted cert      : ${caCertificateFile(context)}`);
  output.appendLine(`                      ${certificate}`);
  output.appendLine(`  CA bundle for fox : ${caBundlePath || 'none'}`);
  if (server) {
    let reach;
    try {
      const response = await request(context, `${server}/api/`);
      reach = response.ok ? 'yes, and its certificate is trusted' : `HTTP ${response.status}`;
    } catch (err) {
      reach = `NO: ${describeError(context, err)}`;
    }
    output.appendLine(`  server reachable  : ${reach}`);
  }

  const folder = await findRepoFolder();
  output.appendLine(`  repository        : ${folder ? folder.uri.fsPath : 'none in this workspace'}`);

  if (pythonArgv) {
    const version = await fox(context, ['--version'], folder ? folder.uri.fsPath : undefined);
    output.appendLine(`  fox --version     : ${(version.stdout || version.stderr).trim().split('\n')[0]}`);
  }

  // A different `fox` earlier on PATH would shadow ours and is worth naming.
  const which = await run(
    process.platform === 'win32' ? 'where' : 'which',
    ['fox'],
    { timeout: 8000 }
  );
  output.appendLine(
    `  fox on system PATH: ${which.ok ? which.stdout.trim().split('\n')[0] : 'none (expected)'}`
  );
  output.appendLine('=== end ===');
}

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

async function activate(context) {
  output = vscode.window.createOutputChannel('Zanbeel');
  context.subscriptions.push(output);
  log('Zanbeel extension activating');

  context.subscriptions.push(
    vscode.commands.registerCommand('zanbeel.signIn', () => commandSignIn(context)),
    vscode.commands.registerCommand('zanbeel.signOut', () => commandSignOut(context)),
    vscode.commands.registerCommand('zanbeel.openTerminal', () => commandOpenTerminal(context)),
    vscode.commands.registerCommand('zanbeel.status', () => commandStatus(context)),
    vscode.commands.registerCommand('zanbeel.diagnostics', () => commandDiagnostics(context)),
    vscode.commands.registerCommand('zanbeel.installDependencies', () =>
      commandInstallDependencies(context)
    )
  );

  pythonArgv = await resolvePython();

  if (!pythonArgv) {
    log('No suitable Python interpreter found');
    vscode.window
      .showErrorMessage(
        'Zanbeel needs Python 3.8+ to run the fox CLI, and none was found.',
        'Open Settings',
        'Show Diagnostics'
      )
      .then((pick) => {
        if (pick === 'Open Settings') {
          vscode.commands.executeCommand('workbench.action.openSettings', 'zanbeel.pythonPath');
        } else if (pick === 'Show Diagnostics') {
          vscode.commands.executeCommand('zanbeel.diagnostics');
        }
      });
    return;
  }

  if (config().get('injectPath')) {
    try {
      const binDir = await writeLauncher(context, pythonArgv, bundledCli(context));
      injectPath(context, binDir);
    } catch (err) {
      log(`Could not set up the launcher: ${err}`);
      vscode.window.showErrorMessage(`Zanbeel could not set up the fox launcher: ${err.message}`);
    }
  }

  caBundlePath = await writeCaBundle(context).catch((err) => {
    log(`Could not write the CA bundle: ${err}`);
    return null;
  });
  await injectEnvironment(context);

  // Checked after PATH is wired up, so the offer to install lands on a terminal
  // where `fox` already resolves.
  if (!(await hasRequests(pythonArgv))) {
    log('requests is missing from the resolved interpreter');
    vscode.window
      .showWarningMessage(
        'Zanbeel: the fox CLI needs the "requests" package, which is not installed for the detected Python.',
        'Install Now',
        'Not Now'
      )
      .then((pick) => {
        if (pick === 'Install Now') vscode.commands.executeCommand('zanbeel.installDependencies');
      });
  }

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(async (event) => {
      if (!event.affectsConfiguration('zanbeel')) return;
      pythonArgv = (await resolvePython()) || pythonArgv;
      if (config().get('injectPath') && pythonArgv) {
        const binDir = await writeLauncher(context, pythonArgv, bundledCli(context));
        context.environmentVariableCollection.clear();
        injectPath(context, binDir);
      } else if (!config().get('injectPath')) {
        context.environmentVariableCollection.clear();
      }
      caBundlePath = await writeCaBundle(context).catch(() => null);
      await injectEnvironment(context);
      await refreshStatusBar(context);
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => refreshStatusBar(context))
  );

  await refreshStatusBar(context);
  log('Zanbeel extension ready');
}

function deactivate() {
  // environmentVariableCollection is cleaned up by VS Code on uninstall.
}

module.exports = { activate, deactivate };
