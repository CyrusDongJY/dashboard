"""VIX 30D/3M index ratio from native, contemporaneous daily closes."""

import math

import pandas as pd

from data_contracts import classify_vix_curve


def compute_vix_index_ratio(raw_close):
    """Reject mismatched latest dates rather than carrying a leg forward.

    Missing observations are absent bars. A present but invalid latest quote
    (zero, negative, infinity, or non-numeric) fails closed; it must not cause a
    fallback to an older valid price. ``raw_close`` must be the unfilled frame.
    """
    result = {
        "ratio": None,
        "state": "UNAVAILABLE",
        "status": "MISSING_SOURCE",
        "reasons": [],
        "source_date": None,
        "source_name": "yfinance:^VIX/^VIX3M:native_daily_close",
        "source_dates": {"^VIX": None, "^VIX3M": None},
        "prices": {"^VIX": None, "^VIX3M": None},
        "calc_version": "vix_30d_3m_same_date_v1",
    }
    for symbol in ("^VIX", "^VIX3M"):
        if symbol not in raw_close.columns:
            result["reasons"].append(f"MISSING_SOURCE:{symbol}")
            continue
        native = raw_close[symbol].dropna().sort_index()
        if native.empty:
            result["reasons"].append(f"MISSING_SOURCE:{symbol}")
            continue
        result["source_dates"][symbol] = pd.Timestamp(
            native.index[-1]).strftime("%Y-%m-%d")
        try:
            value = float(native.iloc[-1])
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value <= 0:
            result["reasons"].append(f"INVALID_PRICE:{symbol}")
        else:
            result["prices"][symbol] = value

    dates = result["source_dates"]
    if all(dates.values()) and dates["^VIX"] != dates["^VIX3M"]:
        result["reasons"].append("SOURCE_DATE_MISMATCH")
    if result["reasons"]:
        if "SOURCE_DATE_MISMATCH" in result["reasons"]:
            result["status"] = "SOURCE_DATE_MISMATCH"
        elif any(reason.startswith("INVALID_PRICE") for reason in result["reasons"]):
            result["status"] = "INVALID_PRICE"
        return result

    vix, vix3m = result["prices"]["^VIX"], result["prices"]["^VIX3M"]
    state, _ = classify_vix_curve(vix, vix3m)
    result.update(
        ratio=vix / vix3m,
        state=state,
        status="OK",
        source_date=dates["^VIX"],
    )
    return result


def format_vix_index_ratio(result):
    dates = result["source_dates"]
    if result["status"] != "OK":
        reasons = ",".join(result["reasons"]) or result["status"]
        return (
            f"不可用 [{reasons}; VIX={dates['^VIX'] or 'NA'} / "
            f"VIX3M={dates['^VIX3M'] or 'NA'}]"
        )
    state_text = {
        "FLAT": "⚪平坦(Flat)",
        "CONTANGO": "🟢升水(Contango)",
        "BACKWARDATION": "🔴倒挂(Backwardation)",
    }[result["state"]]
    return f"{result['ratio']:.4f} [{state_text}] (数据日: {result['source_date']})"
