"""Reconstruct display-only histories without replacing issued daily scores.

Default is dry run. --write adds a hashed reference to existing temporal JSON
details, with a backup and compare-and-set readback. No migration is required.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

import pandas as pd

import environment_indices as env
from liquidity_digest.history_reference import package, digest
from liquidity_monitor import build_database_frame, merge_official_frame, score_liquidity_frame
from liquidity_sources import OfficialLiquiditySources
from risk_capital_ladder import ALL_MEMBERS, CALC_VERSION as CAPITAL_VERSION, aggregate_risk_capital
from temporal_monitor import fetch_panel_histories, build_temporal_history


def restore_breadth_lineage(history, market_rows):
    """Resolve an archived original capture, only inside this non-PIT replay."""
    frame, aliases = history.copy(), []
    for row in market_rows:
        recovery = (row.get("full_metrics") or {}).get("breadth_log_recovery") or {}
        day = row["record_date"]
        archive = recovery.get("archive_sha256", "")
        if (recovery.get("basis") != "original_500_stock_snapshot_not_recomputed"
                or recovery.get("source_date") != day or len(archive) != 64):
            continue
        for metric in ("pct_20ma", "pct_200ma"):
            selected = frame[(frame.report_date == day) & (frame.metric == metric)
                             & (frame.scope == "MACRO")]
            if len(selected) != 1:
                continue
            index, point = next(selected.iterrows())
            if (point.get("source_name") != "archived_auto_review_log / " + archive
                    or point.get("value") != recovery.get("values", {}).get(metric)):
                continue
            prior = frame[(frame.metric == metric) & (frame.scope == "MACRO")
                          & (frame.report_date < day) & (frame.source_name == "market_history")
                          & (frame.calc_version == point.calc_version)]
            if prior.empty:
                continue
            frame.at[index, "source_name"] = "market_history"
            # Use only documented original top-500 aggregates for this missing
            # day's baseline. The old metric wrapper version is not raw lineage.
            baseline = {}
            for old in market_rows:
                full = old.get("full_metrics") or {}
                value = full.get(metric)
                if (old["record_date"] <= day and full.get("breadth_sample") == "top500_by_mktcap"
                        and full.get("breadth_sample_size") == 500
                        and isinstance(value, (int, float)) and not isinstance(value, bool)
                        and 0 <= value <= 100):
                    baseline[old["record_date"]] = float(value)
            baseline[day] = float(point["value"])
            values = [baseline[d] for d in sorted(baseline)[-env.BASELINE:]]
            if len(values) >= env.MIN_EFFECTIVE_OBS:
                frame.at[index, "percentile"] = round(sum(v < point["value"] for v in values) / len(values) * 100, 1)
                frame.at[index, "effective_obs_count"] = len(values)
                frame.at[index, "sample_len"] = len(values)
            aliases.append({"date": day, "metric": metric, "archive_sha256": archive,
                            "original_source_name": point.source_name,
                            "basis": recovery["basis"], "verified_native_count": len(values),
                            "point_in_time_eligible": False})
    return frame, aliases


def environment_rows(history, dates):
    rows = []
    for day in dates:
        frame = env.rebuild_latest_statistics(history, day)
        scores, coverage, maturity, diagnostics = env.score_frame_details(frame)
        signature = env.ENV_CALC_VERSION + ":" + ";".join(
            item["metric"] + "=" + ",".join(item.get("source_identity", []))
            for key in env.CORE_INDEX_KEYS for item in diagnostics[key])
        rows.append({"report_date": day, "source_date": day,
                     "composite": env.composite_score(scores, coverage, maturity=maturity),
                     "coverage": sum(coverage[k] for k in env.CORE_INDEX_KEYS) / len(env.CORE_INDEX_KEYS),
                     "calc_version": env.ENV_CALC_VERSION, "state": "历史重算参考",
                     "details": {"comparison_signature": signature, "diagnostics": diagnostics}})
    return rows


def capital_rows(summaries, members):
    by_day = defaultdict(list)
    for row in members:
        if (row.get("calc_version") == CAPITAL_VERSION and row.get("quality") == "OK"
                and row.get("decision_eligible") is True
                and row.get("source_date") == row.get("report_date")):
            by_day[row["report_date"]].append(row)
    result = []
    for row in summaries:
        day = row["report_date"]
        evidence = by_day[day]
        if len(evidence) != len(ALL_MEMBERS) or {r["ticker"] for r in evidence} != set(ALL_MEMBERS):
            continue
        benchmark = {"return_1d_pct": row.get("qqq_return_1d_pct"),
                     "return_5d_pct": row.get("qqq_return_5d_pct"), "source_date": day}
        rebuilt = aggregate_risk_capital(evidence, benchmark, day)
        if rebuilt["score"] != row.get("score") or rebuilt["coverage"] != 1:
            continue
        result.append(rebuilt)
    return result


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--liquidity-frame", help="Trusted private audit snapshot, not an uploaded/user pickle")
    p.add_argument("--liquidity-frame-sha256")
    args = p.parse_args()
    os.umask(0o077)
    folder = Path(args.backup_dir)
    folder.mkdir(parents=True, exist_ok=False)
    cfg = json.loads(Path(args.config).read_text())
    from supabase import create_client
    client = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
    end = args.as_of
    start = str(pd.Timestamp(end).date() - timedelta(days=1200))
    sources = fetch_panel_histories(client, str(pd.Timestamp(end).date() - timedelta(days=150)), end)
    save(folder / "source_panels.json", sources)
    history = env._fetch_history(client, end)
    history.to_json(folder / "environment_metrics.json", orient="records")
    history, aliases = restore_breadth_lineage(history, sources["tactical_stress"])
    dates = sorted(set(history.report_date))[-65:]
    replay = {"environment": environment_rows(history, dates)}
    if args.liquidity_frame:
        archive = Path(args.liquidity_frame)
        if not args.liquidity_frame_sha256 or sha256(archive.read_bytes()).hexdigest() != args.liquidity_frame_sha256:
            raise ValueError("Private input snapshot hash mismatch")
        frame = pd.read_pickle(archive)
    else:
        official = OfficialLiquiditySources(cfg["FRED_API_KEY"]).build_frame(start, end)
        database = build_database_frame(client, start, end)
        frame = merge_official_frame(database, official)
    frame.to_pickle(folder / "liquidity_frame.pkl")
    replay["liquidity"] = [score_liquidity_frame(frame.loc[:day], day).to_row() for day in dates]
    members, offset = [], 0
    while True:
        page = (client.table("risk_capital_member_daily").select("*")
                .gte("report_date", dates[0]).lte("report_date", end)
                .order("report_date").order("ticker").range(offset, offset + 999).execute()).data
        if not page:
            break
        members.extend(page)
        offset += len(page)
    save(folder / "capital_members.json", members)
    replay["risk_capital"] = capital_rows(sources["risk_capital"], members)
    replay["tactical_stress"] = sources["tactical_stress"]
    temporal = build_temporal_history(replay, observation_mode="REPLAY")
    now = datetime.now(timezone.utc).isoformat()
    bundles, acceptance = {}, {}
    for panel in replay:
        rows = [r for r in temporal if r["panel"] == panel]
        if not rows:
            continue
        provenance = {"source_rows_sha256": digest(replay[panel]), "raw_archive": str(folder),
                      "method": "original_formula_prefix_only_no_fill",
                      "liquidity_input_sha256": args.liquidity_frame_sha256 if panel == "liquidity" else None,
                      "breadth_archive_aliases": aliases if panel == "environment" else []}
        bundles[panel] = package(panel, rows, now, provenance)
        acceptance[panel] = {"rows": len(rows), "valid": sum(r["quality_status"] == "OK" for r in rows),
                             "first": rows[0]["report_date"], "last": rows[-1]["report_date"],
                             "latest_risk_value": rows[-1]["risk_value"]}
    save(folder / "source_reconstructions.json", replay)
    save(folder / "reference_bundles.json", bundles)
    before = (client.table("temporal_state_daily").select("*").eq("report_date", end).execute()).data
    save(folder / "temporal_before.json", before)
    from liquidity_digest.history_reference import compatible
    for panel, bundle in bundles.items():
        old = next(r for r in before if r["panel"] == panel)
        last = bundle["rows"][-1]
        if (last["report_date"] != end or last["risk_value"] != old["risk_value"]
                or not compatible(last, old) or last["quality_status"] != "OK"):
            raise RuntimeError("Current anchor differs: " + panel)
    if args.write:
        for panel, bundle in bundles.items():
            old = next(r for r in before if r["panel"] == panel)
            details = {**old["details"], "history_reference": bundle}
            (client.table("temporal_state_daily").update({"details": details})
             .eq("report_date", end).eq("panel", panel)
             .eq("details", json.dumps(old["details"])).execute())
            actual = (client.table("temporal_state_daily").select("*")
                      .eq("report_date", end).eq("panel", panel).single().execute()).data
            expected = {**old, "details": details}
            if actual != expected:
                raise RuntimeError("Readback mismatch: " + panel)
            acceptance[panel]["write_verified"] = True
    save(folder / "acceptance.json", acceptance)
    print(json.dumps(acceptance, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
