import json
import unittest

from tactical_stress import StressInput, nullable_int, score_eod_stress


REPORT_DATE = "2026-07-23"


def item(value, lag=0, source_date=REPORT_DATE):
    return StressInput(value, source_date, lag)


def complete_inputs(vix=18.0, trin=1.0):
    return {
        "vix": item(vix),
        "move": item(90.0),
        "credit_spread": item(3.0),
        "trin": item(trin),
        "pcr": item(0.8),
        "cmf": item(0.0),
    }


class TacticalStressReliabilityTests(unittest.TestCase):
    def test_all_missing_is_unavailable_not_six(self):
        result = score_eod_stress({})
        self.assertIsNone(result.score)
        self.assertEqual(result.coverage, 0.0)
        self.assertIn("VIX_CORE_UNAVAILABLE", result.reasons)

    def test_vix_is_required_even_when_other_components_exist(self):
        inputs = complete_inputs()
        inputs["vix"] = item(None, source_date=None)
        result = score_eod_stress(inputs)
        self.assertIsNone(result.score)
        self.assertIn("VIX_CORE_UNAVAILABLE", result.reasons)

    def test_stale_vix_fails_closed(self):
        inputs = complete_inputs()
        inputs["vix"] = item(20.0, lag=1, source_date="2026-07-22")
        result = score_eod_stress(inputs)
        self.assertIsNone(result.score)
        self.assertEqual(result.components["vix"]["status"], "STALE_SOURCE")

    def test_weighted_partial_score_requires_both_confirmation_groups(self):
        inputs = {
            "vix": item(18.0),
            "move": item(90.0, lag=1, source_date="2026-07-22"),
            "trin": item(1.0),
        }
        result = score_eod_stress(inputs)
        self.assertEqual(result.score, 20)
        self.assertEqual(result.status, "PARTIAL")
        self.assertAlmostEqual(result.coverage, 0.72)
        self.assertLess(result.confidence, result.coverage)

    def test_missing_market_internal_confirmation_is_unavailable(self):
        result = score_eod_stress({
            "vix": item(22.0),
            "move": item(105.0),
            "credit_spread": item(5.1),
        })
        self.assertIsNone(result.score)
        self.assertIn("NO_MARKET_INTERNAL_CONFIRMATION", result.reasons)

    def test_missing_source_date_is_not_valid(self):
        inputs = complete_inputs()
        inputs["pcr"] = item(1.2, source_date=None)
        result = score_eod_stress(inputs)
        self.assertEqual(
            result.components["pcr"]["status"], "MISSING_SOURCE_DATE")

    def test_result_is_json_serializable(self):
        result = score_eod_stress(
            complete_inputs(), context={"vix_term_ratio": 0.9})
        encoded = json.dumps(result.to_dict())
        self.assertIn("eod_stress_v1.1-shadow", encoded)

    def test_nullable_int_never_turns_missing_into_zero(self):
        self.assertIsNone(nullable_int(None))
        self.assertIsNone(nullable_int(float("nan")))
        self.assertEqual(nullable_int(0), 0)


class TacticalStressFormulaCompatibilityTests(unittest.TestCase):
    def test_vix_boundaries_are_unchanged(self):
        expected = {12.0: 0, 18.0: 20, 25.0: 40, 35.0: 60}
        for vix, score in expected.items():
            with self.subTest(vix=vix):
                result = score_eod_stress(complete_inputs(vix=vix))
                self.assertEqual(result.score, score)

    def test_up_day_high_trin_remains_weaker_divergence_signal(self):
        inputs = complete_inputs(vix=18.0, trin=1.6)
        up_day = score_eod_stress(inputs, spy_down=False)
        down_day = score_eod_stress(inputs, spy_down=True)
        self.assertEqual(up_day.score, 30)
        self.assertEqual(down_day.score, 35)


if __name__ == "__main__":
    unittest.main()
