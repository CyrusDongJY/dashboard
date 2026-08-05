import unittest

import pandas as pd

from data_contracts import (
    classify_vix_curve,
    calculate_iv_rank_percentile,
    classify_repeated_value,
    classify_hyg_tlt,
    concentration_attribution,
    compute_etf_share_metrics,
    gap_acceptance,
    opening_probability,
    oi_verification_quality,
    option_trade_side,
    up_down_volume_ratio,
    volume_profile_nodes,
    vwap_acceptance,
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

    def test_third_identical_observation_is_stale(self):
        result = classify_repeated_value(1117, [1117, 1117])
        self.assertEqual(result['status'], 'STALE_VALUE')
        self.assertEqual(result['repeat_count'], 3)


class IVDefinitionTests(unittest.TestCase):
    def test_iv_rank_and_percentile_are_distinct(self):
        metrics = calculate_iv_rank_percentile([10, 11, 12, 19, 20, 15])
        self.assertAlmostEqual(metrics["iv_rank_pct"], 50.0)
        self.assertAlmostEqual(metrics["iv_percentile_pct"], 50.0)

        skewed = calculate_iv_rank_percentile([10, 10.5, 11, 19, 20, 12])
        self.assertAlmostEqual(skewed["iv_rank_pct"], 20.0)
        self.assertAlmostEqual(skewed["iv_percentile_pct"], 50.0)


class AcceptanceTests(unittest.TestCase):
    def test_gap_acceptance_expansion_and_small_gap_guard(self):
        expanded = gap_acceptance(100, 101, 101.25, atr20=2)
        self.assertEqual(expanded['quality'], 'OK')
        self.assertEqual(expanded['state'], 'EXPANDED')
        self.assertAlmostEqual(expanded['acceptance_ratio'], 1.25)

        small = gap_acceptance(100, 100.05, 101, atr20=2)
        self.assertEqual(small['quality'], 'SMALL_GAP')
        self.assertIsNone(small['acceptance_ratio'])

    def test_vwap_acceptance_uses_contemporaneous_cumulative_vwap(self):
        result = vwap_acceptance([
            {'high': 100.2, 'low': 99.8, 'close': 100, 'volume': 100},
            {'high': 101.2, 'low': 100.8, 'close': 101, 'volume': 100},
            {'high': 102.2, 'low': 101.8, 'close': 102, 'volume': 100},
        ], expected_samples=3)
        self.assertEqual(result['quality'], 'OK')
        self.assertAlmostEqual(result['time_acceptance_pct'], 2 / 3 * 100)
        self.assertAlmostEqual(result['volume_acceptance_pct'], 2 / 3 * 100)

    def test_vwap_duplicate_bars_cannot_claim_full_quality(self):
        result = vwap_acceptance([
            {'date': '2026-07-28 09:30', 'close': 100, 'volume': 100},
            {'date': '2026-07-28 09:35', 'close': 101, 'volume': 100},
            {'date': '2026-07-28 09:35', 'close': 102, 'volume': 100},
        ], expected_samples=2)
        self.assertEqual(result['quality'], 'DUPLICATE_BARS')
        self.assertEqual(result['duplicate_samples'], 1)

    def test_missing_reversal_window_is_partial_and_listed(self):
        bars = [
            {'date': timestamp, 'close': 100, 'volume': 100}
            for timestamp in pd.date_range(
                '2026-07-28 09:30', periods=78, freq='5min')
            if not (timestamp.hour == 10 and timestamp.minute >= 30)
            and not (timestamp.hour == 11 and timestamp.minute == 0)
        ]
        result = vwap_acceptance(bars, expected_samples=78)
        self.assertEqual(result['quality'], 'PARTIAL_COVERAGE')
        self.assertIn('2026-07-28T10:30:00', result['missing_intervals'])

    def test_volume_profile_reports_three_nodes_and_bin_contract(self):
        result = volume_profile_nodes([
            {'date': f'2026-07-{20 + index:02d}', 'close': 100 + index,
             'volume': (index + 1) * 100}
            for index in range(5)
        ], bins=5, top_n=3)
        self.assertEqual(result['quality'], 'OK')
        self.assertEqual(len(result['nodes']), 3)
        self.assertGreater(result['bin_width'], 0)

    def test_hyg_tlt_label_does_not_call_rate_driven_ratio_full_risk_on(self):
        self.assertEqual(
            classify_hyg_tlt(1.0, -2.0, 3.0),
            'CREDIT_STRONG_RATE_DRIVEN')

    def test_concentration_attribution_uses_exclusive_weight_differences(self):
        result = concentration_attribution(
            {'NVDA': 2.0, 'AMD': 1.0},
            {'NVDA': 0.7, 'AMD': 0.3},
            {'NVDA': 0.5, 'AMD': 0.5},
            {'NVDA': 'MAG7', 'AMD': 'SEMIS_EX_MAG7'},
        )
        self.assertEqual(result['quality'], 'OK')
        self.assertAlmostEqual(result['spread_pct'], 0.2)
        self.assertAlmostEqual(
            result['group_contributions_pct']['MAG7'], 0.4)
        self.assertAlmostEqual(
            result['group_contributions_pct']['SEMIS_EX_MAG7'], -0.2)


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

    def test_three_unchanged_share_observations_are_na_not_zero_flow(self):
        history = pd.DataFrame({
            'Date': pd.bdate_range('2026-07-24', periods=3),
            'Ticker': ['SPY'] * 3,
            'Shares': [100_000_000] * 3,
            'Price': [700.0, 701.0, 702.0],
        })
        metric = compute_etf_share_metrics(history)['SPY']
        self.assertEqual(metric['quality'], 'STALE_UNCHANGED')
        self.assertIsNone(metric['changes']['1d'])


class OptionFlowVerificationTests(unittest.TestCase):
    def test_trade_side_and_next_day_oi_are_labeled_as_heuristics(self):
        self.assertEqual(option_trade_side(1.19, 1.00, 1.20), 'ASK_SIDE')
        self.assertEqual(opening_probability(1000, 700), 'HIGH')
        self.assertEqual(opening_probability(1000, -200), 'LOW')

    def test_low_next_day_oi_coverage_disables_structure_inference(self):
        result = oi_verification_quality(6, 37)
        self.assertEqual(result['quality'], 'LOW_COVERAGE')
        self.assertAlmostEqual(result['coverage_pct'], 6 / 37 * 100)
        self.assertFalse(result['structure_inference_eligible'])

    def test_vix_curve_has_flat_dead_band(self):
        self.assertEqual(classify_vix_curve(18.00, 18.00)[0], 'FLAT')
        self.assertEqual(classify_vix_curve(18.00, 18.08)[0], 'FLAT')
        self.assertEqual(classify_vix_curve(18.00, 18.20)[0], 'CONTANGO')
        self.assertEqual(classify_vix_curve(18.20, 18.00)[0], 'BACKWARDATION')


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
