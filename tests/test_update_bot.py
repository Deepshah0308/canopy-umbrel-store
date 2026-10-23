"""The update bot's rules for which new versions are acceptable (no network needed)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import registry  # noqa: E402
import update_apps as bot  # noqa: E402

TAGS = ["1.46.0", "1.47.0", "1.47.1", "1.48.0-rc1", "1.48.0-beta", "latest", "2.0.0", "v1.49.0", "1.47", "nightly"]


def test_only_same_shaped_releases_count():
    assert bot.candidates("1.47.0", TAGS, False) == ["1.47.1", "2.0.0"]
    assert bot.candidates("1.47.0", TAGS, True) == ["1.47.1"], "databases stay on their major"
    assert bot.candidates("v1.47.0", ["v1.47.2", "1.48.0", "v1.48.0-rc.1"], False) == ["v1.47.2"]


def test_suffixes_must_match():
    tags = ["3.12.15-alpine", "3.12.15", "3.13.1-alpine", "3.12.16-slim", "4.0.0-alpine"]
    assert bot.candidates("3.12.14-alpine", tags, True, hold_minor=True) == ["3.12.15-alpine"]


def test_date_tags_never_mix_with_normal_versions():
    assert bot.candidates("0.11.0", ["0.11.1", "2021.12.13", "5.9.5"], False) == ["0.11.1", "5.9.5"]
    assert "2021.12.13" not in bot.candidates("0.11.0", ["2021.12.13"], False)
    assert bot.candidates("2026.10.3", ["2026.10.4", "1.3.15"], False) == ["2026.10.4"]


@pytest.mark.parametrize("old,new,kind", [
    ("1.47.0", "1.47.1", "patch"), ("1.47.0", "1.48.0", "minor"), ("1.47.0", "2.0.0", "major"),
    ("0.11.0", "0.12.0", "major"), ("0.11.0", "0.11.1", "patch"), ("v26.3", "v26.4", "minor"),
    ("5.2.3", "5.2.3", "rebuild"),
])
def test_classify(old, new, kind):
    assert bot.classify(old, new) == kind


def test_ref_parsing():
    assert registry.parse_ref("postgres:17.11-alpine@sha256:abc") == ("registry-1.docker.io", "library/postgres", "17.11-alpine", "sha256:abc")
    assert registry.parse_ref("ghcr.io/jeffvli/feishin:1.17.0")[:3] == ("ghcr.io", "jeffvli/feishin", "1.17.0")
    assert registry.parse_ref("localhost:5000/x/y")[:3] == ("localhost:5000", "x/y", "latest")


def test_manifest_fields_are_rewritten_in_place():
    text = 'id: x\nversion: "1.0.0"\nreleaseNotes: >-\n  old notes\n  more\nsubmitter: me\n'
    text = bot.set_field(text, "version", "1.1.0")
    text = bot.set_field(text, "releaseNotes", "new notes")
    assert text == 'id: x\nversion: "1.1.0"\nreleaseNotes: >-\n  new notes\nsubmitter: me\n'
    assert bot.bump_patch("1.4.0") == "1.4.1"
