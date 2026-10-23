import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core_funding_context import SOURCES, build_context
from core_funding_index import rolling_base_effect
from core_funding_sources import SOURCE_VERSION, digest_rows
from liquidity_monitor import (COMPONENTS, PILLARS, _percentile_rank,
                               _score_component, compute_liquidity_monitor,
                               score_liquidity_frame)
from test_liquidity_monitor import synthetic_frame


class FundingRevisionTests(unittest.TestCase):
    def test_actual_base_rolloff_bridge(self):
        frame = pd.DataFrame({"net": [5896.482, 5749.465, 5859.439, 5869.615]},
                             index=pd.to_datetime(["2026-09-09", "2026-09-16", "2026-10-07", "2026-10-14"]))
        bridge = rolling_base_effect(frame, pd.Timestamp("2026-10-15"))["4w"]
        self.assertAlmostEqual(bridge["current_week_b"], 10.176)
        self.assertAlmostEqual(bridge["base_rolloff_b"], 147.017)
        self.assertAlmostEqual(bridge["rolling_change_delta_b"],
                               bridge["current_window_b"] - bridge["previous_window_b"])
        self.assertTrue(bridge["base_effect_dominant"])
        missing = rolling_base_effect(frame.drop(pd.Timestamp("2026-09-16")), pd.Timestamp("2026-10-15"))
        self.assertEqual(missing["4w"]["status"], "MISSING_WINDOW")

    def test_native_frequency_has_one_vote_per_source_date(self):
        spec = replace(next(s for s in COMPONENTS if s.key == "reserves_buffer"), min_obs=3)
        index = pd.bdate_range("2026-08-03", periods=15)
        sparse = pd.DataFrame({spec.column: [10., 20., 15.],
                               spec.column + "_source_date": index[::5]}, index=index[::5])
        dense = sparse.reindex(index).ffill()
        a = _score_component(sparse, spec, index[-1])
        b = _score_component(dense, spec, index[-1])
        self.assertEqual(a.score, b.score)
        self.assertEqual(a.valid_obs_count, 3)
        self.assertEqual(a.valid_obs_count, b.valid_obs_count)

    def test_invalid_native_dates_fail_closed(self):
        spec = next(s for s in COMPONENTS if s.key == "m2_growth")
        index = pd.bdate_range("2026-08-03", periods=5)
        frame = pd.DataFrame({spec.column: 5., spec.column + "_source_date": pd.Timestamp("2027-01-01")}, index=index)
        self.assertIsNone(_score_component(frame, spec, index[-1]).score)

    def test_tied_rates_use_midpoint(self):
        self.assertEqual(_percentile_rank(pd.Series([1., 2., 2., 3., 2.])), 50.)

    def test_monthly_freshness_starts_at_month_end_not_month_start(self):
        spec = replace(next(s for s in COMPONENTS if s.key == "m2_growth"), min_obs=3)
        index = pd.to_datetime(["2026-06-30", "2026-07-31", "2026-08-31"])
        frame = pd.DataFrame({spec.column: [2., 3., 4.],
                              spec.column + "_source_date": pd.to_datetime(["2026-06-01", "2026-07-01", "2026-08-01"])}, index=index)
        result = _score_component(frame, spec, pd.Timestamp("2026-10-22"))
        self.assertIsNotNone(result.score)
        self.assertEqual(result.source_date, "2026-08-01")
        self.assertLess(result.lag_days, 45)
        self.assertIsNone(_score_component(frame, spec, pd.Timestamp("2026-12-01")).score)

    def test_calendar_window_missing_is_not_compressed(self):
        frame = synthetic_frame()
        spec = next(s for s in COMPONENTS if s.key == "net_liq_20d")
        frame = frame.drop(frame.index[-1] - pd.offsets.BDay(20))
        value = _score_component(frame, spec, frame.index[-1])
        self.assertIsNone(value.score)
        self.assertEqual(value.note, "missing exact calendar window")

    def test_fixed_weights_do_not_use_quality(self):
        frame = synthetic_frame()
        baseline = score_liquidity_frame(frame)
        original = _score_component
        def varying_quality(*args):
            component = original(*args)
            component.quality = .2 if component.key.endswith("level") else .9
            return component
        with patch("liquidity_monitor._score_component", side_effect=varying_quality):
            result = score_liquidity_frame(frame)
        self.assertEqual(result.composite, baseline.composite)
        self.assertNotEqual(result.confidence, baseline.confidence)
        weights = result.to_row()["details"]["pillar_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1)
        self.assertAlmostEqual(result.composite, sum(weights[k] * p.score for k, p in result.pillars.items()), places=1)

    def test_missing_component_does_not_reweight_formal_composite(self):
        frame = synthetic_frame().drop(columns=["skew"])
        result = score_liquidity_frame(frame)
        self.assertIsNone(result.composite)
        self.assertIsNotNone(result.observation_composite)
        self.assertFalse(result.to_row()["details"]["observation_comparable"])

    def test_context_only_series_cannot_change_score(self):
        frame = synthetic_frame()
        initial = score_liquidity_frame(frame)
        for column in ("nfci", "move", "hyg_tlt_ratio", "tga_b", "fed_assets_b"):
            frame[column] = frame[column] * 10
        self.assertEqual(initial.composite, score_liquidity_frame(frame).composite)

    def test_old_issued_waterline_snapshot_is_not_overwritten(self):
        frame = synthetic_frame()
        client = Mock()
        query = client.table.return_value
        query.upsert.return_value.execute.return_value = Mock()
        query.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"calc_version": "liquidity_v2.3", "details": {}}]
        with patch("liquidity_monitor.build_database_frame", return_value=frame), patch("market_utils.log_data_quality"):
            result, _ = compute_liquidity_monitor(client, str(frame.index[-1].date()))
        self.assertEqual(result.persistence_status, "existing_snapshot_preserved")
        self.assertTrue(query.upsert.call_args.kwargs["ignore_duplicates"])

    def test_optional_sources_preserve_dates_and_missing_state(self):
        def observation(key, value, date="2026-10-14"):
            return {"series": key, "observation_date": date, "value": value,
                    "release_date": "2026-10-15", "available_at": "2026-10-16T04:00:00+00:00",
                    "source": SOURCES[key].fred_id, "source_version": SOURCE_VERSION}
        rows = [observation("fed_securities", 6000), observation("sofr_volume", 2500)]
        archive = {"version": SOURCE_VERSION, "rows": rows, "digest": digest_rows(rows)}
        before = build_context(archive, [], "2026-10-15T16:50:00-04:00")
        self.assertEqual(before["status"], "UNAVAILABLE")
        after = build_context(archive, [], "2026-10-16T16:50:00-04:00")
        self.assertFalse(after["score_eligible"])
        self.assertEqual(after["observations"]["sofr_volume"]["value"], 2500)
        self.assertEqual(after["observations"]["loan_demand"]["status"], "MISSING")
        self.assertEqual(after["fed_asset_composition_1w"]["status"], "MISSING_COMMON_WINDOW")
        stale = build_context(archive, [], "2026-11-16T16:50:00-05:00")
        self.assertEqual(stale["observations"]["sofr_volume"]["status"], "STALE")


if __name__ == "__main__":
    unittest.main()
