# -*- coding: utf-8 -*-
"""Pure calculations for the pre-market options monitor."""
import math
from copy import deepcopy
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd


GAMMA_SIGN_MODEL = "OI_CALL_PLUS_PUT_MINUS_PROXY"
GAMMA_CURVE_VERSION = "gamma_curve_v2"
GAMMA_BUCKETS = ("0DTE", "1-7D", "8-30D", "31-60D", "ALL")
DISTANCE_SIGN_VERSION = "LEVEL_MINUS_SPOT_V2"
MONTHLY_WALL_METHOD = "MAX_OI_BY_RIGHT_STANDARD_MONTHLY_V1"


def finite_number(value, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if positive and number <= 0:
        return None
    return number


def quote_midpoint(bid, ask):
    bid = finite_number(bid, positive=True)
    ask = finite_number(ask, positive=True)
    if bid is None or ask is None or ask < bid:
        return None
    return (bid + ask) / 2.0


def distance_pct(level, spot):
    level = finite_number(level, positive=True)
    spot = finite_number(spot, positive=True)
    if level is None or spot is None:
        return None
    return (level - spot) / spot * 100.0


def _third_friday(year, month):
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _month_offset(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return month_index // 12, month_index % 12 + 1


def standard_monthly_oi_walls(frame, as_of_date, spot):
    """Return max-OI put/call walls for the next standard monthly expiry.

    The listed third Friday is preferred. If it is absent (for example Good
    Friday), the nearest listed expiry in the preceding three calendar days is
    treated as that month's standard settlement. OI is aggregated by strike
    within the single selected expiry; ties prefer the strike nearest spot.
    """
    result = {
        "expiry": None,
        "call_wall": None,
        "put_wall": None,
        "call_oi": None,
        "put_oi": None,
        "quality": "MISSING_CHAIN",
        "method": MONTHLY_WALL_METHOD,
    }
    if frame is None or frame.empty:
        return result
    try:
        as_of = pd.Timestamp(as_of_date).date()
    except (TypeError, ValueError):
        result["quality"] = "INVALID_AS_OF_DATE"
        return result
    spot = finite_number(spot, positive=True)
    if spot is None:
        result["quality"] = "INVALID_SPOT"
        return result

    working = frame.copy()
    required = {"Exp", "S", "R", "OI"}
    if not required.issubset(working.columns):
        result["quality"] = "MISSING_COLUMNS"
        return result
    working["_expiry"] = pd.to_datetime(
        working["Exp"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.date
    working["_strike"] = pd.to_numeric(working["S"], errors="coerce")
    working["_oi"] = pd.to_numeric(working["OI"], errors="coerce")
    working["_right"] = working["R"].astype(str).str.upper()
    working = working.loc[
        working["_expiry"].notna()
        & (working["_expiry"] >= as_of)
        & working["_strike"].gt(0)
        & working["_oi"].gt(0)
        & working["_right"].isin(["C", "P"])
    ].copy()
    if working.empty:
        result["quality"] = "NO_VALID_OI"
        return result

    available = set(working["_expiry"])
    target_expiry = None
    for offset in range(4):
        year, month = _month_offset(as_of, offset)
        nominal = _third_friday(year, month)
        candidates = sorted(
            expiry
            for expiry in available
            if nominal - timedelta(days=3) <= expiry <= nominal
        )
        if candidates:
            target_expiry = candidates[-1]
            break
    if target_expiry is None:
        result["quality"] = "NO_STANDARD_MONTHLY_EXPIRY"
        return result

    monthly = working.loc[working["_expiry"] == target_expiry].copy()
    result["expiry"] = target_expiry.isoformat()
    missing = []
    for right, prefix in (("C", "call"), ("P", "put")):
        side = monthly.loc[monthly["_right"] == right]
        if side.empty:
            missing.append(prefix)
            continue
        strikes = side.groupby("_strike", as_index=False)["_oi"].sum()
        strikes["_distance"] = (strikes["_strike"] - spot).abs()
        best = strikes.sort_values(
            ["_oi", "_distance", "_strike"],
            ascending=[False, True, True],
        ).iloc[0]
        result[f"{prefix}_wall"] = float(best["_strike"])
        result[f"{prefix}_oi"] = int(best["_oi"])
    result["quality"] = "OK" if not missing else "MISSING_" + "_AND_".join(
        value.upper() for value in missing
    )
    return result


def _compact_number(value, digits=2, prefix="", signed=False):
    number = finite_number(value)
    if number is None:
        return "NA"
    sign = "+" if signed else ""
    return f"{prefix}{number:{sign}.{digits}f}"


def format_premarket_symbol_summary(
        symbol, reference_price, price_source, market_label, previous_close,
        expected_move, oi_pcr, iv_skew, gamma, gamma_flip_quality,
        iv_skew_quality=None):
    """Render a concise email view without reducing persisted diagnostics."""
    source_labels = {
        "PREMARKET_MID": "盘前中间价",
        "PREMARKET_LAST": "盘前成交",
        "PREVIOUS_CLOSE_FALLBACK": "昨收回退",
        "MISSING": "缺失",
    }
    source_text = source_labels.get(price_source, price_source or "未知")
    em_pct = expected_move.get("pct") if expected_move else None
    em_quality = expected_move.get("quality") if expected_move else "MISSING"

    skew = finite_number(iv_skew)
    skew_text = _compact_number(
        skew * 100 if skew is not None else None, signed=True)
    headline = (
        f"{symbol} | 参考 {_compact_number(reference_price, prefix='$')} "
        f"({source_text}/{market_label}) | "
        f"昨收 {_compact_number(previous_close, prefix='$')} | "
        f"预期 ±{_compact_number(em_pct)}% | "
        f"PCR {_compact_number(oi_pcr)} | "
        f"Skew {skew_text}%"
    )

    flip = gamma.get("ALL", {}).get("primary_flip")
    if gamma.get("pin_strike") is not None:
        structure_parts = [
            "Pin " + _compact_number(gamma.get("pin_strike"), prefix="$"),
        ]
    else:
        structure_parts = [
            "C " + _compact_number(gamma.get("call_wall"), prefix="$"),
            "P " + _compact_number(gamma.get("put_wall"), prefix="$"),
        ]
    structure_parts.append("Flip " + _compact_number(flip, prefix="$"))
    distance_values = [
        distance_pct(gamma.get("call_wall"), reference_price),
        distance_pct(gamma.get("put_wall"), reference_price),
        distance_pct(flip, reference_price),
    ]
    distance_text = "/".join(
        _compact_number(value, signed=True) + "%" for value in distance_values)
    structure_line = (
        "  关键位 " + " / ".join(structure_parts)
        + f" | C/P/Flip距离 {distance_text}"
    )

    bucket_labels = (
        ("0DTE", "0D"), ("1-7D", "1-7D"),
        ("8-30D", "8-30D"), ("ALL", "全期"),
    )
    gamma_parts = []
    bad_gamma = []
    for bucket, label in bucket_labels:
        metrics = gamma.get(bucket, {})
        quality = metrics.get("quality", "MISSING")
        if quality == "OK":
            value = _compact_number(
                metrics.get("net_gamma_m"), signed=True) + "M"
        else:
            value = f"NA[{quality}]"
            bad_gamma.append(f"{label}:{quality}")
        gamma_parts.append(f"{label} {value}")
    gamma_line = "  Gamma " + " | ".join(gamma_parts)
    all_gamma = gamma.get("ALL", {})
    coverage_line = None
    if all_gamma.get("net_gamma_m") is not None:
        coverage_line = (
            "  覆盖 "
            f"有效{all_gamma.get('contract_count', 0)}/"
            f"请求{all_gamma.get('requested_contract_count', 0)} | "
            f"合约{_compact_number(all_gamma.get('coverage_pct'), digits=1)}% | "
            f"OI{_compact_number(all_gamma.get('oi_coverage_pct'), digits=1)}% | "
            f"C/P OI {_compact_number(all_gamma.get('call_oi_coverage_pct'), digits=1)}%/"
            f"{_compact_number(all_gamma.get('put_oi_coverage_pct'), digits=1)}% | "
            f"IV最旧{_compact_number(all_gamma.get('iv_quote_max_age_seconds'), digits=0)}秒"
        )

    notices = []
    if price_source == "PREVIOUS_CLOSE_FALLBACK":
        notices.append("未取得盘前价")
    elif market_label != "实时":
        notices.append(f"行情={market_label}")
    if em_quality != "OK":
        notices.append(f"预期振幅={em_quality}")
    if iv_skew_quality not in (None, "OK"):
        notices.append(f"Skew={iv_skew_quality}")
    if bad_gamma:
        notices.append("Gamma=" + ",".join(bad_gamma))
    if gamma_flip_quality not in (None, "OK", "BASELINE_RESET", "NO_CROSSING"):
        notices.append(f"Flip={gamma_flip_quality}")

    lines = [headline, structure_line, gamma_line]
    if coverage_line:
        lines.append(coverage_line)
    if notices:
        lines.append("  注意 " + "；".join(notices))
    return "\n".join(lines)


def format_premarket_quality_summary(rows_written, total_symbols, issues):
    """Collapse detailed quality keys into a short operational email footer."""
    unique_issues = sorted(set(issues or []))
    if not unique_issues:
        return f"入库 {rows_written}/{total_symbols} | 无数据质量告警"

    categories = {}
    for issue in unique_issues:
        key = str(issue).split(":", 1)[0]
        if key.startswith("gamma_"):
            label = "Gamma"
        elif key.startswith("db_"):
            label = "数据库"
        elif key in ("price", "premarket_quote", "premarket_not_live"):
            label = "行情"
        elif key == "expected_move":
            label = "预期振幅"
        elif key == "futures_ref":
            label = "期货映射"
        elif key == "monthly_wall":
            label = "月度OI墙"
        elif key == "dpsv":
            label = "FINRA"
        elif key in ("expirations", "strikes", "option_qualification", "option_rows"):
            label = "期权链"
        else:
            label = "其他"
        categories[label] = categories.get(label, 0) + 1
    category_text = "，".join(
        f"{label}{count}" for label, count in sorted(categories.items()))
    return (
        f"入库 {rows_written}/{total_symbols} | "
        f"质量项 {len(unique_issues)}（{category_text}）"
    )


def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def norm_pdf(x):
    return math.exp(-x ** 2 / 2.0) / math.sqrt(2.0 * math.pi)


def bs_price(spot, strike, years, rate, sigma, right):
    if min(spot, strike, years, sigma) <= 0:
        return None
    d1 = (
        math.log(spot / strike) + (rate + sigma ** 2 / 2.0) * years
    ) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    if right == "C":
        return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
    return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_vega(spot, strike, years, rate, sigma):
    if min(spot, strike, years, sigma) <= 0:
        return 0.0
    d1 = (
        math.log(spot / strike) + (rate + sigma ** 2 / 2.0) * years
    ) / (sigma * math.sqrt(years))
    return spot * norm_pdf(d1) * math.sqrt(years)


def implied_volatility(target_price, spot, strike, years, rate, right):
    target = finite_number(target_price, positive=True)
    if target is None or min(spot, strike, years) <= 0:
        return None
    intrinsic = (
        max(0.0, spot - strike * math.exp(-rate * years))
        if right == "C"
        else max(0.0, strike * math.exp(-rate * years) - spot)
    )
    if target + 1e-6 < intrinsic:
        return None

    sigma = 0.5
    for _ in range(60):
        price = bs_price(spot, strike, years, rate, sigma, right)
        if price is None:
            return None
        diff = price - target
        if abs(diff) < 1e-5:
            return sigma
        vega = bs_vega(spot, strike, years, rate, sigma)
        if vega < 1e-7:
            break
        sigma = min(4.0, max(0.005, sigma - diff / vega))
    return sigma if 0.005 <= sigma <= 4.0 else None


def calc_delta_gamma(spot, strike, years, rate, sigma, right):
    if min(spot, strike, years, sigma) <= 0:
        return None, None
    d1 = (
        math.log(spot / strike) + (rate + sigma ** 2 / 2.0) * years
    ) / (sigma * math.sqrt(years))
    gamma = norm_pdf(d1) / (spot * sigma * math.sqrt(years))
    delta = norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0
    return delta, gamma


def calc_vanna_charm(spot, strike, years, rate, sigma):
    if min(spot, strike, years, sigma) <= 0:
        return None, None
    d1 = (
        math.log(spot / strike) + (rate + sigma ** 2 / 2.0) * years
    ) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    pdf_d1 = norm_pdf(d1)
    vanna = -pdf_d1 * (d2 / sigma)
    charm = -pdf_d1 * (
        (rate / (sigma * math.sqrt(years))) - (d2 / (2.0 * years))
    )
    return vanna, charm


def time_to_expiry_years(expiration, as_of):
    """Calendar-year fraction to 16:00 New York on expiration day."""
    try:
        exp_date = datetime.strptime(str(expiration), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None
    expiry_dt = datetime.combine(exp_date, time(16, 0))
    if as_of.tzinfo is not None:
        expiry_dt = as_of.tzinfo.localize(expiry_dt) if hasattr(
            as_of.tzinfo, "localize"
        ) else expiry_dt.replace(tzinfo=as_of.tzinfo)
    seconds = (expiry_dt - as_of).total_seconds()
    if seconds <= 0:
        return None
    return max(seconds / (365.0 * 86400.0), 1.0 / (365.0 * 24.0 * 60.0))


def expiration_bucket(dte_days):
    dte = finite_number(dte_days)
    if dte is None or dte < 0:
        return None
    if dte < 1:
        return "0DTE"
    if dte <= 7:
        return "1-7D"
    if dte <= 30:
        return "8-30D"
    if dte <= 60:
        return "31-60D"
    return None


def select_expirations(expirations, as_of_date, horizon_days=60):
    """Select a pacing-safe, representative set for each expiry bucket."""
    grouped = {key: [] for key in GAMMA_BUCKETS if key != "ALL"}
    for expiration in sorted(set(expirations)):
        try:
            exp_date = datetime.strptime(str(expiration), "%Y%m%d").date()
        except (TypeError, ValueError):
            continue
        dte = (exp_date - as_of_date).days
        if dte < 0 or dte > horizon_days:
            continue
        bucket = expiration_bucket(dte)
        if bucket:
            grouped[bucket].append(str(expiration))

    limits = {"0DTE": 1, "1-7D": 1, "8-30D": 2, "31-60D": 1}
    selected = []
    selected_by_bucket = {}
    for bucket, values in grouped.items():
        limit = limits[bucket]
        if len(values) > limit:
            indexes = sorted(set(np.linspace(0, len(values) - 1, limit).astype(int)))
            values = [values[index] for index in indexes]
        selected_by_bucket[bucket] = values
        selected.extend(values)
    return sorted(set(selected)), selected_by_bucket


def expected_move_metrics(frame, spot, short_expiration, previous_iv=None,
                          previous_iv_date=None,
                          previous_iv_source="PREVIOUS_DAY_IV"):
    result = {
        "value": None,
        "pct": None,
        "source": None,
        "dte": None,
        "quality": "MISSING",
        "atm_strike": None,
        "iv": None,
        "source_date": None,
    }
    spot = finite_number(spot, positive=True)
    if spot is None or frame.empty:
        return result
    expiry = frame[frame["Exp"] == short_expiration].copy()
    if expiry.empty:
        return result
    strike = min(expiry["S"].dropna().unique(), key=lambda value: abs(value - spot))
    atm = expiry[expiry["S"] == strike]
    call = atm[atm["R"] == "C"]
    put = atm[atm["R"] == "P"]
    result["atm_strike"] = float(strike)
    years = pd.to_numeric(atm.get("T"), errors="coerce").dropna()
    if not years.empty:
        result["dte"] = float(years.median() * 365.0)
    if "QuoteAsOf" in atm:
        quote_times = pd.to_datetime(atm["QuoteAsOf"], errors="coerce", utc=True).dropna()
        if not quote_times.empty:
            result["source_date"] = quote_times.max().date().isoformat()

    call_mid = finite_number(call.iloc[0].get("Mid"), positive=True) if not call.empty else None
    put_mid = finite_number(put.iloc[0].get("Mid"), positive=True) if not put.empty else None
    if call_mid is not None and put_mid is not None:
        value = call_mid + put_mid
        result.update({
            "value": value,
            "pct": value / spot * 100.0,
            "source": "ATM_STRADDLE_MID",
            "quality": "OK",
        })
        return result

    ivs = pd.to_numeric(atm.get("IV"), errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    years_value = finite_number(years.median(), positive=True) if not years.empty else None
    if not ivs.empty and years_value is not None:
        iv_value = float(ivs.median())
        value = spot * iv_value * math.sqrt(years_value)
        result.update({
            "value": value,
            "pct": value / spot * 100.0,
            "source": "ATM_IV_FALLBACK",
            "quality": "FALLBACK",
            "iv": iv_value,
        })
        return result

    expiry["_iv"] = pd.to_numeric(expiry.get("IV"), errors="coerce")
    neighbors = (
        expiry.loc[expiry["_iv"].gt(0), ["S", "_iv"]]
        .groupby("S", as_index=False)["_iv"].median()
        .sort_values("S")
    )
    below = neighbors[neighbors["S"] < strike]
    above = neighbors[neighbors["S"] > strike]
    if not below.empty and not above.empty and years_value is not None:
        lower, upper = below.iloc[-1], above.iloc[0]
        span = float(upper["S"] - lower["S"])
        if span > 0:
            weight = (float(strike) - float(lower["S"])) / span
            iv_value = float(lower["_iv"] + weight * (upper["_iv"] - lower["_iv"]))
            value = spot * iv_value * math.sqrt(years_value)
            result.update({
                "value": value,
                "pct": value / spot * 100.0,
                "source": "ADJACENT_IV_INTERPOLATION",
                "quality": "FALLBACK",
                "iv": iv_value,
                "source_date": result["source_date"],
            })
            return result

    prior_iv = finite_number(previous_iv, positive=True)
    if prior_iv is not None and years_value is not None:
        value = spot * prior_iv * math.sqrt(years_value)
        result.update({
            "value": value,
            "pct": value / spot * 100.0,
            "source": previous_iv_source,
            "quality": "STALE",
            "iv": prior_iv,
            "source_date": previous_iv_date,
        })
    return result


def validate_iv_skew(iv_skew, call_row=None, put_row=None,
                     extreme_abs=0.15, max_spread_pct=0.50,
                     max_quote_age_seconds=300):
    """Return an explicit quote-sanity state for the 25-delta IV skew."""
    skew = finite_number(iv_skew)
    if skew is None:
        return "MISSING"
    rows = [row for row in (call_row, put_row) if row is not None]
    for row in rows:
        age = finite_number(row.get("QuoteAgeSeconds"))
        if age is not None and age > max_quote_age_seconds:
            return "STALE_QUOTE"
        bid = finite_number(row.get("Bid"), positive=True)
        ask = finite_number(row.get("Ask"), positive=True)
        if bid is not None and ask is not None and ask >= bid:
            midpoint = (bid + ask) / 2.0
            if midpoint > 0 and (ask - bid) / midpoint > max_spread_pct:
                return "WIDE_MARKET"
    return "EXTREME_REVIEW" if abs(skew) >= extreme_abs else "OK"


def dollar_gamma_m(row, evaluation_spot):
    spot = finite_number(evaluation_spot, positive=True)
    strike = finite_number(row.get("S"), positive=True)
    years = finite_number(row.get("T"), positive=True)
    sigma = finite_number(row.get("IV"), positive=True)
    oi = finite_number(row.get("OI"), positive=True)
    right = row.get("R")
    if None in (spot, strike, years, sigma, oi) or right not in ("C", "P"):
        return None
    _, gamma = calc_delta_gamma(spot, strike, years, 0.053, sigma, right)
    if gamma is None:
        return None
    sign = 1.0 if right == "C" else -1.0
    return sign * gamma * oi * 100.0 * spot ** 2 * 0.01 / 1e6


def _zero_crossings(spots, values):
    scale = max(abs(float(value)) for value in values)
    if scale < 1e-12:
        return []
    tolerance = max(1e-12, scale * 1e-9)
    crossings = []
    for index in range(len(spots) - 1):
        x0, x1 = float(spots[index]), float(spots[index + 1])
        y0, y1 = float(values[index]), float(values[index + 1])
        if abs(y0) <= tolerance:
            crossings.append(x0)
        elif y0 * y1 < 0:
            crossings.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    if abs(values[-1]) <= tolerance:
        crossings.append(float(spots[-1]))
    return [round(value, 4) for value in dict.fromkeys(round(x, 4) for x in crossings)]


def _empty_gamma_bucket():
    return {
        "net_gamma_m": None,
        "primary_flip": None,
        "zero_points": [],
        "contract_count": 0,
        "expiration_count": 0,
        "expirations": [],
        "call_count": 0,
        "put_count": 0,
        "strike_count": 0,
    }


def gamma_structure(frame, spot, grid_width=0.20, grid_points=161):
    """Reprice gamma across a spot grid and return expiry-bucket zero crossings."""
    spot = finite_number(spot, positive=True)
    result = {bucket: _empty_gamma_bucket() for bucket in GAMMA_BUCKETS}
    result.update({"call_wall": None, "put_wall": None, "pin_strike": None,
                   "pin_state": None, "sign_model": GAMMA_SIGN_MODEL,
                   "curve_version": GAMMA_CURVE_VERSION,
                   "grid_width_pct": grid_width * 100.0,
                   "grid_points": int(grid_points)})
    if spot is None or frame.empty:
        return result

    working = frame.copy()
    working["Bucket"] = working["DTE"].apply(expiration_bucket)
    working = working[working["Bucket"].notna()]
    working = working[
        pd.to_numeric(working["OI"], errors="coerce").gt(0)
        & pd.to_numeric(working["IV"], errors="coerce").gt(0)
        & pd.to_numeric(working["T"], errors="coerce").gt(0)
    ]
    if working.empty:
        return result

    grid = np.linspace(spot * (1.0 - grid_width), spot * (1.0 + grid_width), grid_points)
    bucket_frames = {
        "0DTE": working[working["Bucket"] == "0DTE"],
        "1-7D": working[working["Bucket"] == "1-7D"],
        "8-30D": working[working["Bucket"] == "8-30D"],
        "31-60D": working[working["Bucket"] == "31-60D"],
        "ALL": working,
    }
    for bucket, subset in bucket_frames.items():
        if subset.empty:
            continue
        current_values = [
            value for value in (dollar_gamma_m(row, spot) for _, row in subset.iterrows())
            if value is not None
        ]
        curve = []
        for grid_spot in grid:
            exposures = [
                value for value in (
                    dollar_gamma_m(row, grid_spot) for _, row in subset.iterrows()
                ) if value is not None
            ]
            curve.append(sum(exposures))
        zeroes = _zero_crossings(grid, curve)
        result[bucket] = {
            "net_gamma_m": sum(current_values) if current_values else None,
            "primary_flip": min(zeroes, key=lambda value: abs(value - spot)) if zeroes else None,
            "zero_points": zeroes,
            "contract_count": int(len(subset)),
            "expiration_count": int(subset["Exp"].nunique()),
            "expirations": sorted(str(value) for value in subset["Exp"].unique()),
            "call_count": int((subset["R"] == "C").sum()),
            "put_count": int((subset["R"] == "P").sum()),
            "strike_count": int(subset["S"].nunique()),
        }

    near = working[working["Bucket"].isin(("0DTE", "1-7D"))].copy()
    if not near.empty:
        near["GammaDollarM"] = [
            dollar_gamma_m(row, spot) for _, row in near.iterrows()
        ]
        calls = near[near["R"] == "C"].dropna(subset=["GammaDollarM"])
        puts = near[near["R"] == "P"].dropna(subset=["GammaDollarM"])
        if not calls.empty:
            result["call_wall"] = float(
                calls.assign(Weight=calls["GammaDollarM"].abs())
                .groupby("S")["Weight"].sum().idxmax()
            )
        if not puts.empty:
            result["put_wall"] = float(
                puts.assign(Weight=puts["GammaDollarM"].abs())
                .groupby("S")["Weight"].sum().idxmax()
            )
        if result["call_wall"] == result["put_wall"] and result["call_wall"] is not None:
            result["pin_strike"] = result["call_wall"]
            all_gamma = result["ALL"]["net_gamma_m"]
            result["pin_state"] = (
                "PIN_CANDIDATE" if all_gamma is not None and all_gamma >= 0
                else "BREAKOUT_PIVOT"
            )
    return result


def apply_gamma_quality_gate(structure, requested_counts, qualified_counts,
                             oi_valid_counts, side_counts=None,
                             iv_quote_ages=None):
    """Suppress precise Gamma outputs when sampled-chain coverage is unusable."""
    result = deepcopy(structure)
    minimum_contracts = {
        "0DTE": 12, "1-7D": 12, "8-30D": 16,
        "31-60D": 12, "ALL": 30,
    }
    for bucket in GAMMA_BUCKETS:
        metrics = result[bucket]
        requested = int(requested_counts.get(bucket, 0) or 0)
        qualified = int(qualified_counts.get(bucket, 0) or 0)
        oi_valid = int(oi_valid_counts.get(bucket, 0) or 0)
        valid = int(metrics.get("contract_count", 0) or 0)
        coverage = valid / requested if requested > 0 else 0.0
        details = (side_counts or {}).get(bucket, {})
        requested_calls = int(details.get("requested_calls", 0) or 0)
        requested_puts = int(details.get("requested_puts", 0) or 0)
        oi_calls = int(details.get("oi_calls", metrics.get("call_count", 0)) or 0)
        oi_puts = int(details.get("oi_puts", metrics.get("put_count", 0)) or 0)
        oi_coverage = oi_valid / requested if requested > 0 else 0.0
        call_oi_coverage = oi_calls / requested_calls if requested_calls > 0 else 0.0
        put_oi_coverage = oi_puts / requested_puts if requested_puts > 0 else 0.0
        ages = [finite_number(value) for value in (iv_quote_ages or {}).get(bucket, [])]
        ages = [value for value in ages if value is not None and value >= 0]

        if requested == 0:
            quality = "NO_EXPIRY"
        elif qualified == 0:
            quality = "QUALIFICATION_FAILED"
        elif oi_valid == 0:
            quality = "OI_MISSING"
        elif valid == 0:
            quality = "IV_MISSING"
        elif (
            valid < minimum_contracts[bucket]
            or coverage < 0.20
            or metrics.get("call_count", 0) < 4
            or metrics.get("put_count", 0) < 4
            or metrics.get("strike_count", 0) < 4
        ):
            quality = "LOW_COVERAGE"
        else:
            quality = "OK"

        metrics.update({
            "quality": quality,
            "requested_contract_count": requested,
            "qualified_contract_count": qualified,
            "oi_valid_contract_count": oi_valid,
            "coverage_pct": coverage * 100.0,
            "oi_coverage_pct": oi_coverage * 100.0,
            "call_oi_coverage_pct": call_oi_coverage * 100.0,
            "put_oi_coverage_pct": put_oi_coverage * 100.0,
            "iv_quote_max_age_seconds": max(ages) if ages else None,
            "raw_net_gamma_m": metrics.get("net_gamma_m"),
            "raw_primary_flip": metrics.get("primary_flip"),
            "raw_zero_points": metrics.get("zero_points", []),
        })
        if quality != "OK":
            metrics["net_gamma_m"] = None
            metrics["primary_flip"] = None
            metrics["zero_points"] = []
            metrics["flip_quality"] = quality
        elif metrics.get("primary_flip") is None:
            metrics["flip_quality"] = "VALID_NO_CROSS"
        else:
            metrics["flip_quality"] = "OK"

    active_near = [
        bucket for bucket in ("0DTE", "1-7D")
        if result[bucket]["requested_contract_count"] > 0
    ]
    if not active_near or any(result[bucket]["quality"] != "OK"
                              for bucket in active_near):
        result["call_wall"] = None
        result["put_wall"] = None
        result["pin_strike"] = None
        result["pin_state"] = None
    result["quality"] = {
        bucket: result[bucket]["quality"] for bucket in GAMMA_BUCKETS
    }
    return result


def max_oi_metrics(frame, spot):
    result = {
        "strike": None, "right": None, "expiration": None, "oi": None,
        "delta": None, "gamma_dollar_m": None, "distance_pct": None,
    }
    if frame.empty:
        return result
    oi = pd.to_numeric(frame.get("OI"), errors="coerce")
    if oi.dropna().empty or oi.max() <= 0:
        return result
    row = frame.loc[oi.idxmax()]
    gamma_m = dollar_gamma_m(row, spot)
    result.update({
        "strike": finite_number(row.get("S"), positive=True),
        "right": row.get("R"),
        "expiration": str(row.get("Exp")) if row.get("Exp") is not None else None,
        "oi": int(row.get("OI")),
        "delta": finite_number(row.get("Delta")),
        "gamma_dollar_m": gamma_m,
        "distance_pct": distance_pct(row.get("S"), spot),
    })
    return result


def put_call_oi_ratio(frame):
    if frame.empty:
        return None
    calls = pd.to_numeric(frame.loc[frame["R"] == "C", "OI"], errors="coerce").sum()
    puts = pd.to_numeric(frame.loc[frame["R"] == "P", "OI"], errors="coerce").sum()
    return float(puts / calls) if calls > 0 else None
