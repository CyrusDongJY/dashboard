import subprocess
import unittest
from types import SimpleNamespace

from qqq_signal_bridge import render_qqq_signal_section


class QQQSignalBridgeTests(unittest.TestCase):
    def test_premarket_uses_section_mode_without_wait(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout="[QQQ 冻结策略信号监控]\n运行阶段: 盘前复核",
                stderr="",
            )

        section = render_qqq_signal_section(
            "pre_market",
            python_executable="/venv/python",
            builder_path="/app/multi_tenor_builder.py",
            runner=runner,
        )

        self.assertIn("运行阶段: 盘前复核", section)
        self.assertEqual(
            calls[0][0],
            [
                "/venv/python",
                "/app/multi_tenor_builder.py",
                "--mode",
                "qqq-section",
                "--stage",
                "pre_market",
            ],
        )
        self.assertFalse(calls[0][1]["check"])

    def test_postclose_waits_for_completed_daily_bar(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="complete", stderr="")

        render_qqq_signal_section(
            "post_close",
            python_executable="/venv/python",
            builder_path="/app/multi_tenor_builder.py",
            runner=runner,
        )

        self.assertEqual(commands[0][-1], "--wait-until-ready")

    def test_failure_is_fail_closed_and_keeps_parent_report_alive(self):
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

        section = render_qqq_signal_section(
            "pre_market",
            python_executable="/venv/python",
            builder_path="/app/multi_tenor_builder.py",
            runner=runner,
        )

        self.assertIn("调用失败", section)
        self.assertIn("不得据此执行", section)


if __name__ == "__main__":
    unittest.main()
