import os
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

try:
    import matplotlib  # noqa: F401
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

from liquidity_monitor import (
    CALC_VERSION,
    COMPONENTS,
    PillarScore,
    _business_day_change,
    _classify,
    _score_component,
    _shares_to_millions,
    build_database_frame,
    compute_liquidity_monitor,
    format_liquidity_summary,
    merge_official_frame,
    score_liquidity_frame,
)
from liquidity_report import build_liquidity_history, generate_liquidity_chart
from liquidity_sources import OfficialLiquiditySources


def synthetic_frame(supportive=True, periods=420):
    index = pd.bdate_range("2024-01-02", periods=periods)
    direction = 1 if supportive else -1
    ramp = np.linspace(0, 1, periods)
    frame = pd.DataFrame(index=index)

    frame["net_liq_b"] = 5_500 + direction * ramp * 700
    frame["reserves_t"] = 3.12 if supportive else 2.70
    frame["m2_yoy"] = 2.0 + direction * ramp * 3.0
    frame["tga_b"] = 850 - direction * ramp * 250
    frame["fed_assets_b"] = 6_500 + direction * ramp * 350

    for column, base in (
        ("sofr_iorb_bps", 2.0),
        ("tgcr_iorb_bps", 1.5),
        ("effr_iorb_bps", -1.0),
        ("sofr_75_iorb_bps", 5.0),
        ("sofr_dispersion_bps", 8.0),
        ("nfci", 0.0),
        ("move", 105.0),
        ("hy_spread", 4.0),
        ("ig_spread", 1.2),
        ("vvix", 110.0),
        ("skew", 145.0),
        ("breadth_diff_pct", 1.2),
    ):
        frame[column] = base - direction * ramp

    frame["hyg_tlt_ratio"] = 0.8 + direction * ramp * 0.16
    frame["hyg_sh_m"] = 200 + direction * ramp * 30
    frame["jnk_sh_m"] = 150 + direction * ramp * 20
    frame["pct_20ma"] = 45 + direction * ramp * 30
    frame["pct_50ma"] = 48 + direction * ramp * 28
    frame["pct_200ma"] = 50 + direction * ramp * 25
    frame["vix_futures_contango_pct"] = 3 + direction * ramp * 6
    frame["vix_ratio_contango_pct"] = 5 + direction * ramp * 7

    for column in list(frame.columns):
        frame[f"{column}_source_date"] = index
    frame["rrp_b"] = 10.0
    frame["rrp_b_source_date"] = index
    frame["sofr_rate"] = 3.6
    frame["sofr_rate_source_date"] = index
    frame["dix_pct"] = 44.0
    frame["gex_billions"] = 5.0
    frame.index.name = "report_date"
    return frame


def state_pillars(funding=50.0, credit=50.0):
    return {
        "water_stock": PillarScore(
            "water_stock", "基础水量", "System Stock", 50.0, 1.0),
        "flow_pulse": PillarScore(
            "flow_pulse", "边际水流", "Flow Pulse", 50.0, 1.0),
        "funding_health": PillarScore(
            "funding_health", "融资管道", "Funding Pipes", funding, 1.0),
        "credit_transmission": PillarScore(
            "credit_transmission", "信用传导", "Credit Transfer", credit, 1.0),
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payloads.pop(0))


class LiquidityScoreTests(unittest.TestCase):
    def test_supportive_frame_scores_higher_than_tightening_frame(self):
        supportive = score_liquidity_frame(synthetic_frame(True))
        tightening = score_liquidity_frame(synthetic_frame(False))
        self.assertIsNotNone(supportive.composite)
        self.assertIsNotNone(tightening.composite)
        self.assertGreater(supportive.composite, tightening.composite)
        self.assertGreater(
            supportive.pillars["flow_pulse"].score,
            tightening.pillars["flow_pulse"].score,
        )

    def test_calc_version_is_v2(self):
        self.assertEqual(CALC_VERSION, "liquidity_v2")

    def test_missing_data_never_defaults_to_healthy(self):
        index = pd.bdate_range("2026-01-02", periods=5)
        result = score_liquidity_frame(
            pd.DataFrame({"sofr_rate": [3.6] * 5}, index=index))
        self.assertEqual(result.state, "数据不足")
        self.assertIsNone(result.composite)

    def test_raw_sofr_dix_and_gex_are_not_scoring_components(self):
        columns = {spec.column for spec in COMPONENTS}
        self.assertNotIn("sofr_rate", columns)
        self.assertNotIn("dix_pct", columns)
        self.assertNotIn("gex_billions", columns)
        self.assertNotIn("hyg_sh_m", columns)
        self.assertNotIn("jnk_sh_m", columns)

    def test_missing_pillar_summary_explains_observation_gap(self):
        index = pd.bdate_range("2026-01-02", periods=5)
        result = score_liquidity_frame(
            pd.DataFrame({"pct_20ma": [50.0] * 5}, index=index))
        summary = format_liquidity_summary(result)
        self.assertIn("有效观测", summary)
        self.assertIn("5/60", summary)

    def test_vix_curve_contract_keeps_two_distinct_columns(self):
        columns = {spec.column for spec in COMPONENTS}
        self.assertIn("vix_futures_contango_pct", columns)
        self.assertIn("vix_ratio_contango_pct", columns)
        self.assertNotIn("vix_contango_pct", columns)

    def test_net_liquidity_pulse_uses_dollar_change_not_percent(self):
        frame = synthetic_frame(True)
        spec = next(item for item in COMPONENTS if item.key == "net_liq_20d")
        component = _score_component(frame, spec, frame.index[-1])
        expected = frame["net_liq_b"].diff(20).iloc[-1]
        self.assertAlmostEqual(component.signal_value, expected, places=4)

    def test_relative_funding_tail_needs_absolute_guard(self):
        state, _ = _classify(
            state_pillars(funding=20.0), 1.0, {"sofr_iorb_bps": 3.0})
        self.assertEqual(state, "平衡水位")
        state, _ = _classify(
            state_pillars(funding=20.0), 1.0, {"sofr_iorb_bps": 5.0})
        self.assertEqual(state, "融资管道承压")

    def test_relative_credit_tail_needs_absolute_guard(self):
        state, _ = _classify(
            state_pillars(credit=20.0), 1.0, {"hy_spread": 4.9})
        self.assertEqual(state, "平衡水位")
        state, _ = _classify(
            state_pillars(credit=20.0), 1.0, {"hy_spread": 5.0})
        self.assertEqual(state, "信用收缩")

    def test_share_parser_normalizes_units(self):
        self.assertEqual(_shares_to_millions("917.78M"), 917.78)
        self.assertEqual(_shares_to_millions("1.2B"), 1200.0)
        self.assertTrue(np.isnan(_shares_to_millions("-")))

    def test_native_source_dates_prevent_filled_sample_inflation(self):
        index = pd.bdate_range("2025-01-02", periods=80)
        frame = pd.DataFrame({
            "m2_yoy": np.linspace(2.0, 3.0, len(index)),
            "m2_yoy_source_date": [index[0]] * len(index),
        }, index=index)
        spec = next(item for item in COMPONENTS if item.key == "m2_growth")
        component = _score_component(frame, spec, index[-1])
        self.assertIsNone(component.score)
        self.assertIn("1/12", component.note)

    def test_rrp_exhaustion_is_context_not_false_crisis(self):
        result = score_liquidity_frame(synthetic_frame(True))
        self.assertTrue(any("RRP" in item for item in result.warnings))
        self.assertGreater(result.pillars["water_stock"].score, 50)

    def test_tga_warning_window_uses_business_dates(self):
        index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-09"])
        frame = pd.DataFrame({"tga_b": [600.0, 620.0, 725.0]}, index=index)
        change = _business_day_change(frame, "tga_b", index[-1], 5)
        self.assertEqual(change, 125.0)

    def test_persistence_failure_is_visible(self):
        frame = synthetic_frame(True)
        report_date = str(frame.index[-1].date())
        market_utils = types.ModuleType("market_utils")
        market_utils.safe_upsert = Mock(return_value=None)
        market_utils.log_data_quality = Mock()
        with patch.dict("sys.modules", {"market_utils": market_utils}):
            with patch(
                    "liquidity_monitor.build_database_frame",
                    return_value=frame):
                result, _ = compute_liquidity_monitor(
                    None, report_date, official_frame=None, persist=True)
        self.assertEqual(result.persistence_status, "failed")
        self.assertIn("写入失败", format_liquidity_summary(result))
        market_utils.log_data_quality.assert_called_once()


class LiquiditySourceTests(unittest.TestCase):
    def test_database_fallback_uses_native_fred_dates(self):
        market_rows = [{
            "record_date": "2026-01-09",
            "source_date": "2026-01-09",
            "fed_assets": 6500.0,
            "net_liq": 5800.0,
            "tga": 600.0,
            "rrp": 100.0,
            "reserves": 3.1,
            "sofr": 3.6,
            "dix": 44.0,
            "gex": 5.0,
            "full_metrics": {
                "vix_term_ratio": 0.9,
                "fred_source_dates": {
                    "Fed_Assets": "2026-01-07",
                    "TGA": "2026-01-09",
                    "RRP": "2026-01-09",
                    "Reserves": "2026-01-07",
                    "SOFR": "2026-01-09",
                },
            },
        }]

        def fake_rows(_supabase, table, _date_col, _start, _end):
            return pd.DataFrame(market_rows if table == "market_history" else [])

        with patch("liquidity_monitor._fetch_rows", side_effect=fake_rows):
            frame = build_database_frame(None, "2026-01-01", "2026-01-09")
        row = frame.iloc[-1]
        self.assertEqual(row["rrp_b"], 100.0)
        self.assertAlmostEqual(row["vix_ratio_contango_pct"], 10.0)
        self.assertEqual(
            row["fed_assets_b_source_date"], pd.Timestamp("2026-01-07"))
        self.assertEqual(
            row["net_liq_b_source_date"], pd.Timestamp("2026-01-07"))

    def test_misaligned_tables_keep_union_and_do_not_mix_vix_contracts(self):
        market_rows = [{
            "record_date": "2026-01-08",
            "source_date": "2026-01-08",
            "full_metrics": {"vix_term_ratio": 0.88},
        }]
        macro_rows = [{
            "date": "2026-01-09",
            "source_date": "2026-01-09",
            "vix_contango_pct": 4.85,
            "pct_20ma": 61.0,
        }]

        def fake_rows(_supabase, table, _date_col, _start, _end):
            if table == "market_history":
                return pd.DataFrame(market_rows)
            if table == "macro_spot_daily":
                return pd.DataFrame(macro_rows)
            return pd.DataFrame()

        with patch("liquidity_monitor._fetch_rows", side_effect=fake_rows):
            frame = build_database_frame(None, "2026-01-01", "2026-01-09")
        first, second = pd.Timestamp("2026-01-08"), pd.Timestamp("2026-01-09")
        self.assertEqual(list(frame.index), [first, second])
        self.assertAlmostEqual(frame.loc[first, "vix_ratio_contango_pct"], 12.0)
        self.assertTrue(pd.isna(frame.loc[first, "vix_futures_contango_pct"]))
        self.assertAlmostEqual(frame.loc[second, "vix_futures_contango_pct"], 4.85)
        self.assertTrue(pd.isna(frame.loc[second, "vix_ratio_contango_pct"]))
        self.assertEqual(frame.loc[second, "pct_20ma"], 61.0)

    def test_official_frame_has_priority_over_database(self):
        index = pd.to_datetime(["2026-01-09"])
        database = pd.DataFrame({"tga_b": [600.0]}, index=index)
        official = pd.DataFrame({"tga_b": [625.0]}, index=index)
        merged = merge_official_frame(database, official)
        self.assertEqual(merged.loc[index[0], "tga_b"], 625.0)

    def test_asof_alignment_preserves_native_observation_dates(self):
        target = pd.bdate_range("2026-01-05", periods=5)
        native = pd.Series(
            [10.0, 12.0], index=[target[0], target[3]], dtype=float)
        aligned = OfficialLiquiditySources._asof_column(
            target, native, "sample")
        self.assertEqual(aligned.loc[target[2], "sample"], 10.0)
        self.assertEqual(
            aligned.loc[target[2], "sample_source_date"], target[0])
        self.assertEqual(
            aligned.loc[target[4], "sample_source_date"], target[3])

    def test_tga_parser_paginates_and_supports_current_schema(self):
        session = FakeSession([
            {
                "data": [{
                    "record_date": "2026-01-08",
                    "close_today_bal": "600000",
                    "open_today_bal": "null",
                }],
                "meta": {"total-pages": "2"},
            },
            {
                "data": [{
                    "record_date": "2026-01-09",
                    "close_today_bal": "null",
                    "open_today_bal": "625000",
                }],
                "meta": {"total-pages": "2"},
            },
        ])
        source = OfficialLiquiditySources("unused", session=session)
        series = source.fetch_tga("2026-01-01", "2026-01-09")
        self.assertEqual(series.loc[pd.Timestamp("2026-01-08")], 600.0)
        self.assertEqual(series.loc[pd.Timestamp("2026-01-09")], 625.0)
        self.assertEqual(
            [call["params"]["page[number]"] for call in session.calls], [1, 2])

    def test_nyfed_parser_uses_documented_fields(self):
        session = FakeSession([{"refRates": [{
            "effectiveDate": "2026-01-09",
            "percentRate": "3.63",
            "percentPercentile75": "3.65",
            "percentPercentile99": "3.74",
            "volumeInBillions": "2100",
        }]}])
        source = OfficialLiquiditySources("unused", session=session)
        frame = source.fetch_nyfed_rate("sofr", "secured", limit=1)
        row = frame.loc[pd.Timestamp("2026-01-09")]
        self.assertEqual(row["rate"], 3.63)
        self.assertEqual(row["p75"], 3.65)
        self.assertEqual(row["p99"], 3.74)
        self.assertEqual(row["volume_b"], 2100.0)

    def test_default_session_has_retry_policy(self):
        source = OfficialLiquiditySources("unused")
        retries = source.session.adapters["https://"].max_retries
        self.assertEqual(retries.total, 3)
        self.assertIn(503, retries.status_forcelist)


@unittest.skipUnless(HAS_MATPLOTLIB, "matplotlib not installed")
class LiquidityChartTests(unittest.TestCase):
    def test_png_is_nonempty(self):
        frame = synthetic_frame(True)
        result = score_liquidity_frame(frame)
        history = build_liquidity_history(frame, lookback_days=40)
        path = os.path.join(tempfile.gettempdir(), "liquidity_chart_test.png")
        generated = generate_liquidity_chart(result, history, path)
        self.assertEqual(generated, path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 20_000)

    def test_missing_core_data_does_not_draw_balanced_current_point(self):
        index = pd.bdate_range("2026-01-05", periods=3)
        result = score_liquidity_frame(
            pd.DataFrame({"sofr_rate": [3.6] * 3}, index=index))
        with tempfile.TemporaryDirectory() as temp_dir:
            import matplotlib.pyplot  # Ensure pyplot wraps the real Axes methods.
            path = os.path.join(temp_dir, "nested", "missing.png")
            with patch("matplotlib.axes.Axes.scatter") as scatter:
                generated = generate_liquidity_chart(result, output_path=path)
            scatter.assert_not_called()
            self.assertEqual(generated, path)
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
