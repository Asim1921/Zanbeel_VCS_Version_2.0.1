# Zanbeel VS Code Extension — Planning

**Status:** **Option A chosen and built.** v0.1.0 packages, installs and works; see §11 for what shipped.
**Goal:** replace per-machine installation of the `fox` client with a VS Code extension, so that installing the extension is the only step a developer takes, and every `fox` command works in the VS Code terminal immediately afterwards.

**Decision (2026-09-11):** Python is a standing requirement on developer machines regardless, so **Option A** — ship `fox.py` and use the machine's interpreter — was selected over the PyInstaller route. The extension detects Python, offers to `pip install` the one missing package, and keeps the CLI as a single source of truth. The result is a **67 KB** extension rather than a 15–40 MB one per platform, and no cross-platform build pipeline.

---

## 1. The problem today

Getting a developer onto Zanbeel currently requires all of this, on every machine:

1. Install Python 3.8+ and put it on `PATH`
2. `pip install requests` (hard dependency of `fox.py`)
3. `pip install cryptography` if they want `fox ssh login`
4. Download `fox.py` from `GET /api/download/client`
5. Download or write a `fox.bat` shim from `GET /api/download/fox.bat`
6. Put that shim somewhere on `PATH`
7. `fox login`

Steps 1–6 are the ones that go wrong: wrong Python, no Python, `pip` behind a proxy, `PATH` not refreshed until logout, a `fox.bat` pointing at a `fox.py` that has since moved. The repository already carries the scar tissue — `download-client-windows.ps1`, `tools/patch_fox_windows.ps1`, and three separate installer scripts under `client/client1/`.

An extension collapses steps 1–6 into "click Install".

---

## 2. What exists today (verified, not assumed)

| Fact | Detail |
|---|---|
| CLI size | `client/fox.py` — **6,628 lines**, duplicated byte-for-byte at `release/fox.py` |
| Commands | **78 subparsers** across ~25 top-level groups: `init add commit push pull status log branch checkout merge tag release pr token ssh webhook status-check hook search admin config gc docs login logout link set` |
| Hard dependency | `requests` (top-level import) |
| Lazy dependencies | `cryptography` (only `fox ssh login`), local `docs_generator` / `llm_helper` (only `fox docs`) |
| Credentials | `~/.foxnest/credentials.json` — **plaintext**, global, shared by every workspace |
| Repo state | `.fox/` directory, resolved as a **relative path** |
| Packaging | PyInstaller specs already exist (`client/client1/fox.spec`, `fox_installer.spec`); PyInstaller is **not currently installed** |
| Server-side distribution | `GET /api/download/client`, `GET /api/download/fox.bat` |
| Build toolchain available | Node v22.23.2, npm 10.9.8 — sufficient to build and package an extension |
| Existing extension code | none |

### Two limitations worth fixing while we are here

**`fox` does not find the repository root.** `check_repository()` only tests the current directory for `.fox`; it never walks up the way `git` does. So `cd src && fox status` prints *"Fatal: src not a repository"*. Inside VS Code this matters more than on a bare terminal, because a developer's integrated terminal is frequently opened in a subfolder.

**Credentials sit in plaintext on disk.** `~/.foxnest/credentials.json` holds a live token in the clear. VS Code offers `SecretStorage`, which is backed by the OS keychain (DPAPI / Keychain / libsecret). Moving to it is a real security improvement and is essentially free once the extension owns the login flow — but it requires the CLI to accept a token from the environment (§6).

---

## 3. The key enabler: can an extension really put `fox` in the terminal?

Yes, and this is the mechanism the whole plan rests on.

VS Code exposes `ExtensionContext.environmentVariableCollection`. An extension can do:

```ts
const bin = vscode.Uri.joinPath(context.extensionUri, 'bin').fsPath;
context.environmentVariableCollection.prepend('PATH', bin + path.delimiter);
context.environmentVariableCollection.persistent = true;
context.environmentVariableCollection.description = 'Adds fox to PATH';
```

Every terminal VS Code spawns from then on inherits the modified `PATH`. Properties that matter here:

- **No admin rights, no system `PATH` change.** The mutation applies to VS Code's terminals only.
- **It survives a reload** when `persistent` is set, and VS Code shows the user a badge explaining which extension changed their environment.
- **It is reverted automatically on uninstall.** No orphaned entries.

This is exactly the ask — "once the extension is installed, execute all the commands directly from the VS Code terminal" — and it is a supported API rather than a hack.

**This assumption is the single point of failure for the plan, so Phase 0 exists to prove it before anything else is built.**

---

## 4. What ships in `bin/` — three options

This is the real decision. The extension has to put *something* executable on `PATH`.

### Option A — ship `fox.py` and rely on the machine's Python  ← **chosen and built**

- Extension is tiny — the packaged VSIX is **67 KB**.
- Requires Python and `requests` on every machine. That was the original objection, but Python is a standing requirement for this team anyway, so it removes steps 3–6 of §1 and reduces step 2 to a prompt the extension raises itself.
- **The CLI stays the single source of truth** — the same benefit Option B claimed, without the build pipeline.
- No per-platform builds, no code-signing, no Gatekeeper or antivirus problems, and updating the CLI means replacing one file.
- Verdict: **selected.** The Python prerequisite is acceptable here; the packaging complexity of B was not worth avoiding it.

### Option B — ship a standalone binary built with PyInstaller  *(not taken)*

- `fox.exe` / `fox` built per platform, bundled in the extension, no Python on the developer's machine at all.
- **The CLI stays the single source of truth.** One implementation of the ignore engine, the pack format, the delta cache and the hashing — which matters, because the ignore engine already has to agree byte-for-byte with the server's copy, and a third implementation would be a third thing to keep in sync.
- VS Code supports **platform-targeted VSIX packages**, so a Windows user downloads only the Windows binary rather than all three.
- Costs: an estimated 15–40 MB per platform (to be measured in Phase 0), and a CI job that builds on Windows, macOS and Linux — PyInstaller cannot cross-compile.
- The specs in `client/client1/` mean this was already attempted once; worth reading before rebuilding.

### Option C — port the CLI to TypeScript

- The extension host *is* Node, so there would be no runtime dependency and the smallest possible download.
- Costs 6,628 lines of port, and leaves two implementations of every format rule to keep in agreement. Given that the ignore engine alone previously had to be diffed across 25 paths to prove client and server matched, tripling that surface is a poor trade for a size saving.
- Verdict: **not for v1.** Revisit only if binary size proves unacceptable.

**Outcome: Option A.** Option B remains the fallback if the Python prerequisite ever becomes a problem — nothing in the extension's design would have to change except what lands in the launcher.

---

## 5. Architecture

```
zanbeel-vscode/
  package.json              contributes: commands, settings, menus
  src/
    extension.ts            activate(): PATH injection, token injection
    cli.ts                  locate + invoke the bundled binary
    auth.ts                 login flow -> SecretStorage
    repo.ts                 find the .fox root from any open file
    statusBar.ts            branch + pending-change indicator
  bin/
    fox.exe | fox           the PyInstaller build for this platform
  .github/workflows/
    build.yml               matrix build: win32-x64, darwin-arm64, linux-x64
```

**Activation:** on `onStartupFinished`, or earlier if a `.fox` directory is present in the workspace.

**Command surface:** the terminal is the primary interface, as requested. The Command Palette entries are a thin convenience layer over the same binary — they are not a second implementation.

---

## 6. Changes needed outside the extension

Small, and each is independently useful to the existing CLI.

| Change | Where | Why |
|---|---|---|
| Walk up to find `.fox` | `client/fox.py` `check_repository()` / `fox_dir` | So commands work from a subfolder, as git does. Fixes a real bug for terminal users too. |
| Honour `FOXNEST_TOKEN` from the environment | `client/fox.py` `get_auth_headers()` | Lets the extension inject the token from the OS keychain via `environmentVariableCollection`, so no plaintext credential file is needed. Falls back to the existing file when unset. |
| Honour `FOXNEST_SERVER` from the environment | `client/fox.py` `_resolve_server_url()` | Lets the extension set the server per workspace rather than globally. |
| Report a version | `fox --version` and the server's `/` response | The extension must be able to warn when the bundled CLI is older than the server expects. |
| Publish a compatibility floor | server `/` | So the extension can say *"this server needs extension ≥ 1.2"* rather than failing obscurely. |

Note the first two also remove the need for `download-client-windows.ps1` and `tools/patch_fox_windows.ps1` once the extension is the supported install path.

---

## 7. Phases

### Phase 0 — Spike ✅ **done**

Both assumptions were proven before anything was built on them:

1. `environmentVariableCollection.prepend('PATH', …)` does make a bundled launcher callable as a bare `fox`.
2. Option A was chosen, so the PyInstaller sizing question fell away.

**Result:** 10/10 automated checks — interpreter discovery, launcher generation, `fox --version` and `fox --help` through an injected `PATH`, `fox status` at the repository root *and* from `server/app`, and `FOXNEST_TOKEN` proven to take precedence over the credentials file by having the server reject a deliberately invalid injected token.

### Phase 1 — Minimum viable extension ✅ **done**

Shipped as `zanbeel-vcs-0.1.0.vsix` (67 KB), installed and verified. See §11.

### Phase 2 — Distribution ✅ **server-side done**

Option A removed the need for a per-platform build matrix. Delivered:

- **`GET /api/download/extension`** serves the newest `.vsix` on the server; **`GET /api/download/extension/info`** reports its version, size and build time so a client can tell whether it is behind without downloading megabytes to find out. Both unauthenticated, for the same reason the client download is: a machine needs the extension *before* it can obtain a credential.
- **Fixed `GET /api/download/client`, which had been returning 404.** Its path arithmetic (`Path(__file__).parent.parent`) lost a level when the single-file server was split into `app/`, so it resolved to `server/app/api/client` — a directory that does not exist. The repository root is now computed once and named, so it cannot drift again.
- Verified by downloading over the LAN and installing: archive integrity ok, all three payload files present, `code --install-extension` succeeded.

Remaining: a version-compatibility check on activation, and deciding between marketplace publication and self-hosting (§12).

### Phase 3 — Editor integration (5–7 days, optional)

Beyond the terminal ask, and only worth doing once Phases 1–2 are stable:

- Command Palette entries for the common commands, with input prompts
- SCM provider (`vscode.scm.createSourceControl`) — changed files in the sidebar, stage and commit without typing
- Inline diff against the last commit
- Pull-request and review surfacing

---

## 8. Risks

Choosing Option A removed the four largest ones — binary size, code signing, Gatekeeper and the cross-platform build matrix. What remains:

| Risk | Impact | Mitigation |
|---|---|---|
| No Python, or the wrong Python | Extension cannot run the CLI | Detects `py -3.11`, `py -3`, `python3`, `python`; requires 3.8+; `zanbeel.pythonPath` overrides. Fails with a clear message and a link to settings, not silently |
| `requests` missing from the detected interpreter | Every command fails | Checked on activation; the extension offers to install it *through the same interpreter*, so the package lands where the CLI will look |
| Bundled CLI drifts from the server | Confusing failures | **Open.** Needs a version floor published by the server and checked on activation — Phase 2 |
| A stale system-wide `fox` shadows the bundled one | Wrong CLI runs silently | The extension **prepends** to `PATH`, and *Run Diagnostics* reports any other `fox` it finds |
| Terminal already open when the extension activates | `fox` not found in that terminal | Documented in the README; *Zanbeel: Open Terminal* always gives a correctly configured one |
| `FOXNEST_TOKEN` readable by any process started from a VS Code terminal | Token exposure to local processes | Deliberate trade for keeping it out of a plaintext file. Documented in the README and the setting's description; `zanbeel.injectToken` turns it off |

---

## 9. Out of scope

- Replacing the web UI. The extension is a client for the CLI, not a second front end.
- Supporting editors other than VS Code. Cursor and VS Code forks inherit it for free; JetBrains would be a separate project.
- Rewriting the CLI in TypeScript (see §4, Option C).
- Offline/air-gapped installation of the extension itself.

---

## 10. Decisions still open

Two of the original four were settled by choosing Option A (platforms and code signing no longer matter — one VSIX runs everywhere Python does).

1. **How is the extension distributed?** Serving the `.vsix` from the Zanbeel server reuses the existing `/api/download/*` endpoints and keeps it internal. The alternative is a private marketplace.
2. **Do we keep the standalone CLI as a supported install path?** Build agents and the server itself still need the bare CLI, so probably "both, but the extension is what humans use." If so, `download-client-windows.ps1` and `tools/patch_fox_windows.ps1` can be retired.

---

## 11. What shipped in v0.1.0

**Location:** `vscode-extension/` — packaged as `zanbeel-vcs-0.1.0.vsix` (67 KB), installed and verified as `zanbeel.zanbeel-vcs@0.1.0`.

| Piece | Detail |
|---|---|
| `PATH` injection | Generates a `fox` launcher into the extension's **global storage** — not its install directory, which is not reliably writable and is replaced wholesale on update — then prepends that directory to the terminal `PATH` |
| Windows launcher | Both `fox.cmd` and `fox.ps1`. PowerShell is the default VS Code shell and prefers a `.ps1`, so shipping only the `.cmd` would have resolved unreliably. Both set `PYTHONIOENCODING=utf-8`, which the CLI needs when output is piped |
| Interpreter discovery | `py -3.11` → `py -3` → `python` → `python3`, requiring 3.8+; overridable, and accepts a form like `py -3.11` rather than only a bare path |
| Dependency check | Verifies `requests` imports; offers to install it through the same interpreter so it lands where the CLI will look |
| Sign in | Password or paste-a-token; **verified against `/api/auth/me` before being stored**, so a bad credential fails at sign-in rather than on the next push. Surfaces the server's rate-limit message verbatim, because "try again in 15 minutes" is actionable |
| Credential storage | VS Code `SecretStorage` (OS keychain), exported as `FOXNEST_TOKEN` |
| Commands | Sign In, Sign Out, Open Terminal, Status, Install Python Dependencies, Run Diagnostics |
| Diagnostics | Reports interpreter, Python version, whether `requests` is present, launcher location, signed-in user, repository, and **any other `fox` on the system `PATH`** that could be shadowing this one |
| Status bar | Current branch, click for full status |

### CLI changes that went with it

Both were prerequisites, and both are independently useful to anyone using the bare CLI:

- **`find_repository_root()`** — walks up for `.fox` the way git does. `cd server/app && fox status` now works. `fox init` deliberately does *not* discover, so creating a nested repository stays a deliberate act rather than silently re-initialising the parent. Paths the user typed are remapped onto the root, so `fox add main.py` from `src/` stages `src/main.py`.
- **`FOXNEST_TOKEN` / `FOXNEST_SERVER`** — read from the environment ahead of the credentials file and the repository origin. Falls back to the previous behaviour when unset.

**Verification:** 15/15 CLI-fix tests, 10/10 launcher and PATH-injection tests, plus the shipped copy confirmed working from a subfolder of the real repository with an injected token.

---

## 12. Publishing: marketplace versus self-hosting

The goal — *"install it from anywhere, on any computer"* — has two readings, and they lead to different places.

### The thing to know first

**The VS Code Marketplace has no private or internal option.** Publishing there makes the extension world-visible and world-installable. There is no "only my organisation" setting. Open VSX, the open-source alternative registry, is public for the same reason.

So the choice is not "marketplace = convenient, self-host = awkward". It is:

| | Reach | Who can see the code |
|---|---|---|
| **Marketplace** | Any computer with internet | **Everyone.** The `.vsix` bundles `fox.py` — all 6,700 lines of the Zanbeel client — and anyone can download and unzip it |
| **Self-hosted** (built) | Any computer that can reach the Zanbeel server | Only people who can reach the server |

Zanbeel is internal infrastructure and the extension ships its client source, so this is a business decision rather than a technical one. Worth deciding deliberately rather than by default.

### If the answer is "internal only" — already done

`GET /api/download/extension` serves it today. To reach it from outside the office, the constraint moves to the server rather than the extension: expose Zanbeel over VPN, or host it somewhere reachable. The extension needs no change either way.

### If the answer is "publish publicly"

Five steps, roughly an hour the first time:

1. **Azure DevOps organisation.** Sign in at `dev.azure.com` with a Microsoft account — free, and the marketplace identity is built on it.
2. **Personal Access Token.** In Azure DevOps → User settings → Personal access tokens → New. Set **Organization: All accessible organizations** and **Scopes: Marketplace → Manage**. Both matter; the token is rejected for publishing otherwise.
3. **Create a publisher** at `marketplace.visualstudio.com/manage`. The ID you choose must match the `publisher` field in `package.json` — currently `zanbeel`, which would have to be claimed or changed.
4. **Log in and publish:**
   ```
   cd vscode-extension
   npx @vscode/vsce login zanbeel
   npx @vscode/vsce publish
   ```
5. **Install from anywhere** with `code --install-extension zanbeel.zanbeel-vcs`, or by searching "Zanbeel" in the Extensions panel.

Before publishing, three things the package currently skips:

- A **LICENSE** file — it was packaged with `--skip-license`. The marketplace expects one, and "UNLICENSED" is the wrong signal for something publicly downloadable.
- A **`repository`** field — packaged with `--allow-missing-repository`.
- An **icon** and a fuller README, since both become the public listing.

Also worth changing before it goes public: the default `zanbeel.serverUrl` should stay blank rather than shipping an internal address, and the README should not name internal hosts.

### Recommendation

**Self-host.** It already works, it keeps the client source internal, and the only thing the marketplace adds is reach that a VPN also provides. Revisit if Zanbeel is ever open-sourced or used outside the organisation.

---

*Grounded in a read of `client/fox.py` (6,628 lines), the server's download endpoints, and the existing PyInstaller specs under `client/client1/`. Written 2026-09-11; §11 and §12 record what was built the same day.*
