"""Bounded sector-price reconstruction. No email, scores, alerts or breadth writes."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from market_internals import (RECONSTRUCTION_VERSION, SECTORS, capture_sectors,
                              eod_time, reconstruct_sectors, schedule, verified_previous_close)


def reconstruction_payload(raw, day, captured_at, price_recovery=None):
    sector = capture_sectors(raw, day, captured_at)
    sector["price_recovery"] = price_recovery or []
    rebuilt = reconstruct_sectors(sector, captured_at)
    complete = [d for d, item in rebuilt.items() if len(item["sectors"]) == 11
                and all(r["excess"]["5d"] is not None for r in item["sectors"])]
    if len(complete) != 6 or day not in complete:
        raise ValueError("Fewer than six complete sector windows; no database update")
    return {"version": RECONSTRUCTION_VERSION, "computed_at": str(captured_at),
            "point_in_time_eligible": False, "complete_dates": complete, "sectors": sector}


def recover_quote_gaps(raw, day, provider):
    import pandas as pd
    previous = schedule(day).index[-2]
    repaired, evidence = raw.copy(), []
    deadline = time.monotonic() + 180
    for symbol in ("SPY", "RSP", "QQQ", "QQQE", *SECTORS):
        if time.monotonic() >= deadline:
            break
        if symbol not in raw or pd.notna(raw[symbol].get(previous)):
            continue
        try:
            ticker = provider.Ticker(symbol)
            quote = ticker.get_info()
            terminal = ticker.history(start=day, end=str((datetime.fromisoformat(day) + timedelta(days=1)).date()),
                                      auto_adjust=False, actions=True, timeout=15)
            recovered = verified_previous_close(raw, symbol, day, quote, terminal,
                                                 datetime.now(ZoneInfo("America/New_York")).isoformat())
            if recovered:
                repaired.loc[previous, symbol] = recovered["close"]
                evidence.append(recovered)
        except Exception:
            # A failed independent check leaves the hole intact; the six-window gate still applies.
            continue
    return repaired.sort_index(), evidence


def merged_full_metrics(row, payload):
    if (not row or row.get("record_date") != payload["sectors"]["source_date"]
            or not row.get("ingested_at") or not isinstance(row.get("full_metrics"), dict)):
        raise ValueError("Existing timestamped market row required; no synthetic daily rows")
    full = deepcopy(row["full_metrics"])
    full["sector_history_reconstruction"] = deepcopy(payload)
    return full


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="Completed NYSE date; defaults to latest closed session")
    parser.add_argument("--apply", action="store_true", help="Write only the reconstruction JSON namespace")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    os.umask(0o077)
    now = datetime.now(ZoneInfo("America/New_York"))
    days = schedule(str(now.date())).index.strftime("%Y-%m-%d").tolist()
    day = args.as_of or next(d for d in reversed(days) if eod_time(now, d)[1])
    if day not in days or not eod_time(now, day)[1]:
        parser.error("Only recently completed NYSE sessions may be reconstructed")
    import yfinance as yf
    start = (datetime.fromisoformat(day) - timedelta(days=65)).date()
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date()
    downloaded = yf.download(["SPY", "RSP", "QQQ", "QQQE", *SECTORS], start=str(start),
                             end=str(end), auto_adjust=True, progress=False, timeout=25)
    raw, evidence = recover_quote_gaps(downloaded["Close"], day, yf)
    captured = datetime.now(ZoneInfo("America/New_York")).isoformat()
    payload = reconstruction_payload(raw, day, captured, evidence)
    summary = {"date": day, "captured_at": captured, "complete_dates": payload["complete_dates"],
               "kind": "RECONSTRUCTED", "point_in_time_eligible": False, "written": False,
               "verified_previous_close_count": len(evidence)}
    if args.apply:
        import market_config as cfg
        from supabase import create_client
        state = Path.home() / "market_dashboard/state"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "auto_review.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
            query = lambda: client.table("market_history").select("*").eq("record_date", day).execute().data
            rows = query()
            if len(rows) != 1:
                raise ValueError("Expected one existing market row")
            before = rows[0]
            updated = merged_full_metrics(before, payload)
            args.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = args.backup_dir / ("sector-backfill-" + now.strftime("%Y%m%dT%H%M%S%f") + ".json")
            with backup.open("x", encoding="utf-8") as stream:
                json.dump({"before": before, "reconstruction": payload}, stream, ensure_ascii=False, allow_nan=False)
            response = (client.table("market_history").update({"full_metrics": updated})
                        .eq("record_date", day).eq("ingested_at", before["ingested_at"]).execute())
            if len(response.data or []) != 1:
                raise RuntimeError("Concurrent row change; inspect backup, do not retry blindly")
            after = query()[0]
            if after.get("full_metrics") != updated:
                raise RuntimeError("Independent readback mismatch")
            if any(after.get(k) != v for k, v in before.items() if k not in ("full_metrics", "updated_at")):
                raise RuntimeError("Unexpected change outside reconstruction; inspect backup")
            summary.update(written=True, backup=str(backup), readback="MATCH")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
