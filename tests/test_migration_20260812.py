from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migration_20260812_event_gamma_breadth.sql"
)


class Migration20260812Tests(unittest.TestCase):
    def test_null_historical_gamma_quality_fails_closed(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn(
            "decision_eligible = COALESCE(quality = 'OK', false)", sql)


if __name__ == "__main__":
    unittest.main()
