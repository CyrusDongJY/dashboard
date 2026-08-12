import sys
import types
import unittest
from pathlib import Path

import pandas as pd


ANALYST_PATH = Path(__file__).resolve().parents[1] / "auto_analyst.py"


class _Calendar:
    def schedule(self, start_date, end_date):
        return pd.DataFrame(index=pd.bdate_range(start_date, end_date))


calendar_module = types.ModuleType("pandas_market_calendars")
calendar_module.get_calendar = lambda _name: _Calendar()
sys.modules["pandas_market_calendars"] = calendar_module

market_config = types.ModuleType("market_config")
sys.modules["market_config"] = market_config

supabase = types.ModuleType("supabase")
supabase.create_client = lambda *_args, **_kwargs: None
supabase.Client = object
sys.modules["supabase"] = supabase

genai = types.ModuleType("google.genai")
google = types.ModuleType("google")
google.genai = genai
sys.modules["google"] = google
sys.modules["google.genai"] = genai

from auto_analyst import format_raw_appendix  # noqa: E402


class AutoAnalystFormattingTests(unittest.TestCase):
    def test_shared_dashboard_modules_take_import_precedence(self):
        source = ANALYST_PATH.read_text(encoding="utf-8")
        self.assertIn("sys.path.insert(0, CONFIG_DIR)", source)
        self.assertNotIn("sys.path.append(CONFIG_DIR)", source)

    def test_close_report_reuses_verified_premarket_event_context(self):
        text = format_raw_appendix({}, {}, [{
            "ticker": "QQQ",
            "expected_move_event_name": "美国CPI",
            "expected_move_event_at": "2026-08-12T08:30:00-04:00",
            "expected_move_event_trading_days": 1,
            "expected_move_event_risk": "HIGH",
            "expected_move_event_status": "HIGH_IMPACT_NEXT_SESSION",
            "gamma_decision_eligible": False,
        }])
        self.assertIn("【已核验重大事件】美国CPI", text)
        self.assertIn("2026-08-12 08:30 ET", text)
        self.assertIn("距离1个交易日", text)
        self.assertIn("风险HIGH", text)
        self.assertIn("Gamma数据不足", text)


if __name__ == "__main__":
    unittest.main()
