Signing the fox CLI in
======================

The CLI never stores your password. There are three ways to give a machine a
credential, and every one of them can be revoked on its own.

1. Password, exchanged for a machine token (most people)
---------------------------------------------------------

```powershell
fox login --username jalal
```

Your password is used once, to create an access token named
`fox CLI on <this machine>`, and only that token is saved in
`~/.foxnest/credentials.json`. It carries `repo:write` (plus `admin` for team
leads) and expires after 90 days; `--scope` and `--expires-in-days` change that.
Logging in again replaces the machine's token. `fox logout` revokes it on the
server, not only on disk.

If a laptop is lost: Settings -> Access tokens -> Revoke, or
`fox token revoke <id>` from another machine. Nothing else is affected.

2. SSH key (no password at all after setup)
-------------------------------------------

```powershell
ssh-keygen -t ed25519           # if you do not have a key
fox ssh add work-laptop         # registers ~/.ssh/id_ed25519.pub (asks for your password once)
fox login --ssh                 # signs a one-time challenge with the key
```

The sign-in lasts 8 hours and the CLI renews it with the key on its own.
Passphrase-protected keys and keys held in ssh-agent work too (for an agent-only
key, pass `--key ~/.ssh/id_ed25519.pub`). A key is scoped like a token:
`fox ssh add deploy --scope repo:read` makes a key that can fetch but not push.
`fox ssh remove <id>` ends every sign-in that key made, immediately.

3. A token on a build agent
---------------------------

```powershell
fox token create ci-runner --scope repo:read --expires-in-days 30
```

Put the printed `fxp_...` value in the CI system's secret store and give the job:

```
FOXNEST_TOKEN=fxp_...
FOXNEST_SERVER=http://your-server:33333
```

Nothing is written to the agent's disk. `fox login --token fxp_...` saves a
token on a machine instead. On a Windows agent that writes fox's output to a
log, also set `PYTHONIOENCODING=utf-8`: fox prints a few non-ASCII characters
that a cp1252 log stream cannot encode.

What a token cannot do
----------------------

Tokens and SSH sign-ins are limited to their scopes, and none of them can create
or change credentials: making a token, adding or removing an SSH key, and
changing your password or email always ask for your password (the CLI prompts;
in a script, pipe it on stdin). So a leaked token cannot mint its own
replacement or take over the account. A team lead's token also needs the
`admin` scope before it can use team-lead powers.

`fox whoami` shows who you are signed in as, with which credential, its scopes
and its expiry.

Using the patched client on an older install
--------------------------------------------

If your system `fox` is the older installed client, run the included PowerShell
patcher to make the system `fox` call the local client:

```powershell
# Run from the repo root in PowerShell
.\tools\patch_fox_windows.ps1
```

It backs up any file it replaces (`.backup.YYYYMMDDHHMMSS`), and otherwise
creates a user-level `fox.bat` wrapper and prepends its directory to your user
PATH. Open a new shell afterwards.

Note that the patcher wraps `client/fox.py`, an older copy of the client: its
`fox login` saves a password session rather than a machine token, it has no
`fox whoami`, and it signs SSH challenges in the older raw format (still
accepted). The current client is `client/client1/fox.py`.
