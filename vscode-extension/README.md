# Zanbeel VCS for VS Code

Run the Zanbeel `fox` CLI from any VS Code terminal without installing the
client on each machine.

## What it does

Installing this extension puts `fox` on the `PATH` of every terminal VS Code
opens. Nothing is written to your system `PATH`, no administrator rights are
needed, and uninstalling the extension removes it again.

```
fox status
fox add --all
fox commit -m "..."
fox push
```

All 78 commands work — `token`, `ssh`, `webhook`, `status-check`, `hook`,
`search`, `admin` included.

## Requirements

Python 3.8 or newer on the machine. The extension finds it automatically
(`py -3.11`, `py -3`, `python3`, `python`) and will offer to install the one
package the CLI needs (`requests`) if it is missing.

## Signing in

**Zanbeel: Sign In** from the Command Palette. It offers the Zanbeel server,
`https://192.168.15.207:8090`, and remembers the address you confirm in
`zanbeel.serverUrl`. Signing in with a password uses
it once, to create an access token named "VS Code on *your machine*"
(`repo:write`, 90 days); the password itself is not kept. You can also paste a
token you made under Settings → Access tokens. Either way the token is stored in
the OS keychain through VS Code's `SecretStorage` and handed to terminals as
`FOXNEST_TOKEN`, so it is never written to `~/.foxnest/credentials.json` in the
clear. **Zanbeel: Sign Out** revokes the token on the server as well as
forgetting it.

Note that any process you start from a VS Code terminal can read that variable.
Turn it off with `zanbeel.injectToken` if that is not acceptable, and use
`fox login` instead.

## The server's certificate

The Zanbeel server uses HTTPS with a self-signed certificate, which VS Code,
Node and Python do not trust on their own. This extension ships a copy of it
(`certs/zanbeel.crt`) and trusts it next to the usual public certificate
authorities:

- for its own requests (sign in, sign out, diagnostics);
- for `fox` in VS Code terminals, through `REQUESTS_CA_BUNDLE`, which points at
  a bundle of the public roots plus this certificate. Other HTTPS sites keep
  working from those terminals.

Nothing outside VS Code changes: no system certificate store, no global
environment variable. When the server's certificate is replaced (it is valid
until January 2029), download the new one from
`https://192.168.15.207:8090/zanbeel.crt` and set `zanbeel.caCertificate` to
it, or install a newer build of the extension.

## Settings

| Setting | Purpose |
|---|---|
| `zanbeel.serverUrl` | Server for this workspace, exported as `FOXNEST_SERVER` |
| `zanbeel.caCertificate` | Certificate to trust for the server; blank uses the bundled one |
| `zanbeel.pythonPath` | Interpreter override, e.g. `py -3.11` |
| `zanbeel.injectPath` | Put `fox` on the terminal `PATH` (the main feature) |
| `zanbeel.injectToken` | Export the signed-in token to terminals |
| `zanbeel.showStatusBar` | Show the current branch in the status bar |

## If something is wrong

**Zanbeel: Run Diagnostics** reports the interpreter, whether `requests` is
present, where the launcher was written, which certificate is trusted, whether
the server answers over HTTPS, and whether a different `fox` earlier on your
`PATH` is shadowing this one.

Terminals opened *before* the extension activated will not have `fox`. Open a
new one.

## Installing and building

Install the packaged extension from the Extensions view (**…** → **Install from
VSIX…**) or with `code --install-extension zanbeel-vcs-<version>.vsix`.

To build it, on the server:

```
cd vscode-extension
cp /etc/nginx/ssl/zanbeel.crt certs/zanbeel.crt   # the certificate clients will trust
npm install
npm run package                                    # writes zanbeel-vcs-<version>.vsix
```
