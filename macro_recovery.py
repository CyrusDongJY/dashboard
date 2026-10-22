"""Partial capture outbox and date-bounded macro repair, without score backdating."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time

import pandas as pd
import requests

from fred_client import FredClient, FredUnavailable
from liquidity_sources import OfficialLiquiditySources
from macro_score import score_macro, NUM_KEYS


FRED_COLUMNS = {
    "WALCL": "Fed_Assets", "WTREGEN": "TGA", "RRPONTSYD": "RRP",
    "BAMLH0A0HYM2": "Credit_Spread", "BAMLC0A0CM": "IG_Spread",
    "M2SL": "M2_Money_Supply", "T10Y2Y": "Spread_10Y2Y", "NFCI": "NFCI",
    "DGS10": "US10Y", "DFII10": "TIPS10Y", "SOFR": "SOFR",
    "WRESBAL": "Reserves", "ECBASSETSW": "ECB_Assets", "JPNASSETS": "BOJ_Assets",
    "DEXUSEU": "EUR_USD", "DEXJPUS": "USD_JPY",
}
RAW_FIELDS = {
    "fed_assets": ("Fed_Assets", .001), "tga": ("TGA", .001),
    "rrp": ("RRP", 1), "credit_spread": ("Credit_Spread", 1),
    "ig_spread": ("IG_Spread", 1), "nfci": ("NFCI", 1),
    "sofr": ("SOFR", 1), "us10y": ("US10Y", 1), "tips": ("TIPS10Y", 1),
    "reserves": ("Reserves", .000001), "spread_10y2y": ("Spread_10Y2Y", 100),
}
PRICE_FIELDS = {"vix": "^VIX", "move": "^MOVE", "vvix": "^VVIX", "skew": "^SKEW",
                "dxy": "DX-Y.NYB", "jpy": "JPY=X", "oil": "CL=F",
                "gold": "GC=F", "btc": "BTC-USD"}
MACRO_FIELDS = tuple(dict.fromkeys((*RAW_FIELDS, "net_liq", "g3_liq", *NUM_KEYS.values())))
REPAIR_FIELDS = (*MACRO_FIELDS, *PRICE_FIELDS)
DERIVED_SOURCES = {
    "net_liq": ("Fed_Assets", "TGA", "RRP"),
    "g3_liq": ("Fed_Assets", "ECB_Assets", "EUR_USD", "BOJ_Assets", "USD_JPY"),
    "tnx_z": ("^TNX",), "oil_roc": ("CL=F",), "yc_chg": ("Spread_10Y2Y",),
    "liq_roc": ("Fed_Assets", "TGA", "RRP"), "m2_yoy": ("M2_Money_Supply",),
    "nfci": ("NFCI",), "credit_z": ("Credit_Spread",), "cg_z": ("HG=F", "GC=F"),
}
VERSION = "partial_capture_date_repair_v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def missing(value):
    if value is None or (isinstance(value, str) and value in ("", "-", "N/A", "未知")):
        return True
    return isinstance(value, float) and not math.isfinite(value)


def json_safe(value):
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return None if missing(value) else value


def merge_non_missing(old, new):
    result = deepcopy(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_non_missing(result[key], value)
        elif not missing(value) or key not in result:
            result[key] = deepcopy(value)
    return result


def merge_capture(existing, payload, captured_at):
    if existing and existing["record_date"] != payload["record_date"]:
        raise ValueError("CAPTURE_DATE_MISMATCH")
    result = merge_non_missing(existing, json_safe(payload))
    old_full = existing.get("full_metrics") or {}
    incoming = payload.get("full_metrics") or {}
    full = result.setdefault("full_metrics", {})
    # Counts, denominators, prices and timestamps form one observation cohort.
    # Recursive non-null merging would blend a retry with an older snapshot.
    if "market_internals" in incoming:
        full["market_internals"] = json_safe(deepcopy(incoming["market_internals"]))
    # Keep a score and its input cohort together if a later attempt regresses.
    if not missing(existing.get("macro_score")) and (
        missing(payload.get("macro_score")) or
        (incoming.get("macro_score_coverage") or 0) < (old_full.get("macro_score_coverage") or 0)
    ):
        for key in ("macro_score", *MACRO_FIELDS):
            if key in existing:
                result[key] = existing[key]
                full[key] = existing[key]
        for key in ("macro_score_coverage", "macro_score_status", "macro_components",
                    "macro_formula_version", "fred_source_dates", "liquidity_source_names"):
            if key in old_full:
                full[key] = deepcopy(old_full[key])
    full.setdefault("original_macro_evidence", {
        "score": existing.get("macro_score", payload.get("macro_score")),
        "captured_at": existing.get("as_of_time") or captured_at,
        "coverage": old_full.get("macro_score_coverage", incoming.get("macro_score_coverage")),
    })
    missing_fields = [k for k, v in payload.items() if k != "full_metrics" and missing(v)]
    full["capture_quality"] = {
        "version": VERSION, "captured_at": captured_at,
        "status": "PARTIAL" if missing_fields or incoming.get("source_errors") or incoming.get("macro_score_status") != "OK" else "COMPLETE",
        "macro_score_status": incoming.get("macro_score_status", "UNAVAILABLE"),
        "macro_score_coverage": incoming.get("macro_score_coverage"),
        "missing_fields": missing_fields,
        "source_errors": incoming.get("source_errors", []),
    }
    result.setdefault("source_date", payload["record_date"])
    result["as_of_time"] = existing.get("as_of_time") or captured_at
    result["ingested_at"] = utc_now()
    return result


class RecoveryStore:
    """Durable snapshots first; DB failure never loses a successful collection."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), timeout=10)
        os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY, record_date TEXT NOT NULL,
                captured_at TEXT NOT NULL, payload TEXT NOT NULL, synced INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS repairs (
                record_date TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT, detail TEXT);
        """)
        self.db.commit()

    def stage(self, payload, captured_at=None):
        at = captured_at or utc_now()
        with self.db:
            cur = self.db.execute("INSERT INTO captures(record_date,captured_at,payload) VALUES(?,?,?)",
                                  (payload["record_date"], at, json.dumps(json_safe(payload), allow_nan=False)))
        return cur.lastrowid

    def enqueue(self, day):
        datetime.strptime(day, "%Y-%m-%d")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO repairs(record_date) VALUES(?)", (day,))

    def unsynced(self):
        return self.db.execute("SELECT id,record_date,captured_at,payload FROM captures WHERE synced=0 ORDER BY id LIMIT 10").fetchall()

    def synced(self, capture_id):
        with self.db:
            self.db.execute("UPDATE captures SET synced=1 WHERE id=?", (capture_id,))

    def pending(self, before, limit=2):
        return [r[0] for r in self.db.execute(
            "SELECT record_date FROM repairs WHERE status='PENDING' AND record_date < ? "
            "ORDER BY COALESCE(last_attempt,''),record_date LIMIT ?", (before, limit))]

    def attempted(self, day, complete, detail):
        with self.db:
            self.db.execute("UPDATE repairs SET status=?, attempts=attempts+1, last_attempt=?, detail=? WHERE record_date=?",
                            ("REPAIRED" if complete else "PENDING", utc_now(), json.dumps(detail), day))


def read_row(client, day):
    rows = client.table("market_history").select("*").eq("record_date", day).execute().data
    return rows[0] if rows else {}


def write_checked(client, row):
    # Do not send generated primary keys / created timestamps back in an upsert.
    payload = {k: v for k, v in row.items() if k not in ("id", "created_at", "updated_at")}
    payload = json_safe(payload)
    client.table("market_history").upsert(payload, on_conflict="record_date").execute()
    saved = read_row(client, row["record_date"])
    if not saved or saved.get("full_metrics") != payload.get("full_metrics"):
        raise RuntimeError("DATABASE_READBACK_MISMATCH")
    return saved


def sync_captures(client, store):
    outcome = {"synced": 0, "failed": 0}
    for capture_id, day, captured_at, encoded in store.unsynced():
        try:
            payload = json.loads(encoded)
            merged = merge_capture(read_row(client, day), payload, captured_at)
            write_checked(client, merged)
            store.synced(capture_id)
            if recovery_needed(merged):
                store.enqueue(day)
            outcome["synced"] += 1
        except Exception as exc:
            outcome["failed"] += 1
            print(f"macro capture retained locally: {day}; {type(exc).__name__}")
            break
    return outcome


def recovery_needed(row):
    full = row.get("full_metrics") or {}
    if full.get("macro_recovery", {}).get("status") == "REPAIRED":
        return False
    return not row or any(missing(row.get(k)) for k in REPAIR_FIELDS) or full.get("macro_score_coverage") != 1


def discover_missing_dates(client, store, through, is_trading_day, lookback_days=10):
    end = datetime.strptime(through, "%Y-%m-%d").date()
    start = end - timedelta(days=lookback_days)
    rows = client.table("market_history").select("*").gte("record_date", str(start)).lte(
        "record_date", through).execute().data
    by_day = {r["record_date"]: r for r in rows}
    for offset in range(lookback_days + 1):
        day = str(start + timedelta(days=offset))
        if is_trading_day(day) and recovery_needed(by_day.get(day, {})):
            store.enqueue(day)


class HistoricalMacroProvider:
    def __init__(self, api_key, fred=None, price_loader=None, timeout_seconds=150):
        self.fred = fred or FredClient(api_key)
        self.official = OfficialLiquiditySources(api_key, session=requests.Session(), timeout=10)
        self.price_loader = price_loader or self._load_prices
        self.deadline = time.monotonic() + timeout_seconds

    @staticmethod
    def _load_prices(start, end):
        import yfinance as yf
        # No option chain, intraday breadth, or today's snapshot in a past row.
        symbols = list(dict.fromkeys(["^TNX", "CL=F", "HG=F", "GC=F", *PRICE_FIELDS.values()]))
        result = yf.download(symbols, start=start,
                             end=str(pd.Timestamp(end).date() + timedelta(days=1)),
                             auto_adjust=True, progress=False, threads=False, timeout=10)
        return result["Close"] if not result.empty else pd.DataFrame()

    def fetch(self, day, existing):
        start = str((pd.Timestamp(day) - pd.Timedelta(days=800)).date())
        histories, dates, sources, errors = {}, {}, {}, []
        for series_id, column in FRED_COLUMNS.items():
            if time.monotonic() >= self.deadline:
                errors.append({"series_id": series_id, "reason": "REPAIR_TIME_BUDGET"})
                continue
            try:
                native = self.fred.series(series_id, start, day)
                histories[column] = native
                dates[column] = native.index[-1].strftime("%Y-%m-%d")
                sources[column] = "FRED:" + series_id
            except FredUnavailable as exc:
                errors.append(exc.to_dict())
        fred = pd.DataFrame(histories).sort_index()
        if not fred.empty:
            days = pd.date_range(fred.index.min(), day, freq="B")
            fred = fred.reindex(fred.index.union(days)).ffill().reindex(days)
        # Match production's daily Treasury overlay and retain its native date.
        if time.monotonic() < self.deadline:
            try:
                tga = self.official.fetch_tga(str((pd.Timestamp(day) - pd.Timedelta(days=45)).date()), day)
                tga = tga.loc[tga.index <= pd.Timestamp(day)]
                if not tga.empty:
                    if fred.empty:
                        fred = pd.DataFrame(index=pd.date_range(tga.index.min(), day, freq="B"))
                    aligned = tga.reindex(fred.index.union(tga.index)).sort_index().ffill().reindex(fred.index)
                    mask = fred.index >= tga.index.min()
                    fred.loc[mask, "TGA"] = aligned.loc[mask] * 1000
                    dates["TGA"] = tga.index[-1].strftime("%Y-%m-%d")
                    sources["TGA"] = "TreasuryFiscalData:DTS"
            except Exception as exc:
                errors.append({"source": "TreasuryFiscalData:DTS", "reason": type(exc).__name__})
        original = (existing.get("full_metrics") or {}).get("macro_components") or {}
        close = pd.DataFrame()
        if (not all(original.get(k, {}).get("value") is not None for k in ("tnx", "oil", "cg"))
                or any(missing(existing.get(k)) for k in PRICE_FIELDS)) and time.monotonic() < self.deadline:
            try:
                close = self.price_loader(start, day)
                close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
                close = close.loc[close.index <= pd.Timestamp(day)]
            except Exception as exc:
                errors.append({"source": "yfinance", "reason": type(exc).__name__})
                close = pd.DataFrame()
        macro = score_macro(close, fred)
        for key in ("tnx", "oil", "cg"):
            component = original.get(key, {})
            if component.get("value") is not None:
                macro["components"][key] = component
                macro["num"][NUM_KEYS[key]] = (existing.get(NUM_KEYS[key]) if existing.get(NUM_KEYS[key]) is not None else round(component["value"], 2))
        valid = [c for c in macro["components"].values() if c["value"] is not None]
        macro["coverage"] = sum(c["weight"] for c in valid) / 100
        macro["score"] = min(100, sum(c["points"] for c in valid)) if macro["coverage"] >= .7 else None
        macro["status"] = "OK" if macro["coverage"] == 1 else ("PARTIAL" if macro["coverage"] >= .7 else "UNAVAILABLE")
        values = {k: v for k, v in macro["num"].items() if k in NUM_KEYS.values() or k == "liq_roc_window"}
        for field, symbol in PRICE_FIELDS.items():
            if symbol in close:
                native = pd.to_numeric(close[symbol], errors="coerce").dropna()
                # Do not fill a target-day quote with the previous day's price.
                if not native.empty and native.index[-1] == pd.Timestamp(day):
                    values[field] = round(float(native.iloc[-1]), 2)
                    dates[field] = day
                    sources[field] = "yfinance:" + symbol
        for symbol in ("^TNX", "CL=F", "HG=F", "GC=F"):
            if symbol in close and not close[symbol].dropna().empty:
                dates[symbol] = close[symbol].dropna().index[-1].strftime("%Y-%m-%d")
                sources[symbol] = "yfinance:" + symbol
        latest = fred.iloc[-1] if not fred.empty else pd.Series(dtype=float)
        for field, (column, scale) in RAW_FIELDS.items():
            value = latest.get(column)
            values[field] = round(float(value) * scale, 3 if field in ("reserves", "nfci") else 2) if pd.notna(value) else None
        if all(values.get(k) is not None for k in ("fed_assets", "tga", "rrp")):
            values["net_liq"] = round(values["fed_assets"] - values["tga"] - values["rrp"], 2)
        if all(pd.notna(latest.get(k)) for k in ("Fed_Assets", "ECB_Assets", "EUR_USD", "BOJ_Assets", "USD_JPY")) and latest["USD_JPY"] != 0:
            values["g3_liq"] = round(latest["Fed_Assets"] / 1000 + latest["ECB_Assets"] * latest["EUR_USD"] / 1000 + latest["BOJ_Assets"] / 10 / latest["USD_JPY"], 2)
        return {"values": values, "macro": macro, "source_dates": dates,
                "source_names": sources, "errors": errors}


def merge_repair(existing, day, repair):
    if existing and existing["record_date"] != day:
        raise ValueError("REPAIR_DATE_MISMATCH")
    if any(d > day for d in repair["source_dates"].values()):
        raise ValueError("FUTURE_SOURCE_DATE")
    at = utc_now()
    row = deepcopy(existing) or {"record_date": day, "macro_score": None}
    full = row.setdefault("full_metrics", {})
    filled = []
    for key, value in repair["values"].items():
        if key not in REPAIR_FIELDS and key not in PRICE_FIELDS and key != "liq_roc_window":
            raise ValueError("NON_MACRO_REPAIR_FIELD")
        if missing(row.get(key)) and not missing(value):
            row[key] = value
            full[key] = value
            filled.append(key)
    full.setdefault("original_macro_evidence", {"score": existing.get("macro_score"),
                    "captured_at": existing.get("as_of_time"),
                    "capture_existed": bool(existing)})
    old_repair = full.get("macro_recovery") or {}
    provenance = deepcopy(old_repair.get("field_provenance") or {})
    for field in filled:
        if field in RAW_FIELDS:
            keys = (RAW_FIELDS[field][0],)
        elif field in PRICE_FIELDS:
            keys = (field,)
        else:
            keys = DERIVED_SOURCES.get(field, ())
        source_dates = {k: repair["source_dates"][k] for k in keys if k in repair["source_dates"]}
        native_date = min(source_dates.values()) if len(source_dates) == len(keys) and keys else None
        lineage = {"source_date": native_date, "input_source_dates": source_dates,
                   "source_name": "+".join(repair["source_names"].get(k, k) for k in keys),
                   "reconstructed_at": at, "point_in_time_eligible": False}
        provenance[field] = lineage
        full.setdefault("market_metric_lineage", {})[field] = lineage
        for key in keys:
            if key in repair["source_dates"] and key in FRED_COLUMNS.values():
                full.setdefault("fred_source_dates", {})[key] = repair["source_dates"][key]
                full.setdefault("liquidity_source_names", {})[key] = repair["source_names"].get(key)
    current_macro = {k: v for k, v in repair["macro"].items() if k not in ("num", "labels")}
    best_macro = current_macro
    old_macro = old_repair.get("reconstructed_macro") or {}
    if old_macro.get("coverage", 0) > current_macro["coverage"]:
        best_macro = old_macro
    remaining = [k for k in REPAIR_FIELDS if missing(row.get(k))]
    complete = not remaining and repair["macro"]["coverage"] == 1
    full["macro_recovery"] = {
        "version": VERSION, "status": "REPAIRED" if complete else "PENDING",
        "target_date": day, "reconstructed_at": at,
        "point_in_time_eligible": False,
        "basis": "historical_observations_latest_vintage_not_original_signal",
        "filled_fields": sorted(set(old_repair.get("filled_fields", []) + filled)),
        "field_provenance": provenance,
        "remaining_fields": remaining, "source_dates": repair["source_dates"],
        "source_names": repair["source_names"], "errors": repair["errors"],
        "reconstructed_macro": best_macro,
        "last_attempt_macro": current_macro,
    }
    full.setdefault("macro_score_status", "UNAVAILABLE")
    full.setdefault("capture_quality", {"status": "PARTIAL", "reason": "HISTORICAL_MACRO_ONLY_NO_ORIGINAL_CAPTURE"})
    row.setdefault("source_date", day)
    row.setdefault("as_of_time", at)
    row["ingested_at"] = at
    return row, complete


def repair_pending(client, store, provider, before, limit=2):
    results = []
    for day in store.pending(before, limit):
        try:
            existing = read_row(client, day)
            repair = provider.fetch(day, existing)
            row, complete = merge_repair(existing, day, repair)
            write_checked(client, row)
            detail = row["full_metrics"]["macro_recovery"]
            store.attempted(day, complete, detail)
            results.append({"date": day, "status": detail["status"], "filled_fields": detail["filled_fields"],
                            "remaining_fields": detail["remaining_fields"], "errors": detail["errors"]})
        except Exception as exc:
            detail = {"reason": type(exc).__name__}
            store.attempted(day, False, detail)
            results.append({"date": day, "status": "PENDING", **detail})
        if provider.fred.unavailable_reason or time.monotonic() >= provider.deadline:
            break
    return results
