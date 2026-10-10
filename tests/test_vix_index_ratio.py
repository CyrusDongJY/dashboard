import json
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

from vix_index_ratio import compute_vix_index_ratio, format_vix_index_ratio


class VixIndexRatioTests(unittest.TestCase):
    def frame(self, vix, vix3m):
        return pd.DataFrame(
            {"^VIX": vix, "^VIX3M": vix3m},
            index=pd.date_range("2026-10-08", periods=len(vix)),
        )

    def test_native_closes_align_on_latest_date(self):
        result = compute_vix_index_ratio(self.frame([19, 18], [21, 20]))
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source_date"], "2026-10-09")
        self.assertAlmostEqual(result["ratio"], 0.9)
        self.assertEqual(result["state"], "CONTANGO")
        self.assertIn("2026-10-09", format_vix_index_ratio(result))

    def test_mismatched_latest_dates_reject_older_common_pair(self):
        frame = self.frame([19, 18], [21, None])
        result = compute_vix_index_ratio(frame)
        self.assertEqual(result["status"], "SOURCE_DATE_MISMATCH")
        self.assertIsNone(result["ratio"])
        self.assertIsNone(result["source_date"])
        self.assertEqual(result["source_dates"]["^VIX"], "2026-10-09")
        self.assertEqual(result["source_dates"]["^VIX3M"], "2026-10-08")
        self.assertIn("SOURCE_DATE_MISMATCH", format_vix_index_ratio(result))
        self.assertIn("VIX3M=2026-10-08", format_vix_index_ratio(result))

    def test_invalid_latest_price_never_falls_back_to_previous_close(self):
        for bad in (0, -1, float("inf"), float("-inf"), "bad"):
            for symbol in ("^VIX", "^VIX3M"):
                with self.subTest(price=bad, symbol=symbol):
                    frame = self.frame([19, 18], [21, 20]).astype(object)
                    frame.loc[frame.index[-1], symbol] = bad
                    result = compute_vix_index_ratio(frame)
                    self.assertEqual(result["status"], "INVALID_PRICE")
                    self.assertIsNone(result["ratio"])
                    self.assertEqual(
                        result["source_dates"][symbol], "2026-10-09")
                    # Missing/invalid numbers remain legal JSON nulls.
                    json.dumps(result, allow_nan=False)

    def test_missing_leg_is_explicit(self):
        for frame in (pd.DataFrame(), self.frame([18], [None])):
            result = compute_vix_index_ratio(frame)
            self.assertEqual(result["status"], "MISSING_SOURCE")
            self.assertIsNone(result["ratio"])
            self.assertIn("MISSING_SOURCE:^VIX3M", result["reasons"])

    def test_equality_and_dead_band_are_flat(self):
        for vix, vix3m in ((18, 18), (18.08, 18), (18, 18.08)):
            with self.subTest(vix=vix, vix3m=vix3m):
                result = compute_vix_index_ratio(self.frame([vix], [vix3m]))
                self.assertEqual(result["state"], "FLAT")
                self.assertIn("平坦", format_vix_index_ratio(result))
                self.assertNotIn("倒挂", format_vix_index_ratio(result))

    def test_inversion_is_reported_without_extreme_risk_claim(self):
        result = compute_vix_index_ratio(self.frame([20], [18]))
        self.assertEqual(result["state"], "BACKWARDATION")
        self.assertIn("倒挂", format_vix_index_ratio(result))
        self.assertNotIn("极限", format_vix_index_ratio(result))

    def test_raw_trailing_empty_calendar_rows_do_not_create_new_source_dates(self):
        result = compute_vix_index_ratio(self.frame([18, None], [20, None]))
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source_date"], "2026-10-08")
        self.assertAlmostEqual(result["ratio"], 0.9)


class DashboardVixIndexRatioTests(unittest.TestCase):
    def setUp(self):
        config = types.ModuleType("market_config")
        config.FRED_API_KEY = "test-only"
        finance = types.ModuleType("yfinance")
        finance.Ticker = lambda _symbol: types.SimpleNamespace(options=[])
        market_utils = types.ModuleType("market_utils")
        market_utils.lag_trading_days = lambda *_args: 0
        liquidity_sources = types.ModuleType("liquidity_sources")
        liquidity_sources.OfficialLiquiditySources = object
        path = Path(__file__).resolve().parents[1] / "ultimate_dashboard.py"
        spec = importlib.util.spec_from_file_location("vix_ratio_dashboard_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "market_config": config, "yfinance": finance,
            "market_utils": market_utils,
            "requests": types.ModuleType("requests"),
            "liquidity_sources": liquidity_sources,
        }):
            spec.loader.exec_module(module)
        with patch.object(module.os, "makedirs"):
            self.dashboard = module.UltimateDashboard()
        self.dashboard.liquidity["y10"] = "-"
        self.dashboard.log = lambda _message: None

    def calculate(self, frame):
        self.dashboard.data_cache = {"raw_close": frame, "close": frame.ffill()}
        self.dashboard.market_source_dates = {
            symbol: frame[symbol].dropna().index[-1].strftime("%Y-%m-%d")
            for symbol in frame.columns if not frame[symbol].dropna().empty
        }
        self.dashboard.calculate_metrics()
        return self.dashboard.get_db_payload("2026-10-09")[0]["full_metrics"]

    def test_dashboard_uses_raw_bars_and_clears_previous_ratio(self):
        frame = pd.DataFrame(
            {"^VIX": [19, 18], "^VIX3M": [21, 20]},
            index=pd.to_datetime(["2026-10-08", "2026-10-09"]),
        )
        metrics = self.calculate(frame)
        self.assertAlmostEqual(metrics["vix_term_ratio"], 0.9)
        self.assertEqual(
            metrics["market_metric_lineage"]["vix_ratio_contango_pct"]["source_date"],
            "2026-10-09",
        )
        frame.loc[frame.index[-1], "^VIX3M"] = None
        metrics = self.calculate(frame)
        self.assertIsNone(metrics["vix_term_ratio"])
        self.assertEqual(
            metrics["vix_term_ratio_quality"]["status"], "SOURCE_DATE_MISMATCH")
        self.assertNotIn("vix_ratio_contango_pct", metrics["market_metric_lineage"])
        self.assertIn("VIX3M=2026-10-08", metrics["vix_term"])


if __name__ == "__main__":
    unittest.main()
