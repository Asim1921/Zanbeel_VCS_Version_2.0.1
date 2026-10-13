#!/usr/bin/env python3
"""End-to-end check of the Integrations screens in a real browser.

    ..\\venv\\Scripts\\python.exe tools/e2e_integrations_ui.py [--keep] [--headed]

Starts the backend on a throwaway database, a webhook receiver on 127.0.0.2 and the Vite
dev server pointed at the backend, then runs foxnestFrontend/scripts/e2e-integrations-ui.mjs
in four phases. In between it uses what the browser set up from outside it -- the CI token
through the real fox CLI, the webhook through the receiver, the push rule over HTTP --
and checks on the server that every click did what it said.

Needs Chrome (or Edge with --browser msedge) and, once:
    cd foxnestFrontend && npm i --no-save playwright-core
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_integrations import (  # noqa: E402
    AWS_KEY_LINE, BASE_FILES, PASSWORD, SERVER_ENV, Machine, Receiver, event, make_token, push,
    seed, session_of, signed_by, wait_for,
)
from e2e_merge_conflicts import Api, Report, Repo, free_port, live_server, remove_tree  # noqa: E402
from e2e_merge_conflicts_ui import start_frontend, stop  # noqa: E402

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "foxnestFrontend"
DRIVER = FRONTEND_ROOT / "scripts" / "e2e-integrations-ui.mjs"


def run_driver(report, url, phase, user, shots, args, **extra):
    env = dict(os.environ, FRONTEND_URL=url, E2E_USER=user, E2E_PASSWORD=PASSWORD, E2E_PHASE=phase,
               E2E_REPO="int-app", SCREENSHOT_DIR=str(shots), E2E_BROWSER=args.browser,
               E2E_HEADED="1" if args.headed else "0", **extra)
    driver = subprocess.run(["node", str(DRIVER)], cwd=FRONTEND_ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=600)
    print(driver.stdout.rstrip())
    if driver.stderr.strip():
        print(driver.stderr.rstrip())
    values = {}
    for line in driver.stdout.splitlines():
        line = line.strip()
        if line.startswith("PASS"):
            report.passed += 1
        elif line.startswith("FAIL"):
            report.failed.append(f"UI {phase}: " + line[6:])
        elif line.startswith(("TOKEN ", "SECRET ")):
            key, value = line.split(" ", 1)
            values[key] = value
    report.check(f"browser phase '{phase}' finished", driver.returncode in (0, 1), f"exit {driver.returncode}")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--browser", default="chrome", help="chrome or msedge")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_integrations_ui_"))
    workdir = root / "server"
    workdir.mkdir()
    shots = root / "screenshots"
    report = Report()
    frontend = frontend_log = receiver = None
    try:
        api_port, ui_port = free_port(), free_port()
        ui_origin = f"http://127.0.0.1:{ui_port}"
        receiver = Receiver()
        with live_server(workdir, api_port, {"CORS_ORIGINS": ui_origin, **SERVER_ENV}) as base:
            report.section("Setup")
            int_id, _rules_id = seed(workdir)
            lead = Api(base, session_of(base, "lead"))
            repo = Repo(lead, int_id, "lead")
            repo.push("main", BASE_FILES, "base")
            repo.branch("feature")
            repo.push("feature", {**BASE_FILES, "feature.txt": "f\n"}, "feature work")
            pr = repo.open_pr("feature", title="Feature that needs CI")
            frontend, frontend_log, url = start_frontend(ui_port, base, root / "vite.log")
            report.check("backend, receiver and web client are running; a pull request is open", bool(pr))

            report.section("Browser, as ci: a token that can only report")
            token = run_driver(report, url, "token", "ci", shots, args).get("TOKEN")
            status, body = Api(base, token).get("/api/auth/me")
            report.check("the token made in the browser carries repo:status only",
                         status == 200 and body.get("credential", {}).get("scopes") == ["repo:status"], body)
            status, _b, _id = push(Api(base, token), int_id, "ci", "main", BASE_FILES, "ci pushes")
            report.check("...and cannot push", status == 403, _b)

            report.section("Browser, as lead: webhook, required check, push rule")
            secret = run_driver(report, url, "setup", "lead", shots, args,
                                E2E_RECEIVER=receiver.url("/hook")).get("SECRET")

            report.section("What the browser set up, used from outside it")
            def both_pings():
                found = [e for e in receiver.events() if e["json"].get("event") == "ping"]
                return found if len(found) >= 2 else None
            pings = wait_for(both_pings, 10)
            report.check("the ping and the redelivery reached the receiver, signed with the secret shown once",
                         bool(secret) and pings and all(signed_by(secret, e) for e in pings))
            report.check("...and the redelivery names the delivery it repeats",
                         pings and pings[1]["json"].get("redelivery_of") == pings[0]["json"].get("delivery_id"))
            _s, hooks = lead.get(f"/api/webhooks?repository_id={int_id}")
            report.check("the refused webhook was not saved", len(hooks.get("webhooks", [])) == 1, hooks)
            _s, body = lead.get(repo.url("/branch-policy"))
            report.check("the required check is saved pinned to ci",
                         body.get("policy", {}).get("required_status_checks") == [{"context": "ci/build", "reporter": "ci"}],
                         body)
            _s, rules = lead.get(f"/api/server-hooks?repository_id={int_id}")
            report.check("the refused rule was not saved; the other was",
                         [h["name"] for h in rules.get("hooks", [])] == ["no-secrets"], rules)
            status, body, _id = push(lead, int_id, "lead", "feature",
                                     {**BASE_FILES, "feature.txt": "f\n", "settings.py": AWS_KEY_LINE}, "leak")
            report.check("the rule made in the browser refuses a push carrying a key",
                         status == 422 and "AWS access key" in json.dumps(body), body)
            status, body = lead.post(repo.url("/branches"), {"name": "wip", "from_branch": "main"})
            report.check("...and a branch name it does not allow", status == 422, body)

            mark = len(receiver.events())
            head = repo.push("feature", {**BASE_FILES, "feature.txt": "f\n", "more.txt": "m\n"}, "more work")
            got = event(receiver, "pull_request.updated", mark,
                        lambda d: (d.get("pull_request") or {}).get("head") == head)
            report.check("a push to the PR's branch reaches the receiver, signed",
                         got is not None and signed_by(secret, got))
            status, body = Api(base, session_of(base, "dev")).post(
                repo.url(f"/commits/{head}/statuses"), {"context": "ci/build", "state": "success"})
            report.check("dev reports ci/build success on the new head (it will not count)", status == 200, body)

            report.section("Browser: the report that did not count")
            run_driver(report, url, "waiting", "lead", shots, args)

            report.section("CI reports, with the token made in the browser")
            agent = Machine(root, "ci-agent", {"FOXNEST_TOKEN": token, "FOXNEST_SERVER": base,
                                               "FOXNEST_REPO_ID": int_id})
            code, out = agent.fox("status-check", "report", "ci/build", "success", "--commit", head,
                                  "--url", "https://ci.example/run/7")
            report.check("fox status-check report succeeds and says the gate is satisfied",
                         code == 0 and "All 1 required check(s) have passed" in out, out)

            report.section("Browser: merge")
            mark = len(receiver.events())
            run_driver(report, url, "merge", "lead", shots, args)

            report.section("Server state after the browser run")
            _s, body = lead.get(repo.url(f"/pull-requests/{pr}"))
            report.check("the pull request is merged",
                         (body.get("pull_request") or body).get("status") == "merged", body)
            report.check("main has the feature's work", repo.file("main", "more.txt") == b"m\n")
            merged = event(receiver, "pull_request.merged", mark)
            report.check("the receiver was told, signed", merged is not None and signed_by(secret, merged))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        report.check("the run completed without an unexpected error", False, repr(exc))
    finally:
        if frontend is not None:
            stop(frontend)
            frontend_log.close()
        if receiver is not None:
            receiver.close()
        if args.keep:
            print(f"\nkept: {root}")
        else:
            remove_tree(root)

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
