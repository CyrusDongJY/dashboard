import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from temporal_monitor import (
    MIN_LIVE_REVIEW_DAYS,
    build_temporal_history,
    backfill_temporal_pipeline,
    evaluate_temporal_states,
    format_temporal_summary,
    summarize_evaluations,
)
from temporal_report import generate_temporal_chart


def dates(count=30):
    return pd.bdate_range("2026-01-02", periods=count)


def continuous_rows(panel, values, coverage=1.0):
    rows = []
    for date, value in zip(dates(len(values)), values):
        day = date.strftime("%Y-%m-%d")
        if panel == "environment":
            rows.append({
                "report_date": day, "composite": value,
                "coverage": coverage, "state": "常态观察 | 测试",
                "calc_version": "env_test",
            })
        elif panel == "liquidity":
            rows.append({
                "report_date": day, "composite": value,
                "coverage": coverage, "state": "平衡水位",
                "calc_version": "liq_test",
            })
        elif panel == "tactical_stress":
            rows.append({
                "record_date": day, "eod_stress_score": value,
                "stress_coverage": coverage, "stress_confidence": coverage,
                "stress_calc_version": "stress_test",
            })
        elif panel == "risk_capital":
            rows.append({
                "report_date": day, "score": value,
                "coverage": coverage, "confidence": coverage,
                "state": "MIXED", "calc_version": "capital_test",
                "source_date": day,
            })
    return rows


def row_for(rows, panel, day=None):
    matches = [row for row in rows if row["panel"] == panel]
    if day:
        matches = [row for row in matches if row["report_date"] == day]
    return matches[-1]


class TemporalStateTests(unittest.TestCase):
    def test_liquidity_is_inverted_to_common_pressure_direction(self):
        history = build_temporal_history({
            "liquidity": continuous_rows("liquidity", [80.0, 70.0]),
        })
        latest = row_for(history, "liquidity")
        self.assertEqual(latest["level_value"], 70.0)
        self.assertEqual(latest["risk_value"], 30.0)
        self.assertEqual(latest["change_1d"], 10.0)

    def test_missing_coverage_fails_closed_without_healthy_default(self):
        history = build_temporal_history({
            "tactical_stress": continuous_rows(
                "tactical_stress", [75.0], coverage=0.40),
        })
        latest = row_for(history, "tactical_stress")
        self.assertIsNone(latest["risk_value"])
        self.assertEqual(latest["quality_status"], "INSUFFICIENT")
        self.assertEqual(latest["temporal_state"], "INSUFFICIENT_DATA")

    def test_future_rows_do_not_change_an_earlier_state(self):
        base_values = list(np.linspace(35.0, 70.0, 22))
        base = build_temporal_history({
            "environment": continuous_rows("environment", base_values),
        })
        target_date = dates(len(base_values))[-1].strftime("%Y-%m-%d")
        expected = row_for(base, "environment", target_date)
        extended = build_temporal_history({
            "environment": continuous_rows(
                "environment", base_values + [10.0, 95.0, 5.0]),
        })
        actual = row_for(extended, "environment", target_date)
        for key in ("risk_value", "change_5d", "change_21d",
                    "temporal_state", "persistence_days"):
            self.assertEqual(actual[key], expected[key])

    def test_sudden_turn_requires_an_independent_panel_confirmation(self):
        reversal = [80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 90.0]
        unconfirmed = build_temporal_history({
            "environment": continuous_rows("environment", reversal),
        })
        self.assertEqual(
            row_for(unconfirmed, "environment")["temporal_state"],
            "TURN_WATCH")

        overlapping = build_temporal_history({
            "environment": continuous_rows("environment", reversal),
            "tactical_stress": continuous_rows(
                "tactical_stress", reversal),
        })
        self.assertEqual(
            row_for(overlapping, "environment")["temporal_state"],
            "TURN_WATCH")

        confirmed = build_temporal_history({
            "environment": continuous_rows("environment", reversal),
            "liquidity": continuous_rows(
                "liquidity", [100.0 - value for value in reversal]),
        })
        latest = row_for(confirmed, "environment")
        self.assertEqual(latest["temporal_state"], "SUDDEN_TURN")
        self.assertEqual(latest["independent_confirmation_count"], 1)

    def test_event_pulse_preserves_isolated_event_semantics(self):
        event_rows = []
        source_states = ["QUIET", "ISOLATED_EVENT_SPIKE", "QUIET"]
        for date, state in zip(dates(3), source_states):
            event_rows.append({
                "report_date": date.strftime("%Y-%m-%d"),
                "state": state, "candidate_count": 8, "eligible_count": 6,
                "scan_coverage": 1.0, "candidate_coverage": 0.75,
                "risk_on_tickers": ["XYZ"] if "SPIKE" in state else [],
                "risk_off_tickers": [], "positive_industries": ["Biotech"],
                "negative_industries": [], "source_date": date,
            })
        history = build_temporal_history({"event_pulse": event_rows})
        spike = row_for(
            history, "event_pulse", dates(3)[1].strftime("%Y-%m-%d"))
        latest = row_for(history, "event_pulse")
        self.assertEqual(spike["temporal_state"], "ISOLATED_EVENT")
        self.assertEqual(latest["temporal_state"], "STABLE")
        self.assertEqual(latest["details"]["risk_on_days_5d"], 1)

    def test_risk_capital_source_state_overrides_a_neutral_numeric_zone(self):
        rows = continuous_rows("risk_capital", [45.0, 45.0])
        for row in rows:
            row["state"] = "ACTIVE_DELEVERAGING"
        history = build_temporal_history({"risk_capital": rows})
        latest = row_for(history, "risk_capital")
        self.assertEqual(latest["temporal_state"], "PRESSURE_PERSISTENT")
        self.assertEqual(latest["persistence_days"], 2)
        self.assertIn("主动去杠杆延续", latest["temporal_state_cn"])

    def test_replay_backfill_never_overwrites_live_shadow_rows(self):
        source = {"environment": continuous_rows(
            "environment", [40.0, 45.0])}
        live_date = source["environment"][-1]["report_date"]
        persisted_batches = []

        def capture(_supabase, table, rows, _conflict):
            persisted_batches.append((table, list(rows)))
            return True, len(rows)

        with patch("temporal_monitor.fetch_panel_histories", return_value=source), \
                patch("temporal_monitor.fetch_benchmark_prices", return_value={}), \
                patch("temporal_monitor._fetch_persisted_temporal", return_value=[{
                    "report_date": live_date, "panel": "environment",
                    "observation_mode": "LIVE_SHADOW",
                }]), \
                patch("temporal_monitor._persist_chunks", side_effect=capture):
            result = backfill_temporal_pipeline(
                object(), source["environment"][0]["report_date"], live_date,
                persist=True)
        state_rows = next(rows for table, rows in persisted_batches
                          if table == "temporal_state_daily")
        self.assertFalse(any(row["report_date"] == live_date for row in state_rows))
        self.assertEqual(result["protected_live_count"], 1)


class TemporalEvaluationTests(unittest.TestCase):
    def benchmark(self, count=24):
        index = dates(count)
        close = np.linspace(100.0, 123.0, count)
        return pd.DataFrame({
            "report_date": [date.strftime("%Y-%m-%d") for date in index],
            "close": close,
            "open": close * 1.002,
            "previous_close": np.r_[np.nan, close[:-1]],
        })

    def signal(self, position=0, mode="REPLAY"):
        return {
            "report_date": dates(24)[position].strftime("%Y-%m-%d"),
            "panel": "environment", "temporal_state": "PRESSURE_BUILDING",
            "quality_status": "OK", "risk_value": 70.0,
            "observation_mode": mode, "calc_version": "test",
        }

    def test_only_full_forward_windows_are_evaluated(self):
        rows = evaluate_temporal_states(
            [self.signal(0), self.signal(22)], {"QQQ": self.benchmark()})
        first = [row for row in rows if row["signal_date"] ==
                 self.signal(0)["report_date"]]
        late = [row for row in rows if row["signal_date"] ==
                self.signal(22)["report_date"]]
        self.assertEqual({row["horizon_days"] for row in first}, {1, 5, 21})
        self.assertEqual({row["horizon_days"] for row in late}, {1})
        result_21 = next(row for row in first if row["horizon_days"] == 21)
        self.assertGreater(result_21["forward_return_pct"], 0)
        self.assertEqual(result_21["max_drawdown_pct"], 0.0)
        self.assertIsNotNone(result_21["realized_vol_pct"])
        self.assertGreater(result_21["max_abs_gap_pct"], 0)

    def test_progress_never_counts_replay_as_live_shadow(self):
        temporal = [self.signal(0, "REPLAY"), self.signal(1, "LIVE_SHADOW")]
        evaluations = evaluate_temporal_states(
            temporal, {"QQQ": self.benchmark()})
        summary = summarize_evaluations(temporal, evaluations)
        progress = summary["progress"]["environment"]
        self.assertEqual(progress["replay_days"], 1)
        self.assertEqual(progress["live_days"], 1)
        self.assertFalse(progress["stage_review_ready"])
        self.assertEqual(MIN_LIVE_REVIEW_DAYS, 60)

    def test_email_summary_discloses_shadow_and_replay_status(self):
        history = build_temporal_history({
            "environment": continuous_rows("environment", [40.0] * 7),
        })
        current = [dict(row_for(history, "environment"))]
        current[0]["observation_mode"] = "LIVE_SHADOW"
        result = {
            "current": current,
            "evaluation_summary": summarize_evaluations(current, []),
            "persistence_status": "ok", "calc_version": "test",
        }
        text = format_temporal_summary(result)
        self.assertIn("统一口径", text)
        self.assertIn("实盘影子 1/60日", text)
        self.assertIn("历史回放不等同于实盘观察", text)
        self.assertIn("不接入告警或仓位", text)

    def test_compact_chart_contains_real_pixels(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed")
        history = build_temporal_history({
            "environment": continuous_rows(
                "environment", np.linspace(20.0, 80.0, 30)),
            "liquidity": continuous_rows(
                "liquidity", np.linspace(80.0, 40.0, 30)),
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_temporal_chart(
                history, dates(30)[-1].strftime("%Y-%m-%d"),
                output_path=os.path.join(tmp, "temporal.png"))
            self.assertTrue(os.path.isfile(path))
            self.assertGreater(os.path.getsize(path), 20_000)


if __name__ == "__main__":
    unittest.main()
