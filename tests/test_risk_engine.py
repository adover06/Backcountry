"""Risk engine tests.

These focus on the failure paths, because that is where the dangerous behavior was:
before this rewrite, every check that errored produced a green "go".
"""

from __future__ import annotations

import pytest

from planner.risk_engine import evaluate_risk


def _ok_checks(**overrides):
    """A baseline where every check ran and found nothing concerning."""
    checks = {
        "weather": {"forecast": [{"name": "Today", "short": "Sunny", "temp": 68, "wind": "5 mph NW"}]},
        "alerts": {"alerts": []},
        "aqi": {"observations": [{"parameter": "PM2.5", "aqi": 30, "category": "Good"}]},
        "fire": {"perimeters": {"type": "FeatureCollection", "features": []}},
        "snow": {"max_depth_in": 0.0, "max_snowfall_in": 0.0},
        "water": {"count": 3},
    }
    checks.update(overrides)
    return checks


class TestBaseline:
    def test_all_clear_is_go(self):
        result = evaluate_risk(_ok_checks())
        assert result["status"] == "go"
        assert result["complete"] is True
        assert result["unavailable_checks"] == []


class TestFailedChecksAreNotPasses:
    """The core regression: a check that failed must never read as green."""

    @pytest.mark.parametrize("check", ["weather", "aqi", "fire", "snow"])
    def test_error_payload_forces_incomplete(self, check):
        result = evaluate_risk(_ok_checks(**{check: {"error": "connection timed out"}}))
        assert result["status"] == "incomplete"
        assert check in result["unavailable_checks"]

    def test_snow_unavailable_status_is_not_go(self):
        # Regression: snow.py returns max_depth_in=None on failure, and the old
        # isinstance() guard silently skipped it, yielding "go".
        checks = _ok_checks(
            snow={"status": "unavailable", "max_depth_in": None, "max_snowfall_in": None}
        )
        result = evaluate_risk(checks)
        assert result["status"] == "incomplete"
        assert "snow" in result["unavailable_checks"]

    def test_missing_check_key_is_incomplete(self):
        checks = _ok_checks()
        del checks["aqi"]
        assert evaluate_risk(checks)["status"] == "incomplete"

    def test_empty_aqi_observations_is_unavailable_not_clean(self):
        # AirNow returns [] when no monitor is in range. That is not clean air.
        result = evaluate_risk(_ok_checks(aqi={"observations": []}))
        assert result["status"] == "incomplete"
        assert "aqi" in result["unavailable_checks"]

    def test_alerts_never_checked_is_incomplete(self):
        checks = _ok_checks()
        del checks["alerts"]
        assert evaluate_risk(checks)["status"] == "incomplete"

    def test_incomplete_never_downgrades_a_real_hazard(self):
        checks = _ok_checks(
            aqi={"observations": [{"parameter": "PM2.5", "aqi": 200, "category": "Very Unhealthy"}]},
            snow={"error": "timeout"},
        )
        result = evaluate_risk(checks)
        assert result["status"] == "no-go"


class TestAirQuality:
    def test_high_aqi_is_no_go(self):
        checks = _ok_checks(aqi={"observations": [{"aqi": 175, "category": "Unhealthy"}]})
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_moderate_aqi_is_caution(self):
        checks = _ok_checks(aqi={"observations": [{"aqi": 120, "category": "USG"}]})
        assert evaluate_risk(checks)["status"] == "caution"

    def test_worst_observation_governs(self):
        checks = _ok_checks(
            aqi={"observations": [{"aqi": 20, "category": "Good"}, {"aqi": 190, "category": "Unhealthy"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"


class TestWeather:
    """Weather was entirely absent from scoring before this rewrite."""

    def test_blizzard_is_no_go(self):
        # Regression: the old regex matched only thunder|severe|shower|rain|snow,
        # so "Blizzard Conditions" rendered as Good in green.
        checks = _ok_checks(
            weather={"forecast": [{"name": "Tonight", "short": "Blizzard Conditions", "temp": 20, "wind": "30 mph"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    @pytest.mark.parametrize("phrase", ["Ice Storm", "Freezing Rain", "Heavy Snow", "High Wind"])
    def test_winter_hazards_are_caught(self, phrase):
        checks = _ok_checks(
            weather={"forecast": [{"name": "Day", "short": phrase, "temp": 45, "wind": "10 mph"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_dangerous_cold_is_no_go(self):
        checks = _ok_checks(
            weather={"forecast": [{"name": "Tonight", "short": "Clear", "temp": 8, "wind": "5 mph"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_freezing_overnight_is_caution(self):
        checks = _ok_checks(
            weather={"forecast": [{"name": "Tonight", "short": "Clear", "temp": 28, "wind": "5 mph"}]}
        )
        assert evaluate_risk(checks)["status"] == "caution"

    def test_sunny_but_dangerously_windy_is_not_go(self):
        # "Sunny" with 45 mph wind used to score Good on the text alone.
        checks = _ok_checks(
            weather={"forecast": [{"name": "Today", "short": "Sunny", "temp": 60, "wind": "20 to 45 mph W"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_extreme_heat_is_no_go(self):
        checks = _ok_checks(
            weather={"forecast": [{"name": "Today", "short": "Sunny", "temp": 108, "wind": "5 mph"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_forecast_not_covering_trip_dates_is_incomplete(self):
        checks = _ok_checks(
            weather={
                "forecast": [{"name": "Today", "short": "Sunny", "temp": 70, "wind": "5 mph"}],
                "covers_trip_dates": False,
            }
        )
        result = evaluate_risk(checks)
        assert result["status"] == "incomplete"
        assert "weather" in result["unavailable_checks"]


class TestAlerts:
    def test_severe_alert_is_no_go(self):
        checks = _ok_checks(
            alerts={"alerts": [{"event": "Winter Storm Warning", "severity": "Severe"}]}
        )
        assert evaluate_risk(checks)["status"] == "no-go"

    def test_advisory_is_caution(self):
        checks = _ok_checks(
            alerts={"alerts": [{"event": "Frost Advisory", "severity": "Minor"}]}
        )
        assert evaluate_risk(checks)["status"] == "caution"


class TestFire:
    def _perimeter(self, **props):
        return {"type": "Feature", "properties": props, "geometry": None}

    def test_near_recent_fire_is_caution(self):
        checks = _ok_checks(
            fire={"perimeters": {"features": [self._perimeter(distance_mi=3.0, days_since_update=2)]}}
        )
        assert evaluate_risk(checks)["status"] == "caution"

    def test_distant_fire_is_ignored(self):
        checks = _ok_checks(
            fire={"perimeters": {"features": [self._perimeter(distance_mi=80.0, days_since_update=2)]}}
        )
        assert evaluate_risk(checks)["status"] == "go"

    def test_old_burn_scar_is_ignored(self):
        # Regression: FIRE_HISTORY_DAYS defaulted to 3650, so decade-old scars were
        # reported as "Active fire perimeters present", training the user to ignore it.
        checks = _ok_checks(
            fire={"perimeters": {"features": [self._perimeter(distance_mi=3.0, days_since_update=2000)]}}
        )
        assert evaluate_risk(checks)["status"] == "go"

    def test_fire_with_unknown_metadata_is_kept(self):
        # Regression: fire.py dropped features whose date field was null, and
        # itinerary_ai coerced missing distance to 999 and filtered them out.
        checks = _ok_checks(
            fire={"perimeters": {"features": [self._perimeter(distance_mi=None, days_since_update=None)]}}
        )
        assert evaluate_risk(checks)["status"] == "caution"

    def test_truncated_feed_is_incomplete(self):
        # The ArcGIS query caps at 2000 records; absence proves nothing if capped.
        checks = _ok_checks(
            fire={"perimeters": {"features": []}, "truncated": True}
        )
        result = evaluate_risk(checks)
        assert result["status"] == "incomplete"
        assert "fire" in result["unavailable_checks"]


class TestSnow:
    def test_deep_snow_is_caution(self):
        checks = _ok_checks(snow={"max_depth_in": 30.0, "max_snowfall_in": 0.0})
        assert evaluate_risk(checks)["status"] == "caution"

    def test_heavy_snowfall_is_caution(self):
        checks = _ok_checks(snow={"max_depth_in": 0.0, "max_snowfall_in": 10.0})
        assert evaluate_risk(checks)["status"] == "caution"


class TestWater:
    def test_no_water_found_is_caution(self):
        assert evaluate_risk(_ok_checks(water={"count": 0}))["status"] == "caution"

    def test_water_absent_from_payload_is_not_a_failure(self):
        checks = _ok_checks()
        del checks["water"]
        assert evaluate_risk(checks)["status"] == "go"


class TestOutputShape:
    def test_reasons_are_sorted_most_severe_first(self):
        checks = _ok_checks(
            aqi={"observations": [{"aqi": 200, "category": "Very Unhealthy"}]},
            water={"count": 0},
        )
        severities = [r["severity"] for r in evaluate_risk(checks)["reasons"]]
        assert severities[0] == "no-go"

    def test_summary_names_the_missing_checks(self):
        result = evaluate_risk(_ok_checks(snow={"error": "boom"}))
        assert "snow" in result["summary"]
