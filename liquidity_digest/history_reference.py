"""Verified historical reconstructions for display, never live signal evidence."""
from copy import deepcopy
from hashlib import sha256
import json

from .model import finite, stamp

VERSION = "digest_history_reference_v1"
NOTE = "历史重算参考，非当时可得信号；不参与预警或宏微观投票"


def digest(rows):
    return sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def package(panel, rows, computed_at, provenance):
    rows = deepcopy(rows)
    if not rows or any(r.get("panel") != panel or r.get("observation_mode") != "REPLAY" for r in rows):
        raise ValueError("Reference must contain explicit replay rows for one panel")
    stamp(computed_at)
    return {"version": VERSION, "panel": panel, "computed_at": computed_at,
            "point_in_time_eligible": False, "alert_eligible": False,
            "basis": "LATEST_VINTAGE_RECONSTRUCTION", "provenance": provenance,
            "rows": rows, "sha256": digest(rows)}


def compatible(left, right):
    a, b = left.get("details") or {}, right.get("details") or {}
    return (left.get("calc_version") == right.get("calc_version")
            and a.get("source_calc_version") == b.get("source_calc_version")
            and a.get("comparison_signature") == b.get("comparison_signature"))


def extract(report, source_rows):
    """Accept only hashed, pre-cutoff bundles anchored to a real live snapshot."""
    from .states import eligible
    result = {}
    if report.get("source_audit", {}).get("db_temporal_state_daily", {}).get("status") != "OK":
        return result
    for panel, live_rows in report["temporal"].items():
        current = next((r for r in live_rows if r["report_date"] == report["market_date"]), None)
        if not current or not eligible(current, panel):
            continue
        candidates = []
        for holder in source_rows:
            bundle = (holder.get("details") or {}).get("history_reference")
            if holder.get("panel") != panel or not isinstance(bundle, dict):
                continue
            try:
                rows = bundle["rows"]
                when = stamp(bundle["computed_at"])
                if (bundle.get("version") != VERSION or bundle.get("panel") != panel
                        or bundle.get("point_in_time_eligible") is not False
                        or bundle.get("alert_eligible") is not False
                        or bundle.get("basis") != "LATEST_VINTAGE_RECONSTRUCTION"
                        or when > stamp(report["cutoff"]) or not rows
                        or digest(rows) != bundle.get("sha256")):
                    continue
                indexed = {r["report_date"]: r for r in rows}
                anchor = indexed[holder["report_date"]]
                if (len(indexed) != len(rows) or max(indexed) != holder["report_date"]
                        or not eligible(holder, panel) or not compatible(anchor, holder)
                        or not compatible(anchor, current)
                        or finite(anchor.get("risk_value")) != finite(holder.get("risk_value"))):
                    continue
                if any(r.get("panel") != panel or r.get("observation_mode") != "REPLAY"
                       or stamp(r["computed_at"]) > when for r in rows):
                    continue
                # A later actual snapshot supersedes a replay point, including an invalid one.
                for live in live_rows:
                    if live["report_date"] >= holder["report_date"]:
                        if not compatible(live, anchor):
                            raise ValueError("Comparison definition changed")
                        clean = deepcopy(live)
                        clean["details"].pop("history_reference", None)
                        clean["observation_mode"] = "REPLAY"
                        clean["details"]["comparison_start_date"] = anchor["details"].get("comparison_start_date", "")
                        indexed[live["report_date"]] = clean
                candidates.append((when, [indexed[d] for d in sorted(indexed)]))
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        if candidates:
            result[panel] = max(candidates, key=lambda c: c[0])[1]
    return result


def attach(report, source_rows):
    from .states import panel_state
    report["reference_history"] = extract(report, source_rows)
    display = {}
    for panel, actual in report["states"]["panels"].items():
        reference = panel_state(report, panel, reference=True)
        reference.update(history_basis="RECONSTRUCTED_REFERENCE", reason=NOTE + "；" + reference["reason"])
        report["states"]["panels"][panel]["history_reference"] = reference
        display[panel] = reference if actual["direction"] == "UNKNOWN" and reference["direction"] != "UNKNOWN" else actual
    report["states"]["display_panels"] = display
    report["states"]["display_pressure"] = display[report["states"]["pressure"]["panel"]]
