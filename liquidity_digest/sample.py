"""Explicit synthetic inputs for layout and deployment smoke tests, never emailed."""
from datetime import date, datetime, time, timedelta
import math

import pandas as pd
from market_internals import OBSERVATION_VERSION, SECTORS, capture_breadth, capture_sectors

from .model import NY, SPECS, DB_METRICS, assemble, sessions, native_sessions, stamp
from .sources import FRED
from .frontpage import CORE_VERSION, LIQUIDITY_VERSION


def sample_report(report_plan):
    end = date.fromisoformat(report_plan["market_date"])
    days = sessions(end - timedelta(days=420), end)
    capture = (stamp(report_plan["cutoff"]).astimezone(NY) - timedelta(minutes=5)).isoformat()
    baseline = {"fed_assets": 6500, "tga_weekly": 700, "rrp": 40, "reserves": 3200,
                "tga_daily": 710, "sofr": 4.3, "effr": 4.28, "iorb": 4.4,
                "sofr99": 4.36, "ig": 80, "hy": 300, "ccc": 850,
                "ust2": 4.1, "real10": 1.8, "vix": 28, "vix_ratio_contango_pct": 8,
                "dxy": 100, "usdjpy_return_5d_pct": -.8, "pct_200ma": 58,
                "pct_20ma": 56, "pct_50ma": 57,
                "spy_rsp_spread_pct": .3, "spy_vwap_time_acceptance_pct": 62,
                "qqq_vwap_time_acceptance_pct": 57}
    inputs, metric_rows = {}, []
    for key, base in baseline.items():
        _, _, frequency = SPECS[key]
        selected = ([d for d in days if date.fromisoformat(d).weekday() == 2] if frequency == "weekly"
                    else native_sessions(key, end - timedelta(days=420), end))
        values = [{"date": d, "value": base + (math.sin(i / 8) + i / 100) * abs(base) * .025}
                  for i, d in enumerate(selected)]
        if key == "ccc":
            values = values[:-1]
        if key == "vix":
            for i, point in enumerate(values[-6:]):
                point["value"] = 31 - .6 * i
        if key == "vix_ratio_contango_pct":
            values[-1]["value"] = 3
        # Demonstrate macro relief alongside weaker equity-market internals.
        endpoints = {
            "iorb": (4.4, 4.4), "sofr": (4.80, 4.46), "effr": (4.66, 4.44),
            "sofr99": (5.55, 4.74), "ig": (140, 105), "hy": (520, 410),
            "pct_20ma": (70, 38), "pct_50ma": (70, 42), "pct_200ma": (64, 48),
            "spy_vwap_time_acceptance_pct": (70, 44),
            "qqq_vwap_time_acceptance_pct": (68, 39),
        }
        if key in endpoints:
            start, finish = endpoints[key]
            for i, point in enumerate(values[-22:]):
                point["value"] = start + (finish - start) * i / 21
        if key in DB_METRICS:
            metric_rows.extend({"report_date": p["date"], "source_date": p["date"], "metric": key,
                                "value": p["value"], "scope": "MACRO", "session": "EOD",
                                "is_final": True, "is_filled": False, "calc_version": "SYNTHETIC"}
                               for p in values)
        else:
            inputs[key] = {"data": values, "status": "OK", "source": "SYNTHETIC / " + FRED.get(
                key, (key,))[0], "captured_at": capture}
    inputs["db_metric_daily"] = {"data": metric_rows, "status": "OK", "captured_at": capture}
    temporal = []
    for j, panel in enumerate(("environment", "tactical_stress", "risk_capital", "liquidity")):
        for i, day in enumerate(days[-90:]):
            if i in (27, 28):
                continue
            value = 45 + 25 * i / 89 + 8 * math.sin(i / 10 + j)
            if i >= 84:
                base, slope = {"environment": (80, -1.6), "tactical_stress": (78, -1.4),
                               "risk_capital": (56, 1.2), "liquidity": (60, -2)}[panel]
                value = base + slope * (i - 84)
            temporal.append({"report_date": day, "source_date": day, "panel": panel,
                             "risk_value": value, "coverage": .95, "confidence": .95,
                             "observation_mode": "LIVE_SHADOW", "quality_status": "OK",
                             "calc_version": "SYNTHETIC",
                             "details": {"source_calc_version": "SYNTHETIC_" + panel}})
    inputs["db_temporal_state_daily"] = {"data": temporal, "status": "OK", "captured_at": capture}
    components = {key: {"value": value, "contribution": contribution, "status": "OK"}
                  for key, value, contribution in (("vix", 28, 46), ("move", 125, 15),
                                                  ("credit_spread", 4.1, 0), ("trin", 1.1, 0),
                                                  ("pcr", 1.1, 10), ("cmf", .03, 0))}
    components["_meta"] = {"status": "OK"}
    components["_context"] = {"vix_5d_change": -3, "vix_percentile_252": 88, "vix_term_ratio": .97}
    market_rows = []
    price_days = days[-60:]
    rates = dict(zip(SECTORS, (-.0015, .002, .003, .0015, -.0008, -.0025, .0035, .001, -.0005, .0025, .004)))
    rates.update(SPY=.001, RSP=.0015, QQQ=.0012, QQQE=.002)
    prices = pd.DataFrame({s: [100 * (1 + r) ** i for i in range(len(price_days))]
                           for s, r in rates.items()}, index=pd.to_datetime(price_days))
    for i, day in enumerate(days[-8:]):
        captured = datetime.combine(date.fromisoformat(day), time(16, 35), NY).isoformat()
        stocks = [{"s": f"NASDAQ:DEMO{j}", "d": [f"DEMO{j}", 1 if j < 280 + i * 12 else 0 if j < 290 + i * 12 else -1,
                   100, 90, 95, 98, 120, 50, 1000]} for j in range(500)]
        market_rows.append({"record_date": day, "ingested_at": captured,
                            "full_metrics": {"market_internals": {
                                "version": OBSERVATION_VERSION, "computed_at": captured,
                                "breadth": capture_breadth(stocks, captured),
                                "sectors": capture_sectors(prices, day, captured)}}})
    market_rows[-1].update({
        "record_date": str(end), "eod_stress_score": 71, "stress_coverage": .95, "stress_confidence": .95,
        "stress_components": components, "stress_calc_version": "SYNTHETIC"})
    inputs["db_market_history"] = {"data": market_rows, "status": "OK", "captured_at": capture}
    core_rows, liquidity_rows = [], []
    liquidity_values = {r["report_date"]: 100 - r["risk_value"] for r in temporal if r["panel"] == "liquidity"}
    for i, day in enumerate(days[-90:]):
        if i in (27, 28):
            continue
        computed = datetime.combine(date.fromisoformat(day), time(17), NY).isoformat()
        score = round(40 + i / 4 + 9 * math.sin(i / 12), 2)
        core = dict(report_date=day, calc_version=CORE_VERSION, observation_mode="LIVE_SHADOW",
                    as_of=computed, score=score, coverage=1.0)
        core_rows.append({**core, "created_at": computed, "result": {**core, "state": "SUPPORTIVE"}})
        liquidity_rows.append(dict(report_date=day, calc_version=LIQUIDITY_VERSION, created_at=computed,
                                   composite=round(liquidity_values[day], 2), coverage=1.0,
                                   state="低位修复", details={"weight_policy": "FIXED_BASKET_FULL_COVERAGE"}))
    inputs["db_core_funding_daily"] = {"data": core_rows, "status": "OK", "captured_at": capture}
    inputs["db_liquidity_daily"] = {"data": liquidity_rows, "status": "OK", "captured_at": capture}
    from .capital import record, CFTC_URL, ICI_URL, FINRA_URL, CONTRACTS, CATEGORIES
    weekly = end - timedelta(days=(end.weekday() - 1) % 7)
    cot = []
    for week in range(6):
        day = str(weekly - timedelta(weeks=week))
        for code in CONTRACTS:
            for category in CATEGORIES:
                long, short = (120000 - week * 1000, 40000) if category == "asset_mgr" else (45000, 60000 - week * 500)
                cot.append(record(f"cot_{code}_{category}", day, long-short, capture, CFTC_URL, "SYNTHETIC",
                                  dict(long=long, short=short, spreading=5000, open_interest=250000,
                                       net_oi_pct=(long-short)/2500)))
    inputs["cftc_positions"] = {"data": cot, "status": "OK", "captured_at": capture}
    ici_day = end - timedelta(days=7 + (end.weekday() - 2) % 7)
    inputs["ici_flows"] = {"status": "OK", "captured_at": capture,
                          "data": [record("ici_domestic", str(ici_day - timedelta(weeks=i)), 2500-i*900,
                                          capture, ICI_URL, "SYNTHETIC", {"estimate": True}) for i in range(5)]}
    month = pd.Timestamp(end).replace(day=1) - pd.Timedelta(days=1)
    inputs["finra_margin"] = {"status": "OK", "captured_at": capture,
                             "data": [record("finra_debit", str((month-pd.DateOffset(months=i)+pd.offsets.MonthEnd(0)).date()),
                                             1400000-i*18000, capture, FINRA_URL, "SYNTHETIC") for i in range(4)]}
    for table, state in (("risk_capital_daily", "边缘收缩"), ("risk_event_pulse_daily", "事件驱动，待市场确认")):
        inputs["db_" + table] = {"data": [{"report_date": str(end), "state_cn": state}], "status": "OK"}
    return assemble(report_plan, inputs, demo=True)
