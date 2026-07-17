# -*- coding: utf-8 -*-
"""Pure calculations for the pre-market options monitor."""
import math
from datetime import datetime, time

import numpy as np
import pandas as pd


GAMMA_SIGN_MODEL = "OI_CALL_PLUS_PUT_MINUS_PROXY"
GAMMA_CURVE_VERSION = "gamma_curve_v2"
GAMMA_BUCKETS = ("0DTE", "1-7D", "8-30D", "31-60D", "ALL")


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
    return (spot - level) / spot * 100.0


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


def expected_move_metrics(frame, spot, short_expiration):
    result = {
        "value": None,
        "pct": None,
        "source": None,
        "dte": None,
        "quality": "MISSING",
        "atm_strike": None,
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
        value = spot * float(ivs.median()) * math.sqrt(years_value)
        result.update({
            "value": value,
            "pct": value / spot * 100.0,
            "source": "ATM_IV_FALLBACK",
            "quality": "FALLBACK",
        })
    return result


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
