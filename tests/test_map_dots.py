"""The dots endpoint: the browse view that must never load geometry.

Measured on the real index, dense Sierra viewport (320 trails):

    full geometry   7.90 MB   206,311 coordinates
    dots            0.07 MB   111x smaller

The byte count is not the main point. `load_geometry()` parses a 167 MB file into
~945 MB of Python objects, which is most of the backend's ~1 GB RSS and 2.3s of
startup. If browsing stops calling it, that cost disappears from the hot path — so
"does not touch geometry" is a correctness property of this endpoint, not a
nice-to-have, and it is what these tests pin down.
"""

from __future__ import annotations

import pytest

import planner.discover as discover


@pytest.fixture
def index(monkeypatch):
    payload = {
        "trails": [
            {"id": "a", "name": "Alpha", "length_miles": 4.0, "center": [-120.1, 38.1],
             "marker": {"point": [-120.15, 38.15], "kind": "trailhead"},
             "wilderness": {"name": "John Muir Wilderness"}},
            {"id": "b", "name": "Beta", "length_miles": 9.0, "center": [-120.2, 38.2],
             "wilderness": {}},
            {"id": "c", "name": "Gamma", "length_miles": 2.0, "center": None,
             "wilderness": {}},
        ]
    }
    payload["trails"] = [discover._augment(t) for t in payload["trails"]]
    payload["by_id"] = {t["id"]: t for t in payload["trails"]}
    monkeypatch.setattr(discover, "_index", payload)
    return payload


@pytest.fixture
def geometry_is_forbidden(monkeypatch):
    """Make any geometry load an error, so a regression fails loudly."""
    def _boom(*args, **kwargs):
        raise AssertionError("map_dots must not load the geometry sidecar")

    monkeypatch.setattr(discover, "load_geometry", _boom)


class TestMapDots:
    def test_returns_a_point_per_trail(self, index, geometry_is_forbidden):
        fc = discover.map_dots()
        assert fc["type"] == "FeatureCollection"
        assert {f["id"] for f in fc["features"]} == {"a", "b"}
        assert all(f["geometry"]["type"] == "Point" for f in fc["features"])

    def test_never_loads_geometry(self, index, geometry_is_forbidden):
        # The fixture raises if load_geometry is called at all. This is the whole
        # reason the endpoint exists, so it gets its own test.
        discover.map_dots()

    def test_a_trail_without_a_center_is_skipped_not_faked(self, index, geometry_is_forbidden):
        # Trail "c" has center None. Emitting [0, 0] would put it off West Africa.
        fc = discover.map_dots()
        assert "c" not in {f["id"] for f in fc["features"]}

    def test_honors_the_same_filters_as_search(self, index, geometry_is_forbidden):
        fc = discover.map_dots(wilderness_area="John Muir Wilderness")
        assert [f["id"] for f in fc["features"]] == ["a"]

    def test_reports_truncation_only_when_the_cap_hid_something(self, index, geometry_is_forbidden):
        fc = discover.map_dots(limit=1)
        assert fc["returned"] == 1
        assert fc["total"] == 3          # agrees with /search
        assert fc["truncated"] is True

    def test_an_unmappable_trail_is_not_reported_as_truncation(self, index, geometry_is_forbidden):
        # Trail "c" has no centroid. It is not "hidden by the cap", and saying so
        # would tell the user to zoom in to reveal something that cannot be drawn.
        fc = discover.map_dots(limit=50)
        assert fc["truncated"] is False
        assert fc["unmappable"] == 1
        assert fc["returned"] == 2

    def test_properties_stay_minimal(self, index, geometry_is_forbidden):
        # 10,694 copies of a field is 10,694 copies of it; anything beyond what the
        # map draws or labels belongs in the per-trail lookup.
        props = discover.map_dots()["features"][0]["properties"]
        assert set(props) == {
            "id", "name", "length_miles", "gain_ft", "difficulty", "marker_kind",
        }

    def test_prefers_the_on_trail_marker_over_the_bbox_center(self, index, geometry_is_forbidden):
        # `center` is a bounding-box midpoint and can be miles off the trail;
        # `marker.point` is on it. Measured: 13.2% of centers are >0.25 mi off.
        fc = discover.map_dots()
        alpha = next(f for f in fc["features"] if f["id"] == "a")
        assert alpha["geometry"]["coordinates"] == [-120.15, 38.15]
        assert alpha["properties"]["marker_kind"] == "trailhead"
