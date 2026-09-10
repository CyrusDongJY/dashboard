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

from anomaly_engine import (  # noqa: E402
    AnomalyEvent,
    _expand_market_history_metrics,
    _metric_series_by_source_date,
    build_market_shock_series,
    collapse_events_by_cluster_family,
    gamma_decision_mask,
    market_native_series,
    metric_snapshot,
    scan_metric,
)
from backfill_history import (  # noqa: E402
    build_metric_series,
    latest_completed_nyse_day,
    parse_period_days,
    replay,
)
from environment_indices import (  # noqa: E402
    INDEX_DEFS,
    classify_state,
    composite_score,
    format_env_summary,
    rebuild_latest_statistics,
    score_frame,
    score_frame_details,
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
    def test_gamma_mask_fails_closed_for_low_coverage_derived_metrics(self):
        frame = pd.DataFrame({
            "gamma_quality": [
                {"ALL": "LOW_COVERAGE"}, {"ALL": "OK"}, {"ALL": "OK"}],
            "gamma_decision_eligible": [False, False, True],
            "call_wall": [500, 501, 502],
        })
        self.assertEqual(
            gamma_decision_mask(frame).tolist(), [False, False, True])

    def test_distance_version_filter_cannot_reinclude_ineligible_gamma(self):
        frame = pd.DataFrame({
            "gamma_quality": [
                {"ALL": "LOW_COVERAGE"}, {"ALL": "OK"}],
            "gamma_decision_eligible": [False, True],
            "distance_sign_version": [
                "LEVEL_MINUS_SPOT_V2", "LEVEL_MINUS_SPOT_V2"],
            "distance_to_call_wall_pct": [9.0, 1.0],
        })
        metric = frame.loc[
            gamma_decision_mask(frame), "distance_to_call_wall_pct"]
        metric = metric.loc[
            frame.loc[metric.index, "distance_sign_version"]
            == "LEVEL_MINUS_SPOT_V2"]
        self.assertEqual(metric.tolist(), [1.0])

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

    def test_core_maturity_gate_matches_sixty_observation_entry_rule(self):
        scores = {"idx_risk_pressure": 76.0, "idx_liquidity_stress": 20.0,
                  "idx_breadth_decay": 45.0}
        coverage = {"idx_risk_pressure": 1.0, "idx_liquidity_stress": 1.0,
                    "idx_breadth_decay": 1.0}
        maturity = {"idx_risk_pressure": 1.0, "idx_liquidity_stress": 0.72,
                    "idx_breadth_decay": 0.34}
        state, _ = classify_state(
            scores, coverage, 0, False, maturity=maturity)
        self.assertEqual(state, "脆弱观察")

    def test_low_average_core_maturity_still_fails_closed(self):
        scores = {"idx_risk_pressure": 76.0, "idx_liquidity_stress": 20.0,
                  "idx_breadth_decay": 45.0}
        coverage = {"idx_risk_pressure": 1.0, "idx_liquidity_stress": 1.0,
                    "idx_breadth_decay": 1.0}
        maturity = {key: 0.238 for key in coverage}
        state, _ = classify_state(
            scores, coverage, 0, False, maturity=maturity)
        self.assertEqual(state, "数据不足")

    def test_low_sample_component_is_excluded(self):
        frame = pd.DataFrame([metric_row("vix", 99, sample=30)])
        scores, coverage = score_frame(frame)
        self.assertIsNone(scores["idx_risk_pressure"])
        self.assertEqual(coverage["idx_risk_pressure"], 0.0)

    def test_coverage_and_sample_maturity_are_separate(self):
        frame = pd.DataFrame([
            metric_row("vix", 95, sample=60),
            metric_row("vvix", 90, sample=60),
            metric_row("move", 92, sample=60),
            metric_row("vix_ratio_contango_pct", 5, sample=60),
        ])
        scores, coverage, maturity, _ = score_frame_details(frame)
        self.assertIsNotNone(scores["idx_risk_pressure"])
        self.assertEqual(coverage["idx_risk_pressure"], 1.0)
        self.assertAlmostEqual(maturity["idx_risk_pressure"], 0.238)

    def test_backfilled_native_dates_repair_sparse_current_snapshot(self):
        dates = pd.bdate_range(end="2026-07-14", periods=80)
        rows = []
        for index, date in enumerate(dates):
            rows.append({
                "report_date": date.strftime("%Y-%m-%d"),
                "source_date": date.strftime("%Y-%m-%d"),
                "metric": "vix",
                "scope": "MACRO",
                "value": float(index + 1),
                "percentile": None,
                "sample_len": 10 if index == len(dates) - 1 else index + 1,
                "effective_obs_count": 10 if index == len(dates) - 1 else index + 1,
                "lag_days": 0,
            })
        rebuilt = rebuild_latest_statistics(pd.DataFrame(rows), "2026-07-14")
        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(int(rebuilt.iloc[0]["effective_obs_count"]), 80)
        self.assertGreater(float(rebuilt.iloc[0]["percentile"]), 98.0)

    def test_weekly_fill_is_deduplicated_by_native_source_date(self):
        rows = []
        report_dates = pd.bdate_range(end="2026-07-14", periods=160)
        native_dates = set()
        for index, report_date in enumerate(report_dates):
            native_date = report_date - pd.Timedelta(days=report_date.weekday())
            native_dates.add(native_date.strftime("%Y-%m-%d"))
            rows.append({
                "report_date": report_date.strftime("%Y-%m-%d"),
                "source_date": native_date.strftime("%Y-%m-%d"),
                "metric": "nfci",
                "scope": "MACRO",
                "value": float(index // 5),
                "percentile": None,
                "sample_len": 5,
                "effective_obs_count": 5,
                "lag_days": 0,
            })
        rebuilt = rebuild_latest_statistics(
            pd.DataFrame(rows), report_dates[-1].strftime("%Y-%m-%d"))
        self.assertEqual(
            int(rebuilt.iloc[0]["effective_obs_count"]), len(native_dates))
        self.assertLess(
            int(rebuilt.iloc[0]["effective_obs_count"]), len(report_dates))

    def test_composite_requires_eligible_coverage(self):
        scores = {key: None for key in INDEX_DEFS}
        coverage = {key: 0.0 for key in INDEX_DEFS}
        scores["idx_risk_pressure"] = 90.0
        coverage["idx_risk_pressure"] = 0.2
        self.assertIsNone(composite_score(scores, coverage))

    def test_history_builder_scores_each_day(self):
        rows = [
            metric_row("vix", 95), metric_row("vvix", 90),
            metric_row("move", 92), metric_row("vix_ratio_contango_pct", 5),
        ]
        history = build_history(pd.DataFrame(rows))
        self.assertEqual(len(history), 1)
        self.assertGreater(history.iloc[0]["idx_risk_pressure"], 85)

    def test_summary_explains_reference_score_and_zero_resonance(self):
        text = format_env_summary({
            "state": "数据不足",
            "state_desc": "核心维度覆盖不足，继续积累；本日不作环境分类",
            "idx_risk_pressure": 82.0,
            "idx_liquidity_stress": None,
            "idx_breadth_decay": None,
            "idx_options_fragility": None,
            "idx_flow_behavior": None,
            "coverage": {"idx_risk_pressure": 1.0},
            "maturity": {"idx_risk_pressure": 1.0},
            "diagnostics": {
                "idx_liquidity_stress": [{
                    "metric": "nfci", "status": "INSUFFICIENT_SAMPLE",
                    "effective_obs_count": 40, "required_obs_count": 60,
                }],
                "idx_options_fragility": [{
                    "metric": "expected_move_pct",
                    "status": "INSUFFICIENT_SAMPLE",
                    "effective_obs_count": 19,
                    "required_obs_count": 60,
                }],
            },
            "composite": 82.0,
            "resonance_count": 0,
            "resonance_themes": [],
        })
        self.assertIn(
            "可用面板风险压力参考值：82.0/100（越高越危险；不代表整体环境）",
            text,
        )
        self.assertIn("数值越高表示风险压力越大", text)
        self.assertIn("有效样本不足 40/60", text)
        self.assertIn("预期波幅 19/60", text)
        self.assertIn("不代表环境健康", text)


class SnapshotTests(unittest.TestCase):
    def test_market_history_vix_ratio_uses_canonical_contango_basis(self):
        expanded = _expand_market_history_metrics(pd.DataFrame([{
            "record_date": "2026-07-14",
            "full_metrics": {
                "vix_term_ratio": 0.9,
                "vrp_num": 7.5,
                "market_metric_lineage": {
                    "vix_ratio_contango_pct": {
                        "source_date": "2026-07-13",
                        "source_name": "yfinance:^VIX/^VIX3M",
                    },
                },
            },
        }]))
        self.assertAlmostEqual(expanded.iloc[0]["vix_ratio_contango_pct"], 10.0)
        self.assertEqual(expanded.iloc[0]["vrp_num"], 7.5)
        self.assertEqual(
            expanded.iloc[0]["vix_ratio_contango_pct_source_date"],
            "2026-07-13")

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

    def test_market_lineage_deduplicates_stale_source_dates(self):
        frame = pd.DataFrame([
            {"record_date": "2026-09-04", "jpy": 156.0,
             "full_metrics": {"market_metric_lineage": {"jpy": {
                 "source_date": "2026-09-04", "source_name": "yfinance:JPY=X"}}}},
            {"record_date": "2026-09-07", "jpy": 156.0,
             "full_metrics": {"market_metric_lineage": {"jpy": {
                 "source_date": "2026-09-04", "source_name": "yfinance:JPY=X"}}}},
            {"record_date": "2026-09-08", "jpy": 153.7,
             "full_metrics": {"market_metric_lineage": {"jpy": {
                 "source_date": "2026-09-08", "source_name": "yfinance:JPY=X"}}}},
        ])
        series, source = market_native_series(frame, "jpy")
        self.assertEqual(len(series), 2)
        self.assertEqual(source, "yfinance:JPY=X")
        self.assertEqual(series.index[-1], pd.Timestamp("2026-09-08"))

    def test_low_frequency_metric_uses_native_dates_for_statistics(self):
        frame = pd.DataFrame({
            "record_date": ["2026-09-04", "2026-09-07", "2026-09-08"],
            "nfci": [-0.5, -0.5, -0.4],
            "nfci_source_date": ["2026-09-04", "2026-09-04", "2026-09-08"],
        })
        series = _metric_series_by_source_date(
            frame, "nfci", "record_date", "nfci_source_date")
        self.assertEqual(series.tolist(), [-0.5, -0.4])

    def test_market_shocks_use_returns_and_basis_points(self):
        prices = pd.Series([100.0, 99.0, 97.0, 96.0, 95.0, 94.0],
                           index=pd.bdate_range("2026-09-01", periods=6))
        fx = build_market_shock_series("usdjpy", prices)
        self.assertAlmostEqual(fx["usdjpy_return_5d_pct"].iloc[-1], -6.0)

        yields = pd.Series([4.0, 4.1], index=pd.bdate_range("2026-09-01", periods=2))
        rates = build_market_shock_series("us10y", yields)
        self.assertAlmostEqual(rates["us10y_change_1d_bp"].iloc[-1], 10.0)

    def test_two_sided_market_shock_detects_large_negative_move(self):
        baseline = pd.Series(np.linspace(-1.0, 1.0, 126))
        values = pd.concat([baseline, pd.Series([-4.2])], ignore_index=True)
        events = scan_metric(
            "usdjpy_return_5d_pct", values, "2026-09-09",
            source_date="2026-09-09")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, "down")
        self.assertEqual(events[0].severity, 3)
        self.assertIn("日元走强", events[0].explanation)
        self.assertEqual(events[0].confidence, 1.0)

    def test_noise_floor_blocks_tiny_statistical_outlier(self):
        values = pd.Series([0.0] * 126 + [0.2])
        events = scan_metric(
            "dxy_return_1d_pct", values, "2026-09-09",
            source_date="2026-09-09")
        self.assertEqual(events, [])

    def test_cluster_counts_one_asset_once_across_windows(self):
        events = [
            AnomalyEvent("2026-09-09", metric, "MACRO", window,
                         severity=2, confidence=1.0)
            for metric, window in [
                ("usdjpy_return_1d_pct", "1D"),
                ("usdjpy_return_5d_pct", "5D"),
                ("usdjpy_return_21d_pct", "21D"),
            ]
        ]
        self.assertEqual(len(collapse_events_by_cluster_family(events)), 1)


class BackfillTests(unittest.TestCase):
    def test_period_parser(self):
        self.assertEqual(parse_period_days("2y"), 730)
        self.assertEqual(parse_period_days("18mo"), 540)

    def test_backfill_end_date_waits_for_eod_files(self):
        midday = pd.Timestamp("2026-08-03 11:30", tz="America/New_York")
        evening = pd.Timestamp("2026-08-03 18:05", tz="America/New_York")
        self.assertEqual(
            latest_completed_nyse_day(midday), pd.Timestamp("2026-07-31"))
        self.assertEqual(
            latest_completed_nyse_day(evening), pd.Timestamp("2026-08-03"))

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

    def test_yfinance_backfill_includes_skew(self):
        index = pd.bdate_range("2026-01-02", periods=3)
        yf_frame = pd.DataFrame({
            "^SKEW": [140.0, 141.0, 139.0],
            "^VIX": [20.0, 21.0, 22.0],
            "^VIX3M": [22.0, 23.1, 24.2],
        }, index=index)
        metrics = build_metric_series(yf_frame, pd.DataFrame())
        self.assertIn("skew", metrics)
        self.assertIn("vix_ratio_contango_pct", metrics)
        self.assertNotIn("vix_contango_pct", metrics)
        self.assertEqual(metrics["skew"]["source_name"], "yfinance")
        self.assertAlmostEqual(
            metrics["vix_ratio_contango_pct"]["series"].iloc[0],
            (1.0 - 20.0 / 22.0) * 100,
        )

    def test_yfinance_backfill_includes_cross_asset_shocks(self):
        index = pd.bdate_range("2025-01-02", periods=140)
        yf_frame = pd.DataFrame({
            "JPY=X": np.linspace(150.0, 140.0, len(index)),
            "CL=F": np.linspace(70.0, 80.0, len(index)),
            "GC=F": np.linspace(2000.0, 2200.0, len(index)),
            "BTC-USD": np.linspace(50000.0, 70000.0, len(index)),
            "^TNX": np.linspace(4.0, 4.5, len(index)),
            "DX-Y.NYB": np.linspace(100.0, 105.0, len(index)),
            "^VIX": np.linspace(15.0, 20.0, len(index)),
            "SPY": np.linspace(500.0, 550.0, len(index)),
        }, index=index)
        metrics = build_metric_series(yf_frame, pd.DataFrame())
        for metric in (
                "usdjpy_return_1d_pct", "oil_return_5d_pct",
                "gold_return_21d_pct", "btc_return_1d_pct",
                "us10y_change_5d_bp", "dxy_return_21d_pct", "vrp_num"):
            self.assertIn(metric, metrics)
            self.assertTrue(metrics[metric]["source_name"].startswith("yfinance:"))

    def test_vrp_backfill_ignores_crypto_weekend_rows(self):
        index = pd.date_range("2026-01-01", periods=60, freq="D")
        weekdays = index.dayofweek < 5
        frame = pd.DataFrame(index=index)
        frame["SPY"] = np.where(weekdays, np.linspace(500.0, 520.0, len(index)), np.nan)
        frame["^VIX"] = np.where(weekdays, np.linspace(18.0, 20.0, len(index)), np.nan)
        frame["BTC-USD"] = np.linspace(60000.0, 65000.0, len(index))
        metrics = build_metric_series(frame, pd.DataFrame())
        self.assertIn("vrp_num", metrics)
        self.assertGreater(len(metrics["vrp_num"]["series"]), 0)


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
            metric_row("move", 92), metric_row("vix_ratio_contango_pct", 5),
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
