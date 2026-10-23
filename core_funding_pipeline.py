"""Standalone CLI for the funding index; no production-program imports."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import pandas as pd

from core_funding_index import (CALC_VERSION, ObservationStore, add_trend,
                                build_core_history, compute_core_funding)
from core_funding_report import (format_core_funding, generate_core_funding_chart,
                                 generate_diagnostics_chart)
from core_funding_diagnostics import (DIAGNOSTICS_VERSION, build_diagnostics, collect_credit_context,
                                      empty_credit_archive, format_diagnostics)
from core_funding_m2 import build_m2_transmission, format_m2_transmission
from core_funding_context import collect_context, build_context, format_context
from core_funding_sources import (CoreFundingSources, NY, SOURCE_VERSION, digest_rows,
                                  utc_time, write_json_atomic)

logger = logging.getLogger(__name__)
TABLE = "core_funding_daily"


def market_sessions(end_date, count):
    import pandas_market_calendars as mcal
    start = pd.Timestamp(end_date) - pd.Timedelta(days=count * 2 + 30)
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end_date)
    return list(schedule.index[-count:])


def fetch_adjusted_benchmarks(start_date, end_date):
    import yfinance as yf
    frames = {}
    for ticker in ("SPY", "QQQ"):
        try:
            data = yf.Ticker(ticker).history(
                start=start_date, end=str((pd.Timestamp(end_date) + pd.Timedelta(days=1)).date()),
                auto_adjust=True, actions=False, timeout=15)
            if not data.empty and "Close" in data:
                series = pd.to_numeric(data["Close"], errors="coerce")
                series.index = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
                frames[ticker] = series.loc[~series.index.duplicated()]
        except Exception as exc:
            logger.warning("Funding comparison %s unavailable (%s)", ticker, type(exc).__name__)
    return pd.DataFrame(frames)


def fetch_issued_history(client, report_date, mode="LIVE_SHADOW"):
    if client is None:
        return []
    response = (client.table(TABLE).select("result").eq("calc_version", CALC_VERSION)
                .eq("observation_mode", mode).lte("report_date", report_date)
                .order("report_date", desc=True).limit(800).execute())
    return sorted([row["result"] for row in response.data or []], key=lambda r: r["report_date"])


def persist_result(client, result):
    """Insert-once by date/version/mode and read back the winner, even after a race."""
    if client is None:
        result["persistence_status"] = "NOT_REQUESTED"
        return result
    try:
        row = {key: result[key] for key in (
            "report_date", "calc_version", "observation_mode", "as_of", "score", "coverage")}
        row["result"] = {**result, "persistence_status": "STORED"}
        client.table(TABLE).upsert(
            row, on_conflict="report_date,calc_version,observation_mode",
            ignore_duplicates=True).execute()
        query = client.table(TABLE).select("result")
        for key in ("report_date", "calc_version", "observation_mode"):
            query = query.eq(key, result[key])
        response = query.limit(1).execute()
        if not response.data:
            raise RuntimeError("Funding insert was not confirmed by readback")
        saved = dict(response.data[0]["result"])
        saved["persistence_status"] = "VERIFIED"
        return saved
    except Exception as exc:
        logger.warning("Core funding persistence failed (%s)", type(exc).__name__)
        result["persistence_status"] = "FAILED"
        return result


def run_core_funding(fred_api_key=None, client=None, output_dir="reports", as_of=None,
                     archive=None, prices=None, sessions=300, refresh=False, write_replay=False,
                     credit_archive=None, context_archive=None):
    cutoff = utc_time(as_of if as_of is not None else pd.Timestamp.now(tz="UTC"))
    if cutoff > pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=5):
        raise ValueError("Future funding snapshots are forbidden")
    if sessions < 21 or sessions > 2000:
        raise ValueError("sessions must be between 21 and 2000")
    report_date = str(cutoff.tz_convert(NY).date())
    output_dir = Path(output_dir)
    if archive is None:
        if not fred_api_key:
            raise ValueError("FRED_API_KEY is required, or provide an archived input")
        archive = CoreFundingSources(fred_api_key).collect(
            output_dir / "core_funding_cache", refresh=refresh)
    if credit_archive is None:
        try:
            credit_archive = collect_credit_context(
                fred_api_key, output_dir / "core_funding_credit_cache", refresh=refresh)
        except Exception as exc:
            credit_archive = empty_credit_archive()
            credit_archive["source_status"] = {key: "UNAVAILABLE" for key in credit_archive["source_status"]}
            logger.warning("Optional credit sources unavailable (%s)", type(exc).__name__)
    if as_of is None:
        cutoff = pd.Timestamp.now(tz="UTC")
        report_date = str(cutoff.tz_convert(NY).date())
        if cutoff < pd.Timestamp(f"{report_date} 16:45", tz=NY).tz_convert("UTC"):
            raise ValueError("LIVE_SHADOW requires 16:45 ET or later; use --as-of for historical preview")
    if archive.get("version") != SOURCE_VERSION or digest_rows(archive["rows"]) != archive.get("digest"):
        raise ValueError("Funding archive version or checksum mismatch")
    store = ObservationStore(archive["rows"])
    mode = "PIT_REPLAY" if as_of is not None else "LIVE_SHADOW"
    result = compute_core_funding(store, cutoff, mode)
    dates = market_sessions(report_date, sessions)
    if not dates or str(dates[-1].date()) != report_date:
        raise ValueError("The report date must be a NYSE session")
    if cutoff < pd.Timestamp(f"{report_date} 16:45", tz=NY).tz_convert("UTC"):
        dates = dates[:-1]
    history = build_core_history(store, dates)
    # Live trends use actual issued live snapshots, never a mixture with replay.
    issued = []
    try:
        issued = fetch_issued_history(client, report_date, mode)
    except Exception as exc:
        result["history_status"] = "UNAVAILABLE"
        logger.warning("Funding issued history unavailable (%s)", type(exc).__name__)
    add_trend(result, history if mode == "PIT_REPLAY" else issued,
              session_dates=market_sessions(report_date, max(21, sessions)))
    if history:
        reference = history[-1]
        result["replay_reference"] = {key: reference[key] for key in (
            "as_of", "change_20_sessions", "historical_percentile", "trend")}
    result["source_status"] = archive.get("source_status", {})
    archive_path = output_dir / "core_funding_inputs" / f"{archive['digest']}.json"
    if not archive_path.exists():
        write_json_atomic(archive_path, archive)
    result["archive_digest"] = archive["digest"]
    result["archive_path"] = str(archive_path)
    try:
        if context_archive is None:
            context_archive = collect_context(fred_api_key, output_dir / "core_funding_context_cache", refresh)
        result["supplemental_context"] = build_context(context_archive, store.rows, cutoff)
        context_path = output_dir / "core_funding_context_inputs" / f"{context_archive['digest']}.json"
        if not context_path.exists():
            write_json_atomic(context_path, context_archive)
        result["supplemental_context"]["archive_path"] = str(context_path)
    except Exception as exc:
        result["supplemental_context"] = {"status": "UNAVAILABLE", "score_eligible": False}
        logger.warning("Optional funding context unavailable (%s)", type(exc).__name__)
    replay_status = None
    if write_replay and client is not None:
        failed = False
        for row in history:
            row["archive_digest"] = archive["digest"]
            failed |= persist_result(client, row)["persistence_status"] == "FAILED"
        replay_status = "FAILED" if failed else "VERIFIED"
    result = persist_result(client, result)
    if replay_status is not None:
        result["replay_write_status"] = replay_status
    if prices is None:
        try:
            prices = fetch_adjusted_benchmarks(str(dates[0].date()), report_date)
        except Exception as exc:
            logger.warning("Funding benchmarks unavailable (%s)", type(exc).__name__)
            prices = pd.DataFrame()
    chart = None
    try:
        chart = generate_core_funding_chart(
            result, history, prices, output_path=output_dir / f"core_funding_{report_date}.png")
    except Exception as exc:
        logger.warning("Funding chart unavailable (%s)", type(exc).__name__)
    try:
        m2_transmission = build_m2_transmission(store.rows, result["as_of"])
    except Exception as exc:
        m2_transmission = {"status": "UNAVAILABLE", "scope": "US_M2_ONLY", "score_eligible": False}
        logger.warning("M2 transmission clock unavailable (%s); funding score is unchanged", type(exc).__name__)
    diagnostics_chart = None
    try:
        diagnostics = build_diagnostics(result, history if mode == "PIT_REPLAY" else issued,
                                        store, credit_archive)
        credit_path = output_dir / "core_funding_credit_inputs" / f"{credit_archive['digest']}.json"
        if not credit_path.exists():
            write_json_atomic(credit_path, credit_archive)
        diagnostics["credit_archive_path"] = str(credit_path)
        diagnostics["core_archive_digest"] = archive["digest"]
    except Exception as exc:
        diagnostics = {"status": "UNAVAILABLE", "version": DIAGNOSTICS_VERSION,
                       "score_eligible": False, "decision_eligible": False}
        logger.warning("Optional funding diagnostics unavailable (%s)", type(exc).__name__)
    if diagnostics.get("status") != "UNAVAILABLE":
        try:
            diagnostics_chart = generate_diagnostics_chart(
                result, diagnostics, output_dir / f"core_funding_diagnostics_{report_date}.png")
        except Exception as exc:
            logger.warning("Optional funding diagnostics chart unavailable (%s)", type(exc).__name__)
    write_json_atomic(output_dir / f"core_funding_{report_date}.json", {
        "result": result, "history": history, "m2_transmission": m2_transmission,
        "diagnostics": diagnostics})
    summary = format_core_funding(result) + "\n\n" + format_m2_transmission(
        m2_transmission, scoring_component=result["components"].get("m2_yoy"))
    summary += "\n\n" + format_diagnostics(diagnostics)
    summary += "\n\n" + format_context(result.get("supplemental_context", {"status": "UNAVAILABLE"}))
    if diagnostics_chart is None:
        summary += "\n附加诊断图不可用；原主图及核心评分不受影响。"
    if chart is None:
        summary += "\nPNG图表生成失败；本次仅有文本与JSON结果。"
    return {"result": result, "history": history, "chart": chart, "m2_transmission": m2_transmission,
            "diagnostics": diagnostics, "diagnostics_chart": diagnostics_chart,
            "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent core funding index (shadow, dry-run by default)")
    parser.add_argument("--as-of", help="Timezone-aware historical cutoff, e.g. 2026-09-24T16:45:00-04:00")
    parser.add_argument("--archive", help="Replay an archived JSON input without fetching FRED")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-prices", action="store_true")
    parser.add_argument("--write", action="store_true", help="Persist daily snapshot after applying migration")
    parser.add_argument("--write-replay", action="store_true", help="Also persist PIT_REPLAY history; requires --write")
    args = parser.parse_args(argv)
    if args.write_replay and not args.write:
        parser.error("--write-replay requires --write")
    client = None
    if args.write:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            parser.error("Supabase credentials are required for --write")
        client = create_client(url, key)
    archive = json.loads(Path(args.archive).read_text()) if args.archive else None
    key = os.environ.get("FRED_API_KEY")
    output = run_core_funding(key, client, args.output_dir, as_of=args.as_of, archive=archive,
                              prices=pd.DataFrame() if args.no_prices else None,
                              sessions=args.sessions, refresh=args.refresh, write_replay=args.write_replay)
    print(output["summary"])
    print(f"Chart: {output['chart']}")
    if args.write and (output["result"]["persistence_status"] != "VERIFIED"
                       or output["result"].get("replay_write_status") == "FAILED"):
        raise SystemExit("Funding persistence failed; inspect migration and credentials")
    if output["result"]["score"] is None:
        raise SystemExit("Funding index unavailable: inspect component coverage above")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    main()
