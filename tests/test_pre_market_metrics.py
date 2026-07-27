import math
import unittest
from datetime import date

import pandas as pd

from pre_market_metrics import (
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
)


def option_row(
        expiration, strike, right, oi, iv=0.25, years=30 / 365,
        dte=30, mid=None, delta=None):
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
        self.assertEqual(gated['ALL']['quality'], 'LOW_COVERAGE')
        self.assertIsNone(gated['ALL']['net_gamma_m'])
        self.assertIsNotNone(gated['ALL']['raw_net_gamma_m'])
        self.assertIsNone(gated['call_wall'])
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

        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("SPY | 参考 $100.00", text)
        self.assertIn("C $105.00 / P $95.00 / Flip $101.00", text)
        self.assertIn("C/P/Flip距离 +5.00%/-5.00%/+1.00%", text)
        self.assertIn("Gamma 0D +1.20M", text)
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
