#!/usr/bin/env python3
"""Next release tag and notes from the commits since the last vX.Y.Z tag.

Prints nothing and exits 0 when there's nothing new. Otherwise writes the notes to the path
given as the first argument and prints the new tag. Daily app updates ("X: update to 1.2.3")
are listed once each at their newest version; anything else is a change of its own and bumps
the minor version, app updates alone bump the patch version.
"""
import re
import subprocess
import sys

REPO = "https://github.com/Deepshah0308/canopy-umbrel-store"
UPDATE = re.compile(r"^(?P<app>.+?): update to (?P<version>\S+)$")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def next_tag(last, has_changes):
    major, minor, patch = map(int, last.lstrip("v").split("."))
    return f"v{major}.{minor + 1}.0" if has_changes else f"v{major}.{minor}.{patch + 1}"


def notes(last, subjects):
    updates, changes = {}, []
    for s in subjects:  # newest first, so the first version seen per app is its latest
        m = UPDATE.match(s)
        if m:
            updates.setdefault(m["app"], m["version"])
        elif not s.startswith("Merge "):
            changes.append(s)
    tag = next_tag(last, bool(changes))
    lines = [f"Add the store in Umbrel: **App Store → ⋯ → Community App Stores** → `{REPO}`", ""]
    if changes:
        lines += ["### Changes", *[f"- {c}" for c in changes], ""]
    if updates:
        lines += ["### Updated", " · ".join(f"{a} {v}" for a, v in sorted(updates.items(), key=lambda x: x[0].lower())), ""]
    lines.append(f"Details in [CHANGELOG.md]({REPO}/blob/main/CHANGELOG.md) · [All commits]({REPO}/compare/{last}...{tag})")
    return tag, "\n".join(lines) + "\n"


def main():
    last = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    subjects = [s for s in git("log", "--format=%s", f"{last}..HEAD").splitlines() if s]
    if not subjects:
        return 0
    tag, body = notes(last, subjects)
    with open(sys.argv[1], "w") as fh:
        fh.write(body)
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
