import pandas as pd
import pytest

from environment_indices import CORE_INDEX_KEYS, OPTION_UNIVERSE, composite_score, score_frame_details
from temporal_monitor import build_temporal_history, comparison_segments


def metric(name, pct, samples=252, lag=0, scope="MACRO"):
    return dict(metric=name, scope=scope, percentile=pct, effective_obs_count=samples,
                lag_days=lag, report_date="2026-10-23", source_date="2026-10-23")


def test_quality_changes_do_not_change_fixed_weight_score():
    first = [metric("vix", 90), metric("vvix", 10), metric("move", 20), metric("vix_ratio_contango_pct", 80)]
    second = [dict(r, effective_obs_count=60 if r["metric"] == "vix" else 252, lag_days=1) for r in first]
    a, _, qa, _ = score_frame_details(pd.DataFrame(first))
    b, _, qb, _ = score_frame_details(pd.DataFrame(second))
    assert a["idx_risk_pressure"] == b["idx_risk_pressure"]
    assert qa != qb
    assert score_frame_details(pd.DataFrame(first[:-1]))[0]["idx_risk_pressure"] is None


def test_composite_has_three_fixed_equal_weight_panels():
    scores = dict(zip(CORE_INDEX_KEYS, (20, 40, 60)))
    coverage = dict.fromkeys(CORE_INDEX_KEYS, 1)
    assert composite_score(scores, coverage) == 40
    scores["idx_options_fragility"] = 100
    assert composite_score(scores, coverage) == 40
    scores[CORE_INDEX_KEYS[0]] = None
    assert composite_score(scores, coverage) is None


def test_option_stock_universe_is_fixed_and_partial_observations_remain_visible():
    rows = [metric(name, 70, scope=ticker) for name in ("ivr_pct", "expected_move_pct", "short_gamma_m")
            for ticker in OPTION_UNIVERSE]
    assert score_frame_details(pd.DataFrame(rows))[0]["idx_options_fragility"] is not None
    result, _, _, diagnostics = score_frame_details(pd.DataFrame(rows[:-1]))
    assert result["idx_options_fragility"] is None
    assert diagnostics["idx_options_fragility"][-1]["status"] == "INCOMPLETE_UNIVERSE"
    assert diagnostics["idx_options_fragility"][-1]["diagnostic_value"] is not None


@pytest.mark.parametrize("change", [{"lag_days": None}, {"source_date": None},
                                  {"source_date": "2026-10-26"}, {"lag_days": 100}])
def test_unknown_freshness_does_not_default_to_current(change):
    rows = [metric("credit_spread", 50), {**metric("nfci", 60), **change}]
    assert score_frame_details(pd.DataFrame(rows))[0]["idx_liquidity_stress"] is None


def test_temporary_membership_change_resets_trends_even_after_restoration():
    rows = []
    for i, day in enumerate(pd.bdate_range("2026-10-05", periods=9)):
        rows.append(dict(report_date=str(day.date()), score=50+i, state="MIXED", coverage=1, confidence=1,
                         calc_version="risk_v1", layer_details={"_meta": {"comparison_signature": "changed" if i == 7 else "original"}}))
    latest = build_temporal_history({"risk_capital": rows})[-1]
    assert latest["change_1d"] is None
    assert latest["change_5d"] is None
    assert latest["details"]["comparison_reset"]
    segments = comparison_segments(build_temporal_history({"risk_capital": rows}))
    assert [len(s) for s in segments] == [7, 1, 1]


def test_legacy_partial_risk_basket_without_members_is_not_comparable():
    history = build_temporal_history({"risk_capital": [dict(
        report_date="2026-10-23", score=65, state="MIXED", coverage=.875, confidence=.875)]})
    assert history[0]["risk_value"] is None
    assert history[0]["quality_status"] == "INSUFFICIENT"
