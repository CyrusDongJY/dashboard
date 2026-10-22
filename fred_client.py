"""Bounded, secret-safe FRED reads, including HTTP-200 maintenance pages."""
from __future__ import annotations

import time

import pandas as pd
import requests


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredUnavailable(RuntimeError):
    def __init__(self, series_id, reason, http_status=None):
        self.series_id = series_id
        self.reason = reason
        self.http_status = http_status
        # Never embed a response body, request URL, or underlying exception.
        super().__init__(f"FRED {series_id}: {reason} (HTTP {http_status})")

    def to_dict(self):
        return {"source": "FRED", "series_id": self.series_id,
                "reason": self.reason, "http_status": self.http_status}


class FredClient:
    def __init__(self, api_key, session=None, timeout=12, attempts=2,
                 sleep=time.sleep):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.attempts = max(1, min(3, attempts))
        self.sleep = sleep
        self.unavailable_reason = None
        self.failures = {}

    def observations(self, series_id, start_date=None, end_date=None):
        if self.unavailable_reason:
            error = FredUnavailable(series_id, self.unavailable_reason)
            self.failures[series_id] = error.to_dict()
            raise error
        params = {"api_key": self.api_key, "series_id": series_id,
                  "file_type": "json"}
        if start_date:
            params["observation_start"] = str(start_date)
        if end_date:
            params["observation_end"] = str(end_date)
        error = FredUnavailable(series_id, "NETWORK_ERROR")
        for attempt in range(self.attempts):
            try:
                response = self.session.get(FRED_URL, params=params,
                                            timeout=self.timeout)
                status = response.status_code
                body = (getattr(response, "text", "") or "")[:2048].lower()
                content_type = (getattr(response, "headers", {}) or {}).get(
                    "Content-Type", "").lower()
                if "maintenance" in body and ("<html" in body or "html" in content_type):
                    raise FredUnavailable(series_id, "UPSTREAM_MAINTENANCE", status)
                if status != 200:
                    raise FredUnavailable(series_id, "HTTP_ERROR", status)
                if "html" in content_type or body.lstrip().startswith(("<!doctype", "<html")):
                    raise FredUnavailable(series_id, "NON_JSON_RESPONSE", status)
                try:
                    payload = response.json()
                except (ValueError, requests.exceptions.JSONDecodeError):
                    raise FredUnavailable(series_id, "NON_JSON_RESPONSE", status) from None
                rows = payload.get("observations") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    raise FredUnavailable(series_id, "INVALID_SCHEMA", status)
                self.failures.pop(series_id, None)
                return rows
            except FredUnavailable as exc:
                error = exc
                if exc.http_status in (400, 401, 403, 404):
                    break
            except requests.RequestException:
                error = FredUnavailable(series_id, "NETWORK_ERROR")
            if attempt + 1 < self.attempts:
                self.sleep(0.5 * (attempt + 1))
        if error.reason in ("UPSTREAM_MAINTENANCE", "NON_JSON_RESPONSE", "NETWORK_ERROR"):
            self.unavailable_reason = error.reason
        elif error.http_status in (401, 403, 429, 500, 502, 503, 504):
            self.unavailable_reason = "SOURCE_UNAVAILABLE"
        self.failures[series_id] = error.to_dict()
        raise error from None

    def series(self, series_id, start_date=None, end_date=None):
        rows = self.observations(series_id, start_date, end_date)
        values = {}
        for row in rows:
            try:
                stamp = pd.Timestamp(row["date"]).tz_localize(None)
                value = float(row["value"])
                if pd.isna(value) or not float("-inf") < value < float("inf"):
                    continue
                if start_date and stamp < pd.Timestamp(start_date):
                    continue
                if end_date and stamp > pd.Timestamp(end_date):
                    continue
                values[stamp] = value
            except (KeyError, TypeError, ValueError):
                continue
        series = pd.Series(values, dtype=float).sort_index()
        series.index = pd.DatetimeIndex(series.index)
        if series.empty:
            error = FredUnavailable(series_id, "EMPTY_OBSERVATIONS", 200)
            self.failures[series_id] = error.to_dict()
            raise error
        return series
