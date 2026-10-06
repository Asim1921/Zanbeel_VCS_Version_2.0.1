#!/usr/bin/env python3
"""End-to-end check of the credentials screens in a real browser.

    ..\\venv\\Scripts\\python.exe tools/e2e_credentials_ui.py [--keep] [--headed]

Starts the backend on a throwaway database and the Vite dev server pointed at it, then
runs foxnestFrontend/scripts/e2e-credentials-ui.mjs twice. Between the two browser
phases it uses what the browser created from outside it -- the token over HTTP, the SSH
key through the real fox CLI -- and afterwards checks on the server that every click
did what it said.

Needs Chrome (or Edge with --browser msedge), ssh-keygen, and, once:
    cd foxnestFrontend && npm i --no-save playwright-core
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_credentials import (  # noqa: E402
    KEYGEN, PASSWORD, Machine, creds, keygen, me, notifications, seed, session_of,
)
from e2e_merge_conflicts import Api, Report, free_port, live_server, remove_tree  # noqa: E402
from e2e_merge_conflicts_ui import start_frontend, stop  # noqa: E402

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "foxnestFrontend"
DRIVER = FRONTEND_ROOT / "scripts" / "e2e-credentials-ui.mjs"


def run_driver(report, url, phase, shots, args, **extra):
    env = dict(os.environ, FRONTEND_URL=url, E2E_USER="lead", E2E_PASSWORD=PASSWORD,
               E2E_PHASE=str(phase), SCREENSHOT_DIR=str(shots), E2E_BROWSER=args.browser,
               E2E_HEADED="1" if args.headed else "0", **extra)
    driver = subprocess.run(["node", str(DRIVER)], cwd=FRONTEND_ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=600)
    print(driver.stdout.rstrip())
    if driver.stderr.strip():
        print(driver.stderr.rstrip())
    token = None
    for line in driver.stdout.splitlines():
        line = line.strip()
        if line.startswith("PASS"):
            report.passed += 1
        elif line.startswith("FAIL"):
            report.failed.append(f"UI phase {phase}: " + line[6:])
        elif line.startswith("TOKEN "):
            token = line.split(" ", 1)[1]
    report.check(f"browser phase {phase} finished", driver.returncode in (0, 1), f"exit {driver.returncode}")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--browser", default="chrome", help="chrome or msedge")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_credentials_ui_"))
    workdir = root / "server"
    workdir.mkdir()
    shots = root / "screenshots"
    report = Report()
    frontend = frontend_log = None
    try:
        if not KEYGEN:
            raise RuntimeError("ssh-keygen (OpenSSH) is required")
        api_port, ui_port = free_port(), free_port()
        ui_origin = f"http://127.0.0.1:{ui_port}"
        with live_server(workdir, api_port, {"CORS_ORIGINS": ui_origin}) as base:
            report.section("Setup")
            repo_id = seed(workdir)
            lead_machine = Machine(root, "lead", base, repo_id)
            key = keygen(lead_machine.home / ".ssh" / "id_ed25519")
            pub = Path(str(key) + ".pub").read_text().strip()
            fingerprint = subprocess.run([KEYGEN, "-lf", str(key) + ".pub"], capture_output=True,
                                         text=True).stdout.split()[1]
            frontend, frontend_log, url = start_frontend(ui_port, base, root / "vite.log")
            report.check("backend and web client are running", True)

            report.section("Browser, phase 1: create")
            token = run_driver(report, url, 1, shots, args, E2E_PUBKEY=pub, E2E_FINGERPRINT=fingerprint)

            report.section("Using what the browser made, from outside it")
            cred = me(base, token).json().get("credential", {}) if token else {}
            report.check("the token works, read only", cred.get("scopes") == ["repo:read"], cred)
            expires = datetime.fromisoformat(cred["expires_at"]) if cred.get("expires_at") else None
            report.check("...and expires in 30 days, as chosen",
                         expires is not None and abs(expires - datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)) < timedelta(hours=1),
                         cred.get("expires_at"))
            status, body = Api(base, token).post("/api/repository/create",
                                                 {"username": "lead", "repo_name": "nope"})
            report.check("...and cannot write", status == 403 and "read-only" in str(body), body)

            (lead_machine.home / ".foxnest").mkdir()
            (lead_machine.home / ".foxnest" / "credentials.json").write_text(
                json.dumps({"username": "lead", "origin_url": base}))
            code, out = lead_machine.fox("login", "--ssh")
            report.check("the fox CLI signs in with the key added in the browser, read only",
                         code == 0 and creds(lead_machine.home).get("scopes") == ["repo:read"], out)
            ssh_token = creds(lead_machine.home).get("access_token")

            dev_session = session_of(base, "dev")
            _s, body = Api(base, dev_session).post("/api/tokens", {"name": "dev-ci", "scopes": ["repo:write"]})
            dev_token = body.get("token")
            dev_key = keygen(root / "dev-keys" / "id_ed25519")
            Api(base, dev_session).post("/api/ssh-keys", {
                "title": "dev-laptop", "public_key": Path(str(dev_key) + ".pub").read_text().strip()})
            report.check("dev holds a token and a key for the admin to see", me(base, dev_token).status_code == 200)

            report.section("Browser, phase 2: use, remove, revoke")
            run_driver(report, url, 2, shots, args, E2E_OTHER_USER="dev")

            report.section("Server state after the browser run")
            report.check("removing the key in the browser ended the CLI's sign-in",
                         ssh_token is not None and me(base, ssh_token).status_code == 401)
            report.check("revoking in the browser locked the token out", me(base, token).status_code == 401)
            report.check("Revoke all ended dev's token", me(base, dev_token).status_code == 401)
            _s, keys = Api(base, session_of(base, "dev")).get("/api/ssh-keys")
            report.check("...and removed dev's key", keys.get("keys") == [], keys)
            report.check("dev was told", "credentials_revoked" in notifications(workdir, "dev"))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        report.check("the run completed without an unexpected error", False, repr(exc))
    finally:
        if frontend is not None:
            stop(frontend)
            frontend_log.close()
        if args.keep:
            print(f"\nkept: {root}")
        else:
            remove_tree(root)

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
