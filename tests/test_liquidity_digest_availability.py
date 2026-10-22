from copy import deepcopy

import pytest

from liquidity_digest.model import (assemble, plan, native_sessions, summarize,
                                    expected_daily_date)
from liquidity_digest.domains import metric_evidence
from liquidity_digest.sample import sample_report
from liquidity_digest.states import panel_state, matrix_note
from liquidity_digest.breadth_recovery import extract_snapshot, merge_snapshot


LOG = """开始执行云端自动化投研流
[20MA占比]: 71.6
[50MA占比]: 57.6
[200MA占比]: 57.4
[前500大市值样本TRIN]: 1.06 (NYSE+Nasdaq, 样本数=500, 截面=2026-10-21T16:30:33-04:00, 收盘竞价=UNVERIFIED)
"""


def test_archive_recovery_only_restores_exact_breadth_observations():
    snap = extract_snapshot(LOG, "2026-10-21")
    old = {"record_date": "2026-10-21", "macro_score": None, "eod_stress_score": None,
           "vix": 16.68, "pct_20ma": None, "full_metrics": {}}
    before = deepcopy(old)
    row, fields = merge_snapshot(old, snap, "verified_digest", "2026-10-22T15:00:00+00:00")
    assert old == before
    assert row["pct_20ma"] == 71.6 and len(fields) == 3
    assert row["macro_score"] is None and row["eod_stress_score"] is None
    assert row["vix"] == 16.68
    meta = row["full_metrics"]["breadth_log_recovery"]
    assert meta["point_in_time_eligible"] is False
    assert meta["original_captured_at"].startswith("2026-10-21")
    assert merge_snapshot(row, snap, "verified_digest", "later")[1] == []
    row["pct_20ma"] = 50
    with pytest.raises(ValueError, match="CONFLICT"):
        merge_snapshot(row, snap, "verified_digest", "later")


@pytest.mark.parametrize("bad", [LOG + LOG, LOG.replace("500", "400"),
    LOG.replace("16:30", "09:30"), LOG.replace("71.6", "101"),
    LOG.replace("[20MA占比]: 71.6", "[20MA占比]: -"),
    LOG.replace("2026-10-21", "2026-10-22")])
def test_archive_recovery_rejects_ambiguous_or_unqualified_snapshots(bad):
    with pytest.raises(ValueError):
        extract_snapshot(bad, "2026-10-21")


def test_bond_holiday_is_not_a_funding_data_gap():
    p = plan("2026-10-22")
    days = native_sessions("sofr_iorb", "2026-08-01", p["market_date"])
    assert "2026-10-12" not in days
    m = summarize("sofr_iorb", [{"date": d, "value": -2} for d in days], p)
    evidence = metric_evidence({**p, "metrics": {"sofr_iorb": m}}, "sofr_iorb")
    assert evidence["medium"]["direction"] == "FLAT"
    m["history"].pop(-3)
    assert metric_evidence({**p, "metrics": {"sofr_iorb": m}}, "sofr_iorb")["medium"]["direction"] == "UNKNOWN"


def test_sofr_and_effr_use_different_july_holiday_calendars():
    assert "2026-07-03" not in native_sessions("sofr", "2026-07-01", "2026-07-06")
    assert "2026-07-03" in native_sessions("effr", "2026-07-01", "2026-07-06")
    p = plan("2026-07-06")
    assert expected_daily_date("sofr", p) == "2026-07-02"
    assert expected_daily_date("effr", p) == "2026-07-03"
    m = summarize("effr", [{"date": "2026-07-03", "value": 3.8}], p)
    assert m["status"] == "OK" and m["source_date"] == "2026-07-03"
    assert "2026-04-03" in native_sessions("sofr", "2026-04-01", "2026-04-06")
    assert "2026-11-11" not in native_sessions("effr", "2026-11-09", "2026-11-12")


def test_tga_expected_lag_is_not_a_fetch_failure_or_blanket_grace_period():
    p = plan("2026-10-22")
    m = summarize("tga_daily", [{"date": "2026-10-20", "value": 970}], p)
    assert m["status"] == "OK" and m["expected_date"] == "2026-10-20"
    assert "发布节奏" in m["publication_note"]
    assert summarize("tga_daily", [{"date": "2026-10-19", "value": 970}], p)["status"] == "LAGGED"
    assert expected_daily_date("tga_daily", plan("2026-10-13")) == "2026-10-08"


def test_credit_asof_is_bounded_and_keeps_original_observation_dates():
    p = plan("2026-10-22")
    days = native_sessions("ig", "2026-08-01", "2026-10-20")
    m = summarize("ig", [{"date": d, "value": 80} for d in days], p)
    assert m["status"] == "AS_OF" and m["source_date"] == "2026-10-20"
    r = {**p, "metrics": {"ig": m}}
    e = metric_evidence(r, "ig")["short"]
    assert e["direction"] == "FLAT" and e["as_of_only"]
    assert e["end"] == "2026-10-20"
    stale = summarize("ig", m["history"][:-1], p)
    assert stale["status"] == "LAGGED"
    m["acquisition"] = "CACHED_AFTER_ERROR"
    assert metric_evidence(r, "ig")["short"]["direction"] == "UNKNOWN"


@pytest.mark.parametrize("key", ["pct_20ma", "vix", "dxy", "sofr_iorb"])
def test_market_and_funding_missing_values_never_receive_credit_grace(key):
    m = summarize(key, [{"date": "2026-10-20", "value": 10}], plan("2026-10-22"))
    assert m["status"] == "LAGGED"


def test_known_level_is_retained_when_same_version_window_is_short():
    r = sample_report(plan("2026-10-22"))
    r["temporal"]["environment"] = r["temporal"]["environment"][-3:]
    state = panel_state(r, "environment")
    assert state["zone"] == "HIGH" and state["direction"] == "UNKNOWN"
    assert state["windows"]["5d"]["available"] == 3
    assert "3/6" in state["reason"] and "3/6" in matrix_note(state)
    assert "无法定位" not in matrix_note(state)


def test_missing_current_observation_still_has_unknown_level():
    r = sample_report(plan("2026-10-22"))
    r["temporal"]["environment"].pop()
    assert "无法定位" in matrix_note(panel_state(r, "environment"))


def raw_capture():
    return {"db_market_history": {"status": "OK", "data": [{
        "record_date": "2026-10-21", "vix": 16.68,
        "ingested_at": "2026-10-22T08:00:00-04:00",
        "full_metrics": {"market_metric_lineage": {"vix": {
            "source_date": "2026-10-21", "source_name": "yfinance:^VIX",
            "reconstructed_at": "2026-10-22T08:00:00-04:00", "point_in_time_eligible": False}}}}]}}


def test_dated_raw_vix_can_fill_missing_snapshot_but_not_score():
    r = assemble(plan("2026-10-22"), raw_capture())
    assert r["metrics"]["vix"]["value"] == 16.68
    assert r["metrics"]["vix"]["status"] == "OK"
    assert r["metrics"]["vix"]["raw_fallback"]["lineage"]["point_in_time_eligible"] is False
    assert r["states"]["panels"]["tactical_stress"]["risk_value"] is None


@pytest.mark.parametrize("case", ["late_ingest", "late_reconstruction", "wrong_date", "no_lineage", "fetch_error", "no_time"])
def test_raw_fallback_requires_original_date_and_real_availability(case):
    data = raw_capture(); row = data["db_market_history"]["data"][0]
    lineage = row["full_metrics"]["market_metric_lineage"]["vix"]
    if case == "late_ingest": row["ingested_at"] = "2026-10-22T10:00:00-04:00"
    if case == "late_reconstruction": lineage["reconstructed_at"] = "2026-10-22T10:00:00-04:00"
    if case == "wrong_date": lineage["source_date"] = "2026-10-20"
    if case == "no_lineage": row["full_metrics"] = {}
    if case == "fetch_error": data["db_market_history"]["status"] = "FETCH_ERROR"
    if case == "no_time": row["ingested_at"] = None
    assert assemble(plan("2026-10-22"), data)["metrics"]["vix"]["value"] is None
