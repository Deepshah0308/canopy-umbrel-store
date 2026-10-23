#!/usr/bin/env python3
"""
Fills the app list on the website (docs/index.html) from the apps' manifests, with each
app's version and when it was last updated (from git). The update bot runs this after
shipping updates, so the site always matches the store.

    python3 tools/build_site.py          # rewrite docs/index.html and docs/apps.json
    python3 tools/build_site.py --check  # exit 1 if the site is out of date
"""
import datetime
import glob
import html
import json
import os
import re
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "docs", "index.html")
START, END = "<!-- apps:start -->", "<!-- apps:end -->"
CATEGORY_NAMES = {"media": "Media", "files": "Files & notes", "social": "Reading", "networking": "Downloads & network",
                  "automation": "Home", "developer": "Developer", "finance": "Finance", "ai": "AI"}


def last_updated(app):
    try:
        out = subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%cs", "--", app],
                             text=True, capture_output=True, check=True).stdout.strip()
        return out or None
    except (OSError, subprocess.CalledProcessError):
        return None


def apps():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "*", "umbrel-app.yml"))):
        with open(path) as fh:
            m = yaml.safe_load(fh)
        out.append({
            "id": m["id"], "name": m["name"], "version": str(m["version"]), "tagline": m["tagline"],
            "category": m["category"], "icon": m["icon"], "website": m.get("website", ""),
            "updated": last_updated(os.path.basename(os.path.dirname(path))),
        })
    # YAMS first: it's the store's flagship bundle
    out.sort(key=lambda a: (a["id"] != "canopy-yams", a["name"].lower()))
    return out


def rows(items):
    lines = []
    for a in items:
        esc = {k: html.escape(str(v or ""), quote=True) for k, v in a.items()}
        updated = f'<time datetime="{esc["updated"]}">{esc["updated"]}</time>' if a["updated"] else ""
        lines.append(
            f'        <li data-updated="{esc["updated"]}">'
            f'<img src="{esc["icon"]}" alt="" width="40" height="40" loading="lazy">'
            f'<div class="app-main"><a href="{esc["website"]}" rel="noopener">{esc["name"]}</a>'
            f'<span class="app-line">{esc["tagline"]}</span></div>'
            f'<span class="app-cat">{html.escape(CATEGORY_NAMES.get(a["category"], a["category"].title()))}</span>'
            f'<span class="app-ver">{esc["version"]}</span>'
            f'<span class="app-date"><span class="fresh" aria-hidden="true"></span>{updated}</span></li>')
    return "\n".join(lines)


def render(text, items):
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    body = head + START + "\n" + rows(items) + "\n        " + END + tail
    return re.sub(r'(<span id="app-count">)\d+(</span>)', lambda m: f"{m.group(1)}{len(items)}{m.group(2)}", body)


def main():
    check = "--check" in sys.argv
    items = apps()
    with open(SITE) as fh:
        text = fh.read()
    new = render(text, items)
    data = json.dumps({"generated": datetime.date.today().isoformat(), "apps": items}, indent=2) + "\n"
    if check:
        stale = [a["id"] for a in items if f'>{html.escape(a["version"])}</span>' not in text or a["name"] not in text]
        if stale:
            print("The website's app list is out of date for: " + ", ".join(stale) + ". Run tools/build_site.py")
            return 1
        return 0
    with open(SITE, "w") as fh:
        fh.write(new)
    with open(os.path.join(ROOT, "docs", "apps.json"), "w") as fh:
        fh.write(data)
    print(f"website lists {len(items)} apps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
