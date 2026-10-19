#!/usr/bin/env python3
"""End-to-end check of sign-in limits, backups and schema health in a real browser.

    ..\\venv\\Scripts\\python.exe tools/e2e_security_operations_ui.py [--keep] [--headed]

Starts the backend on a throwaway database, blob store, backup folder and docs folder;
locks one account (twenty wrong passwords from twenty addresses) and throttles one
address; starts the Vite dev server pointed at the backend, and runs
foxnestFrontend/scripts/e2e-security-operations-ui.mjs signed out and then as the
administrator. Afterwards it checks on the server what the browser did.

Needs Chrome (or Edge with --browser msedge) and, once:
    cd foxnestFrontend && npm i --no-save playwright-core
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_security_operations import PASSWORD, ops_env, push, seed, session  # noqa: E402
from e2e_merge_conflicts import Api, Report, free_port, live_server, remove_tree  # noqa: E402
from e2e_merge_conflicts_ui import start_frontend, stop  # noqa: E402

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "foxnestFrontend"
DRIVER = FRONTEND_ROOT / "scripts" / "e2e-security-operations-ui.mjs"


def run_driver(report, url, phase, user, shots, args):
    env = dict(os.environ, FRONTEND_URL=url, E2E_USER=user, E2E_PASSWORD=PASSWORD, E2E_PHASE=phase,
               SCREENSHOT_DIR=str(shots), E2E_BROWSER=args.browser, E2E_HEADED="1" if args.headed else "0")
    driver = subprocess.run(["node", str(DRIVER)], cwd=FRONTEND_ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=900)
    print(driver.stdout.rstrip())
    if driver.stderr.strip():
        print(driver.stderr.rstrip())
    for line in driver.stdout.splitlines():
        line = line.strip()
        if line.startswith("PASS"):
            report.passed += 1
        elif line.startswith("FAIL"):
            report.failed.append(f"UI {phase}: " + line[6:])
    report.check(f"browser phase '{phase}' finished", driver.returncode in (0, 1), f"exit {driver.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--browser", default="chrome", help="chrome or msedge")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_secops_ui_"))
    workdir = root / "server"
    workdir.mkdir()
    (workdir / "docs").mkdir()
    shots = root / "screenshots"
    report = Report()
    frontend = frontend_log = None
    try:
        api_port, ui_port = free_port(), free_port()
        ui_origin = f"http://127.0.0.1:{ui_port}"
        with live_server(workdir, api_port, {**ops_env(workdir), "CORS_ORIGINS": ui_origin}) as base:
            report.section("Setup")
            repo = seed(workdir)
            lead = Api(base, session(base, "lead"))
            push(lead, repo, {"src/app.py": "print('hello')\n", "assets/logo.png": bytes(range(256)) * 4},
                 "first")
            for i in range(20):
                requests.post(f"{base}/api/auth/login", json={"username": "lead", "password": "nope"},
                              headers={"X-Forwarded-For": f"100.64.2.{i}"}, timeout=60)
            for i in range(20):
                requests.post(f"{base}/api/auth/login", json={"username": f"spray{i}", "password": "nope"},
                              headers={"X-Forwarded-For": "198.51.100.9"}, timeout=60)
            admin = Api(base, session(base, "admin"))
            _s, body = admin.get("/api/admin/security/lockouts")
            report.check("an account is locked and an address throttled",
                         any(x["username"] == "lead" for x in body.get("locked_accounts", []))
                         and any(x["ip_address"] == "198.51.100.9" for x in body.get("throttled_addresses", [])),
                         body)

            frontend, frontend_log, url_ui = start_frontend(ui_port, base, root / "vite.log")
            report.check("the web client is being served", True)

            report.section("Browser, signed out")
            run_driver(report, url_ui, "signin", "dev", shots, args)
            report.section("Browser, as the administrator")
            run_driver(report, url_ui, "admin", "admin", shots, args)

            report.section("Server state after the browser run")
            response = requests.post(f"{base}/api/auth/login", json={"username": "lead", "password": PASSWORD},
                                     headers={"X-Forwarded-For": "100.64.3.1"}, timeout=60)
            report.check("the account unlocked in the browser signs in", response.status_code == 200, response.text)
            _s, body = admin.get("/api/admin/security/lockouts")
            report.check("the address cleared in the browser is no longer throttled",
                         not any(x["ip_address"] == "198.51.100.9" for x in body.get("throttled_addresses", [])),
                         body.get("throttled_addresses"))
            _s, body = admin.get("/api/admin/backups")
            rows = body.get("backups", [])
            report.check("the backup taken in the browser is verified and drilled",
                         rows and rows[0]["verified"] and rows[0]["restore_test_status"] == "passed"
                         and rows[0]["verify_status"] == "passed", rows[:1])
            _s, body = admin.get("/api/activities", params={"type": "backup_created,backup_restore_tested,"
                                                                   "backup_verified,account_unlocked"})
            kinds = {a.get("activity_type") for a in body.get("activities", [])}
            report.check("each action is in the activity log, as the administrator",
                         {"backup_created", "backup_restore_tested", "backup_verified", "account_unlocked"} <= kinds,
                         kinds)
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
