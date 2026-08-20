import unittest

from daily_review import build_daily_review, format_daily_review_email


REPORT_DATE = "2026-08-14"


def market_row(**updates):
    row = {
        "record_date": REPORT_DATE,
        "pct_20ma": 76.0,
        "pct_50ma": 71.0,
        "pct_200ma": 69.0,
        "net_nh_nl": 125,
        "pct_adv": 66.0,
        "trin": 0.82,
        "up_down_volume_ratio": None,
        "qqq_qqqe_spread_pct": 0.20,
        "spy_rsp_spread_pct": 0.15,
        "mag7_rsp_spread_pct": 0.30,
        "concentration_quality": "OK",
        "vix": 15.2,
        "vvix": 88.0,
        "credit_spread": 3.1,
        "credit_z": 0.2,
        "tnx_z": 0.1,
        "liq_roc": 3.0,
        "liq_roc_window": 63,
    }
    row.update(updates)
    return row


def spot_row(**updates):
    row = {
        "date": REPORT_DATE,
        "qqq_mom_pct": 1.0,
        "vix_curve_state": "CONTANGO",
        "vix_contango_pct": 5.2,
    }
    for symbol, vwap in (("spy", 640.0), ("qqq", 575.0)):
        row.update({
            f"{symbol}_gap_acceptance": 1.1,
            f"{symbol}_gap_quality": "OK",
            f"{symbol}_vwap_time_acceptance_pct": 72.0,
            f"{symbol}_vwap_volume_acceptance_pct": 78.0,
            f"{symbol}_vwap_quality": "OK",
            f"{symbol}_open_to_close_pct": 0.8,
            f"{symbol}_last_vwap": vwap,
            f"{symbol}_close_vs_vwap_pct": 0.5,
        })
    row.update(updates)
    return row


def micro_rows():
    return [
        {
            "ticker": "SPY", "current_price": 642.0,
            "call_wall": 650.0, "put_wall": 630.0,
            "gamma_decision_eligible": True,
            "gamma_quality": {"ALL": "OK"}, "gamma_flip_quality": "OK",
        },
        {
            "ticker": "QQQ", "current_price": 578.0,
            "call_wall": 585.0, "put_wall": 565.0,
            "gamma_decision_eligible": True,
            "gamma_quality": {"ALL": "OK"}, "gamma_flip_quality": "OK",
        },
    ]


class DailyReviewTests(unittest.TestCase):
    def build(self, **overrides):
        args = {
            "market_row": market_row(),
            "spot_row": spot_row(),
            "prior_market_row": market_row(
                record_date="2026-08-13", pct_200ma=66.0, pct_adv=55.0),
            "prior_spot_row": spot_row(
                date="2026-08-13", vix_contango_pct=2.0),
            "prior_review": {"structure_state": "结构牛市"},
            "quality_rows": [],
            "metric_rows": [{
                "metric": "gex_billions", "value": 4.2,
                "percentile": 72.0, "zscore": 0.8,
                "source_date": REPORT_DATE,
            }],
            "anomaly_events": [],
            "micro_rows": micro_rows(),
        }
        args.update(overrides)
        return build_daily_review(REPORT_DATE, **args)

    def test_healthy_inputs_produce_consistent_three_layer_state(self):
        review = self.build()
        self.assertEqual(review["data_quality"], "HIGH")
        self.assertEqual(review["structure_state"], "结构牛市")
        self.assertEqual(review["tactical_state"], "趋势上涨")
        self.assertEqual(review["risk_state"], "低风险")
        self.assertEqual(review["modules"]["cash"]["state"], "Accepted")
        self.assertEqual(review["modules"]["breadth"]["state"], "Broad")

    def test_stale_cash_data_is_zero_weight_not_neutral(self):
        stale_spot = spot_row(date="2026-08-13")
        review = self.build(spot_row=stale_spot)
        self.assertEqual(review["data_quality"], "LOW")
        self.assertEqual(review["modules"]["cash"]["state"], "UNAVAILABLE")
        self.assertFalse(review["modules"]["cash"]["decision_eligible"])
        self.assertEqual(review["tactical_state"], "战术状态不可用")

    def test_suppressed_vol_with_rotation_is_fragile_low_vol(self):
        concentrated = market_row(
            pct_adv=52.0, qqq_qqqe_spread_pct=1.1,
            spy_rsp_spread_pct=0.9, mag7_rsp_spread_pct=1.2)
        review = self.build(market_row=concentrated)
        self.assertEqual(review["modules"]["breadth"]["state"], "Concentrated")
        self.assertEqual(review["tactical_state"], "资金轮动")
        self.assertEqual(review["risk_state"], "脆弱低波")

    def test_anomalies_are_ranked_and_limited_to_three(self):
        events = [
            {"report_date": REPORT_DATE, "metric": "vix", "scope": "MACRO",
             "window": "1D", "severity": 1, "confidence": 0.9,
             "explanation": "VIX上升"},
            {"report_date": REPORT_DATE, "metric": "credit_z", "scope": "MACRO",
             "window": "5D", "severity": 3, "confidence": 0.7,
             "explanation": "信用扩大"},
            {"report_date": REPORT_DATE, "metric": "trin", "scope": "MACRO",
             "window": "1D", "severity": 2, "confidence": 0.8,
             "explanation": "广度承压"},
            {"report_date": REPORT_DATE, "metric": "move", "scope": "MACRO",
             "window": "1D", "severity": 2, "confidence": 0.6,
             "explanation": "利率波动"},
        ]
        review = self.build(anomaly_events=events)
        self.assertEqual(len(review["anomalies"]), 3)
        self.assertEqual(review["anomalies"][0]["metric"], "credit_z")
        self.assertEqual(review["anomalies"][1]["metric"], "trin")

    def test_price_map_uses_gamma_only_when_quality_passes(self):
        rows = micro_rows()
        rows[1]["gamma_quality"] = {"ALL": "LOW_COVERAGE"}
        review = self.build(micro_rows=rows)
        self.assertEqual(review["price_map"]["SPY"]["breakout_confirmation"], 650.0)
        self.assertIsNone(review["price_map"]["QQQ"]["breakout_confirmation"])
        self.assertIsNone(review["price_map"]["QQQ"]["first_risk"])

    def test_price_map_requires_explicit_gamma_decision_eligibility(self):
        rows = micro_rows()
        rows[0].pop("gamma_decision_eligible")
        review = self.build(micro_rows=rows)
        self.assertIsNone(review["price_map"]["SPY"]["breakout_confirmation"])
        self.assertFalse(review["price_map"]["SPY"]["gamma_eligible"])

    def test_email_is_compact_and_does_not_render_raw_snapshot(self):
        review = self.build()
        email = format_daily_review_email(review)
        self.assertIn("① 今日一句话结论", email)
        self.assertIn("⑩ 三情景", email)
        self.assertIn("完整原始快照已入库", email)
        self.assertNotIn("全息数据库快照", email)
        self.assertNotIn("机密附件一", email)
        self.assertLess(len(email.splitlines()), 70)

    def test_email_translates_rule_enums_without_mutating_internal_states(self):
        rejected = spot_row(
            spy_vwap_time_acceptance_pct=25.0,
            spy_vwap_volume_acceptance_pct=30.0,
            spy_close_vs_vwap_pct=-0.8,
            qqq_vwap_time_acceptance_pct=20.0,
            qqq_vwap_volume_acceptance_pct=28.0,
            qqq_close_vs_vwap_pct=-1.0,
            qqq_mom_pct=-1.2,
        )
        mixed = market_row(
            pct_adv=50.0,
            trin=1.0,
            up_down_volume_ratio=1.0,
            vix=20.0,
            liq_roc=-3.0,
        )
        review = self.build(market_row=mixed, spot_row=rejected)
        email = format_daily_review_email(review)

        self.assertEqual(review["modules"]["cash"]["state"], "Rejected")
        self.assertEqual(review["modules"]["breadth"]["state"], "Rotation")
        self.assertEqual(review["modules"]["volatility"]["state"], "Normal")
        self.assertEqual(review["modules"]["credit"]["state"], "Stable")
        self.assertEqual(review["modules"]["rates"]["state"], "Neutral")
        self.assertEqual(review["modules"]["liquidity"]["state"], "Drain")
        self.assertIn("数据质量：高", email)
        self.assertIn("现金市场：价格未获接受 | 广度：轮动", email)
        self.assertIn("波动率：常态 | 信用：稳定", email)
        self.assertIn("利率：中性 | 流动性：收缩", email)
        self.assertIn("乐观:", email)
        self.assertIn("基准:", email)
        self.assertIn("悲观:", email)
        for raw_state in (
                "Rejected", "Rotation", "Normal", "Stable", "Neutral", "Drain"):
            self.assertNotIn(raw_state, email)


if __name__ == "__main__":
    unittest.main()
