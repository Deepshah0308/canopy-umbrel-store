"""
YAMS for Umbrel - dashboard + auto-configurator.

Pure standard-library Python so it runs on the stock python:alpine image.
  * Serves the setup dashboard (index.html) and a small JSON API.
  * In the background, wires every app together through their APIs:
      Sonarr/Radarr -> qBittorrent + library folders + file renaming
      Prowlarr      -> Sonarr, Radarr, FlareSolverr
      Jellyfin      -> first-run wizard, admin user, Movies & Shows libraries
  * Switches the optional Extras on and off and connects them too (see extras.py):
      Seerr, Lidarr, Recyclarr, Unpackerr, Decluttarr
  * Accepts a pasted WireGuard file and turns it into something gluetun accepts.
  * Talks to Docker (only this app's containers) for status, logs and restarts.
  * Serves the Umbrel home-screen widgets.
  * Backs up and restores every app's settings (see backups.py).
  * Updates the apps to their newest images on demand or weekly (see updater.py).
  * Offers one-click fixes for the most common problems.
"""
import configparser
import http.client as httpclient
import ipaddress
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import backups
import extras
import updater

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
from seed import friendly_password, migrate_container_names, qbit_hash  # noqa: E402  (same helpers the first boot uses)

PREFIX = os.environ.get("APP_PREFIX", "canopy-yams")
JELLYFIN_PORT = os.environ.get("JELLYFIN_PORT", "8097")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", "/yams/state")
WG_PATH = os.environ.get("WG_PATH", "/yams/vpn/wireguard/wg0.conf")
YAMS_VERSION = os.environ.get("YAMS_VERSION", "")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# The dashboard's controls are only served on this socket, which only the router
# (Caddy, behind your Umbrel login) can reach. The network port serves widgets only.
API_SOCK = os.environ.get("API_SOCK", "/sock/dashboard.sock")
QBIT_CONF = os.environ.get("QBIT_CONF", "/yams/config/qbittorrent/qBittorrent/qBittorrent.conf")
SECRETS_PATH = f"{STATE_DIR}/secrets.json"
SETUP_PATH = f"{STATE_DIR}/setup.json"


def host(service):
    return f"{PREFIX}_{service}_1"


SONARR = f"http://{host('sonarr')}:8989/sonarr"
RADARR = f"http://{host('radarr')}:7878/radarr"
PROWLARR = f"http://{host('prowlarr')}:9696/prowlarr"
BAZARR = f"http://{host('bazarr')}:6767/bazarr"
LIDARR = f"http://{host('lidarr')}:8686/lidarr"
SEERR = f"http://{host('seerr')}:5055"
SEERR_PORT = os.environ.get("SEERR_PORT", "7475")
JELLYFIN = f"http://{host('jellyfin')}:8096"
QBIT_HOST = host("gluetun")  # qBittorrent shares gluetun's network
GLUETUN = f"http://{host('gluetun')}:8000"
FLARESOLVERR = f"http://{host('flaresolverr')}:8191/"
JELLYFIN_AUTH = 'MediaBrowser Client="YAMS", Device="YAMS dashboard", DeviceId="yams-dashboard", Version="1.0"'


# --------------------------------------------------------------------------- helpers
def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def secrets():
    return read_json(SECRETS_PATH, {})


extras.hooks.update(host=host, secrets=secrets)


def http(method, url, body=None, headers=None, timeout=8):
    """Returns (status, parsed_body). status is 0 when the server can't be reached."""
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        raw = err.read()
        status = err.code
    except Exception:
        return 0, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def ok(status):
    return 200 <= status < 300


# --------------------------------------------------------------------------- setup log
setup_lock = threading.Lock()


def setup_state():
    return read_json(SETUP_PATH, {"done": {}, "message": "Getting everything ready…"})


def mark(task, message=None):
    with setup_lock:
        state = setup_state()
        state["done"][task] = True
        if message:
            state["message"] = message
        write_json(SETUP_PATH, state)


def say(message):
    with setup_lock:
        state = setup_state()
        state["message"] = message
        write_json(SETUP_PATH, state)
    print(f"[yams] {message}", flush=True)


def is_done(task):
    return setup_state()["done"].get(task, False)


def unmark(task):
    with setup_lock:
        state = setup_state()
        state["done"].pop(task, None)
        write_json(SETUP_PATH, state)


def wait_for(url, headers=None, label="app", timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        status, _ = http("GET", url, headers=headers, timeout=5)
        if ok(status):
            return True
        time.sleep(5)
    say(f"{label} is taking a while to start. YAMS will keep trying.")
    return False


# --------------------------------------------------------------------------- Sonarr / Radarr
ARR = {
    "sonarr": {"title": "Sonarr", "base": SONARR, "key": "sonarr_key", "root": "/data/media/tv",
               "category_field": "tvCategory", "category": "tv", "rename_field": "renameEpisodes"},
    "radarr": {"title": "Radarr", "base": RADARR, "key": "radarr_key", "root": "/data/media/movies",
               "category_field": "movieCategory", "category": "movies", "rename_field": "renameMovies"},
    # An Extra: only used while it's switched on
    "lidarr": {"title": "Lidarr", "base": LIDARR, "key": "lidarr_key", "root": "/data/media/music",
               "category_field": "musicCategory", "category": "music", "rename_field": "renameTracks",
               "api": "v1", "extra": True},
}
CLIENT_NAME = "qBittorrent (VPN)"  # Decluttarr's settings use the same name
# The first-run setup, the 10-minute watcher, Quick fixes and Extras can all try to add
# the same entry at once; one at a time avoids duplicates.
wiring_lock = threading.RLock()


def one_at_a_time(fn):
    def wrapper(*args, **kwargs):
        with wiring_lock:
            return fn(*args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper
# kind -> {"state": "connected" | "unreachable" | "missing" | "unknown", "at": time}
client_status = {kind: {"state": "unknown", "at": 0} for kind in ("sonarr", "radarr", "lidarr")}


def arr_kinds():
    """Sonarr and Radarr, plus Lidarr while it's switched on."""
    return [k for k in ARR if not ARR[k].get("extra") or extras.enabled(k)]


def arr_titles(kinds):
    names = [ARR[k]["title"] for k in kinds]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def arr_api(kind):
    cfg = ARR[kind]
    return f"{cfg['base']}/api/{cfg.get('api', 'v3')}", {"X-Api-Key": secrets().get(cfg["key"], "")}


def ensure_root_folder(kind):
    api, h = arr_api(kind)
    root = ARR[kind]["root"]
    status, folders = http("GET", f"{api}/rootfolder", headers=h)
    if not ok(status):
        return False
    if any(f.get("path", "").rstrip("/") == root for f in folders or []):
        return True
    body = {"path": root}
    if kind == "lidarr":
        # Lidarr's root folders also carry the defaults new artists get.
        _, quality = http("GET", f"{api}/qualityprofile", headers=h)
        _, metadata = http("GET", f"{api}/metadataprofile", headers=h)
        if not quality or not metadata:
            return False
        meta = next((m for m in metadata if m.get("name") == "Standard"), metadata[0])
        body.update(name="Music", defaultQualityProfileId=quality[0]["id"], defaultMetadataProfileId=meta["id"],
                    defaultMonitorOption="all", defaultNewItemMonitorOption="all", defaultTags=[])
    status, _ = http("POST", f"{api}/rootfolder", body, headers=h)
    return ok(status)


def _client_values(kind):
    s = secrets()
    cfg = ARR[kind]
    return {
        "host": QBIT_HOST,
        "port": 8080,
        "useSsl": False,
        "urlBase": "",
        "username": s.get("username", ""),
        "password": s.get("password", ""),
        cfg["category_field"]: cfg["category"],
    }


def _apply_values(fields, values):
    for field in fields:
        if field.get("name") in values:
            field["value"] = values[field["name"]]
    have = {f.get("name") for f in fields}
    for name, value in values.items():
        if name not in have:
            fields.append({"name": name, "value": value})
    return fields


@one_at_a_time
def ensure_download_client(kind, force=False):
    """Make sure Sonarr/Radarr/Lidarr has a qBittorrent entry. Adds it, or repairs one
    that points somewhere else or was switched off. force=True rewrites the login too
    (after a password change). Returns True when it's in place.

    The apps refuse to save a download client they can't reach (even with forceSave),
    and qBittorrent only runs once the VPN is up. So before that, the entry is saved
    switched off, and switched on as soon as qBittorrent answers."""
    api, h = arr_api(kind)
    title = ARR[kind]["title"]
    values = _client_values(kind)
    status, clients = http("GET", f"{api}/downloadclient", headers=h)
    if not ok(status) or not isinstance(clients, list):
        return False
    existing = [c for c in clients if c.get("implementation") == "QBittorrent"]
    if existing:
        client = existing[0]
        current = {f.get("name"): f.get("value") for f in client.get("fields", [])}
        settings_wrong = force or (current.get("host") != QBIT_HOST or str(current.get("port")) != "8080"
                                   or current.get("username") != values["username"])
        if not settings_wrong and client.get("enable"):
            return True
        _apply_values(client.setdefault("fields", []), values)
        client["enable"] = True
        status, _ = http("PUT", f"{api}/downloadclient/{client['id']}?forceSave=true", client, headers=h)
        if ok(status):
            print(f"[yams] {'Switched on' if not settings_wrong else 'Repaired'} the qBittorrent connection in {title}", flush=True)
            return True
        if not settings_wrong:
            return True  # right settings, qBittorrent just isn't up yet: leave it off for now
        client["enable"] = False
        status, _ = http("PUT", f"{api}/downloadclient/{client['id']}?forceSave=true", client, headers=h)
        if ok(status):
            print(f"[yams] Updated the qBittorrent connection in {title}; it switches on once qBittorrent is running", flush=True)
        return ok(status)

    # Start from the app's own blank qBittorrent form so every field it expects is present.
    status, schema = http("GET", f"{api}/downloadclient/schema", headers=h)
    template = None
    if ok(status) and isinstance(schema, list):
        template = next((x for x in schema if x.get("implementation") == "QBittorrent"), None)
    if template:
        body = {k: v for k, v in template.items() if k not in ("id", "presets")}
        body["fields"] = _apply_values(body.get("fields", []), values)
    else:
        body = {"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
                "protocol": "torrent", "fields": _apply_values([], values)}
    body.update(name=CLIENT_NAME, enable=True, priority=1, tags=[],
                removeCompletedDownloads=True, removeFailedDownloads=True)
    status, result = http("POST", f"{api}/downloadclient?forceSave=true", body, headers=h)
    if ok(status):
        print(f"[yams] Added qBittorrent to {title}", flush=True)
        return True
    body["enable"] = False
    status, result2 = http("POST", f"{api}/downloadclient?forceSave=true", body, headers=h)
    if ok(status):
        print(f"[yams] Added qBittorrent to {title}; it switches on once qBittorrent is running behind the VPN", flush=True)
        client_status[kind] = {"state": "waiting", "at": time.time()}
        return True
    print(f"[yams] {title} didn't accept the qBittorrent entry ({status}): {str(result2 or result)[:300]}", flush=True)
    return False


def enable_download_clients():
    """qBittorrent just came up: switch on any entries that were saved switched off."""
    for kind in arr_kinds():
        try:
            if ensure_download_client(kind):
                test_download_client(kind)
        except Exception:
            traceback.print_exc()


def test_download_client(kind):
    """Ask Sonarr/Radarr to test its qBittorrent entry, the same as its Test button."""
    api, h = arr_api(kind)
    status, clients = http("GET", f"{api}/downloadclient", headers=h)
    client = next((c for c in clients or [] if c.get("implementation") == "QBittorrent"), None) if ok(status) else None
    if not client:
        state = "missing"
    elif not client.get("enable"):
        state = "waiting"
    else:
        status, _ = http("POST", f"{api}/downloadclient/test", client, headers=h, timeout=20)
        state = "connected" if ok(status) else "unreachable"
    client_status[kind] = {"state": state, "at": time.time()}
    return state


def wire_arr(kind):
    cfg = ARR[kind]
    api, h = arr_api(kind)
    if is_done(kind):
        return True
    say(f"Waiting for {cfg['title']} to start\u2026")
    if not wait_for(f"{api}/system/status", h, cfg["title"]):
        return False
    say(f"Connecting {cfg['title']} to your library and qBittorrent\u2026")
    if not ensure_root_folder(kind) or not ensure_download_client(kind):
        say(f"{cfg['title']} isn't fully connected yet. YAMS will try again shortly.")
        return False

    # Tidy file names (the default is off)
    status, naming = http("GET", f"{api}/config/naming", headers=h)
    if ok(status) and isinstance(naming, dict) and not naming.get(cfg["rename_field"]):
        naming[cfg["rename_field"]] = True
        http("PUT", f"{api}/config/naming/{naming.get('id', 1)}", naming, headers=h)

    mark(kind, f"{cfg['title']} is connected.")
    return True


def connection_watch():
    """Every 10 minutes: re-check that Sonarr and Radarr still reach qBittorrent, and
    repair their settings if needed (this also fixes installs made by older versions)."""
    time.sleep(30)
    while True:
        for kind in arr_kinds():
            try:
                api, h = arr_api(kind)
                status, _ = http("GET", f"{api}/system/status", headers=h, timeout=5)
                if not ok(status):
                    continue
                ensure_root_folder(kind)
                ensure_download_client(kind)
                vpn_up = (status_cache["data"] or {}).get("answering", {}).get("qbittorrent")
                if vpn_up:
                    test_download_client(kind)
                else:
                    client_status[kind] = {"state": "waiting", "at": time.time()}
            except Exception:
                traceback.print_exc()
        try:
            api, h = prowlarr_api()
            status, _ = http("GET", f"{api}/system/status", headers=h, timeout=5)
            if ok(status):
                ensure_flaresolverr()
                if not all([ensure_prowlarr_app(k) for k in arr_kinds()]):
                    print("[yams] Prowlarr's links need attention; will retry in 10 minutes", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(600)


# --------------------------------------------------------------------------- Prowlarr
PROWLARR_APPS = {
    "sonarr": {"name": "Sonarr", "cats": [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090], "anime": [5070]},
    "radarr": {"name": "Radarr", "cats": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090], "anime": None},
    "lidarr": {"name": "Lidarr", "cats": [3000, 3010, 3030, 3040, 3050, 3060], "anime": None},
}


def prowlarr_api():
    return f"{PROWLARR}/api/v1", {"X-Api-Key": secrets().get("prowlarr_key", "")}


def _prowlarr_app_values(kind):
    cfg, app = ARR[kind], PROWLARR_APPS[kind]
    values = {
        "prowlarrUrl": PROWLARR,
        "baseUrl": cfg["base"],
        "apiKey": secrets().get(cfg["key"], ""),
        "syncCategories": app["cats"],
    }
    if app["anime"]:
        values["animeSyncCategories"] = app["anime"]
    return values


@one_at_a_time
def ensure_prowlarr_app(kind):
    """Make sure Prowlarr pushes its indexers to Sonarr/Radarr. Adds the link, or repairs
    one that points at the wrong address or fails Prowlarr's own connection test."""
    api, h = prowlarr_api()
    app = PROWLARR_APPS[kind]
    values = _prowlarr_app_values(kind)
    status, apps = http("GET", f"{api}/applications", headers=h)
    if not ok(status) or not isinstance(apps, list):
        return False
    existing = next((a for a in apps if a.get("implementation") == app["name"]), None)
    if existing:
        current = {f.get("name"): f.get("value") for f in existing.get("fields", [])}
        pointed_right = (current.get("baseUrl") or "").rstrip("/") == values["baseUrl"] and \
            (current.get("prowlarrUrl") or "").rstrip("/") == values["prowlarrUrl"]
        if pointed_right and existing.get("syncLevel") != "disabled":
            test_status, _ = http("POST", f"{api}/applications/test", existing, headers=h, timeout=20)
            if ok(test_status):
                return True
        existing["syncLevel"] = "fullSync"
        _apply_values(existing.setdefault("fields", []), values)
        status, _ = http("PUT", f"{api}/applications/{existing['id']}?forceSave=true", existing, headers=h)
        if ok(status):
            print(f"[yams] Repaired Prowlarr's link to {app['name']}", flush=True)
        return ok(status)

    status, schema = http("GET", f"{api}/applications/schema", headers=h)
    template = None
    if ok(status) and isinstance(schema, list):
        template = next((x for x in schema if x.get("implementation") == app["name"]), None)
    if template:
        body = {k: v for k, v in template.items() if k not in ("id", "presets")}
        body["fields"] = _apply_values(body.get("fields", []), values)
    else:
        body = {"implementation": app["name"], "configContract": f"{app['name']}Settings",
                "fields": _apply_values([], values)}
    body.update(name=app["name"], syncLevel="fullSync", tags=[])
    status, result = http("POST", f"{api}/applications?forceSave=true", body, headers=h)
    if ok(status):
        print(f"[yams] Linked Prowlarr to {app['name']}", flush=True)
        return True
    print(f"[yams] Prowlarr didn't accept the {app['name']} link ({status}): {str(result)[:300]}", flush=True)
    return False


@one_at_a_time
def ensure_flaresolverr():
    """FlareSolverr, used by any indexer tagged "flaresolverr"."""
    api, h = prowlarr_api()
    status, tags = http("GET", f"{api}/tag", headers=h)
    if not ok(status):
        return False
    tag_id = next((t["id"] for t in tags or [] if t.get("label") == "flaresolverr"), None)
    if tag_id is None:
        status, created = http("POST", f"{api}/tag", {"label": "flaresolverr"}, headers=h)
        if not ok(status):
            return False
        tag_id = created["id"]
    status, proxies = http("GET", f"{api}/indexerProxy", headers=h)
    if not ok(status):
        return False
    values = {"host": FLARESOLVERR, "requestTimeout": 60}
    existing = next((p for p in proxies or [] if p.get("implementation") == "FlareSolverr"), None)
    if existing:
        current = {f.get("name"): f.get("value") for f in existing.get("fields", [])}
        if (current.get("host") or "").rstrip("/") == FLARESOLVERR.rstrip("/"):
            return True
        _apply_values(existing.setdefault("fields", []), {"host": FLARESOLVERR})
        status, _ = http("PUT", f"{api}/indexerProxy/{existing['id']}?forceSave=true", existing, headers=h)
        if ok(status):
            print("[yams] Pointed Prowlarr's FlareSolverr at this app", flush=True)
        return ok(status)
    status, schema = http("GET", f"{api}/indexerProxy/schema", headers=h)
    template = next((x for x in schema or [] if x.get("implementation") == "FlareSolverr"), None) if ok(status) else None
    if template:
        body = {k: v for k, v in template.items() if k not in ("id", "presets")}
        body["fields"] = _apply_values(body.get("fields", []), values)
    else:
        body = {"implementation": "FlareSolverr", "configContract": "FlareSolverrSettings",
                "fields": _apply_values([], values)}
    body.update(name="FlareSolverr", tags=[tag_id])
    status, _ = http("POST", f"{api}/indexerProxy?forceSave=true", body, headers=h)
    return ok(status)


def remove_prowlarr_app(kind):
    """When an Extra is switched off, stop Prowlarr pushing indexers to it."""
    api, h = prowlarr_api()
    status, apps = http("GET", f"{api}/applications", headers=h)
    if not ok(status):
        return False
    for app in apps or []:
        if app.get("implementation") == PROWLARR_APPS[kind]["name"]:
            http("DELETE", f"{api}/applications/{app['id']}", headers=h)
    return True


def sync_prowlarr_indexers():
    api, h = prowlarr_api()
    status, _ = http("POST", f"{api}/command", {"name": "ApplicationIndexerSync"}, headers=h)
    return ok(status)


def wire_prowlarr():
    if is_done("prowlarr"):
        return True
    api, h = prowlarr_api()
    say("Waiting for Prowlarr to start\u2026")
    if not wait_for(f"{api}/system/status", h, "Prowlarr"):
        return False
    say("Connecting Prowlarr to Sonarr, Radarr and FlareSolverr\u2026")
    ensure_flaresolverr()
    linked = all([ensure_prowlarr_app("sonarr"), ensure_prowlarr_app("radarr")])
    if not linked:
        say("Prowlarr isn't linked to Sonarr and Radarr yet. YAMS will try again shortly.")
        return False
    sync_prowlarr_indexers()
    mark("prowlarr", "Prowlarr is connected.")
    return True


# --------------------------------------------------------------------------- Jellyfin
def jellyfin_token(s):
    status, auth = http(
        "POST",
        f"{JELLYFIN}/Users/AuthenticateByName",
        {"Username": s["username"], "Pw": s["password"]},
        headers={"Authorization": JELLYFIN_AUTH},
    )
    if ok(status) and isinstance(auth, dict):
        return auth.get("AccessToken")
    return None


def wire_jellyfin():
    if is_done("jellyfin"):
        return True
    s = secrets()
    say("Waiting for Jellyfin to start…")
    if not wait_for(f"{JELLYFIN}/System/Info/Public", label="Jellyfin"):
        return False
    _, info = http("GET", f"{JELLYFIN}/System/Info/Public")
    if isinstance(info, dict) and not info.get("StartupWizardCompleted"):
        say("Setting up Jellyfin…")
        http("POST", f"{JELLYFIN}/Startup/Configuration", {
            "UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en",
        })
        http("GET", f"{JELLYFIN}/Startup/User")
        http("POST", f"{JELLYFIN}/Startup/User", {"Name": s["username"], "Password": s["password"]})
        http("POST", f"{JELLYFIN}/Startup/RemoteAccess", {
            "EnableRemoteAccess": True, "EnableAutomaticPortMapping": False,
        })
        http("POST", f"{JELLYFIN}/Startup/Complete")
        time.sleep(3)

    h = jellyfin_headers()
    if not h:
        say("Jellyfin was already set up by hand, so YAMS left it alone.")
        mark("jellyfin")
        return True
    ensure_jellyfin_libraries(h)
    mark("jellyfin", "Jellyfin is ready with Movies and Shows libraries.")
    return True


def jellyfin_headers():
    token = jellyfin_token(secrets())
    return {"Authorization": f'{JELLYFIN_AUTH}, Token="{token}"'} if token else None


def ensure_jellyfin_libraries(h):
    """Movies and Shows libraries, watching the folders Sonarr and Radarr fill. Returns
    the libraries it had to add."""
    status, folders = http("GET", f"{JELLYFIN}/Library/VirtualFolders", headers=h)
    if not ok(status):
        return None
    existing = folders or []
    paths = {p for f in existing for p in (f.get("Locations") or [])}
    added = []
    wanted = [("Movies", "movies", "/data/media/movies"), ("Shows", "tvshows", "/data/media/tv")]
    if extras.enabled("lidarr"):
        wanted.append(("Music", "music", "/data/media/music"))
    for name, ctype, path in wanted:
        if path in paths or any(f.get("Name") == name for f in existing):
            continue
        query = urllib.parse.urlencode({"name": name, "collectionType": ctype, "refreshLibrary": "true"})
        status, _ = http("POST", f"{JELLYFIN}/Library/VirtualFolders?{query}", {
            "LibraryOptions": {"EnableRealtimeMonitor": True, "PathInfos": [{"Path": path}]}
        }, headers=h, timeout=30)
        if ok(status):
            added.append(name)
    return added


def configurator():
    tasks = [("sonarr", lambda: wire_arr("sonarr")),
             ("radarr", lambda: wire_arr("radarr")),
             ("prowlarr", wire_prowlarr),
             ("jellyfin", wire_jellyfin)]
    while True:
        pending = False
        for name, task in tasks:
            try:
                if not task():
                    pending = True
            except Exception:
                pending = True
                traceback.print_exc()
        if not pending:
            say("All set. Everything is connected.")
            return
        time.sleep(20)


# --------------------------------------------------------------------------- Extras
extra_state = {}  # name -> {"message", "ok", "busy", "at"}
extras_lock = threading.Lock()


def extra_note(name, message, good=None, busy=False):
    extra_state[name] = {"message": message, "ok": good, "busy": busy, "at": time.time()}
    print(f"[yams] {extras.EXTRAS[name]['name']}: {message}", flush=True)


def wait_until(check, timeout):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def docker_exec(service, cmd, timeout=900):
    """Run a command inside one of this app's containers. Returns (exit code, output)."""
    c = (project_containers() or {}).get(service)
    if not c or c["state"] != "running":
        return None, f"{service} isn't running"
    status, body = docker("POST", f"/containers/{c['id']}/exec",
                          body={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True})
    if status != 201:
        return None, "Docker wouldn't run the command"
    exec_id = json.loads(body)["Id"]
    _, raw = docker("POST", f"/exec/{exec_id}/start", timeout=timeout, body={"Detach": False, "Tty": False})
    status, info = docker("GET", f"/exec/{exec_id}/json")
    code = json.loads(info).get("ExitCode") if status == 200 else None
    return code, demux(raw)


def wire_lidarr():
    if is_done("extra:lidarr"):
        return True
    api, h = arr_api("lidarr")
    extra_note("lidarr", "Waiting for Lidarr to start\u2026", busy=True)
    if not wait_until(lambda: ok(http("GET", f"{api}/system/status", headers=h, timeout=5)[0]), 300):
        extra_note("lidarr", "Lidarr is taking a while to start. YAMS will keep trying.", busy=True)
        return False
    extra_note("lidarr", "Connecting Lidarr to your Music folder, qBittorrent and Prowlarr\u2026", busy=True)
    if not ensure_root_folder("lidarr") or not ensure_download_client("lidarr"):
        return False
    status, naming = http("GET", f"{api}/config/naming", headers=h)
    if ok(status) and isinstance(naming, dict) and not naming.get("renameTracks"):
        naming["renameTracks"] = True
        http("PUT", f"{api}/config/naming/{naming.get('id', 1)}", naming, headers=h)
    papi, ph = prowlarr_api()
    if ok(http("GET", f"{papi}/system/status", headers=ph, timeout=5)[0]):
        if not ensure_prowlarr_app("lidarr"):
            return False
        sync_prowlarr_indexers()
    jh = jellyfin_headers()
    if jh:
        ensure_jellyfin_libraries(jh)
    mark("extra:lidarr")
    extra_note("lidarr", "Connected: music lands in Downloads \u2192 media \u2192 music and shows up in Jellyfin.", True)
    return True


def wire_seerr():
    if is_done("extra:seerr"):
        return True
    extra_note("seerr", "Waiting for Seerr to start (the first start takes a minute)\u2026", busy=True)
    if not wait_until(lambda: extras.seerr_ready(host), 600):
        extra_note("seerr", "Seerr is taking a while to start. YAMS will keep trying.", busy=True)
        return False
    if not (_arr_up("sonarr") and _arr_up("radarr") and service_up(f"{JELLYFIN}/health")):
        extra_note("seerr", "Waiting for Jellyfin, Sonarr and Radarr\u2026", busy=True)
        return False
    extra_note("seerr", "Connecting Seerr to Jellyfin, Sonarr and Radarr\u2026", busy=True)
    try:
        extras.setup_seerr()
    except extras.SeerrError as err:
        extra_note("seerr", f"{err}. YAMS will try again shortly.", busy=True)
        return False
    if is_done("extra:recyclarr"):
        extras.seerr_prefer_trash_profiles()
    mark("extra:seerr")
    extra_note("seerr", f"Ready on port {SEERR_PORT}. Sign in with your Jellyfin account; family members can use theirs.", True)
    return True


def wire_recyclarr():
    if is_done("extra:recyclarr"):
        return True
    extra_note("recyclarr", "Waiting for Sonarr and Radarr\u2026", busy=True)
    if not wait_until(lambda: _arr_up("sonarr") and _arr_up("radarr"), 600):
        return False
    if not wait_until(lambda: ((project_containers() or {}).get("recyclarr") or {}).get("state") == "running", 120):
        extra_note("recyclarr", "Recyclarr's container isn't running. Try Restart.", False)
        return False
    extra_note("recyclarr", "Syncing the TRaSH Guides quality settings (takes a minute)\u2026", busy=True)
    code, output = docker_exec("recyclarr", ["recyclarr", "sync"])
    if code != 0:
        lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
        errors = [ln for ln in lines if "[ERR]" in ln or "[WRN]" in ln]
        if any("TRaSH-Guides" in ln or "git" in ln.lower() for ln in errors):
            why = "it couldn't download the TRaSH Guides. Check your Umbrel's internet connection"
        else:
            why = (errors or lines or ["no output"])[0][:200]
        extra_note("recyclarr", f"The first sync didn't finish: {why}. It tries again daily; "
                   "switch Recyclarr off and on to retry now.", False)
        return True  # don't hammer it; the daily schedule (or a toggle) retries
    if extras.enabled("seerr") and is_done("extra:seerr"):
        extras.seerr_prefer_trash_profiles()
    mark("extra:recyclarr")
    extra_note("recyclarr", "Synced. Sonarr has a WEB-1080p profile and Radarr an HD Bluray + WEB profile; "
               "pick them when adding shows and movies. Syncs again daily.", True)
    return True


def wire_simple(name, message):
    def task():
        ok_now = wait_until(lambda: ((project_containers() or {}).get(name) or {}).get("state") == "running", 120)
        if ok_now:
            mark(f"extra:{name}")
            extra_note(name, message, True)
        else:
            extra_note(name, "Its container didn't start. Open Logs to see why.", False)
        return ok_now
    return task


EXTRA_WIRING = {
    "seerr": wire_seerr,
    "lidarr": wire_lidarr,
    "recyclarr": wire_recyclarr,
    "unpackerr": wire_simple("unpackerr", "Running. It checks for packed downloads every 2 minutes."),
    "decluttarr": wire_simple("decluttarr", "Running. It checks downloads every 10 minutes and never touches "
                                            "private-tracker torrents."),
}


def run_extra_wiring(name, attempts=20):
    for _ in range(attempts):
        if not extras.enabled(name):
            return False
        try:
            if EXTRA_WIRING[name]():
                return True
        except Exception:
            traceback.print_exc()
        time.sleep(20)
    if extras.enabled(name) and (extra_state.get(name) or {}).get("busy"):
        extra_note(name, "Couldn't finish connecting it. Try switching it off and on, or open Logs.", False)
    return False


def restart_changed_extras(changed, skip=None):
    for other in changed:
        if other != skip and extras.enabled(other):
            restart_service(other)


def toggle_extra(name, on):
    with extras_lock:
        info = extras.EXTRAS[name]
        if on:
            extra_note(name, "Switching on\u2026", busy=True)
            # Settings files first (Lidarr's flag first, since the others' files list it),
            # so the app never starts without them.
            if name == "lidarr":
                extras.set_flag(name, True)
            changed = extras.write_configs()
            extras.set_flag(name, True)
            restart_changed_extras(changed, skip=name)
            c = (project_containers() or {}).get(name)
            if c and c["state"] in ("running", "restarting"):
                restart_service(name)  # leaves its wait loop straight away
            elif c:
                docker("POST", f"/containers/{c['id']}/start", timeout=60)
        else:
            extra_note(name, "Switching off\u2026", busy=True)
            extras.set_flag(name, False)
            restart_service(name)  # back into its wait loop
            unmark(f"extra:{name}")
            if name == "lidarr":
                remove_prowlarr_app("lidarr")
            restart_changed_extras(extras.write_configs(), skip=name)
            extra_state[name] = {"message": f"{info['name']} is off.", "ok": None, "busy": False, "at": time.time()}
            print(f"[yams] {info['name']} switched off", flush=True)
            return
    run_extra_wiring(name)


def extras_status(containers):
    out = []
    for name in extras.ORDER:
        info = extras.EXTRAS[name]
        on = extras.enabled(name)
        st = dict(extra_state.get(name) or {})
        message = st.get("message")
        if on and not message:
            done = is_done(f"extra:{name}")
            message = "On." if done else "Connecting\u2026"
            st["ok"] = True if done else None
        out.append({"id": name, "name": info["name"], "what": info["what"], "on": on,
                    "link": info["link"] if on else None, "busy": bool(st.get("busy")),
                    "message": message if on or st.get("busy") else None, "ok": st.get("ok"),
                    "exists": containers is None or name in containers})
    return out


# --------------------------------------------------------------------------- WireGuard
class VpnError(ValueError):
    pass


def clean_wireguard(text):
    """Turn a provider's WireGuard file into the IPv4-only, IP-endpoint form gluetun needs."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text.strip() + "\n")
    except configparser.Error:
        raise VpnError("That doesn't look like a WireGuard file. It should contain [Interface] and [Peer] sections.")
    if not parser.has_section("Interface") or not parser.has_section("Peer"):
        raise VpnError("The file needs both an [Interface] and a [Peer] section.")
    iface, peer = parser["Interface"], parser["Peer"]

    private_key = iface.get("PrivateKey", "").strip()
    public_key = peer.get("PublicKey", "").strip()
    endpoint = peer.get("Endpoint", "").strip()
    if not private_key:
        raise VpnError("PrivateKey is missing under [Interface].")
    if not public_key:
        raise VpnError("PublicKey is missing under [Peer].")
    if not endpoint or ":" not in endpoint:
        raise VpnError("Endpoint under [Peer] should look like server.example.com:51820.")

    addresses = []
    for part in iface.get("Address", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if ipaddress.ip_interface(part).version == 4:
                addresses.append(part)
        except ValueError:
            raise VpnError(f"Couldn't read the address {part!r}.")
    if not addresses:
        raise VpnError("The file needs an IPv4 Address under [Interface].")

    ep_host, _, ep_port = endpoint.rpartition(":")
    ep_host = ep_host.strip("[]")
    if not ep_port.isdigit():
        raise VpnError("The Endpoint port should be a number.")
    try:
        ipaddress.ip_address(ep_host)
        ep_ip = ep_host
    except ValueError:
        try:
            ep_ip = socket.getaddrinfo(ep_host, None, socket.AF_INET)[0][4][0]
        except socket.gaierror:
            raise VpnError(f"Couldn't look up the VPN server {ep_host}. Check your internet connection or pick another server.")
    if ipaddress.ip_address(ep_ip).version != 4:
        raise VpnError("Please use a VPN server with an IPv4 address.")

    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {', '.join(addresses)}", "",
             "[Peer]", f"PublicKey = {public_key}"]
    psk = peer.get("PresharedKey", "").strip()
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += [f"Endpoint = {ep_ip}:{ep_port}", "AllowedIPs = 0.0.0.0/0", ""]
    return "\n".join(lines), ep_host


# --------------------------------------------------------------------------- Docker
# Only this app's containers are ever touched: every call is scoped to the
# compose project label, and restarts are limited to the list below.
SERVICES = [
    # (service, display name, what it does, open link)
    ("jellyfin", "Jellyfin", "Watch everything, on any screen", "jellyfin"),
    ("sonarr", "Sonarr", "TV shows: follows series and grabs new episodes", "/sonarr/"),
    ("radarr", "Radarr", "Movies: finds and organises films", "/radarr/"),
    ("prowlarr", "Prowlarr", "Indexers: where YAMS searches", "/prowlarr/"),
    ("bazarr", "Bazarr", "Subtitles. Pick your languages once in Settings \u2192 Languages", "/bazarr/"),
    ("qbittorrent", "qBittorrent", "Downloads, only ever through your VPN", "/qbittorrent/"),
    ("gluetun", "VPN", "gluetun: the tunnel qBittorrent downloads through", None),
    ("flaresolverr", "FlareSolverr", "Helps Prowlarr past Cloudflare checks", None),
]
EXTRA_SERVICES = [(name, x["name"], x["what"], x["link"]) for name, x in extras.EXTRAS.items()]
LOG_SERVICES = [s[0] for s in SERVICES] + list(extras.ORDER) + ["dashboard", "web", "init"]
LOG_LABELS = {s[0]: s[1] for s in SERVICES + EXTRA_SERVICES}
LOG_LABELS.update({"dashboard": "YAMS dashboard", "web": "Router (Caddy)", "init": "First-boot setup"})
RESTARTABLE = {s[0] for s in SERVICES} | set(extras.ORDER)


def services_now():
    """The core apps plus whichever Extras are switched on."""
    return SERVICES + [s for s in EXTRA_SERVICES if extras.enabled(s[0])]


class _DockerConnection(httpclient.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(DOCKER_SOCK)
        self.sock = sock


def docker(method, path, timeout=15, body=None):
    conn = _DockerConnection(timeout)
    headers = {"Host": "docker"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, httpclient.HTTPException):
        return 0, b""
    finally:
        conn.close()


def docker_available():
    return os.path.exists(DOCKER_SOCK)


def project_containers():
    """{service: {id, state, status}} for this app's containers, or None if Docker is unreachable."""
    filters = urllib.parse.quote(json.dumps({"label": [f"com.docker.compose.project={PREFIX}"]}))
    status, body = docker("GET", f"/containers/json?all=1&filters={filters}")
    if status != 200:
        return None
    result = {}
    for c in json.loads(body):
        service = (c.get("Labels") or {}).get("com.docker.compose.service")
        if service:
            result[service] = {"id": c["Id"], "state": c.get("State", ""), "status": c.get("Status", "")}
    return result


def inspect(container_id):
    status, body = docker("GET", f"/containers/{container_id}/json")
    return json.loads(body) if status == 200 else None


def demux(raw):
    """Docker multiplexes stdout/stderr into 8-byte-header frames when there's no TTY."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        out, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        raw = b"".join(out)
    return raw.decode("utf-8", errors="replace")


def container_logs(service, tail=400):
    containers = project_containers()
    if containers is None:
        return None, "YAMS can't reach Docker right now."
    c = containers.get(service)
    if not c:
        return None, "That app's container doesn't exist yet."
    status, raw = docker("GET", f"/containers/{c['id']}/logs?stdout=1&stderr=1&timestamps=1&tail={int(tail)}", timeout=20)
    if status != 200:
        return None, "Docker didn't return logs for that app."
    return demux(raw), None


def last_log_line(container_id):
    status, raw = docker("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=3", timeout=5)
    if status != 200:
        return None
    lines = [ln.strip() for ln in demux(raw).splitlines() if ln.strip()]
    return lines[-1][:220] if lines else None


busy = {}  # service -> message while an action runs
busy_lock = threading.Lock()


def set_busy(service, message):
    with busy_lock:
        if message:
            busy[service] = message
        else:
            busy.pop(service, None)


def restart_service(service):
    containers = project_containers() or {}
    c = containers.get(service)
    if not c:
        return
    set_busy(service, "Restarting\u2026")
    try:
        docker("POST", f"/containers/{c['id']}/restart?t=15", timeout=60)
        # qBittorrent lives inside gluetun's network: it must be restarted after it.
        if service == "gluetun" and "qbittorrent" in containers:
            set_busy("qbittorrent", "Restarting with the VPN\u2026")
            time.sleep(3)
            docker("POST", f"/containers/{containers['qbittorrent']['id']}/restart?t=15", timeout=60)
            set_busy("qbittorrent", None)
    finally:
        set_busy(service, None)


def restart_all():
    set_busy("_all", "Restarting all apps\u2026")
    try:
        restart_service("gluetun")  # also restarts qBittorrent
        for service in ["flaresolverr", "prowlarr", "sonarr", "radarr", "bazarr", "jellyfin"] + extras.enabled_list():
            restart_service(service)
    finally:
        set_busy("_all", None)


# Order matters: qBittorrent stops before the VPN and starts after it.
# Extras stop first (they talk to the others) and start last; a stopped extra that's
# switched off goes straight back to its wait loop when started again.
PAUSE_ORDER = ["decluttarr", "unpackerr", "recyclarr", "seerr", "lidarr",
               "qbittorrent", "sonarr", "radarr", "prowlarr", "bazarr", "jellyfin"]


def pause_apps(reason, include_vpn=False):
    """Stop the apps (for a clean backup or restore). Returns the ones that were running."""
    containers = project_containers() or {}
    order = PAUSE_ORDER + (["gluetun"] if include_vpn else [])
    stopped = []
    for service in order:
        c = containers.get(service)
        if c and c["state"] in ("running", "restarting"):
            set_busy(service, reason)
            docker("POST", f"/containers/{c['id']}/stop?t=30", timeout=90)
            stopped.append(service)
    return stopped


def resume_apps(services):
    containers = project_containers() or {}
    order = ["gluetun", "jellyfin", "prowlarr", "sonarr", "radarr", "bazarr", "qbittorrent",
             "lidarr", "seerr", "recyclarr", "unpackerr", "decluttarr"]
    for service in [s for s in order if s in services]:
        c = containers.get(service)
        if c:
            docker("POST", f"/containers/{c['id']}/start", timeout=60)
            if service == "gluetun":
                time.sleep(3)
        set_busy(service, None)


def repair_after_restore(delay=60):
    """Once the apps are back up, re-point their connections (they may name the old app's
    containers if the backup came from YAMS installed through another store)."""
    time.sleep(delay)
    for _ in range(10):
        try:
            if all(_arr_up(k) for k in arr_kinds()):
                for kind in arr_kinds():
                    ensure_download_client(kind)
                papi, ph = prowlarr_api()
                if ok(http("GET", f"{papi}/system/status", headers=ph, timeout=5)[0]):
                    ensure_flaresolverr()
                    for kind in arr_kinds():
                        ensure_prowlarr_app(kind)
                print("[yams] Checked every connection after the restore", flush=True)
                return
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def after_restore():
    # Logins may have changed with the restored settings.
    qb_state.update(sid=None, blocked_until=0)
    updater.check_cache.update(data=None, at=0)
    extra_state.clear()
    try:
        extras.write_configs()
    except Exception:
        traceback.print_exc()
    # A backup from YAMS installed through another store points at that app's container names.
    try:
        migrate_container_names(f"{os.path.dirname(STATE_DIR)}/config", PREFIX)
    except Exception:
        traceback.print_exc()
    threading.Thread(target=repair_after_restore, daemon=True).start()
    # Extras the backup had on but not fully connected pick up where they left off.
    for name in extras.enabled_list():
        if not is_done(f"extra:{name}"):
            threading.Thread(target=run_extra_wiring, args=(name,), daemon=True).start()


# --------------------------------------------------------------------------- qBittorrent API
qb_state = {"sid": None, "blocked_until": 0}
qb_lock = threading.Lock()
QBIT = f"http://{QBIT_HOST}:8080"


def qb_login():
    # qBittorrent bans an address after repeated failed logins, so back off hard on failure.
    if time.time() < qb_state["blocked_until"]:
        return False
    s = secrets()
    data = urllib.parse.urlencode({"username": s.get("username", ""), "password": s.get("password", "")}).encode()
    req = urllib.request.Request(f"{QBIT}/api/v2/auth/login", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode(errors="replace")
            cookie = resp.headers.get("Set-Cookie", "")
    except Exception:
        return False
    match = re.search(r"SID=([^;]+)", cookie)
    if body.strip().lower().startswith("ok") and match:
        qb_state["sid"] = match.group(1)
        return True
    qb_state["blocked_until"] = time.time() + 600
    return False


def qb_get(path):
    with qb_lock:
        for _ in range(2):
            if not qb_state["sid"] and not qb_login():
                return None
            req = urllib.request.Request(f"{QBIT}{path}", headers={"Cookie": f"SID={qb_state['sid']}"})
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    return json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as err:
                if err.code == 403:
                    qb_state["sid"] = None
                    continue
                return None
            except Exception:
                return None
    return None


DOWNLOADING = {"downloading", "metaDL", "forcedDL", "stalledDL", "queuedDL", "checkingDL", "allocating", "forcedMetaDL"}


def human_speed(bps):
    if not bps:
        return "0 KB/s"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.0f} {unit}" if unit in ("B/s", "KB/s") else f"{bps:.1f} {unit}"
        bps /= 1024


def human_eta(seconds):
    if not seconds or seconds >= 8640000:
        return ""
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min left"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min left"
    return f"{seconds // 86400} days left"


def downloads():
    torrents = qb_get("/api/v2/torrents/info?sort=added_on&reverse=true")
    if not isinstance(torrents, list):
        return None
    active = [t for t in torrents if t.get("state") in DOWNLOADING]
    return {
        "downloading": len(active),
        "seeding": sum(1 for t in torrents if t.get("state") in ("uploading", "stalledUP", "forcedUP", "queuedUP")),
        "speed": sum(t.get("dlspeed", 0) for t in active),
        "items": [{
            "name": t.get("name", "")[:120],
            "progress": round(float(t.get("progress", 0)), 4),
            "speed": human_speed(t.get("dlspeed", 0)),
            "eta": human_eta(t.get("eta", 0)),
            "state": t.get("state"),
        } for t in active[:8]],
    }


# --------------------------------------------------------------------------- VPN check
def check_vpn():
    """Like `yams check-vpn`: compare your home address with the one downloads use."""
    s = secrets()
    result = {"home_ip": None, "vpn_ip": None, "country": None, "qbit_ip": None,
              "qbit_in_vpn": None, "protected": False, "checked_at": int(time.time())}
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "yams-umbrel"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            result["home_ip"] = json.loads(resp.read()).get("ip")
    except Exception:
        pass
    status, ipinfo = http("GET", f"{GLUETUN}/v1/publicip/ip",
                          headers={"X-API-Key": s.get("gluetun_key", "")}, timeout=5)
    if ok(status) and isinstance(ipinfo, dict):
        result["vpn_ip"] = ipinfo.get("public_ip") or None
        result["country"] = ipinfo.get("country")
    containers = project_containers() or {}
    if "qbittorrent" in containers and "gluetun" in containers:
        info = inspect(containers["qbittorrent"]["id"]) or {}
        mode = (info.get("HostConfig") or {}).get("NetworkMode", "")
        result["qbit_in_vpn"] = mode in (f"container:{containers['gluetun']['id']}",) or mode.endswith("gluetun_1")
    transfer = qb_get("/api/v2/transfer/info")
    if isinstance(transfer, dict):
        result["qbit_ip"] = transfer.get("last_external_address_v4") or None
    result["protected"] = bool(
        result["vpn_ip"] and result["qbit_in_vpn"]
        and (not result["home_ip"] or result["home_ip"] != result["vpn_ip"])
        and (not result["qbit_ip"] or result["qbit_ip"] != result["home_ip"])
    )
    return result


# --------------------------------------------------------------------------- updates
def backup_before_update():
    try:
        backups._begin("backup")
    except backups.BackupError:
        return False
    return backups.run_backup("pre-update") is not None


def something_running():
    """A backup, restore, update or restart-all is in progress."""
    if backups.job["active"]:
        return "Wait for the backup or restore to finish."
    if updater.job["active"]:
        return "Wait for the update to finish."
    if busy.get("_all"):
        return "Wait for the restart to finish."
    return None


# --------------------------------------------------------------------------- password change
def _set_qbit_password_in_file(new):
    """Rewrite qBittorrent's saved password. qBittorrent must be stopped, or it would
    overwrite the file when it next saves its settings."""
    with open(QBIT_CONF) as fh:
        text = fh.read()
    line = f'WebUI\\Password_PBKDF2="{qbit_hash(new)}"'
    if re.search(r"^WebUI\\Password_PBKDF2=.*$", text, flags=re.M):
        text = re.sub(r"^WebUI\\Password_PBKDF2=.*$", lambda _: line, text, flags=re.M)
    elif "[Preferences]" in text:
        text = text.replace("[Preferences]", "[Preferences]\n" + line, 1)
    else:
        text += "\n[Preferences]\n" + line + "\n"
    tmp = QBIT_CONF + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    st = os.stat(QBIT_CONF)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, 0o600)
    os.replace(tmp, QBIT_CONF)


def change_qbit_password(new):
    containers = project_containers() or {}
    c = containers.get("qbittorrent")
    was_running = bool(c and c["state"] in ("running", "restarting"))
    if was_running:
        set_busy("qbittorrent", "Changing its password\u2026")
        docker("POST", f"/containers/{c['id']}/stop?t=30", timeout=90)
    try:
        _set_qbit_password_in_file(new)
    finally:
        if was_running:
            docker("POST", f"/containers/{c['id']}/start", timeout=60)
            set_busy("qbittorrent", None)
    qb_state.update(sid=None, blocked_until=0)


def change_jellyfin_password(old, new):
    s = secrets()
    status, auth = http("POST", f"{JELLYFIN}/Users/AuthenticateByName",
                        {"Username": s.get("username", ""), "Pw": old},
                        headers={"Authorization": JELLYFIN_AUTH})
    if not ok(status) or not isinstance(auth, dict):
        return False
    user_id = (auth.get("User") or {}).get("Id")
    h = {"Authorization": f'{JELLYFIN_AUTH}, Token="{auth.get("AccessToken")}"'}
    status, _ = http("POST", f"{JELLYFIN}/Users/{user_id}/Password",
                     {"CurrentPw": old, "NewPw": new}, headers=h)
    return ok(status)


def change_password():
    """New password for Jellyfin and qBittorrent, updated in Sonarr and Radarr too."""
    s = secrets()
    old, new = s.get("password", ""), friendly_password()
    done, problems = [], []
    if change_jellyfin_password(old, new):
        done.append("Jellyfin")
    else:
        problems.append("Jellyfin (it didn't accept the current password; if you changed it yourself, "
                        "change it again in Jellyfin's Dashboard \u2192 Users)")
    try:
        change_qbit_password(new)
        done.append("qBittorrent")
    except OSError as err:
        problems.append(f"qBittorrent ({err})")
    if not done:
        return False, "Nothing was changed. " + "; ".join(problems)
    s["password"] = new
    write_json(SECRETS_PATH, s)
    os.chmod(SECRETS_PATH, 0o600)
    for kind in arr_kinds():
        if ensure_download_client(kind, force=True):
            done.append(ARR[kind]["title"])
        else:
            problems.append(f"{ARR[kind]['title']} (use Quick fixes \u2192 Connect qBittorrent once it's running)")
    # Decluttarr signs in to qBittorrent with this password too.
    restart_changed_extras(extras.write_configs())
    message = f"New password set for {', '.join(done)}. Sign in again on your TV and phone apps with the new password."
    if problems:
        return False, message + " Still to sort out: " + "; ".join(problems) + "."
    return True, message


password_job = {"running": False, "ok": None, "message": None, "at": 0}


def run_change_password():
    password_job.update(running=True, ok=None, message="Changing the password\u2026", at=time.time())
    try:
        good, message = change_password()
    except Exception as err:
        traceback.print_exc()
        good, message = False, f"Changing the password hit an error: {err}"
    password_job.update(running=False, ok=good, message=message, at=time.time())
    print(f"[yams] Change password: {message}", flush=True)


# --------------------------------------------------------------------------- quick fixes
# Each fix is safe to run at any time and returns (ok, plain-English result).
def _arr_up(kind):
    api, h = arr_api(kind)
    status, _ = http("GET", f"{api}/system/status", headers=h, timeout=5)
    return ok(status)


def fix_prowlarr():
    api, h = prowlarr_api()
    status, _ = http("GET", f"{api}/system/status", headers=h, timeout=5)
    if not ok(status):
        return False, "Prowlarr isn't running. Restart it, then try again."
    down = [ARR[k]["title"] for k in arr_kinds() if not _arr_up(k)]
    if down:
        return False, f"{' and '.join(down)} isn't running yet. Restart it, then try again."
    ensure_flaresolverr()
    results = {k: ensure_prowlarr_app(k) for k in arr_kinds()}
    failed = [ARR[k]["title"] for k, good in results.items() if not good]
    if failed:
        return False, f"Prowlarr couldn't be linked to {' and '.join(failed)}. The YAMS dashboard logs say why."
    sync_prowlarr_indexers()
    status, indexers = http("GET", f"{api}/indexer", headers=h)
    n = len(indexers) if ok(status) and isinstance(indexers, list) else 0
    note = f" Syncing {n} indexer{'s' if n != 1 else ''} now." if n else " Add indexers in Prowlarr and they'll appear in each."
    return True, f"Prowlarr is linked to {arr_titles(arr_kinds())}." + note


def fix_qbittorrent():
    down = [ARR[k]["title"] for k in arr_kinds() if not _arr_up(k)]
    if down:
        return False, f"{' and '.join(down)} isn't running yet. Restart it, then try again."
    failed = [ARR[k]["title"] for k in arr_kinds() if not ensure_download_client(k)]
    if failed:
        return False, f"qBittorrent couldn't be added to {' and '.join(failed)}. The YAMS dashboard logs say why."
    qb_up = (status_cache["data"] or {}).get("answering", {}).get("qbittorrent")
    if not qb_up:
        for k in arr_kinds():
            client_status[k] = {"state": "waiting", "at": time.time()}
        return True, f"qBittorrent is set up in {arr_titles(arr_kinds())}. It switches on once qBittorrent is running behind the VPN."
    states = {k: test_download_client(k) for k in arr_kinds()}
    bad = [ARR[k]["title"] for k, st in states.items() if st != "connected"]
    if bad:
        return False, f"{' and '.join(bad)} can't reach qBittorrent. Check qBittorrent's logs, or use Reconnect the VPN."
    return True, f"{arr_titles(arr_kinds())} are connected to qBittorrent and passed the connection test."


def fix_folders():
    notes, problems = [], []
    containers = project_containers() or {}
    init = containers.get("init")
    if init:
        docker("POST", f"/containers/{init['id']}/start", timeout=30)
        for _ in range(30):
            info = inspect(init["id"]) or {}
            if not (info.get("State") or {}).get("Running"):
                break
            time.sleep(1)
        notes.append("recreated the media folders")
    for k in arr_kinds():
        if not _arr_up(k):
            problems.append(f"{ARR[k]['title']} isn't running")
        elif ensure_root_folder(k):
            notes.append(f"{ARR[k]['title']} saves to {ARR[k]['root']}")
        else:
            problems.append(f"{ARR[k]['title']} wouldn't accept its library folder")
    h = jellyfin_headers()
    if not h:
        problems.append("YAMS can't log in to Jellyfin (was its password changed?)")
    else:
        added = ensure_jellyfin_libraries(h)
        if added is None:
            problems.append("Jellyfin didn't answer")
        else:
            notes.append(f"added Jellyfin's {' and '.join(added)} librar{'ies' if len(added) > 1 else 'y'}" if added
                         else "Jellyfin's libraries are in place")
    message = "Done: " + "; ".join(notes) + "." if notes else ""
    if problems:
        return False, (message + " " if message else "") + "Still to sort out: " + "; ".join(problems) + "."
    return True, message


def fix_rescan():
    h = jellyfin_headers()
    if not h:
        return False, "YAMS can't log in to Jellyfin (was its password changed?). Rescan from Jellyfin's Dashboard instead."
    status, _ = http("POST", f"{JELLYFIN}/Library/Refresh", headers=h, timeout=20)
    if not ok(status):
        return False, "Jellyfin didn't start the scan. Is it running?"
    return True, "Jellyfin is rescanning your libraries. New items appear within a few minutes."


def fix_search():
    started = []
    searches = [("sonarr", "MissingEpisodeSearch", "missing episodes"),
                ("radarr", "MissingMoviesSearch", "missing movies")]
    if extras.enabled("lidarr"):
        searches.append(("lidarr", "MissingAlbumSearch", "missing albums"))
    for kind, command, label in searches:
        api, h = arr_api(kind)
        status, _ = http("POST", f"{api}/command", {"name": command}, headers=h)
        if ok(status):
            started.append(label)
    if not started:
        return False, "Neither Sonarr nor Radarr answered. Are they running?"
    return True, f"Searching for {' and '.join(started)} now. Check the Activity tab in each app."


def fix_vpn():
    if not (os.path.exists(WG_PATH) and os.path.getsize(WG_PATH) > 0):
        return False, "No VPN is set up yet. Paste your WireGuard file in step 1 first."
    restart_service("gluetun")
    return True, "Reconnecting the VPN. qBittorrent restarts once the tunnel is back, usually within a minute."


def fix_all():
    results = [fix_prowlarr(), fix_qbittorrent(), fix_folders()]
    good = all(r[0] for r in results)
    return good, " ".join(r[1] for r in results)


FIXES = {
    "prowlarr": ("Connect Prowlarr to your apps", fix_prowlarr),
    "qbittorrent": ("Connect qBittorrent to your apps", fix_qbittorrent),
    "folders": ("Fix library folders", fix_folders),
    "rescan": ("Rescan Jellyfin", fix_rescan),
    "search": ("Search for missing episodes and movies", fix_search),
    "vpn": ("Reconnect the VPN", fix_vpn),
    "all": ("Fix everything", fix_all),
}
fix_state = {}  # id -> {"running", "ok", "message", "at"}


def run_fix(fix_id):
    fix_state[fix_id] = {"running": True, "ok": None, "message": "Working\u2026", "at": time.time()}
    try:
        good, message = FIXES[fix_id][1]()
    except Exception as err:
        traceback.print_exc()
        good, message = False, f"That fix hit an error: {err}"
    fix_state[fix_id] = {"running": False, "ok": good, "message": message, "at": time.time()}
    print(f"[yams] Fix '{FIXES[fix_id][0]}': {message}", flush=True)


# --------------------------------------------------------------------------- status
status_cache = {"data": None}


def service_up(url, headers=None):
    status, _ = http("GET", url, headers=headers, timeout=4)
    return status != 0 and status < 500


def count(url, headers):
    status, items = http("GET", url, headers=headers, timeout=8)
    return len(items) if ok(status) and isinstance(items, list) else None


def describe(service, container, answering):
    """Turn Docker's state plus an HTTP check into one plain-English status."""
    if busy.get(service):
        return "busy", busy[service]
    if not container:
        return "missing", "Not created"
    state, status = container["state"], container["status"]
    if state == "running":
        if answering:
            return "up", "Running"
        if service == "qbittorrent":
            return "wait", "Waiting for the VPN"
        return "wait", "Starting"
    if state == "restarting":
        match = re.search(r"\((\d+)\)", status)
        return "down", "Keeps restarting" if match and int(match.group(1)) > 0 else "Restarting"
    if state == "exited":
        return "down", "Stopped"
    return "wait", state.capitalize() or "Starting"


def gather_status():
    s = secrets()
    vpn_meta = read_json(f"{STATE_DIR}/vpn.json", {})
    vpn = {"configured": os.path.exists(WG_PATH) and os.path.getsize(WG_PATH) > 0,
           "connected": False, "ip": None, "country": None,
           "server": vpn_meta.get("server"), "saved_at": vpn_meta.get("saved_at")}
    if vpn["configured"]:
        status, ipinfo = http("GET", f"{GLUETUN}/v1/publicip/ip",
                              headers={"X-API-Key": s.get("gluetun_key", "")}, timeout=4)
        if ok(status) and isinstance(ipinfo, dict) and ipinfo.get("public_ip"):
            vpn.update(connected=True, ip=ipinfo.get("public_ip"), country=ipinfo.get("country"))

    sh = {"X-Api-Key": s.get("sonarr_key", "")}
    rh = {"X-Api-Key": s.get("radarr_key", "")}
    ph = {"X-Api-Key": s.get("prowlarr_key", "")}
    answering = {
        "jellyfin": service_up(f"{JELLYFIN}/health"),
        "sonarr": service_up(f"{SONARR}/ping"),
        "radarr": service_up(f"{RADARR}/ping"),
        "prowlarr": service_up(f"{PROWLARR}/ping"),
        "bazarr": service_up(f"{BAZARR}/"),
        "qbittorrent": vpn["connected"] and service_up(f"{QBIT}/api/v2/app/version"),
        "gluetun": vpn["connected"],
        "flaresolverr": service_up(f"http://{host('flaresolverr')}:8191/health"),
    }
    containers = project_containers() if docker_available() else None
    on = extras.enabled_list()
    answering["seerr"] = "seerr" in on and service_up(f"{SEERR}/api/v1/status")
    answering["lidarr"] = "lidarr" in on and service_up(f"{LIDARR}/ping")
    for name in ("recyclarr", "unpackerr", "decluttarr"):  # no web page: running is the best sign
        answering[name] = name in on and ((containers or {}).get(name) or {}).get("state") == "running"
    apps = []
    for service, name, what, link in services_now():
        c = (containers or {}).get(service)
        if containers is None:
            tone, label = ("up", "Running") if answering[service] else ("wait", "Starting")
        else:
            tone, label = describe(service, c, answering[service])
        if service == "gluetun" and not vpn["configured"]:
            tone, label = "wait", "Waiting for your VPN file"
        detail = None
        if tone in ("wait", "down") and c and not busy.get(service):
            detail = last_log_line(c["id"])
        note = None
        if service in ARR and tone == "up":
            note = {
                "missing": "qBittorrent isn't set up here yet. YAMS is adding it now.",
                "waiting": "qBittorrent is set up and switches on once the VPN is connected.",
                "unreachable": "Can't reach qBittorrent right now. YAMS checks again every 10 minutes.",
            }.get(client_status[service]["state"])
        apps.append({"service": service, "name": name, "what": what, "link": link,
                     "tone": tone, "label": label, "detail": detail, "note": note,
                     "can_restart": service in RESTARTABLE and bool(c)})

    indexers = count(f"{PROWLARR}/api/v1/indexer", ph) if answering["prowlarr"] else None
    series = count(f"{SONARR}/api/v3/series", sh) if answering["sonarr"] else None
    movies = count(f"{RADARR}/api/v3/movie", rh) if answering["radarr"] else None
    dl = downloads() if answering["qbittorrent"] else None
    setup = setup_state()
    return {
        "version": YAMS_VERSION,
        "docker": containers is not None,
        "vpn": vpn,
        "apps": apps,
        "answering": answering,
        "downloads": dl,
        "indexers": indexers,
        "series": series,
        "movies": movies,
        "busy_all": busy.get("_all") or (backups.job["step"] if backups.job["active"] else None)
        or (updater.job["step"] if updater.job["active"] else None),
        "backup": backups.status(),
        "updates": updater.status(),
        "fixes": [{"id": k, "label": v[0], **fix_state.get(k, {})} for k, v in FIXES.items()],
        "extras": extras_status(containers),
        "seerr_port": SEERR_PORT,
        "setup": {"message": setup.get("message"), "done": setup.get("done", {})},
        "login": {"username": s.get("username"), "password": s.get("password")},
        "password_job": password_job if time.time() - password_job["at"] < 900 or password_job["running"] else None,
        "jellyfin_port": JELLYFIN_PORT,
        "log_services": [{"service": x, "name": LOG_LABELS[x]} for x in LOG_SERVICES
                         if x not in extras.EXTRAS or x in on],
        "updated": int(time.time()),
    }


def status_loop():
    qbit_was_up = False
    while True:
        try:
            status_cache["data"] = gather_status()
            qbit_up = bool(status_cache["data"]["answering"].get("qbittorrent"))
            if qbit_up and not qbit_was_up:
                # The VPN just came up: switch on download clients saved while it was down.
                threading.Thread(target=enable_download_clients, daemon=True).start()
            qbit_was_up = qbit_up
        except Exception:
            traceback.print_exc()
        time.sleep(4)


# --------------------------------------------------------------------------- widgets
def widget_stats():
    data = status_cache["data"] or gather_status()
    vpn, dl = data["vpn"], data.get("downloads") or {}
    return {
        "type": "four-stats",
        "link": "",
        "items": [
            {"title": "VPN", "text": "On" if vpn["connected"] else "Off",
             "subtext": vpn.get("country") or ("downloads paused" if not vpn["connected"] else "")},
            {"title": "Downloading", "text": str(dl.get("downloading", 0)),
             "subtext": human_speed(dl.get("speed", 0)) if dl else "paused"},
            {"title": "Shows", "text": str(data.get("series") or 0), "subtext": "followed"},
            {"title": "Movies", "text": str(data.get("movies") or 0), "subtext": "in library"},
        ],
    }


def widget_downloads():
    data = status_cache["data"] or gather_status()
    dl = data.get("downloads")
    if not data["vpn"]["connected"]:
        items = [{"text": "Downloads paused", "subtext": "Connect a VPN in YAMS"}]
    elif not dl or not dl["items"]:
        items = [{"text": "Nothing downloading", "subtext": "Add a show or movie in YAMS"}]
    else:
        items = [{"text": f"{int(t['progress'] * 100)}% \u2022 {t['name']}",
                  "subtext": " \u2022 ".join(x for x in ("\u2193 " + t["speed"], t["eta"]) if x)}
                 for t in dl["items"][:5]]
    return {"type": "list", "link": "", "items": items}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ValueError("too big")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/api/status":
            return self.send(200, status_cache["data"] or gather_status())
        if path == "/api/logs":
            service = (query.get("service") or [""])[0]
            if service not in LOG_SERVICES:
                return self.send(400, {"error": "Unknown app."})
            try:
                tail = max(50, min(2000, int((query.get("tail") or ["400"])[0])))
            except ValueError:
                tail = 400
            text, error = container_logs(service, tail)
            if error:
                return self.send(503, {"error": error})
            return self.send(200, {"service": service, "logs": text})
        if path == "/api/updates":
            force = (query.get("refresh") or ["0"])[0] == "1"
            return self.send(200, updater.check(force))
        if path == "/api/backups/download":
            try:
                file_path = backups.safe_name((query.get("name") or [""])[0])
            except backups.BackupError as err:
                return self.send(404, {"error": str(err)})
            size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(file_path)}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(file_path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return None
        if path == "/widgets/stats":
            return self.send(200, widget_stats())
        if path == "/widgets/downloads":
            return self.send(200, widget_downloads())
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # Browsers won't let another website add a custom header without CORS approval,
        # which this server never gives, so this blocks cross-site requests.
        if self.headers.get("X-Yams-Request") != "1" or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return self.send(403, {"error": "That request didn't come from the YAMS dashboard."})
        if path == "/api/backups/upload":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                name = backups.receive_upload(self.rfile, length, urllib.parse.unquote(self.headers.get("X-Filename", "")))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            except ValueError:
                return self.send(400, {"error": "Couldn't read that upload."})
            return self.send(200, {"ok": True, "name": name})
        try:
            body = self.read_body()
        except ValueError:
            return self.send(400, {"error": "Couldn't read that request."})

        if path in ("/api/restart", "/api/restart-all", "/api/backups/create", "/api/backups/restore",
                    "/api/updates/apply"):
            reason = something_running()
            if reason:
                return self.send(409, {"error": reason})
        if path == "/api/password":
            if password_job["running"]:
                return self.send(409, {"error": "Already changing the password."})
            reason = something_running()
            if reason:
                return self.send(409, {"error": reason})
            threading.Thread(target=run_change_password, daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/fix":
            fix_id = body.get("fix")
            if fix_id not in FIXES:
                return self.send(400, {"error": "Unknown fix."})
            if any(f.get("running") for f in fix_state.values()):
                return self.send(409, {"error": "Another fix is still running."})
            reason = something_running()
            if reason:
                return self.send(409, {"error": reason})
            threading.Thread(target=run_fix, args=(fix_id,), daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/extras":
            name, on = body.get("extra"), body.get("on")
            if name not in extras.EXTRAS or not isinstance(on, bool):
                return self.send(400, {"error": "Unknown extra."})
            reason = something_running()
            if reason:
                return self.send(409, {"error": reason})
            if (extra_state.get(name) or {}).get("busy") and extras.enabled(name) == on:
                return self.send(409, {"error": "Already working on that one."})
            threading.Thread(target=toggle_extra, args=(name, on), daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/updates/apply":
            services = body.get("services")
            if services is not None and (not isinstance(services, list) or not set(services) <= set(updater.APPS)):
                return self.send(400, {"error": "Unknown app."})
            try:
                updater.start_update(services)
            except updater.UpdateError as err:
                return self.send(409, {"error": str(err)})
            return self.send(202, {"ok": True})
        if path == "/api/updates/settings":
            try:
                return self.send(200, updater.update_settings(body.get("auto")))
            except updater.UpdateError as err:
                return self.send(400, {"error": str(err)})
        if path == "/api/restart":
            service = body.get("service")
            if service not in RESTARTABLE:
                return self.send(400, {"error": "That app can't be restarted from here."})
            if busy.get(service) or busy.get("_all"):
                return self.send(409, {"error": "Already restarting."})
            threading.Thread(target=restart_service, args=(service,), daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/restart-all":
            if busy.get("_all"):
                return self.send(409, {"error": "Already restarting."})
            threading.Thread(target=restart_all, daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/backups/create":
            try:
                backups.start_backup("manual")
            except backups.BackupError as err:
                return self.send(409, {"error": str(err)})
            return self.send(202, {"ok": True})
        if path == "/api/backups/restore":
            try:
                backups.start_restore(body.get("name"))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            return self.send(202, {"ok": True})
        if path == "/api/backups/delete":
            try:
                backups.delete_backup(body.get("name"))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            return self.send(200, {"ok": True})
        if path == "/api/backups/settings":
            try:
                return self.send(200, backups.update_settings(body.get("schedule"), body.get("keep")))
            except (backups.BackupError, ValueError, TypeError) as err:
                return self.send(400, {"error": str(err)})
        if path == "/api/vpn-check":
            return self.send(200, check_vpn())
        if path == "/api/vpn":
            try:
                conf, server_name = clean_wireguard(body.get("config", ""))
            except VpnError as err:
                return self.send(400, {"error": str(err)})
            tmp = WG_PATH + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(conf)
            os.chmod(tmp, 0o600)
            os.replace(tmp, WG_PATH)
            write_json(f"{STATE_DIR}/vpn.json", {"server": server_name, "saved_at": int(time.time())})
            return self.send(200, {"ok": True, "server": server_name})
        return self.send(404, {"error": "Not found"})


class WidgetHandler(Handler):
    """What the shared Umbrel network can reach: the two read-only widgets, nothing else."""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/widgets/stats":
            return self.send(200, widget_stats())
        if path == "/widgets/downloads":
            return self.send(200, widget_downloads())
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        return self.send(404, {"error": "Not found"})


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


def serve_api(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    server = UnixHTTPServer(path, Handler)
    os.chmod(path, 0o660)
    return server


def main():
    backups.hooks.update(stop=pause_apps, start=resume_apps, after_restore=after_restore, version=YAMS_VERSION)
    backups.ensure_backup_dir()
    try:
        backups.resume_after_crash()
    except Exception:
        traceback.print_exc()
    threading.Thread(target=backups.scheduler, daemon=True).start()
    updater.hooks.update(docker=docker, containers=project_containers, inspect=inspect,
                         backup=backup_before_update, set_busy=set_busy)
    threading.Thread(target=updater.scheduler, daemon=True).start()
    if not os.path.exists(SETUP_PATH):
        write_json(SETUP_PATH, {"done": {}, "message": "Getting everything ready\u2026"})
    threading.Thread(target=configurator, daemon=True).start()
    try:
        extras.write_configs()
    except Exception:
        traceback.print_exc()
    for name in extras.enabled_list():
        threading.Thread(target=run_extra_wiring, args=(name,), daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=connection_watch, daemon=True).start()
    api = serve_api(API_SOCK)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    print(f"[yams] dashboard controls on {API_SOCK}; widgets on :8000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), WidgetHandler).serve_forever()


if __name__ == "__main__":
    main()
