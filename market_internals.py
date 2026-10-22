"""Auditable EOD participation snapshots, independent of scores and alerts."""
from datetime import date, timedelta
from functools import lru_cache
import math
from statistics import median

import pandas as pd
import pandas_market_calendars as mcal


OBSERVATION_VERSION = "market_internals_observation_v1"
RECONSTRUCTION_VERSION = "sector_price_reconstruction_v1"
SECTORS = {
    "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费",
    "XLP": "日常消费", "XLE": "能源", "XLI": "工业", "XLU": "公用事业",
    "XLB": "材料", "XLRE": "房地产", "XLC": "通信",
}
PERIODS = {"1d": 1, "5d": 5, "21d": 21}
SAMPLE_SIZE = 500
MIN_COVERAGE = .90


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


@lru_cache(maxsize=128)
def schedule(day):
    end = date.fromisoformat(day)
    return mcal.get_calendar("NYSE").schedule(
        start_date=end - timedelta(days=70), end_date=end)


def eod_time(captured_at, source_date=None):
    """Require a timezone and a completed session, including early closes."""
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None or pd.isna(captured):
        raise ValueError("Capture timestamp must have a timezone")
    day = source_date or str(captured.tz_convert("America/New_York").date())
    dates = schedule(day)
    if day not in dates.index.strftime("%Y-%m-%d"):
        return day, False
    return day, bool(captured >= dates.loc[day, "market_close"] + pd.Timedelta(minutes=5))


def capture_breadth(stocks, captured_at):
    day, completed = eod_time(captured_at)
    constituents, seen, duplicate_count = [], set(), 0
    for item in stocks:
        if not isinstance(item, dict):
            continue
        values = item.get("d")
        symbol = item.get("s")
        if not isinstance(values, list) or len(values) < 9 or not isinstance(symbol, str):
            continue
        if symbol in seen:
            duplicate_count += 1
            continue
        seen.add(symbol)
        constituents.append({
            "symbol": symbol, "change_pct": finite(values[1]), "close": finite(values[2]),
            "sma20": finite(values[3]), "sma50": finite(values[4]), "sma200": finite(values[5]),
            "volume": finite(values[8]),
        })
    # Participation does not depend on volume availability; zero change is a flat stock.
    valid = [r for r in constituents if r["change_pct"] is not None
             and r["close"] is not None and r["close"] > 0 and r["change_pct"] > -100]
    counts = {name: sum(predicate(r["change_pct"]) for r in valid)
              for name, predicate in (("advancing", lambda x: x > 0),
                                      ("declining", lambda x: x < 0),
                                      ("unchanged", lambda x: x == 0))}
    coverage = len(valid) / SAMPLE_SIZE
    status = ("NOT_EOD" if not completed else "INVALID_SAMPLE" if duplicate_count or len(constituents) > SAMPLE_SIZE
              else "OK" if coverage >= MIN_COVERAGE else "LOW_COVERAGE" if constituents else "MISSING")
    ma = {}
    for window in (20, 50, 200):
        usable = [r for r in constituents if r["close"] is not None and r["close"] > 0
                  and r[f"sma{window}"] is not None and r[f"sma{window}"] > 0]
        above = sum(r["close"] > r[f"sma{window}"] for r in usable)
        ma[str(window)] = {"above": above, "valid": len(usable),
                           "pct": round(100 * above / len(usable), 4) if usable else None}
    return {
        "status": status, "source": "TradingViewScanner", "source_date": day,
        "captured_at": str(captured_at), "session": "EOD" if completed else "UNCONFIRMED",
        "scope": "top500_nyse_nasdaq_by_mktcap", "expected_size": SAMPLE_SIZE,
        "sample_size": len(constituents), "valid_count": len(valid), "coverage": coverage,
        "missing_count": len(constituents) - len(valid), "duplicate_count": duplicate_count,
        **counts, **{k + "_pct": round(v / len(valid) * 100, 4) if valid else None
                    for k, v in counts.items()},
        "median_return_pct": median(r["change_pct"] for r in valid) if valid else None,
        "date_basis": "CAPTURE_SESSION", "provider_timestamp_status": "UNAVAILABLE",
        "closing_auction_inclusion": "UNVERIFIED", "ma": ma, "constituents": constituents,
    }


def capture_sectors(raw_close, source_date, captured_at):
    day, completed = eod_time(captured_at, source_date)
    grid = schedule(day).index.strftime("%Y-%m-%d").tolist()[-22:]
    prices, observations = {}, {}
    symbols = ("SPY", "RSP", "QQQ", "QQQE", *SECTORS)
    for symbol in symbols:
        values = {}
        if isinstance(raw_close, pd.DataFrame) and symbol in raw_close.columns:
            for index, value in raw_close[symbol].items():
                number = finite(value)
                observed = str(pd.Timestamp(index).date())
                if observed in grid and number is not None and number > 0:
                    values[observed] = number
        prices[symbol] = [{"date": d, "close": v} for d, v in sorted(values.items())]
        returns, quality = {}, {}
        for period, size in PERIODS.items():
            wanted = grid[-size - 1:]
            complete = (completed and len(wanted) == size + 1 and wanted[-1] == day
                        and all(d in values for d in wanted))
            calculated = finite((values[day] / values[wanted[0]] - 1) * 100) if complete else None
            returns[period] = round(calculated, 6) if calculated is not None else None
            quality[period] = "OK" if calculated is not None else "NOT_EOD" if not completed else "MISSING_WINDOW"
        observations[symbol] = {"symbol": symbol, "label": SECTORS.get(symbol, symbol),
                                "source_date": max(values, default=None),
                                "returns": returns, "window_quality": quality}
    benchmark = observations["SPY"]["returns"]
    for symbol in SECTORS:
        row = observations[symbol]
        row["excess"] = {p: round(row["returns"][p] - benchmark[p], 6)
                         if row["returns"][p] is not None and benchmark[p] is not None else None
                         for p in PERIODS}
    valid = sum(observations[s]["returns"]["1d"] is not None for s in SECTORS)
    return {
        "source": "yfinance:adjusted_close", "source_date": day, "captured_at": str(captured_at),
        "session": "EOD" if completed else "UNCONFIRMED",
        "status": "OK" if valid == 11 else "PARTIAL" if valid else "MISSING",
        "scope": "sp500_sector_etfs", "valid_count": valid, "expected_size": 11,
        "sectors": [observations[s] for s in SECTORS],
        "benchmarks": {s: observations[s] for s in symbols[:4]}, "prices": prices,
        "return_basis": "adjusted_close; unfilled; exact NYSE windows",
    }


def reconstruct_sectors(observation, cutoff):
    """Rebuild sector-only windows from one saved price vintage, never breadth."""
    try:
        captured, limit = pd.Timestamp(observation["captured_at"]), pd.Timestamp(cutoff)
        day = observation["source_date"]
        if (captured.tzinfo is None or limit.tzinfo is None or captured > limit
                or observation.get("source") != "yfinance:adjusted_close"
                or observation.get("scope") != "sp500_sector_etfs"
                or observation.get("session") != "EOD"
                or observation.get("status") not in ("OK", "PARTIAL")
                or not eod_time(captured, day)[1]):
            return {}
        grid = schedule(day).index.strftime("%Y-%m-%d").tolist()
        columns = {}
        for symbol in ("SPY", "RSP", "QQQ", "QQQE", *SECTORS):
            values = {}
            for row in observation.get("prices", {}).get(symbol, []):
                d, v = row["date"], finite(row["close"])
                if d in values or d not in grid or v is None or v <= 0:
                    raise ValueError("Invalid saved price series")
                values[d] = v
            columns[symbol] = pd.Series(values, dtype=float)
        raw = pd.DataFrame(columns)
        result = {}
        for target in grid[-6:]:
            rebuilt = capture_sectors(raw, target, captured.isoformat())
            rebuilt.update(history_kind="RECONSTRUCTED", point_in_time_eligible=False,
                           reconstruction_version=RECONSTRUCTION_VERSION,
                           price_vintage_at=captured.isoformat())
            result[target] = rebuilt
        return result
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return {}
