#!/usr/bin/env python3
"""
Runs in GitHub Actions every day (see .github/workflows/update-apps.yml):

  for every app with a newer upstream release:
    1. update its files (tools/update_apps.py)
    2. install it the way umbrelOS does and check it works (tools/umbrel_sim.py)
    3. patch, minor and rebuild updates that pass  -> committed straight to main, so
       Umbrel shows the update within the hour
       major updates, or anything that fails        -> a pull request for a human, with
       the test result in its description

    python3 tools/ship_updates.py [--dry-run] [--only app-id ...]
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_apps  # noqa: E402

ROOT = update_apps.ROOT
SIM = os.path.join(ROOT, "tools", "umbrel_sim.py")


def git(*args, check=True, capture=False):
    res = subprocess.run(["git", "-C", ROOT, *args], check=check, text=True,
                         stdout=subprocess.PIPE if capture else None)
    return (res.stdout or "").strip() if capture else res.returncode


def sim(app):
    """(passed, log tail). passed is None when this machine couldn't run the check."""
    res = subprocess.run([sys.executable, SIM, app, "--timeout", "600"], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = "\n".join((res.stdout or "").splitlines()[-60:])
    if res.returncode == 3:
        return None, tail
    return res.returncode == 0, tail


def describe(plan):
    rows = "\n".join(f"| {c['service']} | `{c['old_tag']}` | `{c['new_tag']}` | {c['kind']} |" for c in plan["changes"])
    return f"| Service | From | To | Change |\n|---|---|---|---|\n{rows}"


def open_pr(plan, passed, log, reason):
    app, version = plan["app"], plan["new_version"]
    branch = f"update/{app}-{version}"
    git("checkout", "-q", "-B", branch)
    git("add", "-A", os.path.relpath(plan["dir"], ROOT))
    git("commit", "-q", "-m", f"{plan['name']}: update to {version}")
    git("push", "-q", "-f", "origin", branch)
    result = {True: "✅ installs and starts", False: "❌ failed", None: "⚠️ couldn't run here"}[passed]
    body = f"""{reason}

{describe(plan)}

**Install test:** {result}

<details><summary>Install test output</summary>

```
{log[-6000:]}
```
</details>

Merge this when you're happy; Umbrel shows the update to everyone within the hour.

🤖 Opened by Canopy's update bot."""
    exists = subprocess.run(["gh", "pr", "view", branch, "--json", "number"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        subprocess.run(["gh", "pr", "edit", branch, "--body", body], cwd=ROOT, check=False)
    else:
        labels = ["update", "major" if plan["kind"] == "major" else "needs-fix"]
        cmd = ["gh", "pr", "create", "--base", "main", "--head", branch,
               "--title", f"{plan['name']} {plan['version']} → {version}", "--body", body]
        for label in labels:
            cmd += ["--label", label]
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            # labels may not exist yet
            subprocess.run(cmd[:cmd.index("--label")], cwd=ROOT, check=False)
    git("checkout", "-q", "main")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't commit or push")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    if git("status", "--porcelain", capture=True):
        raise SystemExit("working tree isn't clean")
    summary = {"shipped": [], "pull_requests": [], "up_to_date": 0}
    for app_dir in update_apps.app_dirs():
        app = os.path.basename(app_dir)
        if args.only and app not in args.only:
            continue
        plan = update_apps.plan_app(app_dir)
        for problem in plan["problems"]:
            print(f"[{app}] note: {problem}")
        if not plan["changes"]:
            summary["up_to_date"] += 1
            continue
        update_apps.apply_plan(plan)
        print(f"[{app}] {plan['kind']} update to {plan['new_version']}; testing the install…", flush=True)
        passed, log = sim(app)
        print(log.splitlines()[-1] if log else "", flush=True)
        if args.dry_run:
            git("checkout", "-q", "--", os.path.relpath(app_dir, ROOT))
            continue
        if plan["kind"] != "major" and passed:
            git("add", "-A", os.path.relpath(app_dir, ROOT))
            git("commit", "-q", "-m", "tmp")  # so the site sees this app's new date
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_site.py")], check=True)
            git("reset", "-q", "--soft", "HEAD~1")
            git("add", "-A", os.path.relpath(app_dir, ROOT), "docs")
            git("commit", "-q", "-m", f"{plan['name']}: update to {plan['new_version']}\n\n"
                + "\n".join(f"{c['service']}: {c['old_tag']} -> {c['new_tag']} ({c['kind']})" for c in plan["changes"]))
            git("push", "-q", "origin", "main")  # before any pull request branches off main
            summary["shipped"].append(f"{app} {plan['new_version']}")
        else:
            reason = ("A new major version: these can change settings or need a migration, so a person "
                      "should look before everyone gets it." if plan["kind"] == "major"
                      else "The update didn't pass the install test, so it wasn't shipped.")
            open_pr(plan, passed, log, reason)
            summary["pull_requests"].append(f"{app} {plan['new_version']}")
    print(json.dumps(summary, indent=2))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write("## Canopy update bot\n\n")
            fh.write(f"- Shipped: {', '.join(summary['shipped']) or 'nothing'}\n")
            fh.write(f"- Waiting for review: {', '.join(summary['pull_requests']) or 'nothing'}\n")
            fh.write(f"- Already up to date: {summary['up_to_date']} app(s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
