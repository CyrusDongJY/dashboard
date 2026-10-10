import unittest
from unittest.mock import MagicMock

import pandas as pd

from market_probes import _gamma_decision_eligible, scan_volatility_spot, scan_macro_regime


class GammaProbeGateTests(unittest.TestCase):
    def test_explicit_low_coverage_disables_directional_probe(self):
        row = pd.Series({
            "gamma_decision_eligible": False,
            "gamma_quality": {"ALL": "OK"},
        })
        self.assertFalse(_gamma_decision_eligible(row))

    def test_legacy_quality_is_supported_but_missing_quality_fails_closed(self):
        self.assertTrue(_gamma_decision_eligible(pd.Series({
            "gamma_quality": {"ALL": "OK"},
        })))
        self.assertFalse(_gamma_decision_eligible(pd.Series(dtype=object)))


class VixProbeTests(unittest.TestCase):
    def client(self, row):
        client = MagicMock()
        client.table.return_value.select.return_value.gte.return_value.order.return_value.execute.return_value.data = [row]
        return client

    def test_legacy_negative_spread_is_unavailable_not_alert(self):
        text, severity, _ = scan_volatility_spot(self.client({
            "vix_contango_pct": -8.0, "vix_m1": 20.0, "vix_m2": 18.4,
        }), "2026-10-01")
        self.assertEqual(severity, 0)
        self.assertIn("不可用", text)
        self.assertNotIn("倒挂", text)

    def test_monthly_flat_uses_price_dead_band(self):
        text, severity, _ = scan_volatility_spot(self.client({
            "vix_contango_pct": -0.8, "vix_m1": 10.0, "vix_m2": 9.92,
            "vix_curve_quality": "OK",
            "vix_curve_metadata": {"selection": "STANDARD_MONTHLY"},
        }), "2026-10-01")
        self.assertEqual(severity, 1)
        self.assertIn("平坦", text)
        self.assertNotIn("倒挂", text)

    def test_index_ratio_alert_requires_quality_and_is_named_separately(self):
        row = {"vix_term": "1.02 倒挂"}
        _, severity, _ = scan_macro_regime(self.client(row), "2026-10-01")
        self.assertEqual(severity, 0)
        row["full_metrics"] = {"vix_term_ratio_quality": {
            "status": "OK", "state": "BACKWARDATION",
        }}
        text, severity, _ = scan_macro_regime(self.client(row), "2026-10-01")
        self.assertEqual(severity, 2)
        self.assertIn("VIX 30D/3M期限比", text)
        self.assertNotIn("结构崩溃", text)


if __name__ == "__main__":
    unittest.main()
