from copy import deepcopy
from datetime import date, timedelta

import pandas as pd
import pytest

from backfill_digest_reference import restore_breadth_lineage
from liquidity_digest.history_reference import attach, digest, extract, package
from liquidity_digest.internals import build_internals
from liquidity_digest.model import plan, sessions
from liquidity_digest.states import build_states


def example():
    p = plan("2026-10-20")
    days = sessions(date.fromisoformat(p["market_date"]) - timedelta(days=40), p["market_date"])[-22:]
    replay = [{"report_date": d, "source_date": d, "panel": "environment", "risk_value": 50 + i,
               "quality_status": "OK", "coverage": 1, "confidence": 1, "calc_version": "temporal_v1",
               "observation_mode": "REPLAY", "computed_at": "2026-10-20T12:00:00Z",
               "details": {"source_calc_version": "env_v1", "comparison_signature": "basket",
                           "comparison_start_date": days[0]}} for i, d in enumerate(days)]
    current = deepcopy(replay[-1])
    current["observation_mode"] = "LIVE_SHADOW"
    current["details"]["comparison_start_date"] = days[-1]
    bundle = package("environment", replay, "2026-10-20T12:01:00Z", {"archive": "verified"})
    current["details"]["history_reference"] = bundle
    report = {**p, "temporal": {"environment": [current]},
              "source_audit": {"db_temporal_state_daily": {"status": "OK"}}}
    report["states"] = build_states(report)
    return report, current, bundle


def test_reference_changes_display_but_never_live_state_or_alerts():
    report, current, bundle = example()
    alerts = deepcopy(report["states"]["alerts"])
    attach(report, [current])
    assert report["states"]["pressure"]["direction"] == "UNKNOWN"
    assert report["states"]["pressure"]["changes"]["5d"] is None
    assert report["states"]["display_pressure"]["changes"]["5d"] == 5
    assert report["states"]["display_pressure"]["history_basis"] == "RECONSTRUCTED_REFERENCE"
    assert report["states"]["alerts"] == alerts
    assert all(r["observation_mode"] == "REPLAY" for r in report["reference_history"]["environment"])
    assert bundle["point_in_time_eligible"] is False


@pytest.mark.parametrize("bad", ["hash", "future", "anchor", "version", "signature", "mode", "duplicate", "pit", "alert"])
def test_invalid_reference_is_rejected(bad):
    report, current, bundle = example()
    if bad == "hash":
        bundle["sha256"] = "bad"
    elif bad == "future":
        bundle["computed_at"] = "2026-10-21T00:00:00Z"
    elif bad == "pit":
        bundle["point_in_time_eligible"] = True
    elif bad == "alert":
        bundle["alert_eligible"] = True
    else:
        if bad == "anchor":
            bundle["rows"][-1]["risk_value"] = 0
        if bad == "version":
            bundle["rows"][-1]["details"]["source_calc_version"] = "old"
        if bad == "signature":
            bundle["rows"][-1]["details"]["comparison_signature"] = "other"
        if bad == "mode":
            bundle["rows"][0]["observation_mode"] = "LIVE_SHADOW"
        if bad == "duplicate":
            bundle["rows"].append(deepcopy(bundle["rows"][-1]))
        bundle["sha256"] = digest(bundle["rows"])
    assert extract(report, [current]) == {}


def test_internal_missing_session_still_blocks_reference_direction():
    report, current, bundle = example()
    bundle["rows"].pop(-3)
    bundle["sha256"] = digest(bundle["rows"])
    attach(report, [current])
    assert report["states"]["display_pressure"]["direction"] == "UNKNOWN"


def test_stale_capture_does_not_reactivate_reference():
    report, current, _ = example()
    report["source_audit"]["db_temporal_state_daily"]["status"] = "CACHED_AFTER_ERROR"
    assert extract(report, [current]) == {}


def test_next_day_live_snapshot_extends_reference_without_replaying_it_as_live():
    report, current, _ = example()
    later = deepcopy(current)
    later["report_date"] = later["source_date"] = "2026-10-20"
    later["risk_value"] = 72
    later["details"].pop("history_reference")
    report.update(plan("2026-10-21"))
    report["temporal"]["environment"].append(later)
    report["states"] = build_states(report)
    attach(report, [current, later])
    assert report["states"]["display_pressure"]["changes"]["5d"] == 5
    assert later["observation_mode"] == "LIVE_SHADOW"


def test_dynamic_breadth_is_separate_from_fixed_cohort():
    p = plan("2026-10-20")
    days = sessions(date.fromisoformat(p["market_date"]) - timedelta(days=40), p["market_date"])[-22:]
    rows = [{"record_date": d, "ingested_at": d + "T21:00:00Z", "full_metrics": {
        "pct_20ma": 30 + i, "breadth_sample": "top500_by_mktcap", "breadth_sample_size": 500}}
        for i, d in enumerate(days)]
    m = {"pct_20ma": {"value": 51, "status": "OK", "acquisition": "OK", "source_date": days[-1]}}
    capture = {"status": "OK", "captured_at": "2026-10-20T12:00:00Z", "data": rows}
    panel = build_internals(p, capture, m)
    assert panel["persistence"]["state"] == "UNKNOWN"
    assert panel["display_persistence"]["changes"] == {"5d": 5, "21d": 21}
    assert panel["display_persistence"]["basis"] == "DYNAMIC_TOP500_REFERENCE"
    assert panel["breadth"]["state"] == "UNKNOWN"
    rows[-3]["full_metrics"]["breadth_sample_size"] = 400
    panel = build_internals(p, capture, m)
    assert panel["reference_persistence"]["changes"]["5d"] is None
    assert panel["reference_persistence"]["state"] == "UNKNOWN"


def test_archive_alias_requires_verified_original_value_and_never_mutates_source():
    frame = pd.DataFrame([
        dict(report_date="2026-10-20", metric="pct_20ma", scope="MACRO", source_name="market_history", calc_version="v1", value=50),
        dict(report_date="2026-10-21", metric="pct_20ma", scope="MACRO", source_name="archived_auto_review_log / " + "a" * 64, calc_version="v1", value=55)])
    rows = [{"record_date": "2026-10-21", "full_metrics": {"breadth_log_recovery": {
        "basis": "original_500_stock_snapshot_not_recomputed", "archive_sha256": "a" * 64,
        "source_date": "2026-10-21", "values": {"pct_20ma": 55}}}}]
    original = frame.copy()
    rebuilt, aliases = restore_breadth_lineage(frame, rows)
    assert rebuilt.iloc[1].source_name == "market_history"
    assert aliases[0]["point_in_time_eligible"] is False
    pd.testing.assert_frame_equal(frame, original)
    rows[0]["full_metrics"]["breadth_log_recovery"]["values"]["pct_20ma"] = 0
    assert restore_breadth_lineage(frame, rows)[1] == []
