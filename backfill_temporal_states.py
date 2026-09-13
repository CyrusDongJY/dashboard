# -*- coding: utf-8 -*-
"""Replay cross-panel temporal states and matured shadow outcomes.

The default is dry-run.  Use --write only after applying the temporal-state
database migration.  Replayed rows are always labeled REPLAY and never count
toward the 60-120 day prospective shadow window.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
import os
import sys

import pandas as pd
import pytz


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR in sys.path:
    sys.path.remove(MODULE_DIR)
sys.path.insert(0, MODULE_DIR)

from temporal_monitor import backfill_temporal_pipeline  # noqa: E402


NY_TZ = pytz.timezone("America/New_York")
logger = logging.getLogger("temporal_backfill")
logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="回放时间状态与已成熟的未来1/5/21日影子结果")
    parser.add_argument("--start", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", help="结束交易日 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=180,
                        help="未指定start时向前读取的交易日数，默认180")
    parser.add_argument("--write", action="store_true",
                        help="实际写库；缺省仅dry-run")
    return parser.parse_args(argv)


def _validated_date(value, label):
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        raise SystemExit(f"{label} 必须为 YYYY-MM-DD")


def _trading_days_back(days, end_date):
    try:
        from market_utils import trading_days_back
        return trading_days_back(days, end_date=end_date)
    except Exception:
        return (pd.Timestamp(end_date) - pd.Timedelta(
            days=int(days * 1.7) + 10)).strftime("%Y-%m-%d")


def main(argv=None):
    args = parse_args(argv)
    end_date = _validated_date(
        args.end or datetime.now(NY_TZ).strftime("%Y-%m-%d"), "--end")
    start_date = _validated_date(
        args.start or _trading_days_back(args.days, end_date=end_date), "--start")
    if start_date > end_date:
        raise SystemExit("--start 不能晚于 --end")

    try:
        import market_config as cfg
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit(f"依赖缺失: {exc}")

    supabase = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    result = backfill_temporal_pipeline(
        supabase, start_date=start_date, end_date=end_date,
        persist=args.write)
    states = result["states"]
    evaluations = result["evaluations"]
    logger.info(
        "时间状态回放完成: %s 至 %s, 状态 %d 行, 成熟评估 %d 行",
        start_date, end_date, len(states), len(evaluations))
    if args.write:
        if not result["state_write_ok"] or not result["evaluation_write_ok"]:
            raise SystemExit("写库未完整成功，请检查 temporal 表和日志")
        logger.info("写库完成；全部历史行明确标记为 REPLAY")
        if result.get("protected_live_count"):
            logger.info(
                "已保护 %d 条 LIVE_SHADOW 状态，未被回放覆盖",
                result["protected_live_count"])
    else:
        logger.info("DRY-RUN：未写入数据库；加 --write 才会落库")


if __name__ == "__main__":
    main()
