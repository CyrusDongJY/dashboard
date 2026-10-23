import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from core_funding_index import (
    CALC_VERSION, COMPONENTS, GROUPS, ObservationStore, _component_score,
    _growth, add_trend, build_core_history, build_features, compute_core_funding,
)
from core_funding_pipeline import market_sessions, persist_result, run_core_funding
from core_funding_report import format_core_funding, generate_core_funding_chart, rebase_benchmarks
from core_funding_sources import CoreFundingSources, SOURCES, SOURCE_VERSION, digest_rows


def observation(key, date, value, release=None, available=None):
    date = pd.Timestamp(date)
    release = pd.Timestamp(release) if release is not None else date + pd.Timedelta(days=1)
    available = (pd.Timestamp(available) if available is not None else
                 (release + pd.Timedelta(days=1)).tz_localize("America/New_York"))
    return {"series": key, "observation_date": str(date.date()), "value": float(value),
            "release_date": str(release.date()), "available_at": available.tz_convert("UTC").isoformat(),
            "source": SOURCES[key].fred_id, "source_version": SOURCE_VERSION}


def fixture_rows():
    """Artificial test observations, not captured market history."""
    records = []
    for key, spec in SOURCES.items():
        freq = {"daily": "B", "weekly": "W-WED", "monthly": "MS", "quarterly": "QS"}[spec.frequency]
        dates = pd.date_range("2014-01-01", "2026-09-23", freq=freq)
        for i, date in enumerate(dates):
            values = {
                "fed_assets": 5000 + i * 3 + 100 * np.sin(i / 13),
                "tga": 600 + 100 * np.cos(i / 8),
                "rrp": 250 + 100 * np.sin(i / 35),
                "reserves": 2000 + i + 100 * np.sin(i / 20),
                "bank_assets": 15000 + i * 8,
                "m2": 10000 + i * 50 + 100 * np.sin(i / 11),
                "sofr": 4.9 + .04 * np.sin(i / 20),
                "effr": 4.85 + .02 * np.sin(i / 25),
                "iorb": 4.9,
                "sofr99": 5.05 + .07 * np.sin(i / 20),
                "loans": 7000 + i * 4 + 80 * np.sin(i / 17),
                "deposits": 11000 + i * 7,
                "loan_standards": 10 + 20 * np.sin(i / 4),
            }
            lag = (45 if spec.frequency == "monthly" else 35 if spec.frequency == "quarterly"
                   else 9 if key in {"bank_assets", "loans", "deposits"} else 1)
            records.append(observation(key, date, values[key], release=date + pd.Timedelta(days=lag)))
    return records


class CoreFundingEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = fixture_rows()
        cls.store = ObservationStore(cls.rows)
        cls.cutoff = "2026-09-24T16:45:00-04:00"
        cls.result = compute_core_funding(cls.store, cls.cutoff)

    def test_complete_fixture_produces_fixed_weight_score(self):
        result = self.result
        self.assertIsNotNone(result["score"])
        self.assertEqual(result["coverage"], 1)
        self.assertAlmostEqual(result["score"], sum(
            group["score"] * .25 for group in result["groups"].values()), places=2)
        self.assertFalse(result["decision_eligible"])
        self.assertTrue(result["shadow_mode"])

    def test_empty_inputs_are_not_healthy(self):
        result = compute_core_funding([], self.cutoff)
        self.assertIsNone(result["score"])
        self.assertEqual(result["coverage"], 0)
        self.assertEqual(result["funding_pressure"]["state"], "UNAVAILABLE")

    def test_unshifted_m2_reference_uses_published_month_and_is_not_a_score_input(self):
        reference = self.result["m2_unshifted_reference"]
        self.assertIsNotNone(reference["score"])
        self.assertFalse(reference["score_eligible"])
        self.assertEqual(reference["m2_component"]["source_date"], "2026-08-01")

    def test_missing_component_never_renormalizes_weights(self):
        result = compute_core_funding([r for r in self.rows if r["series"] != "m2"], self.cutoff)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["groups"]["stock"]["score"])
        self.assertAlmostEqual(result["coverage"], .9)

    def test_future_releases_cannot_change_past_result(self):
        changed = copy.deepcopy(self.rows)
        changed.append(observation("m2", "2026-10-01", 1000000,
                                   release="2026-10-25"))
        actual = compute_core_funding(changed, self.cutoff)
        self.assertEqual(actual["score"], self.result["score"])
        self.assertEqual(actual["input_digest"], self.result["input_digest"])

    def test_available_time_not_observation_date_controls_inclusion(self):
        rows = [observation("sofr", "2026-09-23", 5, release="2026-09-24",
                            available="2026-09-24T17:00:00-04:00")]
        result = compute_core_funding(rows, self.cutoff)
        self.assertEqual(result["components"]["sofr_iorb"]["status"], "MISSING")
        self.assertEqual(result["input_digest"], digest_rows([]))

    def test_naive_asof_rejected(self):
        with self.assertRaises(ValueError):
            compute_core_funding([], "2026-09-24")

    def test_conflicting_duplicates_rejected(self):
        rows = [observation("m2", "2020-01-01", 100), observation("m2", "2020-01-01", 200)]
        with self.assertRaises(ValueError):
            compute_core_funding(rows, self.cutoff)

    def test_exact_duplicates_do_not_inflate_native_samples(self):
        result = compute_core_funding(self.rows + self.rows, self.cutoff)
        self.assertEqual(result["components"], self.result["components"])

    def test_invalid_source_or_provenance_rejected(self):
        row = observation("m2", "2020-01-01", 100)
        row["source"] = "FAKE"
        with self.assertRaises(ValueError):
            compute_core_funding([row], self.cutoff)
        row["source"] = "M2SL"
        row["available_at"] = "2019-12-31T00:00:00Z"
        with self.assertRaises(ValueError):
            compute_core_funding([row], self.cutoff)

    def test_preannounced_policy_rate_still_waits_for_effective_date(self):
        row = observation("iorb", "2026-09-25", 3.5, release="2026-09-23")
        self.assertEqual(compute_core_funding([row], self.cutoff)["input_digest"], digest_rows([]))

    def test_daily_copies_of_monthly_data_rejected(self):
        row = observation("m2", "2026-08-02", 21000)
        with self.assertRaises(ValueError):
            compute_core_funding([row], self.cutoff)

    def test_daily_copies_of_weekly_data_rejected(self):
        row = observation("fed_assets", "2026-09-22", 6500)
        with self.assertRaises(ValueError):
            compute_core_funding([row], self.cutoff)

    def test_stale_funding_suppresses_total_and_pressure_health(self):
        rows = [row for row in self.rows if row["series"] not in {"sofr", "effr", "sofr99"}
                or row["observation_date"] < "2026-09-01"]
        result = compute_core_funding(rows, self.cutoff)
        self.assertIsNone(result["score"])
        self.assertEqual(result["funding_pressure"]["state"], "UNAVAILABLE")

    def test_pressure_override_does_not_require_mature_composite(self):
        rows = [observation("sofr", "2026-09-22", 6), observation("iorb", "2026-09-22", 4.9)]
        result = compute_core_funding(rows, self.cutoff)
        self.assertIsNone(result["score"])
        self.assertEqual(result["funding_pressure"]["state"], "ACUTE")

    def test_normal_small_spread_does_not_trigger_on_rank_alone(self):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            if row["observation_date"] >= "2026-09-21" and row["series"] in {"sofr", "effr", "sofr99"}:
                row["value"] = 4.93 if row["series"] != "sofr99" else 5.02
        result = compute_core_funding(rows, self.cutoff)
        self.assertEqual(result["funding_pressure"]["state"], "NOT_TRIGGERED")

    def test_m2_has_monthly_not_daily_observation_count(self):
        count = self.result["components"]["m2_yoy"]["prior_native_observations"]
        self.assertEqual(count, 120)
        shortened = [r for r in self.rows if r["series"] != "m2" or r["observation_date"] >= "2025-01-01"]
        result = compute_core_funding(shortened, self.cutoff)
        self.assertEqual(result["components"]["m2_yoy"]["status"], "WARMUP")
        self.assertLess(result["components"]["m2_yoy"]["prior_native_observations"], 12)

    def test_m2_score_uses_shifted_month_and_preserves_source_date(self):
        item = self.result["components"]["m2_yoy"]
        values = {row["observation_date"]: row["value"] for row in self.rows if row["series"] == "m2"}
        self.assertAlmostEqual(item["value"], 100 * (values["2026-05-01"] / values["2025-05-01"] - 1))
        self.assertEqual(item["source_date"], "2026-05-01")
        self.assertEqual(item["observation_month"], "2026-05")
        self.assertEqual(item["effective_date"], "2026-08-31")
        self.assertEqual(item["expected_observation_month"], "2026-05")
        self.assertEqual(item["effective_age_days"], 24)
        self.assertGreater(item["age_days"], 100)
        self.assertEqual(item["status"], "OK")
        self.assertEqual(item["growth_basis"], "YOY")
        self.assertEqual(item["lag_months"], 3)
        self.assertEqual(item["validation"], "USER_SPECIFIED_UNVALIDATED_HYPOTHESIS")
        self.assertEqual(self.result["calc_version"], "core_funding_v1.2.1_m2lag3")
        series = pd.Series(values, dtype=float)
        series.index = pd.to_datetime(series.index)
        delayed = _growth(series.sort_index(), months=12).loc[:"2026-05-01"]
        delayed.index = (delayed.index.to_period("M") + 3).to_timestamp(how="end").normalize()
        spec = next(c for c in COMPONENTS if c.key == "m2_yoy")
        expected = _component_score(delayed, spec, pd.Timestamp("2026-09-24"))
        self.assertEqual(item["score"], expected["score"])
        self.assertEqual(item["prior_native_observations"], expected["prior_native_observations"])

    def test_known_but_not_yet_effective_m2_cannot_change_score_or_baseline(self):
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["series"] == "m2" and row["observation_date"] >= "2026-06-01":
                row["value"] *= 10
        result = compute_core_funding(changed, self.cutoff)
        self.assertEqual(result["components"], self.result["components"])
        self.assertEqual(result["score"], self.result["score"])

    def test_m2_delay_uses_new_york_calendar_month_end_including_leap_year(self):
        before = compute_core_funding(self.store, "2024-02-29T04:59:59Z")
        after = compute_core_funding(self.store, "2024-02-29T05:00:00Z")
        self.assertEqual(before["components"]["m2_yoy"]["observation_month"], "2023-10")
        self.assertEqual(after["components"]["m2_yoy"]["observation_month"], "2023-11")
        self.assertEqual(after["components"]["m2_yoy"]["effective_date"], "2024-02-29")
        january = compute_core_funding(self.store, "2026-04-30T16:50:00-04:00")
        self.assertEqual(january["components"]["m2_yoy"]["observation_month"], "2026-01")
        self.assertEqual(january["components"]["m2_yoy"]["effective_date"], "2026-04-30")

    def test_m2_late_publication_does_not_become_visible_at_assumed_date(self):
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["series"] == "m2" and row["observation_date"] == "2026-05-01":
                row["release_date"] = "2026-09-01"
                row["available_at"] = "2026-09-01T14:00:00-04:00"
        before = compute_core_funding(changed, "2026-09-01T13:59:59-04:00")
        self.assertEqual(before["components"]["m2_yoy"]["status"], "MISSING_WINDOW")
        self.assertIsNone(before["score"])
        after = compute_core_funding(changed, "2026-09-01T14:00:00-04:00")
        item = after["components"]["m2_yoy"]
        self.assertEqual(item["status"], "OK")
        self.assertEqual(item["observation_month"], "2026-05")
        self.assertEqual(item["eligible_from"], "2026-09-01T18:00:00+00:00")

    def test_missing_target_m2_or_yoy_base_does_not_substitute_another_month(self):
        for missing in ("2026-05-01", "2025-05-01"):
            with self.subTest(missing=missing):
                rows = [row for row in self.rows if not (row["series"] == "m2" and row["observation_date"] == missing)]
                result = compute_core_funding(rows, self.cutoff)
                item = result["components"]["m2_yoy"]
                self.assertEqual(item["status"], "MISSING_WINDOW")
                self.assertEqual(item["expected_observation_month"], "2026-05")
                self.assertIsNone(item["score"])
                self.assertIsNone(result["score"])
                self.assertAlmostEqual(result["coverage"], .9)

    def test_m2_shift_does_not_move_other_components_or_mutate_raw_rows(self):
        before = copy.deepcopy(self.rows)
        result = compute_core_funding(self.rows, self.cutoff)
        self.assertEqual(self.rows, before)
        from core_funding_index import visible_series
        native, _ = visible_series(self.store, self.cutoff)
        features, _ = build_features(native)
        for spec in COMPONENTS:
            if spec.key != "m2_yoy":
                self.assertEqual(result["components"][spec.key], _component_score(
                    features[spec.key], spec, pd.Timestamp("2026-09-24")))

    def test_new_formula_never_splices_old_formula_trend(self):
        result = copy.deepcopy(self.result)
        history = [dict(self.result, report_date=str(day.date()), calc_version="core_funding_v1.0")
                   for day in market_sessions(result["report_date"], 40)[:-1]]
        add_trend(result, history)
        self.assertIsNone(result["change_20_sessions"])
        self.assertIsNone(result["historical_percentile"])

    def test_missing_month_does_not_shift_yoy_window(self):
        series = pd.Series([100, 110, 120], index=pd.to_datetime(["2024-01-01", "2025-02-01", "2025-03-01"]))
        self.assertTrue(_growth(series, months=12).isna().all())

    def test_latest_incomplete_window_does_not_silently_use_old_signal(self):
        spec = next(c for c in COMPONENTS if c.key == "m2_yoy")
        series = pd.Series([1., 2., np.nan], index=pd.date_range("2026-06-01", periods=3, freq="MS"))
        self.assertEqual(_component_score(series, spec, pd.Timestamp("2026-09-24"))["status"], "MISSING_WINDOW")

    def test_attribution_reconciles_and_only_net_flow_is_scored(self):
        for item in self.result["net_liquidity_attribution"].values():
            self.assertAlmostEqual(item["net_change_b"], item["fed_assets_b"] + item["tga_b"] + item["rrp_b"])
        self.assertEqual({c.key for c in COMPONENTS if c.group == "flow"}, {"net_flow_4w", "net_flow_13w"})
        self.assertTrue(all(c.key not in {"vix", "nfci", "m1", "spy", "qqq", "g3"} for c in COMPONENTS))

    def test_no_iorb_ioer_automatic_stitch(self):
        self.assertEqual(SOURCES["iorb"].fred_id, "IORB")
        self.assertNotIn("IOER", {spec.fred_id for spec in SOURCES.values()})

    def test_net_flow_near_zero_uses_stable_asset_denominator(self):
        native = {key: pd.Series(dtype=float, index=pd.DatetimeIndex([])) for key in SOURCES}
        dates = pd.date_range("2026-01-07", periods=16, freq="W-WED")
        native["fed_assets"] = pd.Series(6500., index=dates)
        native["tga"] = pd.Series(np.linspace(6500., 6400., 16), index=dates)
        native["rrp"] = pd.Series(0., index=dates)
        features, _ = build_features(native)
        self.assertAlmostEqual(features["net_flow_4w"].iloc[4], 100 * (100 * 4 / 15) / 6500)
        self.assertTrue(np.isfinite(features["net_flow_4w"].dropna()).all())

    def test_current_observation_is_not_in_its_own_percentile_baseline(self):
        spec = next(c for c in COMPONENTS if c.key == "m2_yoy")
        dates = pd.date_range("2023-01-01", periods=37, freq="MS")
        values = pd.Series(list(range(36)) + [100.], index=dates)
        result = _component_score(values, spec, dates[-1])
        self.assertEqual(result["score"], 100.)
        self.assertEqual(result["prior_native_observations"], 36)

    def test_payload_contains_only_json_finite_values(self):
        json.dumps(self.result, allow_nan=False)
        json.dumps(compute_core_funding([], self.cutoff), allow_nan=False)

    def test_history_is_prefix_invariant(self):
        dates = ["2026-09-21", "2026-09-22"]
        first = build_core_history(self.store, dates)
        extended = build_core_history(self.store, dates + ["2026-09-23"])
        self.assertEqual(first, extended[:2])

    def test_live_trend_never_uses_replay_snapshots(self):
        result = copy.deepcopy(self.result)
        result["observation_mode"] = "LIVE_SHADOW"
        history = [dict(self.result, report_date=str(date.date()))
                   for date in pd.bdate_range("2026-08-01", periods=30)]
        add_trend(result, history)
        self.assertIsNone(result["change_20_sessions"])

    def test_trend_excludes_missing_points_and_requires_confirmation(self):
        result = copy.deepcopy(self.result)
        history = []
        for date in market_sessions(result["report_date"], 31)[:-1]:
            prior = copy.deepcopy(result)
            prior["report_date"] = str(date.date())
            history.append(prior)
        for prior in history:
            prior["score"] -= 10
            for group in prior["groups"].values():
                group["score"] -= 10
        add_trend(result, history)
        self.assertEqual(result["change_20_sessions"], 10)
        self.assertEqual(result["trend"], "IMPROVING_CANDIDATE")
        self.assertAlmostEqual(sum(result["score_change_attribution"].values()), 10)

    def test_missing_session_does_not_turn_twenty_rows_into_twenty_sessions(self):
        result = copy.deepcopy(self.result)
        history = [dict(self.result, report_date=str(day.date()))
                   for day in market_sessions(result["report_date"], 40)[:-1]]
        history.pop(-5)
        add_trend(result, history)
        self.assertIsNone(result["change_20_sessions"])

    def test_text_discloses_scoring_direction_and_shadow_contract(self):
        text = format_core_funding(self.result)
        self.assertIn("不触发预警", text)
        self.assertIn("首次发布", text)
        self.assertIn("延后3个自然月计分", text)
        self.assertIn("总权重10%", text)
        self.assertIn("2026-05", text)
        self.assertIn("不是现金比例", text)


class SourceAdapterTests(unittest.TestCase):
    def source(self, pages):
        session = Mock()
        session.headers = {}
        responses = []
        for page in pages:
            response = Mock()
            response.json.return_value = page
            responses.append(response)
        session.get.side_effect = responses
        return CoreFundingSources("test-not-a-secret", session=session), session

    def test_first_release_query_pagination_and_units(self):
        source, session = self.source([
            {"count": 2, "observations": [{"date": "2026-09-09", "realtime_start": "2026-09-10", "value": "6500000"}]},
            {"count": 2, "observations": [{"date": "2026-09-16", "realtime_start": "2026-09-17", "value": "6501000"}]},
        ])
        rows = source.fetch_initial_releases("fed_assets", "2020-01-01", "2026-09-24T20:45:00Z")
        self.assertEqual(rows[0]["value"], 6500)
        self.assertEqual(rows[0]["available_at"], "2026-09-11T04:00:00+00:00")
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["output_type"], 4)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["offset"], 1)

    def test_actual_capture_can_prove_same_day_availability(self):
        source, _ = self.source([{"count": 1, "observations": [
            {"date": "2026-09-23", "realtime_start": "2026-09-24", "value": "4.9"}]}])
        rows = source.fetch_initial_releases("sofr", "2026-01-01", "2026-09-24T20:45:00Z")
        self.assertEqual(rows[0]["available_at"], "2026-09-24T20:45:00+00:00")

    def test_vintage_limit_splits_without_replacing_initial_values(self):
        source, session = self.source([])
        limit = Mock(status_code=400)
        limit.json.return_value = {"error_message":
            "There are 2608 vintage dates. This exceeds the maximum number allowed (2000)."}
        early = Mock(status_code=200)
        early.json.return_value = {"count": 1, "observations": [
            {"date": "2020-01-02", "realtime_start": "2020-01-03", "value": "10"}]}
        late = Mock(status_code=200)
        late.json.return_value = {"count": 2, "observations": [
            {"date": "2020-01-02", "realtime_start": "2021-09-25", "value": "99"},
            {"date": "2026-09-23", "realtime_start": "2026-09-24", "value": "20"}]}
        session.get.side_effect = [limit, early, late]
        rows = source.fetch_initial_releases("rrp", "2008-01-01", "2026-09-24T20:45:00Z")
        self.assertEqual([r["value"] for r in rows], [10, 20])
        first = session.get.call_args_list[1].kwargs["params"]
        second = session.get.call_args_list[2].kwargs["params"]
        self.assertEqual(first["realtime_start"], "1776-07-04")
        self.assertEqual(first["realtime_end"], "2021-09-24")
        self.assertEqual(second["realtime_start"], "2021-09-25")
        self.assertEqual(second["realtime_end"], "2026-09-24")
        self.assertTrue(all(c.kwargs["params"]["output_type"] == 4 for c in session.get.call_args_list))

    def test_same_vintage_conflict_is_rejected(self):
        source, _ = self.source([{"count": 2, "observations": [
            {"date": "2026-09-23", "realtime_start": "2026-09-24", "value": "4.9"},
            {"date": "2026-09-23", "realtime_start": "2026-09-24", "value": "5.9"}]}])
        with self.assertRaises(ValueError):
            source.fetch_initial_releases("sofr", "2026-01-01", "2026-09-24T20:45:00Z")

    def test_same_wednesday_tga_contract(self):
        self.assertEqual(SOURCES["tga"].fred_id, "WDTGAL")
        self.assertNotIn("WTREGEN", {spec.fred_id for spec in SOURCES.values()})

    def test_failed_pagination_is_not_partial_success(self):
        source, _ = self.source([{"count": 5, "observations": []}])
        with self.assertRaises(ValueError):
            source.fetch_initial_releases("sofr", "2020-01-01", "2026-09-24T20:45:00Z")

    def test_missing_first_release_timestamp_is_not_backdated(self):
        source, _ = self.source([{"count": 1, "observations": [
            {"date": "2026-09-23", "value": "4.9"}]}])
        with self.assertRaises(KeyError):
            source.fetch_initial_releases("sofr", "2026-01-01", "2026-09-24T20:45:00Z")

    def test_cache_roundtrip_and_failure_preserve_original_availability(self):
        source, _ = self.source([])
        key = "sofr"
        row = observation(key, "2026-09-22", 4.9)
        with tempfile.TemporaryDirectory() as directory, patch(
                "core_funding_sources.SOURCES", {key: SOURCES[key]}):
            with patch.object(source, "fetch_initial_releases", return_value=[row]):
                first = source.collect(directory, now="2026-09-24T20:45:00Z")
            with patch.object(source, "fetch_initial_releases", side_effect=RuntimeError("offline")) as fetch:
                cached = source.collect(directory, now="2026-09-24T21:45:00Z")
                fetch.assert_not_called()
                fallback = source.collect(directory, now="2026-09-25T20:45:00Z")
            self.assertEqual(first["rows"], cached["rows"])
            self.assertEqual(first["rows"], fallback["rows"])
            self.assertEqual(fallback["source_status"][key], "CACHED_AFTER_ERROR")


class ReportPersistenceTests(unittest.TestCase):
    def test_benchmarks_rebase_on_common_date_without_fill(self):
        prices = pd.DataFrame({"SPY": [100., 102., 104.], "QQQ": [np.nan, 200., 210.]},
                              index=pd.bdate_range("2026-09-21", periods=3))
        result = rebase_benchmarks(prices)
        self.assertEqual(len(result), 2)
        self.assertTrue((result.iloc[0] == 100).all())
        self.assertEqual(result["QQQ"].iloc[-1], 105)
        self.assertTrue(rebase_benchmarks(prices[["SPY"]]).empty)

    def test_persistence_readback_wins_and_duplicate_is_not_updated(self):
        result = compute_core_funding([], "2026-09-24T16:45:00-04:00", "LIVE_SHADOW")
        stored = copy.deepcopy(result)
        stored["as_of"] = "2026-09-24T20:30:00+00:00"
        client = Mock()
        query = client.table.return_value
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.execute.return_value.data = [{"result": stored}]
        saved = persist_result(client, result)
        self.assertEqual(saved["as_of"], stored["as_of"])
        self.assertEqual(saved["persistence_status"], "VERIFIED")
        self.assertTrue(query.upsert.call_args.kwargs["ignore_duplicates"])
        self.assertIn("observation_mode", query.upsert.call_args.kwargs["on_conflict"])

    def test_write_failure_is_explicit(self):
        result = compute_core_funding([], "2026-09-24T16:45:00-04:00")
        client = Mock()
        client.table.side_effect = RuntimeError("no migration")
        self.assertEqual(persist_result(client, result)["persistence_status"], "FAILED")

    def test_empty_readback_does_not_claim_persistence(self):
        result = compute_core_funding([], "2026-09-24T16:45:00-04:00")
        client = Mock()
        query = client.table.return_value
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.execute.return_value.data = []
        self.assertEqual(persist_result(client, result)["persistence_status"], "FAILED")

    def test_bad_archive_digest_rejected_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_core_funding(archive={"rows": [], "version": SOURCE_VERSION, "digest": "wrong"},
                                 as_of="2026-09-24T16:45:00-04:00", output_dir=directory)

    def test_replay_failure_survives_existing_snapshot_readback(self):
        archive = {"rows": [], "version": SOURCE_VERSION, "digest": digest_rows([])}
        saved = compute_core_funding([], "2026-09-24T16:45:00-04:00")
        saved["persistence_status"] = "VERIFIED"
        responses = [{**saved, "persistence_status": "FAILED"}] * 21 + [saved]
        with tempfile.TemporaryDirectory() as directory, patch(
                "core_funding_pipeline.persist_result", side_effect=responses), patch(
                "core_funding_pipeline.fetch_issued_history", return_value=[]), patch(
                "core_funding_pipeline.generate_core_funding_chart", return_value=None):
            output = run_core_funding(archive=archive, client=Mock(), output_dir=directory,
                                      as_of="2026-09-24T16:45:00-04:00", sessions=21,
                                      write_replay=True, prices=pd.DataFrame())
            self.assertEqual(output["result"]["replay_write_status"], "FAILED")

    def test_offline_pipeline_chart_and_archive(self):
        rows = [observation("m2", "2026-01-01", 21000)]
        archive = {"version": SOURCE_VERSION, "rows": rows, "digest": digest_rows(rows)}
        with tempfile.TemporaryDirectory() as directory, patch("core_funding_pipeline.fetch_adjusted_benchmarks") as prices:
            output = run_core_funding(archive=archive, prices=pd.DataFrame(), output_dir=directory,
                                      as_of="2026-09-24T16:45:00-04:00", sessions=21)
            prices.assert_not_called()
            self.assertTrue(Path(output["chart"]).is_file())
            self.assertGreater(Path(output["chart"]).stat().st_size, 15000)
            from PIL import Image
            pixels = np.asarray(Image.open(output["chart"]).convert("RGB"))
            self.assertGreater(pixels.std(), 10)
            self.assertTrue(Path(output["result"]["archive_path"]).is_file())
            self.assertIsNone(output["result"]["score"])

    def test_m2_clock_failure_cannot_change_funding_result(self):
        archive = {"version": SOURCE_VERSION, "rows": [], "digest": digest_rows([])}
        stored = compute_core_funding(fixture_rows(), "2026-09-24T16:45:00-04:00")
        self.assertIsNotNone(stored["score"])
        with tempfile.TemporaryDirectory() as directory, patch(
                "core_funding_pipeline.build_m2_transmission", side_effect=RuntimeError("fixture failure")), patch(
                "core_funding_pipeline.persist_result", return_value=stored), patch(
                "core_funding_pipeline.generate_core_funding_chart", return_value=None):
            output = run_core_funding(archive=archive, prices=pd.DataFrame(), output_dir=directory,
                                      as_of="2026-09-24T16:45:00-04:00", sessions=21)
            self.assertEqual(output["m2_transmission"]["status"], "UNAVAILABLE")
            self.assertFalse(output["m2_transmission"]["score_eligible"])
            self.assertNotIn("m2_transmission", output["result"])
            self.assertEqual(output["result"]["calc_version"], CALC_VERSION)
            self.assertEqual(output["result"]["score"], stored["score"])

    def test_overlay_keeps_independent_scales_and_missing_score_gap(self):
        import matplotlib.pyplot as plt

        dates = pd.date_range("2026-09-22", periods=3)
        history = [{"report_date": str(date.date()), "score": score,
                    "coverage": 0 if score is None else 1,
                    "funding_pressure": {"state": "NOT_TRIGGERED"}}
                   for date, score in zip(dates, [35, None, 65])]
        result = {**history[-1], "observation_mode": "PIT_REPLAY",
                  "as_of": "2026-09-24T16:45:00-04:00", "calc_version": CALC_VERSION}
        prices = pd.DataFrame({"SPY": [500, 505, 510], "QQQ": [400, 408, 416]}, index=dates)
        fig = None
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(plt, "close"):
                generate_core_funding_chart(result, history, prices, Path(directory) / "overlay.png")
                fig = plt.gcf()
                price_axis, change_axis, funding_axis = fig.axes
                self.assertEqual(price_axis.get_position().bounds, funding_axis.get_position().bounds)
                self.assertLess(change_axis.get_position().y1, price_axis.get_position().y0)
                self.assertEqual(funding_axis.get_ylim(), (0, 100))
                self.assertTrue(price_axis.get_shared_x_axes().joined(price_axis, funding_axis))
                self.assertFalse(price_axis.get_shared_y_axes().joined(price_axis, funding_axis))
                np.testing.assert_allclose(price_axis.lines[0].get_ydata(), [100, 101, 102])
                np.testing.assert_allclose(price_axis.lines[1].get_ydata(), [100, 102, 104])
                np.testing.assert_allclose(funding_axis.lines[0].get_ydata(), [35, np.nan, 65])
                labels = [text.get_text() for text in price_axis.get_legend().get_texts()]
                self.assertEqual(labels, ["SPY", "QQQ", "Funding index (PIT replay)", "Replay cutoff"])
        finally:
            if fig is not None:
                plt.close(fig)

    def test_migration_is_additive_shadow_only_and_modes_separated(self):
        source = (Path(__file__).resolve().parents[1] / "migration_20260925_core_funding.sql").read_text()
        self.assertIn("PRIMARY KEY (report_date, calc_version, observation_mode)", source)
        self.assertIn("IS FALSE", source)
        self.assertIn("REVOKE ALL ON public.core_funding_daily FROM PUBLIC, anon, authenticated, service_role;", source)
        self.assertIn("GRANT SELECT, INSERT ON public.core_funding_daily TO service_role;", source)
        self.assertNotIn("DROP TABLE", source)


if __name__ == "__main__":
    unittest.main()
