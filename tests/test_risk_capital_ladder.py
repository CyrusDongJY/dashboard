import unittest

import numpy as np
import pandas as pd

from risk_capital_ladder import (
    ALL_MEMBERS,
    MEMBER_LAYER,
    aggregate_risk_capital,
    compute_risk_capital_ladder,
    format_risk_capital_summary,
)


REPORT_DATE = "2026-08-18"


def member(symbol, score=60, rel1=0.5, rel5=1.0, ret1=0.6, ret5=1.5,
           z=0.0, active="NORMAL", eligible=True, suspect=False):
    return {
        "report_date": REPORT_DATE,
        "ticker": symbol,
        "layer": MEMBER_LAYER[symbol],
        "member_score": score,
        "relative_1d_pct": rel1,
        "relative_5d_pct": rel5,
        "return_1d_pct": ret1,
        "return_5d_pct": ret5,
        "abnormal_turnover_z60": z,
        "turnover_ratio_20d": 1.0,
        "active_direction": active,
        "event_suspect": suspect,
        "sample_len": 140,
        "quality": "OK" if eligible else "MISSING",
        "decision_eligible": eligible,
    }


def benchmark(one_day=0.2, five_day=0.8):
    return {
        "source_date": REPORT_DATE,
        "return_1d_pct": one_day,
        "return_5d_pct": five_day,
        "quality": "OK",
    }


class RiskCapitalAggregationTests(unittest.TestCase):
    def test_three_layer_confirmation_produces_broad_expansion(self):
        rows = [member(symbol, score=80) for symbol in ALL_MEMBERS]
        result = aggregate_risk_capital(rows, benchmark(), REPORT_DATE)
        self.assertEqual(result["state"], "BROAD_EXPANSION")
        self.assertEqual(result["score"], 80.0)
        self.assertEqual(result["coverage"], 1.0)

    def test_qqq_up_while_members_lag_is_concentration_not_liquidity(self):
        rows = [member(
            symbol, score=20, rel1=-1.0, rel5=-2.0,
            ret1=-0.2, ret5=-1.0) for symbol in ALL_MEMBERS]
        result = aggregate_risk_capital(
            rows, benchmark(one_day=1.0, five_day=2.0), REPORT_DATE)
        self.assertEqual(result["state"], "MEGACAP_CONCENTRATION")
        self.assertIn("QQQ上涨", result["state_detail"])

    def test_high_turnover_down_move_requires_cross_member_confirmation(self):
        rows = [member(symbol, score=20, rel1=-1, rel5=-2,
                       ret1=-1, ret5=-2) for symbol in ALL_MEMBERS]
        for row in rows[:3]:
            row["abnormal_turnover_z60"] = 2.0
            row["active_direction"] = "DELEVERAGING"
        result = aggregate_risk_capital(
            rows, benchmark(one_day=-0.5, five_day=-1.0), REPORT_DATE)
        self.assertEqual(result["state"], "ACTIVE_DELEVERAGING")
        self.assertEqual(len(result["active_deleveraging"]), 3)

    def test_edge_assets_can_contract_before_institutional_layer(self):
        rows = []
        for symbol in ALL_MEMBERS:
            layer = MEMBER_LAYER[symbol]
            score = 70 if layer == "institutional" else (
                40 if layer == "speculative" else 20)
            rows.append(member(
                symbol, score=score,
                rel1=0.3 if layer == "institutional" else -0.3,
                rel5=0.5 if layer == "institutional" else -0.5,
                ret1=0.4 if layer == "institutional" else -0.2,
                ret5=0.8 if layer == "institutional" else -0.5))
        result = aggregate_risk_capital(rows, benchmark(0.0, 0.0), REPORT_DATE)
        self.assertEqual(result["state"], "EDGE_CONTRACTION")

    def test_one_company_event_cannot_create_blowoff(self):
        rows = [member(symbol, score=(40 if MEMBER_LAYER[symbol] == "institutional" else 50),
                       rel1=0.0, rel5=0.0,
                       ret1=0.0, ret5=0.0) for symbol in ALL_MEMBERS]
        mstr = next(row for row in rows if row["ticker"] == "MSTR")
        mstr.update({
            "member_score": 100,
            "relative_1d_pct": 18.0,
            "relative_5d_pct": 20.0,
            "return_1d_pct": 20.0,
            "return_5d_pct": 25.0,
            "active_direction": "RISK_ON",
            "event_suspect": True,
        })
        result = aggregate_risk_capital(rows, benchmark(), REPORT_DATE)
        self.assertNotEqual(result["state"], "SPECULATIVE_BLOWOFF")
        self.assertEqual(result["event_suspects"], ["MSTR"])

    def test_two_member_layers_fail_closed_when_one_member_is_missing(self):
        rows = [member(symbol, eligible=(symbol != "COIN"))
                for symbol in ALL_MEMBERS]
        result = aggregate_risk_capital(rows, benchmark(), REPORT_DATE)
        self.assertEqual(result["state"], "UNAVAILABLE")
        self.assertIsNone(result["score"])

    def test_layer_equal_weight_prevents_four_name_layer_dominance(self):
        rows = []
        for symbol in ALL_MEMBERS:
            score = 0 if MEMBER_LAYER[symbol] == "speculative" else 100
            rows.append(member(symbol, score=score))
        result = aggregate_risk_capital(rows, benchmark(), REPORT_DATE)
        self.assertAlmostEqual(result["score"], 66.7)


class RiskCapitalMemberTests(unittest.TestCase):
    def histories(self, volume_spike=1.0):
        dates = pd.bdate_range(end=REPORT_DATE, periods=90)
        histories = {}
        qqq_close = np.linspace(100, 110, len(dates))
        histories["QQQ"] = pd.DataFrame({
            "date": dates, "close": qqq_close,
            "volume": np.full(len(dates), 10_000_000),
        })
        for offset, symbol in enumerate(ALL_MEMBERS):
            close = np.linspace(40 + offset, 55 + offset, len(dates))
            volume = np.full(len(dates), 2_000_000.0)
            volume[-1] *= volume_spike
            histories[symbol] = pd.DataFrame({
                "date": dates, "close": close, "volume": volume,
            })
        return histories

    def test_abnormal_dollar_volume_uses_own_history(self):
        result = compute_risk_capital_ladder(
            self.histories(volume_spike=4.0), REPORT_DATE)
        nvda = next(row for row in result["members"] if row["ticker"] == "NVDA")
        self.assertTrue(nvda["decision_eligible"])
        self.assertGreater(nvda["turnover_ratio_20d"], 3.0)
        self.assertGreater(nvda["abnormal_turnover_z60"], 1.0)

    def test_stale_latest_bar_is_not_decision_eligible(self):
        histories = self.histories()
        histories["COIN"] = histories["COIN"].iloc[:-1]
        result = compute_risk_capital_ladder(histories, REPORT_DATE)
        coin = next(row for row in result["members"] if row["ticker"] == "COIN")
        self.assertEqual(coin["quality"], "STALE")
        self.assertFalse(coin["decision_eligible"])
        self.assertEqual(result["state"], "UNAVAILABLE")

    def test_summary_states_proxy_limit_and_lists_members(self):
        result = compute_risk_capital_ladder(self.histories(), REPORT_DATE)
        text = format_risk_capital_summary(result)
        self.assertIn("风险资本阶梯", text)
        self.assertIn("收盘价×RTH成交量代理", text)
        self.assertIn("不等同于美元流动性", text)
        for symbol in ALL_MEMBERS:
            self.assertIn(symbol, text)


if __name__ == "__main__":
    unittest.main()
