import math
import unittest
from datetime import date

import pandas as pd

from pre_market_metrics import (
    expected_move_metrics,
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


if __name__ == "__main__":
    unittest.main()
