import ast
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core_funding_job import load_private_config, main, send_report

ROOT = Path(__file__).resolve().parents[1]


class CoreFundingIsolationTests(unittest.TestCase):
    def test_runtime_budget_leaves_shutdown_margin(self):
        source = (ROOT / "scripts" / "run_core_funding.sh").read_text()
        timeout = re.search(r"/usr/bin/timeout --kill-after=(\d+)s (\d+)m", source)
        self.assertIsNotNone(timeout)
        grace, minutes = map(int, timeout.groups())
        self.assertLessEqual(minutes * 60 + grace, 9 * 60)
        cpu_seconds = int(re.search(r"ulimit -t (\d+)", source).group(1))
        self.assertLessEqual(cpu_seconds, minutes * 60)

    def test_no_production_imports(self):
        allowed_local = {p.stem for p in ROOT.glob("core_funding_*.py")}
        production = {p.stem for p in ROOT.glob("*.py")} - allowed_local
        for path in ROOT.glob("core_funding_*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                         else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse({n.split(".")[0] for n in names} & production, path.name)

    def test_config_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"FRED_API_KEY": "fixture"}))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_private_config(path)
            path.chmod(0o600)
            self.assertEqual(load_private_config(path)["FRED_API_KEY"], "fixture")

    def test_holiday_skips_before_config_or_api(self):
        with patch("core_funding_job.load_private_config") as config:
            self.assertEqual(main(["--config", "unused", "--output-dir", "unused",
                                   "--as-of", "2026-09-26T17:20:00-04:00"]), 0)
            config.assert_not_called()

    def test_email_requires_write(self):
        with self.assertRaises(SystemExit):
            main(["--config", "unused", "--output-dir", "unused", "--email"])

    def test_smtp_receipt_and_replay_label(self):
        with tempfile.TemporaryDirectory() as directory, patch("core_funding_job.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            path = Path(directory) / "receipt.json"
            chart = Path(directory) / "chart.png"
            chart.write_bytes(b"fixture")
            config = {"SENDER_EMAIL": "test@example.com", "APP_PASSWORD": "test",
                      "RECEIVER_EMAIL": "test@example.com"}
            output = {"result": {"observation_mode": "PIT_REPLAY", "report_date": "2026-09-24"},
                      "summary": "fixture", "chart": str(chart)}
            self.assertEqual(send_report(config, output, path), "SENT")
            msg = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertIn("历史回放", msg["Subject"])
            self.assertEqual(len(list(msg.iter_attachments())), 1)
            self.assertEqual(send_report(config, output, path), "ALREADY_SENT")
            smtp.assert_called_once()

    def test_uncertain_delivery_is_not_automatically_repeated(self):
        with tempfile.TemporaryDirectory() as directory, patch("core_funding_job.smtplib.SMTP_SSL") as smtp:
            path = Path(directory) / "receipt.json"
            path.write_text('{"status": "PENDING"}')
            with self.assertRaises(RuntimeError):
                send_report({}, {}, path)
            smtp.assert_not_called()

    def test_optional_diagnostics_attachment_and_missing_file_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch("core_funding_job.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            chart = Path(directory) / "diagnostics.png"
            chart.write_bytes(b"fixture")
            config = {"SENDER_EMAIL": "test@example.com", "APP_PASSWORD": "test",
                      "RECEIVER_EMAIL": "test@example.com"}
            output = {"result": {"observation_mode": "PIT_REPLAY", "report_date": "2026-09-24"},
                      "summary": "fixture", "chart": None, "diagnostics_chart": str(chart)}
            send_report(config, output, Path(directory) / "first.json")
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(len(list(message.iter_attachments())), 1)
            output["diagnostics_chart"] = str(Path(directory) / "missing.png")
            send_report(config, output, Path(directory) / "second.json")
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(len(list(message.iter_attachments())), 0)

    def test_failed_database_blocks_email(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "core_funding_job.load_private_config", return_value={
                    "FRED_API_KEY": "x", "SUPABASE_URL": "x", "SUPABASE_KEY": "x"}), patch(
                "supabase.create_client"), patch("core_funding_job.run_core_funding", return_value={
                    "result": {"persistence_status": "FAILED"}, "summary": "FAILED", "chart": None}), patch(
                "core_funding_job.send_report") as email:
            with self.assertRaises(RuntimeError):
                main(["--config", "unused", "--output-dir", directory,
                      "--as-of", "2026-09-24T17:20:00-04:00", "--write", "--email"])
            email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
