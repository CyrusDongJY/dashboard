from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migration_20260819_risk_capital_ladder.sql"
)


class Migration20260819Tests(unittest.TestCase):
    def test_summary_and_member_tables_preserve_shadow_lineage(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS risk_capital_daily", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS risk_capital_member_daily", sql)
        self.assertIn("PRIMARY KEY (report_date, ticker)", sql)
        self.assertIn("shadow_mode", sql)
        self.assertIn("calc_version", sql)
        self.assertIn("decision_eligible", sql)


if __name__ == "__main__":
    unittest.main()
