Using the patched Fox client (login/logout)

This repository includes a modified Fox client that supports `fox login` and `fox logout`.

Quick usage:

1. Authenticate and save token:

```powershell
# interactive prompt
fox login --username jalal
```

2. If your system `fox` is the older installed client, run the included PowerShell patcher to make the system `fox` call the local modified client:

```powershell
# Run from the repo root in PowerShell
.\tools\patch_fox_windows.ps1
```

When the patch script runs it will:
- Detect the installed `fox` command (if any).
- Back it up and replace it with a wrapper if it's a script.
- Otherwise create a user-level wrapper directory and place a `fox.bat` that forwards to the local `client/fox.py`.
- Prepend the wrapper directory to your user PATH so new shells pick up the wrapper.

3. After login, run `fox push` as normal. If the client receives HTTP 401 it will prompt you to login interactively once and retry automatically.

4. Logout when needed:

```powershell
fox logout
```

Notes:
- The patcher creates backups of any file it replaces with a timestamped `.backup.YYYYMMDDHHMMSS` file.
- Changing user PATH requires opening a new shell to take effect.
