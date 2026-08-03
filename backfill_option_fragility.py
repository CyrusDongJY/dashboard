# -*- coding: utf-8 -*-
"""No-lookahead option-fragility history backfill.

IBKR supplies the underlying 30-day option-implied-volatility history used to
rebuild rolling IV Rank. ThetaData supplies same-day EOD option quotes/Greeks;
its morning open-interest report is joined as the previous-close OI known on
that date. The script writes only replayable aggregates to metric_daily and
never overwrites finalized EOD snapshots.
"""
from __future__ import annotations

import argparse
from datetime import date
import logging
import math
import os
import sys
import time
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


CONFIG_DIR = os.path.expanduser("~/market_dashboard")
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

logger = logging.getLogger("option_fragility_backfill")
logging.basicConfig(
    level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

DEFAULT_SYMBOLS = [
    "SPY", "QQQ", "AAPL", "MSFT", "GOOGL",
    "AMZN", "META", "NVDA", "TSLA", "ORCL",
]
THETA_SOURCE = "ThetaData:EODGreeks+previous_close_OI"
IB_SOURCE = "IBKR:OPTION_IMPLIED_VOLATILITY"


def rolling_iv_metrics(iv_history: pd.Series, lookback: int = 252,
                       min_obs: int = 60) -> pd.DataFrame:
    """Compute each day's IV Rank/Percentile using data available through that day."""
    from data_contracts import calculate_iv_rank_percentile

    clean = pd.to_numeric(iv_history, errors="coerce").replace(
        [np.inf, -np.inf], np.nan).dropna()
    clean = clean[clean > 0]
    clean.index = pd.to_datetime(clean.index).tz_localize(None).normalize()
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    records = []
    for as_of in clean.index:
        window = clean.loc[:as_of].tail(lookback)
        if len(window) < min_obs:
            continue
        stats = calculate_iv_rank_percentile(window)
        records.append({
            "date": as_of,
            "ivr_pct": stats["iv_rank_pct"],
            "iv_percentile_pct": stats["iv_percentile_pct"],
            "iv_sample_count": stats["sample_count"],
        })
    if not records:
        return pd.DataFrame(
            columns=["ivr_pct", "iv_percentile_pct", "iv_sample_count"])
    return pd.DataFrame(records).set_index("date").sort_index()


def _right_code(value) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in {"c", "call"}:
        return "C"
    if text in {"p", "put"}:
        return "P"
    return None


def _prepare_theta_contracts(greeks: pd.DataFrame, oi: pd.DataFrame,
                             as_of: date) -> pd.DataFrame:
    required = {"expiration", "strike", "right", "underlying_price", "gamma"}
    if greeks.empty or not required.issubset(greeks.columns) or oi.empty:
        return pd.DataFrame()
    if not {"expiration", "strike", "right", "open_interest"}.issubset(oi.columns):
        return pd.DataFrame()

    option = greeks.copy()
    interest = oi.copy()
    for frame in (option, interest):
        frame["_expiration"] = pd.to_datetime(
            frame["expiration"], errors="coerce").dt.date
        frame["_strike"] = pd.to_numeric(frame["strike"], errors="coerce").round(6)
        frame["_right"] = frame["right"].map(_right_code)

    if "timestamp" in interest:
        timestamps = pd.to_datetime(interest["timestamp"], errors="coerce", utc=True)
        known_date = timestamps.dt.tz_convert("America/New_York").dt.date
        interest = interest[known_date <= as_of].copy()
        interest["_timestamp"] = timestamps[known_date <= as_of]
        interest = interest.sort_values("_timestamp")
    interest["_oi"] = pd.to_numeric(
        interest["open_interest"], errors="coerce")
    interest = (interest.dropna(
        subset=["_expiration", "_strike", "_right", "_oi"])
        .drop_duplicates(["_expiration", "_strike", "_right"], keep="last"))

    option["_spot"] = pd.to_numeric(
        option["underlying_price"], errors="coerce")
    option["_gamma"] = pd.to_numeric(option["gamma"], errors="coerce")
    option["_bid"] = pd.to_numeric(option.get("bid"), errors="coerce")
    option["_ask"] = pd.to_numeric(option.get("ask"), errors="coerce")
    option["_dte"] = option["_expiration"].map(
        lambda expiry: (expiry - as_of).days if pd.notna(expiry) else np.nan)
    option = option.dropna(
        subset=["_expiration", "_strike", "_right", "_spot"])
    option = option[(option["_spot"] > 0) & option["_dte"].between(0, 7)]
    if option.empty or interest.empty:
        return pd.DataFrame()
    return option.merge(
        interest[["_expiration", "_strike", "_right", "_oi"]],
        on=["_expiration", "_strike", "_right"], how="left",
    )


def aggregate_theta_day(greeks: pd.DataFrame, oi: pd.DataFrame,
                        as_of: date) -> Dict[str, Optional[float]]:
    """Build the two production-compatible fragility aggregates for one date."""
    contracts = _prepare_theta_contracts(greeks, oi, as_of)
    if contracts.empty:
        return {
            "expected_move_pct": None, "short_gamma_m": None,
            "contract_count": 0, "oi_contract_count": 0,
        }
    spot = float(contracts["_spot"].median())

    expected_move = None
    nearest_expiry = min(contracts["_expiration"])
    nearest = contracts[contracts["_expiration"] == nearest_expiry].copy()
    valid_mid = (
        nearest["_bid"].gt(0) & nearest["_ask"].gt(0) &
        nearest["_ask"].ge(nearest["_bid"])
    )
    nearest.loc[valid_mid, "_mid"] = (
        nearest.loc[valid_mid, "_bid"] + nearest.loc[valid_mid, "_ask"]) / 2.0
    pairs = nearest.pivot_table(
        index="_strike", columns="_right", values="_mid", aggfunc="last")
    if {"C", "P"}.issubset(pairs.columns):
        pairs = pairs.dropna(subset=["C", "P"])
        if not pairs.empty:
            atm_strike = min(pairs.index, key=lambda strike: abs(strike - spot))
            expected_move = float(
                (pairs.loc[atm_strike, "C"] + pairs.loc[atm_strike, "P"])
                / spot * 100.0)

    gamma_rows = contracts[
        contracts["_gamma"].gt(0) & contracts["_oi"].gt(0)
    ].copy()
    short_gamma = None
    if not gamma_rows.empty and {"C", "P"}.issubset(set(gamma_rows["_right"])):
        signs = gamma_rows["_right"].map({"C": 1.0, "P": -1.0})
        exposure = (
            signs * gamma_rows["_gamma"] * gamma_rows["_oi"] *
            100.0 * spot ** 2 * 0.01 / 1e6
        )
        short_gamma = float(exposure.sum())

    return {
        "expected_move_pct": expected_move,
        "short_gamma_m": short_gamma,
        "contract_count": int(len(contracts)),
        "oi_contract_count": int(contracts["_oi"].notna().sum()),
    }


def fetch_ib_iv_history(symbols: Iterable[str], start_date: pd.Timestamp,
                        end_date: pd.Timestamp, host: str, port: int,
                        client_id: int) -> Dict[str, pd.Series]:
    try:
        from ib_insync import IB, Stock, util
    except ImportError as exc:
        raise RuntimeError("IB回填需要 ib_insync") from exc

    ib = IB()
    history: Dict[str, pd.Series] = {}
    try:
        ib.connect(host, port, clientId=client_id, timeout=20)
        duration_years = max(1, math.ceil((end_date - start_date).days / 365))
        end_text = (end_date + pd.Timedelta(days=1)).strftime(
            "%Y%m%d 23:59:59 US/Eastern")
        for symbol in symbols:
            try:
                contract = Stock(symbol, "SMART", "USD")
                qualified = ib.qualifyContracts(contract)
                if not qualified:
                    logger.warning("IB %s 合约验证失败", symbol)
                    continue
                bars = ib.reqHistoricalData(
                    qualified[0], endDateTime=end_text,
                    durationStr=f"{duration_years} Y", barSizeSetting="1 day",
                    whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True,
                    formatDate=1, keepUpToDate=False,
                )
                frame = util.df(bars)
                if frame.empty or not {"date", "close"}.issubset(frame.columns):
                    logger.warning("IB %s 未返回隐含波动率历史", symbol)
                    continue
                index = pd.DatetimeIndex(pd.to_datetime(
                    frame["date"], errors="coerce")).tz_localize(None).normalize()
                series = pd.Series(
                    pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
                    index=index,
                ).dropna()
                history[symbol] = series.loc[start_date:end_date]
                logger.info("IB %s IV history: %d rows", symbol, len(history[symbol]))
            except Exception as exc:
                logger.warning("IB %s IV回填失败: %s", symbol, exc)
    finally:
        if ib.isConnected():
            ib.disconnect()
    return history


def fetch_theta_history(symbols: Iterable[str], days: Iterable[pd.Timestamp],
                        max_dte: int = 7, strike_range: int = 25,
                        pause_seconds: float = 0.05
                        ) -> Dict[str, pd.DataFrame]:
    try:
        from thetadata import ThetaClient
    except ImportError as exc:
        raise RuntimeError(
            "Theta回填需要 Python 3.12+ 与 thetadata 包") from exc

    client = ThetaClient(dataframe_type="pandas")
    output: Dict[str, pd.DataFrame] = {}
    days = list(days)
    for symbol in symbols:
        records = []
        for offset, day in enumerate(days, start=1):
            as_of = day.date()
            try:
                greeks = client.option_history_greeks_eod(
                    symbol=symbol, expiration="*",
                    start_date=as_of, end_date=as_of,
                    max_dte=max_dte, strike_range=strike_range,
                    version="latest", underlyer_use_nbbo=True,
                )
                oi = client.option_history_open_interest(
                    symbol=symbol, expiration="*", date=as_of,
                    max_dte=max_dte, strike_range=strike_range,
                )
                metrics = aggregate_theta_day(greeks, oi, as_of)
                metrics["date"] = day
                records.append(metrics)
            except Exception as exc:
                logger.warning("Theta %s %s 跳过: %s", symbol, as_of, exc)
            if offset % 20 == 0:
                logger.info("Theta %s 进度 %d/%d", symbol, offset, len(days))
            if pause_seconds:
                time.sleep(pause_seconds)
        if records:
            output[symbol] = pd.DataFrame(records).set_index("date").sort_index()
    return output


def _rows_from_iv(history: Dict[str, pd.Series], output_start: pd.Timestamp,
                  output_end: pd.Timestamp) -> List[dict]:
    from backfill_history import replay

    rows = []
    for symbol, iv_series in history.items():
        metrics = rolling_iv_metrics(iv_series)
        for metric in ("ivr_pct", "iv_percentile_pct"):
            if metric not in metrics or metrics[metric].dropna().empty:
                continue
            rows.extend(replay(
                metric, metrics[metric].dropna(), IB_SOURCE,
                output_start, output_end, scope=symbol))
    return rows


def _rows_from_theta(history: Dict[str, pd.DataFrame],
                     output_start: pd.Timestamp,
                     output_end: pd.Timestamp) -> List[dict]:
    from backfill_history import replay

    rows = []
    for symbol, frame in history.items():
        for metric in ("expected_move_pct", "short_gamma_m"):
            if metric not in frame or frame[metric].dropna().empty:
                continue
            rows.extend(replay(
                metric, frame[metric].dropna(), THETA_SOURCE,
                output_start, output_end, scope=symbol))
    return rows


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill option fragility metrics without lookahead")
    parser.add_argument("--source", choices=("ib", "theta", "all"), default="all")
    parser.add_argument("--period", default="6mo", help="Output window, e.g. 6mo or 2y")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--write", action="store_true",
                        help="Write metric_daily; default is dry-run")
    parser.add_argument("--audit-csv", help="Optional path for generated rows")
    parser.add_argument("--ib-host", default=os.getenv("IB_HOST", "127.0.0.1"))
    parser.add_argument("--ib-port", type=int, default=int(os.getenv("IB_PORT", "4001")))
    parser.add_argument("--ib-client-id", type=int,
                        default=int(os.getenv("IB_CLIENT_ID", "41")))
    parser.add_argument("--theta-max-dte", type=int, default=7)
    parser.add_argument("--theta-strike-range", type=int, default=25)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    from backfill_history import finalized_keys, nyse_days, parse_period_days

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    output_end = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    output_start = output_end - pd.Timedelta(days=parse_period_days(args.period))
    days = nyse_days(output_start, output_end)
    rows: List[dict] = []

    if args.source in {"ib", "all"}:
        # Extra history makes the first output day's rolling IV calculation mature.
        ib_start = output_start - pd.Timedelta(days=730)
        rows.extend(_rows_from_iv(fetch_ib_iv_history(
            symbols, ib_start, output_end, args.ib_host,
            args.ib_port, args.ib_client_id), output_start, output_end))
    if args.source in {"theta", "all"}:
        rows.extend(_rows_from_theta(fetch_theta_history(
            symbols, days, max_dte=args.theta_max_dte,
            strike_range=args.theta_strike_range), output_start, output_end))

    logger.info("生成 %d 行 option-fragility metric_daily", len(rows))
    if args.audit_csv:
        pd.DataFrame(rows).to_csv(args.audit_csv, index=False)
        logger.info("审计明细已写入 %s", args.audit_csv)
    if not args.write:
        logger.info("DRY-RUN：未写入数据库；确认审计结果后添加 --write")
        return rows

    try:
        import market_config as cfg
        from market_utils import safe_upsert
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit(f"写库依赖缺失: {exc}")
    supabase = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    protected = finalized_keys(
        supabase, output_start.strftime("%Y-%m-%d"), output_end.strftime("%Y-%m-%d"))
    if protected is None:
        raise SystemExit("无法验证 finalized 快照，已停止写库")
    rows = [row for row in rows if (
        row["report_date"], row["metric"], row["scope"], row["session"]
    ) not in protected]
    logger.info("排除 finalized 生产快照后待写 %d 行", len(rows))
    for offset in range(0, len(rows), 500):
        result = safe_upsert(
            supabase, "metric_daily", rows[offset:offset + 500],
            conflict_cols="report_date,metric,scope,session")
        if result is None:
            raise RuntimeError(f"回填写入失败，批次起点 {offset}")
    logger.info("期权脆弱度历史回填完成")
    return rows


if __name__ == "__main__":
    main()
