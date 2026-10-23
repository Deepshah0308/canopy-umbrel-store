"""Extras: on/off flags, generated settings files, and connecting them safely."""
import json
import os
import stat
import tomllib

import pytest
import yaml

import extras
import server
from conftest import KEYS
from fakes import FakeApps

TRICKY = 'p"a\\ss: #word'  # quotes, a backslash, a colon and a hash


@pytest.fixture
def flags(tmp_path, monkeypatch):
    monkeypatch.setattr(extras, "FLAG_DIR", str(tmp_path / "flags"))
    monkeypatch.setattr(extras, "CFG_DIR", str(tmp_path / "config"))
    return tmp_path


def secrets_with(**extra):
    s = {"username": "admin", "password": "not-a-real-password", "sonarr_key": "sk", "radarr_key": "rk",
         "lidarr_key": "lk"}
    s.update(extra)
    return s


def test_flags_switch_extras_on_and_off(flags):
    assert extras.enabled_list() == []
    extras.set_flag("seerr", True)
    extras.set_flag("lidarr", True)
    assert extras.enabled("seerr") and extras.enabled_list() == ["seerr", "lidarr"]
    extras.set_flag("seerr", False)
    extras.set_flag("seerr", False)  # switching off twice is fine
    assert extras.enabled_list() == ["lidarr"]
    with pytest.raises(KeyError):
        extras.set_flag("../../etc", True)


@pytest.mark.parametrize("lidarr_on", [False, True])
def test_generated_files_parse_and_follow_lidarr(lidarr_on):
    s = secrets_with(password=TRICKY)
    rec = yaml.safe_load(extras.recyclarr_yaml(s, server.host))
    assert rec["sonarr"]["yams-sonarr"]["api_key"] == "sk"
    assert rec["radarr"]["yams-radarr"]["base_url"].endswith("/radarr")

    unp = tomllib.loads(extras.unpackerr_toml(s, server.host, lidarr_on))
    assert [x["api_key"] for x in unp["sonarr"]] == ["sk"]
    assert ("lidarr" in unp) == lidarr_on
    assert all(x["paths"] == ["/data/torrents"] for x in unp["sonarr"] + unp["radarr"])

    dec = yaml.safe_load(extras.decluttarr_yaml(s, server.host, lidarr_on))
    assert dec["download_clients"]["qbittorrent"][0]["password"] == TRICKY
    assert dec["download_clients"]["qbittorrent"][0]["name"] == server.CLIENT_NAME
    assert ("lidarr" in dec["instances"]) == lidarr_on
    assert dec["general"]["private_tracker_handling"] == "skip"
    assert "remove_orphans" not in dec["jobs"], "would delete torrents people added by hand"
    assert "remove_done_seeding" not in dec["jobs"]


def test_write_configs_reports_changes_and_keeps_files_private(flags, monkeypatch):
    s = secrets_with()
    monkeypatch.setitem(extras.hooks, "secrets", lambda: s)
    monkeypatch.setitem(extras.hooks, "host", server.host)
    assert sorted(extras.write_configs()) == ["decluttarr", "recyclarr", "unpackerr"]
    assert extras.write_configs() == []
    path = os.path.join(extras.CFG_DIR, "decluttarr", "config.yaml")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    s["password"] = "a-new-password"
    assert extras.write_configs() == ["decluttarr"]
    extras.set_flag("lidarr", True)
    assert sorted(extras.write_configs()) == ["decluttarr", "unpackerr"]


def test_seerr_prefers_trash_profile_then_hd():
    test = {"profiles": [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}],
            "rootFolders": [{"id": 1, "path": "/data/media/tv"}]}
    body = extras._arr_body("sonarr", {"hostname": "h", "port": 1, "apiKey": "k", "useSsl": False, "baseUrl": "/sonarr"}, test)
    assert body["activeProfileName"] == "HD-1080p" and body["activeDirectory"] == "/data/media/tv"
    assert body["isDefault"] and not body["is4k"] and body["enableSeasonFolders"]
    test["profiles"].append({"id": 9, "name": "WEB-1080p"})
    body = extras._arr_body("sonarr", {"hostname": "h", "port": 1, "apiKey": "k", "useSsl": False, "baseUrl": "/sonarr"}, test)
    assert body["activeProfileId"] == 9
    radarr = extras._arr_body("radarr", {}, {"profiles": [{"id": 2, "name": "SD"}], "rootFolders": []})
    assert radarr["activeDirectory"] == "/data/media/movies" and radarr["minimumAvailability"] == "released"


@pytest.fixture
def apps(secrets_file, monkeypatch):
    fake = FakeApps(KEYS).start()
    monkeypatch.setitem(server.ARR["sonarr"], "base", fake.url("sonarr"))
    monkeypatch.setitem(server.ARR["radarr"], "base", fake.url("radarr"))
    monkeypatch.setattr(server, "PROWLARR", fake.url("prowlarr"))
    yield fake
    fake.stop()


def test_download_client_is_saved_switched_off_until_qbittorrent_runs(apps):
    apps.qbit_ok = False  # VPN not set up yet, so qBittorrent isn't running
    assert server.ensure_download_client("sonarr")
    client = apps.clients["sonarr"][0]
    assert client["enable"] is False and client["name"] == server.CLIENT_NAME
    assert server.test_download_client("sonarr") == "waiting"
    apps.qbit_ok = True  # the VPN connects
    server.enable_download_clients()
    assert apps.clients["sonarr"][0]["enable"] is True and len(apps.clients["sonarr"]) == 1
    assert apps.clients["radarr"][0]["enable"] is True


def test_lidarr_only_counts_while_switched_on(apps, flags):
    assert server.arr_kinds() == ["sonarr", "radarr"]
    extras.set_flag("lidarr", True)
    assert server.arr_kinds() == ["sonarr", "radarr", "lidarr"]
    assert server.arr_titles(server.arr_kinds()) == "Sonarr, Radarr and Lidarr"


def test_switching_lidarr_off_removes_its_prowlarr_link(apps, flags, monkeypatch):
    monkeypatch.setitem(server.ARR["lidarr"], "base", apps.url("lidarr"))
    apps.apps.append({"id": 7, "implementation": "Lidarr", "name": "Lidarr", "fields": []})
    apps.apps.append({"id": 8, "implementation": "Sonarr", "name": "Sonarr", "fields": []})
    assert server.remove_prowlarr_app("lidarr")
    assert [a["implementation"] for a in apps.apps] == ["Sonarr"]


def test_extras_api_rejects_bad_requests(secrets_file):
    class Req:
        pass
    sent = {}

    class H(server.Handler):
        def __init__(self):
            pass

        def send(self, code, body, ctype="application/json"):
            sent.update(code=code, body=body)

    for body in ({"extra": "nope", "on": True}, {"extra": "seerr", "on": "yes"}, {"extra": "seerr"}):
        h = H()
        h.path = "/api/extras"
        raw = json.dumps(body).encode()
        h.headers = {"X-Yams-Request": "1", "Content-Length": str(len(raw))}
        import io
        h.rfile = io.BytesIO(raw)
        h.do_POST()
        assert sent["code"] == 400, body
