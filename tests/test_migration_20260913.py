from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migration_20260913_temporal_state.sql"


class TemporalMigrationTests(unittest.TestCase):
    def test_temporal_tables_preserve_mode_versions_and_outcomes(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS temporal_state_daily", sql)
        self.assertIn("PRIMARY KEY (report_date, panel)", sql)
        self.assertIn("observation_mode IN ('REPLAY', 'LIVE_SHADOW')", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS temporal_shadow_evaluation", sql)
        self.assertIn("horizon_days IN (1, 5, 21)", sql)
        self.assertIn("forward_return_pct", sql)
        self.assertIn("max_drawdown_pct", sql)
        self.assertIn("realized_vol_pct", sql)
        self.assertIn("max_abs_gap_pct", sql)

    def test_consolidated_migration_contains_temporal_tables(self):
        sql = (ROOT / "migrations.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS temporal_state_daily", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS temporal_shadow_evaluation", sql)


if __name__ == "__main__":
    unittest.main()
