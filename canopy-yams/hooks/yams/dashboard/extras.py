"""
YAMS Extras: optional apps you switch on from the dashboard.

Every extra is already part of the YAMS app, but its container waits in a tiny loop
(extras/gate.sh) until a flag file exists in /yams/state/extras. Switching one on
writes whatever config it needs, creates the flag and connects it to the rest of
YAMS; switching it off removes the flag and restarts the container back into the wait.

Extras that need settings files get them from here, generated from the same API keys
YAMS already manages, so nobody has to copy keys around:
  Recyclarr  -> config/recyclarr/recyclarr.yml   (TRaSH Guides quality settings)
  Unpackerr  -> config/unpackerr/unpackerr.conf  (unpacks .rar downloads)
  Decluttarr -> config/decluttarr/config.yaml    (clears stuck downloads)
Seerr is set up through its own API on first start (see setup_seerr).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

DATA_DIR = os.environ.get("DATA_DIR", "/yams")
FLAG_DIR = os.environ.get("EXTRAS_DIR", os.path.join(os.environ.get("STATE_DIR", f"{DATA_DIR}/state"), "extras"))
CFG_DIR = os.environ.get("CONFIG_DIR", f"{DATA_DIR}/config")
UID = GID = 1000

EXTRAS = {
    "seerr": {"name": "Seerr", "link": "seerr",
              "what": "Requests: a Netflix-style page where you and your family ask for shows and movies, "
                      "signing in with your Jellyfin accounts"},
    "lidarr": {"name": "Lidarr", "link": "/lidarr/",
               "what": "Music: follows artists and grabs new albums into a Music library in Jellyfin"},
    "recyclarr": {"name": "Recyclarr", "link": None,
                  "what": "Keeps Sonarr's and Radarr's quality settings in line with the TRaSH Guides, once a day"},
    "unpackerr": {"name": "Unpackerr", "link": None,
                  "what": "Unpacks downloads that arrive as .rar files so Sonarr, Radarr and Lidarr can import them"},
    "decluttarr": {"name": "Decluttarr", "link": None,
                   "what": "Clears stuck, stalled and failed downloads and searches for a better copy"},
}
ORDER = list(EXTRAS)

# Filled in by server.py: host(service) -> container name, secrets() -> dict
hooks = {}

# TRaSH Guides quality profiles Recyclarr creates (from Recyclarr's own templates)
TRASH_SONARR_PROFILE = ("72dae194fc92bf828f32cde7744e51a1", "WEB-1080p")
TRASH_RADARR_PROFILE = ("d1d67249d3890e49bc12e275d989a7e9", "HD Bluray + WEB")


# --------------------------------------------------------------------------- flags
def flag_path(name):
    if name not in EXTRAS:
        raise KeyError(name)
    return os.path.join(FLAG_DIR, name)


def enabled(name):
    return name in EXTRAS and os.path.exists(flag_path(name))


def enabled_list():
    return [name for name in ORDER if enabled(name)]


def set_flag(name, on):
    path = flag_path(name)
    os.makedirs(FLAG_DIR, exist_ok=True)
    if on:
        with open(path, "w") as fh:
            fh.write(f"switched on {int(time.time())}\n")
        os.chmod(path, 0o644)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- config files
def _urls(host):
    return {
        "sonarr": f"http://{host('sonarr')}:8989/sonarr",
        "radarr": f"http://{host('radarr')}:7878/radarr",
        "lidarr": f"http://{host('lidarr')}:8686/lidarr",
        "qbittorrent": f"http://{host('gluetun')}:8080",
    }


def _q(value):
    """A value safe to put inside double quotes in YAML or TOML."""
    return json.dumps(str(value))


def recyclarr_yaml(s, host):
    u = _urls(host)
    return f"""# Written by YAMS. Turn Recyclarr off and on in the YAMS dashboard to rewrite it.
# Syncs the TRaSH Guides quality sizes and one recommended profile per app, daily.
sonarr:
  yams-sonarr:
    base_url: {_q(u['sonarr'])}
    api_key: {_q(s.get('sonarr_key', ''))}
    quality_definition:
      type: series
    quality_profiles:
      - trash_id: {TRASH_SONARR_PROFILE[0]}  # {TRASH_SONARR_PROFILE[1]}
        reset_unmatched_scores:
          enabled: true

radarr:
  yams-radarr:
    base_url: {_q(u['radarr'])}
    api_key: {_q(s.get('radarr_key', ''))}
    quality_definition:
      type: movie
    quality_profiles:
      - trash_id: {TRASH_RADARR_PROFILE[0]}  # {TRASH_RADARR_PROFILE[1]}
        reset_unmatched_scores:
          enabled: true
"""


def unpackerr_toml(s, host, lidarr_on):
    u = _urls(host)
    blocks = []
    apps = [("sonarr", "sonarr_key"), ("radarr", "radarr_key")] + ([("lidarr", "lidarr_key")] if lidarr_on else [])
    for kind, key in apps:
        blocks.append(f"""[[{kind}]]
  url = {_q(u[kind])}
  api_key = {_q(s.get(key, ''))}
  paths = ["/data/torrents"]
  protocols = "torrent"
  timeout = "10s"
  delete_orig = false
  delete_delay = "5m"
""")
    return """# Written by YAMS. It's rewritten when Extras change.
debug = false
quiet = false
interval = "2m"
start_delay = "1m"
retry_delay = "5m"
max_retries = 3
parallel = 1
file_mode = "0644"
dir_mode = "0755"

""" + "\n".join(blocks)


def decluttarr_yaml(s, host, lidarr_on):
    u = _urls(host)
    instances = [("sonarr", "sonarr_key"), ("radarr", "radarr_key")] + ([("lidarr", "lidarr_key")] if lidarr_on else [])
    inst = "\n".join(f"""  {kind}:
    - base_url: {_q(u[kind])}
      api_key: {_q(s.get(key, ''))}""" for kind, key in instances)
    return f"""# Written by YAMS. It's rewritten when Extras change or the password changes.
general:
  log_level: INFO
  timer: 10
  # Never remove torrents from private trackers: they count against your ratio.
  private_tracker_handling: skip
  public_tracker_handling: remove

jobs:
  remove_bad_files:
  remove_failed_downloads:
  remove_failed_imports:
  remove_metadata_missing:
  remove_missing_files:
  # remove_orphans stays off: it would delete torrents you added to qBittorrent yourself.
  remove_slow:
  remove_stalled:
  remove_unmonitored:

instances:
{inst}

download_clients:
  qbittorrent:
    - name: "qBittorrent (VPN)"
      base_url: {_q(u['qbittorrent'])}
      username: {_q(s.get('username', ''))}
      password: {_q(s.get('password', ''))}
"""


def config_files(s, host, lidarr_on):
    return {
        "recyclarr": (os.path.join(CFG_DIR, "recyclarr", "recyclarr.yml"), recyclarr_yaml(s, host)),
        "unpackerr": (os.path.join(CFG_DIR, "unpackerr", "unpackerr.conf"), unpackerr_toml(s, host, lidarr_on)),
        "decluttarr": (os.path.join(CFG_DIR, "decluttarr", "config.yaml"), decluttarr_yaml(s, host, lidarr_on)),
    }


def _write_private(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(content)
    os.chmod(tmp, 0o600)  # they hold API keys and the qBittorrent password
    try:
        os.chown(tmp, UID, GID)
    except PermissionError:
        pass
    os.replace(tmp, path)


def write_configs():
    """(Re)write the settings files. Returns the extras whose file changed, so the
    caller can restart the ones that are running."""
    s, host = hooks["secrets"](), hooks["host"]
    changed = []
    for name, (path, content) in config_files(s, host, enabled("lidarr")).items():
        try:
            with open(path) as fh:
                if fh.read() == content:
                    continue
        except FileNotFoundError:
            pass
        _write_private(path, content)
        changed.append(name)
    return changed


# --------------------------------------------------------------------------- Seerr
class SeerrError(RuntimeError):
    pass


class _Seerr:
    """Tiny client for Seerr's API, keeping the login cookie between calls."""

    def __init__(self, base):
        self.base = base.rstrip("/") + "/api/v1"
        self.cookie = None

    def call(self, method, path, body=None, timeout=60):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for header in resp.headers.get_all("Set-Cookie") or []:
                    match = re.match(r"(connect\.sid=[^;]+)", header)
                    if match:
                        self.cookie = match.group(1)
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as err:
            raw, status = err.read(), err.code
        except Exception:
            return 0, None
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")


def seerr_url(host):
    return f"http://{host('seerr')}:5055"


def seerr_ready(host):
    status, _ = _Seerr(seerr_url(host)).call("GET", "/status", timeout=5)
    return status == 200


def _pick_profile(profiles, preferred):
    by_name = {p.get("name"): p for p in profiles or []}
    for name in preferred:
        if name in by_name:
            return by_name[name]
    return (profiles or [None])[0]


def _arr_body(kind, conn, test):
    preferred = ([TRASH_SONARR_PROFILE[1]] if kind == "sonarr" else [TRASH_RADARR_PROFILE[1]]) + ["HD-1080p", "HD - 720p/1080p"]
    profile = _pick_profile(test.get("profiles"), preferred)
    if not profile:
        raise SeerrError(f"{kind.capitalize()} has no quality profiles yet")
    folders = test.get("rootFolders") or []
    root = folders[0]["path"] if folders else ("/data/media/tv" if kind == "sonarr" else "/data/media/movies")
    body = dict(conn, name=kind.capitalize(), activeProfileId=profile["id"], activeProfileName=profile["name"],
                activeDirectory=root, is4k=False, isDefault=True, tags=[], externalUrl="",
                syncEnabled=True, preventSearch=False, tagRequests=False)
    if kind == "sonarr":
        body.update(seriesType="standard", animeSeriesType="anime", activeAnimeProfileId=profile["id"],
                    activeAnimeProfileName=profile["name"], activeAnimeDirectory=root, animeTags=[],
                    enableSeasonFolders=True)
    else:
        body.update(minimumAvailability="released")
    return body


def setup_seerr(log=print):
    """Connect Seerr to Jellyfin (signing in as the YAMS admin), turn on the Movies and
    Shows libraries, add Sonarr and Radarr, and finish Seerr's setup wizard. Safe to run
    again: every step checks what's already there."""
    s, host = hooks["secrets"](), hooks["host"]
    api = _Seerr(seerr_url(host))
    status, public = api.call("GET", "/settings/public")
    if status != 200 or not isinstance(public, dict):
        raise SeerrError("Seerr isn't answering yet")

    login = {"username": s.get("username", ""), "password": s.get("password", ""), "email": "admin@yams.local",
             "serverType": 2}  # 2 = Jellyfin
    status, _ = api.call("POST", "/auth/jellyfin", dict(login, hostname=host("jellyfin"), port=8096,
                                                        useSsl=False, urlBase=""))
    if status != 200:
        # Already pointed at Jellyfin (an earlier run got this far): sign in without a hostname.
        status, detail = api.call("POST", "/auth/jellyfin", login)
        if status != 200:
            raise SeerrError(f"Seerr couldn't sign in to Jellyfin ({status}): {str(detail)[:200]}")

    # Libraries: Seerr v3 syncs with POST and enables one at a time; older builds used query flags.
    status, libraries = api.call("POST", "/settings/jellyfin/library/sync", {})
    if status == 200 and isinstance(libraries, list):
        for lib in libraries:
            if not lib.get("enabled") and lib.get("type") in ("movie", "show"):
                api.call("PUT", f"/settings/jellyfin/library/{lib['id']}", {"enabled": True})
    else:
        status, libraries = api.call("GET", "/settings/jellyfin/library?sync=true")
        if status == 200 and isinstance(libraries, list) and libraries:
            ids = ",".join(lib["id"] for lib in libraries if lib.get("type") in ("movie", "show"))
            api.call("GET", f"/settings/jellyfin/library?enable={ids}")

    for kind, port, key, base in (("sonarr", 8989, "sonarr_key", "/sonarr"), ("radarr", 7878, "radarr_key", "/radarr")):
        status, existing = api.call("GET", f"/settings/{kind}")
        if status == 200 and existing:
            continue
        conn = {"hostname": host(kind), "port": port, "apiKey": s.get(key, ""), "useSsl": False, "baseUrl": base}
        status, test = api.call("POST", f"/settings/{kind}/test", conn)
        if status != 200 or not isinstance(test, dict):
            raise SeerrError(f"Seerr couldn't reach {kind.capitalize()} yet")
        status, detail = api.call("POST", f"/settings/{kind}", _arr_body(kind, conn, test))
        if status not in (200, 201):
            raise SeerrError(f"Seerr didn't accept {kind.capitalize()} ({status}): {str(detail)[:200]}")
        log(f"[yams] Connected Seerr to {kind.capitalize()}")

    status, public = api.call("GET", "/settings/public")
    if not (isinstance(public, dict) and public.get("initialized")):
        status, _ = api.call("POST", "/settings/initialize", {})
        if status != 200:
            raise SeerrError("Seerr didn't finish its setup")
    return True


def seerr_prefer_trash_profiles():
    """After Recyclarr has created the TRaSH profiles, make them what Seerr requests with."""
    s, host = hooks["secrets"](), hooks["host"]
    api = _Seerr(seerr_url(host))
    status, _ = api.call("POST", "/auth/jellyfin", {"username": s.get("username", ""),
                                                     "password": s.get("password", ""), "serverType": 2})
    if status != 200:
        return False
    changed = False
    for kind, (_, wanted) in (("sonarr", TRASH_SONARR_PROFILE), ("radarr", TRASH_RADARR_PROFILE)):
        status, servers = api.call("GET", f"/settings/{kind}")
        if status != 200 or not servers:
            continue
        server = servers[0]
        if server.get("activeProfileName") == wanted:
            continue
        conn = {k: server.get(k) for k in ("hostname", "port", "apiKey", "useSsl", "baseUrl")}
        status, test = api.call("POST", f"/settings/{kind}/test", conn)
        profile = next((p for p in (test or {}).get("profiles", []) if p.get("name") == wanted), None) \
            if status == 200 else None
        if not profile:
            continue
        server.update(activeProfileId=profile["id"], activeProfileName=profile["name"])
        if kind == "sonarr":
            server.update(activeAnimeProfileId=profile["id"], activeAnimeProfileName=profile["name"])
        status, _ = api.call("PUT", f"/settings/{kind}/{server['id']}", server)
        changed = changed or status == 200
    return changed
