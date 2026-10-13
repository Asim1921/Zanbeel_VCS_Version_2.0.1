#!/usr/bin/env python3
"""End-to-end check of search, the activity log and notifications in a real browser.

    ..\\venv\\Scripts\\python.exe tools/e2e_discovery_ui.py [--keep] [--headed]

Starts the backend on a throwaway database and search index, seeds two repositories
(one the developer cannot read), a queued commit that gets approved and a pull request
that mentions the lead, starts the Vite dev server pointed at the backend, and runs
foxnestFrontend/scripts/e2e-discovery-ui.mjs as the lead and then as the developer.
Afterwards it checks on the server that opening notifications marked them read.

Needs Chrome (or Edge with --browser msedge) and, once:
    cd foxnestFrontend && npm i --no-save playwright-core
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_discovery import PASSWORD, index_env, push, seed, session_of, wait_indexed  # noqa: E402
from e2e_merge_conflicts import Api, Report, Repo, b64, free_port, live_server, remove_tree  # noqa: E402
from e2e_merge_conflicts_ui import start_frontend, stop  # noqa: E402

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "foxnestFrontend"
DRIVER = FRONTEND_ROOT / "scripts" / "e2e-discovery-ui.mjs"


def run_driver(report, url, phase, user, shots, args, **extra):
    env = dict(os.environ, FRONTEND_URL=url, E2E_USER=user, E2E_PASSWORD=PASSWORD, E2E_PHASE=phase,
               SCREENSHOT_DIR=str(shots), E2E_BROWSER=args.browser, E2E_HEADED="1" if args.headed else "0",
               **extra)
    driver = subprocess.run(["node", str(DRIVER)], cwd=FRONTEND_ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=600)
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

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_discovery_ui_"))
    workdir = root / "server"
    workdir.mkdir()
    shots = root / "screenshots"
    report = Report()
    frontend = frontend_log = None
    try:
        api_port, ui_port = free_port(), free_port()
        ui_origin = f"http://127.0.0.1:{ui_port}"
        with live_server(workdir, api_port, {"CORS_ORIGINS": ui_origin, **index_env(workdir)}) as base:
            report.section("Setup")
            billing, payroll = seed(workdir)
            lead, dev = Api(base, session_of(base, "lead")), Api(base, session_of(base, "dev"))
            invoice = "def parse_invoice(total):\n    return total * 100\n"
            push(lead, billing, "lead", "main", {"src/invoice.py": invoice, "README.md": "Billing\n"}, "Add invoice parser")
            push(lead, billing, "lead", "main", {"src/invoice.py": invoice, "README.md": "Billing service\n"}, "Bill at 100% rate")
            push(lead, billing, "lead", "main",
                 {"src/invoice.py": "def parse_invoice(total):\n    return round(total * 100)  # RoundedCents\n",
                  "README.md": "Billing service\n"}, "Round cents")
            push(lead, payroll, "lead", "main", {"src/salary.py": "def parse_invoice():\n    pass\n"}, "Payroll parser")

            status, body = dev.post(f"/api/repository/{billing}/push", {
                "branch": "main",
                "commit": {"id": hashlib.sha1(f"queued{time.time_ns()}".encode()).hexdigest(), "author": "dev",
                           "message": "Developer fix", "parent": Repo(lead, billing, "lead").head("main"),
                           "files": {"src/invoice.py": b64("def parse_invoice(total):\n    return int(total * 100)  # RoundedCents\n"),
                                     "README.md": b64("Billing service\n")}},
            })
            lead.post(f"/api/pending-commits/{body.get('commit_id')}/review", {"action": "approve"})
            dev.post(f"/api/repository/{billing}/branches", {"name": "rounding", "from_branch": "main"})
            status, body = dev.post(f"/api/repository/{billing}/pull-requests", {
                "title": "Rounding from dev", "description": "@lead could you review?",
                "source_branch": "rounding", "target_branch": "main"})
            pr = (body.get("pull_request") or {}).get("id")
            indexed = wait_indexed(lead, expect=2)
            report.check("two repositories indexed, a commit approved, a pull request mentioning the lead",
                         indexed.get("repositories") == 2 and bool(pr), indexed)

            frontend, frontend_log, url = start_frontend(ui_port, base, root / "vite.log")
            report.check("the web client is being served", True)

            report.section("Browser, as the lead")
            run_driver(report, url, "lead", "lead", shots, args, E2E_BILLING=billing, E2E_PAYROLL=payroll)
            report.section("Browser, as the developer")
            run_driver(report, url, "dev", "dev", shots, args, E2E_BILLING=billing, E2E_PAYROLL=payroll)

            report.section("Server state after the browser run")
            _s, body = lead.get("/api/notifications", params={"type": "pr_mention"})
            report.check("opening the lead's mention marked it read",
                         body.get("notifications") and all(n["is_read"] for n in body["notifications"]), body)
            _s, body = dev.get("/api/notifications", params={"type": "commit_approved"})
            report.check("opening dev's approval marked it read",
                         body.get("notifications") and all(n["is_read"] for n in body["notifications"]), body)
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
