#!/usr/bin/env python3
"""End-to-end check of the conflict resolver in a real browser.

    ..\\venv\\Scripts\\python.exe tools/e2e_merge_conflicts_ui.py [--keep] [--headed]

Starts the backend on a throwaway database, seeds a pull request with three kinds of
conflict (an overlapping edit, an Urdu file added on both sides, a file edited on one side
and deleted on the other), starts the Vite dev server pointed at that backend, and runs
foxnestFrontend/scripts/e2e-merge-conflicts-ui.mjs, which signs in and resolves the
conflicts through the UI. Afterwards it checks on the server that what was clicked is
exactly what was committed.

Needs Chrome (or Edge with --browser msedge) and, once:
    cd foxnestFrontend && npm i --no-save playwright-core
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_merge_conflicts import (  # noqa: E402
    PASSWORD, URDU_FEATURE, URDU_MAIN, Api, Report, Repo, app_lines, free_port, live_server,
    login, remove_tree, seed,
)

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "foxnestFrontend"
DRIVER = FRONTEND_ROOT / "scripts" / "e2e-merge-conflicts-ui.mjs"
URDU_RESOLVED = "سلام دنیا اور دوستو\nخوش آمدید ✓\n"


def start_frontend(port, api_base, log_path):
    env = dict(os.environ, VITE_API_URL=f"{api_base}/api", VITE_API_SERVER_URL=api_base)
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        "npx vite --host 127.0.0.1 --port %d --strictPort" % port,
        cwd=FRONTEND_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, shell=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"vite exited early; see {log_path}")
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return proc, log, url
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("vite did not start within 120s")


def stop(proc):
    if proc.poll() is None:
        if os.name == "nt":
            # npx starts node under a shell; kill the whole tree or the port stays taken.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def prepare(base, repo_id):
    lead = Api(base, login(base, "lead")[1])
    alice = Api(base, login(base, "alice")[1])
    repo = Repo(lead, repo_id, "lead")
    repo.push("main", {"app.py": app_lines(), "notes.txt": "notes\n", "README.md": "readme\n"},
              "base", parents=[])
    repo.branch("feature")
    repo.push("main", {"app.py": app_lines(l5="line 5 main"), "README.md": "readme\n",
                       "i18n/ur.txt": URDU_MAIN}, "main side")
    feature_head = repo.push("feature", {
        "app.py": app_lines(l5="line 5 feature", l9="line 9 feature"), "notes.txt": "edited\n",
        "README.md": "readme\n", "i18n/ur.txt": URDU_FEATURE}, "feature side")
    lead.put(repo.url("/branch-protection"),
             {"branch_pattern": "main", "mode": "open", "rules": {"required_approvals": 1}})
    pr = repo.open_pr("feature", title="Feature with conflicts")
    alice.post(repo.url(f"/pull-requests/{pr}/reviews"), {"state": "approved"})
    return repo, pr, feature_head


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--browser", default="chrome", help="chrome or msedge")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="foxnest_e2e_ui_"))
    report = Report()
    shots = root / "screenshots"
    frontend = frontend_log = None
    try:
        api_port, ui_port = free_port(), free_port()
        ui_origin = f"http://127.0.0.1:{ui_port}"
        with live_server(root, api_port, {"CORS_ORIGINS": ui_origin}) as base:
            report.section("Setup")
            repo_id = seed(root)
            repo, pr, feature_head = prepare(base, repo_id)
            main_head = repo.head("main")
            _s, state = repo.api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
            report.check("a pull request with three conflicting files is ready",
                         state.get("conflicts") == ["app.py", "i18n/ur.txt", "notes.txt"], state)

            frontend, frontend_log, url = start_frontend(ui_port, base, root / "vite.log")
            report.check("the web client is being served", True)

            env = dict(os.environ, FRONTEND_URL=url, E2E_USER="lead", E2E_PASSWORD=PASSWORD,
                       E2E_REPO="e2e-app", E2E_PR=str(pr), SCREENSHOT_DIR=str(shots),
                       E2E_BROWSER=args.browser, E2E_HEADED="1" if args.headed else "0")
            driver = subprocess.run(["node", str(DRIVER)], cwd=FRONTEND_ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=600)
            print(driver.stdout.rstrip())
            if driver.stderr.strip():
                print(driver.stderr.rstrip())
            # Fold the browser's own checks into the total.
            for line in driver.stdout.splitlines():
                if line.strip().startswith("PASS"):
                    report.passed += 1
                elif line.strip().startswith("FAIL"):
                    report.failed.append("UI: " + line.strip()[6:])
            report.check("the browser run finished", driver.returncode in (0, 1),
                         f"exit {driver.returncode}")

            report.section("Server state after the browser run")
            expected_app = app_lines(l5="line 5 main\nline 5 feature", l9="line 9 feature")
            report.check("the resolution is committed to feature", repo.head("feature") != feature_head)
            report.check("main is untouched", repo.head("main") == main_head)
            report.check("app.py holds both sides of the block (Accept both)",
                         repo.file("feature", "app.py") == expected_app.encode(),
                         repo.file("feature", "app.py"))
            report.check("the Urdu file is byte-exact as typed",
                         repo.file("feature", "i18n/ur.txt") == URDU_RESOLVED.encode(),
                         repo.file("feature", "i18n/ur.txt"))
            report.check("notes.txt is deleted", repo.file("feature", "notes.txt") is None)
            _s, reviews = repo.api.get(repo.url(f"/pull-requests/{pr}/reviews"))
            report.check("the earlier approval is stale",
                         len(reviews.get("stale_approvals", [])) == 1 and not reviews.get("can_merge"),
                         reviews)
            _s, state = repo.api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
            report.check("the pull request now merges cleanly", state.get("state") == "clean", state)
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
