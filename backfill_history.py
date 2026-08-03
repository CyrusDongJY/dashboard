# -*- coding: utf-8 -*-
"""可审计的宏观历史回补。

日频价格按交易日回补；低频 FRED 指标只用真实观测计算 z-score/分位，再映射到
NYSE 交易日用于画图。填充日保留真实 source_date，且不增加 effective_obs_count。
历史回补不会覆盖已经 finalized 的 EOD 快照。
"""
import logging
import io
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd
import pandas_market_calendars as mcal
import requests
import yfinance as yf

CONFIG_DIR = os.path.expanduser("~/market_dashboard")
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

from anomaly_engine import METRIC_REGISTRY, metric_snapshot, window_change  # noqa: E402
from market_utils import safe_upsert  # noqa: E402

logger = logging.getLogger("backfill")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

YF_TICKERS = [
    "^VIX", "^MOVE", "^VVIX", "^SKEW", "^VIX3M",
    "DX-Y.NYB", "HYG", "TLT",
]
FRED_MAP = {
    "BAMLH0A0HYM2": "credit_spread",
    "NFCI": "nfci",
    "WALCL": "Fed_Assets",
    "WTREGEN": "TGA",
    "RRPONTSYD": "RRP",
}
WINDOWS = {"chg_1d": 1, "chg_5d": 5, "chg_21d": 21, "chg_63d": 63}
MICRO_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "ORCL"]


def parse_period_days(period):
    match = re.fullmatch(r"(\d+)(y|mo)", period.lower())
    if not match:
        raise ValueError("period 必须形如 2y 或 18mo")
    value, unit = int(match.group(1)), match.group(2)
    return value * (365 if unit == "y" else 30)


def nyse_days(start, end):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex(schedule.index).tz_localize(None).normalize()


def fetch_yf(fetch_start):
    logger.info("拉取 yfinance 日频历史，起点 %s", fetch_start.date())
    df = yf.download(
        YF_TICKERS, start=fetch_start.strftime("%Y-%m-%d"), interval="1d",
        auto_adjust=False, progress=False)
    if df.empty:
        return pd.DataFrame()
    close = df["Adj Close"] if "Adj Close" in df else (df["Close"] if "Close" in df else df)
    if isinstance(close, pd.Series):
        close = close.to_frame()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index()


def fetch_fred(fetch_start, api_key):
    """只请求所需起点之后的观测，不再拉取全部历史。"""
    logger.info("拉取 FRED 原生频率历史，起点 %s", fetch_start.date())
    out = {}
    session = requests.Session()
    for series_id, name in FRED_MAP.items():
        try:
            response = session.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "observation_start": fetch_start.strftime("%Y-%m-%d"),
                },
                timeout=30,
            )
            response.raise_for_status()
            obs = response.json().get("observations", [])
            frame = pd.DataFrame(obs)
            if frame.empty:
                continue
            frame["date"] = pd.to_datetime(frame["date"])
            frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
            out[name] = frame.dropna(subset=["value"]).set_index("date")["value"]
        except Exception as exc:
            logger.warning("FRED %s 拉取失败: %s", series_id, exc)
    return pd.DataFrame(out).sort_index() if out else pd.DataFrame()


def fetch_squeeze_history(fetch_start):
    """现有DIX.csv包含历史行；按日期过滤后回补DIX/GEX。"""
    try:
        response = requests.get(
            "https://squeezemetrics.com/monitor/static/DIX.csv", timeout=30)
        response.raise_for_status()
        frame = pd.read_csv(io.StringIO(response.text))
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date").sort_index().loc[fetch_start:]
        return frame
    except Exception as exc:
        logger.warning("DIX/GEX 历史拉取失败: %s", exc)
        return pd.DataFrame()


def fetch_finra_history(days, tickers=MICRO_SYMBOLS):
    """可选回补FINRA Consolidated NMS日文件；返回 ticker -> 原生DPSV序列。"""
    values = {ticker: {} for ticker in tickers}
    session = requests.Session()
    for offset, day in enumerate(days, start=1):
        date_token = day.strftime("%Y%m%d")
        url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date_token}.txt"
        try:
            response = session.get(url, timeout=20)
            if response.status_code != 200:
                continue
            frame = pd.read_csv(io.StringIO(response.text), sep="|")
            frame = frame[frame["Symbol"].isin(tickers)].copy()
            frame["dpsv_pct"] = pd.to_numeric(frame["ShortVolume"], errors="coerce") / \
                pd.to_numeric(frame["TotalVolume"], errors="coerce") * 100
            for _, row in frame.dropna(subset=["dpsv_pct"]).iterrows():
                values[row["Symbol"]][day] = float(row["dpsv_pct"])
        except Exception as exc:
            logger.debug("FINRA %s 跳过: %s", date_token, exc)
        if offset % 50 == 0:
            logger.info("FINRA 回补进度 %d/%d", offset, len(days))
    return {ticker: pd.Series(series, dtype=float).sort_index()
            for ticker, series in values.items() if series}


def build_metric_series(yf_df, fred_df, squeeze_df=None):
    """返回 metric -> {series, source_name}，序列保持数据源原生观测频率。"""
    result = {}

    def add(metric, series, source):
        clean = pd.to_numeric(series, errors="coerce").dropna()
        clean = clean[~clean.index.duplicated(keep="last")].sort_index()
        if metric in METRIC_REGISTRY and not clean.empty:
            result[metric] = {"series": clean, "source_name": source}

    if not yf_df.empty:
        for ticker, metric in {
            "^VIX": "vix", "^MOVE": "move", "^VVIX": "vvix",
            "^SKEW": "skew", "DX-Y.NYB": "dxy",
        }.items():
            if ticker in yf_df:
                add(metric, yf_df[ticker], "yfinance")
        if {"^VIX", "^VIX3M"}.issubset(yf_df.columns):
            add("vix_ratio_contango_pct",
                (1.0 - yf_df["^VIX"] / yf_df["^VIX3M"]) * 100,
                "yfinance")
        if {"HYG", "TLT"}.issubset(yf_df.columns):
            add("hyg_tlt_ratio", yf_df["HYG"] / yf_df["TLT"], "yfinance")

    if not fred_df.empty:
        if "credit_spread" in fred_df:
            add("credit_spread", fred_df["credit_spread"], "FRED:BAMLH0A0HYM2")
        if "nfci" in fred_df:
            add("nfci", fred_df["nfci"], "FRED:NFCI")
        if {"Fed_Assets", "TGA", "RRP"}.issubset(fred_df.columns):
            # 在任一组成项出现真实观测的日期重算；ffill 只用于跨频率对齐组成项。
            aligned = fred_df[["Fed_Assets", "TGA", "RRP"]].sort_index().ffill().dropna()
            net_liq = aligned["Fed_Assets"] / 1000 - aligned["TGA"] / 1000 - aligned["RRP"]
            add("net_liq", net_liq, "FRED:WALCL+WTREGEN+RRPONTSYD")
    if squeeze_df is not None and not squeeze_df.empty:
        if "dix" in squeeze_df:
            add("dix_pct", squeeze_df["dix"] * 100, "SqueezeMetrics:DIX.csv")
        if "gex" in squeeze_df:
            add("gex_billions", squeeze_df["gex"] / 1e9, "SqueezeMetrics:DIX.csv")
    return result


def replay(metric, series, source_name, output_start, output_end, scope="MACRO"):
    """按NYSE交易日输出，但统计量只使用截至当日的原生有效观测。"""
    days = nyse_days(output_start, output_end)
    if len(days) == 0:
        return []
    carried = series.reindex(series.index.union(days)).sort_index().ffill().reindex(days)
    rows = []
    for day in days:
        native = series.loc[:day]
        if native.empty or pd.isna(carried.loc[day]):
            continue
        source_day = native.index[-1].normalize()
        carried_history = carried.loc[:day].dropna()
        row = metric_snapshot(
            metric, native, day.strftime("%Y-%m-%d"), scope=scope,
            source_date=source_day.strftime("%Y-%m-%d"), session="EOD",
            is_final=False, source_name=source_name,
            is_filled=source_day != day.normalize(), effective_obs_count=len(native))
        if not row:
            continue
        # 窗口变化按交易日的可见值计算；z/分位仍按原生独立观测计算。
        for field, window in WINDOWS.items():
            change = window_change(carried_history, window)
            row[field] = round(change, 4) if change is not None else None
        rows.append(row)
    return rows


def finalized_keys(supabase, start_date, end_date):
    try:
        rows, offset, page_size = [], 0, 1000
        while True:
            result = (supabase.table("metric_daily")
                      .select("report_date,metric,scope,session")
                      .eq("session", "EOD")
                      .eq("is_final", True)
                      .gte("report_date", start_date)
                      .lte("report_date", end_date)
                      .order("report_date").order("metric").order("scope")
                      .range(offset, offset + page_size - 1)
                      .execute())
            batch = result.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return {(str(r["report_date"]), r["metric"], r["scope"], r["session"])
                for r in rows}
    except Exception as exc:
        logger.warning("读取 finalized 快照失败，为避免覆盖生产数据，本次停止写库: %s", exc)
        return None


def main():
    args = [arg.lower() for arg in sys.argv[1:]]
    dry = "dry" in args
    include_finra = "finra" in args
    period = next((arg for arg in args if arg.endswith("y") or arg.endswith("mo")), "2y")
    period_days = parse_period_days(period)
    output_end = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    output_start = output_end - pd.Timedelta(days=period_days)
    # NFCI 为周频；多取约六年用于形成最多252个原生观测的基准。
    fetch_start = output_start - pd.Timedelta(days=6 * 365)

    try:
        import market_config as cfg
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit(f"依赖缺失: {exc}")

    yf_df = fetch_yf(fetch_start)
    fred_df = fetch_fred(fetch_start, cfg.FRED_API_KEY)
    squeeze_df = fetch_squeeze_history(fetch_start)
    metrics = build_metric_series(yf_df, fred_df, squeeze_df)
    rows = []
    for metric, payload in metrics.items():
        metric_rows = replay(
            metric, payload["series"], payload["source_name"], output_start, output_end)
        rows.extend(metric_rows)
        logger.info("%-20s 回补 %d 个NYSE交易日", metric, len(metric_rows))

    if include_finra:
        output_days = nyse_days(output_start, output_end)
        for ticker, series in fetch_finra_history(output_days).items():
            metric_rows = replay(
                "dpsv_pct", series, "FINRA:ConsolidatedNMS",
                output_start, output_end, scope=ticker)
            rows.extend(metric_rows)
            logger.info("dpsv_pct[%s] 回补 %d 个NYSE交易日", ticker, len(metric_rows))

    logger.info("合计生成 %d 行 metric_daily", len(rows))
    if dry:
        logger.info("DRY-RUN：未写入数据库")
        return

    supabase = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    protected = finalized_keys(
        supabase, output_start.strftime("%Y-%m-%d"), output_end.strftime("%Y-%m-%d"))
    if protected is None:
        return
    rows = [row for row in rows if (
        row["report_date"], row["metric"], row["scope"], row["session"]) not in protected]
    logger.info("排除 finalized 生产快照后待写 %d 行", len(rows))
    for offset in range(0, len(rows), 500):
        chunk = rows[offset:offset + 500]
        result = safe_upsert(
            supabase, "metric_daily", chunk,
            conflict_cols="report_date,metric,scope,session")
        if result is None:
            raise RuntimeError(f"回补写入失败，批次起点 {offset}")
    logger.info("历史回补完成；填充点保留真实 source_date，未覆盖 finalized 数据")


if __name__ == "__main__":
    main()
