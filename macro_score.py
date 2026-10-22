"""One macro formula for live captures and labelled historical reconstruction.

Weights, thresholds and the 70% availability gate match the existing engine.
Historical calculations are not evidence of what was available in real time.
"""
from __future__ import annotations

import math

import pandas as pd


VERSION = "macro_v9_formula_recovery_v1"
WEIGHTS = {"tnx": 15, "oil": 15, "yc": 15, "liq": 15,
           "m2": 10, "stress": 10, "credit": 10, "cg": 10}
NUM_KEYS = {"tnx": "tnx_z", "oil": "oil_roc", "yc": "yc_chg",
            "liq": "liq_roc", "m2": "m2_yoy", "stress": "nfci",
            "credit": "credit_z", "cg": "cg_z"}


def clean(series):
    s = pd.to_numeric(series, errors="coerce").replace(
        [float("inf"), float("-inf")], float("nan")).dropna().sort_index()
    s = s.loc[s.index.dayofweek < 5]
    return s.loc[~s.index.duplicated(keep="last")]


def zscore(series, window=252):
    s = clean(series)
    if len(s) < 30:
        return None
    min_p = max(30, min(window, len(s)) // 2)
    z = (s - s.rolling(window, min_periods=min_p).mean()) / s.rolling(
        window, min_periods=min_p).std()
    value = float(z.iloc[-1])
    return value if math.isfinite(value) else None


def score_macro(close, fred):
    def series(frame, key):
        return clean(frame[key]) if key in frame else pd.Series(
            dtype=float, index=pd.DatetimeIndex([]))

    def roc(s, window):
        return float((s.iloc[-1] / s.iloc[-window] - 1) * 100) if (
            len(s) >= window and s.iloc[-window] != 0) else None

    values = {"tnx": zscore(series(close, "^TNX"), 63),
              "oil": roc(series(close, "CL=F"), 21)}
    yc = series(fred, "Spread_10Y2Y")
    values["yc"] = float(yc.iloc[-1] - yc.iloc[-21]) if len(yc) >= 21 else None
    nl = ((fred["Fed_Assets"] / 1000 - fred["TGA"] / 1000 - fred["RRP"])
          if {"Fed_Assets", "TGA", "RRP"}.issubset(fred.columns) else pd.Series(
              dtype=float, index=pd.DatetimeIndex([])))
    values["liq"] = roc(clean(nl), 63)
    values["m2"] = roc(series(fred, "M2_Money_Supply"), 252)
    nfci = series(fred, "NFCI")
    values["stress"] = float(nfci.iloc[-1]) if not nfci.empty else None
    values["credit"] = zscore(series(fred, "Credit_Spread"))
    cg = (close["HG=F"] * 100 / close["GC=F"] if {"HG=F", "GC=F"}.issubset(
        close.columns) else pd.Series(dtype=float, index=pd.DatetimeIndex([])))
    values["cg"] = zscore(cg)
    points = {
        "tnx": lambda v: 15 if v > 2 else (5 if v > 1 else 0),
        "oil": lambda v: 15 if v > 15 else (10 if v > 10 else 0),
        "yc": lambda v: (15 if v > .2 else (5 if v > 0 else 0)) if yc.iloc[-1] < 0 else 0,
        "liq": lambda v: 15 if v < -5 else (8 if v < -2 else 0),
        "m2": lambda v: 10 if v < 0 else (5 if v < 2 else 0),
        "stress": lambda v: 10 if v > 1 else (5 if v > .5 else 0),
        "credit": lambda v: 10 if v > 2 else (5 if v > 1 else 0),
        "cg": lambda v: 10 if v < -2 else (5 if v < -1 else 0),
    }
    components = {}
    num, labels = {}, {}
    formats = {"tnx": "Z: {:+.2f}", "oil": "ROC: {:+.2f}%", "yc": "{:+.2f}%",
               "liq": "63业务日ROC: {:+.2f}%", "m2": "YoY: {:+.2f}%",
               "stress": "{:.2f} (NFCI)", "credit": "Z: {:+.2f}", "cg": "Z: {:+.2f}"}
    for key, value in values.items():
        if value is not None and not math.isfinite(value):
            value = None
        components[key] = {"value": value, "weight": WEIGHTS[key],
                           "points": points[key](value) if value is not None else None}
        num[NUM_KEYS[key]] = round(value, 3 if key in ("yc", "stress") else 2) if value is not None else None
        labels[key] = formats[key].format(value) if value is not None else "数据不足"
    num["liq_roc_window"] = 63 if values["liq"] is not None else None
    coverage = sum(c["weight"] for c in components.values() if c["value"] is not None) / 100
    status = "OK" if coverage == 1 else ("PARTIAL" if coverage >= .70 else "UNAVAILABLE")
    num.update(macro_score_coverage=round(coverage, 3), macro_score_status=status)
    return {"score": min(100, sum(c["points"] or 0 for c in components.values())) if coverage >= .70 else None,
            "coverage": round(coverage, 3), "status": status, "components": components,
            "num": num, "labels": labels, "version": VERSION}
