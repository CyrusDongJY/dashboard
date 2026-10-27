from copy import deepcopy
from datetime import date, timedelta

import pytest

from liquidity_digest.model import assemble, plan, sessions
from liquidity_digest.states import build_states, panel_state, STATE_VERSION
from liquidity_digest.render import email_html, email_text


@pytest.fixture
def report():
    p = plan("2026-10-20")
    days = sessions(date.fromisoformat(p["market_date"]) - timedelta(days=70), p["market_date"])[-22:]
    temporal = {}
    for key in ("liquidity", "environment", "tactical_stress", "risk_capital"):
        temporal[key] = [{
            "report_date": d, "source_date": d, "panel": key, "risk_value": 50,
            "coverage": .95, "confidence": .95, "observation_mode": "LIVE_SHADOW",
            "quality_status": "OK", "calc_version": "temporal_v1",
            "details": {"source_calc_version": key + "_v1"},
        } for d in days]
    return {**p, "temporal": temporal,
            "source_audit": {"db_temporal_state_daily": {"status": "OK"}}}


def tail(report, key, start, end):
    for i, row in enumerate(report["temporal"][key][-6:]):
        row["risk_value"] = start + (end - start) * i / 5


def test_funding_polarity_is_inverted_but_risk_delta_is_not(report):
    tail(report, "liquidity", 60, 50)
    state = panel_state(report, "liquidity")
    assert state["direction_cn"] == "趋于放松"
    assert state["changes"]["5d"] == -10
    assert state["display_value"] == 50
    tail(report, "liquidity", 50, 60)
    assert panel_state(report, "liquidity")["direction_cn"] == "趋于收紧"


def test_high_and_falling_is_not_reported_as_low_risk(report):
    tail(report, "environment", 82, 72)
    states = build_states(report)
    assert states["pressure"]["zone"] == "HIGH"
    assert states["pressure"]["direction"] == "DOWN"
    assert "仍在高压区" in states["pressure_note"]
    assert any(a["kind"] == "pressure" for a in states["alerts"])


def test_low_but_rising_triggers_watch(report):
    tail(report, "environment", 15, 25)
    states = build_states(report)
    assert states["pressure"]["zone"] == "LOW"
    assert states["pressure"]["direction"] == "UP"
    assert any(a["kind"] == "watch" for a in states["alerts"])


@pytest.mark.parametrize("end,expected", [(54.99, "FLAT"), (55, "UP"), (45, "DOWN"), (45.01, "FLAT")])
def test_direction_boundary(report, end, expected):
    tail(report, "environment", 50, end)
    assert panel_state(report, "environment")["direction"] == expected


@pytest.mark.parametrize("level,zone", [(35, "LOW"), (35.01, "MID"), (64.99, "MID"), (65, "HIGH")])
def test_level_boundaries(report, level, zone):
    report["temporal"]["environment"][-1]["risk_value"] = level
    assert panel_state(report, "environment")["zone"] == zone


@pytest.mark.parametrize("patch", [
    {"risk_value": -1}, {"risk_value": 101}, {"risk_value": None},
    {"risk_value": float("nan")}, {"coverage": .1}, {"confidence": .74},
    {"confidence": None}, {"quality_status": "PARTIAL"}, {"observation_mode": "REPLAY"},
    {"source_date": "2026-10-16"}, {"calc_version": None}, {"details": {}},
])
def test_bad_current_observation_does_not_become_flat(report, patch):
    report["temporal"]["liquidity"][-1].update(patch)
    state = panel_state(report, "liquidity")
    assert state["direction"] == "UNKNOWN"
    assert state["risk_value"] is None


def test_old_cache_cannot_drive_current_state(report):
    report["source_audit"]["db_temporal_state_daily"]["status"] = "CACHED_AFTER_ERROR"
    assert panel_state(report, "environment")["zone"] == "UNKNOWN"


def test_missing_internal_trading_day_breaks_direction_window(report):
    tail(report, "environment", 50, 65)
    report["temporal"]["environment"].pop(-3)
    state = panel_state(report, "environment")
    assert state["zone"] == "HIGH"
    assert state["direction"] == "UNKNOWN"
    assert state["changes"]["5d"] is None


def test_underlying_rule_version_change_breaks_history(report):
    report["temporal"]["environment"][-1]["details"]["source_calc_version"] = "environment_v2"
    assert panel_state(report, "environment")["changes"]["5d"] is None


def test_observation_streak_counts_real_sessions_and_stops_at_gap(report):
    for row in report["temporal"]["environment"][-5:]:
        row["risk_value"] = 70
    assert panel_state(report, "environment")["high_zone_days"] == 5
    report["temporal"]["environment"].pop(-3)
    assert panel_state(report, "environment")["high_zone_days"] == 2


def test_pressure_fallback_is_explicit_and_not_averaged(report):
    report["temporal"]["environment"] = []
    tail(report, "tactical_stress", 60, 70)
    states = build_states(report)
    assert states["pressure"]["panel"] == "tactical_stress"
    assert states["pressure"]["risk_value"] == 70
    assert any("代理" in a["text"] for a in states["alerts"])


def test_divergences_are_not_erased_by_a_total_score(report):
    tail(report, "liquidity", 60, 50)
    tail(report, "environment", 50, 60)
    tail(report, "tactical_stress", 60, 50)
    states = build_states(report)
    assert sum(a["kind"] == "divergence" for a in states["alerts"]) >= 2
    assert "composite" not in states
    assert states["pressure"]["risk_value"] == 60


@pytest.mark.parametrize("panel", ["environment", "liquidity"])
def test_acute_move_is_reported_even_if_5d_change_is_flat(report, panel):
    report["temporal"][panel][-2]["risk_value"] = 35
    states = build_states(report)
    assert states["panels"][panel]["direction"] == "FLAT"
    assert any(a["kind"] == "shock" for a in states["alerts"])


def test_all_missing_is_gray_and_has_no_gauge_pointer():
    full = assemble(plan("2026-10-20"), {})
    assert full["states"]["funding"]["display_value"] is None
    assert full["states"]["pressure"]["zone"] == "UNKNOWN"
    assert full["states"]["version"] == STATE_VERSION
    assert "方向待确认" in email_html(full)
    assert "缺数不等于平稳" in email_text(full)


def test_rule_evaluation_never_modifies_inputs(report):
    before = deepcopy(report)
    build_states(report)
    assert report == before


def test_morning_funding_watch_does_not_reclassify_eod_state(report):
    tail(report, "liquidity", 60, 50)
    report["metrics"] = {"sofr_iorb": {
        "value": 7, "source_date": report["market_date"], "status": "OK", "acquisition": "OK"}}
    states = build_states(report)
    assert states["funding"]["direction_cn"] == "趋于放松"
    watch = next(a for a in states["alerts"] if a["kind"] == "raw_guard")
    assert watch["evidence"][0]["threshold_bps"] == 5
    report["metrics"]["sofr_iorb"]["status"] = "LAGGED"
    assert all(a["kind"] != "raw_guard" for a in build_states(report)["alerts"])


def test_shock_warning_is_not_hidden_by_other_warnings(report):
    tail(report, "environment", 60, 75)
    report["temporal"]["environment"][-2]["risk_value"] = 50
    report["temporal"]["liquidity"] = []
    report["metrics"] = {"sofr_iorb": {
        "value": 7, "source_date": report["market_date"], "status": "OK", "acquisition": "OK"}}
    states = build_states(report)
    assert states["alerts"][0]["kind"] == "shock"
    assert len(states["alerts"]) >= 4


def test_assembly_does_not_join_different_underlying_score_versions(report):
    from liquidity_digest.model import assemble
    rows = report["temporal"]["environment"]
    rows[-1]["details"]["source_calc_version"] = "environment_v2"
    full = assemble(plan(report["report_date"]), {"db_temporal_state_daily": {
        "status": "OK", "data": rows}})
    assert len(full["temporal"]["environment"]) == 1
    assert full["states"]["pressure"]["risk_value"] == 50
    assert full["states"]["pressure"]["direction"] == "UNKNOWN"


def test_multiple_warning_pdf_preserves_five_page_layout(tmp_path):
    from pypdf import PdfReader
    from liquidity_digest.render import render_pdf
    from liquidity_digest.sample import sample_report
    full = sample_report(plan("2026-10-20"))
    full["temporal"]["liquidity"] = []
    full["temporal"]["environment"][-2]["risk_value"] = 50
    full["metrics"]["sofr_iorb"]["value"] = 7
    full["states"] = build_states(full)
    from liquidity_digest.domains import build_domains
    full["domains"] = build_domains(full)
    from liquidity_digest.frontpage import build_frontpage
    full["frontpage"] = build_frontpage(full, {})
    path = tmp_path / "warnings.pdf"
    render_pdf(full, path)
    reader = PdfReader(path)
    assert len(reader.pages) == 6
    assert "单日突变" in reader.pages[0].extract_text()
    assert "证据不足" in reader.pages[0].extract_text()
    assert "方向待确认" in reader.pages[4].extract_text()
