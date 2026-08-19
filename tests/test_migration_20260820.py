from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migration_20260820_risk_event_pulse.sql"
)


class Migration20260820Tests(unittest.TestCase):
    def test_dynamic_summary_and_candidate_tables_preserve_lineage(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS risk_event_pulse_daily", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS risk_event_candidate_daily", sql)
        self.assertIn("PRIMARY KEY (report_date, ticker)", sql)
        self.assertIn("market_confirmation", sql)
        self.assertIn("scans_completed", sql)
        self.assertIn("catalyst_status", sql)
        self.assertIn("decision_eligible", sql)
        self.assertIn("shadow_mode", sql)


if __name__ == "__main__":
    unittest.main()
