import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from core_funding_diagnostics import (CREDIT_SOURCES, balance_ledger, build_diagnostics,
    collect_credit_context, credit_observation, empty_credit_archive, format_diagnostics,
    score_dynamics, session_dates)
from core_funding_index import ObservationStore, build_core_history, compute_core_funding
from core_funding_pipeline import run_core_funding
from core_funding_report import generate_diagnostics_chart
from core_funding_sources import CoreFundingSources, NY, SOURCE_VERSION, digest_rows
from test_core_funding import fixture_rows, observation


def credit_fixture():
    """Synthetic source rows only; never captured market observations."""
    dates = session_dates("2026-09-24", 6)
    rows = []
    for key, start, finish in (("hy_oas", 2.8, 3.24), ("ig_oas", .8, .86), ("bbb_oas", 1., 1.06)):
        for day, value in zip(dates, np.linspace(start, finish, len(dates))):
            rows.append({"series": key, "value": value, "observation_date": str(day.date()),
                         "release_date": str(day.date()),
                         "available_at": (day.tz_localize(NY) + pd.Timedelta(hours=17)).isoformat(),
                         "source": CREDIT_SOURCES[key].fred_id, "source_version": SOURCE_VERSION})
    return {"rows": rows, "version": SOURCE_VERSION, "digest": digest_rows(rows)}


class FundingDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = fixture_rows()
        cls.store = ObservationStore(cls.rows)
        cls.history = build_core_history(cls.store, session_dates("2026-09-24", 64))
        cls.result = cls.history[-1]

    def test_multiple_horizons_have_exact_session_endpoints_and_reconcile(self):
        dynamics = score_dynamics(self.result, self.history)
        for count in (1, 5, 20, 63):
            item = dynamics[str(count)]
            self.assertEqual(item["status"], "OK")
            self.assertEqual(item["from_date"], self.history[-count - 1]["report_date"])
            self.assertAlmostEqual(item["delta"], self.result["score"] - self.history[-count - 1]["score"])
            self.assertAlmostEqual(item["delta"], sum(item["component_contributions"].values()) + item["rounding_residual"])
            self.assertAlmostEqual(sum(item["group_contributions"].values()), sum(item["component_contributions"].values()))

    def test_missing_middle_session_suppresses_window_not_just_endpoints(self):
        history = self.history[:-3] + self.history[-2:]
        dynamics = score_dynamics(self.result, history)
        self.assertEqual(dynamics["1"]["status"], "OK")
        self.assertIsNone(dynamics["5"]["delta"])

    def test_old_formula_replay_or_future_rows_never_patch_live_history(self):
        result = {**self.result, "observation_mode": "LIVE_SHADOW"}
        self.assertIsNone(score_dynamics(result, self.history)["1"]["delta"])
        old = [{**row, "calc_version": "old"} for row in self.history]
        self.assertIsNone(score_dynamics(self.result, old)["1"]["delta"])
        future = [{**row, "as_of": "2027-01-01T00:00:00Z"} for row in self.history]
        self.assertIsNone(score_dynamics(self.result, future)["1"]["delta"])

    def test_conflicting_duplicate_snapshot_is_not_silently_selected(self):
        history = self.history + [{**self.history[-2], "score": 99.}]
        self.assertIsNone(score_dynamics(self.result, history)["1"]["delta"])

    def test_level_does_not_determine_direction(self):
        before = copy.deepcopy(self.history[-2])
        before["score"] = 80.
        result = {**self.result, "score": 75.}
        item = score_dynamics(result, [before])["1"]
        self.assertEqual(item["direction"], "TIGHTENING")
        self.assertEqual(item["attribution_status"], "RECONCILIATION_FAILED")
        self.assertEqual(item["component_contributions"], {})

    def test_diagnostics_do_not_mutate_core_result(self):
        before = copy.deepcopy(self.result)
        output = build_diagnostics(self.result, self.history, self.store)
        self.assertEqual(self.result, before)
        self.assertFalse(output["score_eligible"])
        self.assertFalse(output["decision_eligible"])
        self.assertEqual(output["credit"]["status"], "UNAVAILABLE")
        json.dumps(output, allow_nan=False)

    def test_high_but_falling_credit_is_not_called_worsening(self):
        archive = credit_fixture()
        for row in archive["rows"]:
            if row["series"] == "hy_oas":
                row["value"] = 9. - row["value"]
        archive["digest"] = digest_rows(archive["rows"])
        result = {**self.result, "as_of": "2026-09-24T18:00:00-04:00"}
        with patch("core_funding_diagnostics.score_dynamics", return_value={
                "5": {"status": "OK", "direction": "IMPROVING"}}):
            output = build_diagnostics(result, [], self.store, archive)
            self.assertEqual(output["divergence"], "FUNDING_IMPROVES_CREDIT_STILL_ELEVATED")
            output = build_diagnostics(result, [], self.store, credit_fixture())
            self.assertEqual(output["divergence"], "FUNDING_IMPROVES_CREDIT_WORSENS")

    def test_synchronized_balance_ledger_uses_wednesday_rrp_not_latest_daily(self):
        rows = [observation("fed_assets", "2026-09-30", 6743.031),
                observation("tga", "2026-09-30", 984.046),
                observation("rrp", "2026-09-30", 11.539),
                observation("rrp", "2026-10-02", 1.501)]
        result = balance_ledger(rows, "2026-10-05T17:00:00-04:00")
        self.assertEqual(result["status"], "OK")
        self.assertAlmostEqual(result["net_b"], 5747.446)
        self.assertEqual(result["legs_b"]["rrp"], 11.539)
        self.assertEqual(result["latest_inputs"]["rrp"]["value_b"], 1.501)
        self.assertEqual(result["source_date"], "2026-09-30")
        self.assertTrue(result["quarter_end"])

    def test_week_average_tga_cannot_enter_balance_ledger(self):
        wrong = {**observation("tga", "2026-09-30", 948.674), "source": "WTREGEN"}
        with self.assertRaises(ValueError):
            balance_ledger([wrong], "2026-10-05T17:00:00-04:00")

    def test_balance_missing_latest_leg_does_not_present_old_common_date_as_current(self):
        rows = [observation("fed_assets", day, 6500.) for day in ("2026-09-23", "2026-09-30")]
        rows += [observation(key, "2026-09-23", 100.) for key in ("tga", "rrp")]
        result = balance_ledger(rows, "2026-10-05T17:00:00-04:00")
        self.assertEqual(result["status"], "NO_COMMON_LATEST_DATE")
        self.assertIsNone(result["net_b"])

    def test_balance_changes_are_calendar_weeks_with_contributions(self):
        result = balance_ledger(self.store, self.result["as_of"])
        for weeks, change in result["changes"].items():
            self.assertEqual((pd.Timestamp(change["to_date"]) - pd.Timestamp(change["from_date"])).days, int(weeks) * 7)
            self.assertAlmostEqual(change["net_change_b"], sum(change["contributions_b"].values()))
        self.assertEqual(result["percentile_observations"], 52)

    def test_empty_diagnostics_render_na_not_normal(self):
        result = compute_core_funding([], "2026-09-24T18:00:00-04:00")
        output = build_diagnostics(result, [], [])
        text = format_diagnostics(output)
        self.assertIn("NA", text)
        self.assertIn("缺数不等于无风险", text)
        self.assertNotIn("正常", text)

    def test_optional_diagnostics_failure_cannot_block_core_report(self):
        archive = {"rows": [], "version": SOURCE_VERSION, "digest": digest_rows([])}
        with tempfile.TemporaryDirectory() as directory, patch(
                "core_funding_pipeline.build_diagnostics", side_effect=ValueError("fixture")), patch(
                "core_funding_pipeline.generate_core_funding_chart", return_value="core.png"):
            output = run_core_funding(archive=archive, prices=pd.DataFrame(), sessions=21,
                                      output_dir=directory, as_of="2026-09-24T18:00:00-04:00")
            self.assertEqual(output["chart"], "core.png")
            self.assertEqual(output["diagnostics"]["status"], "UNAVAILABLE")
            self.assertIn("不影响核心评分", output["summary"])

    def test_chart_is_nonblank_and_missing_changes_stay_na(self):
        from PIL import Image
        import matplotlib.pyplot as plt
        result = compute_core_funding([], "2026-09-24T18:00:00-04:00")
        diagnostics = build_diagnostics(result, [], [], credit_fixture())
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(plt, "close"):
                path = generate_diagnostics_chart(result, diagnostics, Path(directory) / "chart.png",
                                                   dataset_label="TEST FIXTURE ONLY - NOT MARKET DATA")
                figure = plt.gcf()
                self.assertEqual(figure.axes[0].get_xlim(), (-.5, 3.5))
                self.assertEqual(len(figure.axes[0].get_xticklabels()), 4)
                self.assertEqual(sum(text.get_text() == "NA" for text in figure.axes[0].texts), 4)
            plt.close(figure)
            pixels = np.asarray(Image.open(path).convert("RGB"))
            self.assertGreater(pixels.std(), 10)


class CreditContextTests(unittest.TestCase):
    cutoff = "2026-09-24T18:00:00-04:00"

    def test_percent_to_basis_points_and_separate_level_change_guards(self):
        result = credit_observation(credit_fixture(), self.cutoff)
        hy = result["series"]["hy_oas"]
        self.assertAlmostEqual(hy["value_bp"], 324.)
        self.assertAlmostEqual(hy["change_5_sessions_bp"], 44.)
        self.assertEqual(hy["flags"], ["FAST_WIDENING"])
        self.assertEqual(hy["source_date"], "2026-09-24")
        self.assertEqual(result["status"], "WATCH")
        self.assertFalse(result["score_eligible"])

    def test_missing_middle_day_suppresses_five_session_change(self):
        archive = credit_fixture()
        archive["rows"] = [row for row in archive["rows"] if row["observation_date"] != "2026-09-21"]
        archive["digest"] = digest_rows(archive["rows"])
        result = credit_observation(archive, self.cutoff)
        self.assertIsNone(result["series"]["hy_oas"]["change_5_sessions_bp"])
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_partial_credit_never_becomes_complete_normal(self):
        archive = credit_fixture()
        archive["rows"] = [row for row in archive["rows"] if row["series"] == "hy_oas"]
        archive["digest"] = digest_rows(archive["rows"])
        result = credit_observation(archive, self.cutoff)
        self.assertEqual(result["status"], "WATCH_PARTIAL")
        self.assertAlmostEqual(result["coverage"], 1 / 3)

    def test_unpublished_and_stale_values_cannot_trigger_watch(self):
        self.assertEqual(credit_observation(credit_fixture(), "2026-09-16T18:00:00-04:00")["status"], "UNAVAILABLE")
        result = credit_observation(credit_fixture(), "2026-10-05T18:00:00-04:00")
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["series"]["hy_oas"]["flags"], [])
        self.assertIsNone(result["series"]["hy_oas"]["change_5_sessions_bp"])

    def test_archive_integrity_and_source_identity(self):
        archive = credit_fixture()
        archive["rows"][0]["value"] = 9.
        with self.assertRaises(ValueError):
            credit_observation(archive, self.cutoff)
        archive["digest"] = digest_rows(archive["rows"])
        archive["rows"][0]["source"] = "UNKNOWN"
        archive["digest"] = digest_rows(archive["rows"])
        with self.assertRaises(ValueError):
            credit_observation(archive, self.cutoff)

    def test_no_key_means_offline_no_network(self):
        with patch("core_funding_diagnostics.requests.Session") as session:
            self.assertEqual(collect_credit_context(None, "unused"), empty_credit_archive())
            session.assert_not_called()

    def test_optional_source_deadline_prevents_network_after_budget(self):
        session = Mock()
        source = CoreFundingSources("fixture", session=session, source_specs=CREDIT_SOURCES, deadline=0)
        with self.assertRaises(TimeoutError):
            source.fetch_initial_releases("hy_oas", "2023-01-01", self.cutoff)
        session.get.assert_not_called()

    def test_credit_source_uses_initial_release_and_preserves_percent_unit(self):
        session = Mock()
        response = session.get.return_value
        response.status_code = 200
        response.json.return_value = {"count": 1, "observations": [
            {"date": "2026-09-23", "realtime_start": "2026-09-24", "value": "3.24"}]}
        source = CoreFundingSources("fixture", session=session, source_specs=CREDIT_SOURCES)
        rows = source.fetch_initial_releases("hy_oas", "2023-01-01", self.cutoff)
        self.assertEqual(rows[0]["value"], 3.24)
        self.assertEqual(session.get.call_args.kwargs["params"]["output_type"], 4)
        self.assertEqual(rows[0]["source"], "BAMLH0A0HYM2")


if __name__ == "__main__":
    unittest.main()
