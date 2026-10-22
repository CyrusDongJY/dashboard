import copy
import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import requests

from fred_client import FredClient, FredUnavailable
from macro_score import score_macro, zscore, NUM_KEYS
from macro_recovery import (
    RecoveryStore, HistoricalMacroProvider, RAW_FIELDS, REPAIR_FIELDS,
    merge_capture, merge_repair, merge_non_missing, sync_captures,
    repair_pending, discover_missing_dates, recovery_needed, write_checked,
)


DAY = "2026-10-21"


class Response:
    def __init__(self, payload=None, text="", status=200, content_type="application/json"):
        self.payload = payload
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self.payload is None:
            raise ValueError("bad JSON including secret query string")
        return self.payload


class FredTests(unittest.TestCase):
    def client(self, responses):
        session = Mock()
        session.get.side_effect = responses
        return FredClient("SECRET", session=session, sleep=lambda _: None), session

    def test_200_maintenance_retries_once_then_circuit_breaks(self):
        body = Response(text="<html>Site Maintenance SECRET</html>", content_type="text/html")
        client, session = self.client([body, body])
        for symbol in ("WALCL", "M2SL", "NFCI"):
            with self.assertRaises(FredUnavailable) as error:
                client.series(symbol)
            self.assertEqual(error.exception.reason, "UPSTREAM_MAINTENANCE")
            self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(client.failures), 3)

    def test_invalid_json_is_not_an_empty_success(self):
        client, session = self.client([Response(), Response()])
        with self.assertRaises(FredUnavailable) as exc:
            client.series("NFCI")
        self.assertEqual(exc.exception.reason, "NON_JSON_RESPONSE")
        self.assertEqual(session.get.call_count, 2)

    def test_transient_maintenance_can_recover_on_second_attempt(self):
        client, session = self.client([
            Response(text="<html>maintenance</html>", content_type="text/html"),
            Response({"observations": [{"date": DAY, "value": "0"}]}),
        ])
        self.assertEqual(client.series("NFCI").iloc[0], 0)
        self.assertIsNone(client.unavailable_reason)

    def test_auth_failure_no_retry_or_key_leak(self):
        client, session = self.client([Response(status=403, text="SECRET")])
        with self.assertRaises(FredUnavailable) as exc:
            client.series("NFCI")
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn("SECRET", str(exc.exception))

    def test_network_error_redacts_url(self):
        client, session = self.client([requests.Timeout("api_key=SECRET")] * 2)
        with self.assertRaises(FredUnavailable) as exc:
            client.series("NFCI")
        self.assertNotIn("SECRET", str(exc.exception))

    def test_date_boundaries_empty_and_nonfinite(self):
        rows = [{"date": "2026-10-20", "value": "3"},
                {"date": DAY, "value": "0"},
                {"date": "2026-10-22", "value": "999"},
                {"date": "2026-10-19", "value": "inf"}]
        client, session = self.client([Response({"observations": rows})])
        result = client.series("NFCI", "2026-10-20", DAY)
        self.assertEqual(result.tolist(), [3.0, 0.0])
        self.assertEqual(session.get.call_args.kwargs["params"]["observation_end"], DAY)
        client, _ = self.client([Response({"observations": []})])
        with self.assertRaisesRegex(FredUnavailable, "EMPTY_OBSERVATIONS"):
            client.series("NFCI")


def histories():
    idx = pd.bdate_range(end=DAY, periods=560)
    x = np.linspace(1, 2, len(idx))
    close = pd.DataFrame({"^TNX": x, "CL=F": x * 70,
                          "HG=F": x * 3, "GC=F": 2000 + x}, index=idx)
    fred = pd.DataFrame({"Spread_10Y2Y": x - 3, "Fed_Assets": 7000000 - x * 20000,
                         "TGA": 500000 + x * 10000, "RRP": 100 - x,
                         "M2_Money_Supply": x * 20000, "NFCI": x - 2,
                         "Credit_Spread": 2 + x}, index=idx)
    return close, fred


class MacroFormulaTests(unittest.TestCase):
    def test_full_coverage_same_weighted_formula(self):
        close, fred = histories()
        result = score_macro(close, fred)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["coverage"], 1)
        # Linear levels: TNX z63 in (1,2), credit z252 in (1,2),
        # inverted curve steepens less than .2, other signals zero.
        self.assertEqual(result["score"], 15)

    def test_no_fred_preserves_other_components_but_score_null(self):
        close, _ = histories()
        result = score_macro(close, pd.DataFrame())
        self.assertEqual(result["coverage"], .4)
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIsNotNone(result["num"]["tnx_z"])

    def test_70_percent_gate_unchanged(self):
        close, fred = histories()
        result = score_macro(close, fred.drop(columns=["NFCI", "Credit_Spread", "M2_Money_Supply"]))
        self.assertEqual(result["coverage"], .7)
        self.assertIsNotNone(result["score"])
        self.assertEqual(result["status"], "PARTIAL")
        result = score_macro(close.drop(columns=["^TNX"]), fred.drop(columns=["NFCI", "Credit_Spread", "M2_Money_Supply"]))
        self.assertIsNone(result["score"])

    def test_zero_denominator_and_constant_zscore_are_missing(self):
        close, fred = histories()
        close["CL=F"] = 0
        close["^TNX"] = 1
        result = score_macro(close, fred)
        self.assertIsNone(result["num"]["oil_roc"])
        self.assertIsNone(result["num"]["tnx_z"])
        json.dumps(result, allow_nan=False)

    def test_full_no_input_is_null_not_zero(self):
        result = score_macro(pd.DataFrame(), pd.DataFrame())
        self.assertEqual(result["coverage"], 0)
        self.assertIsNone(result["score"])


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.fail_write = False
        self.write_count = 0

    def table(self, name):
        assert name == "market_history"
        return Query(self)


class Query:
    def __init__(self, db):
        self.db = db
        self.filters = []
        self.payload = None

    def select(self, _):
        return self

    def eq(self, key, value):
        self.filters.append(lambda r: r.get(key) == value)
        return self

    def gte(self, key, value):
        self.filters.append(lambda r: r.get(key) >= value)
        return self

    def lte(self, key, value):
        self.filters.append(lambda r: r.get(key) <= value)
        return self

    def upsert(self, payload, on_conflict):
        self.payload = payload
        return self

    def execute(self):
        if self.payload is not None:
            if self.db.fail_write:
                raise RuntimeError("write unavailable")
            self.db.rows[self.payload["record_date"]] = copy.deepcopy(self.payload)
            self.db.write_count += 1
            return Mock(data=[self.payload])
        return Mock(data=[copy.deepcopy(r) for r in self.db.rows.values() if all(f(r) for f in self.filters)])


def partial():
    return {"record_date": DAY, "macro_score": None, "vix": 21.0,
            "credit_spread": None, "full_metrics": {
                "macro_score_coverage": .4, "macro_score_status": "UNAVAILABLE",
                "source_errors": [{"reason": "UPSTREAM_MAINTENANCE"}]}}


def repaired():
    close, fred = histories()
    return {"values": {k: 1.0 for k in REPAIR_FIELDS},
            "macro": score_macro(close, fred), "source_dates": {"NFCI": "2026-10-16"},
            "source_names": {"NFCI": "FRED:NFCI"}, "errors": []}


class MergeAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "recovery.sqlite3"
        self.store = RecoveryStore(self.path)
        self.client = FakeDB()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_partial_capture_written_with_null_score(self):
        self.store.stage(partial(), "2026-10-21T20:30:00+00:00")
        result = sync_captures(self.client, self.store)
        self.assertEqual(result, {"synced": 1, "failed": 0})
        row = self.client.rows[DAY]
        self.assertEqual(row["vix"], 21)
        self.assertIsNone(row["macro_score"])
        self.assertEqual(row["full_metrics"]["capture_quality"]["status"], "PARTIAL")
        self.assertEqual(self.store.pending("2026-10-22"), [DAY])

    def test_readback_compares_the_normalized_payload_that_was_sent(self):
        row = partial()
        row["full_metrics"]["optional_source"] = ""
        row["full_metrics"]["missing_value"] = float("nan")
        saved = write_checked(self.client, row)
        self.assertIsNone(saved["full_metrics"]["optional_source"])
        self.assertIsNone(saved["full_metrics"]["missing_value"])

    def test_readback_still_rejects_actual_data_corruption(self):
        with patch("macro_recovery.read_row", return_value={"full_metrics": {}}):
            with self.assertRaisesRegex(RuntimeError, "READBACK_MISMATCH"):
                write_checked(self.client, partial())

    def test_missing_never_erases_valid_and_zero_is_valid(self):
        self.assertEqual(merge_non_missing({"x": 9, "y": 2}, {"x": None, "y": 0}), {"x": 9, "y": 0})
        self.assertEqual(merge_non_missing({"x": 9}, {"x": float("nan")}), {"x": 9})

    def test_later_failed_capture_keeps_valid_score_cohort(self):
        old = {"record_date": DAY, "macro_score": 0, "credit_spread": 3.2,
               "full_metrics": {"macro_score_coverage": 1, "macro_score_status": "OK"}}
        row = merge_capture(old, partial(), "2026-10-21T20:35:00+00:00")
        self.assertEqual(row["macro_score"], 0)
        self.assertEqual(row["credit_spread"], 3.2)
        self.assertEqual(row["full_metrics"]["macro_score_status"], "OK")
        self.assertEqual(row["full_metrics"]["capture_quality"]["status"], "PARTIAL")

    def test_internals_retry_preserves_atomic_observation_cohort(self):
        old, new = partial(), partial()
        old["full_metrics"]["market_internals"] = {
            "version": "v1", "captured_at": "old", "returns": {"5d": 2.0}, "status": "OK"}
        new["full_metrics"]["market_internals"] = {
            "version": "v1", "captured_at": "new", "returns": {"5d": None}, "status": "MISSING_WINDOW"}
        saved = merge_capture(old, new, "2026-10-21T20:35:00+00:00")
        self.assertEqual(saved["full_metrics"]["market_internals"], new["full_metrics"]["market_internals"])

    def test_write_failure_stays_in_durable_outbox_then_recovers(self):
        self.store.stage(partial())
        self.client.fail_write = True
        self.assertEqual(sync_captures(self.client, self.store)["failed"], 1)
        self.assertEqual(len(self.store.unsynced()), 1)
        self.store.db.close()
        self.store = RecoveryStore(self.path)
        self.client.fail_write = False
        self.assertEqual(sync_captures(self.client, self.store)["synced"], 1)
        self.assertEqual(sync_captures(self.client, self.store)["synced"], 0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_repair_keeps_original_null_score_and_values(self):
        row, complete = merge_repair(partial(), DAY, repaired())
        self.assertTrue(complete)
        self.assertIsNone(row["macro_score"])
        self.assertEqual(row["vix"], 21)
        detail = row["full_metrics"]["macro_recovery"]
        self.assertEqual(detail["reconstructed_macro"]["score"], 15)
        self.assertFalse(detail["point_in_time_eligible"])
        self.assertEqual(row["full_metrics"]["original_macro_evidence"]["score"], None)
        second, _ = merge_repair(row, DAY, repaired())
        self.assertEqual(second["full_metrics"]["macro_recovery"]["filled_fields"], detail["filled_fields"])
        self.assertFalse(recovery_needed(row))

    def test_no_original_capture_remains_historical_only(self):
        row, _ = merge_repair({}, DAY, repaired())
        self.assertIsNone(row["macro_score"])
        self.assertNotIn("pcr", row)
        self.assertFalse(row["full_metrics"]["original_macro_evidence"]["capture_existed"])

    def test_repair_native_dates_visible_to_existing_consumers(self):
        row, _ = merge_repair({}, DAY, repaired())
        full = row["full_metrics"]
        self.assertEqual(full["fred_source_dates"]["NFCI"], "2026-10-16")
        self.assertEqual(full["market_metric_lineage"]["nfci"]["source_date"], "2026-10-16")
        self.assertFalse(full["market_metric_lineage"]["nfci"]["point_in_time_eligible"])

    def test_failed_repair_does_not_erase_prior_reconstructed_score(self):
        row, _ = merge_repair({}, DAY, repaired())
        failed = repaired()
        failed["values"] = {}
        failed["macro"] = score_macro(pd.DataFrame(), pd.DataFrame())
        later, _ = merge_repair(row, DAY, failed)
        detail = later["full_metrics"]["macro_recovery"]
        self.assertEqual(detail["reconstructed_macro"]["score"], 15)
        self.assertIsNone(detail["last_attempt_macro"]["score"])

    def test_future_dates_or_live_fields_cannot_enter_repair(self):
        repair = repaired()
        repair["source_dates"]["NFCI"] = "2026-10-22"
        with self.assertRaisesRegex(ValueError, "FUTURE_SOURCE_DATE"):
            merge_repair({}, DAY, repair)
        repair = repaired()
        repair["values"]["pcr"] = .7
        with self.assertRaisesRegex(ValueError, "NON_MACRO_REPAIR_FIELD"):
            merge_repair({}, DAY, repair)
        with self.assertRaisesRegex(ValueError, "DATE_MISMATCH"):
            merge_capture({"record_date": "2026-10-20"}, partial(), "now")

    def test_queue_discovery_bounded_and_respects_closed_days(self):
        discover_missing_dates(self.client, self.store, DAY,
                               lambda d: pd.Timestamp(d).weekday() < 5, lookback_days=4)
        self.assertEqual(self.store.pending("2026-10-22", 9), ["2026-10-19", "2026-10-20", DAY])
        discover_missing_dates(self.client, self.store, DAY, lambda _: True, lookback_days=0)
        self.assertEqual(len(self.store.pending("2026-10-22", 9)), 3)

    def test_repair_remains_pending_when_fred_fails(self):
        self.store.enqueue(DAY)
        provider = Mock()
        incomplete = repaired()
        incomplete["values"] = {"tga": 100.0}
        incomplete["macro"] = score_macro(pd.DataFrame(), pd.DataFrame())
        provider.fetch.return_value = incomplete
        provider.fred.unavailable_reason = "UPSTREAM_MAINTENANCE"
        provider.deadline = time.monotonic() + 30
        results = repair_pending(self.client, self.store, provider, "2026-10-22")
        self.assertEqual(results[0]["status"], "PENDING")
        self.assertEqual(self.client.rows[DAY]["tga"], 100)
        self.assertEqual(self.store.pending("2026-10-22"), [DAY])


class HistoricalProviderTests(unittest.TestCase):
    def test_future_prices_removed_and_native_dates_preserved(self):
        close, _ = histories()
        close.loc[pd.Timestamp("2026-10-22")] = 999
        close["^VIX"] = 20
        close.loc[pd.Timestamp("2026-10-22"), "^VIX"] = 999
        fred = Mock()
        fred.series.side_effect = FredUnavailable("ALL", "UPSTREAM_MAINTENANCE")
        provider = HistoricalMacroProvider("SECRET", fred=fred, price_loader=Mock(return_value=close))
        provider.official = Mock()
        provider.official.fetch_tga.return_value = pd.Series([200.0], index=pd.to_datetime(["2026-10-20"]))
        result = provider.fetch(DAY, {})
        self.assertEqual(result["values"]["vix"], 20)
        self.assertEqual(result["values"]["tga"], 200)
        self.assertEqual(result["source_dates"]["TGA"], "2026-10-20")
        self.assertEqual(result["macro"]["coverage"], .4)
        for args in fred.series.call_args_list:
            self.assertEqual(args.args[2], DAY)
        self.assertIsNone(result["macro"]["score"])


class DailyOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module_patches = patch.dict(sys.modules, {
            "market_config": types.SimpleNamespace(FRED_API_KEY="SECRET", SUPABASE_URL="url", SUPABASE_KEY="key"),
            "yfinance": types.SimpleNamespace(Ticker=Mock(side_effect=RuntimeError("no live quotes"))),
            "google": types.SimpleNamespace(genai=Mock()),
            "supabase": types.SimpleNamespace(create_client=Mock(), Client=object),
            "pandas_market_calendars": Mock(),
        })
        self.module_patches.start()
        self.addCleanup(self.module_patches.stop)
        # Load the actual entrypoint; only external transports are replaced.
        spec = importlib.util.spec_from_file_location("review_under_test", Path(__file__).resolve().parents[1] / "auto_review.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.module_patches.stop()
        self.tmp.cleanup()

    def test_partial_live_capture_persists_and_never_calls_ai(self):
        module = self.module
        db = FakeDB()
        payload = partial()
        today = module.datetime.datetime.now(module.NY_TZ).strftime("%Y-%m-%d")
        payload["record_date"] = today
        with patch.object(module, "is_trading_day", return_value=True), \
             patch.object(module, "run_dashboard", return_value=("raw", payload, {"日期": today})), \
             patch.object(module, "append_local_csv"), \
             patch.object(module, "create_client", return_value=db), \
             patch.object(module, "discover_missing_dates"), \
             patch.object(module, "repair_pending", return_value=[]), \
             patch.object(module, "log_data_quality"), \
             patch.object(module.os.path, "expanduser", return_value=self.tmp.name), \
             patch.object(module, "generate_ai_report") as ai, \
             patch.object(module, "send_email") as email:
            self.assertEqual(module.main([]), 0)
        self.assertIsNone(db.rows[today]["macro_score"])
        self.assertEqual(db.rows[today]["vix"], 21)
        ai.assert_not_called()
        self.assertIn("部分数据", email.call_args.args[0])

    def test_repair_only_never_runs_live_engine_or_sends_email(self):
        module = self.module
        with patch.object(module, "run_dashboard") as live, \
             patch.object(module, "create_client", return_value=FakeDB()), \
             patch.object(module, "discover_missing_dates"), \
             patch.object(module, "repair_pending", return_value=[]), \
             patch.object(module.os.path, "expanduser", return_value=self.tmp.name), \
             patch.object(module, "send_email") as email:
            self.assertEqual(module.main(["--repair-only", "--no-email"]), 0)
        live.assert_not_called()
        email.assert_not_called()

    def test_real_engine_can_export_non_fred_fields_without_macro_score(self):
        with patch.object(self.module.os.path, "expanduser", return_value=self.tmp.name):
            engine = self.module.UltimateDashboard()
        close, _ = histories()
        engine.data_cache.update(close=close, raw_close=close)
        engine.calculate_metrics()
        payload, _ = engine.get_db_payload(DAY)
        self.assertIsNone(payload["macro_score"])
        self.assertIsNotNone(payload["tnx_z"])
        self.assertEqual(payload["full_metrics"]["macro_score_coverage"], .4)
        self.assertEqual(payload["full_metrics"]["market_internals"]["version"],
                         "market_internals_observation_v1")
        self.assertEqual(payload["full_metrics"]["market_internals"]["sectors"]["reason"],
                         "NO_CAPTURE_TIMESTAMP")
        json.dumps(payload, allow_nan=False)

    def test_partial_g3_inputs_never_assume_zero_assets_or_fixed_fx(self):
        with patch.object(self.module.os.path, "expanduser", return_value=self.tmp.name):
            engine = self.module.UltimateDashboard()
        def native(symbol, *args):
            if symbol == "WALCL":
                return pd.Series([7000000.0], index=pd.to_datetime([DAY]))
            raise FredUnavailable(symbol, "EMPTY_OBSERVATIONS")
        engine.fred_client = Mock(series=Mock(side_effect=native))
        engine.log = Mock()
        engine.fetch_fred_data_direct()
        self.assertEqual(engine.liquidity['assets'], 7000)
        self.assertEqual(engine.liquidity['g3_liq'], '-')

    def test_csv_partial_rerun_preserves_prior_non_null(self):
        module = self.module
        path = Path(self.tmp.name) / "Market_History.csv"
        with patch.object(module.os.path, "expanduser", return_value=str(path)):
            module.append_local_csv({"日期": DAY, "valid": 7, "zero": 3}, DAY)
            module.append_local_csv({"日期": DAY, "valid": None, "zero": 0}, DAY)
        row = pd.read_csv(path).iloc[0]
        self.assertEqual(row["valid"], 7)
        self.assertEqual(row["zero"], 0)


if __name__ == "__main__":
    unittest.main()
