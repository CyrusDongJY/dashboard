import math
import unittest
from datetime import date

import pandas as pd

from pre_market_metrics import (
    assess_expected_move_context,
    apply_gamma_quality_gate,
    distance_pct,
    expected_move_metrics,
    format_premarket_quality_summary,
    format_premarket_symbol_summary,
    gamma_structure,
    max_oi_metrics,
    put_call_oi_ratio,
    quote_midpoint,
    select_expirations,
    standard_monthly_oi_walls,
    validate_iv_skew,
)


def option_row(
        expiration, strike, right, oi, iv=0.25, years=30 / 365,
        dte=30, mid=None, delta=None, quote_age=None, market_data_type=None,
        quote_as_of=None):
    return {
        "Exp": expiration,
        "S": float(strike),
        "R": right,
        "OI": float(oi),
        "IV": iv,
        "T": years,
        "DTE": dte,
        "Mid": mid,
        "Delta": delta,
        "QuoteAgeSeconds": quote_age,
        "MarketDataType": market_data_type,
        "QuoteAsOf": quote_as_of,
    }


class QuoteTests(unittest.TestCase):
    def test_midpoint_requires_two_sided_valid_market(self):
        self.assertEqual(quote_midpoint(9.8, 10.2), 10.0)
        self.assertIsNone(quote_midpoint(None, 10.2))
        self.assertIsNone(quote_midpoint(10.2, 9.8))


class ExpectedMoveTests(unittest.TestCase):
    def test_atm_straddle_mid_has_priority(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, mid=3.2),
            option_row("20260815", 100, "P", 100, mid=2.8),
        ])
        result = expected_move_metrics(frame, 100, "20260815")
        self.assertEqual(result["source"], "ATM_STRADDLE_MID")
        self.assertEqual(result["quality"], "OK")
        self.assertAlmostEqual(result["value"], 6.0)
        self.assertAlmostEqual(result["pct"], 6.0)

    def test_iv_fallback_uses_square_root_of_time(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, iv=0.30),
            option_row("20260815", 100, "P", 100, iv=0.30),
        ])
        result = expected_move_metrics(frame, 100, "20260815")
        expected = 100 * 0.30 * math.sqrt(30 / 365)
        self.assertEqual(result["source"], "ATM_IV_FALLBACK")
        self.assertEqual(result["quality"], "FALLBACK")
        self.assertAlmostEqual(result["value"], expected)

    def test_missing_expected_move_is_none_not_zero(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, iv=None, mid=None),
            option_row("20260815", 100, "P", 100, iv=None, mid=None),
        ])
        result = expected_move_metrics(frame, 100, "20260815")
        self.assertEqual(result["quality"], "MISSING")
        self.assertIsNone(result["value"])
        self.assertIsNone(result["pct"])

    def test_adjacent_strikes_interpolate_iv_before_stale_fallback(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, iv=None),
            option_row("20260815", 100, "P", 100, iv=None),
            option_row("20260815", 95, "C", 100, iv=0.20),
            option_row("20260815", 105, "P", 100, iv=0.30),
        ])
        result = expected_move_metrics(frame, 100, "20260815", previous_iv=0.40)
        self.assertEqual(result['source'], 'ADJACENT_IV_INTERPOLATION')
        self.assertAlmostEqual(result['iv'], 0.25)

    def test_previous_day_iv_is_stale_not_missing_or_zero(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, iv=None),
            option_row("20260815", 100, "P", 100, iv=None),
        ])
        result = expected_move_metrics(
            frame, 100, "20260815", previous_iv=0.28,
            previous_iv_date='2026-07-27')
        self.assertEqual(result['source'], 'PREVIOUS_DAY_IV')
        self.assertEqual(result['quality'], 'STALE')
        self.assertEqual(result['source_date'], '2026-07-27')

    def test_previous_expected_move_iv_proxy_keeps_explicit_lineage(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, iv=None),
            option_row("20260815", 100, "P", 100, iv=None),
        ])
        result = expected_move_metrics(
            frame, 100, "20260815", previous_iv=0.27,
            previous_iv_date='2026-07-27',
            previous_iv_source='PREVIOUS_DAY_EXPECTED_MOVE_IV_PROXY')
        self.assertEqual(
            result['source'], 'PREVIOUS_DAY_EXPECTED_MOVE_IV_PROXY')

    def test_frozen_expected_move_is_raw_only_not_decision_eligible(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "C", 100, mid=3.2,
                       quote_age=600, market_data_type=4,
                       quote_as_of="2026-08-04T20:00:00Z"),
            option_row("20260815", 100, "P", 100, mid=2.8,
                       quote_age=600, market_data_type=4,
                       quote_as_of="2026-08-04T20:00:00Z"),
        ])
        raw = expected_move_metrics(frame, 100, "20260815")
        result = assess_expected_move_context(
            raw, 99, 100, report_date="2026-08-05", event_status="UNKNOWN")
        self.assertFalse(result['decision_eligible'])
        self.assertEqual(result['reliability_score'], 0.0)
        self.assertIn('STALE', result['decision_quality'])
        self.assertIn('FROZEN', result['decision_quality'])

    def test_premarket_gap_consumption_is_scored_separately(self):
        raw = {
            'pct': 1.0, 'quality': 'OK', 'source_date': '2026-08-05',
            'quote_coverage_pct': 100.0, 'quote_max_age_seconds': 30,
            'market_data_types': [1],
        }
        result = assess_expected_move_context(
            raw, 100, 100.8, report_date="2026-08-05",
            event_status="EVENT_DAY")
        self.assertAlmostEqual(result['gap_consumed_pct'], 80.0)
        self.assertIn('PREMARKET_GAP_CONSUMED', result['decision_quality'])
        self.assertIn('EVENT_DAY_REVIEW', result['decision_quality'])
        self.assertFalse(result['decision_eligible'])


class StructureTests(unittest.TestCase):
    def test_distance_is_positive_when_level_is_above_spot(self):
        self.assertAlmostEqual(distance_pct(105, 100), 5.0)
        self.assertAlmostEqual(distance_pct(95, 100), -5.0)

    def test_low_gamma_coverage_suppresses_precise_outputs(self):
        frame = pd.DataFrame([
            option_row("20260720", 100 + index, "C" if index % 2 else "P",
                       1000, years=4 / 365, dte=4)
            for index in range(8)
        ])
        raw = gamma_structure(frame, 100)
        gated = apply_gamma_quality_gate(
            raw,
            {'0DTE': 0, '1-7D': 62, '8-30D': 0, '31-60D': 0, 'ALL': 62},
            {'0DTE': 0, '1-7D': 62, '8-30D': 0, '31-60D': 0, 'ALL': 62},
            {'0DTE': 0, '1-7D': 8, '8-30D': 0, '31-60D': 0, 'ALL': 8},
        )
        self.assertEqual(gated['ALL']['quality'], 'LOW_OI_COVERAGE')
        self.assertIsNone(gated['ALL']['net_gamma_m'])
        self.assertIsNotNone(gated['ALL']['raw_net_gamma_m'])
        self.assertIsNone(gated['call_wall'])

    def test_gamma_gate_checks_atm_side_and_input_oi_coverage(self):
        rows = []
        for strike in range(90, 110):
            rows.extend([
                option_row("20260815", strike, "C", 1000, dte=30),
                option_row("20260815", strike, "P", 1000, dte=30),
            ])
        raw = gamma_structure(pd.DataFrame(rows), 100)
        counts = {'0DTE': 0, '1-7D': 0, '8-30D': 40,
                  '31-60D': 0, 'ALL': 40}
        side_counts = {
            bucket: {'requested_calls': 0, 'requested_puts': 0,
                     'qualified_calls': 0, 'qualified_puts': 0,
                     'oi_calls': 0, 'oi_puts': 0}
            for bucket in counts
        }
        side_counts['8-30D'] = side_counts['ALL'] = {
            'requested_calls': 20, 'requested_puts': 20,
            'qualified_calls': 20, 'qualified_puts': 20,
            'oi_calls': 20, 'oi_puts': 20,
        }
        atm = {bucket: {'requested': 0, 'gamma_valid': 0} for bucket in counts}
        atm['8-30D'] = atm['ALL'] = {'requested': 10, 'gamma_valid': 4}
        weights = {bucket: {'oi_total': 0, 'gamma_input_oi': 0} for bucket in counts}
        weights['8-30D'] = weights['ALL'] = {
            'oi_total': 40_000, 'gamma_input_oi': 40_000}
        ages = {bucket: [] for bucket in counts}
        ages['8-30D'] = ages['ALL'] = [30]
        gated = apply_gamma_quality_gate(
            raw, counts, counts, counts, side_counts=side_counts,
            iv_quote_ages=ages, atm_counts=atm,
            oi_weight_totals=weights)
        self.assertEqual(gated['ALL']['quality'], 'LOW_ATM_COVERAGE')
        self.assertEqual(gated['ALL']['atm_coverage_pct'], 40.0)
        self.assertIsNone(gated['ALL']['gamma_dollar_coverage_pct'])
        self.assertEqual(
            gated['ALL']['gamma_dollar_coverage_status'],
            'UNAVAILABLE_NO_FULL_CHAIN_DENOMINATOR')

    def test_valid_curve_without_cross_has_distinct_flip_status(self):
        rows = []
        for strike in range(90, 110):
            rows.extend([
                option_row("20260815", strike, "C", 1000, dte=30),
                option_row("20260815", strike, "P", 1000, dte=30),
            ])
        raw = gamma_structure(pd.DataFrame(rows), 100)
        gated = apply_gamma_quality_gate(
            raw,
            {'0DTE': 0, '1-7D': 0, '8-30D': 40, '31-60D': 0, 'ALL': 40},
            {'0DTE': 0, '1-7D': 0, '8-30D': 40, '31-60D': 0, 'ALL': 40},
            {'0DTE': 0, '1-7D': 0, '8-30D': 40, '31-60D': 0, 'ALL': 40},
        )
        self.assertEqual(gated['ALL']['quality'], 'OK')
        self.assertEqual(gated['ALL']['flip_quality'], 'VALID_NO_CROSS')
        self.assertIsNone(gated['ALL']['primary_flip'])

    def test_extreme_skew_triggers_quote_review(self):
        self.assertEqual(validate_iv_skew(0.214), 'EXTREME_REVIEW')
        self.assertEqual(
            validate_iv_skew(
                0.05,
                {'Bid': 1.0, 'Ask': 2.0, 'QuoteAgeSeconds': 10},
                {'Bid': 1.0, 'Ask': 1.1, 'QuoteAgeSeconds': 10},
            ),
            'WIDE_MARKET')
    def test_put_call_ratio_never_turns_missing_denominator_into_zero(self):
        frame = pd.DataFrame([
            option_row("20260815", 100, "P", 200),
        ])
        self.assertIsNone(put_call_oi_ratio(frame))

    def test_expiration_sampling_covers_each_horizon(self):
        selected, buckets = select_expirations(
            ["20260716", "20260717", "20260722", "20260731",
             "20260814", "20260828", "20260911", "20261016"],
            date(2026, 7, 16),
            horizon_days=60,
        )
        self.assertIn("20260716", buckets["0DTE"])
        self.assertTrue(buckets["1-7D"])
        self.assertTrue(buckets["8-30D"])
        self.assertTrue(buckets["31-60D"])
        self.assertLessEqual(len(selected), 8)

    def test_gamma_flip_is_curve_crossing_not_minimum_strike_imbalance(self):
        frame = pd.DataFrame([
            option_row("20260815", 110, "C", 2500),
            option_row("20260815", 90, "P", 2500),
        ])
        result = gamma_structure(frame, 100)
        self.assertTrue(result["ALL"]["zero_points"])
        self.assertAlmostEqual(result["ALL"]["primary_flip"], 100, delta=2.0)

    def test_same_wall_is_labeled_pin_candidate_or_breakout_pivot(self):
        frame = pd.DataFrame([
            option_row("20260720", 100, "C", 1200, years=4 / 365, dte=4),
            option_row("20260720", 100, "P", 1000, years=4 / 365, dte=4),
        ])
        result = gamma_structure(frame, 100)
        self.assertEqual(result["call_wall"], 100)
        self.assertEqual(result["put_wall"], 100)
        self.assertEqual(result["pin_strike"], 100)
        self.assertIn(result["pin_state"], ("PIN_CANDIDATE", "BREAKOUT_PIVOT"))

    def test_flat_zero_gamma_curve_does_not_emit_every_grid_point(self):
        frame = pd.DataFrame([
            option_row("20260720", 100, "C", 1000, years=4 / 365, dte=4),
            option_row("20260720", 100, "P", 1000, years=4 / 365, dte=4),
        ])
        result = gamma_structure(frame, 100)
        self.assertEqual(result["ALL"]["zero_points"], [])
        self.assertIsNone(result["ALL"]["primary_flip"])

    def test_max_oi_preserves_expiry_and_risk_fields(self):
        frame = pd.DataFrame([
            option_row("20260720", 100, "C", 500, years=4 / 365, dte=4, delta=0.52),
            option_row("20260815", 105, "P", 2500, years=30 / 365, dte=30, delta=-0.61),
        ])
        result = max_oi_metrics(frame, 100)
        self.assertEqual(result["expiration"], "20260815")
        self.assertEqual(result["oi"], 2500)
        self.assertEqual(result["right"], "P")
        self.assertAlmostEqual(result["delta"], -0.61)
        self.assertIsNotNone(result["gamma_dollar_m"])


class StandardMonthlyWallTests(unittest.TestCase):
    def test_selects_one_monthly_expiry_and_max_oi_by_right(self):
        frame = pd.DataFrame([
            option_row("20260814", 100, "C", 900),
            option_row("20260821", 100, "C", 1200),
            option_row("20260821", 105, "C", 2000),
            option_row("20260821", 95, "P", 2400),
            option_row("20260821", 90, "P", 1000),
            option_row("20260828", 100, "P", 5000),
        ])
        result = standard_monthly_oi_walls(frame, date(2026, 8, 3), 101)
        self.assertEqual(result["quality"], "OK")
        self.assertEqual(result["expiry"], "2026-08-21")
        self.assertEqual(result["call_wall"], 105)
        self.assertEqual(result["put_wall"], 95)
        self.assertEqual(result["call_oi"], 2000)
        self.assertEqual(result["put_oi"], 2400)

    def test_good_friday_uses_preceding_listed_expiry(self):
        frame = pd.DataFrame([
            option_row("20250416", 100, "C", 500),
            option_row("20250417", 105, "C", 1500),
            option_row("20250417", 95, "P", 1700),
            option_row("20250421", 100, "P", 9000),
        ])
        result = standard_monthly_oi_walls(frame, date(2025, 4, 1), 100)
        self.assertEqual(result["quality"], "OK")
        self.assertEqual(result["expiry"], "2025-04-17")

    def test_missing_one_side_is_explicit(self):
        frame = pd.DataFrame([
            option_row("20260821", 105, "C", 1500),
        ])
        result = standard_monthly_oi_walls(frame, date(2026, 8, 3), 100)
        self.assertEqual(result["quality"], "MISSING_PUT")
        self.assertIsNone(result["put_wall"])


class EmailSummaryTests(unittest.TestCase):
    @staticmethod
    def gamma_fixture(low_quality=False):
        result = {
            "call_wall": 105,
            "put_wall": 95,
            "pin_strike": None,
        }
        for bucket, value in (
                ("0DTE", 1.2), ("1-7D", 2.4),
                ("8-30D", -0.8), ("ALL", 2.8)):
            result[bucket] = {
                "net_gamma_m": value,
                "primary_flip": 101 if bucket == "ALL" else None,
                "quality": (
                    "LOW_COVERAGE"
                    if low_quality and bucket == "0DTE" else "OK"),
            }
        return result

    def test_symbol_email_is_compact_but_keeps_decision_fields(self):
        text = format_premarket_symbol_summary(
            "SPY", 100, "PREMARKET_MID", "实时", 99,
            {"pct": 1.25, "quality": "OK"},
            0.92, 0.015, self.gamma_fixture(), "OK")

        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn("SPY | 参考 $100.00", text)
        self.assertIn("C $105.00 / P $95.00 / Flip $101.00", text)
        self.assertIn("C/P/Flip距离 +5.00%/-5.00%/+1.00%", text)
        self.assertIn("Gamma 0D +1.20M", text)
        self.assertIn("覆盖 有效0/请求0", text)
        self.assertNotIn("全部零点", text)
        self.assertNotIn("最大OI", text)
        self.assertNotIn("符号假设", text)

    def test_symbol_email_surfaces_quality_degradation(self):
        text = format_premarket_symbol_summary(
            "QQQ", 500, "PREVIOUS_CLOSE_FALLBACK", "延迟冻结", 500,
            {"pct": None, "quality": "MISSING"},
            None, None, self.gamma_fixture(low_quality=True), "JUMP_REVIEW")

        self.assertIn("昨收回退", text)
        self.assertIn("0D NA[LOW_COVERAGE]", text)
        self.assertIn("未取得盘前价", text)
        self.assertIn("预期振幅=MISSING", text)
        self.assertIn("Flip=JUMP_REVIEW", text)

    def test_quality_footer_groups_internal_issue_keys(self):
        text = format_premarket_quality_summary(
            9, 10,
            ["gamma_0dte:QQQ:LOW_COVERAGE", "dpsv:QQQ",
             "dpsv:QQQ", "db_main:TSLA"])
        self.assertIn("入库 9/10", text)
        self.assertIn("质量项 3", text)
        self.assertIn("Gamma1", text)
        self.assertIn("FINRA1", text)
        self.assertIn("数据库1", text)


if __name__ == "__main__":
    unittest.main()
