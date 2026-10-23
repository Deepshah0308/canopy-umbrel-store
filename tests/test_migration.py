"""Moving YAMS between stores: settings restored from the old 'yams-media' app."""
import os
import sys

import server
from conftest import KEYS, ROOT
from fakes import FakeApps

sys.path.insert(0, os.path.join(ROOT, "canopy-yams", "hooks", "yams", "setup"))
import seed  # noqa: E402

OLD_BAZARR = """sonarr:
  ip: yams-media_sonarr_1
  port: 8989
radarr:
  ip: yams-media_radarr_1
other: keep_this_1 and canopy-yams_radarr_1
"""


def test_old_container_names_are_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "UID", os.getuid())
    monkeypatch.setattr(seed, "GID", os.getgid())
    path = tmp_path / "bazarr" / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(OLD_BAZARR)
    assert seed.migrate_container_names(str(tmp_path), "canopy-yams") == ["bazarr/config/config.yaml"]
    text = path.read_text()
    assert "yams-media" not in text
    assert "ip: canopy-yams_sonarr_1" in text and "ip: canopy-yams_radarr_1" in text
    assert "keep_this_1" in text
    assert seed.migrate_container_names(str(tmp_path), "canopy-yams") == []  # nothing left to do


def test_missing_config_is_fine(tmp_path):
    assert seed.migrate_container_names(str(tmp_path), "canopy-yams") == []


def test_flaresolverr_pointing_at_old_app_is_repaired(secrets_file, monkeypatch):
    fake = FakeApps(KEYS).start()
    try:
        monkeypatch.setattr(server, "PROWLARR", fake.url("prowlarr"))
        assert server.ensure_flaresolverr()  # already right: left alone
        fake.proxies[0]["fields"][0]["value"] = "http://yams-media_flaresolverr_1:8191/"
        assert server.ensure_flaresolverr()
        host = {f["name"]: f["value"] for f in fake.proxies[0]["fields"]}["host"]
        assert host == server.FLARESOLVERR
    finally:
        fake.stop()
