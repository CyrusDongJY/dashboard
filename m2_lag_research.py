"""Four-region M2 lag study. Revised-data research, never production signals."""
from __future__ import annotations

import argparse
import hashlib
from io import StringIO
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from core_funding_sources import write_json_atomic

VERSION = "g4_m2_lag_research_v1"
SERIES = {
    "us": ("M2NS", "USD billions", "monthly"),
    "cn": ("MYAGM2CNM189N", "CNY units", "monthly"),
    "jp": ("MYAGM2JPM189N", "JPY units", "monthly"),
    "eu": ("MYAGM2EZM196N", "EUR units", "monthly"),
    "cny_per_usd": ("DEXCHUS", "CNY per USD", "daily"),
    "jpy_per_usd": ("DEXJPUS", "JPY per USD", "daily"),
    "usd_per_eur": ("DEXUSEU", "USD per EUR", "daily"),
    "NASDAQ100": ("NASDAQ100", "price index; month-end close; not QQQ total return", "daily"),
    "US_SHARE_PRICE": ("SPASTT01USM661N", "OECD monthly share-price index; not SPY total return", "monthly"),
}
LAGS = tuple(range(7))
HOLDOUT_START = "2011-01"


def fetch_inputs(directory, refresh=False):
    directory = Path(directory)
    series, provenance = {}, {}
    for key, (sid, unit, frequency) in SERIES.items():
        path = directory / f"{sid}.json"
        if path.exists() and not refresh:
            item = json.loads(path.read_text())
        else:
            response = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                                    params={"id": sid}, timeout=30)
            response.raise_for_status()
            data = pd.read_csv(StringIO(response.text), na_values=["."])
            if list(data.columns) != ["observation_date", sid]:
                raise ValueError(f"Unexpected FRED columns: {sid}")
            item = {"id": sid, "unit": unit, "frequency": frequency,
                    "vintage": "LATEST_REVISED_NOT_PIT", "url": response.url,
                    "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    "download_sha256": hashlib.sha256(response.content).hexdigest(),
                    "rows": json.loads(data.to_json(orient="records"))}
            item["rows_sha256"] = hashlib.sha256(json.dumps(item["rows"], sort_keys=True).encode()).hexdigest()
            write_json_atomic(path, item)
        if (item["id"] != sid or item["unit"] != unit or item["frequency"] != frequency
                or hashlib.sha256(json.dumps(item["rows"], sort_keys=True).encode()).hexdigest() != item["rows_sha256"]):
            raise ValueError(f"Source archive contract mismatch: {sid}")
        data = pd.DataFrame(item["rows"])
        values = pd.Series(pd.to_numeric(data[sid], errors="raise").to_numpy(),
                           index=pd.to_datetime(data["observation_date"]), name=key).sort_index()
        if not values.index.is_unique or np.isinf(values.dropna()).any() or (values.dropna() <= 0).any():
            raise ValueError(f"Invalid levels: {sid}")
        series[key] = values
        valid = values.dropna()
        provenance[key] = {k: item[k] for k in ("id", "unit", "frequency", "vintage", "url", "retrieved_at", "rows_sha256")}
        provenance[key].update(first=str(valid.index[0].date()), last=str(valid.index[-1].date()), observations=len(valid))
        print(f"SOURCE {sid}: {provenance[key]['first']}..{provenance[key]['last']} ({len(valid)})", flush=True)
    return series, provenance


def to_monthly(series, kind):
    groups = series.groupby(series.index.to_period("M"))
    if kind == "monthly":
        if groups.size().gt(1).any():
            raise ValueError("More than one native monthly observation")
        return groups.first()
    if kind == "fx":
        return groups.mean().where(groups.count() >= 15)
    if kind == "close":
        import pandas_market_calendars as mcal
        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=series.index.min().to_period("M").start_time,
            end_date=series.index.max().to_period("M").end_time)
        expected = pd.Series(schedule.index, index=schedule.index.to_period("M")).groupby(level=0).last()
        observed = series.dropna()
        last_dates = pd.Series(observed.index, index=observed.index.to_period("M")).groupby(level=0).last()
        complete = last_dates.reindex(expected.index).eq(expected)
        return groups.last().where((groups.count() >= 15) & complete)
    raise ValueError("Unknown aggregation")


def assemble_panel(series):
    monthly = {key: to_monthly(value, "monthly" if SERIES[key][2] == "monthly"
                               else "close" if key == "NASDAQ100" else "fx")
               for key, value in series.items()}
    frame = pd.DataFrame(monthly)
    frame = frame.reindex(pd.period_range(frame.index.min(), frame.index.max(), freq="M"))
    required = ["us", "cn", "jp", "eu", "cny_per_usd", "jpy_per_usd", "usd_per_eur"]
    valid = frame[required].dropna()
    if valid.empty:
        raise ValueError("No complete four-region overlap")
    base = valid.iloc[0]
    components = pd.DataFrame({"us": frame.us, "cn": frame.cn / frame.cny_per_usd / 1e9,
                               "jp": frame.jp / frame.jpy_per_usd / 1e9,
                               "eu": frame.eu * frame.usd_per_eur / 1e9})
    fixed = pd.DataFrame({"us": frame.us, "cn": frame.cn / base.cny_per_usd / 1e9,
                         "jp": frame.jp / base.jpy_per_usd / 1e9,
                         "eu": frame.eu * base.usd_per_eur / 1e9})
    complete = frame[required].notna().all(axis=1)
    frame["G4_USD"] = components.sum(axis=1, min_count=4).where(complete)
    frame["G4_FIXED_FX"] = fixed.sum(axis=1, min_count=4).where(complete)
    return frame, {"month": str(valid.index[0]), "cny_per_usd": float(base.cny_per_usd),
                   "jpy_per_usd": float(base.jpy_per_usd), "usd_per_eur": float(base.usd_per_eur)}


def growth(series, months):
    return (series / series.shift(months) - 1).mul(100).where(
        series.rolling(months + 1).count() == months + 1)


def align_sample(signal, returns, lag):
    if lag not in LAGS or not isinstance(signal.index, pd.PeriodIndex):
        raise ValueError("A prespecified calendar-month lag is required")
    calendar = pd.period_range(min(signal.index.min(), returns.index.min()),
                               max(signal.index.max(), returns.index.max()), freq="M")
    shifted = signal.reindex(calendar).copy()
    shifted.index = shifted.index + lag
    return pd.concat([shifted.rename("x"), returns.reindex(calendar).rename("y")], axis=1).dropna()


def controls(index):
    # NSA monetary aggregates have seasonal patterns; compare the same controls in both models.
    months = np.array([period.month for period in index])
    return np.column_stack([np.ones(len(index))] + [(months == month).astype(float) for month in range(2, 13)])


def statistics(sample):
    import statsmodels.api as sm
    if len(sample) < 60 or sample.x.std() <= 1e-10 or sample.y.std() <= 1e-10:
        return {"n": len(sample), "status": "INSUFFICIENT"}
    scaled = (sample.x - sample.x.mean()) / sample.x.std()
    design = np.column_stack([controls(sample.index), scaled.to_numpy()])
    fit = sm.OLS(sample.y.to_numpy(), design).fit(
        cov_type="HAC", cov_kwds={"maxlags": 6, "use_correction": True}, use_t=True)
    return {"n": len(sample), "status": "OK", "from": str(sample.index[0]), "to": str(sample.index[-1]),
            "pearson": float(sample.x.corr(sample.y)), "spearman": float(sample.x.corr(sample.y, method="spearman")),
            "beta_pct_per_signal_sd": float(fit.params[-1]), "hac_p": float(fit.pvalues[-1]),
            "hac_ci95": fit.conf_int()[-1].tolist()}


def walk_forward(sample, split=HOLDOUT_START):
    observed, base_errors, model_errors = [], [], []
    first = pd.Period(split, freq="M")
    for i, date in enumerate(sample.index):
        if date < first or i < 60:
            continue
        train = sample.iloc[:i]
        if train.x.std() <= 1e-10:
            continue
        base_train, base_now = controls(train.index), controls(pd.PeriodIndex([date], freq="M"))
        center, scale = train.x.mean(), train.x.std()
        x_train = np.column_stack([base_train, (train.x - center) / scale])
        x_now = np.column_stack([base_now, [(sample.x.iloc[i] - center) / scale]])
        predicted_base = float((base_now @ np.linalg.lstsq(base_train, train.y, rcond=None)[0])[0])
        predicted_model = float((x_now @ np.linalg.lstsq(x_train, train.y, rcond=None)[0])[0])
        actual = float(sample.y.iloc[i])
        observed.append(str(date))
        base_errors.append((actual - predicted_base) ** 2)
        model_errors.append((actual - predicted_model) ** 2)
    return {"n": len(observed), "from": observed[0] if observed else None,
            "to": observed[-1] if observed else None,
            "incremental_oos_r2": float(1 - np.sum(model_errors) / np.sum(base_errors)) if observed and sum(base_errors) > 0 else None,
            "caveat": "Chronological expanding fit on revised data, NOT a point-in-time trading backtest"}


def analyze_panel(frame, base):
    from statsmodels.stats.multitest import multipletests
    rows = []
    complete = frame.G4_USD.dropna()
    start, end = complete.index[0], complete.index[-1]
    # All lags use the same outcome months, not different convenient subperiods.
    start = start + 12 + max(LAGS)
    for basket in ("G4_USD", "G4_FIXED_FX"):
        for horizon in (3, 12):
            signal = growth(frame[basket], horizon)
            for asset in ("NASDAQ100", "US_SHARE_PRICE"):
                asset_returns = growth(frame[asset], 1)
                samples = {lag: align_sample(signal, asset_returns, lag) for lag in LAGS}
                common = pd.period_range(start, end, freq="M")
                for sample in samples.values():
                    common = common.intersection(sample.index)
                for lag in LAGS:
                    sample = samples[lag].loc[common]
                    record = {"basket": basket, "growth_months": horizon, "asset": asset, "lag_months": lag,
                              **statistics(sample)}
                    if lag == 3:
                        record["train"] = statistics(sample[sample.index < pd.Period(HOLDOUT_START)])
                        record["holdout"] = statistics(sample[sample.index >= pd.Period(HOLDOUT_START)])
                        record["walk_forward"] = walk_forward(sample)
                    rows.append(record)
    eligible = [row for row in rows if row["status"] == "OK"]
    if eligible:
        adjusted = multipletests([row["hac_p"] for row in eligible], method="holm")[1]
        for row, p in zip(eligible, adjusted):
            row["holm_p"] = float(p)
    return {"version": VERSION, "status": "EXPLORATORY_REVISED_DATA", "decision_eligible": False,
            "automatic_score_shift_allowed": False, "regions": ["United States", "Euro area", "China", "Japan"],
            "coverage": "Four monetary areas, NOT the whole world; euro-area composition can change",
            "m2_first_month": str(complete.index[0]), "m2_last_month": str(end),
            "fixed_fx_base": base, "holdout_start": HOLDOUT_START,
            "primary_hypothesis": "M2 3-month growth is associated with the one-month equity return three calendar months later",
            "lags_prespecified": list(LAGS), "family_tests": len(eligible), "rows": rows,
            "limitations": ["Latest revised values; no historical publication/vintage proof for the four-region aggregate",
                            "Legacy non-US series stop in 2017-2019; no extrapolation or partial-basket filling",
                            "Indices are price proxies, not executable SPY/QQQ adjusted total returns",
                            "NSA aggregates; seasonal controls, HAC(6), Holm correction; not causal identification",
                            "Continuous growth tested; no separately defined large-increase event threshold was tested",
                            "No trading strategy or costs modeled; no claim of fixed three-month transmission"]}


def write_charts(frame, result, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    fig.subplots_adjust(left=.09, right=.96, top=.86, bottom=.14, hspace=.33)
    fig.suptitle("FOUR-REGION M2: DOES A 3-MONTH LAG HOLD?", fontsize=17, weight="bold", y=.96)
    valid = next(row for row in result["rows"] if row["status"] == "OK")
    fig.text(.09,.905, f"{valid['from']} to {valid['to']} | Revised-data pilot | 3-month M2 growth vs monthly price change | Not a trading backtest", fontsize=9)
    for axis, asset in zip(axes, ("NASDAQ100", "US_SHARE_PRICE")):
        for basket, color in (("G4_USD", "#2867aa"), ("G4_FIXED_FX", "#b74f57")):
            subset = [r for r in result["rows"] if r["basket"] == basket and r["asset"] == asset
                      and r["growth_months"] == 3 and r["status"] == "OK"]
            axis.plot([r["lag_months"] for r in subset], [r["pearson"] for r in subset],
                      marker="o", color=color, label=basket, linewidth=1.7)
        axis.axhline(0, color="#808890", linewidth=.8)
        axis.axvline(3, color="#aaa", linestyle=":")
        axis.set_ylim(-1,1)
        axis.set_title(asset, loc="left", fontsize=11)
        axis.set_ylabel("Pearson r")
        axis.legend(frameon=False, ncol=2)
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=.15)
    axes[-1].set_xticks(LAGS)
    axes[-1].set_xlabel("M2 observation precedes return by N calendar months (0 = same month)")
    fig.text(.09,.055, "Source: FRED / IMF / Federal Reserve / OECD / Nasdaq. Different lags use the same outcome months.", fontsize=8)
    fig.savefig(directory / "lag_comparison.png", dpi=150, facecolor="white")
    plt.close(fig)
    series = growth(frame.G4_FIXED_FX, 3).dropna()
    shifted = series.copy()
    shifted.index = shifted.index + 3
    fig, axis = plt.subplots(figsize=(12,5))
    fig.subplots_adjust(left=.08, right=.96, top=.79, bottom=.18)
    axis.plot(series.index.to_timestamp(how="end"), series, color="#7a828c", alpha=.6, label="M2 growth: observation month")
    axis.plot(shifted.index.to_timestamp(how="end"), shifted, color="#b74f57", label="Same values: hypothetical impact +3 months")
    axis.axhline(0,color="#aaa",linewidth=.8)
    axis.legend(frameon=False)
    axis.set_ylabel("3-month M2 growth (%) / fixed FX")
    axis.spines[["top", "right"]].set_visible(False)
    fig.suptitle("M2 TRANSMISSION AXIS: RIGHT SHIFT, NOT A FORECAST", fontsize=15, weight="bold", y=.95)
    fig.text(.08,.84, "Original observation dates are preserved. Shifted dates are an unvalidated hypothesis; no stock prediction is shown.",fontsize=9)
    fig.text(.08,.065,"Legacy four-region history only. Current global-M2 coverage and historical release dates are not verified.",fontsize=9)
    fig.savefig(directory / "m2_three_month_axis.png",dpi=150,facecolor="white")
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="reports/m2_lag_research")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    series, provenance = fetch_inputs(directory / "inputs", args.refresh)
    frame, base = assemble_panel(series)
    result = analyze_panel(frame, base)
    result["provenance"] = provenance
    write_json_atomic(directory / "statistics.json", result)
    write_charts(frame, result, directory)
    print(json.dumps({key: value for key, value in result.items() if key not in {"rows", "provenance"}}, indent=2))
    for row in result["rows"]:
        if row["lag_months"] in {0,3}:
            print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
