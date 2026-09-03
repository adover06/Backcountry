"""Tests for the wilderness, USFS recreation and OSM access enrichment stages."""

from __future__ import annotations

import json
import os
import time

import pytest

from pipeline.enrich_osm import merge_pois
from pipeline.osm_access import _classify
from pipeline.usfs_rec import _clean, site_records
from pipeline.wilderness import (
    MIN_INSIDE_FRACTION,
    WildernessIndex,
    _point_in_ring,
    enrich_trail,
    link_permits,
)


def _square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _area(name="Test Wilderness", polygons=None, wid="W1"):
    return {
        "type": "Feature",
        "properties": {"wildernessname": name, "wildernessid": wid, "gis_acres": 100.0},
        "geometry": {"type": "Polygon", "coordinates": polygons or [_square(0, 0, 1, 1)]},
    }


class TestPointInPolygon:
    def test_inside_and_outside(self):
        ring = _square(0, 0, 1, 1)
        assert _point_in_ring(0.5, 0.5, ring)
        assert not _point_in_ring(1.5, 0.5, ring)
        assert not _point_in_ring(-0.5, 0.5, ring)

    def test_ray_only_counts_crossings_to_the_east(self):
        # A point east of the polygon must be outside even though the ray's
        # latitude does cross both edges — the crossings are behind it.
        assert not _point_in_ring(2.0, 0.5, _square(0, 0, 1, 1))

    def test_concave_polygon(self):
        # A C shape: the notch at x>0.5, 0.4<y<0.6 is outside the polygon.
        c_shape = [
            [0, 0], [1, 0], [1, 0.4], [0.5, 0.4],
            [0.5, 0.6], [1, 0.6], [1, 1], [0, 1], [0, 0],
        ]
        assert _point_in_ring(0.25, 0.5, c_shape)
        assert not _point_in_ring(0.75, 0.5, c_shape)

    def test_hole_is_not_inside(self):
        index = WildernessIndex([_area(polygons=[_square(0, 0, 10, 10), _square(4, 4, 6, 6)])])
        assert index.contains(1, 1) is not None
        assert index.contains(5, 5) is None


class TestWildernessEnrichment:
    def setup_method(self):
        self.index = WildernessIndex([_area()])

    def _line(self, coords):
        return {"type": "LineString", "coordinates": coords}

    def test_fully_inside(self):
        trail = enrich_trail({}, self.index, self._line([[0.2, 0.2], [0.5, 0.5], [0.8, 0.8]]))
        assert trail["wilderness"]["name"] == "Test Wilderness"
        assert trail["wilderness"]["fully_inside"] is True
        assert trail["wilderness"]["inside_fraction"] == 1.0

    def test_entirely_outside_records_an_empty_answer_not_none(self):
        # {} means "looked, found nothing"; None means "never looked". The
        # distinction is the pipeline's governing rule.
        trail = enrich_trail({}, self.index, self._line([[5, 5], [6, 6]]))
        assert trail["wilderness"] == {}

    def test_missing_geometry_stays_none(self):
        assert enrich_trail({}, self.index, None)["wilderness"] is None

    def test_clipping_a_corner_does_not_inherit_the_area(self):
        # 1 of 12 points inside is below MIN_INSIDE_FRACTION, so the trail is not
        # "in" the wilderness and must not pick up its permit implications.
        coords = [[0.99, 0.99]] + [[2 + i * 0.1, 2] for i in range(11)]
        trail = enrich_trail({}, self.index, self._line(coords))
        assert trail["wilderness"] == {}

    def test_majority_inside_is_recorded_as_partial(self):
        coords = [[0.5, 0.5]] * 6 + [[5, 5]] * 4
        trail = enrich_trail({}, self.index, self._line(coords))
        assert trail["wilderness"]["inside_fraction"] >= MIN_INSIDE_FRACTION
        assert trail["wilderness"]["fully_inside"] is False


class TestUsfsCleaning:
    def test_no_data_sentinels_become_none(self):
        # The feed spells absence as text; passing it through would render the
        # string "No Data" as a facility's water supply.
        for sentinel in ("No Data", "none", "N/A", "", "  ", "Unknown"):
            assert _clean(sentinel) is None

    def test_real_values_survive(self):
        assert _clean("  Vault toilet(s) ") == "Vault toilet(s)"

    def _feature(self, **props):
        base = {
            "site_type": "TRAILHEAD",
            "public_site_name": "Bear Gulch Trailhead",
            "objectid": 1,
        }
        base.update(props)
        return {
            "type": "Feature",
            "properties": base,
            "geometry": {"type": "Point", "coordinates": [-120.0, 38.0]},
        }

    def test_maps_site_type_to_kind(self):
        recs = site_records([self._feature(), self._feature(site_type="CAMPGROUND")])
        assert [r["kind"] for r in recs] == ["trailhead", "campground"]

    def test_unlisted_site_types_are_dropped(self):
        # A whitelist: TARGET RANGE must not enter the index as an unknown kind.
        assert site_records([self._feature(site_type="TARGET RANGE")]) == []

    def test_details_omitted_entirely_when_nothing_survives(self):
        rec = site_records([self._feature(water_availability="No Data")])[0]
        assert "details" not in rec

    def test_details_kept_when_present(self):
        rec = site_records([self._feature(water_availability="Hand pump", fee_charged="Y")])[0]
        assert rec["details"] == {"water": "Hand pump", "fee": "Y"}

    def test_public_name_preferred_over_operational_name(self):
        rec = site_records([self._feature(site_name="GOLDLEDGE")])[0]
        assert rec["name"] == "Bear Gulch Trailhead"


class TestWildernessPermitLinking:
    PERMITS = [
        {"id": "233261", "name": "Desolation Wilderness Permit"},
        {"id": "233262", "name": "Inyo National Forest - Wilderness Permits"},
        {"id": "1", "name": "Humboldt-Toiyabe National Forest - Hoover Wilderness Permits"},
    ]

    def test_exact_area_is_linked(self):
        trails = [{"wilderness": {"name": "Desolation Wilderness"}}]
        link_permits(trails, self.PERMITS, verbose=False)
        assert trails[0]["wilderness"]["permit"]["id"] == "233261"

    def test_forest_level_desk_is_not_claimed_as_governing(self):
        # "Inyo National Forest - Wilderness Permits" covers several wildernesses;
        # asserting it governs John Muir specifically would overstate the match.
        # The 35 mi proximity join in permits.py still carries it.
        trails = [{"wilderness": {"name": "John Muir Wilderness"}}]
        link_permits(trails, self.PERMITS, verbose=False)
        assert "permit" not in trails[0]["wilderness"]

    def test_trail_outside_wilderness_is_untouched(self):
        trails = [{"wilderness": {}}, {"wilderness": None}]
        link_permits(trails, self.PERMITS, verbose=False)
        assert trails[0]["wilderness"] == {}
        assert trails[1]["wilderness"] is None


class TestOsmAccessClassification:
    def test_trailhead_and_water(self):
        assert _classify({"highway": "trailhead"}) == "trailhead"
        assert _classify({"amenity": "drinking_water"}) == "water"

    def test_parking_requires_a_trailhead_name(self):
        # Guards against a mirror matching looser than the query asked, which would
        # pull in every supermarket car park within the join radius.
        assert _classify({"amenity": "parking", "name": "Mist Falls Trailhead"}) == "trailhead"
        assert _classify({"amenity": "parking", "name": "Safeway"}) is None
        assert _classify({"amenity": "parking"}) is None

    def test_spring_is_not_promoted_to_drinking_water(self):
        # A spring is untreated and may be seasonal; calling it water would tell a
        # hiker there is water where there may be none.
        assert _classify({"natural": "spring"}) is None

    def test_backcountry_camping_is_distinct_from_a_campground(self):
        # A walk-in primitive site and a drive-in campground are different trips.
        assert _classify({"tourism": "camp_site"}) == "campground"
        assert _classify({"tourism": "camp_site", "backcountry": "yes"}) == "backcountry_camp"

    def test_shelters(self):
        assert _classify({"tourism": "wilderness_hut"}) == "shelter"
        assert _classify({"amenity": "shelter", "shelter_type": "lean_to"}) == "shelter"


class TestWildernessSearchFilter:
    """The `wilderness` filter previously tested `mgmt_area`.

    `mgmt_area` is an administrative management-area name, not a land-status
    designation, so `?wilderness=true` returned any trail with an admin area
    attached — including trails nowhere near designated wilderness.
    """

    def _index(self):
        return {
            "trails": [
                {
                    "id": "a",
                    "name": "Inside JMW",
                    "length_miles": 4.0,
                    "mgmt_area": None,
                    "wilderness": {"name": "John Muir Wilderness"},
                },
                {
                    "id": "b",
                    "name": "Has admin area only",
                    "length_miles": 3.0,
                    "mgmt_area": "Some Ranger District",
                    "wilderness": {},
                },
                {
                    "id": "c",
                    "name": "Inside Desolation",
                    "length_miles": 2.0,
                    "mgmt_area": "Eldorado",
                    "wilderness": {"name": "Desolation Wilderness"},
                },
            ]
        }

    def setup_method(self):
        import planner.discover as discover

        self.discover = discover
        self._saved = discover._index
        payload = self._index()
        payload["trails"] = [discover._augment(t) for t in payload["trails"]]
        payload["by_id"] = {t["id"]: t for t in payload["trails"]}
        discover._index = payload

    def teardown_method(self):
        self.discover._index = self._saved

    def test_admin_area_alone_is_not_wilderness(self):
        got = {t["id"] for t in self.discover.search(wilderness_only=True)["results"]}
        assert got == {"a", "c"}

    def test_filter_by_named_area(self):
        result = self.discover.search(wilderness_area="Desolation Wilderness")
        assert [t["id"] for t in result["results"]] == ["c"]

    def test_area_facet_counts_each_designation(self):
        facets = self.discover.search()["facets"]
        assert facets["wilderness_area"] == {
            "Desolation Wilderness": 1,
            "John Muir Wilderness": 1,
        }

    def test_wilderness_survives_the_detail_projection(self):
        # _PUBLIC_FIELDS is a whitelist; a field missing from it never reaches the
        # client no matter what the pipeline computed. Search results use the lean
        # list projection instead — no card shows wilderness — so this is asserted
        # against the detail projection, which is what /trail/{id} returns.
        trail = self.discover.get_trail("a")
        assert self.discover._public(trail)["wilderness"]["name"] == "John Muir Wilderness"

    def test_the_list_projection_stays_lean(self):
        # 203 KB of every 290 KB search response was nearby/permits/access, none of
        # which a result card renders. Regressing this makes every pan heavy again.
        result = self.discover.search()
        assert not ({"nearby", "permits", "access"} & set(result["results"][0]))


class TestPoiMerge:
    """GNIS is primary; OSM supplements it. Overlap must not double-count."""

    WHITNEY = {"kind": "peak", "name": "Mount Whitney", "lat": 36.5785, "lng": -118.2923}

    def test_same_named_feature_is_dropped(self):
        # 2 m apart. An earlier implementation rounded coordinates to a grid, and
        # these two landed either side of a cell boundary and both survived.
        osm = [{**self.WHITNEY, "lat": 36.57852, "lng": -118.29231}]
        assert len(merge_pois([self.WHITNEY], osm)) == 1

    def test_unnamed_duplicate_of_a_named_feature_is_dropped(self):
        osm = [{"kind": "peak", "name": "", "lat": 36.5786, "lng": -118.2924}]
        assert len(merge_pois([self.WHITNEY], osm)) == 1

    def test_a_differently_named_nearby_peak_is_kept(self):
        # Summits cluster; the names are the evidence they are distinct features.
        osm = [{"kind": "peak", "name": "Mount Muir", "lat": 36.5640, "lng": -118.2920}]
        assert len(merge_pois([self.WHITNEY], osm)) == 2

    def test_kinds_do_not_dedupe_against_each_other(self):
        osm = [{"kind": "viewpoint", "name": "Mount Whitney", **{
            "lat": 36.5785, "lng": -118.2923}}]
        assert len(merge_pois([self.WHITNEY], osm)) == 2

    def test_categories_gnis_lacks_survive(self):
        # viewpoint / cave / glacier are why the supplement is worth merging at all.
        osm = [
            {"kind": "viewpoint", "name": "Trail Crest", "lat": 36.56, "lng": -118.30},
            {"kind": "glacier", "name": "Palisade Glacier", "lat": 37.09, "lng": -118.51},
        ]
        merged = merge_pois([self.WHITNEY], osm)
        assert {p["kind"] for p in merged} == {"peak", "viewpoint", "glacier"}

    def test_primary_is_never_dropped(self):
        merged = merge_pois([self.WHITNEY], [])
        assert merged == [self.WHITNEY]


class TestAccessCacheFreshness:
    """The OSM sweep runs on its own schedule, so version alone cannot gate the cache.

    A cache written at the current version *before* the sweep produced its file
    would otherwise keep answering without OSM data indefinitely.
    """

    def _setup_paths(self, tmp_path, monkeypatch):
        import pipeline.access as access

        cache = tmp_path / "access_points.json"
        osm = tmp_path / "osm_access_points.json"
        monkeypatch.setattr(access, "ACCESS_PATH", cache)
        monkeypatch.setattr(access, "OSM_ACCESS_PATH", osm)
        return access, cache, osm

    def _write_cache(self, access, cache, records):
        cache.write_text(
            json.dumps({"version": access.ACCESS_CACHE_VERSION, "records": records})
        )

    def test_current_version_is_reused(self, tmp_path, monkeypatch):
        access, cache, _ = self._setup_paths(tmp_path, monkeypatch)
        self._write_cache(access, cache, [{"kind": "trailhead"}])
        assert access.load_access_points(verbose=False) == [{"kind": "trailhead"}]

    def test_a_newer_osm_sweep_invalidates_the_cache(self, tmp_path, monkeypatch):
        access, cache, osm = self._setup_paths(tmp_path, monkeypatch)
        self._write_cache(access, cache, [{"kind": "trailhead"}])
        osm.write_text("[]")
        os.utime(osm, (time.time() + 60, time.time() + 60))

        called = {}

        def _fail(*a, **k):
            called["refetched"] = True
            raise RuntimeError("network disabled in tests")

        monkeypatch.setattr(access, "fetch_pois", _fail)
        with pytest.raises(RuntimeError):
            access.load_access_points(verbose=False)
        assert called["refetched"]

    def test_an_older_osm_sweep_does_not_invalidate(self, tmp_path, monkeypatch):
        access, cache, osm = self._setup_paths(tmp_path, monkeypatch)
        osm.write_text("[]")
        os.utime(osm, (time.time() - 600, time.time() - 600))
        self._write_cache(access, cache, [{"kind": "water"}])
        assert access.load_access_points(verbose=False) == [{"kind": "water"}]

    def test_bare_list_is_the_old_format_and_is_rebuilt(self, tmp_path, monkeypatch):
        access, cache, _ = self._setup_paths(tmp_path, monkeypatch)
        cache.write_text(json.dumps([{"kind": "trailhead"}]))
        monkeypatch.setattr(
            access, "fetch_pois", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refetch"))
        )
        with pytest.raises(RuntimeError):
            access.load_access_points(verbose=False)
