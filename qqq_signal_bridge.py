"""Attach staged QQQ frozen-signal output to existing dashboard reports."""

from __future__ import annotations

import os
import subprocess


VALID_STAGES = {"post_close", "pre_market"}


def render_qqq_signal_section(
    stage,
    *,
    python_executable=None,
    builder_path=None,
    timeout_seconds=None,
    runner=subprocess.run,
):
    if stage not in VALID_STAGES:
        raise ValueError(f"unsupported QQQ report stage: {stage}")
    python_executable = os.path.expanduser(
        python_executable
        or os.environ.get(
            "QQQ_BUILDER_PYTHON",
            "~/trading_venv/bin/python3",
        )
    )
    builder_path = os.path.expanduser(
        builder_path
        or os.environ.get(
            "QQQ_BUILDER_PATH",
            "~/TradingRadar/multi_tenor_builder.py",
        )
    )
    if timeout_seconds is None:
        timeout_seconds = 900 if stage == "post_close" else 120

    command = [
        python_executable,
        builder_path,
        "--mode",
        "qqq-section",
        "--stage",
        stage,
    ]
    if stage == "post_close":
        command.append("--wait-until-ready")

    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return (
            "\n[QQQ 冻结策略信号监控]\n"
            f"数据状态: {stage}阶段调用失败（{type(exc).__name__}）；"
            "原盘前/盘后报告继续生成，本阶段不得据此执行。\n"
        )

    output = (result.stdout or "").strip()
    if result.returncode == 0 and output:
        return "\n" + output + "\n"

    diagnostic = (result.stderr or output or "无诊断输出").strip().splitlines()
    detail = diagnostic[-1][:500] if diagnostic else "无诊断输出"
    return (
        "\n[QQQ 冻结策略信号监控]\n"
        f"数据状态: {stage}阶段返回失败（exit={result.returncode}）；"
        "原盘前/盘后报告继续生成，本阶段不得据此执行。\n"
        f"诊断: {detail}\n"
    )
