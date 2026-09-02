"""Tests for the data pipeline: normalization, elevation, and search filtering."""

from __future__ import annotations

import math
import random

import pytest

from pipeline.elevation import compute_gain
from pipeline.normalize import (
    chain_lines,
    normalize_grade,
    normalize_surface,
    normalize_trail_class,
    parse_season,
)
from pipeline.spatial import PointGrid
from planner.discover import difficulty_rating


class TestElevationGain:
    def test_clean_climb(self):
        gain, loss = compute_gain([5000 + i * 10 for i in range(101)])
        assert gain == 1000.0
        assert loss == 0.0

    def test_clean_descent(self):
        gain, loss = compute_gain([6000 - i * 10 for i in range(101)])
        assert gain == 0.0
        assert loss == 1000.0

    def test_direction_can_reverse_repeatedly(self):
        # Regression: the original guard required direction >= 0 to count a climb,
        # so once a route descended it could never accumulate gain again. A 114 mi
        # stretch of the PCT reported 0 ft of gain.
        up = [5000 + i * 10 for i in range(101)]
        down = [6000 - i * 10 for i in range(1, 101)]
        up_again = [5000 + i * 10 for i in range(1, 101)]
        gain, loss = compute_gain(up + down + up_again)
        assert gain == pytest.approx(2000.0)
        assert loss == pytest.approx(1000.0)

    def test_sampling_noise_does_not_create_gain(self):
        # Regression: summing every positive delta turns DEM/GPS jitter into
        # thousands of feet of phantom climbing.
        random.seed(7)
        noise = [5000 + random.uniform(-8, 8) for _ in range(400)]
        gain, _ = compute_gain(noise)
        naive = sum(max(0.0, noise[i] - noise[i - 1]) for i in range(1, len(noise)))
        assert gain == 0.0
        assert naive > 500  # what the old approach would have reported

    def test_real_climb_survives_noise(self):
        random.seed(11)
        series = [5000 + i * 5 + random.uniform(-6, 6) for i in range(200)]
        gain, _ = compute_gain(series)
        assert 900 < gain < 1100  # true gain is ~995 ft

    def test_short_series(self):
        assert compute_gain([]) == (0.0, 0.0)
        assert compute_gain([100.0]) == (0.0, 0.0)


class TestNormalizeValues:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12-20%", "12-20%"),
            ("TG05 - +12-20%", "12-20%"),  # dual encoding collapses to one label
            ("TG01 - +0-5%", "0-5%"),
            ("N/A", None),
            (None, None),
            ("", None),
        ],
    )
    def test_grade(self, raw, expected):
        result = normalize_grade(raw)
        assert (result["label"] if result else None) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Dual encoding and synonyms both collapse: the feed spells the same
            # surface as "NATIVE MATERIAL", "NAT - NATIVE MATERIAL" and "EARTH",
            # which listed as three separate facet options.
            ("NATIVE MATERIAL", "Native"),
            ("NAT - NATIVE MATERIAL", "Native"),
            ("EARTH", "Native"),
            ("AC- ASPHALT", "Paved"),
            ("CONCRETE", "Paved"),
            ("CRUSHED AGGREGATE OR GRAVEL", "Gravel"),
            ("N/A", None),
        ],
    )
    def test_surface(self, raw, expected):
        assert normalize_surface(raw) == expected

    @pytest.mark.parametrize("raw,expected", [("1", 1), ("3", 3), ("N", None), ("9", None), (None, None)])
    def test_trail_class(self, raw, expected):
        assert normalize_trail_class(raw) == expected

    def test_nullish_strings_are_none(self):
        # "N/A" must not become the string value "N/A" in a facet list.
        assert normalize_surface("N/A") is None
        assert normalize_grade("None") is None


class TestSeason:
    def test_year_round(self):
        season = parse_season("01/01-12/31")
        assert season["year_round"] is True
        assert len(season["months"]) == 12

    def test_summer_window(self):
        season = parse_season("06/15-10/30")
        assert season["year_round"] is False
        assert season["months"] == [6, 7, 8, 9, 10]
        assert 1 not in season["months"]

    def test_window_wrapping_new_year(self):
        season = parse_season("11/16-04/30")
        assert 12 in season["months"] and 1 in season["months"]
        assert 6 not in season["months"]

    def test_unparseable(self):
        assert parse_season("N/A") is None
        assert parse_season(None) is None


class TestChainLines:
    def test_joins_shared_endpoints(self):
        a = [[0.0, 0.0], [1.0, 0.0]]
        b = [[1.0, 0.0], [2.0, 0.0]]
        chains = chain_lines([a, b])
        assert len(chains) == 1
        assert chains[0][0] == [0.0, 0.0]
        assert chains[0][-1] == [2.0, 0.0]

    def test_joins_reversed_segment(self):
        a = [[0.0, 0.0], [1.0, 0.0]]
        b = [[2.0, 0.0], [1.0, 0.0]]  # runs backwards
        assert len(chain_lines([a, b])) == 1

    def test_keeps_disjoint_parts_separate(self):
        # Regression: the old loader concatenated every segment's coordinates into
        # one LineString, drawing a false trail across the gap between them.
        a = [[0.0, 0.0], [1.0, 0.0]]
        b = [[50.0, 50.0], [51.0, 50.0]]
        assert len(chain_lines([a, b])) == 2


class TestDifficulty:
    def test_known_bands(self):
        assert difficulty_rating(2.0, 300)["label"] == "easy"
        assert difficulty_rating(10.0, 6455)["label"] == "very strenuous"

    def test_formula_matches_published_rating(self):
        rating = difficulty_rating(10.0, 2000)
        assert rating["score"] == pytest.approx(math.sqrt(2 * 2000 * 10), abs=0.1)

    def test_unknown_gain_yields_no_rating(self):
        # Must be None, never "easy" — an unmeasured trail is not a flat one.
        assert difficulty_rating(5.0, None) is None
        assert difficulty_rating(None, 1000) is None


class TestPointGrid:
    def test_finds_points_in_radius(self):
        grid = PointGrid()
        grid.add(37.75, -119.55, {"id": "near", "kind": "peak"})
        grid.add(38.90, -120.90, {"id": "far", "kind": "peak"})
        hits = grid.near(37.75, -119.55, radius_miles=1.0)
        assert [h["id"] for h in hits] == ["near"]

    def test_near_path_keeps_closest_hit(self):
        grid = PointGrid()
        grid.add(37.7500, -119.5500, {"id": "wf", "kind": "waterfall"})
        path = [[-119.5600, 37.7500], [-119.5500, 37.7500]]
        hits = grid.near_path(path, radius_miles=1.0)
        assert "wf" in hits
        assert hits["wf"]["distance_mi"] == pytest.approx(0.0, abs=0.01)

    def test_empty_grid(self):
        assert PointGrid().near(37.0, -119.0, 5.0) == []


class TestSteepness:
    """Steepness is a separate axis from effort, calibrated against real ratings."""

    def test_bands(self):
        from planner.discover import steepness_rating

        assert steepness_rating(10.0, 500)["label"] == "gentle"       # 50 ft/mi
        assert steepness_rating(10.0, 2000)["label"] == "moderate"    # 200 ft/mi
        assert steepness_rating(10.0, 4000)["label"] == "steep"       # 400 ft/mi
        assert steepness_rating(10.0, 8000)["label"] == "very steep"  # 800 ft/mi

    def test_reports_ft_per_mile(self):
        from planner.discover import steepness_rating

        assert steepness_rating(4.0, 2000)["ft_per_mi"] == 500

    def test_unknown_gain_yields_no_rating(self):
        from planner.discover import steepness_rating

        assert steepness_rating(5.0, None) is None
        assert steepness_rating(None, 1000) is None

    def test_independent_of_effort(self):
        """A short brutal climb and a long easy walk must not collapse together."""
        from planner.discover import difficulty_rating, steepness_rating

        short_steep = (1.0, 1000)   # 1000 ft/mi
        long_gentle = (20.0, 1000)  # 50 ft/mi

        assert steepness_rating(*short_steep)["label"] == "very steep"
        assert steepness_rating(*long_gentle)["label"] == "gentle"
        # Effort ranks the long one higher even though it is far less steep.
        assert difficulty_rating(*long_gentle)["score"] > difficulty_rating(*short_steep)["score"]


class TestPhotoRelevance:
    """Proximity alone surfaces macro shots of plants beside the trail."""

    def test_scenery_titles_outrank_species(self):
        from pipeline.photos import _relevance

        assert _relevance("File:Ryan Mountain Trail 04.jpg") > _relevance(
            "File:Pinus benthamiana 08822.JPG"
        )

    def test_camera_default_filenames_are_penalized(self):
        from pipeline.photos import _relevance

        assert _relevance("File:Nevada Falls panorama.jpg") > _relevance("File:DSC_1234.jpg")

    def test_numeric_only_titles_are_penalized(self):
        from pipeline.photos import _relevance

        assert _relevance("File:Half Dome summit view.jpg") > _relevance("File:12345678.jpg")

    def test_missing_title(self):
        from pipeline.photos import _relevance

        assert _relevance(None) == 0.0


class TestLicenseFilter:
    def test_only_open_licenses_pass(self):
        from pipeline.photos import _license_ok

        assert _license_ok("CC BY-SA 3.0")
        assert _license_ok("CC0")
        assert _license_ok("Public domain")
        assert not _license_ok("Fair use")
        assert not _license_ok("All rights reserved")
        assert not _license_ok(None)


class TestPhotoPlaceRelevance:
    """Proximity puts orbital imagery inside the radius of almost every trail."""

    def _tokens(self):
        from pipeline.photos import place_tokens_for

        return place_tokens_for({"name": "Tahoe Rim Trail", "mgmt_area": None})

    def test_orbital_imagery_is_rejected(self):
        # Regression: "ISS041-E-34506 - View of Earth" matched the "view" keyword
        # and ranked first for the Tahoe Rim Trail.
        from pipeline.photos import _relevance

        place = self._tokens()
        assert _relevance("File:ISS041-E-34506 - View of Earth.jpg", place) < 0

    def test_place_name_match_outranks_generic_scenery(self):
        from pipeline.photos import _relevance

        place = self._tokens()
        named = _relevance("File:Tahoe Rim Trail near Watson Lake.jpg", place)
        generic = _relevance("File:A lake with a nice view.jpg", place)
        assert named > generic

    def test_generic_terrain_words_are_not_evidence(self):
        from pipeline.photos import place_tokens_for

        # "trail", "lake", "national", "forest" are too common to identify a place.
        tokens = place_tokens_for(
            {"name": "Lake Creek Trail", "mgmt_area": "Eldorado National Forest"}
        )
        assert "trail" not in tokens and "lake" not in tokens and "forest" not in tokens
        assert "eldorado" in tokens


class TestTrailGraphGain:
    """Gain must be computed along the path, not summed per edge."""

    def test_noise_between_close_nodes_is_not_climb(self):
        # Regression: nodes are metres apart, so per-edge max(0, delta) counted DEM
        # noise as ascent and reported Mt Whitney at 14,232 ft against a true 6,100.
        from pipeline.elevation import compute_gain

        import random

        random.seed(3)
        flat_with_noise = [8000 + random.uniform(-4, 4) for _ in range(500)]
        per_edge = sum(
            max(0.0, flat_with_noise[i] - flat_with_noise[i - 1])
            for i in range(1, len(flat_with_noise))
        )
        assert compute_gain(flat_with_noise)[0] == 0.0
        assert per_edge > 400  # what per-edge summation would have reported

    def test_snap_reads_the_module_global(self):
        # Regression: SNAP_METERS was a default argument, evaluated once at
        # definition, so tuning it produced a byte-identical graph.
        import pipeline.trail_graph as tg

        original = tg.SNAP_METERS
        try:
            tg.SNAP_METERS = 4.0
            fine = tg._snap(-119.5, 37.7)
            tg.SNAP_METERS = 400.0
            coarse = tg._snap(-119.5, 37.7)
            assert fine != coarse
        finally:
            tg.SNAP_METERS = original
