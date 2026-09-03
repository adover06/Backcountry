"""Filtering by activity, and keeping jeep roads out of hiking results.

The complaint that prompted this: searching for hikes returned "Rubicon Jeep".
No hand labelling was needed — USFS publishes allowed-use per trail, and that
record carries fourwd + atv + motorcycle.

Two traps this pins down:

* `hiking: allowed` is true for 11,150 of the 11,150 trails that carry use data, so
  it separates nothing. Motorised use is the discriminator.
* 2,603 trails (18.9%) have no use data at all. Treating silence as motorised would
  drop most of the NPS set out of every hiking search.
"""

from __future__ import annotations

import pytest

from planner.discover import (
    ACTIVITY_PREDICATES,
    BACKPACKING_MIN_MILES,
    is_motorised,
)


def _trail(uses=None, **extra):
    activities = {
        name: {"allowed": True, "restricted": None, "season": None}
        for name in (uses or [])
    }
    return {"name": "T", "activities": activities or None, **extra}


class TestMotorised:
    def test_a_jeep_route_is_motorised(self):
        assert is_motorised(_trail(["hiking", "bike", "fourwd", "atv", "motorcycle"]))

    def test_a_foot_trail_is_not(self):
        assert not is_motorised(_trail(["hiking", "bike", "horse"]))

    def test_any_single_motorised_use_counts(self):
        for use in ("fourwd", "atv", "motorcycle", "snowmobile"):
            assert is_motorised(_trail(["hiking", use])), use

    def test_missing_use_data_is_not_treated_as_motorised(self):
        # Silence is not evidence. 18.9% of the index has no allowed-use data.
        assert not is_motorised(_trail())
        assert not is_motorised({"name": "T"})


class TestActivityPredicates:
    def test_hiking_excludes_motorised_but_keeps_unknowns(self):
        hiking = ACTIVITY_PREDICATES["hiking"]
        assert hiking(_trail(["hiking"]))
        assert hiking(_trail())                        # no data — still shown
        assert not hiking(_trail(["hiking", "fourwd"]))

    def test_motorized_is_the_complement_of_hiking(self):
        hiking, motorized = ACTIVITY_PREDICATES["hiking"], ACTIVITY_PREDICATES["motorized"]
        for t in (_trail(["hiking"]), _trail(), _trail(["hiking", "atv"])):
            assert hiking(t) != motorized(t)

    def test_bike_and_horse_read_the_published_use(self):
        assert ACTIVITY_PREDICATES["bike"](_trail(["bike"]))
        assert not ACTIVITY_PREDICATES["bike"](_trail(["hiking"]))
        assert ACTIVITY_PREDICATES["horse"](_trail(["horse"]))

    def test_an_explicitly_disallowed_use_is_not_allowed(self):
        trail = {"activities": {"bike": {"allowed": False}}}
        assert not ACTIVITY_PREDICATES["bike"](trail)


class TestBackpacking:
    def test_wilderness_qualifies_at_any_length(self):
        t = _trail(["hiking"], length_miles=2.0, wilderness={"name": "John Muir Wilderness"})
        assert ACTIVITY_PREDICATES["backpacking"](t)

    def test_long_trails_qualify(self):
        t = _trail(["hiking"], length_miles=BACKPACKING_MIN_MILES, wilderness={})
        assert ACTIVITY_PREDICATES["backpacking"](t)

    def test_short_front_country_trails_do_not(self):
        t = _trail(["hiking"], length_miles=1.5, wilderness={})
        assert not ACTIVITY_PREDICATES["backpacking"](t)

    def test_a_long_jeep_road_is_not_a_backpacking_route(self):
        # The reason backpacking checks motorised use rather than length alone.
        t = _trail(["hiking", "fourwd"], length_miles=40.0, wilderness={})
        assert not ACTIVITY_PREDICATES["backpacking"](t)

    def test_missing_length_does_not_qualify_by_accident(self):
        assert not ACTIVITY_PREDICATES["backpacking"](_trail(["hiking"], wilderness={}))
