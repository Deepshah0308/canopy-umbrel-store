#!/usr/bin/env python3
"""
Store checks that need the internet. CI runs them on every pull request:

  1. Every pinned image runs on both amd64 (Umbrel Home, PCs) and arm64 (Raspberry Pi).
  2. No app's port clashes with an app in Umbrel's official store or the popular
     community stores (two apps on one port can't both run).
  3. No app duplicates one in Umbrel's official store (Canopy only carries apps
     Umbrel doesn't have; YAMS is a bundle, so it's exempt).

    python3 tools/lint_store.py [app-id ...]
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_ARCHES = {"linux/amd64", "linux/arm64"}
OTHER_STORES = {
    "official": "https://github.com/getumbrel/umbrel-apps",
    "big-bear": "https://github.com/bigbeartechworld/big-bear-umbrel",
    "denny": "https://github.com/dennysubke/dennys-umbrel-app-store",
}
BUNDLES = {"canopy-yams"}
# Known clashes we accept, with the reason. Keep this list short.
ACCEPTED_CLASHES = {
    # Jellyfin's port since YAMS 1.0: changing it would make everyone re-add the server on
    # every TV and phone. Only matters if Denny's Emby is installed too.
    ("canopy-yams", 8097, "denny-emby"),
}


def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def load_yaml(path):
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def ports_of(app_dir):
    ports = set()
    m = load_yaml(os.path.join(app_dir, "umbrel-app.yml"))
    if str(m.get("port", "")).isdigit():
        ports.add(int(m["port"]))
    text = ""
    try:
        with open(os.path.join(app_dir, "docker-compose.yml")) as fh:
            text = fh.read()
    except OSError:
        pass
    for match in re.finditer(r"^\s*-\s*[\"']?(\d{2,5}):\d", text, re.M):
        ports.add(int(match.group(1)))
    return ports


def clone(url, dest):
    subprocess.run(["git", "clone", "-q", "--depth", "1", url, dest], check=True)


def main(only):
    problems = []
    apps = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(ROOT, "*", "umbrel-app.yml")))
    if only:
        apps = [a for a in apps if os.path.basename(a) in only]

    # 1. architectures
    for app_dir in apps:
        compose = load_yaml(os.path.join(app_dir, "docker-compose.yml"))
        for service, svc in (compose.get("services") or {}).items():
            image = (svc or {}).get("image")
            if not image or "@sha256:" not in image:
                continue
            try:
                arches = registry.architectures(image)
            except registry.RegistryError as err:
                problems.append(f"{os.path.basename(app_dir)}/{service}: couldn't check {image}: {err}")
                continue
            if not REQUIRED_ARCHES <= arches:
                problems.append(f"{os.path.basename(app_dir)}/{service}: {image} runs on "
                                f"{', '.join(sorted(arches)) or 'nothing we can see'}, needs amd64 and arm64")

    # 2 and 3. other stores
    with tempfile.TemporaryDirectory() as tmp:
        others = {}
        for name, url in OTHER_STORES.items():
            dest = os.path.join(tmp, name)
            try:
                clone(url, dest)
            except subprocess.CalledProcessError:
                print(f"note: couldn't fetch the {name} store; skipping its checks")
                continue
            others[name] = [os.path.dirname(p) for p in glob.glob(os.path.join(dest, "*", "umbrel-app.yml"))]
        for app_dir in apps:
            app = os.path.basename(app_dir)
            mine = ports_of(app_dir)
            m = load_yaml(os.path.join(app_dir, "umbrel-app.yml"))
            for store, dirs in others.items():
                for other in dirs:
                    clash = {p for p in mine & ports_of(other)
                             if (app, p, os.path.basename(other)) not in ACCEPTED_CLASHES}
                    if clash:
                        problems.append(f"{app}: port {', '.join(map(str, sorted(clash)))} is also used by "
                                        f"{os.path.basename(other)} in the {store} store")
                    if store == "official" and app not in BUNDLES:
                        om = load_yaml(os.path.join(other, "umbrel-app.yml"))
                        if norm(om.get("name")) == norm(m.get("name")):
                            problems.append(f"{app}: Umbrel's official store already has {om.get('name')}; "
                                            "Canopy doesn't duplicate official apps")

    for p in problems:
        print(f"PROBLEM {p}")
    print(f"Checked {len(apps)} app(s): {'all good' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
