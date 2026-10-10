import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

try:
    import pandas_market_calendars  # noqa: F401
except ImportError:
    calendar_module = types.ModuleType("pandas_market_calendars")
    calendar_module.get_calendar = lambda _name: types.SimpleNamespace(
        schedule=lambda start_date, end_date: pd.DataFrame(
            index=pd.bdate_range(start_date, end_date)))
    sys.modules["pandas_market_calendars"] = calendar_module

from anomaly_engine import run_engine, metric_snapshot, scan_metric


class VixAnomalyContractTests(unittest.TestCase):
    def run_curve(self, rows):
        frame = pd.DataFrame(rows)

        def history(_client, table, _date_col, **_kwargs):
            return frame if table == "macro_spot_daily" else pd.DataFrame()

        with patch("anomaly_engine._fetch_history", side_effect=history), patch(
                "market_utils.safe_upsert") as upsert, patch(
                "anomaly_engine._sync_anomaly_events"):
            result = run_engine(None, "2026-10-09", persist=True)
        persisted = [call.args[2] for call in upsert.call_args_list
                     if call.args[1] == "metric_daily"]
        return result, persisted

    def test_unverified_history_produces_no_futures_event_or_normalized_row(self):
        (events, snapshot), persisted = self.run_curve([{
            "date": "2026-10-09", "vix_contango_pct": -8.0,
            "vix_m1": 20, "vix_m2": 18.4,
        }])
        self.assertEqual(events, [])
        self.assertIsNone(snapshot["vix_contango_pct"])
        self.assertEqual(persisted, [])

    def test_verified_curve_drops_legacy_sample_and_persists_monthly_lineage(self):
        (events, snapshot), persisted = self.run_curve([
            {"date": "2026-10-08", "vix_contango_pct": -25,
             "vix_m1": 20, "vix_m2": 15},
            {"date": "2026-10-09", "vix_contango_pct": -99,
             "vix_m1": 20, "vix_m2": 19,
             "vix_curve_quality": "OK", "vix_curve_source_date": "2026-10-09",
             "vix_curve_metadata": {"selection": "STANDARD_MONTHLY"}},
        ])
        self.assertEqual(snapshot["vix_contango_pct"], -5.0)
        self.assertEqual(events[0].severity, 3)
        row = persisted[0][0]
        self.assertEqual(row["sample_len"], 1)
        self.assertEqual(row["source_name"], "macro_spot_daily:STANDARD_MONTHLY")
        self.assertEqual(row["value"], -5.0)

    def test_absolute_price_flat_zone_cannot_trigger_severe_inversion(self):
        series = pd.Series([-0.8])
        events = scan_metric("vix_contango_pct", series, "2026-10-09", curve_state="FLAT")
        self.assertEqual(events[0].severity, 1)
        row = metric_snapshot("vix_contango_pct", series, "2026-10-09", curve_state="FLAT")
        self.assertEqual(row["severity"], 1)


if __name__ == "__main__":
    unittest.main()
