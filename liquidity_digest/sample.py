"""Explicit synthetic inputs for layout and deployment smoke tests, never emailed."""
from datetime import date, timedelta
import math

from .model import NY, SPECS, DB_METRICS, assemble, sessions, stamp
from .sources import FRED


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
        selected = [d for d in days if date.fromisoformat(d).weekday() == 2] if frequency == "weekly" else days
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
    inputs["db_market_history"] = {"data": [{
        "record_date": str(end), "eod_stress_score": 71, "stress_coverage": .95,
        "stress_components": components, "stress_calc_version": "SYNTHETIC"}], "status": "OK"}
    for table, state in (("risk_capital_daily", "边缘收缩"), ("risk_event_pulse_daily", "事件驱动，待市场确认")):
        inputs["db_" + table] = {"data": [{"report_date": str(end), "state_cn": state}], "status": "OK"}
    return assemble(report_plan, inputs, demo=True)
