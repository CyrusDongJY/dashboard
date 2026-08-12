import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SNIPER_SCRIPT = ROOT / "ib_intraday_sniper.py"
if not SNIPER_SCRIPT.exists():
    SNIPER_SCRIPT = Path.home() / "TradingRadar" / "ib_intraday_sniper.py"


class IntradaySniperDeploymentTests(unittest.TestCase):
    def _isolated_run(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            radar = home / "TradingRadar"
            shared = home / "market_dashboard"
            stubs = home / "stubs"
            radar.mkdir()
            shared.mkdir()
            stubs.mkdir()

            shutil.copy2(SNIPER_SCRIPT, radar)
            (shared / "market_config.py").write_text(textwrap.dedent("""
                SUPABASE_URL = "https://example.invalid"
                SUPABASE_KEY = "test-key"
                SENDER_EMAIL = "sender@example.com"
                RECEIVER_EMAIL = "receiver@example.com"
                APP_PASSWORD = "test-password"
                BARK_KEY = "test-bark-key"
                ENABLE_GAMMA_FLIP_ALERT = False
            """), encoding="utf-8")
            (stubs / "ib_insync.py").write_text(textwrap.dedent("""
                class IB:
                    pass
                class Stock:
                    pass
                class Index:
                    pass
            """), encoding="utf-8")
            (stubs / "supabase.py").write_text(textwrap.dedent("""
                class Client:
                    pass
                def create_client(*args, **kwargs):
                    return Client()
            """), encoding="utf-8")
            (stubs / "pandas_market_calendars.py").write_text(
                "def get_calendar(*args, **kwargs):\n    return None\n",
                encoding="utf-8",
            )
            (stubs / "pytz.py").write_text(
                "def timezone(*args, **kwargs):\n    return object()\n",
                encoding="utf-8",
            )
            (stubs / "pandas.py").write_text("", encoding="utf-8")
            (stubs / "numpy.py").write_text("", encoding="utf-8")
            (stubs / "requests.py").write_text(
                "def get(*args, **kwargs):\n    return None\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(stubs)
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(body)],
                cwd=radar,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return result.stdout

    def test_imports_shared_config_from_cloud_directory_layout(self):
        output = self._isolated_run("""
            import runpy
            runpy.run_path("ib_intraday_sniper.py", run_name="bootstrap_test")
            print("BOOTSTRAP_OK")
        """)
        self.assertIn("BOOTSTRAP_OK", output)

    def test_uses_valid_ad_contract_and_disables_phantom_ud_contracts(self):
        output = self._isolated_run("""
            import runpy

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="contract_plan_test")
            assert module["IBKR_BREADTH_CONTRACTS"] == {
                "TICK": ("TICK-NYSE", "NYSE"),
                "TRIN": ("TRIN-NYSE", "NYSE"),
                "AD": ("AD-NYSE", "NYSE"),
            }
            assert module["UD_UNSUPPORTED_STATUS"] == (
                "UNSUPPORTED_BY_IBKR_CONTRACT")
            serialized = repr(module["IBKR_BREADTH_CONTRACTS"])
            for invalid in ("ADV-NYSE", "DECL-NYSE", "UVOL-NYSE",
                            "DVOL-NYSE"):
                assert invalid not in serialized
            print("CONTRACT_PLAN_OK")
        """)
        self.assertIn("CONTRACT_PLAN_OK", output)

    def test_zero_breadth_is_valid_and_missing_stays_none(self):
        output = self._isolated_run("""
            import runpy
            from types import SimpleNamespace

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="zero_semantics_test")
            sentinel = module["GlobalSentinel"].__new__(
                module["GlobalSentinel"])
            sentinel.log = lambda _message: None
            sentinel.ib = SimpleNamespace(reqHistoricalData=lambda *a, **k: [])
            contract = SimpleNamespace(symbol="AD-NYSE")
            zero_ticker = SimpleNamespace(
                last=0.0,
                close=float("nan"),
                marketPrice=lambda: float("nan"),
            )
            assert sentinel.get_robust_index_val(
                contract, zero_ticker, allow_zero=True,
                allow_history=False) == 0.0
            assert sentinel.get_robust_index_val(
                contract, zero_ticker, allow_zero=False,
                allow_history=False) is None
            stale_close_ticker = SimpleNamespace(
                last=float("nan"),
                close=0.0,
                marketPrice=lambda: float("nan"),
            )
            assert sentinel.get_robust_index_val(
                contract, stale_close_ticker, allow_zero=True,
                allow_history=False) is None
            assert sentinel.get_robust_index_val(
                contract, None, allow_zero=True,
                allow_history=False) is None
            print("ZERO_SEMANTICS_OK")
        """)
        self.assertIn("ZERO_SEMANTICS_OK", output)

    def test_repeated_or_old_ad_quote_is_stale(self):
        output = self._isolated_run("""
            import runpy

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="stale_breadth_test")
            classify = module["classify_breadth_status"]
            assert classify(1117, [1117, 1117], 10) == ("STALE_VALUE", 3)
            assert classify(1118, [1117, 1117], 181) == ("STALE_VALUE", 1)
            assert classify(1118, [1117, 1117], 10) == ("OK", 1)
            assert classify(
                1117, [1117], 10, prior_statuses=["STALE_VALUE"]
            ) == ("STALE_VALUE", 2)
            assert classify(
                1117, [], 10, history_available=False
            ) == ("HISTORY_UNAVAILABLE", 0)
            assert classify(1117, [], None) == ("MISSING_TIMESTAMP", 1)
            print("STALE_BREADTH_OK")
        """)
        self.assertIn("STALE_BREADTH_OK", output)

    def test_stale_ad_switches_to_price_vwap_framework(self):
        output = self._isolated_run("""
            import runpy

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="framework_test")
            select = module["select_intraday_decision_framework"]
            assert select("OK") == {
                "breadth_state": "AVAILABLE",
                "decision_framework": "NYSE_AD_PRICE_VWAP_TRIN_CTICK",
            }
            assert select("STALE_VALUE") == {
                "breadth_state": "UNAVAILABLE",
                "decision_framework": "PRICE_VWAP_TRIN_CTICK",
            }
            print("FRAMEWORK_SWITCH_OK")
        """)
        self.assertIn("FRAMEWORK_SWITCH_OK", output)

    def test_gamma_value_requires_explicit_decision_eligibility(self):
        output = self._isolated_run("""
            import runpy

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="gamma_gate_test")
            gate = module["gamma_decision_value"]
            assert gate(512.5, True) == 512.5
            assert gate(512.5, False) is None
            assert gate(512.5, None) is None
            print("GAMMA_DISPLAY_GATE_OK")
        """)
        self.assertIn("GAMMA_DISPLAY_GATE_OK", output)

    def test_stale_ad_raw_value_is_audit_only_in_report(self):
        source = SNIPER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("原始值仅保留在add_raw审计字段", source)
        self.assertNotIn("原始={display_signed(add_raw)}", source)

    def test_post_close_breadth_uses_record_date_schema(self):
        source = SNIPER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".select('record_date,pct_adv", source)
        self.assertIn(".eq('record_date', today_str)", source)
        self.assertNotIn(".select('date,pct_adv", source)

    def test_actionable_ib_errors_are_persisted(self):
        output = self._isolated_run("""
            import runpy
            from types import SimpleNamespace

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="ib_error_test")
            sentinel = module["GlobalSentinel"].__new__(
                module["GlobalSentinel"])
            sentinel.ib_errors = []
            sentinel.log = lambda _message: None
            contract = SimpleNamespace(symbol="BAD-NYSE")
            sentinel._capture_ib_error(7, 200, "No security definition", contract)
            sentinel._capture_ib_error(-1, 2104, "farm is OK", None)
            assert sentinel.ib_errors == [{
                "req_id": 7,
                "code": 200,
                "symbol": "BAD-NYSE",
                "message": "No security definition",
            }]
            print("IB_ERROR_CAPTURE_OK")
        """)
        self.assertIn("IB_ERROR_CAPTURE_OK", output)

    def test_send_email_returns_delivery_status(self):
        output = self._isolated_run("""
            import runpy

            module = runpy.run_path(
                "ib_intraday_sniper.py", run_name="email_status_test")

            class WorkingSMTP:
                def __init__(self, *args, **kwargs):
                    pass
                def login(self, *args, **kwargs):
                    pass
                def sendmail(self, *args, **kwargs):
                    pass
                def quit(self):
                    pass

            class BrokenSMTP:
                def __init__(self, *args, **kwargs):
                    raise OSError("offline")

            sentinel = module["GlobalSentinel"].__new__(
                module["GlobalSentinel"])
            module["smtplib"].SMTP_SSL = WorkingSMTP
            assert sentinel.send_email("subject", "body") is True
            module["smtplib"].SMTP_SSL = BrokenSMTP
            assert sentinel.send_email("subject", "body") is False
            print("EMAIL_STATUS_OK")
        """)
        self.assertIn("EMAIL_STATUS_OK", output)


if __name__ == "__main__":
    unittest.main()
