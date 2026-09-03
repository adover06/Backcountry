"""Assembling OSM ways into trails.

The gap this closes, measured on the South Bay (36.95..37.45 N, -122.35..-121.55 W):

    trails in the index          15      3 NPS + 12 OSM relations, 0 USFS
    named OSM path/footway/track 4,415

The risk is the opposite failure: emitting a thousand quarter-mile stubs, or a
"Ridge Trail" that spans two counties because the name repeats.
"""

from __future__ import annotations

from pipeline.osm_trails import (
    MIN_TRAIL_MILES,
    assemble,
    dedupe_against,
    usable,
)


def _way(name, coords, osm_id="way/1", **tags):
    return {"osm_id": osm_id, "name": name, "coordinates": coords, "tags": tags}


def _run(start_lng, start_lat, n=40, step=0.002):
    return [[start_lng + i * step, start_lat] for i in range(n)]


class TestUsable:
    def test_a_named_path_is_a_trail(self):
        assert usable(_way("Bear Gulch Trail", _run(-122.0, 37.0)))

    def test_unnamed_ways_are_dropped(self):
        assert not usable(_way("", _run(-122.0, 37.0)))
        assert not usable(_way(None, _run(-122.0, 37.0)))

    def test_sidewalks_and_crossings_are_not_trails(self):
        # highway=footway covers pavement too; without this the map fills with
        # named sidewalks in every town.
        assert not usable(_way("Main St", _run(-122.0, 37.0), footway="sidewalk"))
        assert not usable(_way("Main St", _run(-122.0, 37.0), footway="crossing"))

    def test_driveways_are_not_trails(self):
        assert not usable(_way("Smith Ln", _run(-122.0, 37.0), service="driveway"))

    def test_areas_are_not_trails(self):
        assert not usable(_way("Plaza", _run(-122.0, 37.0), area="yes"))

    def test_a_way_with_one_point_is_not_a_line(self):
        assert not usable(_way("Stub", [[-122.0, 37.0]]))


class TestAssemble:
    def test_fragments_of_one_name_become_one_trail(self):
        # OSM splits a trail at junctions and surface changes. Emitting one trail
        # per fragment is the failure this exists to prevent.
        a = _run(-122.0, 37.0, n=30)
        b = [[a[-1][0] + i * 0.002, 37.0] for i in range(30)]
        trails = assemble([_way("Long Ridge Trail", a, "way/1"),
                           _way("Long Ridge Trail", b, "way/2")], verbose=False)
        assert len(trails) == 1
        assert trails[0]["segment_count"] == 2

    def test_the_same_name_far_away_is_a_different_trail(self):
        # "Ridge Trail" exists in many counties. Chaining by name alone would draw
        # one trail across fifty miles of nothing.
        trails = assemble([
            _way("Ridge Trail", _run(-122.0, 37.0), "way/1"),
            _way("Ridge Trail", _run(-121.0, 36.5), "way/2"),
        ], verbose=False)
        assert len(trails) == 2

    def test_stubs_are_dropped(self):
        tiny = [[-122.0, 37.0], [-121.9995, 37.0]]
        assert assemble([_way("Nub", tiny)], verbose=False) == []

    def test_length_comes_from_geometry(self):
        trails = assemble([_way("Measured Trail", _run(-122.0, 37.0, n=60))], verbose=False)
        assert trails[0]["length_miles"] >= MIN_TRAIL_MILES
        assert trails[0]["length_miles"] == trails[0]["geometry_length_miles"]

    def test_record_matches_the_index_schema(self):
        trails = assemble([_way("Schema Trail", _run(-122.0, 37.0))], verbose=False)
        record = trails[0]
        for key in ("id", "name", "slug", "length_miles", "route_type", "bbox",
                    "center", "geometry", "source", "segment_count", "part_count"):
            assert key in record, key
        assert record["geometry"]["type"] == "MultiLineString"
        assert record["source"] == "OpenStreetMap ways"

    def test_surface_and_operator_come_from_the_majority_of_fragments(self):
        a = _run(-122.0, 37.0, n=30)
        b = [[a[-1][0] + i * 0.002, 37.0] for i in range(30)]
        trails = assemble([
            _way("Mixed Trail", a, "way/1", surface="dirt", operator="Midpen"),
            _way("Mixed Trail", b, "way/2", surface="dirt", operator="Midpen"),
        ], verbose=False)
        assert trails[0]["surface"] == "dirt"
        assert trails[0]["mgmt_area"] == "Midpen"

    def test_ids_are_stable_across_runs(self):
        ways = [_way("Stable Trail", _run(-122.0, 37.0))]
        assert assemble(ways, verbose=False)[0]["id"] == assemble(ways, verbose=False)[0]["id"]


class TestDedupe:
    def test_an_agency_trail_wins(self):
        # The agency record carries trail class, grade and season; OSM does not.
        osm = assemble([_way("Mist Trail", _run(-119.55, 37.72))], verbose=False)
        existing = [{"name": "Mist Trail", "bbox": [-119.56, 37.71, -119.50, 37.73]}]
        assert dedupe_against(osm, existing, verbose=False) == []

    def test_differing_extents_of_one_trail_still_match(self):
        # One source maps a spur the other stops short of, which moves the centres
        # apart while the trails clearly occupy the same ground.
        osm = assemble([_way("Mist Trail", _run(-119.55, 37.72, n=80))], verbose=False)
        existing = [{"name": "Mist Trail", "bbox": [-119.56, 37.715, -119.545, 37.725]}]
        assert dedupe_against(osm, existing, verbose=False) == []

    def test_the_same_name_elsewhere_is_kept(self):
        osm = assemble([_way("Mist Trail", _run(-122.0, 37.0))], verbose=False)
        existing = [{"name": "Mist Trail", "bbox": [-119.56, 37.71, -119.50, 37.73]}]
        assert len(dedupe_against(osm, existing, verbose=False)) == 1

    def test_trails_with_no_agency_match_are_kept(self):
        osm = assemble([_way("Sierra Azul Loop", _run(-121.9, 37.15))], verbose=False)
        assert len(dedupe_against(osm, [], verbose=False)) == 1
