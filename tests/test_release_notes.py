import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import release_notes  # noqa: E402


def test_app_updates_alone_bump_the_patch_and_list_each_app_once():
    tag, body = release_notes.notes("v1.1.0", ["YAMS: update to 1.5.4", "Miniflux: update to 2.3.9", "YAMS: update to 1.5.3"])
    assert tag == "v1.1.1"
    assert "Miniflux 2.3.9 · YAMS 1.5.4" in body and "1.5.3" not in body and "### Changes" not in body


def test_any_other_change_bumps_the_minor_and_is_listed():
    tag, body = release_notes.notes("v1.1.3", ["Merge pull request #12", "YAMS 1.6.0: new thing", "Gatus: update to 5.39.0"])
    assert tag == "v1.2.0"
    assert "- YAMS 1.6.0: new thing" in body and "Merge pull" not in body and "Gatus 5.39.0" in body
    assert "compare/v1.1.3...v1.2.0" in body
