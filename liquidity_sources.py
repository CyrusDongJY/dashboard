# -*- coding: utf-8 -*-
"""Official-source adapters for the liquidity monitor.

The adapter keeps native observation dates and only aligns series after they
have been fetched. This prevents weekly/monthly observations from masquerading
as fresh daily data.
"""
from __future__ import annotations

from datetime import date
import logging
from typing import Dict, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
TREASURY_TGA_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/dts/operating_cash_balance"
)
NYFED_URL = "https://markets.newyorkfed.org/api/rates"

FRED_SERIES = {
    # output column: (FRED id, unit multiplier)
    "fed_assets_b": ("WALCL", 1 / 1_000),       # USD millions -> billions
    "reserves_t": ("WRESBAL", 1 / 1_000_000),  # USD millions -> trillions
    "rrp_b": ("RRPONTSYD", 1.0),                # USD billions
    "m2_b": ("M2SL", 1.0),                      # USD billions
    "iorb": ("IORB", 1.0),                      # percent
    "nfci": ("NFCI", 1.0),
    "hy_spread": ("BAMLH0A0HYM2", 1.0),         # percentage points
    "ig_spread": ("BAMLC0A0CM", 1.0),
}


def _number(value):
    if value in (None, "", "null", "."):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


class OfficialLiquiditySources:
    """Fetch source-native liquidity histories from public official APIs."""

    def __init__(self, fred_api_key: str, timeout: int = 12,
                 session: Optional[requests.Session] = None):
        self.fred_api_key = fred_api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        if session is None:
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(("GET",)),
                respect_retry_after_header=True,
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "market-dashboard-liquidity-monitor/1.0",
            "Accept": "application/json",
        })

    def fetch_fred(self, series_id: str, start_date: str,
                   end_date: Optional[str] = None) -> pd.Series:
        params = {
            "series_id": series_id,
            "api_key": self.fred_api_key,
            "file_type": "json",
            "observation_start": start_date,
        }
        if end_date:
            params["observation_end"] = end_date
        response = self.session.get(FRED_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        rows = response.json().get("observations", [])
        values = {
            pd.Timestamp(row["date"]): _number(row.get("value"))
            for row in rows
            if row.get("date")
        }
        series = pd.Series(values, dtype=float).dropna().sort_index()
        series.index = pd.to_datetime(series.index).tz_localize(None)
        return series.loc[~series.index.duplicated(keep="last")]

    def fetch_tga(self, start_date: str,
                  end_date: Optional[str] = None) -> pd.Series:
        filters = [
            f"record_date:gte:{start_date}",
            "account_type:eq:Treasury General Account (TGA) Closing Balance",
        ]
        if end_date:
            filters.insert(1, f"record_date:lte:{end_date}")
        page_size = 5000
        base_params = {
            "filter": ",".join(filters),
            "sort": "record_date",
            "page[size]": page_size,
            "format": "json",
        }
        values = {}
        page_number = 1
        while True:
            params = {**base_params, "page[number]": page_number}
            response = self.session.get(
                TREASURY_TGA_URL, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", [])
            for row in rows:
                raw = row.get("close_today_bal")
                value = _number(raw)
                if value is None:
                    # The current DTS schema stores the closing-row amount here.
                    value = _number(row.get("open_today_bal"))
                if value is not None and row.get("record_date"):
                    values[pd.Timestamp(row["record_date"])] = value / 1_000

            total_pages = _number(payload.get("meta", {}).get("total-pages"))
            if total_pages is not None:
                if page_number >= int(total_pages):
                    break
            elif len(rows) < page_size:
                break
            page_number += 1
            if page_number > 100:
                raise RuntimeError("Treasury DTS pagination exceeded 100 pages")
        series = pd.Series(values, dtype=float).sort_index()
        if not series.empty:
            series.index = pd.to_datetime(series.index).tz_localize(None)
        return series

    def fetch_nyfed_rate(self, rate_type: str, market: str,
                         limit: int = 800) -> pd.DataFrame:
        url = f"{NYFED_URL}/{market}/{rate_type.lower()}/last/{limit}.json"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        rows = []
        for row in response.json().get("refRates", []):
            if not row.get("effectiveDate"):
                continue
            rows.append({
                "date": pd.Timestamp(row["effectiveDate"]),
                "rate": _number(row.get("percentRate")),
                "p75": _number(row.get("percentPercentile75")),
                "p99": _number(row.get("percentPercentile99")),
                "volume_b": _number(row.get("volumeInBillions")),
            })
        if not rows:
            return pd.DataFrame(columns=["rate", "p75", "p99", "volume_b"])
        frame = pd.DataFrame(rows).set_index("date").sort_index()
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return frame.loc[~frame.index.duplicated(keep="last")]

    @staticmethod
    def _asof_column(target_index: pd.DatetimeIndex, series: pd.Series,
                     name: str) -> pd.DataFrame:
        if series is None or series.empty:
            return pd.DataFrame(index=target_index)
        series = pd.to_numeric(series, errors="coerce").dropna().sort_index()
        native_dates = pd.Series(series.index, index=series.index)
        union = target_index.union(series.index).sort_values()
        return pd.DataFrame({
            name: series.reindex(union).ffill().reindex(target_index),
            f"{name}_source_date": (
                native_dates.reindex(union).ffill().reindex(target_index)
            ),
        }, index=target_index)

    def build_frame(self, start_date: str,
                    end_date: Optional[str] = None) -> pd.DataFrame:
        end = pd.Timestamp(end_date or date.today().isoformat()).normalize()
        target = pd.bdate_range(pd.Timestamp(start_date), end)
        result = pd.DataFrame(index=target)

        fred_native: Dict[str, pd.Series] = {}
        for column, (series_id, multiplier) in FRED_SERIES.items():
            try:
                series = self.fetch_fred(series_id, start_date, str(end.date()))
                fred_native[column] = series * multiplier
                result = result.join(
                    self._asof_column(target, fred_native[column], column),
                    how="left",
                )
            except Exception as exc:
                logger.warning("FRED %s fetch failed: %s", series_id, exc)

        try:
            tga = self.fetch_tga(start_date, str(end.date()))
            result = result.join(
                self._asof_column(target, tga, "tga_b"), how="left")
        except Exception as exc:
            logger.warning("Treasury daily TGA fetch failed: %s", exc)

        for rate_type, market in (
            ("sofr", "secured"),
            ("tgcr", "secured"),
            ("effr", "unsecured"),
        ):
            try:
                frame = self.fetch_nyfed_rate(rate_type, market)
                if frame.empty:
                    continue
                for field in frame.columns:
                    name = f"{rate_type}_{field}"
                    result = result.join(
                        self._asof_column(target, frame[field], name),
                        how="left",
                    )
            except Exception as exc:
                logger.warning("NY Fed %s fetch failed: %s", rate_type, exc)

        if "m2_b" in fred_native:
            m2_yoy = fred_native["m2_b"].pct_change(12) * 100
            result = result.join(
                self._asof_column(target, m2_yoy.dropna(), "m2_yoy"),
                how="left",
            )

        required = {"fed_assets_b", "tga_b", "rrp_b"}
        if required.issubset(result.columns):
            result["net_liq_b"] = (
                result["fed_assets_b"] - result["tga_b"] - result["rrp_b"])
            source_cols = [f"{column}_source_date" for column in required]
            result["net_liq_b_source_date"] = result[source_cols].min(axis=1)

        if "iorb" in result:
            for rate in ("sofr", "tgcr", "effr"):
                rate_col = f"{rate}_rate"
                if rate_col in result:
                    result[f"{rate}_iorb_bps"] = (
                        result[rate_col] - result["iorb"]) * 100
                    result[f"{rate}_iorb_bps_source_date"] = result[
                        [f"{rate_col}_source_date", "iorb_source_date"]
                    ].min(axis=1)
            if "sofr_p75" in result:
                result["sofr_75_iorb_bps"] = (
                    result["sofr_p75"] - result["iorb"]) * 100
                result["sofr_75_iorb_bps_source_date"] = result[
                    ["sofr_p75_source_date", "iorb_source_date"]
                ].min(axis=1)
            if {"sofr_p99", "sofr_rate"}.issubset(result.columns):
                result["sofr_dispersion_bps"] = (
                    result["sofr_p99"] - result["sofr_rate"]) * 100
                result["sofr_dispersion_bps_source_date"] = result[
                    ["sofr_p99_source_date", "sofr_rate_source_date"]
                ].min(axis=1)

        result.index.name = "report_date"
        return result
