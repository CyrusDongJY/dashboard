from copy import deepcopy
from datetime import date, timedelta

import pytest

from liquidity_digest.domains import METRICS, GROUPS, build_domains, metric_evidence, _combine
from liquidity_digest.model import assemble, plan, DB_METRICS, native_sessions
from liquidity_digest.sample import sample_report
from liquidity_digest.render import email_html, email_text, render_pdf


@pytest.fixture
def report():
    return sample_report(plan("2026-10-20"))


def shift_window(report, key, delta, count=5):
    item = report["metrics"][key]
    end = item["value"]
    points = item["history"]
    if item["frequency"] != "weekly":
        grid = native_sessions(key, date.fromisoformat(item["source_date"]) - timedelta(days=70), item["source_date"])
        points = [p for p in points if p["date"] in grid]
    for i, point in enumerate(points[-count - 1:]):
        point["value"] = end - delta + delta * i / count


def test_macro_relief_can_coexist_with_weaker_market_internals(report):
    result = build_domains(report)
    assert result["macro"]["short"]["direction"] == "DOWN"
    assert result["micro"]["short"]["direction"] == "UP"
    assert "尚未看到" in result["relation"]
    assert result["macro"]["flags"]
    assert result["micro"]["flags"]
    assert "composite" not in result["macro"]
    assert "score" not in result["micro"]


@pytest.mark.parametrize("directions,expected", [
    (["DOWN", "DOWN", "DOWN"], "DOWN"),
    (["DOWN", "DOWN", "FLAT"], "DOWN"),
    (["DOWN", "FLAT", "FLAT"], "EARLY_DOWN"),
    (["UP", "FLAT", "FLAT"], "EARLY_UP"),
    (["UP", "UP", "FLAT"], "UP"),
    (["UP", "DOWN", "DOWN"], "MIXED"),
    (["MIXED", "DOWN", "DOWN"], "MIXED"),
    (["FLAT", "FLAT", "FLAT"], "FLAT"),
    (["UNKNOWN", "DOWN", "DOWN"], "UNKNOWN"),
    ([], "UNKNOWN"),
])
def test_family_consensus_cannot_hide_disagreement_or_missing(directions, expected):
    assert _combine(directions, domain=True) == expected


def test_no_shared_primary_inputs_between_macro_and_micro():
    keys = {domain: {k for _, _, inputs, _ in groups for k in inputs}
            for domain, groups in GROUPS.items()}
    assert not keys["macro"] & keys["micro"]
    assert not ({"vix", "vix_ratio_contango_pct", "dxy", "usdjpy_return_5d_pct",
                 "spy_rsp_spread_pct", "ccc"} & set(METRICS))
    assert {"pct_20ma", "pct_50ma"} <= set(DB_METRICS)


def test_mixed_legacy_scores_are_not_macro_or_micro_votes(report):
    before = build_domains(report)
    for key in ("environment", "liquidity", "tactical_stress"):
        report["states"]["panels"][key].update(risk_value=100, direction="UP")
    assert build_domains(report) == before


def test_net_quantity_and_funding_price_have_opposite_polarity(report):
    shift_window(report, "net_liquidity", 10, count=1)
    assert metric_evidence(report, "net_liquidity")["short"]["direction"] == "DOWN"
    shift_window(report, "sofr_iorb", 2)
    assert metric_evidence(report, "sofr_iorb")["short"]["direction"] == "UP"


@pytest.mark.parametrize("key", ["ig", "pct_20ma", "sofr_iorb"])
@pytest.mark.parametrize("field,value", [("status", "LAGGED"), ("acquisition", "CACHED_AFTER_ERROR")])
def test_unavailable_metric_is_not_flat_or_a_vote(report, key, field, value):
    report["metrics"][key][field] = value
    evidence = metric_evidence(report, key)
    assert evidence["short"]["direction"] == "UNKNOWN"
    domain = "micro" if key == "pct_20ma" else "macro"
    assert build_domains(report)[domain]["short"]["direction"] == "UNKNOWN"


def test_internal_calendar_gap_breaks_both_daily_direction_windows(report):
    report["metrics"]["hy"]["history"].pop(-3)
    evidence = metric_evidence(report, "hy")
    assert evidence["short"]["delta"] is None
    assert evidence["medium"]["delta"] is None


def test_missing_native_week_does_not_fall_back_to_previous_row(report):
    report["metrics"]["reserves"]["history"].pop(-2)
    assert metric_evidence(report, "reserves")["short"]["direction"] == "UNKNOWN"


def test_weekly_comparison_keeps_native_dates_and_window(report):
    evidence = metric_evidence(report, "net_liquidity")
    assert evidence["short"]["end"] != report["market_date"]
    assert evidence["short"]["start"] == "2026-10-07"
    assert evidence["short"]["end"] == "2026-10-14"
    assert evidence["medium"]["start"] == "2026-09-16"


@pytest.mark.parametrize("delta,expected", [(4.9999, "FLAT"), (5, "UP"), (-5, "DOWN")])
def test_new_shadow_noise_floor_is_explicit(report, delta, expected):
    shift_window(report, "ig", delta)
    item = metric_evidence(report, "ig")
    assert item["short"]["direction"] == expected
    assert item["short"]["threshold"] == 5


def test_same_family_correlations_do_not_become_extra_votes(report):
    for key in ("net_liquidity", "reserves"):
        shift_window(report, key, 0, count=4)
    for key in ("ig", "hy"):
        shift_window(report, key, 0, count=21)
    # All three funding spreads fall, but this is still only one macro family.
    domain = build_domains(report)["macro"]
    assert domain["short"]["direction"] == "EARLY_DOWN"


def test_optional_ccc_lag_does_not_replace_or_invalidate_primary_credit_pair(report):
    report["metrics"]["ccc"]["status"] = "STALE"
    assert build_domains(report)["macro"]["short"]["direction"] == "DOWN"
    report["metrics"]["ig"]["status"] = "STALE"
    report["metrics"]["ccc"]["status"] = "OK"
    assert build_domains(report)["macro"]["short"]["direction"] == "UNKNOWN"


def test_invalid_percentage_or_mismatched_current_history_is_not_evidence(report):
    item = report["metrics"]["pct_20ma"]
    item["value"] = item["history"][-1]["value"] = 101
    assert metric_evidence(report, "pct_20ma")["short"]["direction"] == "UNKNOWN"
    report["metrics"]["hy"]["value"] += 1
    assert metric_evidence(report, "hy")["short"]["direction"] == "UNKNOWN"


def test_raw_guard_bps_conversion_and_relief_remains_distinct(report):
    report["metrics"]["hy"]["value"] = 501
    report["metrics"]["hy"]["history"][-1]["value"] = 501
    flags = build_domains(report)["macro"]["flags"]
    assert any("高收益" in f for f in flags)
    report["metrics"]["hy"]["value"] = 499
    assert not any("高收益" in f for f in build_domains(report)["macro"]["flags"])


def test_all_missing_is_unknown_in_both_domains_and_email():
    report = assemble(plan("2026-10-20"), {})
    assert report["domains"]["macro"]["short"]["direction"] == "UNKNOWN"
    assert report["domains"]["micro"]["short"]["direction"] == "UNKNOWN"
    assert "证据不足" in email_html(report)
    assert "暂停两层联判" in email_text(report)


def test_model_version_break_cannot_be_repaired_by_old_observations():
    p = plan("2026-10-20")
    rows = [{"metric": "pct_20ma", "scope": "MACRO", "session": "EOD",
             "is_final": True, "is_filled": False, "report_date": d, "source_date": d,
             "value": 60, "calc_version": version}
            for d, version in (("2026-10-12", "old"), ("2026-10-19", "new"))]
    report = assemble(p, {"db_metric_daily": {"status": "OK", "data": rows}})
    assert metric_evidence(report, "pct_20ma")["short"]["direction"] == "UNKNOWN"


def test_new_classification_does_not_mutate_input_or_existing_scores(report):
    before = deepcopy(report)
    build_domains(report)
    assert report == before


def test_short_and_medium_disagreement_is_explicit(report):
    for key in ("ig", "hy"):
        # Medium-term pressure rises while the recent week narrows.
        shift_window(report, key, 100, count=21)
        shift_window(report, key, -20, count=5)
    for key in ("sofr_iorb", "effr_iorb", "sofr_tail"):
        shift_window(report, key, 20, count=21)
        shift_window(report, key, -5, count=5)
    for key in ("net_liquidity", "reserves"):
        shift_window(report, key, -30, count=4)
        shift_window(report, key, 10, count=1)
    macro = build_domains(report)["macro"]
    assert macro["short"]["direction"] == "DOWN"
    assert macro["medium"]["direction"] == "UP"
    assert macro["disagreement"]


def test_float_roundoff_does_not_create_false_missing_data(report):
    report["metrics"]["sofr_tail"]["history"][-1]["value"] += 1e-12
    assert metric_evidence(report, "sofr_tail")["short"]["direction"] == "DOWN"


def test_pdf_retains_tactical_and_distinguishes_three_layers(report, tmp_path):
    from pypdf import PdfReader
    path = tmp_path / "domains.pdf"
    render_pdf(report, path)
    pages = PdfReader(path).pages
    assert len(pages) == 3
    text = "\n".join(page.extract_text() for page in pages)
    for label in ("宏观资金与融资", "微观市场承接", "跨资产确认", "非纯微观",
                  "边际趋松", "内部压力增加", "综合流动性支持", "非纯宏观"):
        assert label in text
