"""Merging the legs of a composed hike.

Concurrent routes are the problem. A long-distance relation ("Bay Area Ridge
Trail") is mapped over the same ground as the local trail it follows, so Dijkstra's
consecutive edges flip between two records. A real 17.8 mi hike came back as 80
legs alternating every 0.02 mi, and its trail list named 80 trails.

Collapsing only *consecutive* duplicates does not help, because the sequence is
A,B,A,B — nothing is consecutive. Absorbing sub-stride legs into their larger
neighbour does: that route now reports 8 legs and 8 names.
"""

from __future__ import annotations

from pipeline.trail_graph import TrailGraph

MIN = 0.15


def _leg(name, miles, coords=None):
    return {"trail_id": name.lower(), "name": name, "miles": miles,
            "coordinates": coords or [[0.0, 0.0]]}


def _absorb(legs, minimum=MIN):
    return TrailGraph._absorb_short_legs(legs, minimum)


class TestAbsorbShortLegs:
    def test_alternating_concurrent_routes_collapse(self):
        legs = [_leg("Coyote", 0.78), _leg("Mine", 1.21), _leg("Ridge", 0.07),
                _leg("Mine", 0.06), _leg("Ridge", 0.02), _leg("Fortini", 0.81)]
        assert [l["name"] for l in _absorb(legs)] == ["Coyote", "Mine", "Fortini"]

    def test_total_mileage_is_preserved(self):
        legs = [_leg("A", 0.78), _leg("B", 1.21), _leg("C", 0.07),
                _leg("B", 0.06), _leg("C", 0.02), _leg("D", 0.81)]
        before = round(sum(l["miles"] for l in legs), 2)
        assert round(sum(l["miles"] for l in _absorb(legs)), 2) == before

    def test_a_short_leg_joins_the_larger_neighbour(self):
        legs = [_leg("Small", 0.30), _leg("Blip", 0.05), _leg("Large", 3.00)]
        out = _absorb(legs)
        assert [l["name"] for l in out] == ["Small", "Large"]
        assert out[1]["miles"] == 3.05

    def test_genuinely_short_routes_survive(self):
        # A 0.1 mi walk is still the whole walk; never absorb the only leg.
        legs = [_leg("Nub", 0.10)]
        assert _absorb(legs) == legs

    def test_long_legs_are_left_alone(self):
        legs = [_leg("A", 2.0), _leg("B", 3.0), _leg("C", 1.0)]
        assert [l["name"] for l in _absorb(legs)] == ["A", "B", "C"]

    def test_same_name_either_side_merges_into_one(self):
        legs = [_leg("JMT", 1.2), _leg("Blip", 0.03), _leg("JMT", 2.0)]
        out = _absorb(legs)
        assert [l["name"] for l in out] == ["JMT"]
        assert out[0]["miles"] == 3.23

    def test_geometry_is_kept_in_walking_order(self):
        legs = [
            _leg("A", 1.0, [[0, 0], [1, 0]]),
            _leg("Blip", 0.01, [[1, 0], [2, 0]]),
            _leg("B", 2.0, [[2, 0], [3, 0]]),
        ]
        out = _absorb(legs)
        # The blip is absorbed forwards into B, so its points precede B's.
        assert out[-1]["coordinates"] == [[1, 0], [2, 0], [2, 0], [3, 0]]

    def test_shortest_is_absorbed_first(self):
        # Absorbing in path order lets an early leg swallow a connector that a
        # later, larger neighbour should take.
        legs = [_leg("A", 0.20), _leg("Tiny", 0.01), _leg("B", 5.0)]
        out = _absorb(legs)
        assert out[-1]["miles"] == 5.01
        assert out[0]["miles"] == 0.20
