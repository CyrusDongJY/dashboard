"""Narrow recovery of exact rounded breadth values from an archived EOD log.

This is a manual repair tool, never part of the scheduled digest. It restores
three observations, not macro/tactical scores, alerts, or original report files.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from .model import NY, UTC, finite, stamp

FIELDS = {"pct_20ma": "20MA占比", "pct_50ma": "50MA占比", "pct_200ma": "200MA占比"}


def extract_snapshot(content, day):
    date.fromisoformat(day)
    blocks = content.split("开始执行云端自动化投研流")
    matches = []
    for block in blocks:
        stamps = re.findall(r"^\[前500大市值样本TRIN\]:.*样本数=500, 截面=([^,\s]+)", block, re.M)
        target = [s for s in stamps if stamp(s).astimezone(NY).date().isoformat() == day]
        if not target:
            continue
        if len(stamps) != 1 or len(target) != 1:
            raise ValueError("AMBIGUOUS_CAPTURE")
        at = stamp(target[0]).astimezone(NY)
        if not 16 <= at.hour < 18:
            raise ValueError("NOT_EOD_CAPTURE")
        values = {}
        for field, label in FIELDS.items():
            raw = re.findall(r"^\[" + re.escape(label) + r"\]:\s*([0-9]+(?:\.[0-9]+)?)\s*$", block, re.M)
            if len(raw) != 1 or not 0 <= float(raw[0]) <= 100:
                raise ValueError("INVALID_BREADTH_VALUE")
            values[field] = float(raw[0])
        matches.append({"source_date": day, "original_captured_at": target[0], "values": values})
    if len(matches) != 1:
        raise ValueError("NO_UNIQUE_DATED_SNAPSHOT")
    return matches[0]


def merge_snapshot(existing, snapshot, digest, restored_at):
    if existing.get("record_date") != snapshot["source_date"]:
        raise ValueError("DATE_MISMATCH")
    row = deepcopy(existing)
    full = row.setdefault("full_metrics", {})
    filled = []
    for key, value in snapshot["values"].items():
        if key not in FIELDS:
            raise ValueError("UNSUPPORTED_FIELD")
        old = finite(row.get(key))
        if old is not None:
            if old != value:
                raise ValueError("EXISTING_VALUE_CONFLICT")
            continue
        row[key] = full[key] = value
        full.setdefault("market_metric_lineage", {})[key] = {
            "source_date": snapshot["source_date"], "source_name": "archived_auto_review_log",
            "original_captured_at": snapshot["original_captured_at"],
            "restored_at": restored_at, "archive_sha256": digest,
            "point_in_time_eligible": False,
        }
        filled.append(key)
    if filled:
        full["breadth_log_recovery"] = {**snapshot, "archive_sha256": digest,
            "restored_at": restored_at, "filled_fields": filled,
            "basis": "original_500_stock_snapshot_not_recomputed", "point_in_time_eligible": False}
        row["ingested_at"] = restored_at
    return row, filled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = Path(args.log).read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError("ARCHIVE_HASH_MISMATCH")
    snapshot = extract_snapshot(raw.decode("utf-8"), args.date)
    if args.date >= datetime.now(NY).date().isoformat():
        raise ValueError("ONLY_PAST_SESSIONS")
    # Existing production API/engine contracts, imported only by this manual entrypoint.
    import fcntl
    import pandas as pd
    import market_config as cfg
    from supabase import create_client
    from macro_recovery import read_row, write_checked
    from anomaly_engine import CALC_VERSION, metric_snapshot
    state = Path.home() / "market_dashboard/state"
    with open(state / "auto_review.lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
        old = read_row(client, args.date)
        at = datetime.now(UTC).isoformat()
        row, filled = merge_snapshot(old, snapshot, args.sha256, at)
        start = str(date.fromisoformat(args.date) - timedelta(days=430))
        metrics = client.table("metric_daily").select("*").gte("report_date", start).lte(
            "report_date", args.date).eq("scope", "MACRO").eq("session", "EOD").in_(
            "metric", list(FIELDS)).order("report_date").limit(2000).execute().data
        if len(metrics) >= 1000:
            raise ValueError("HISTORY_MAY_BE_TRUNCATED")
        patches = []
        for key, value in snapshot["values"].items():
            current = [m for m in metrics if m["report_date"] == args.date and m["metric"] == key]
            if len(current) > 1:
                raise ValueError("DUPLICATE_CURRENT_METRIC")
            if current and current[0].get("is_filled") is False:
                if current[0].get("value") != value:
                    raise ValueError("VALID_METRIC_CONFLICT")
                continue
            if current and current[0]["calc_version"] != CALC_VERSION:
                raise ValueError("METRIC_VERSION_MISMATCH")
            points = {m["report_date"]: m["value"] for m in metrics if m["metric"] == key
                      and m.get("is_filled") is False and m.get("is_final") is True
                      and m["source_date"] == m["report_date"] and m["calc_version"] == CALC_VERSION}
            points[args.date] = value
            series = pd.Series(points).sort_index()
            series.index = pd.to_datetime(series.index)
            patch = metric_snapshot(key, series, args.date, source_date=args.date,
                session="EOD", is_final=True, is_filled=False,
                source_name="archived_auto_review_log / " + args.sha256)
            patches.append(patch)
        backup = Path(args.backup_dir)
        backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name, payload in (("before.json", {"market_history": old, "metric_daily": metrics}),
                              ("plan.json", {"snapshot": snapshot, "filled": filled, "patches": patches})):
            p = backup / name
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            p.chmod(0o600)
        if args.apply:
            if filled:
                write_checked(client, row)
            for patch in patches:
                client.table("metric_daily").upsert(patch, on_conflict="report_date,metric,scope,session").execute()
                actual = client.table("metric_daily").select("*").eq("report_date", args.date).eq(
                    "metric", patch["metric"]).eq("scope", "MACRO").eq("session", "EOD").execute().data
                if len(actual) != 1 or any(actual[0].get(k) != patch[k] for k in (
                        "value", "source_date", "is_filled", "is_final", "calc_version", "source_name")):
                    raise RuntimeError("METRIC_READBACK_MISMATCH")
        print(json.dumps({"applied": args.apply, "values": snapshot["values"], "fields": filled,
                          "metrics": len(patches), "scores_restored": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
