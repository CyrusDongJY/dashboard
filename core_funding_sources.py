"""Versioned, native-frequency inputs for the independent funding index.

The v1 contract intentionally freezes ALFRED first releases. Revisions are not
silently spliced into this history. Date-only vintage availability is deferred
to the next NY calendar day, unless an actual earlier capture proves availability.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
SOURCE_VERSION = "alfred_initial_v1"
NY = "America/New_York"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class SourceSpec:
    fred_id: str
    multiplier: float
    frequency: str
    unit: str


SOURCES = {
    "fed_assets": SourceSpec("WALCL", .001, "weekly", "USD billions"),
    "tga": SourceSpec("WDTGAL", .001, "weekly", "USD billions"),
    "rrp": SourceSpec("RRPONTSYD", 1., "daily", "USD billions"),
    "reserves": SourceSpec("WRESBAL", .001, "weekly", "USD billions"),
    "bank_assets": SourceSpec("TLAACBW027SBOG", 1., "weekly", "USD billions"),
    "m2": SourceSpec("M2SL", 1., "monthly", "USD billions"),
    "sofr": SourceSpec("SOFR", 1., "daily", "percent"),
    "effr": SourceSpec("EFFR", 1., "daily", "percent"),
    "iorb": SourceSpec("IORB", 1., "daily", "percent"),
    "sofr99": SourceSpec("SOFR99", 1., "daily", "percent"),
    "loans": SourceSpec("TOTLL", 1., "weekly", "USD billions"),
    "deposits": SourceSpec("DPSACBW027SBOG", 1., "weekly", "USD billions"),
    "loan_standards": SourceSpec("DRTSCILM", 1., "quarterly", "percent"),
}


def utc_time(value):
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return result.tz_convert("UTC")


def digest_rows(rows):
    ordered = sorted(rows, key=lambda row: (row["series"], row["observation_date"]))
    return hashlib.sha256(json.dumps(
        ordered, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class CoreFundingSources:
    def __init__(self, fred_api_key, timeout=12, session=None, source_specs=None, deadline=None):
        self.fred_api_key = fred_api_key
        self.timeout = timeout
        self.source_specs = source_specs
        self.deadline = deadline
        self.session = session if session is not None else requests.Session()
        if session is None:
            retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=.6,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=frozenset(("GET",)), respect_retry_after_header=True)
            self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({"User-Agent": "core-funding-monitor/1.0",
                                     "Accept": "application/json"})

    def _observation_pages(self, params):
        params = params.copy()
        for _ in range(100):
            timeout = self.timeout
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Optional source budget exhausted")
                timeout = min(timeout, remaining / 2)
            response = self.session.get(FRED_URL, params=params.copy(), timeout=timeout)
            if response.status_code == 400:
                error = str(response.json().get("error_message", "")).lower()
                if "vintage dates" in error and "exceeds" in error and "maximum" in error:
                    # FRED limits JSON to 2,000 vintages independently of row pagination.
                    start = pd.Timestamp(params["realtime_start"])
                    end = pd.Timestamp(params["realtime_end"])
                    split = end - pd.DateOffset(years=5)
                    if start < split < end and params["offset"] == 0:
                        yield from self._observation_pages({**params, "realtime_end": str(split.date())})
                        yield from self._observation_pages({
                            **params, "realtime_start": str((split + pd.Timedelta(days=1)).date())})
                        return
            response.raise_for_status()
            payload = response.json()
            if "observations" not in payload or "count" not in payload:
                raise ValueError("Invalid ALFRED observations response")
            yield payload
            params["offset"] += len(payload["observations"])
            if params["offset"] >= int(payload["count"]):
                return
            if not payload["observations"]:
                raise ValueError("Incomplete ALFRED pagination")
        raise ValueError("ALFRED pagination exceeded safety limit")

    def fetch_initial_releases(self, key, start_date, captured_at=None):
        spec = (SOURCES if self.source_specs is None else self.source_specs)[key]
        captured = utc_time(captured_at if captured_at is not None else pd.Timestamp.now(tz="UTC"))
        end_date = str(captured.tz_convert(NY).date())
        params = {
            "api_key": self.fred_api_key, "series_id": spec.fred_id,
            "file_type": "json", "output_type": 4,
            "observation_start": start_date, "observation_end": end_date,
            "realtime_start": "1776-07-04", "realtime_end": end_date,
            "limit": 10000, "offset": 0, "sort_order": "asc",
        }
        records = {}
        for payload in self._observation_pages(params):
            page = payload["observations"]
            for row in page:
                if row.get("value") in (None, ".", ""):
                    continue
                value = float(row["value"]) * spec.multiplier
                if not math.isfinite(value):
                    raise ValueError("Non-finite ALFRED value")
                observation = pd.Timestamp(row["date"]).normalize()
                release = pd.Timestamp(row["realtime_start"]).normalize()
                if release.date() > captured.tz_convert(NY).date():
                    raise ValueError("Invalid first-release date")
                conservative = (release + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
                record = {
                    "series": key, "observation_date": str(observation.date()),
                    "value": value, "release_date": str(release.date()),
                    "available_at": min(conservative, captured).isoformat(),
                    "source": spec.fred_id, "source_version": SOURCE_VERSION,
                }
                old = records.get(record["observation_date"])
                if old:
                    if old["release_date"] < record["release_date"]:
                        continue
                    if old["release_date"] == record["release_date"] and old != record:
                        raise ValueError("Conflicting first releases for one observation")
                records[record["observation_date"]] = record
        received = utc_time(captured_at if captured_at is not None else pd.Timestamp.now(tz="UTC"))
        for record in records.values():
            conservative = (pd.Timestamp(record["release_date"]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
            record["available_at"] = min(conservative, received).isoformat()
        return list(records.values())

    def collect(self, cache_dir, start_date="2008-01-01", now=None, refresh=False):
        fixed_clock = now
        now = utc_time(now if now is not None else pd.Timestamp.now(tz="UTC"))
        cache_dir = Path(cache_dir)
        rows, statuses = [], {}
        for key in (SOURCES if self.source_specs is None else self.source_specs):
            path = cache_dir / f"{SOURCE_VERSION}_{key}.json"
            cached = None
            try:
                if path.exists():
                    cached = json.loads(path.read_text())
                    if (cached.get("version") != SOURCE_VERSION
                            or cached.get("start_date", "9999") > start_date
                            or digest_rows(cached["rows"]) != cached["digest"]):
                        cached = None
            except (ValueError, KeyError, OSError):
                cached = None
            fresh_cache = cached and pd.Timedelta(0) <= (
                now - utc_time(cached["fetched_at"])) < pd.Timedelta(hours=6)
            if fresh_cache and not refresh:
                rows.extend(cached["rows"])
                statuses[key] = "CACHED"
                continue
            try:
                fetched = self.fetch_initial_releases(key, start_date, fixed_clock)
                # Keep the earliest actual capture and reject silently revised initial values.
                prior = {r["observation_date"]: r for r in (cached or {}).get("rows", [])}
                for record in fetched:
                    old = prior.get(record["observation_date"])
                    if old:
                        if (old["value"], old["release_date"]) != (
                                record["value"], record["release_date"]):
                            raise ValueError("Initial-release contract changed; review required")
                        record["available_at"] = min(utc_time(old["available_at"]),
                                                     utc_time(record["available_at"])).isoformat()
                    prior[record["observation_date"]] = record
                fetched = list(prior.values())
                if not fetched:
                    raise ValueError("Empty ALFRED source")
                write_json_atomic(path, {
                    "version": SOURCE_VERSION, "start_date": start_date,
                    "fetched_at": now.isoformat(), "rows": fetched,
                    "digest": digest_rows(fetched),
                })
                rows.extend(fetched)
                statuses[key] = "FETCHED"
            except Exception as exc:
                # Request URLs may contain credentials; do not log exception text.
                logger.warning("Core funding source %s unavailable (%s)", key, type(exc).__name__)
                statuses[key] = "CACHED_AFTER_ERROR" if cached else "UNAVAILABLE"
                rows.extend((cached or {}).get("rows", []))
        return {"rows": rows, "source_status": statuses, "fetched_at": now.isoformat(),
                "version": SOURCE_VERSION, "digest": digest_rows(rows)}
