#!/usr/bin/env python3
"""
Canopy's update bot: finds newer releases of every image in the store and updates the
apps to them, so people get the latest version without waiting for a maintainer.

    python3 tools/update_apps.py                 # report what's out of date
    python3 tools/update_apps.py --apply         # also edit the files
    python3 tools/update_apps.py --apply --json plan.json   # and write a machine-readable plan

Rules (the same for every app, so nobody has to remember them):
  * Only real releases: a tag must look like the one in use (same 'v' prefix, number of
    parts and suffix such as -alpine). Betas, release candidates and nightlies never match.
  * Databases and language runtimes (postgres, mariadb, redis, python, ...) only move
    within their major version; a major database upgrade needs a human.
  * Every new version must still run on both amd64 (Umbrel Home, PCs) and arm64
    (Raspberry Pi). If it doesn't, it's skipped and reported.
  * Images are pinned by digest. When a tag is rebuilt (linuxserver.io does this for
    security fixes), the digest is refreshed too.
  * Each change is classified: "major" changes become a pull request for review; "minor",
    "patch" and "rebuild" changes are shipped once the install test passes.

The app's version in umbrel-app.yml follows its main image (the one app_proxy points at).
Apps with their own version number (like YAMS) get a patch bump instead, so umbrelOS
offers the update.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_ARCHES = {"linux/amd64", "linux/arm64"}
# Stay within the current major version for these (data formats change between majors).
HOLD_MAJOR = re.compile(r"(^|/)(postgres|postgis|mariadb|mysql|mongo|redis|valkey|keydb|memcached|"
                        r"elasticsearch|opensearch|clickhouse|golang|openjdk|eclipse-temurin)$")
# Language runtimes stay on their minor version (3.12.x): app code is tested against it.
HOLD_MINOR = re.compile(r"(^|/)(python|node|ruby|php)$")
IMAGE_LINE = re.compile(r"^(?P<indent>\s*)image:\s*(?P<q>[\"']?)(?P<ref>[^\s\"'#]+)(?P=q)", re.M)
TAG_SHAPE = re.compile(r"^(?P<prefix>v?)(?P<nums>\d+(?:\.\d+)*)(?P<suffix>(?:-[A-Za-z][\w.]*)?)$")


# --------------------------------------------------------------------------- versions
def shape(tag):
    m = TAG_SHAPE.match(tag)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group("nums").split("."))
    return m.group("prefix"), len(nums), m.group("suffix"), nums


def candidates(current_tag, tags, hold_major, hold_minor=False):
    cur = shape(current_tag)
    if not cur:
        return []
    prefix, parts, suffix, nums = cur
    out = []
    for tag in tags:
        s = shape(tag)
        if not s or s[0] != prefix or s[1] != parts or s[2] != suffix:
            continue
        if s[3] <= nums:
            continue
        if (s[3][0] >= 1000) != (nums[0] >= 1000):
            continue  # a date-style tag (2021.12.13) next to normal versions (0.11.0): different schemes
        if hold_major and s[3][0] != nums[0]:
            continue
        if hold_minor and s[3][:2] != nums[:2]:
            continue
        out.append((s[3], tag))
    out.sort()
    return [t for _, t in out]


def classify(old_tag, new_tag):
    if old_tag == new_tag:
        return "rebuild"
    a, b = shape(old_tag)[3], shape(new_tag)[3]
    if b[0] != a[0]:
        # 0.x releases change their "major" in the second number
        return "major"
    if len(a) > 1 and b[1] != a[1]:
        return "major" if a[0] == 0 else "minor"
    return "patch"


# --------------------------------------------------------------------------- apps
def app_dirs():
    return sorted(d for d in glob.glob(os.path.join(ROOT, "*")) if os.path.isfile(os.path.join(d, "umbrel-app.yml")))


def load(app_dir):
    with open(os.path.join(app_dir, "docker-compose.yml")) as fh:
        compose_text = fh.read()
    with open(os.path.join(app_dir, "umbrel-app.yml")) as fh:
        manifest_text = fh.read()
    return compose_text, yaml.safe_load(compose_text), manifest_text, yaml.safe_load(manifest_text)


def main_service(compose, app_id):
    env = ((compose.get("services") or {}).get("app_proxy") or {}).get("environment") or {}
    host = str(env.get("APP_HOST", ""))
    prefix = f"{app_id}_"
    if host.startswith(prefix) and host.endswith("_1"):
        return host[len(prefix):-2]
    return None


def check_image(ref):
    """What's the newest acceptable version of this image reference? Returns a dict or None."""
    registry_host, repo, tag, pinned = registry.parse_ref(ref)
    if not pinned:
        return None  # moving tags (e.g. YAMS's :latest) are updated by the app itself
    name = ref.split("@", 1)[0].rsplit(":", 1)[0]
    shown = registry.display_name(registry_host, repo)
    hold, hold_minor = bool(HOLD_MAJOR.search(shown)), bool(HOLD_MINOR.search(shown))
    try:
        tags = registry.list_tags(ref)
    except registry.RegistryError as err:
        return {"ref": ref, "error": str(err)}
    skipped = []
    built_now = None
    for new_tag in reversed(candidates(tag, tags, hold or hold_minor, hold_minor)):
        # Some images keep very old tags around that sort as "newer" (an old 5.9.5 next to a
        # current 0.11.0). Only accept a tag that was actually built after the one in use.
        if built_now is None:
            built_now = registry.created(ref, tag) or ""
        built = registry.created(ref, new_tag) or ""
        if built_now and built and built[:19] < built_now[:19]:
            continue
        try:
            arches = registry.architectures(ref, new_tag)
        except registry.RegistryError as err:
            skipped.append(f"{new_tag}: {err}")
            continue
        if not REQUIRED_ARCHES <= arches:
            skipped.append(f"{new_tag}: only {', '.join(sorted(arches)) or 'unknown'}")
            continue
        new_digest = registry.digest(ref, new_tag)
        return {"ref": ref, "new_ref": f"{name}:{new_tag}@{new_digest}", "old_tag": tag, "new_tag": new_tag,
                "kind": classify(tag, new_tag), "skipped": skipped}
    # No newer release: was the current tag rebuilt?
    try:
        current = registry.digest(ref, tag)
    except registry.RegistryError as err:
        return {"ref": ref, "error": str(err), "skipped": skipped}
    if current and current != pinned:
        return {"ref": ref, "new_ref": f"{name}:{tag}@{current}", "old_tag": tag, "new_tag": tag,
                "kind": "rebuild", "skipped": skipped}
    return {"ref": ref, "skipped": skipped} if skipped else None


def bump_patch(version):
    parts = version.split(".")
    if parts and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return version + ".1"


def release_note(app_name, changes, repo_url):
    lines = []
    for c in changes:
        image = c["ref"].split("@")[0].rsplit(":", 1)[0].rsplit("/", 1)[-1]
        if c["kind"] == "rebuild":
            line = f"{image} {c['new_tag']} rebuilt with the latest security fixes"
        else:
            line = f"{image} {c['old_tag']} → {c['new_tag']}"
        if line not in lines:  # the same image can serve several services
            lines.append(line)
    note = f"Updated {app_name} automatically by Canopy: " + "; ".join(lines) + "."
    if repo_url and "github.com" in repo_url:
        note += f" What's new: {repo_url.rstrip('/')}/releases"
    return note


def set_field(text, key, value):
    """Replace a top-level 'key: value' line in YAML text, keeping everything else as is."""
    if key == "releaseNotes":
        pattern = re.compile(r"^releaseNotes:.*?(?=^\S)", re.M | re.S)
        replacement = "releaseNotes: >-\n  " + value + "\n"
        return pattern.sub(lambda _: replacement, text, count=1) if pattern.search(text) else text + replacement
    pattern = re.compile(rf"^{key}:.*$", re.M)
    return pattern.sub(lambda _: f'{key}: "{value}"', text, count=1)


def plan_app(app_dir):
    compose_text, compose, manifest_text, manifest = load(app_dir)
    app_id = manifest["id"]
    main = main_service(compose, app_id)
    changes = []
    problems = []
    for service, svc in (compose.get("services") or {}).items():
        ref = (svc or {}).get("image")
        if not ref or service == "app_proxy":
            continue
        result = check_image(ref)
        if not result:
            continue
        if result.get("error"):
            problems.append(f"{service}: {result['error']}")
            continue
        for s in result.get("skipped") or []:
            problems.append(f"{service}: skipped {s}")
        if result.get("new_ref"):
            result["service"] = service
            result["main"] = service == main
            changes.append(result)
    return {"app": app_id, "dir": app_dir, "name": manifest.get("name", app_id), "version": manifest.get("version"),
            "repo": manifest.get("repo"), "main": main, "changes": changes, "problems": problems,
            "kind": max((c["kind"] for c in changes), key=["rebuild", "patch", "minor", "major"].index, default=None)}


def apply_plan(p):
    compose_path = os.path.join(p["dir"], "docker-compose.yml")
    manifest_path = os.path.join(p["dir"], "umbrel-app.yml")
    with open(compose_path) as fh:
        compose_text = fh.read()
    with open(manifest_path) as fh:
        manifest_text = fh.read()
    for c in p["changes"]:
        if c["ref"] in compose_text:
            compose_text = compose_text.replace(c["ref"], c["new_ref"])  # every service using it
        elif c["new_ref"] not in compose_text:
            raise SystemExit(f"{p['app']}: couldn't find {c['ref']} in docker-compose.yml")

    main_change = next((c for c in p["changes"] if c["main"]), None)
    old_version = str(p["version"])
    main_tag_matches = main_change and old_version == main_change["old_tag"].lstrip("v").split("-")[0]
    if main_change and main_tag_matches and main_change["kind"] != "rebuild":
        new_version = main_change["new_tag"].lstrip("v").split("-")[0]
    else:
        # Own version number (YAMS), a rebuild, or a helper image changed: bump the patch
        # so umbrelOS offers the update.
        new_version = bump_patch(old_version)
        if "YAMS_VERSION" in compose_text:
            compose_text = re.sub(r'(YAMS_VERSION:\s*")[^"]*(")', lambda m: m.group(1) + new_version + m.group(2),
                                  compose_text)
    manifest_text = set_field(manifest_text, "version", new_version)
    manifest_text = set_field(manifest_text, "releaseNotes", release_note(p["name"], p["changes"], p["repo"]))
    with open(compose_path, "w") as fh:
        fh.write(compose_text)
    with open(manifest_path, "w") as fh:
        fh.write(manifest_text)
    p["new_version"] = new_version
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="edit the files")
    ap.add_argument("--json", help="write the plan here")
    ap.add_argument("--only", nargs="*", help="app ids to check")
    args = ap.parse_args()
    plans = []
    for app_dir in app_dirs():
        app_id = os.path.basename(app_dir)
        if args.only and app_id not in args.only:
            continue
        p = plan_app(app_dir)
        for problem in p["problems"]:
            print(f"[{app_id}] note: {problem}")
        if not p["changes"]:
            print(f"[{app_id}] up to date ({p['version']})")
            continue
        if args.apply:
            apply_plan(p)
        for c in p["changes"]:
            print(f"[{app_id}] {c['kind']}: {c['service']} {c['old_tag']} -> {c['new_tag']}")
        plans.append(p)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                       "apps": plans}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
