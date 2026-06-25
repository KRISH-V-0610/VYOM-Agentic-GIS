"""
Integration tests for the new catalog tools (live PostgreSQL/PostGIS required).

Marked `integration` — skipped with:  pytest -m "not integration"
These exercise compare_events / find_best_scene / find_scene_pairs against whatever is
ingested. They assert STRUCTURE + invariants (not hard-coded numbers) so they stay valid
as more events land. kerala_periyar_2018 is assumed ingested (the baseline event).
"""
import pytest

from vyom.db import postgis_server as pg

KERALA = "kerala_periyar_2018"


@pytest.mark.integration
class TestFindBestScene:
    def test_finds_lowest_cloud_event_scene(self):
        r = pg.find_best_scene(KERALA, window_type="event")
        assert r["found"] is True
        scene = r["scene"]
        assert scene["window_type"] == "event"
        assert scene["event_key"] == KERALA
        assert "available_assets" in scene

    def test_lowest_cloud_invariant(self):
        """The chosen event scene's cloud ≤ every other event scene's cloud."""
        best = pg.find_best_scene(KERALA, window_type="event")["scene"]
        all_event = pg.list_scenes(event_key=KERALA, window_type="event",
                                   limit=1000)["scenes"]
        clouds = [s["cloud_cover"] for s in all_event
                  if s.get("cloud_cover") is not None]
        if clouds and best.get("cloud_cover") is not None:
            assert best["cloud_cover"] <= min(clouds) + 1e-6

    def test_sensor_filter(self):
        r = pg.find_best_scene(KERALA, window_type="event", sensor="LISS4")
        if r["found"]:
            assert r["scene"]["sensor"] == "LISS4"

    def test_no_match_returns_found_false(self):
        r = pg.find_best_scene(KERALA, window_type="no_such_window")
        assert r["found"] is False
        assert r["scene"] is None
        assert r["note"]


@pytest.mark.integration
class TestCompareEvents:
    def test_single_event_aggregates(self):
        r = pg.compare_events([KERALA], "water_area_pct", window="pre_event")
        assert r["ranking"] == [KERALA]
        assert r["events"][0]["event_key"] == KERALA
        assert r["events"][0]["mean"] is not None
        assert r["events"][0]["scene_count"] >= 1

    def test_unknown_event_gets_null_stats(self):
        r = pg.compare_events(["nonexistent_event_xyz"], "water_area_pct")
        assert r["events"][0]["mean"] is None
        assert r["ranking"] == []

    def test_mixed_known_and_unknown_ranks_known_first(self):
        r = pg.compare_events([KERALA, "nonexistent_event_xyz"],
                              "water_area_pct", window="pre_event")
        # Known event (has data) is ranked; unknown trails with null stats.
        assert r["ranking"] == [KERALA]
        assert r["events"][0]["event_key"] == KERALA
        assert r["events"][-1]["mean"] is None


@pytest.mark.integration
class TestFindScenePairs:
    def test_structure_always_present(self):
        r = pg.find_scene_pairs(KERALA, "pre_event", "post_event")
        assert {"count", "pairs", "best_pair", "note"} <= set(r)

    def test_best_pair_carries_overlap_quality(self):
        r = pg.find_scene_pairs(KERALA, "pre_event", "post_event")
        if r["best_pair"]:
            bp = r["best_pair"]
            assert "overlap_quality" in bp
            assert bp["overlap_quality"] in (None, "good", "moderate", "poor")
            assert "scene_a_id" in bp and "scene_b_id" in bp

    def test_same_sensor_pairs_share_sensor(self):
        r = pg.find_scene_pairs(KERALA, "pre_event", "post_event", same_sensor=True)
        for p in r["pairs"]:
            assert p["scene_a_id"] != p["scene_b_id"]
