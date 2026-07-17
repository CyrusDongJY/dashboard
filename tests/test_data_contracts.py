import unittest

import pandas as pd

from data_contracts import (
    calculate_iv_rank_percentile,
    compute_etf_share_metrics,
    up_down_volume_ratio,
)
from pre_market_metrics import gamma_structure


class IntradayFailClosedTests(unittest.TestCase):
    def test_missing_up_down_volume_is_not_neutral_one(self):
        value, status = up_down_volume_ratio(0, 0)
        self.assertIsNone(value)
        self.assertEqual(status, "MISSING_UVOL_DVOL")

    def test_valid_up_down_volume_is_preserved(self):
        value, status = up_down_volume_ratio(200, 100)
        self.assertEqual(value, 2.0)
        self.assertEqual(status, "OK")


class IVDefinitionTests(unittest.TestCase):
    def test_iv_rank_and_percentile_are_distinct(self):
        metrics = calculate_iv_rank_percentile([10, 11, 12, 19, 20, 15])
        self.assertAlmostEqual(metrics["iv_rank_pct"], 50.0)
        self.assertAlmostEqual(metrics["iv_percentile_pct"], 50.0)

        skewed = calculate_iv_rank_percentile([10, 10.5, 11, 19, 20, 12])
        self.assertAlmostEqual(skewed["iv_rank_pct"], 20.0)
        self.assertAlmostEqual(skewed["iv_percentile_pct"], 50.0)


class ETFShareHistoryTests(unittest.TestCase):
    def test_share_changes_use_distinct_observation_dates(self):
        dates = pd.bdate_range("2026-06-01", periods=21)
        history = pd.DataFrame({
            "Date": list(dates) + [dates[-1]],
            "Ticker": ["SPY"] * 22,
            "Shares": [100_000_000 + index * 1_000_000 for index in range(21)]
                      + [120_000_000],
            "Price": [700.0] * 22,
        })
        metrics = compute_etf_share_metrics(history)["SPY"]
        self.assertEqual(metrics["quality"], "CONTEXT_ONLY")
        self.assertEqual(metrics["changes"]["1d"]["share_change_m"], 1.0)
        self.assertEqual(metrics["changes"]["5d"]["share_change_m"], 5.0)
        self.assertEqual(metrics["changes"]["20d"]["share_change_m"], 20.0)
        self.assertEqual(metrics["changes"]["1d"]["dollar_flow_m"], 700.0)


class GammaLineageTests(unittest.TestCase):
    def test_gamma_curve_emits_version_and_grid_contract(self):
        frame = pd.DataFrame([{
            "Exp": "20260815", "S": 100.0, "R": "C", "OI": 1000.0,
            "IV": 0.25, "T": 30 / 365, "DTE": 30,
        }])
        result = gamma_structure(frame, 100.0)
        self.assertEqual(result["curve_version"], "gamma_curve_v2")
        self.assertEqual(result["grid_width_pct"], 20.0)
        self.assertEqual(result["grid_points"], 161)


if __name__ == "__main__":
    unittest.main()
