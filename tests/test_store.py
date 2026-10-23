"""Rules every app in the store follows. These run offline on every pull request."""
import glob
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = yaml.safe_load(open(os.path.join(ROOT, "umbrel-app-store.yml")))
APPS = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "*", "umbrel-app.yml")))
REQUIRED = ["manifestVersion", "id", "category", "name", "version", "tagline", "icon", "description", "developer",
            "website", "repo", "support", "port", "gallery", "path", "releaseNotes", "submitter", "submission"]
CATEGORIES = {"files", "bitcoin", "media", "networking", "social", "automation", "finance", "ai", "developer",
              "crypto", "gaming"}
# YAMS updates its media apps itself (Update apps button), so those use moving tags.
MOVING_TAGS_OK = {"canopy-yams"}


def manifest(app):
    with open(os.path.join(ROOT, app, "umbrel-app.yml")) as fh:
        return yaml.safe_load(fh)


def compose(app):
    with open(os.path.join(ROOT, app, "docker-compose.yml")) as fh:
        return yaml.safe_load(fh)


def test_store_has_apps():
    assert STORE["id"] == "canopy"
    assert len(APPS) >= 11


@pytest.mark.parametrize("app", APPS)
def test_manifest_is_complete(app):
    m = manifest(app)
    missing = [k for k in REQUIRED if k not in m]
    assert not missing, f"missing {missing}"
    assert m["id"] == app, "the folder name must be the app id"
    assert app.startswith(STORE["id"] + "-"), "app ids must start with the store id"
    assert m["category"] in CATEGORIES
    assert isinstance(m["version"], str) and m["version"], "quote the version"
    assert m["icon"].startswith("https://")
    assert ("defaultPassword" in m) or m.get("deterministicPassword") is True


@pytest.mark.parametrize("app", APPS)
def test_compose_follows_umbrel_conventions(app):
    c = compose(app)
    services = c["services"]
    env = services["app_proxy"]["environment"]
    host = env["APP_HOST"]
    assert host.startswith(f"{app}_") and host.endswith("_1"), "APP_HOST must be <app-id>_<service>_1"
    assert host[len(app) + 1:-2] in services, "APP_HOST must name a service in this file"
    assert str(env["APP_PORT"]).isdigit()
    uses_password = "APP_PASSWORD" in open(os.path.join(ROOT, app, "docker-compose.yml")).read()
    if uses_password:
        assert manifest(app).get("deterministicPassword") is True, "APP_PASSWORD needs deterministicPassword: true"


@pytest.mark.parametrize("app", APPS)
def test_images_are_pinned_releases(app):
    for name, svc in compose(app)["services"].items():
        image = (svc or {}).get("image")
        if not image:
            continue
        if app in MOVING_TAGS_OK and "@sha256:" not in image:
            continue
        assert re.search(r":[^/:@]+@sha256:[0-9a-f]{64}$", image), f"{name}: pin a version tag and its digest ({image})"
        assert ":latest@" not in image, f"{name}: use a version tag, not latest"


@pytest.mark.parametrize("app", APPS)
def test_downloads_folder_needs_permission(app):
    text = open(os.path.join(ROOT, app, "docker-compose.yml")).read()
    if "/data/storage/downloads" in text:
        assert "STORAGE_DOWNLOADS" in (manifest(app).get("permissions") or []), \
            "apps that use the Downloads folder must ask for STORAGE_DOWNLOADS"


def test_ports_are_unique_within_the_store():
    seen = {}
    for app in APPS:
        ports = [manifest(app)["port"]]
        for svc in compose(app)["services"].values():
            for p in (svc or {}).get("ports") or []:
                ports.append(int(str(p).split(":")[0]))
        for port in ports:
            assert port not in seen, f"{app} and {seen[port]} both use port {port}"
            seen[port] = app


@pytest.mark.parametrize("app", APPS)
def test_no_placeholder_files_where_apps_need_empty_folders(app):
    # postgres and openHAB refuse to initialise a folder that already has files in it
    for svc in compose(app)["services"].values():
        image = (svc or {}).get("image") or ""
        for vol in (svc or {}).get("volumes") or []:
            host_path, _, target = str(vol).partition(":")
            target = target.split(":")[0]
            if not host_path.startswith("${APP_DATA_DIR}/"):
                continue
            local = os.path.join(ROOT, app, host_path[len("${APP_DATA_DIR}/"):])
            empty_needed = target == "/var/lib/postgresql/data" and "PGDATA" not in str(svc.get("environment")) \
                or target.startswith("/openhab/")
            if empty_needed:
                assert not os.path.exists(os.path.join(local, ".gitkeep")), f"{local} must start empty"


def test_website_lists_every_app_at_its_version():
    import subprocess
    import sys
    res = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_site.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout
