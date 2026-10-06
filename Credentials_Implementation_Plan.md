# Credentials — Implementation Plan and Record

**Document status:** Implemented on this branch
**Branch:** `Credentials` (from `main` after PR #6)
**Roadmap workstream:** Credentials — "Scoped revocable access tokens; SSH public-key authentication"
**Gap named in the roadmap:** "The CLI authenticated by sending the user's actual password, so a compromised machine meant a password rotation… No key-based authentication."
**Week-3 checkpoint:** "Machines authenticate with scoped credentials rather than passwords."
**Guiding rule:** where a choice exists, do what Git, GitHub and OpenSSH do.

---

## 1. Starting point

The 2.0.1 baseline (`a52911c`) already had most of the parts:

- Personal access tokens (`fxp_…`): named, stored as SHA-256 only, scopes `repo:read` / `repo:write` / `admin`, expiry, revocation, `last_used_at`. A central check refused write methods to read-only tokens, and `require_admin_user` asked for the admin scope.
- SSH keys: registration with OpenSSH fingerprints, and a challenge-response sign-in (single-use nonce bound to a username).
- Web panels for both under Settings, and service-level unit tests (28 for tokens, 34 for keys).

So, as with the merge-conflict work, the features existed. What was missing was any check of the paths around them, and the canonical CLI did not use them at all.

## 2. Problems found

Each critical and high finding below was reproduced against the original code. §6.5 has the test that proves it.

| # | Problem | Where | Severity |
|---|---|---|---|
| 1 | A token could create tokens, including one with no expiry or wider scopes, so revoking or expiring the original changed nothing | `POST /api/tokens` | Critical |
| 2 | A token could register an SSH key and sign in with it. The sign-in returned an unscoped session with the account's full powers, so a `repo:read` token became the whole account | `POST /api/ssh-keys`, `/api/auth/ssh/verify` | Critical |
| 3 | A token could change the account's email (then reset the password to the new address) or change the password outright | `PUT /api/profile`, `PUT /api/users/{u}`, `/api/auth/change-password` | Critical |
| 4 | Role-based admin power ignored token scopes. A team lead's `repo:write` token could create a server-wide webhook (every push on the server sent to any URL), set server-wide push rules, edit other users, push commits as another author and create repositories in someone else's name | `webhooks.py`, `server_hooks.py`, `users.py`, `commits.py`, `repositories.py` | High |
| 5 | An SSH sign-in could not be revoked: removing the key left the 12-hour session working | `ssh_keys.py` | High |
| 6 | The client signed raw bytes the server chose. That makes it a signing oracle for the user's SSH key, the same key that signs their SSH logins and git commits. ECDSA P-384/P-521 were verified with SHA-256, and keys in ssh-agent or behind a passphrase could not be used | `client/fox.py`, `ssh_keys.py` | High |
| 7 | The canonical CLI (`client/client1/fox.py`) had no token or SSH commands. `fox login` sent the password and stored a 12-hour session, and on every 401 it asked for the password again. It ignored `FOXNEST_TOKEN`/`FOXNEST_SERVER`, so the VS Code extension's credential injection did nothing | `client/client1/fox.py` | High |
| 8 | `fox logout` and the extension's Sign Out only deleted the local copy; the token stayed valid on the server | CLI, extension | Medium |
| 9 | `_authed_put` called `prompt_login_and_retry()`, which does not exist, so any 401 on a PUT crashed the CLI | `client/client1/fox.py` | Medium |
| 10 | No way for an administrator to see who holds which credentials, or to cut one person's machines off without disabling the account | — | Medium |
| 11 | Nobody was told when a token or key was added to their account, which is the first thing an intruder does | — | Medium |
| 12 | The unauthenticated challenge endpoint created rows without limit | `create_challenge` | Low |
| 13 | 1024-bit RSA keys were accepted | `parse_public_key` | Low |
| 14 | The credential screens read the server's naive-UTC timestamps as local time ("used 5h ago" for a key used a moment ago, in UTC+5) | `AccessTokensPanel`, `SSHKeysPanel` | Low |
| 15 | Nothing tested scopes over HTTP. The service tests passed with problems 1–5 open | tests | — |

## 3. Decisions

### 3.1 Changing credentials or identity needs a password sign-in

GitHub's "sudo mode" is the model. Creating a token, adding or removing an SSH key, changing the password, and changing an email all need a request signed in with a password. Any token or SSH sign-in is refused with HTTP 403 and the header `X-Zanbeel-Credential-Required: password`, so a client can ask for the password instead of failing. Two cases are always allowed:

- A token may revoke itself (`DELETE /api/tokens/current`, or its own id). A logout that leaves the token valid is not a logout.
- Revoking a *different* token needs the password, so a stolen token cannot lock its owner's other machines out.

### 3.2 Admin powers from a role need the `admin` scope

`has_admin_powers(request, user)` = privileged role **and** (password session **or** a token with `admin`). It applies to server-wide hooks, editing other users, pushing another author's commits, and creating repositories for someone else.

Repository-level role privileges stay under `repo:write`, which matches GitHub's classic `repo` scope: "whatever you can do to repositories".

### 3.3 An SSH sign-in returns a short-lived token tied to the key

`/api/auth/ssh/verify` returns an ordinary `fxp_` access token. It has the key's scopes, lasts `FOXNEST_SSH_SESSION_HOURS` (default 8), and records which key issued it (`access_tokens.ssh_key_id`). Because it is an access token, every rule that limits tokens limits key sign-in too. Removing the key deletes its tokens in the same transaction.

Keys now carry scopes like tokens; existing keys get `repo:write` on upgrade. The scopes are re-checked against the user's role at each sign-in, so a demoted lead's old admin key stops yielding `admin`.

### 3.4 Signatures are OpenSSH SSHSIG, in a namespace the client fixes

The client signs `ssh-keygen -Y sign`'s format in the `zanbeel-auth` namespace. The namespace is fixed in the client and never taken from the server, so a signature made for Zanbeel cannot be valid as an SSH login or a git signature.

The client signs unencrypted keys itself with `cryptography`. For everything else it calls `ssh-keygen -Y sign`: passphrase-protected keys (ssh-keygen prompts), keys only in ssh-agent (`--key key.pub`), and hardware keys.

The server checks:

- the namespace;
- that the signature algorithm matches the key type;
- the per-curve hash for ECDSA;
- that `ssh-rsa`/SHA-1 is refused.

It looks the key up by fingerprint and requires it to belong to the user the nonce was issued for.

Raw signatures are still accepted, but only so the older CLI bundled with the VS Code extension keeps working. Accepting them is safe for the server; the risk was in the client that produced them.

### 3.5 `fox login` trades the password for a token for this machine

The password is sent once. The CLI creates `fox CLI on <hostname>` (`repo:write`, plus `admin` for team leads, 90 days; `--scope` and `--expires-in-days` override this) and saves only that token. Logging in again retires the previous token for the same machine, user and server.

The VS Code extension does the same (`VS Code on <hostname>`, `repo:write`), and its Sign Out revokes the token on the server.

### 3.6 Administrators can see and revoke, never create

`GET /api/admin/credentials` lists live tokens and keys per person (metadata only). `POST /api/admin/users/{u}/credentials/revoke` ends all of them and leaves the account usable with the password. There is still no way to create a credential for someone else.

## 4. Phases

1. **Server rules.** Password-session boundary, admin scope on role checks, `/api/tokens/current`, credential details on `/api/auth/me`.
2. **SSH.** SSHSIG verification (`sshsig.py`), key scopes, tokens tied to keys, challenge cap, RSA ≥ 2048.
3. **CLI.** Machine-token login, `whoami`, `token`, `ssh`, automatic SSH renewal, `FOXNEST_TOKEN`/`FOXNEST_SERVER`, a single 401 recovery path.
4. **Web.** Scoped keys, token expiry presets, admin Credentials tab, notification types, UTC display fix.
5. **Extension.** Machine token on sign-in; revoke on sign-out.
6. **Verification.** Unit, HTTP and CLI tests, a live end-to-end harness, a browser harness, and the full suite.

## 5. Out of scope

- **Password sessions are still stateless.** A web session stays valid until it expires (`FOXNEST_TOKEN_EXPIRY_HOURS`, default 12); changing the password does not sign other browsers out. "Sign out everywhere" needs a per-user session epoch.
- **Fine-grained tokens.** Scopes cover kinds of action, not particular repositories.
- **Binding a signature to a server.** The namespace stops a signature being reused in another protocol, but not a malicious Zanbeel server relaying a challenge from a real one. That needs channel binding or a configured server identity.
- **The older CLI copies.** `client/fox.py`, `release/fox.py` and the CLI bundled with the extension were not synced; that one still signs raw challenges, which stay accepted.
- **Two-factor authentication.** Deferred by the roadmap.
- **CLI output on a non-UTF-8 Windows console.** When fox's output is piped or redirected on a cp1252 console, it crashes on the first non-ASCII character it prints (`fox help` on its `→`, `fox push` on its emoji). This predates this work and is unchanged. It matters for Windows build agents, which usually log to a file: set `PYTHONIOENCODING=utf-8` there until the CLI handles it.
- **Other screens' timestamps.** The naive-UTC display fix (#14) covers the three credential views. Other screens that parse server times with a bare `new Date()` were not audited.

## 6. Implementation record

All phases are implemented on this branch.

### 6.1 API surface

| Method | Path | Change |
|---|---|---|
| POST | `/api/tokens` | Needs a password sign-in. Owner is notified |
| GET | `/api/tokens` | Leaves out tokens issued by SSH sign-ins. Rows carry `kind` and `ssh_key_id` |
| DELETE | `/api/tokens/current` | **New.** Revokes the token making the request; any token may do this |
| DELETE | `/api/tokens/{id}` | A token may revoke itself; revoking any other token needs a password sign-in |
| GET | `/api/auth/me` | **New field `credential`:** `session` / `access_token` / `ssh_key`, with name, scopes, expiry and key |
| POST | `/api/ssh-keys` | Needs a password sign-in. Accepts `scopes` (default `repo:write`). Refuses RSA under 2048 bits. Owner is notified |
| DELETE | `/api/ssh-keys/{id}` | Needs a password sign-in. Ends every sign-in the key made |
| GET | `/api/ssh-keys` | Rows carry `scopes` and `active_sessions`. Response adds `session_hours` |
| POST | `/api/auth/ssh/challenge` | Adds `namespace` and `signature_format`. At most 5 outstanding challenges per username |
| POST | `/api/auth/ssh/verify` | Accepts SSHSIG (or the legacy raw format). Returns a key-bound `fxp_` token with `expires_at`, `scopes`, `key` and `signature_format` |
| GET | `/api/admin/credentials` | **New.** Live tokens and keys per person, metadata only |
| POST | `/api/admin/users/{u}/credentials/revoke` | **New.** Revokes every token and removes every key; the user is notified |
| POST | `/api/auth/change-password` | Needs a password sign-in |
| PUT | `/api/profile`, `/api/users/{u}` | Changing an email needs a password sign-in. Editing another user needs admin powers |
| * | `/api/webhooks`, `/api/server-hooks` | Role-based management needs admin powers |
| POST | `/api/repository/{id}/push`, `/api/repository/create` | Acting as another user needs admin powers |

### 6.2 Schema

- `ssh_keys.scopes` (`VARCHAR(200) NOT NULL DEFAULT 'repo:write'`).
- `access_tokens.ssh_key_id` (indexed; `ON DELETE CASCADE` where foreign keys are enforced).

`ensure_credential_columns()` adds both in place at startup. It is idempotent, and is covered by a unit test and by the live upgrade scenario in §6.5.

New setting: `FOXNEST_SSH_SESSION_HOURS` (default 8).

### 6.3 CLI (`client/client1/fox.py`)

```text
fox login                          password -> token for this machine; the password is not stored
fox login --scope repo:read --expires-in-days 30
fox login --token fxp_...          save a token made elsewhere (checked first)
fox login --ssh [--key PATH]       sign in with a key; renews itself
fox logout                         revoke this machine's token on the server, then forget it
fox whoami                         user, credential type, scopes, expiry, source
fox token create NAME [--scope S]... [--expires-in-days N] [--use]
fox token list [--all]             marks the token this machine uses
fox token revoke ID
fox token use TOKEN
fox ssh add TITLE [--key PUB] [--scope S]...
fox ssh list                       scopes, live sign-ins, marks this machine's key
fox ssh remove ID
FOXNEST_TOKEN=fxp_... FOXNEST_SERVER=http://host:33333 fox pull     (CI: nothing written to disk)
```

How the commands behave:

- Commands that change credentials ask for the password: hidden at a terminal, or one line of stdin when piped.
- An SSH sign-in that has run out is renewed before the next request. A token the server rejects is renewed (SSH) or reported (token) instead of crashing.
- The credentials file is created mode 600 on POSIX.

### 6.4 Web

- **Settings → Access tokens:**
  - read-only / read-and-write choice, with an `admin` box for team leads;
  - expiry presets defaulting to 90 days, with a warning on "Never";
  - a "No expiry" badge on such tokens;
  - the one-time reveal shows the CI and `fox login --token` usage;
  - errors appear inside the dialog.
- **Settings → SSH keys:** the same access choice; scope badges and live sign-in count on each row; the warning before removing a key says how many sign-ins it will end.
- **Operations → Credentials (new, admin):** who holds what, with **Revoke all**.
- **Notifications:** new token, new key, credentials revoked.

### 6.5 Verification

| Suite | What it covers | Result |
|---|---|---|
| `tests/test_sshsig.py` | Every key type round-trips. Fails on wrong message, wrong or relabelled namespace, swapped key, tampered bytes, SHA-1 RSA, mismatched curve, unknown hash. Server accepts real `ssh-keygen -Y sign` output; real `ssh-keygen -Y verify` accepts the CLI's signatures (ed25519, RSA 3072, P-256/384/521). A passphrase key fails fast without a terminal | 17 tests, 30 subtests pass |
| `tests/test_credentials_api.py` | Over HTTP: every escalation in §2 is refused (and allowed where it should be), self-revocation, admin scope, key sign-in scopes, expiry, removal, legacy format, demoted lead, cleanup, challenge cap, weak RSA, admin view and Revoke all, notifications, upgrade migration | 32 pass. **Against the original server code, 28 of the 32 fail** — the holes were real |
| `tests/test_fox_cli_credentials.py` | The real CLI against the real API in-process: machine-token login, re-login retires the old token, logout revokes, whoami, token create/use/list/revoke with the password (including a read-only machine revoking its own token), `FOXNEST_TOKEN`, SSH add/login/renew-on-expiry/re-sign on 401/removal, PUT 401 no longer crashes, password from stdin, exit codes | 24 pass |
| `tests/test_access_tokens.py`, `tests/test_ssh_keys.py`, `tests/test_endpoint_auth.py` | Existing tests, unchanged | 70 pass |
| `tools/e2e_credentials.py` | Live server and the real CLI, each "machine" with its own home directory, keys from real `ssh-keygen`: login and push with a machine token; a read-only build agent on `FOXNEST_TOKEN`; revocation and logout; SSH add/login/push/renew/read-only RSA key/removal; escalation attempts; admin Revoke all; the extension's older CLI still signs in; upgrade of a pre-scope database | **58/58 checks** |
| `tools/e2e_credentials_ui.py` | Real Chrome: create a token (one-time reveal) and a read-only key, see a pasted private key refused. Between phases, use both from outside the browser (HTTP and the CLI). Then see the live sign-in, remove the key, revoke the token, read the notifications, and Revoke all as admin. Server state checked afterwards; no console errors | **41/41 checks** |
| `tools/e2e_merge_conflicts.py` | Its CLI scenario now signs in through the machine-token login | 84/84 checks |
| Frontend | `eslint` clean on the changed files; production build passes | pass |
| Full server suite | Everything in `server/tests` | **774 passed, 0 failed** (19 min) |

The full suite collected its tests just before two small final edits: `fox token revoke` uses `/api/tokens/current` for the machine's own token, and an explicit `"email": null` on `PUT /api/profile` is no longer treated as a change. Both were re-verified afterwards:

- credential API and CLI tests: 56 passed, including a new test for the first edit;
- `tests/test_profiles.py`: 25 passed;
- the live harness: 58/58.

The SSH agent path was also checked by hand: Git's OpenSSH agent held the key and only the `.pub` was on disk. The CLI signed through the agent and the server verified the signature.
