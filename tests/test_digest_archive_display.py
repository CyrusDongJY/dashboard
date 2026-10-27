from copy import deepcopy

import pytest

from tactical_stress import COMPONENT_SPECS, StressInput, score_eod_stress
from liquidity_digest.model import plan, sessions
from liquidity_digest.frontpage import index_observation
from liquidity_digest.tactical_history import build_tactical_history
from test_liquidity_digest_frontpage import capture as water_capture, P


def tactical_capture():
    rows = []
    days = sessions("2026-08-01", P["market_date"])[-22:]
    for i, day in enumerate(days):
        values = dict(vix=12 + i / 2, move=105, credit_spread=4, trin=1.3, pcr=1, cmf=.05)
        inputs = {k: StressInput(v, day, 0, "source:" + k) for k, v in values.items()}
        scored = score_eod_stress(inputs, computed_at=day + "T20:35:00Z")
        components = scored.components
        components["_meta"] = {"status": scored.status, "reasons": scored.reasons}
        rows.append(dict(record_date=day, eod_stress_score=scored.score,
                         stress_components=components, stress_coverage=scored.coverage,
                         stress_confidence=scored.confidence, stress_calc_version=scored.calc_version,
                         stress_computed_at=scored.computed_at, ingested_at=day + "T20:36:00Z"))
    return dict(status="OK", captured_at="2026-10-23T13:10:00Z", data=rows)


def test_endpoint_delta_survives_internal_missing_day_without_claiming_continuity():
    capture = tactical_capture()
    old = deepcopy(capture)
    missing = capture["data"].pop(-3)["record_date"]
    history = build_tactical_history(P, capture)
    for key, offset in (("1d", 2), ("5d", 6), ("21d", 22)):
        assert history["changes"][key] == old["data"][-1]["eod_stress_score"] - old["data"][-offset]["eod_stress_score"]
    assert history["windows"]["5d"]["missing_dates"] == [missing]
    assert "不确认连续性" in history["reason"]
    assert history["alert_eligible"] is False
    assert "净压力增加" in history["direction_cn"]


@pytest.mark.parametrize("bad", ["endpoint", "version", "source", "value", "context", "coverage", "future_source", "status", "stale_date"])
def test_incompatible_endpoint_is_not_a_valid_delta(bad):
    capture = tactical_capture()
    row = capture["data"][-6]
    if bad == "endpoint":
        capture["data"].pop(-6)
    elif bad == "version":
        row["stress_calc_version"] = "unreviewed"
    elif bad == "source":
        row["stress_components"]["vix"]["source_name"] = "different-provider"
    elif bad == "value":
        row["eod_stress_score"] = 99
    elif bad == "context":
        row["stress_components"]["_context"] = {}
    elif bad == "coverage":
        row["stress_coverage"] = .5
    elif bad == "future_source":
        row["stress_components"]["vix"]["source_date"] = "2099-01-01"
    elif bad == "status":
        row["stress_components"]["vix"]["status"] = "STALE_SOURCE"
    else:
        row["stress_components"]["credit_spread"]["source_date"] = "2026-01-01"
    assert build_tactical_history(P, capture)["changes"]["5d"] is None


@pytest.mark.parametrize("bad", ["cache", "cutoff", "old_capture", "future_score", "missing_current"])
def test_stale_or_unknown_current_does_not_generate_changes(bad):
    capture = tactical_capture()
    if bad == "cache":
        capture["status"] = "CACHED_AFTER_ERROR"
    elif bad == "cutoff":
        capture["captured_at"] = "2026-10-23T14:00:00Z"
    elif bad == "old_capture":
        capture["captured_at"] = "2026-10-22T21:00:00Z"
    elif bad == "future_score":
        capture["data"][-1]["stress_computed_at"] = "2026-10-23T14:00:00Z"
    else:
        capture["data"].pop()
    assert all(v is None for v in build_tactical_history(P, capture)["changes"].values())


def test_old_liquidity_archive_is_visible_without_splicing_into_new_version():
    capture = water_capture("liquidity")
    for i, row in enumerate(capture["data"][:-1]):
        row["calc_version"] = "liquidity_v2" if i < 8 else "liquidity_v2.3"
        row["coverage"] = .8
    before = deepcopy(capture)
    result = index_observation(P, capture, "liquidity")
    assert len(result["legacy_segments"]) == 2
    assert sum(len(s["points"]) for s in result["legacy_segments"]) == 24
    assert all(s["comparable_to_current"] is False for s in result["legacy_segments"])
    assert result["value"] == 64
    assert result["changes"] == dict.fromkeys(["1d", "5d", "21d"])
    assert capture == before


def test_legacy_gaps_unknown_versions_and_after_cutoff_are_not_filled():
    capture = water_capture("liquidity")
    for row in capture["data"][:-1]:
        row["calc_version"] = "liquidity_v2.3"
    capture["data"][4]["composite"] = None
    capture["data"][6]["created_at"] = "2099-01-01T00:00:00Z"
    capture["data"][8]["calc_version"] = "future_unreviewed"
    result = index_observation(P, capture, "liquidity")
    points = {p["date"]: p["value"] for s in result["legacy_segments"] for p in s["points"]}
    assert points[capture["data"][4]["report_date"]] is None
    assert capture["data"][6]["report_date"] not in points
    assert capture["data"][8]["report_date"] not in points
    assert len(result["legacy_segments"]) == 2


def test_chart_uses_existing_pressure_boundaries(monkeypatch):
    from matplotlib.axes import Axes
    from liquidity_digest.render import chart_images
    from liquidity_digest.sample import sample_report
    from temporal_monitor import HIGH_PRESSURE, LOW_PRESSURE
    seen = []
    original = Axes.axhline

    def record(self, y=0, *args, **kwargs):
        seen.append(y)
        return original(self, y, *args, **kwargs)

    monkeypatch.setattr(Axes, "axhline", record)
    assert len(chart_images(sample_report(plan("2026-10-23")))) == 3
    assert HIGH_PRESSURE in seen and LOW_PRESSURE in seen
