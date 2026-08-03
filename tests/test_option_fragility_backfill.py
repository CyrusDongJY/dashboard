from datetime import date
import logging
import unittest

import numpy as np
import pandas as pd

from backfill_option_fragility import aggregate_theta_day, rolling_iv_metrics


class OptionFragilityBackfillTests(unittest.TestCase):
    def test_ib_client_logging_does_not_emit_account_details_at_info(self):
        self.assertGreaterEqual(
            logging.getLogger("ib_insync").level, logging.WARNING)

    def test_rolling_iv_metrics_has_no_future_leakage(self):
        index = pd.bdate_range("2025-01-02", periods=100)
        values = pd.Series(np.linspace(0.15, 0.35, len(index)), index=index)
        baseline = rolling_iv_metrics(values, lookback=80, min_obs=20)

        changed = values.copy()
        changed.iloc[-1] = 2.0
        replayed = rolling_iv_metrics(changed, lookback=80, min_obs=20)
        pd.testing.assert_frame_equal(
            baseline.iloc[:-1], replayed.loc[baseline.index[:-1]])

    def test_theta_aggregate_uses_atm_straddle_and_signed_near_gamma(self):
        greeks = pd.DataFrame([
            {
                "expiration": "2026-07-17", "strike": 100.0, "right": "call",
                "underlying_price": 101.0, "gamma": 0.020,
                "bid": 2.0, "ask": 2.2,
            },
            {
                "expiration": "2026-07-17", "strike": 100.0, "right": "put",
                "underlying_price": 101.0, "gamma": 0.018,
                "bid": 1.0, "ask": 1.2,
            },
            {
                "expiration": "2026-07-23", "strike": 101.0, "right": "call",
                "underlying_price": 101.0, "gamma": 0.015,
                "bid": 3.0, "ask": 3.2,
            },
            {
                "expiration": "2026-07-23", "strike": 101.0, "right": "put",
                "underlying_price": 101.0, "gamma": 0.014,
                "bid": 2.8, "ask": 3.0,
            },
        ])
        oi = pd.DataFrame([
            {
                "expiration": row["expiration"], "strike": row["strike"],
                "right": row["right"], "open_interest": amount,
                "timestamp": "2026-07-16T06:30:00-04:00",
            }
            for row, amount in zip(greeks.to_dict("records"), [1000, 800, 500, 400])
        ])
        result = aggregate_theta_day(greeks, oi, date(2026, 7, 16))
        self.assertAlmostEqual(result["expected_move_pct"], 3.2 / 101.0 * 100)
        expected_gamma = (
            (0.020 * 1000 - 0.018 * 800 + 0.015 * 500 - 0.014 * 400)
            * 100 * 101 ** 2 * 0.01 / 1e6
        )
        self.assertAlmostEqual(result["short_gamma_m"], expected_gamma)

    def test_theta_aggregate_rejects_future_oi(self):
        greeks = pd.DataFrame([{
            "expiration": "2026-07-17", "strike": 100.0, "right": "call",
            "underlying_price": 100.0, "gamma": 0.02, "bid": 1.0, "ask": 1.2,
        }])
        oi = pd.DataFrame([{
            "expiration": "2026-07-17", "strike": 100.0, "right": "call",
            "open_interest": 1000,
            "timestamp": "2026-07-17T06:30:00-04:00",
        }])
        result = aggregate_theta_day(greeks, oi, date(2026, 7, 16))
        self.assertIsNone(result["expected_move_pct"])
        self.assertIsNone(result["short_gamma_m"])


if __name__ == "__main__":
    unittest.main()
