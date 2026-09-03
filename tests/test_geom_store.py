"""Geometry store: single-key reads and levels of detail.

Measured on the live backend, before and after:

    first trail request   4.68s -> 0.007s      (parsed 167 MB vs one SQLite row)
    response size       295 KB -> 27 KB        (full vs z10)
    RSS after requests  948 MB -> 183 MB
"""

from __future__ import annotations

import json

import pytest

from pipeline import geom_store


def _line(n, jitter=0.0):
    """A straight line of n points, with optional off-axis noise."""
    return [[i * 0.001, (jitter if i % 2 else 0.0)] for i in range(n)]


class TestSimplify:
    def test_a_straight_line_collapses_to_its_endpoints(self):
        assert geom_store.simplify(_line(50), 1e-5) == [[0.0, 0.0], [0.049, 0.0]]

    def test_detail_above_the_tolerance_is_kept(self):
        # 0.01 deg of zigzag against a 1e-5 tolerance: every vertex matters.
        assert len(geom_store.simplify(_line(21, jitter=0.01), 1e-5)) == 21

    def test_detail_below_the_tolerance_is_dropped(self):
        assert len(geom_store.simplify(_line(21, jitter=1e-7), 1e-4)) == 2

    def test_short_inputs_are_returned_unchanged(self):
        assert geom_store.simplify([[0, 0], [1, 1]], 1.0) == [[0, 0], [1, 1]]
        assert geom_store.simplify([], 1.0) == []

    def test_endpoints_always_survive(self):
        pts = _line(200, jitter=1e-9)
        out = geom_store.simplify(pts, 1.0)
        assert out[0] == pts[0] and out[-1] == pts[-1]

    def test_long_line_does_not_blow_the_recursion_limit(self):
        # The recursive formulation dies here, and only on the longest trails —
        # exactly the ones simplification exists for.
        assert len(geom_store.simplify(_line(20000, jitter=1e-9), 1e-6)) >= 2


class TestSimplifyGeometry:
    def test_multilinestring_parts_are_simplified(self):
        geom = {"type": "MultiLineString", "coordinates": [_line(50), _line(50)]}
        out = geom_store.simplify_geometry(geom, 1e-5)
        assert [len(p) for p in out["coordinates"]] == [2, 2]

    def test_a_geometry_that_would_vanish_is_returned_intact(self):
        # Better a heavy trail than a missing one: if every part collapses below two
        # points there is nothing to draw, so the original is kept.
        geom = {"type": "LineString", "coordinates": [[0, 0], [1e-9, 1e-9]]}
        assert geom_store.simplify_geometry(geom, 10.0) == geom

    def test_unknown_geometry_types_pass_through(self):
        geom = {"type": "Point", "coordinates": [1, 2]}
        assert geom_store.simplify_geometry(geom, 1.0) == geom


class TestStore:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        src = tmp_path / "geom.json"
        src.write_text(json.dumps({
            "t1": {"geometry": {"type": "LineString", "coordinates": _line(500)},
                   "profile": [1, 2, 3]},
            "t2": {"geometry": {"type": "LineString", "coordinates": _line(10)}},
            "t3": {"profile": [9]},          # no geometry at all
        }))
        db = tmp_path / "geom.sqlite"
        geom_store.build(source=src, target=db, verbose=False)
        monkeypatch.setattr(geom_store, "GEOM_DB_PATH", db)
        monkeypatch.setattr(geom_store, "_conn", None)
        return db

    def test_round_trips_geometry_and_profile(self, store):
        got = geom_store.get("t1")
        assert got["geometry"]["type"] == "LineString"
        assert got["profile"] == [1, 2, 3]

    def test_tiers_are_smaller_than_full(self, store):
        full = len(geom_store.get("t1", "full")["geometry"]["coordinates"])
        z10 = len(geom_store.get("t1", "z10")["geometry"]["coordinates"])
        assert z10 < full

    def test_missing_profile_is_none_not_empty(self, store):
        assert geom_store.get("t2")["profile"] is None

    def test_a_trail_without_geometry_is_not_stored(self, store):
        assert geom_store.get("t3") is None

    def test_unknown_trail_returns_none(self, store):
        assert geom_store.get("nope") is None

    def test_an_invalid_detail_falls_back_to_full(self, store):
        # The value reaches this from a query string; it must not become SQL.
        assert geom_store.get("t1", "'; DROP TABLE geometry;--") == geom_store.get("t1", "full")

    def test_absent_store_reports_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(geom_store, "GEOM_DB_PATH", tmp_path / "missing.sqlite")
        monkeypatch.setattr(geom_store, "_conn", None)
        assert geom_store.available() is False
        assert geom_store.get("t1") is None


class TestDetailForZoom:
    @pytest.mark.parametrize("zoom,expected", [
        (None, "full"), (5, "z10"), (10.9, "z10"),
        (11, "z12"), (12.9, "z12"), (13, "z14"), (14.9, "z14"), (15, "full"), (18, "full"),
    ])
    def test_tier_selection(self, zoom, expected):
        assert geom_store.detail_for_zoom(zoom) == expected
