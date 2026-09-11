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

**Zanbeel: Sign In** from the Command Palette. The token is stored in the OS
keychain through VS Code's `SecretStorage` and handed to terminals as
`FOXNEST_TOKEN`, so it is never written to `~/.foxnest/credentials.json` in the
clear.

Note that any process you start from a VS Code terminal can read that variable.
Turn it off with `zanbeel.injectToken` if that is not acceptable, and use
`fox login` instead.

## Settings

| Setting | Purpose |
|---|---|
| `zanbeel.serverUrl` | Server for this workspace, exported as `FOXNEST_SERVER` |
| `zanbeel.pythonPath` | Interpreter override, e.g. `py -3.11` |
| `zanbeel.injectPath` | Put `fox` on the terminal `PATH` (the main feature) |
| `zanbeel.injectToken` | Export the signed-in token to terminals |
| `zanbeel.showStatusBar` | Show the current branch in the status bar |

## If something is wrong

**Zanbeel: Run Diagnostics** reports the interpreter, whether `requests` is
present, where the launcher was written, and whether a different `fox` earlier
on your `PATH` is shadowing this one.

Terminals opened *before* the extension activated will not have `fox`. Open a
new one.
