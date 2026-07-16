import sys
import types
import unittest
import importlib.util
import os
import tempfile

import numpy as np
import pandas as pd


class _Calendar:
    def schedule(self, start_date, end_date):
        return pd.DataFrame(index=pd.bdate_range(start_date, end_date))


# 本地审查运行时不一定安装生产环境依赖；这些模块在被测路径中只需最小接口。
calendar_module = types.ModuleType("pandas_market_calendars")
calendar_module.get_calendar = lambda _name: _Calendar()
sys.modules.setdefault("pandas_market_calendars", calendar_module)

yfinance_module = types.ModuleType("yfinance")
yfinance_module.download = lambda *args, **kwargs: pd.DataFrame()
sys.modules.setdefault("yfinance", yfinance_module)

requests_module = types.ModuleType("requests")
requests_module.Session = object
sys.modules.setdefault("requests", requests_module)

from anomaly_engine import AnomalyEvent, metric_snapshot, scan_metric  # noqa: E402
from backfill_history import parse_period_days, replay  # noqa: E402
from environment_indices import (  # noqa: E402
    INDEX_DEFS,
    classify_state,
    composite_score,
    score_frame,
)
from environment_report import build_history, generate_environment_chart  # noqa: E402


def metric_row(metric, percentile, sample=252, lag=0, scope="MACRO"):
    return {
        "report_date": "2026-07-14",
        "metric": metric,
        "scope": scope,
        "percentile": percentile,
        "sample_len": sample,
        "effective_obs_count": sample,
        "lag_days": lag,
    }


class EnvironmentIndexTests(unittest.TestCase):
    def test_no_data_is_not_healthy(self):
        state, _ = classify_state({}, {}, 0, False)
        self.assertEqual(state, "数据不足")

    def test_missing_breadth_cannot_classify_fragile(self):
        scores = {"idx_risk_pressure": 95.0, "idx_liquidity_stress": 50.0,
                  "idx_breadth_decay": None}
        coverage = {"idx_risk_pressure": 1.0, "idx_liquidity_stress": 1.0,
                    "idx_breadth_decay": 0.0}
        state, _ = classify_state(scores, coverage, 0, False)
        self.assertEqual(state, "数据不足")

    def test_low_sample_component_is_excluded(self):
        frame = pd.DataFrame([metric_row("vix", 99, sample=30)])
        scores, coverage = score_frame(frame)
        self.assertIsNone(scores["idx_risk_pressure"])
        self.assertEqual(coverage["idx_risk_pressure"], 0.0)

    def test_composite_requires_eligible_coverage(self):
        scores = {key: None for key in INDEX_DEFS}
        coverage = {key: 0.0 for key in INDEX_DEFS}
        scores["idx_risk_pressure"] = 90.0
        coverage["idx_risk_pressure"] = 0.2
        self.assertIsNone(composite_score(scores, coverage))

    def test_history_builder_scores_each_day(self):
        rows = [
            metric_row("vix", 95), metric_row("vvix", 90),
            metric_row("move", 92), metric_row("vix_contango_pct", 5),
        ]
        history = build_history(pd.DataFrame(rows))
        self.assertEqual(len(history), 1)
        self.assertGreater(history.iloc[0]["idx_risk_pressure"], 85)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_preserves_session_and_quality_contract(self):
        values = pd.Series(np.linspace(10, 20, 80))
        row = metric_snapshot(
            "vix", values, "2026-07-14", source_date="2026-07-13",
            session="INTRADAY", is_final=False, source_name="test",
            is_filled=True, effective_obs_count=40)
        self.assertEqual(row["session"], "INTRADAY")
        self.assertFalse(row["is_final"])
        self.assertTrue(row["is_filled"])
        self.assertEqual(row["effective_obs_count"], 40)

    def test_resonance_identity_is_persisted(self):
        row = AnomalyEvent(
            report_date="2026-07-14", metric="RESONANCE", scope="MACRO",
            window="multi", layer="resonance", resonance_key="theme_a").to_row()
        self.assertEqual(row["resonance_key"], "theme_a")

    def test_dpsv_is_observation_only_not_formal_anomaly(self):
        values = pd.Series(np.linspace(60, 20, 100))
        events = scan_metric("dpsv_pct", values, "2026-07-14", scope="SPY")
        self.assertEqual(events, [])


class BackfillTests(unittest.TestCase):
    def test_period_parser(self):
        self.assertEqual(parse_period_days("2y"), 730)
        self.assertEqual(parse_period_days("18mo"), 540)

    def test_low_frequency_fill_does_not_inflate_effective_observations(self):
        index = pd.date_range("2024-01-05", periods=80, freq="W-FRI")
        series = pd.Series(np.linspace(-0.5, 0.2, len(index)), index=index)
        rows = replay(
            "nfci", series, "FRED:NFCI",
            pd.Timestamp("2025-06-02"), pd.Timestamp("2025-06-13"))
        self.assertTrue(rows)
        for row in rows:
            report_day = pd.Timestamp(row["report_date"])
            expected = int((series.index <= report_day).sum())
            self.assertEqual(row["effective_obs_count"], expected)
            self.assertEqual(row["sample_len"], expected)
        self.assertTrue(any(row["is_filled"] for row in rows))


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, _name):
        return lambda *args, **kwargs: self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _Supabase:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _Query(self.rows)


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib not installed")
class ChartTests(unittest.TestCase):
    def test_png_report_is_nonempty(self):
        rows = [
            metric_row("vix", 95), metric_row("vvix", 90),
            metric_row("move", 92), metric_row("vix_contango_pct", 5),
        ]
        path = os.path.join(
            tempfile.gettempdir(), "environment_chart_test", "nested",
            "environment_chart_test.png")
        generated = generate_environment_chart(
            _Supabase(rows), "2026-07-14", output_path=path)
        self.assertEqual(generated, path)
        self.assertGreater(os.path.getsize(path), 10_000)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")
        from PIL import Image
        image = Image.open(path).convert("RGB")
        extrema = image.getextrema()
        self.assertTrue(any(low < high for low, high in extrema), "PNG must not be blank")

    def test_empty_history_does_not_claim_a_chart(self):
        generated = generate_environment_chart(
            _Supabase([]), "2026-07-14",
            output_path=os.path.join(tempfile.gettempdir(), "should_not_exist.png"))
        self.assertIsNone(generated)


if __name__ == "__main__":
    unittest.main()
