"""Bounded GET-only adapters; captured timestamps are actual reception times."""
from datetime import date, datetime, timedelta
import time

import requests

from liquidity_sources import OfficialLiquiditySources
from .model import DB_METRICS, UTC, stamp, finite

FRED = {
    "fed_assets": ("WALCL", .001), "tga_weekly": ("WDTGAL", .001),
    "reserves": ("WRESBAL", .001), "rrp": ("RRPONTSYD", 1),
    "iorb": ("IORB", 1), "ig": ("BAMLC0A0CM", 100),
    "hy": ("BAMLH0A0HYM2", 100), "ccc": ("BAMLH0A3HYC", 100),
    "ust2": ("DGS2", 1), "real10": ("DFII10", 1),
}
TABLES = {
    "core_funding_daily": ("report_date", "report_date,calc_version,observation_mode,as_of,score,coverage,result,created_at"),
    "liquidity_daily": ("report_date", "report_date,calc_version,composite,coverage,state,details,created_at"),
    "metric_daily": ("report_date", "*"),
    "market_history": ("record_date", "record_date,eod_stress_score,stress_components,"
                       "stress_coverage,stress_confidence,stress_calc_version,stress_computed_at,"
                       "vix,dxy,full_metrics,ingested_at,as_of_time"),
    "temporal_state_daily": ("report_date", "*"),
    "risk_capital_daily": ("report_date", "*"),
    "risk_event_pulse_daily": ("report_date", "*"),
    "daily_review": ("report_date", "*"),
}


class BudgetSession(requests.Session):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def get(self, url, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("Collection budget exhausted")
        kwargs["timeout"] = min(8, remaining / 2)
        return super().get(url, **kwargs)


def points(series, multiplier=1):
    return [{"date": str(index.date()), "value": float(value) * multiplier}
            for index, value in series.items() if finite(value) is not None]


def fetch_table(session, config, table, start, end):
    field, select = TABLES[table]
    if table in ("core_funding_daily", "liquidity_daily"):
        start = max(start, str(date.fromisoformat(end) - timedelta(days=150)))
    elif table == "market_history":
        # Enough complete sessions for 21-day context; do not transfer years of stock snapshots.
        start = max(start, str(date.fromisoformat(end) - timedelta(days=70)))
    elif table not in ("metric_daily", "temporal_state_daily"):
        start = end
    base = config.get("SUPABASE_URL", "").rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("SUPABASE_URL must use HTTPS")
    headers = {"apikey": config["SUPABASE_KEY"],
               "Authorization": "Bearer " + config["SUPABASE_KEY"]}
    result = []
    offset = 0
    while offset < 20000:
        params = {"select": select, "and": f"({field}.gte.{start},{field}.lte.{end})",
                  "order": field + ".asc", "limit": 1000, "offset": offset}
        if table == "metric_daily":
            params.update(session="eq.EOD", scope="eq.MACRO", is_final="eq.true")
            params["metric"] = "in.(" + ",".join(DB_METRICS) + ")"
            params["order"] = "report_date.asc,metric.asc,scope.asc,session.asc"
        elif table == "temporal_state_daily":
            params["order"] = "report_date.asc,panel.asc"
        elif table == "core_funding_daily":
            params["order"] = "report_date.asc,calc_version.asc,observation_mode.asc"
        response = session.get(base + "/rest/v1/" + table, headers=headers, params=params)
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("Invalid table response")
        if not page:
            return result
        result.extend(page)
        # Respect deployments with a PostgREST max_rows smaller than our requested limit.
        offset += len(page)
    raise ValueError("Pagination limit exceeded")


def collect(config, store, report_plan, budget=180):
    until_cutoff = (stamp(report_plan["cutoff"]) - datetime.now(UTC)).total_seconds()
    session = BudgetSession(time.monotonic() + max(0, min(budget, until_cutoff)))
    official = OfficialLiquiditySources(config.get("FRED_API_KEY", ""), session=session)
    start = str(date.fromisoformat(report_plan["market_date"]) - timedelta(days=430))
    end = report_plan["market_date"]
    native_end = str(date.fromisoformat(report_plan["report_date"]) - timedelta(days=1))
    outcomes = {}

    def attempt(key, source, fn, configured=True):
        try:
            if not configured:
                status, data = "NOT_CONFIGURED", []
            else:
                data = fn()
                status = "OK" if data else "EMPTY"
        except TimeoutError:
            status, data = "DEADLINE", []
        except PermissionError:
            status, data = "ACCESS_DENIED", []
        except Exception:
            # Never log URL-bearing exception strings: FRED query strings contain API keys.
            status, data = "FETCH_ERROR", []
        store.append(key, status, data, source)
        outcomes[key] = status

    # Published overnight funding rates have priority over slower supplemental sources.
    for key, rate, market, column in (
        ("sofr", "sofr", "secured", "rate"), ("sofr99", "sofr", "secured", "p99"),
        ("effr", "effr", "unsecured", "rate"),
    ):
        attempt(key, "New York Fed / " + rate,
                lambda r=rate, m=market, c=column: points(official.fetch_nyfed_rate(r, m)[c]))
    for key, (series, multiplier) in FRED.items():
        attempt(key, "FRED / " + series,
                lambda s=series, k=multiplier: points(official.fetch_fred(s, start, native_end), k),
                bool(config.get("FRED_API_KEY")))
    attempt("tga_daily", "US Treasury / DTS", lambda: points(official.fetch_tga(start, native_end)))
    for table in TABLES:
        attempt("db_" + table, "Supabase / " + table,
                lambda t=table: fetch_table(session, config, t, start, end),
                bool(config.get("SUPABASE_URL") and config.get("SUPABASE_KEY")))
    from .capital import fetch_observations, CFTC_URL, ICI_URL, FINRA_URL
    # Supplemental slow data cannot consume the budget reserved for existing producers.
    for key, url in (("cftc_positions", CFTC_URL), ("finra_margin", FINRA_URL), ("ici_flows", ICI_URL)):
        attempt(key, url, lambda k=key: fetch_observations(session, k))
    session.close()
    return outcomes
