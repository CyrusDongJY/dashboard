import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pandas as pd


REPORT_PATH = Path(__file__).resolve().parents[1] / 'daily_post_close.py'


def load_report_module():
    config = types.ModuleType('market_config')
    config.SUPABASE_URL = 'https://example.invalid'
    config.SUPABASE_KEY = 'test-only'
    config.SENDER_EMAIL = 'sender@example.invalid'
    config.RECEIVER_EMAIL = 'receiver@example.invalid'
    config.APP_PASSWORD = 'test-only'
    supabase = types.ModuleType('supabase')
    supabase.create_client = MagicMock()
    supabase.Client = object
    broker = types.ModuleType('ib_insync')
    broker.IB = MagicMock
    broker.Stock = lambda symbol, *_args: SimpleNamespace(symbol=symbol)
    broker.__all__ = ['IB', 'Stock']
    calendar = types.ModuleType('pandas_market_calendars')
    calendar.get_calendar = lambda _name: SimpleNamespace(
        schedule=lambda **_kwargs: pd.DataFrame())
    spec = importlib.util.spec_from_file_location(
        'post_close_delivery_test_module', REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
            'market_config': config, 'supabase': supabase,
            'ib_insync': broker, 'pandas_market_calendars': calendar}):
        spec.loader.exec_module(module)
    return module


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_report_module()
        self.ib = MagicMock()

    def test_connection_stays_readonly_and_uses_explicit_timeout(self):
        self.module.connect_ib_with_retry(self.ib)
        self.ib.connect.assert_called_once_with(
            '127.0.0.1', 4001, clientId=318,
            timeout=20, readonly=True, account='')

    def test_transient_timeouts_are_cleaned_up_then_retried(self):
        self.ib.connect.side_effect = [TimeoutError(), TimeoutError(), None]
        with patch.object(self.module.time, 'sleep') as sleep, self.assertLogs(
                self.module.logger, level='WARNING'):
            self.module.connect_ib_with_retry(self.ib)
        self.assertEqual(self.ib.connect.call_count, 3)
        self.assertEqual(self.ib.disconnect.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(3), call(6)])

    def test_exhaustion_preserves_exception_type_and_traceback(self):
        self.ib.connect.side_effect = TimeoutError()
        with patch.object(self.module.time, 'sleep'), self.assertLogs(
                self.module.logger, level='WARNING') as logs:
            with self.assertRaises(TimeoutError):
                self.module.connect_ib_with_retry(self.ib)
        self.assertEqual(self.ib.connect.call_count, 3)
        self.assertEqual(self.ib.disconnect.call_count, 3)
        self.assertIn('TimeoutError()', '\n'.join(logs.output))
        self.assertIn('Traceback', '\n'.join(logs.output))

    def test_incomplete_handshake_is_not_accepted_as_success(self):
        self.ib.isConnected.return_value = False
        with patch.object(self.module.time, 'sleep'), self.assertLogs(
                self.module.logger, level='WARNING'):
            with self.assertRaises(ConnectionError):
                self.module.connect_ib_with_retry(self.ib)
        self.assertEqual(self.ib.connect.call_count, 3)


class MailTests(unittest.TestCase):
    def setUp(self):
        self.module = load_report_module()
        self.smtp = MagicMock()
        self.smtp.sendmail.return_value = {}

    def test_smtp_has_a_timeout_and_success_requires_acceptance(self):
        with patch.object(self.module.smtplib, 'SMTP_SSL',
                          return_value=self.smtp) as smtp:
            self.assertTrue(self.module.send_email('subject', 'body'))
        smtp.assert_called_once_with('smtp.gmail.com', 465, timeout=15)
        self.smtp.sendmail.assert_called_once()
        self.smtp.quit.assert_called_once()

    def test_authentication_failure_is_not_reported_as_success(self):
        self.smtp.login.side_effect = RuntimeError('authentication unavailable')
        with patch.object(self.module.smtplib, 'SMTP_SSL',
                          return_value=self.smtp), self.assertLogs(
                self.module.logger, level='ERROR') as logs:
            self.assertFalse(self.module.send_email('subject', 'body'))
        self.smtp.sendmail.assert_not_called()
        self.assertIn('RuntimeError', '\n'.join(logs.output))

    def test_recipient_refusal_returns_failure(self):
        self.smtp.sendmail.return_value = {'receiver@example.invalid': (550, b'no')}
        with patch.object(self.module.smtplib, 'SMTP_SSL',
                          return_value=self.smtp), self.assertLogs(
                self.module.logger, level='ERROR'):
            self.assertFalse(self.module.send_email('subject', 'body'))

    def test_quit_failure_after_acceptance_does_not_resend(self):
        self.smtp.quit.side_effect = TimeoutError()
        with patch.object(self.module.smtplib, 'SMTP_SSL',
                          return_value=self.smtp), self.assertLogs(
                self.module.logger, level='WARNING'):
            self.assertTrue(self.module.send_email('subject', 'body'))
        self.smtp.sendmail.assert_called_once()
        self.smtp.close.assert_called_once()


class ReportFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = load_report_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module.POST_CLOSE_REPORT_DIR = Path(self.temp.name) / 'reports'
        self.module.ib = MagicMock()
        self.module.ib.qualifyContracts.side_effect = lambda *contracts: contracts
        self.module.ib.reqHistoricalData.return_value = [
            SimpleNamespace(close=100, open=100),
            SimpleNamespace(close=101, open=100),
        ]
        replacements = {
            'check_market_status': MagicMock(return_value=True),
            'is_trading_day': MagicMock(return_value=True),
            'get_market_breadth_ib': MagicMock(return_value=(
                'breadth-section\n', {'mag7_sample_count': 7})),
            'get_risk_capital_ladder_ib': MagicMock(return_value=(
                'capital-section', {'state': 'QUIET', 'coverage': 1})),
            'get_risk_event_pulse_ib': MagicMock(return_value=(
                'pulse-section', {'state': 'QUIET'})),
            'get_cash_acceptance_ib': MagicMock(return_value=(
                'acceptance-section\n', {})),
            'get_etf_structural_drag': MagicMock(return_value=(
                'drag-section\n', {})),
            'get_vix_term_structure': MagicMock(return_value=('vix-section\n', {})),
            'get_spot_poc_obv_ib': MagicMock(return_value=('poc-section\n', {})),
            'get_ivr_only': MagicMock(return_value=('ivr-section\n', {})),
            'get_unusual_options_activity': MagicMock(return_value='uoa-section\n'),
            'safe_upsert': MagicMock(return_value={}),
            'attach_metadata': MagicMock(),
            'log_data_quality': MagicMock(),
            'render_qqq_signal_section': MagicMock(return_value='qqq-section\n'),
            'append_to_spot_db_local': MagicMock(),
            'send_email': MagicMock(return_value=True),
        }
        for name, value in replacements.items():
            setattr(self.module, name, value)

    def status(self):
        return json.loads((self.module.POST_CLOSE_REPORT_DIR / 'latest.json').read_text())

    def test_success_keeps_original_sections_and_archives_before_email(self):
        def accepted(_subject, body):
            state = self.status()
            self.assertEqual(state['email_status'], 'pending')
            self.assertEqual(Path(state['report_path']).read_text(), body)
            return True

        self.module.send_email.side_effect = accepted
        with self.assertLogs(self.module.logger, level='INFO'):
            state = self.module.get_report()
        self.assertEqual(state['collection_status'], 'completed')
        self.assertEqual(state['email_status'], 'smtp_accepted')
        self.assertEqual(state['stock_spot_rows_written'], len(self.module.SYMBOLS))
        self.assertEqual(state, self.status())
        subject, body = self.module.send_email.call_args.args
        self.assertEqual(subject, '美股盘后复盘 - 客观数据切片')
        for section in ('breadth-section', 'capital-section', 'pulse-section',
                        'acceptance-section', 'vix-section', 'poc-section',
                        'ivr-section', 'uoa-section', 'drag-section', 'qqq-section'):
            self.assertIn(section, body)
        self.module.render_qqq_signal_section.assert_called_once_with(
            'post_close', python_executable=None, builder_path=None)
        self.assertEqual(self.module.safe_upsert.call_count, len(self.module.SYMBOLS) + 1)

    def test_ib_failure_sends_diagnostic_without_fake_data_or_stale_signal(self):
        self.module.ib.connect.side_effect = TimeoutError()
        with patch.object(self.module.time, 'sleep'), self.assertLogs(
                self.module.logger, level='WARNING'):
            state = self.module.get_report()
        self.assertEqual(self.module.ib.connect.call_count, 3)
        self.assertEqual(state['collection_status'], 'failed')
        self.assertEqual(state['error_type'], 'TimeoutError')
        self.assertEqual(state['qqq_signal_status'], 'not_started')
        self.assertEqual(state['email_status'], 'smtp_accepted')
        self.assertEqual(state, self.status())
        self.module.ib.qualifyContracts.assert_not_called()
        self.module.safe_upsert.assert_not_called()
        self.module.render_qqq_signal_section.assert_not_called()
        subject, body = self.module.send_email.call_args.args
        self.assertIn('抓取失败', subject)
        self.assertIn('TimeoutError()', body)
        self.assertIn('IB连接初始化', body)
        self.assertNotIn('盘后数据切片生成完毕', body)

    def test_later_failure_preserves_already_collected_sections(self):
        self.module.get_vix_term_structure.side_effect = ValueError('data unavailable')
        with self.assertLogs(self.module.logger, level='INFO'):
            state = self.module.get_report()
        self.assertEqual(state['failed_stage'], '宏观波动率')
        body = self.module.send_email.call_args.args[1]
        for section in ('breadth-section', 'capital-section', 'pulse-section',
                        'acceptance-section'):
            self.assertIn(section, body)
        self.assertIn('不可视为完整收盘结果', body)
        self.module.render_qqq_signal_section.assert_not_called()

    def test_mail_failure_is_persisted_without_a_false_success_log(self):
        self.module.send_email.return_value = False
        with self.assertLogs(self.module.logger, level='INFO') as logs:
            state = self.module.get_report()
        self.assertEqual(state['collection_status'], 'completed')
        self.assertEqual(self.status()['email_status'], 'failed')
        self.assertTrue(Path(state['report_path']).is_file())
        self.assertNotIn('邮件已获SMTP接受', '\n'.join(logs.output))

    def test_archive_failure_does_not_suppress_the_email(self):
        self.module.POST_CLOSE_REPORT_DIR = Path(self.temp.name) / 'occupied'
        self.module.POST_CLOSE_REPORT_DIR.write_text('not a directory')
        with self.assertLogs(self.module.logger, level='INFO'):
            state = self.module.get_report()
        self.module.send_email.assert_called_once()
        self.assertEqual(state['email_status'], 'smtp_accepted')
        self.assertEqual(state['archive_status'], 'failed')

    def test_disconnect_failure_does_not_suppress_report_delivery(self):
        self.module.ib.disconnect.side_effect = RuntimeError('cleanup failed')
        with self.assertLogs(self.module.logger, level='INFO'):
            state = self.module.get_report()
        self.assertEqual(state['email_status'], 'smtp_accepted')
        self.module.send_email.assert_called_once()

    def test_market_gates_remain_quiet(self):
        self.module.check_market_status.return_value = False
        self.assertIsNone(self.module.get_report())
        self.module.check_market_status.return_value = True
        self.module.is_trading_day.return_value = False
        with self.assertLogs(self.module.logger, level='INFO'):
            self.assertIsNone(self.module.get_report())
        self.module.ib.connect.assert_not_called()
        self.module.send_email.assert_not_called()
        self.assertFalse(self.module.POST_CLOSE_REPORT_DIR.exists())


if __name__ == '__main__':
    unittest.main()
