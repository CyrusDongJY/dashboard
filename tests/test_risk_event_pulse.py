import unittest

import numpy as np
import pandas as pd

from risk_event_pulse import (
    aggregate_event_pulse,
    compute_event_pulse,
    format_event_pulse_summary,
    merge_scan_candidates,
)


REPORT_DATE = "2026-08-19"
SCANS = ["TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME"]


def candidate(ticker, direction="NORMAL", industry="Technology",
              ret=0.0, relative=0.0, extreme=False, eligible=True):
    return {
        "report_date": REPORT_DATE,
        "ticker": ticker,
        "layer": "dynamic",
        "return_1d_pct": ret,
        "relative_1d_pct": relative,
        "turnover_ratio_20d": 2.0,
        "abnormal_turnover_z60": 2.0,
        "pulse_direction": direction,
        "single_name_extreme": extreme,
        "industry_group": industry,
        "decision_eligible": eligible,
        "quality": "OK" if eligible else "MISSING",
        "scan_codes": ["TOP_PERC_GAIN"],
        "scanner_rank": 0,
    }


def benchmark():
    return {
        "source_date": REPORT_DATE,
        "return_1d_pct": 0.5,
        "quality": "OK",
    }


class DiscoveryTests(unittest.TestCase):
    def test_merges_scans_and_excludes_fixed_ladder_members(self):
        scan_results = {
            "TOP_PERC_GAIN": [
                {"ticker": "MRNA", "scanner_rank": 0,
                 "currency": "USD", "sec_type": "STK"},
                {"ticker": "NVDA", "scanner_rank": 1,
                 "currency": "USD", "sec_type": "STK"},
            ],
            "HOT_BY_VOLUME": [
                {"ticker": "MRNA", "scanner_rank": 2,
                 "industry": "Healthcare", "currency": "USD",
                 "sec_type": "STK"},
            ],
        }
        rows = merge_scan_candidates(scan_results)
        self.assertEqual([row["ticker"] for row in rows], ["MRNA"])
        self.assertEqual(
            rows[0]["scan_codes"], ["TOP_PERC_GAIN", "HOT_BY_VOLUME"])
        self.assertEqual(rows[0]["industry"], "Healthcare")


class AggregationTests(unittest.TestCase):
    def test_isolated_mrna_spike_is_not_market_confirmation(self):
        rows = [
            candidate("MRNA", "RISK_ON", "Healthcare", 128, 127, True),
            candidate("AAA", "NORMAL", "Technology"),
            candidate("BBB", "NORMAL", "Industrials"),
            candidate("CCC", "NORMAL", "Financials"),
        ]
        result = aggregate_event_pulse(
            rows, benchmark(), REPORT_DATE, scans_completed=SCANS)
        self.assertEqual(result["state"], "ISOLATED_EVENT_SPIKE")
        self.assertFalse(result["market_confirmation"])
        self.assertEqual(result["extreme_tickers"], ["MRNA"])

    def test_three_industry_confirmations_create_risk_on_diffusion(self):
        rows = [
            candidate("AAA", "RISK_ON", "Technology", 8, 7),
            candidate("BBB", "RISK_ON", "Healthcare", 9, 8),
            candidate("CCC", "RISK_ON", "Industrials", 7, 6),
            candidate("DDD", "NORMAL", "Financials"),
        ]
        result = aggregate_event_pulse(
            rows, benchmark(), REPORT_DATE, scans_completed=SCANS)
        self.assertEqual(result["state"], "RISK_ON_DIFFUSION")
        self.assertTrue(result["market_confirmation"])

    def test_same_industry_is_theme_not_market_confirmation(self):
        rows = [
            candidate("AAA", "RISK_ON", "Biotechnology", 8, 7),
            candidate("BBB", "RISK_ON", "Biotechnology", 9, 8),
            candidate("CCC", "RISK_ON", "Biotechnology", 7, 6),
        ]
        result = aggregate_event_pulse(
            rows, benchmark(), REPORT_DATE, scans_completed=SCANS)
        self.assertEqual(result["state"], "THEME_RISK_ON")
        self.assertFalse(result["market_confirmation"])

    def test_cross_industry_downside_creates_contagion(self):
        rows = [
            candidate("AAA", "RISK_OFF", "Technology", -8, -7),
            candidate("BBB", "RISK_OFF", "Healthcare", -9, -8),
            candidate("CCC", "RISK_OFF", "Financials", -7, -6),
        ]
        result = aggregate_event_pulse(
            rows, benchmark(), REPORT_DATE, scans_completed=SCANS)
        self.assertEqual(result["state"], "RISK_OFF_CONTAGION")
        self.assertTrue(result["market_confirmation"])

    def test_missing_loser_scan_fails_closed(self):
        rows = [candidate("AAA"), candidate("BBB"), candidate("CCC")]
        result = aggregate_event_pulse(
            rows, benchmark(), REPORT_DATE,
            scans_completed=["TOP_PERC_GAIN", "HOT_BY_VOLUME"])
        self.assertEqual(result["state"], "UNAVAILABLE")
        self.assertFalse(result["market_confirmation"])

class EndToEndTests(unittest.TestCase):
    def histories(self):
        dates = pd.bdate_range(end=REPORT_DATE, periods=90)
        histories = {
            "QQQ": pd.DataFrame({
                "date": dates,
                "close": np.linspace(100, 105, len(dates)),
                "volume": np.full(len(dates), 10_000_000.0),
            })
        }
        for ticker in ("MRNA", "AAA", "BBB", "CCC"):
            close = np.linspace(40, 45, len(dates))
            volume = np.full(len(dates), 2_000_000.0)
            if ticker == "MRNA":
                close[-1] = close[-2] * 2.28
                volume[-1] *= 20
            histories[ticker] = pd.DataFrame({
                "date": dates, "close": close, "volume": volume,
            })
        return histories

    def test_mrna_style_event_is_captured_and_labeled_unverified(self):
        discoveries = [
            {"ticker": "MRNA", "scanner_rank": 0,
             "scan_codes": ["TOP_PERC_GAIN", "HOT_BY_VOLUME"],
             "industry": "Healthcare"},
            {"ticker": "AAA", "scanner_rank": 1,
             "scan_codes": ["TOP_PERC_GAIN"], "industry": "Technology"},
            {"ticker": "BBB", "scanner_rank": 1,
             "scan_codes": ["TOP_PERC_LOSE"], "industry": "Financials"},
            {"ticker": "CCC", "scanner_rank": 2,
             "scan_codes": ["HOT_BY_VOLUME"], "industry": "Industrials"},
        ]
        result = compute_event_pulse(
            self.histories(), discoveries, REPORT_DATE,
            scans_completed=SCANS)
        mrna = next(row for row in result["candidates"]
                    if row["ticker"] == "MRNA")
        self.assertEqual(mrna["pulse_direction"], "RISK_ON")
        self.assertTrue(mrna["single_name_extreme"])
        self.assertEqual(mrna["catalyst_status"], "UNVERIFIED")
        self.assertEqual(result["state"], "ISOLATED_EVENT_SPIKE")
        text = format_event_pulse_summary(result)
        self.assertIn("MRNA", text)
        self.assertIn("新闻催化状态均为未核验", text)
        self.assertIn("市场级确认：否", text)

    def test_flat_high_volume_name_is_not_called_extreme_event(self):
        histories = self.histories()
        histories["MRNA"] = histories["AAA"].copy()
        histories["MRNA"].loc[
            histories["MRNA"].index[-1], "volume"] *= 20
        discoveries = [
            {"ticker": ticker, "scanner_rank": rank,
             "scan_codes": ["HOT_BY_VOLUME"],
             "industry": industry}
            for rank, (ticker, industry) in enumerate((
                ("MRNA", "Healthcare"), ("AAA", "Technology"),
                ("BBB", "Financials"), ("CCC", "Industrials")))
        ]
        result = compute_event_pulse(
            histories, discoveries, REPORT_DATE, scans_completed=SCANS)
        mrna = next(row for row in result["candidates"]
                    if row["ticker"] == "MRNA")
        self.assertGreater(mrna["abnormal_turnover_z60"], 5)
        self.assertFalse(mrna["single_name_extreme"])
        self.assertEqual(result["state"], "QUIET")


if __name__ == "__main__":
    unittest.main()
