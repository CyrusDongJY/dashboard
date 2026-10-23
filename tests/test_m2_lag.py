import copy
import importlib.util
import unittest

import numpy as np
import pandas as pd

from core_funding_m2 import build_m2_transmission, format_m2_transmission, impact_date
from core_funding_sources import NY, SOURCE_VERSION
from m2_lag_research import align_sample, analyze_panel, assemble_panel, growth, statistics, to_monthly


def native_m2(month, value=100, published=None):
    period = pd.Period(month, freq="M")
    available = (pd.Timestamp(published, tz=NY) if published else
                 period.to_timestamp(how="end").normalize().tz_localize(NY) + pd.Timedelta(days=25))
    return {"series": "m2", "observation_date": str(period.start_time.date()),
            "release_date": str(available.date()), "available_at": available.tz_convert("UTC").isoformat(),
            "source": "M2SL", "source_version": SOURCE_VERSION, "value": value}


class M2TransmissionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [native_m2(str(month), 100 + i) for i, month in enumerate(
            pd.period_range("2023-01", "2026-09", freq="M"))]
        self.cutoff = "2026-09-24T16:50:00-04:00"

    def test_three_calendar_months_preserve_month_end_and_leap_year(self):
        self.assertEqual(str(impact_date("2026-01").date()), "2026-04-30")
        self.assertEqual(str(impact_date("2023-11").date()), "2024-02-29")
        self.assertEqual(str(impact_date("2026-12").date()), "2027-03-31")

    def test_raw_dates_preserved_and_two_clocks_are_separate(self):
        before = copy.deepcopy(self.rows)
        result = build_m2_transmission(self.rows, self.cutoff)
        self.assertEqual(self.rows, before)
        self.assertEqual(result["current"]["observation_month"], "2026-07")
        self.assertEqual(result["lagged"]["observation_month"], "2026-05")
        self.assertEqual(result["lagged"]["assumed_impact_month"], "2026-08")
        self.assertEqual(result["lagged"]["source_date"], "2026-05-01")
        self.assertEqual(result["scope"], "US_M2_ONLY")
        self.assertFalse(result["score_eligible"])
        self.assertEqual(result["validation"], "UNVALIDATED_HYPOTHESIS")

    def test_not_visible_before_actual_publication_even_after_impact_date(self):
        rows = [native_m2("2025-05"), native_m2("2026-05", 105, "2026-09-15")]
        self.assertIsNone(build_m2_transmission(rows, "2026-09-14T17:00:00-04:00")["lagged"])
        lagged = build_m2_transmission(rows, "2026-09-16T17:00:00-04:00")["lagged"]
        self.assertEqual(lagged["observation_month"], "2026-05")
        self.assertEqual(pd.Timestamp(lagged["eligible_from"]).date(), pd.Timestamp("2026-09-15").date())

    def test_future_data_does_not_change_known_context(self):
        before = build_m2_transmission(self.rows, self.cutoff)
        after = build_m2_transmission(self.rows + [native_m2("2027-01", 100000)], self.cutoff)
        self.assertEqual(before, after)

    def test_missing_prior_year_not_forward_filled(self):
        result = build_m2_transmission([native_m2("2026-05")], self.cutoff)
        self.assertEqual(result["status"], "INSUFFICIENT")
        self.assertIsNone(result["lagged"])

    def test_stale_lagged_input_is_unavailable(self):
        result = build_m2_transmission([native_m2("2025-05"), native_m2("2026-05", 105)],
                                       "2026-10-20T17:00:00-04:00")
        self.assertIsNone(result["lagged"])

    def test_invalid_or_conflicting_inputs_rejected(self):
        with self.assertRaises(ValueError):
            build_m2_transmission([native_m2("2026-05"), native_m2("2026-05", 200)], self.cutoff)
        for value in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                build_m2_transmission([native_m2("2026-05", value)], self.cutoff)
        wrong = {**native_m2("2026-05"), "source": "GLOBAL_M2"}
        with self.assertRaises(ValueError):
            build_m2_transmission([wrong], self.cutoff)
        for lag in [True, -1, 1.5, 13]:
            with self.assertRaises(ValueError):
                impact_date("2026-01", lag)

    def test_summary_cannot_present_hypothesis_as_validated_global_score(self):
        text = format_m2_transmission(build_m2_transmission(self.rows, self.cutoff))
        self.assertIn("美国M2，非全球", text)
        self.assertIn("假设待验证", text)
        self.assertIn("不计入总分", text)
        self.assertIn("2026-05", text)
        self.assertIn("2026-08", text)
        alternate = format_m2_transmission(build_m2_transmission(self.rows, self.cutoff, lag_months=2))
        self.assertIn("2个月滞后观察", alternate)
        self.assertNotIn("三个月", alternate)

    def test_scoring_summary_does_not_claim_delayed_component_is_excluded(self):
        context = build_m2_transmission(self.rows, self.cutoff)
        component = {"lag_months": 3, "status": "OK", "value": 5.25,
                     "observation_month": "2026-05", "effective_date": "2026-08-31"}
        text = format_m2_transmission(context, scoring_component=component)
        self.assertIn("已包含在总分，不重复加分", text)
        self.assertIn("+5.25%", text)
        self.assertIn("假设待验证", text)
        self.assertNotIn("不计入总分", text)
        component["status"] = "MISSING_WINDOW"
        missing = format_m2_transmission(context, scoring_component=component)
        self.assertIn("不可用（MISSING_WINDOW）", missing)
        self.assertNotIn("+5.25%", missing)


class M2ResearchAlignmentTests(unittest.TestCase):
    def test_incomplete_month_cannot_supply_a_month_end_price(self):
        days = pd.bdate_range("2020-01-01", "2020-01-30")
        values = pd.Series(100., index=days)
        self.assertTrue(pd.isna(to_monthly(values, "close").iloc[-1]))
        values.loc[pd.Timestamp("2020-01-31")] = 101.
        self.assertEqual(to_monthly(values, "close").iloc[-1], 101.)

    def test_no_row_compression_when_a_calendar_month_is_missing(self):
        signal = pd.Series([1., 3.], index=pd.PeriodIndex(["2020-01", "2020-03"], freq="M"))
        returns = pd.Series([10., 20., 30.], index=pd.period_range("2020-04", "2020-06", freq="M"))
        sample = align_sample(signal, returns, 3)
        self.assertEqual(list(sample.index.astype(str)), ["2020-04", "2020-06"])
        self.assertEqual(sample.x.tolist(), [1., 3.])

    def test_growth_rejects_intermediate_gaps(self):
        series = pd.Series([100., np.nan, 120., 130.], index=pd.period_range("2020-01", periods=4, freq="M"))
        self.assertTrue(pd.isna(growth(series, 3).iloc[-1]))

    def test_currency_units_directions_and_no_partial_region_sum(self):
        months = pd.to_datetime(["2020-01-01", "2020-02-01"])
        days = pd.bdate_range("2020-01-01", "2020-02-28")
        data = {
            "us": pd.Series([2., 2.], index=months),
            "cn": pd.Series([7e9, 7e9], index=months),
            "jp": pd.Series([100e9, 100e9], index=months),
            "eu": pd.Series([1e9, 1e9], index=months),
            "cny_per_usd": pd.Series(np.where(days.month == 1, 7., 14.), index=days),
            "jpy_per_usd": pd.Series(100., index=days),
            "usd_per_eur": pd.Series(np.where(days.month == 1, 2., 3.), index=days),
        }
        frame, base = assemble_panel(data)
        self.assertEqual(base["month"], "2020-01")
        np.testing.assert_allclose(frame.G4_USD, [6., 6.5])
        np.testing.assert_allclose(frame.G4_FIXED_FX, [6., 6.])
        data["jp"].iloc[1] = np.nan
        frame, _ = assemble_panel(data)
        self.assertTrue(pd.isna(frame.G4_USD.iloc[1]))
        self.assertTrue(pd.isna(frame.G4_FIXED_FX.iloc[1]))

    @unittest.skipUnless(importlib.util.find_spec("statsmodels"), "Optional research dependency")
    def test_prespecified_three_month_shift_recovers_artificial_known_lag(self):
        rng = np.random.default_rng(84)
        dates = pd.period_range("2000-01", periods=180, freq="M")
        signal = pd.Series(rng.normal(size=len(dates)), index=dates)
        returns = signal.shift(3) + pd.Series(rng.normal(scale=.1, size=len(dates)), index=dates)
        right = statistics(align_sample(signal, returns, 3))
        wrong = statistics(align_sample(signal, returns, 0))
        self.assertGreater(right["pearson"], .98)
        self.assertLess(abs(wrong["pearson"]), .3)

    @unittest.skipUnless(importlib.util.find_spec("statsmodels"), "Optional research dependency")
    def test_revised_study_never_authorizes_production_shift_and_uses_equal_windows(self):
        rng = np.random.default_rng(27)
        dates = pd.period_range("1999-01", periods=220, freq="M")
        frame = pd.DataFrame({key: 100 * np.exp(np.cumsum(rng.normal(.003,.02,len(dates))))
                              for key in ("G4_USD", "G4_FIXED_FX", "NASDAQ100", "US_SHARE_PRICE")}, index=dates)
        result = analyze_panel(frame, {"month": "1999-01"})
        self.assertFalse(result["automatic_score_shift_allowed"])
        self.assertFalse(result["decision_eligible"])
        self.assertEqual(result["family_tests"], 56)
        self.assertEqual(len({row["n"] for row in result["rows"]}), 1)
        self.assertTrue(all("holm_p" in row for row in result["rows"]))


if __name__ == "__main__":
    unittest.main()
