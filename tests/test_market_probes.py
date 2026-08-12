import unittest

import pandas as pd

from market_probes import _gamma_decision_eligible


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


if __name__ == "__main__":
    unittest.main()
